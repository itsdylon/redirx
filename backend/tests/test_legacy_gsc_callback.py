"""Historical consent uses the shared one-use account epoch authority."""
import hashlib
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from backend.services.gsc_service import GSCError
from backend.tests import test_legacy_gsc_disconnect as fixture
from backend.tests import test_legacy_gsc_refresh as refresh
from backend.tests.test_migration_planning import A, B
from backend.tests.helpers.pivot_native_client import json_value


class CallbackQuery(refresh.ConnectionQuery):
    def upsert(self, value, on_conflict):
        self.payload=value; self.conflict=on_conflict; self.mode='upsert'; return self

    def execute(self):
        if self.mode!='upsert': return super().execute()
        keys=list(self.payload)
        statement=sql.SQL('INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {} RETURNING *').format(
            sql.Identifier(self.table_name),sql.SQL(',').join(map(sql.Identifier,keys)),
            sql.SQL(',').join(sql.Placeholder() for _ in keys),sql.Identifier(self.conflict),
            sql.SQL(',').join(sql.SQL('{}=excluded.{}').format(sql.Identifier(k),sql.Identifier(k)) for k in keys))
        with psycopg.connect(self.client.dsn,row_factory=dict_row) as conn:
            conn.execute('SET ROLE service_role')
            rows=conn.execute(statement,list(self.payload.values())).fetchall()
        return SimpleNamespace(data=json_value(rows),error=None)


