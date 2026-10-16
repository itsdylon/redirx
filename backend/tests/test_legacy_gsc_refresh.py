"""Historical browser refresh must not replace a reconnected shared GSC account."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import psycopg
import requests
from flask import Flask
from psycopg import sql
from psycopg.rows import dict_row

from backend.services.gsc_service import GSCService, GSCError
from backend.tests import test_migration_gsc as fixture
from backend.tests.helpers.pivot_native_client import NativeClient, NativeQuery, json_value
from backend.tests.test_migration_planning import A, B
from src.redirx.database import GSCConnectionDB


class ConnectionQuery(NativeQuery):
    def delete(self): self.mode='delete'; return self
    def execute(self):
        if self.mode != 'delete': return super().execute()
        with psycopg.connect(self.client.dsn,row_factory=dict_row) as conn:
            conn.execute('SET ROLE service_role')
            statement=sql.SQL('DELETE FROM {} WHERE {} RETURNING *').format(
                sql.Identifier(self.table_name),sql.SQL(' AND ').join(
                    sql.SQL('{}=%s').format(sql.Identifier(key)) for key,_,_ in self.filters))
            rows=conn.execute(statement,[value for _,_,value in self.filters]).fetchall()
        return SimpleNamespace(data=json_value(rows),error=None)


class ConnectionClient(NativeClient):
    def table(self,name): return ConnectionQuery(self,table=name)


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'),'requires disposable loopback PostgreSQL')
class LegacyGSCRefresh(unittest.TestCase):
    cleanup_db=classmethod(fixture.PostgreSQLTests.cleanup_db.__func__)

    @classmethod
    def setUpClass(cls):
        fixture.PostgreSQLTests.setUpClass.__func__(cls)
        with psycopg.connect(cls.dsn) as conn:
            # The historical browser store uses service-role update/delete,
            # matching its retained 033 authority, omitted by the agent fixture.
            conn.execute('GRANT UPDATE,DELETE ON gsc_connections TO service_role')

    def setUp(self):
        with psycopg.connect(self.dsn) as conn:
            conn.execute('DELETE FROM gsc_connections')
            for owner in (A,B):
                conn.execute('''INSERT INTO gsc_connections(user_id,access_token,refresh_token,token_expires_at,google_email)
                    VALUES(%s,'original-access','original-refresh','2020-01-01','original@example.invalid')''',[owner])
        self.service=GSCService.__new__(GSCService)
        self.service.connection_db=GSCConnectionDB(ConnectionClient(self.dsn))
        self.service.metrics_db=Mock()
        self.other=self.current(B)

    def current(self,owner=A):
        with psycopg.connect(self.dsn,row_factory=dict_row) as conn:
            return conn.execute('SELECT * FROM gsc_connections WHERE user_id=%s',[owner]).fetchone()

    def replace(self):
        with psycopg.connect(self.dsn) as conn:
            conn.execute("""UPDATE gsc_connections SET access_token='replacement-access',refresh_token='replacement-refresh',
                google_email='replacement@example.invalid',token_expires_at='2030-01-01' WHERE user_id=%s""",[A])

    def test_late_success_cannot_overwrite_reconnected_account_or_return_its_old_token(self):
        def google_reply(*args,**kwargs):
            self.assertEqual(kwargs['data']['refresh_token'],'original-refresh')
            self.replace()
            return SimpleNamespace(ok=True,json=lambda:{'access_token':'stale-access','expires_in':3600})
        with patch('backend.services.gsc_service.requests.post',side_effect=google_reply):
            with self.assertRaises(GSCError) as conflict:
                self.service._get_valid_access_token(A)
        self.assertEqual(conflict.exception.code,'operation_conflict')
        self.assertEqual(self.current()['access_token'],'replacement-access')
        self.assertEqual(self.current()['refresh_token'],'replacement-refresh')
        self.assertEqual(self.current()['google_email'],'replacement@example.invalid')
        self.assertEqual(self.current(B),self.other)

    def test_late_success_after_disconnect_never_returns_stale_access(self):
        def google_reply(*args,**kwargs):
            with psycopg.connect(self.dsn) as conn:
                conn.execute('DELETE FROM gsc_connections WHERE user_id=%s',[A])
            return SimpleNamespace(ok=True,json=lambda:{'access_token':'stale-access','expires_in':3600})
        with patch('backend.services.gsc_service.requests.post',side_effect=google_reply):
            with self.assertRaises(GSCError) as conflict:
                self.service._get_valid_access_token(A)
        self.assertEqual(conflict.exception.code,'operation_conflict')
        self.assertIsNone(self.current())
        self.assertEqual(self.current(B),self.other)

    def test_late_invalid_grant_or_outage_preserves_new_connection_without_logging_body(self):
        for status,payload,expected in [(400,{'error':'invalid_grant'},'gsc_reauth_required'),
                                       (503,{'error':'temporarily_unavailable'},'gsc_api_error')]:
            with self.subTest(status=status):
                self.setUp()
                def google_reply(*args,**kwargs):
                    self.replace()
                    return SimpleNamespace(ok=False,status_code=status,text='private-provider-body',json=lambda:payload)
                with patch('backend.services.gsc_service.requests.post',side_effect=google_reply), patch(
                    'backend.services.gsc_service.logger') as logger:
                    with self.assertRaises(GSCError) as failed:
                        self.service._get_valid_access_token(A)
                self.assertEqual(failed.exception.code,expected)
                self.assertEqual(self.current()['refresh_token'],'replacement-refresh')
                self.assertEqual(self.current()['access_token'],'replacement-access')
                self.assertNotIn('private-provider-body',str(logger.mock_calls))
                self.assertEqual(self.current(B),self.other)

    def test_ordinary_refresh_updates_only_access_and_expiry_with_positive_ack(self):
        with patch('backend.services.gsc_service.requests.post',return_value=SimpleNamespace(ok=True,
            json=lambda:{'access_token':'renewed-access','expires_in':3600})) as post:
            self.assertEqual(self.service._get_valid_access_token(A),'renewed-access')
        post.assert_called_once()
        row=self.current()
        self.assertEqual(row['access_token'],'renewed-access')
        self.assertEqual(row['refresh_token'],'original-refresh')
        self.assertEqual(row['google_email'],'original@example.invalid')
        self.assertGreater(row['token_expires_at'],self.other['token_expires_at'])
        self.assertEqual(self.current(B),self.other)

    def test_transient_malformed_and_revoked_results_preserve_existing_credentials(self):
        before=self.current()
        responses=[
            (SimpleNamespace(ok=False,status_code=503,json=lambda:{}),503),
            (SimpleNamespace(ok=False,status_code=429,json=lambda:{}),503),
            (SimpleNamespace(ok=False,status_code=400,json=lambda:{'error':'invalid_grant'}),409),
            (SimpleNamespace(ok=True,json=lambda:None),503),
            (SimpleNamespace(ok=True,json=lambda:{}),503),
            (SimpleNamespace(ok=True,json=lambda:{'access_token':'bad-expiry','expires_in':'not-a-number'}),503),
            (SimpleNamespace(ok=False,json=Mock(side_effect=ValueError('private-response'))),503),
        ]
        for response,status in responses:
            with self.subTest(status=status), patch('backend.services.gsc_service.requests.post',return_value=response) as post:
                with self.assertRaises(GSCError) as failed: self.service._get_valid_access_token(A)
                self.assertEqual(failed.exception.status_code,status)
                post.assert_called_once()
                self.assertEqual(self.current(),before)
        with patch('backend.services.gsc_service.requests.post',side_effect=requests.Timeout('private-timeout')) as post:
            with self.assertRaises(GSCError) as failed: self.service._get_valid_access_token(A)
            self.assertEqual(failed.exception.status_code,503)
            self.assertNotIn('private-timeout',str(failed.exception))
            post.assert_called_once()
            self.assertEqual(self.current(),before)

    def test_committed_refresh_with_lost_ack_requires_readback_without_retry(self):
        client=self.service.connection_db.client
        original=client.rpc
        def uncertain_rpc(name,params):
            query=original(name,params)
            def commit_then_timeout():
                self.assertTrue(query.execute().data)
                raise ConnectionError('private-database-detail')
            return SimpleNamespace(execute=commit_then_timeout)
        with patch('backend.services.gsc_service.requests.post',return_value=SimpleNamespace(ok=True,
            json=lambda:{'access_token':'committed-access','expires_in':3600})) as post, patch.object(client,'rpc',side_effect=uncertain_rpc) as rpc:
            with self.assertRaises(GSCError) as failed: self.service._get_valid_access_token(A)
            self.assertEqual(failed.exception.code,'gsc_refresh_unconfirmed')
            self.assertEqual(failed.exception.status_code,503)
            self.assertNotIn('private-database-detail',str(failed.exception))
            rpc.assert_called_once()
            self.assertEqual(self.current()['access_token'],'committed-access')
            # A later explicit read can observe the already committed valid token.
            self.assertEqual(self.service._get_valid_access_token(A),'committed-access')
            post.assert_called_once()
        self.assertEqual(self.current()['refresh_token'],'original-refresh')
        self.assertEqual(self.current(B),self.other)

    def test_browser_properties_reports_changed_connection_before_using_stale_access(self):
        from backend.routes.gsc_routes import gsc_blueprint
        app=Flask(__name__); app.config['TESTING']=True
        app.register_blueprint(gsc_blueprint,url_prefix='/api/gsc')
        def google_reply(*args,**kwargs):
            self.replace()
            return SimpleNamespace(ok=True,json=lambda:{'access_token':'stale-access','expires_in':3600})
        with patch('backend.services.auth_service.AuthService.verify_token',return_value=SimpleNamespace(id=A)), patch(
            'backend.routes.gsc_routes.GSCService',return_value=self.service), patch(
            'backend.services.gsc_service.requests.post',side_effect=google_reply), patch(
            'backend.services.gsc_service.requests.get') as get:
            response=app.test_client().get('/api/gsc/properties',headers={'Authorization':'Bearer fixture-only'})
        self.assertEqual(response.status_code,409,response.json)
        self.assertEqual(response.json['code'],'operation_conflict')
        self.assertFalse(response.json['success'])
        self.assertNotIn('stale-access',str(response.json))
        get.assert_not_called()
        self.assertEqual(self.current()['refresh_token'],'replacement-refresh')


if __name__ == '__main__': unittest.main()
