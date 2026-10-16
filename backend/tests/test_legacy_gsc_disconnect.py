"""Historical disconnect must commit locally before best-effort provider revoke."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
import requests
from flask import Flask

from backend.services.gsc_service import GSCError
from backend.tests import test_legacy_gsc_refresh as fixture
from backend.tests.test_migration_planning import A, B, ROOT


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'),'requires disposable loopback PostgreSQL')
class LegacyGSCDisconnect(unittest.TestCase):
    cleanup_db=classmethod(fixture.LegacyGSCRefresh.cleanup_db.__func__)
    current=fixture.LegacyGSCRefresh.current
    replace=fixture.LegacyGSCRefresh.replace

    @classmethod
    def setUpClass(cls):
        fixture.LegacyGSCRefresh.setUpClass.__func__(cls)
        with psycopg.connect(cls.dsn) as conn:
            conn.execute((ROOT/'database/migrations/068_atomic_legacy_gsc_disconnect.sql').read_text())

    def setUp(self):
        with psycopg.connect(self.dsn) as conn:
            conn.execute('DELETE FROM gsc_agent_accounts')
        fixture.LegacyGSCRefresh.setUp(self)

    def reconnect(self):
        with psycopg.connect(self.dsn) as conn:
            conn.execute('''INSERT INTO gsc_connections(user_id,access_token,refresh_token,google_email)
                VALUES(%s,'replacement-access','replacement-refresh','replacement@example.invalid')
                ON CONFLICT(user_id) DO UPDATE SET access_token=excluded.access_token,
                refresh_token=excluded.refresh_token,google_email=excluded.google_email''',[A])

    def test_reconnect_during_delayed_revoke_survives_return(self):
        observed={}
        def revoke_reply(*args,**kwargs):
            observed['locally_disconnected']=self.current() is None
            self.reconnect()
            return SimpleNamespace(ok=True)
        with patch('backend.services.gsc_service.requests.post',side_effect=revoke_reply) as post:
            self.service.disconnect(A)
        self.assertIsNotNone(self.current(),'newly reconnected row must survive delayed revoke return')
        self.assertEqual(self.current()['refresh_token'],'replacement-refresh')
        self.assertTrue(observed['locally_disconnected'])
        post.assert_called_once()
        self.assertEqual(self.current(B),self.other)

    def test_revocation_uses_form_body_after_positive_local_disconnect(self):
        with patch('backend.services.gsc_service.requests.post',return_value=SimpleNamespace(ok=True)) as post:
            self.service.disconnect(A)
        post.assert_called_once()
        self.assertNotIn('params',post.call_args.kwargs)
        self.assertEqual(post.call_args.kwargs['data'],{'token':'original-refresh'})
        self.assertNotIn('original-refresh',post.call_args.args[0])
        self.assertIsNone(self.current())
        self.assertEqual(self.current(B),self.other)

    def test_later_agent_reconnect_commits_connected_epoch_during_provider_revoke(self):
        def reconnect_while_revoking(*args,**kwargs):
            self.assertIsNone(self.current())
            with psycopg.connect(self.dsn) as conn:
                self.assertEqual(conn.execute('SELECT state FROM gsc_agent_accounts WHERE user_id=%s',[A]).fetchone()[0],'disconnected')
                op=conn.execute("SELECT reserve_gsc_agent_operation(%s,'connect',%s,'{}',%s)",[A,uuid4().hex,'c'*64]).fetchone()[0]
                conn.execute('SELECT consume_gsc_agent_state(%s)',['c'*64])
                result=conn.execute('SELECT finish_gsc_agent_operation(%s,%s,%s,%s,%s)',[
                    A,op['id'],Jsonb({'status':'succeeded'}),'connected',Jsonb({
                        'access_token':'agent-new-access','refresh_token':'agent-new-refresh',
                        'scopes':'https://www.googleapis.com/auth/webmasters.readonly'})]).fetchone()[0]
                self.assertEqual(result['status'],'succeeded')
            return SimpleNamespace(ok=True)
        with patch('backend.services.gsc_service.requests.post',side_effect=reconnect_while_revoking):
            self.service.disconnect(A)
        self.assertEqual(self.current()['refresh_token'],'agent-new-refresh')
        with psycopg.connect(self.dsn) as conn:
            self.assertEqual(conn.execute('SELECT state FROM gsc_agent_accounts WHERE user_id=%s',[A]).fetchone()[0],'connected')
        self.assertEqual(self.current(B),self.other)

    def test_no_credentials_disconnect_still_invalidates_inflight_agent_consent(self):
        with psycopg.connect(self.dsn) as conn:
            conn.execute('DELETE FROM gsc_connections WHERE user_id=%s',[A])
            op=conn.execute("SELECT reserve_gsc_agent_operation(%s,'connect',%s,'{}',%s)",[A,uuid4().hex,'b'*64]).fetchone()[0]
            conn.execute('SELECT consume_gsc_agent_state(%s)',['b'*64])
        with patch('backend.services.gsc_service.requests.post') as post:
            self.service.disconnect(A)
            self.service.disconnect(A)
        post.assert_not_called()
        with psycopg.connect(self.dsn) as conn:
            state,epoch=conn.execute('SELECT state,epoch FROM gsc_agent_accounts WHERE user_id=%s',[A]).fetchone()
            self.assertEqual(state,'disconnected')
            self.assertGreater(epoch,op['epoch'])
            with self.assertRaises(psycopg.Error):
                conn.execute("SELECT finish_gsc_agent_operation(%s,%s,'{\"status\":\"succeeded\"}','connected')",[A,op['id']])
        self.assertIsNone(self.current())
        self.assertEqual(self.current(B),self.other)

    def test_suppressed_delete_rolls_back_account_transition_and_never_revokes(self):
        with psycopg.connect(self.dsn) as conn:
            conn.execute("INSERT INTO gsc_agent_accounts(user_id,state,epoch) VALUES(%s,'connected',7)",[A])
            conn.execute("CREATE FUNCTION fixture_keep_gsc() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$")
            conn.execute('CREATE TRIGGER fixture_keep_gsc BEFORE DELETE ON gsc_connections FOR EACH ROW EXECUTE FUNCTION fixture_keep_gsc()')
        before=self.current()
        try:
            with patch('backend.services.gsc_service.requests.post') as post:
                with self.assertRaises(GSCError) as failed: self.service.disconnect(A)
            self.assertEqual(failed.exception.code,'gsc_disconnect_unconfirmed')
            post.assert_not_called()
            self.assertEqual(self.current(),before)
            with psycopg.connect(self.dsn) as conn:
                self.assertEqual(conn.execute('SELECT state,epoch FROM gsc_agent_accounts WHERE user_id=%s',[A]).fetchone(),('connected',7))
        finally:
            with psycopg.connect(self.dsn) as conn:
                conn.execute('DROP TRIGGER fixture_keep_gsc ON gsc_connections')
                conn.execute('DROP FUNCTION fixture_keep_gsc()')

    def test_suppressed_account_ack_restores_deleted_credentials(self):
        with psycopg.connect(self.dsn) as conn:
            conn.execute("INSERT INTO gsc_agent_accounts(user_id,state,epoch) VALUES(%s,'connected',8)",[A])
            conn.execute("CREATE FUNCTION fixture_keep_gsc_account() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$")
            conn.execute('CREATE TRIGGER fixture_keep_gsc_account BEFORE UPDATE ON gsc_agent_accounts FOR EACH ROW EXECUTE FUNCTION fixture_keep_gsc_account()')
        before=self.current()
        try:
            with patch('backend.services.gsc_service.requests.post') as post:
                with self.assertRaises(GSCError): self.service.disconnect(A)
            post.assert_not_called()
            self.assertEqual(self.current(),before)
            with psycopg.connect(self.dsn) as conn:
                self.assertEqual(conn.execute('SELECT state,epoch FROM gsc_agent_accounts WHERE user_id=%s',[A]).fetchone(),('connected',8))
        finally:
            with psycopg.connect(self.dsn) as conn:
                conn.execute('DROP TRIGGER fixture_keep_gsc_account ON gsc_agent_accounts')
                conn.execute('DROP FUNCTION fixture_keep_gsc_account()')

    def test_lost_commit_ack_is_not_retried_or_revoked_and_browser_reports_readback(self):
        client=self.service.connection_db.client
        original=client.rpc
        def uncertain(name,params):
            query=original(name,params)
            def commit_then_timeout():
                receipt=query.execute().data
                self.assertTrue(receipt['disconnected'])
                self.assertEqual(receipt['deleted_count'],1)
                raise ConnectionError('private-token-or-connection-detail')
            return SimpleNamespace(execute=commit_then_timeout)
        from backend.routes.gsc_routes import gsc_blueprint
        app=Flask(__name__);app.config['TESTING']=True
        app.register_blueprint(gsc_blueprint,url_prefix='/api/gsc')
        with patch.object(client,'rpc',side_effect=uncertain) as rpc, patch(
            'backend.services.gsc_service.requests.post') as revoke, patch(
            'backend.routes.gsc_routes.GSCService',return_value=self.service), patch(
            'backend.services.auth_service.AuthService.verify_token',return_value=SimpleNamespace(id=A)):
            response=app.test_client().post('/api/gsc/disconnect',headers={'Authorization':'Bearer fixture-only'})
        self.assertEqual(response.status_code,503,response.json)
        self.assertEqual(response.json['code'],'gsc_disconnect_unconfirmed')
        self.assertNotIn('private-token',str(response.json))
        self.assertNotIn('original-refresh',str(response.json))
        rpc.assert_called_once();revoke.assert_not_called()
        self.assertIsNone(self.current())
        self.assertEqual(self.service.get_status(A),{'connected':False})
        self.assertEqual(self.current(B),self.other)

    def test_provider_failure_does_not_undo_local_disconnect_or_log_tokens(self):
        with patch('backend.services.gsc_service.requests.post',side_effect=requests.Timeout('private-provider-token')) as post, patch(
            'backend.services.gsc_service.logger') as logger:
            self.service.disconnect(A)
        post.assert_called_once();logger.warning.assert_not_called()
        self.assertIsNone(self.current())
        self.assertEqual(self.current(B),self.other)

    def test_browser_success_never_forwards_secret_bearing_receipt(self):
        from backend.routes.gsc_routes import gsc_blueprint
        app=Flask(__name__);app.config['TESTING']=True
        app.register_blueprint(gsc_blueprint,url_prefix='/api/gsc')
        with patch('backend.services.gsc_service.requests.post',return_value=SimpleNamespace(ok=True)), patch(
            'backend.routes.gsc_routes.GSCService',return_value=self.service), patch(
            'backend.services.auth_service.AuthService.verify_token',return_value=SimpleNamespace(id=A)):
            response=app.test_client().post('/api/gsc/disconnect',headers={'Authorization':'Bearer fixture-only'})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json,{'success':True})
        self.assertIsNone(self.current())

    def test_untrusted_receipt_cannot_trigger_provider_revoke(self):
        for receipt in (None,{}, {'user_id':B,'disconnected':True,'epoch':1,'deleted_count':1,'revocation_token':'foreign-token'},
                        {'user_id':A,'disconnected':True,'epoch':1,'deleted_count':True,'revocation_token':'token'}):
            with self.subTest(receipt=receipt), patch.object(self.service.connection_db.client,'rpc',return_value=SimpleNamespace(
                execute=lambda:SimpleNamespace(data=receipt))), patch('backend.services.gsc_service.requests.post') as post:
                with self.assertRaises(GSCError): self.service.disconnect(A)
                post.assert_not_called()
        self.assertEqual(self.current()['access_token'],'original-access')

    def test_only_service_role_can_execute_secret_bearing_disconnect(self):
        with psycopg.connect(self.dsn) as conn:
            for role in ('anon','authenticated'):
                self.assertFalse(conn.execute("SELECT has_function_privilege(%s,'public.disconnect_legacy_gsc_connection(uuid)','EXECUTE')",[role]).fetchone()[0])
            self.assertTrue(conn.execute("SELECT has_function_privilege('service_role','public.disconnect_legacy_gsc_connection(uuid)','EXECUTE')").fetchone()[0])
        self.assertEqual(self.current(B),self.other)

    def test_disconnect_invalidates_pending_agent_consent(self):
        with psycopg.connect(self.dsn) as conn:
            op=conn.execute("SELECT reserve_gsc_agent_operation(%s,'connect','pending-fixture','{}',%s)",[A,'a'*64]).fetchone()[0]
        with patch('backend.services.gsc_service.requests.post',return_value=SimpleNamespace(ok=True)):
            self.service.disconnect(A)
        with psycopg.connect(self.dsn) as conn:
            state,epoch=conn.execute('SELECT state,epoch FROM gsc_agent_accounts WHERE user_id=%s',[A]).fetchone()
        self.assertEqual(state,'disconnected')
        self.assertGreater(epoch,op['epoch'])
        with psycopg.connect(self.dsn) as conn:
            with self.assertRaises(psycopg.Error):
                conn.execute('SELECT consume_gsc_agent_state(%s)',['a'*64])
        self.assertIsNone(self.current())


if __name__ == '__main__': unittest.main()