class CallbackClient(refresh.ConnectionClient):
    def table(self,name): return CallbackQuery(self,table=name)


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'),'requires disposable loopback PostgreSQL')
class LegacyGSCCallback(unittest.TestCase):
    cleanup_db=classmethod(fixture.LegacyGSCDisconnect.cleanup_db.__func__)
    @classmethod
    def setUpClass(cls):
        fixture.LegacyGSCDisconnect.setUpClass.__func__(cls)
        with psycopg.connect(cls.dsn) as conn:
            # Historical 033 service-role authority, omitted by agent-only fixture.
            conn.execute('GRANT INSERT ON gsc_connections TO service_role')
    current=fixture.LegacyGSCDisconnect.current

    def setUp(self):
        from src.redirx.config import Config
        signing = patch.object(Config, 'GSC_STATE_SECRET', 'fixture-only-state-signing-key-0000000000')
        signing.start(); self.addCleanup(signing.stop)
        fixture.LegacyGSCDisconnect.setUp(self)
        self.service.connection_db.client=CallbackClient(self.dsn)

    def state(self):
        return parse_qs(urlsplit(self.service.build_authorization_url(A,'/review/fixture')).query)['state'][0]

    def tokens(self, name='stale'):
        return SimpleNamespace(ok=True,json=lambda:{'access_token':name+'-access','refresh_token':name+'-refresh','expires_in':3600})

    def disconnect(self):
        self.service.connection_db.client.rpc('disconnect_legacy_gsc_connection',{'p_user_id':A}).execute()

    def test_consent_issued_before_disconnect_cannot_restore_credentials(self):
        state=self.state(); self.disconnect()
        with patch('backend.services.gsc_service.requests.post',return_value=self.tokens()) as post:
            try: self.service.handle_callback('fixture-code',state)
            except GSCError: pass
        self.assertIsNone(self.current(),'older consent resurrected disconnected credentials')
        post.assert_not_called()
        self.assertEqual(self.current(B),self.other)

    def test_disconnect_during_exchange_fences_late_token_write(self):
        state=self.state()
        def exchange(*args,**kwargs):
            self.disconnect()
            return self.tokens()
        with patch('backend.services.gsc_service.requests.post',side_effect=exchange):
            try: self.service.handle_callback('fixture-code',state)
            except GSCError: pass
        self.assertIsNone(self.current(),'late token exchange resurrected disconnected credentials')
        self.assertEqual(self.current(B),self.other)

    def test_newer_consent_wins_over_older_callback(self):
        old=self.state(); new=self.state()
        with patch('backend.services.gsc_service.requests.post',return_value=self.tokens('new')):
            self.service.handle_callback('new-code',new)
        with patch('backend.services.gsc_service.requests.post',return_value=self.tokens('old')) as post:
            try: self.service.handle_callback('old-code',old)
            except GSCError: pass
        self.assertEqual(self.current()['refresh_token'],'new-refresh','old callback replaced newer consent')
        post.assert_not_called()
        self.assertEqual(self.current(B),self.other)

    def operation(self, state):
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return conn.execute('SELECT * FROM gsc_agent_operations WHERE state_hash=%s',
                [hashlib.sha256(state.encode()).hexdigest()]).fetchone()

    def test_same_second_states_are_distinct_and_only_hash_is_stored(self):
        from backend.services.gsc_service import verify_state_token
        from datetime import datetime, timezone
        moment=datetime.now(timezone.utc)
        with patch('backend.services.gsc_service.datetime') as clock:
            clock.now.return_value=moment
            first=self.state(); second=self.state()
        a=verify_state_token(first); b=verify_state_token(second)
        self.assertEqual(a['iat'],b['iat'])
        self.assertNotEqual(first,second)
        self.assertNotEqual(a['jti'],b['jti'])
        self.assertEqual(a['return_to'],b['return_to'])
        for state in (first,second):
            operation=self.operation(state)
            self.assertEqual(operation['state_hash'],hashlib.sha256(state.encode()).hexdigest())
            self.assertNotIn(state,str(operation))
        self.assertGreater(self.operation(second)['epoch'],self.operation(first)['epoch'])

    def test_rightful_success_is_one_use_and_denial_consumes_without_provider(self):
        state=self.state()
        with patch('backend.services.gsc_service.requests.post',return_value=self.tokens('rightful')) as post:
            result=self.service.handle_callback('fixture-code',state)
            self.assertEqual(result['consent_result'],'connected')
            self.assertEqual(result['return_to'],'/review/fixture')
            with self.assertRaises(GSCError): self.service.handle_callback('other-code',state)
            post.assert_called_once()
        self.assertEqual(self.current()['refresh_token'],'rightful-refresh')
        with psycopg.connect(self.dsn) as conn:
            self.assertEqual(conn.execute('SELECT state FROM gsc_agent_accounts WHERE user_id=%s',[A]).fetchone()[0],'connected')
        denied=self.state()
        with patch('backend.services.gsc_service.requests.post') as post:
            self.assertEqual(self.service.handle_callback(None,denied,'access_denied')['consent_result'],'cancelled')
            with self.assertRaises(GSCError): self.service.handle_callback('late-code',denied)
            post.assert_not_called()
        self.assertEqual(self.operation(denied)['status'],'cancelled')
        self.assertEqual(self.current()['refresh_token'],'rightful-refresh')
        self.assertEqual(self.current(B),self.other)

    def test_unrepresentable_provider_expiry_finishes_failed_without_publishing(self):
        for expiry in (10 ** 30, float('inf')):
            with self.subTest(expiry=expiry):
                self.setUp(); state=self.state()
                response=SimpleNamespace(ok=True,json=lambda:{
                    'access_token':'fixture-new-access','refresh_token':'fixture-new-refresh',
                    'expires_in':expiry})
                with patch('backend.services.gsc_service.requests.post',return_value=response) as post:
                    with self.assertRaises(GSCError) as raised:
                        self.service.handle_callback('fixture-code',state)
                    self.assertEqual(raised.exception.code,'gsc_token_exchange_failed')
                    post.assert_called_once()
                self.assertEqual(self.operation(state)['status'],'failed')
                self.assertEqual(self.current()['refresh_token'],'original-refresh')
                self.assertEqual(self.current(B),self.other)

    def test_explicit_missing_or_malformed_scope_never_publishes_connection(self):
        read_scope = 'https://www.googleapis.com/auth/webmasters.readonly'
        for scope in ('openid email', '', ' ', None, False, [read_scope],
                      read_scope + '.unexpected', read_scope.upper()):
            with self.subTest(scope=scope):
                self.setUp(); before=self.current(); state=self.state()
                response=SimpleNamespace(ok=True,json=lambda:{
                    'access_token':'unusable-access','refresh_token':'unusable-refresh',
                    'expires_in':3600,'scope':scope})
                with patch('backend.services.gsc_service.requests.post',return_value=response) as post:
                    with self.assertRaises(GSCError) as raised:
                        self.service.handle_callback('fixture-code',state)
                    self.assertEqual(raised.exception.code,'gsc_missing_scope')
                    post.assert_called_once()
                self.assertEqual(self.operation(state)['status'],'failed')
                self.assertEqual(self.current(),before)
                self.assertEqual(self.current(B),self.other)
                with patch('backend.services.gsc_service.requests.post') as post:
                    with self.assertRaises(GSCError):
                        self.service.handle_callback('replayed-code',state)
                    post.assert_not_called()

    def test_granted_scope_is_preserved_and_only_absent_scope_defaults_to_request(self):
        from backend.services.gsc_service import GSC_SCOPES
        read_scope = 'https://www.googleapis.com/auth/webmasters.readonly'
        for scope in (None, read_scope, 'email ' + read_scope + ' openid'):
            with self.subTest(scope=scope):
                self.setUp(); state=self.state()
                tokens={'access_token':'granted-access','refresh_token':'granted-refresh','expires_in':3600}
                if scope is not None: tokens['scope']=scope
                with patch('backend.services.gsc_service.requests.post',
                           return_value=SimpleNamespace(ok=True,json=lambda:tokens)) as post:
                    result=self.service.handle_callback('fixture-code',state)
                    post.assert_called_once()
                self.assertEqual(result['consent_result'],'connected')
                self.assertEqual(self.current()['scopes'],scope if scope is not None else GSC_SCOPES)
                self.assertEqual(self.operation(state)['status'],'succeeded')
                self.assertEqual(self.current(B),self.other)

    def test_expired_and_pre_rollout_stateless_links_never_exchange(self):
        import jwt
        from src.redirx.config import Config
        from backend.services.gsc_service import verify_state_token
        state=self.state()
        with psycopg.connect(self.dsn) as conn:
            conn.execute("UPDATE gsc_agent_operations SET expires_at=now()-interval '1 second'")
        old=verify_state_token(state); old.pop('jti')
        legacy=jwt.encode(old,Config.GSC_STATE_SECRET,algorithm='HS256')
        with patch('backend.services.gsc_service.requests.post') as post:
            for invalid in (state,legacy):
                with self.assertRaises(GSCError): self.service.handle_callback('fixture-code',invalid)
            post.assert_not_called()
        self.assertEqual(self.current()['refresh_token'],'original-refresh')

    def test_stale_success_or_error_cannot_overwrite_later_connection(self):
        for ok in (True,False):
            with self.subTest(ok=ok):
                self.setUp(); old=self.state()
                def delayed_old_reply(*args,**kwargs):
                    new=self.state()
                    with patch('backend.services.gsc_service.requests.post',return_value=self.tokens('newer')):
                        self.service.handle_callback('new-code',new)
                    return self.tokens('old') if ok else SimpleNamespace(ok=False,text='private-provider-body')
                with patch('backend.services.gsc_service.requests.post',side_effect=delayed_old_reply), patch(
                        'backend.services.gsc_service.logger') as logger:
                    with self.assertRaises(GSCError): self.service.handle_callback('old-code',old)
                    logger.warning.assert_not_called()
                self.assertEqual(self.current()['refresh_token'],'newer-refresh')
                with psycopg.connect(self.dsn) as conn:
                    self.assertEqual(conn.execute('SELECT state FROM gsc_agent_accounts WHERE user_id=%s',[A]).fetchone()[0],'connected')

    def test_agent_connect_supersedes_historical_and_reverse(self):
        from uuid import uuid4
        old=self.state()
        with psycopg.connect(self.dsn) as conn:
            conn.execute("SELECT reserve_gsc_agent_operation(%s,'connect',%s,'{}',%s)",
                [A,uuid4().hex,'a'*64]).fetchone()[0]
        with patch('backend.services.gsc_service.requests.post') as post:
            with self.assertRaises(GSCError): self.service.handle_callback('old-code',old)
            post.assert_not_called()
        newest=self.state()
        with psycopg.connect(self.dsn) as conn:
            with self.assertRaises(psycopg.Error): conn.execute('SELECT consume_gsc_agent_state(%s)',['a'*64])
        with patch('backend.services.gsc_service.requests.post',return_value=self.tokens('newest')):
            self.service.handle_callback('new-code',newest)
        self.assertEqual(self.current()['refresh_token'],'newest-refresh')

    def test_consume_and_finish_lost_ack_do_not_automatically_repeat_exchange(self):
        for boundary in ('consume_gsc_agent_state','finish_gsc_agent_operation'):
            with self.subTest(boundary=boundary):
                self.setUp(); state=self.state(); client=self.service.connection_db.client; original=client.rpc; calls=[]
                def rpc(name,params):
                    query=original(name,params)
                    if name!=boundary: return query
                    def execute():
                        calls.append(name); query.execute(); raise RuntimeError('private transport details')
                    return SimpleNamespace(execute=execute)
                with patch.object(client,'rpc',side_effect=rpc), patch(
                        'backend.services.gsc_service.requests.post',return_value=self.tokens('committed')) as post:
                    with self.assertRaises(GSCError) as raised: self.service.handle_callback('fixture-code',state)
                    self.assertEqual(raised.exception.code,'gsc_connection_unconfirmed')
                    self.assertEqual(raised.exception.status_code,503)
                    self.assertEqual(len(calls),1)
                    self.assertEqual(post.call_count,0 if boundary.startswith('consume') else 1)
                with patch('backend.services.gsc_service.requests.post') as post:
                    with self.assertRaises(GSCError): self.service.handle_callback('retry-code',state)
                    post.assert_not_called()
                expected='original-refresh' if boundary.startswith('consume') else 'committed-refresh'
                self.assertEqual(self.current()['refresh_token'],expected)
                self.assertEqual(self.current(B),self.other)

    def test_wrong_owner_or_protocol_receipt_never_reaches_provider(self):
        for key,value in [('user_id',B),('request',{'protocol':'agent_gsc_oauth'}),('epoch',True)]:
            with self.subTest(key=key):
                self.setUp(); state=self.state(); client=self.service.connection_db.client; original=client.rpc
                def rpc(name,params):
                    query=original(name,params)
                    if name!='consume_gsc_agent_state': return query
                    def execute():
                        receipt=query.execute(); receipt.data[key]=value; return receipt
                    return SimpleNamespace(execute=execute)
                with patch.object(client,'rpc',side_effect=rpc), patch('backend.services.gsc_service.requests.post') as post:
                    with self.assertRaises(GSCError): self.service.handle_callback('fixture-code',state)
                    post.assert_not_called()
                self.assertEqual(self.current()['refresh_token'],'original-refresh')
                self.assertEqual(self.current(B),self.other)

    def test_browser_success_denial_and_stale_callback_keep_safe_redirect(self):
        from flask import Flask
        from backend.routes.gsc_routes import gsc_blueprint
        from src.redirx.config import Config
        app=Flask(__name__); app.register_blueprint(gsc_blueprint,url_prefix='/api/gsc')
        with patch('backend.routes.gsc_routes.GSCService',return_value=self.service), patch.object(
                Config,'APP_BASE_URL','https://app.example.invalid'):
            state=self.state()
            with patch('backend.services.gsc_service.requests.post',return_value=self.tokens('browser')):
                result=app.test_client().get('/api/gsc/callback',query_string={'state':state,'code':'fixture-code','user_id':B})
            self.assertEqual(result.status_code,302)
            self.assertEqual(result.location,'https://app.example.invalid/review/fixture?gsc=connected')
            self.assertEqual(self.current(B),self.other)
            denied=self.state()
            with patch('backend.services.gsc_service.requests.post') as post:
                result=app.test_client().get('/api/gsc/callback',query_string={'state':denied,'error':'access_denied'})
                self.assertEqual(result.location,'https://app.example.invalid/review/fixture?gsc=cancelled')
                self.disconnect()
                result=app.test_client().get('/api/gsc/callback',query_string={'state':state,'code':'late-code'})
                self.assertEqual(result.location,'https://app.example.invalid/?gsc=error')
                post.assert_not_called()

    def test_browser_unknown_ack_redirects_to_status_readback_for_committed_and_uncommitted_connection(self):
        from flask import Flask
        from backend.routes.gsc_routes import gsc_blueprint
        from src.redirx.config import Config
        app=Flask(__name__); app.register_blueprint(gsc_blueprint,url_prefix='/api/gsc')
        for boundary in ('consume_gsc_agent_state','finish_gsc_agent_operation'):
            with self.subTest(boundary=boundary):
                self.setUp(); self.disconnect(); state=self.state()
                client=self.service.connection_db.client; original=client.rpc; calls=[]
                def rpc(name,params):
                    query=original(name,params)
                    if name!=boundary: return query
                    def execute():
                        calls.append(name); query.execute(); raise RuntimeError('private transport details')
                    return SimpleNamespace(execute=execute)
                with patch.object(client,'rpc',side_effect=rpc), patch(
                        'backend.routes.gsc_routes.GSCService',return_value=self.service), patch.object(
                        Config,'APP_BASE_URL','https://app.example.invalid'), patch(
                        'backend.services.gsc_service.requests.post',return_value=self.tokens('committed')) as post:
                    result=app.test_client().get('/api/gsc/callback',query_string={'state':state,'code':'fixture-code'})
                    self.assertEqual(result.location,'https://app.example.invalid/review/fixture?gsc=unconfirmed')
                    self.assertEqual(len(calls),1)
                    self.assertEqual(post.call_count,0 if boundary.startswith('consume') else 1)
                self.assertEqual(self.service.get_status(A)['connected'],boundary.startswith('finish'))
                self.assertNotIn('private',result.get_data(as_text=True))

    def test_reservation_lost_ack_never_reveals_a_consent_url_or_retries(self):
        client=self.service.connection_db.client; original=client.rpc; calls=[]
        def rpc(name,params):
            query=original(name,params)
            def execute():
                calls.append(name); query.execute(); raise RuntimeError('private transport details')
            return SimpleNamespace(execute=execute)
        with patch.object(client,'rpc',side_effect=rpc), patch('backend.services.gsc_service.requests.post') as post:
            with self.assertRaises(GSCError) as raised: self.state()
            self.assertEqual(raised.exception.code,'gsc_connection_unconfirmed')
            self.assertEqual(calls,['reserve_gsc_agent_operation'])
            post.assert_not_called()
        with psycopg.connect(self.dsn) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM gsc_agent_operations WHERE user_id=%s',[A]).fetchone()[0],1)
        self.assertEqual(self.current()['refresh_token'],'original-refresh')

    def test_stale_provider_error_and_deleted_account_cannot_restore_or_publish(self):
        state=self.state()
        def failed_exchange(*args,**kwargs):
            self.disconnect()
            return SimpleNamespace(ok=False,text='private provider body')
        with patch('backend.services.gsc_service.requests.post',side_effect=failed_exchange), patch(
                'backend.services.gsc_service.logger') as logger:
            with self.assertRaises(GSCError): self.service.handle_callback('fixture-code',state)
            logger.warning.assert_not_called()
        self.assertIsNone(self.current())
        state=self.state()
        with psycopg.connect(self.dsn) as conn:
            conn.execute('DELETE FROM gsc_agent_accounts WHERE user_id=%s',[A])
        with patch('backend.services.gsc_service.requests.post') as post:
            with self.assertRaises(GSCError): self.service.handle_callback('deleted-code',state)
            post.assert_not_called()
        self.assertIsNone(self.current())
        self.assertEqual(self.current(B),self.other)
