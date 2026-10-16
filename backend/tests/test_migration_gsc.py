"""Google boundary mocked; actual PostgreSQL verifies ownership and one-use state."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from flask import Flask
from backend.services.migration_gsc_service import (MigrationGSCService, GSCRepository, AgentGSCProvider,
    covers, data_window, GSCError)
from backend.services.migration_repository import MigrationRepository, InvalidInputError, MigrationNotFoundError, OperationConflictError
from backend.services.migration_planning_service import MigrationPlanningService
from backend.routes.migration_gsc_routes import create_migration_gsc_blueprint
from backend.tests import test_migration_planning as fixture
from backend.tests.test_migration_planning import Query, DBClient, A, B, PLAN

ROOT = Path(__file__).resolve().parents[2]


class PureTests(TestCase):
    def test_property_scope_preserves_scheme_host_path_and_boundaries(self):
        for prop, url in [('https://www.example.com/', 'https://example.com/a'),
                          ('https://example.com/', 'http://example.com/a'),
                          ('https://example.com/folder/', 'https://example.com/other'),
                          ('sc-domain:example.com', 'https://notexample.com/a'),
                          ('https://[bad/', 'https://example.com/')]:
            self.assertFalse(covers(prop, url))
        self.assertTrue(covers('sc-domain:example.com', 'https://a.example.com/p'))
        self.assertTrue(covers('https://example.com/folder/', 'https://example.com/folder/a?q=1'))

    def test_data_window_is_inclusive_bounded_and_not_future(self):
        start, end = data_window()
        self.assertEqual((date.fromisoformat(end)-date.fromisoformat(start)).days, 27)
        for first, last in [('2026-01-01', None), ('2026-01-01', '2026-05-01'),
                            ('bad', 'bad'), ('20260101', '20260102'),
                            (date.today().isoformat(), date.today().isoformat())]:
            with self.assertRaises(InvalidInputError): data_window(first, last)

    def test_transient_refresh_keeps_credentials_and_invalid_grant_is_actionable(self):
        provider = AgentGSCProvider.__new__(AgentGSCProvider)
        provider.connection_db = Mock()
        for status, payload, expected in [(503, {}, 'origin_unavailable'),
                                          (400, {'error': 'invalid_grant'}, 'reconnect_required')]:
            with patch('backend.services.migration_gsc_service.requests.post', return_value=SimpleNamespace(ok=False, status_code=status, json=lambda: payload)):
                with self.assertRaises(GSCError) as raised: provider._refresh_access_token(A, 'fixture-refresh')
                self.assertEqual(raised.exception.code, expected)
        provider.connection_db.delete_connection.assert_not_called()

    def test_incomplete_consent_does_not_store_tokens(self):
        provider = AgentGSCProvider.__new__(AgentGSCProvider)
        provider.connection_db = Mock()
        with patch('backend.services.migration_gsc_service.requests.post', return_value=SimpleNamespace(ok=True, json=lambda: {'access_token': 'test', 'refresh_token': 'test', 'scope': 'email'})):
            with self.assertRaises(GSCError): provider.exchange('code', 'https://app.example/callback', 'verifier')
        provider.connection_db.upsert_connection.assert_not_called()


class GSCQuery(Query):
    def select(self, columns): return self
    def in_(self, key, values): self.in_filter = key, values; return self
    def gt(self, key, value): self.gt_filter = key, value; return self
    def execute(self):
        if not self.rpc_name and not hasattr(self, 'in_filter') and not hasattr(self, 'gt_filter'):
            return super().execute()
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        with psycopg.connect(self.client.dsn, row_factory=dict_row) as conn:
            conn.execute('SET ROLE service_role')
            if self.rpc_name:
                args = [psycopg.types.json.Jsonb(v) if isinstance(v, (dict, list)) else v for v in self.params.values()]
                stmt = sql.SQL('SELECT {}({}) AS result').format(sql.Identifier(self.rpc_name), sql.SQL(',').join(sql.Placeholder() for _ in args))
                try: data = conn.execute(stmt, args).fetchone()['result']
                except psycopg.Error as exc:
                    exc.code, exc.message = exc.sqlstate, exc.diag.message_primary
                    raise
            else:
                if hasattr(self, 'in_filter'):
                    key, values = self.in_filter
                    stmt = sql.SQL('SELECT * FROM {} WHERE {} = ANY(%s)').format(sql.Identifier(self.table_name), sql.Identifier(key))
                    args = [values]
                else:
                    key, value = self.gt_filter
                    stmt = sql.SQL('SELECT * FROM {} WHERE {} > %s').format(sql.Identifier(self.table_name), sql.Identifier(key))
                    args = [value]
                for name, value in self.filters:
                    stmt += sql.SQL(' AND {}=%s').format(sql.Identifier(name)); args.append(value)
                if self.orders:
                    stmt += sql.SQL(' ORDER BY ') + sql.SQL(',').join(sql.Identifier(k) for k, _ in self.orders)
                if self.max_rows:
                    stmt += sql.SQL(' LIMIT %s'); args.append(self.max_rows)
                data = conn.execute(stmt, args).fetchall()
        return SimpleNamespace(data=data, error=None)


class GSCClient(DBClient):
    def table(self, name): return GSCQuery(self, table=name)
    def rpc(self, name, params): return GSCQuery(self, rpc=name, params=params)


@skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'requires explicit disposable local PostgreSQL')
class PostgreSQLTests(TestCase):
    cleanup_db = classmethod(fixture.DatabaseHTTPAcceptance.cleanup_db.__func__)

    @classmethod
    def setUpClass(cls):
        fixture.DatabaseHTTPAcceptance.setUpClass.__func__(cls)
        import psycopg
        with psycopg.connect(cls.dsn, autocommit=True) as conn:
            conn.execute('CREATE FUNCTION update_updated_at_column() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.updated_at=now(); RETURN NEW; END $$')
            conn.execute((ROOT/'database/migrations/024_add_gsc_integration.sql').read_text())
            conn.execute((ROOT/'database/migrations/040_agent_search_console.sql').read_text())
            conn.execute((ROOT/'database/migrations/047_gsc_refresh_compare_and_set.sql').read_text())
            conn.execute('GRANT SELECT ON gsc_connections TO service_role')
        cls.repository = MigrationRepository(GSCClient(cls.dsn))

    def setUp(self):
        import psycopg
        with psycopg.connect(self.dsn) as conn:
            conn.execute('DELETE FROM gsc_agent_accounts')
            conn.execute('DELETE FROM gsc_connections')
        self.migration = MigrationPlanningService(self.repository).plan(A, {**PLAN, 'idempotency_key': uuid4().hex})['migration_id']
        self.provider = Mock()
        self.provider.exchange.return_value = {'access_token': 'fixture-access', 'refresh_token': 'fixture-refresh', 'scopes': 'https://www.googleapis.com/auth/webmasters.readonly', 'token_expires_at': None}
        self.provider.get_status.return_value = {'connected': True}
        self.provider.list_properties.return_value = [{'site_url': 'https://old.example/'}]
        self.provider.analytics.return_value = ([{'keys': ['https://old.example/a?x=1'], 'clicks': 0, 'impressions': 5},
                                                {'keys': ['https://old.example/gsc-only'], 'clicks': 3, 'impressions': 7}], False)
        self.provider.connection_db.get_connection.return_value = None
        self.service = MigrationGSCService(self.repository, self.provider, redirect_uri='https://api.example/api/v2/connections/search-console/callback', state_secret='test-only-' * 5)
        self.store = self.service.store

    def connect(self, key=None):
        result = self.service.execute(A, 'connect', migration_id=self.migration, idempotency_key=key or uuid4().hex)
        return result, parse_qs(urlsplit(result['data']['authorization_url']).query)['state'][0]

    def sync(self, key=None, **kwargs):
        return self.service.execute(A, 'sync', migration_id=self.migration, property='https://old.example/', idempotency_key=key or uuid4().hex, **kwargs)

    def test_connect_replay_and_payload_conflict(self):
        first, state = self.connect('same')
        second, state2 = self.connect('same')
        self.assertEqual(first, second); self.assertEqual(state, state2)
        with self.assertRaises(OperationConflictError): self.service.execute(A, 'connect', idempotency_key='same')
        self.assertNotIn('fixture-refresh', str(first))

    def test_refresh_does_not_overwrite_reconnected_or_disconnected_credentials(self):
        _, state = self.connect()
        self.service.callback(state, 'fixture-code')
        provider = AgentGSCProvider.__new__(AgentGSCProvider)
        provider.connection_db = SimpleNamespace(client=GSCClient(self.dsn))
        import psycopg
        def google_reply(*args, **kwargs):
            # Reconnect wins while an earlier refresh request is in flight.
            with psycopg.connect(self.dsn) as conn:
                conn.execute("UPDATE gsc_connections SET refresh_token='replacement-refresh',access_token='replacement-access' WHERE user_id=%s", (A,))
            return SimpleNamespace(ok=True, json=lambda: {'access_token': 'stale-access', 'expires_in': 3600})
        with patch('backend.services.migration_gsc_service.requests.post', side_effect=google_reply):
            with self.assertRaises(GSCError) as conflict:
                provider._refresh_access_token(A, 'fixture-refresh')
            self.assertEqual(conflict.exception.code, 'operation_conflict')
        with psycopg.connect(self.dsn) as conn:
            self.assertEqual(conn.execute('SELECT access_token FROM gsc_connections WHERE user_id=%s', (A,)).fetchone()[0], 'replacement-access')
        with patch('backend.services.migration_gsc_service.requests.post', return_value=SimpleNamespace(ok=True, json=lambda: {'access_token': 'renewed-access', 'expires_in': 3600})):
            self.assertEqual(provider._refresh_access_token(A, 'replacement-refresh'), 'renewed-access')
            with psycopg.connect(self.dsn) as conn:
                conn.execute('DELETE FROM gsc_connections WHERE user_id=%s', (A,))
            with self.assertRaises(GSCError): provider._refresh_access_token(A, 'replacement-refresh')
        with psycopg.connect(self.dsn) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM gsc_connections').fetchone()[0], 0)

    def test_callback_consumed_once_under_concurrency_and_owner_bound(self):
        result, state = self.connect()
        def callback(_):
            try: return self.service.callback(state, 'fixture-code')['status']
            except InvalidInputError: return 'rejected'
        with ThreadPoolExecutor(max_workers=4) as pool: results = list(pool.map(callback, range(4)))
        self.assertEqual(results.count('succeeded'), 1)
        self.assertEqual(results.count('rejected'), 3)
        self.provider.exchange.assert_called_once()
        import psycopg
        with psycopg.connect(self.dsn) as conn:
            self.assertEqual(conn.execute('SELECT user_id FROM gsc_connections').fetchall(), [(A,)])

    def test_expired_and_superseded_state_never_exchange(self):
        _, first = self.connect()
        _, second = self.connect()
        with self.assertRaises(InvalidInputError): self.service.callback(first, 'code')
        import psycopg
        with psycopg.connect(self.dsn) as conn: conn.execute("UPDATE gsc_agent_operations SET expires_at=now()-interval '1 second'")
        with self.assertRaises(InvalidInputError): self.service.callback(second, 'code')
        self.provider.exchange.assert_not_called()

    def test_denial_consumes_state_and_returns_no_untrusted_error(self):
        _, state = self.connect()
        result = self.service.callback(state, error='<script>token=private</script>')
        self.assertEqual(result['status'], 'cancelled')
        self.assertNotIn('script', str(result))
        with self.assertRaises(InvalidInputError): self.service.callback(state, 'code')
        self.provider.exchange.assert_not_called()

    def test_disconnect_invalidates_pending_and_inflight_callback(self):
        _, state = self.connect()
        import hashlib
        op = self.store.rpc('consume_gsc_agent_state', {'p_hash': hashlib.sha256(state.encode()).hexdigest()})
        result = self.service.execute(A, 'disconnect', idempotency_key='disconnect')
        self.assertEqual(result['data']['connection_state'], 'disconnected')
        with self.assertRaises(OperationConflictError): self.service._finish(op, result, state='connected', tokens=self.provider.exchange.return_value)
        self.assertEqual(self.service.execute(A, 'disconnect', idempotency_key='disconnect'), result)

    def test_sync_preserves_query_gsc_only_and_zero_vs_absent(self):
        result = self.sync('sync')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['data']['coverage'], 'source_limited')
        mappings = [{'id': 'mapping-1', 'old_url': 'https://old.example/a?x=1'},
                    {'id': 'mapping-2', 'old_url': 'https://old.example/a?x=2'},
                    {'id': 'mapping-3', 'old_url': 'https://old.example/gsc-only'}]
        joined = self.service.metrics_for_mappings(A, self.migration, mappings)
        self.assertEqual(joined[0]['traffic']['clicks'], 0)
        self.assertEqual(joined[0]['traffic']['state'], 'observed')
        self.assertIsNone(joined[1]['traffic']['clicks'])
        self.assertEqual(joined[1]['traffic']['state'], 'unavailable')
        self.assertEqual(joined[2]['traffic']['clicks'], 3)
        self.assertEqual([m['id'] for m in joined], [m['id'] for m in mappings])
        self.assertEqual(self.sync('sync'), result)
        self.provider.analytics.assert_called_once()
        with self.assertRaises(MigrationNotFoundError): self.service.metrics_for_mappings(B, self.migration, mappings)

    def test_gsc_only_discovery_paginates_with_preserved_provenance(self):
        self.sync()
        first = self.service.discovery_rows(A, self.migration, limit=1)
        second = self.service.discovery_rows(A, self.migration, after_url=first['next_cursor'], limit=1)
        self.assertIsNotNone(first['next_cursor'])
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(second['items'][0]['url'], 'https://old.example/gsc-only')
        self.assertEqual(second['items'][0]['provenance'], ['gsc'])
        self.assertEqual(second['coverage'], 'source_limited')
        with self.assertRaises(MigrationNotFoundError): self.service.discovery_rows(B, self.migration)

    def test_sync_rejects_property_not_accessible_or_wrong_host(self):
        self.provider.list_properties.return_value = [{'site_url': 'https://www.old.example/'}]
        result = self.sync()
        self.assertEqual(result['error']['code'], 'invalid_input')
        self.provider.analytics.assert_not_called()
        with self.assertRaises(MigrationNotFoundError): self.service.execute(B, 'sync', migration_id=self.migration, property='https://old.example/', idempotency_key='steal')

    def test_failed_sync_preserves_last_successful_snapshot(self):
        self.sync()
        self.provider.analytics.side_effect = GSCError('origin_unavailable', 'Try again.', 503)
        result = self.sync()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(self.store.metric_rows(A, self.migration, ['https://old.example/gsc-only'])), 1)
        self.provider.analytics.side_effect = None
        self.provider.analytics.return_value = ([], True)
        self.assertEqual(self.sync()['status'], 'partial')

    def test_browser_callback_and_auth_boundary(self):
        app = Flask(__name__); app.config['TESTING'] = True
        app.register_blueprint(create_migration_gsc_blueprint(lambda: self.service), url_prefix='/api/v2')
        from backend.extensions import limiter
        limiter.enabled = False
        client = app.test_client()
        path = '/api/v2/connections/search-console/actions'
        self.assertEqual(client.post(path, json={'action': 'status'}).status_code, 401)
        with patch('backend.routes.v2_routes.MCPDelegationService.resolve', return_value=A):
            response = client.post(path, headers={'Authorization': 'Bearer fixture'}, json={'action': 'status'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['data']['properties'], [{'site_url': 'https://old.example/'}])
        _, state = self.connect()
        response = client.get('/api/v2/connections/search-console/callback', query_string={'state': state, 'code': 'fixture-code'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Search Console connected', response.text)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertNotIn('fixture-code', response.text)

    def test_registered_legacy_callback_dispatches_one_use_agent_state_without_fallback(self):
        from backend.routes.gsc_routes import gsc_blueprint
        app = Flask(__name__); app.config['TESTING'] = True
        app.register_blueprint(gsc_blueprint, url_prefix='/api/gsc')
        client = app.test_client()
        self.service.redirect_uri = 'https://api.example/api/gsc/callback'
        _, state = self.connect()
        with patch.dict(os.environ, {'MCP_PIVOT_ENABLED': 'true'}), \
                patch('backend.routes.migration_gsc_routes.MigrationGSCService', return_value=self.service), \
                patch('backend.routes.gsc_routes.GSCService') as legacy:
            duplicate = client.get('/api/gsc/callback', query_string=[('state',state),('state',state),('code','fixture-code')])
            self.assertEqual(duplicate.status_code, 400)
            self.provider.exchange.assert_not_called()
            response = client.get('/api/gsc/callback', query_string={'state':state,'code':'fixture-code','user_id':B,'return_to':'https://attacker.invalid/'})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('Location', response.headers)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.provider.exchange.assert_called_once_with('fixture-code', self.service.redirect_uri, self.service._secret_value('pkce',state))
            self.assertIsNotNone(self.store.connection(A))
            self.assertIsNone(self.store.connection(B))
            replay = client.get('/api/gsc/callback', query_string={'state':state,'code':'fixture-code'})
            self.assertEqual(replay.status_code, 400)
            legacy.assert_not_called()

    def test_registered_callback_retains_legacy_jwt_and_flag_off_behavior(self):
        from backend.routes.gsc_routes import gsc_blueprint
        app = Flask(__name__); app.config['TESTING'] = True
        app.register_blueprint(gsc_blueprint, url_prefix='/api/gsc')
        with patch('backend.routes.gsc_routes.GSCService') as legacy, \
                patch('backend.routes.migration_gsc_routes.MigrationGSCService') as agent:
            legacy.return_value.handle_callback.return_value = {'return_to':'/review/legacy','consent_result':'connected'}
            for flag,state in [('true','signed.legacy.jwt'),('false','a'*43)]:
                with patch.dict(os.environ, {'MCP_PIVOT_ENABLED':flag}):
                    response=app.test_client().get('/api/gsc/callback',query_string={'state':state,'code':'fixture-code'})
                self.assertEqual(response.status_code,302)
                self.assertTrue(response.headers['Location'].endswith('/review/legacy?gsc=connected'))
                legacy.return_value.handle_callback.assert_called_with('fixture-code',state,None)
            agent.assert_not_called()

    def test_rls_and_rpc_privileges_and_account_cleanup(self):
        import psycopg
        for role in ('anon', 'authenticated'):
            with psycopg.connect(self.dsn) as conn:
                conn.execute('SET ROLE ' + role)
                with self.assertRaises(psycopg.errors.InsufficientPrivilege): conn.execute('SELECT * FROM gsc_agent_operations')
            with psycopg.connect(self.dsn) as conn:
                conn.execute('SET ROLE ' + role)
                with self.assertRaises(psycopg.errors.InsufficientPrivilege): conn.execute("SELECT consume_gsc_agent_state('bad')")
        _, state = self.connect()
        self.service.callback(state, 'code')
        self.sync()
        with psycopg.connect(self.dsn) as conn:
            conn.execute('DELETE FROM auth.users WHERE id=%s', [A])
            self.assertEqual(conn.execute('SELECT count(*) FROM gsc_agent_operations WHERE user_id=%s', [A]).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT count(*) FROM gsc_connections WHERE user_id=%s', [A]).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT count(*) FROM gsc_migration_metrics WHERE migration_id=%s', [self.migration]).fetchone()[0], 0)
            conn.rollback()
