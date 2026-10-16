"""Actual shared040/047/066 authority; all Google responses are fixture-only."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import psycopg
from flask import Flask

from backend.services.gsc_service import GSCError
from backend.services.migration_gsc_service import AgentGSCProvider, MigrationGSCService
from backend.services.migration_repository import MigrationRepository
from backend.tests import test_legacy_gsc_callback as fixture
from backend.tests.test_migration_planning import A, B


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'requires disposable loopback PostgreSQL')
class GSCLifecycleInterleavings(unittest.TestCase):
    cleanup_db = classmethod(fixture.LegacyGSCCallback.cleanup_db.__func__)
    setUpClass = classmethod(fixture.LegacyGSCCallback.setUpClass.__func__)
    current = fixture.LegacyGSCCallback.current

    def setUp(self):
        fixture.LegacyGSCCallback.setUp(self)
        self.legacy = self.service
        self.provider = Mock()
        self.provider.get_status.side_effect = self.legacy.get_status
        self.provider.list_properties.return_value = []
        self.provider.exchange.return_value = {
            'access_token': 'new-agent-access', 'refresh_token': 'new-agent-refresh',
            'scopes': 'https://www.googleapis.com/auth/webmasters.readonly',
            'token_expires_at': None,
        }
        self.agent = MigrationGSCService(
            MigrationRepository(self.legacy.connection_db.client), self.provider,
            redirect_uri='https://api.example/api/v2/connections/search-console/callback',
            state_secret='fixture-agent-state-secret-0000000000000000',
        )

    def state(self):
        result = self.agent.execute(A, 'connect', idempotency_key=uuid4().hex)
        return parse_qs(urlsplit(result['data']['authorization_url']).query)['state'][0]

    def legacy_state(self):
        return parse_qs(urlsplit(self.legacy.build_authorization_url(A, '/review/fixture')).query)['state'][0]

    @staticmethod
    def legacy_tokens():
        return SimpleNamespace(ok=True, json=lambda: {
            'access_token': 'new-legacy-access', 'refresh_token': 'new-legacy-refresh', 'expires_in': 3600,
        })

    def account_state(self):
        with psycopg.connect(self.dsn) as conn:
            return conn.execute('SELECT state FROM gsc_agent_accounts WHERE user_id=%s', [A]).fetchone()[0]

    def browser(self):
        from backend.routes.gsc_routes import gsc_blueprint
        from backend.routes.migration_gsc_routes import create_migration_gsc_blueprint
        app = Flask(__name__)
        app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
        app.register_blueprint(gsc_blueprint, url_prefix='/api/gsc')
        app.register_blueprint(create_migration_gsc_blueprint(lambda: self.agent), url_prefix='/api/v2')
        return app.test_client()

    def test_committed_agent_callback_lost_ack_reports_unconfirmed_on_both_routes(self):
        for path in ('/api/v2/connections/search-console/callback', '/api/gsc/callback'):
            with self.subTest(path=path):
                self.setUp()
                state = self.state()
                client = self.legacy.connection_db.client
                original = client.rpc
                attempts = []

                def rpc(name, params):
                    query = original(name, params)
                    if name != 'finish_gsc_agent_operation':
                        return query

                    def execute():
                        result = query.execute()
                        self.assertEqual(result.data['status'], 'succeeded')
                        attempts.append(name)
                        raise ConnectionError('private-transport-token-detail')
                    return SimpleNamespace(execute=execute)

                browser = self.browser()
                with patch.object(client, 'rpc', side_effect=rpc), patch.dict(os.environ, {'MCP_PIVOT_ENABLED': 'true'}), patch(
                    'backend.routes.migration_gsc_routes.MigrationGSCService', return_value=self.agent), patch(
                    'backend.routes.gsc_routes.GSCService') as legacy_fallback:
                    response = browser.get(path, query_string={'state': state, 'code': 'fixture-code'})
                # Positive native proof: publication committed despite the lost response.
                self.assertEqual(self.current()['refresh_token'], 'new-agent-refresh')
                self.assertEqual(self.current(B), self.other)
                self.assertEqual(len(attempts), 1)
                self.provider.exchange.assert_called_once()
                legacy_fallback.assert_not_called()
                self.assertEqual(response.status_code, 503, response.text)
                self.assertIn('could not be confirmed', response.text)
                self.assertIn('status', response.text)
                for secret in ('new-agent-refresh', 'fixture-code', state, 'private-transport'):
                    self.assertNotIn(secret, response.text)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
                self.assertNotIn('Location', response.headers)
                # Explicit readback sees saved credentials; it does not re-exchange.
                readback = self.agent.execute(A, 'status')
                self.assertEqual(readback['data']['connection_state'], 'connected')
                with patch.dict(os.environ, {'MCP_PIVOT_ENABLED': 'true'}), patch(
                    'backend.routes.migration_gsc_routes.MigrationGSCService', return_value=self.agent):
                    replay = browser.get(path, query_string={'state': state, 'code': 'fixture-code'})
                self.assertEqual(replay.status_code, 400)
                self.provider.exchange.assert_called_once()

    def test_uncertain_consumption_before_or_after_commit_never_exchanges(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                self.setUp()
                state = self.state()
                client = self.legacy.connection_db.client
                original = client.rpc
                attempts = []

                def rpc(name, params):
                    query = original(name, params)
                    if name != 'consume_gsc_agent_state':
                        return query

                    def execute():
                        attempts.append(name)
                        if committed:
                            query.execute()
                        raise ConnectionError('private-consume-transport')
                    return SimpleNamespace(execute=execute)

                with patch.object(client, 'rpc', side_effect=rpc):
                    response = self.browser().get('/api/v2/connections/search-console/callback',
                        query_string={'state': state, 'code': 'fixture-code'})
                self.assertEqual(response.status_code, 503)
                self.assertIn('could not be confirmed', response.text)
                self.assertEqual(len(attempts), 1)
                self.provider.exchange.assert_not_called()
                self.assertEqual(self.current()['refresh_token'], 'original-refresh')
                self.assertEqual(self.current(B), self.other)
                with psycopg.connect(self.dsn) as conn:
                    self.assertEqual(conn.execute('SELECT status FROM gsc_agent_operations WHERE user_id=%s', [A]).fetchone()[0],
                                     'running' if committed else 'awaiting_consent')
                # Saved access is not proof that this callback completed.
                self.assertEqual(self.agent.execute(A, 'status')['data']['connection_state'], 'connected')
                self.assertIn('earlier attempt', response.text)

    def test_failed_publication_transport_leaves_old_connection_and_running_operation(self):
        state = self.state()
        client = self.legacy.connection_db.client
        original = client.rpc
        attempts = []

        def rpc(name, params):
            if name != 'finish_gsc_agent_operation':
                return original(name, params)

            def execute():
                attempts.append(name)
                raise ConnectionError('private-before-commit-transport')
            return SimpleNamespace(execute=execute)

        with patch.object(client, 'rpc', side_effect=rpc):
            response = self.browser().get('/api/v2/connections/search-console/callback',
                query_string={'state': state, 'code': 'fixture-code'})
        self.assertEqual(response.status_code, 503)
        self.assertIn('earlier attempt', response.text)
        self.assertEqual(len(attempts), 1)
        self.provider.exchange.assert_called_once()
        self.assertEqual(self.current()['refresh_token'], 'original-refresh')
        self.assertEqual(self.agent.execute(A, 'status')['data']['connection_state'], 'connected')
        with psycopg.connect(self.dsn) as conn:
            self.assertEqual(conn.execute('SELECT status,result FROM gsc_agent_operations WHERE user_id=%s', [A]).fetchone(),
                             ('running', None))
        self.assertEqual(self.current(B), self.other)

    def test_lost_success_ack_after_later_disconnect_reads_disconnected(self):
        state = self.state()
        client = self.legacy.connection_db.client
        original = client.rpc
        attempts = []

        def rpc(name, params):
            query = original(name, params)
            if name != 'finish_gsc_agent_operation':
                return query

            def execute():
                query.execute()
                attempts.append(name)
                self.legacy.disconnect(A)
                raise ConnectionError('private-finish-transport')
            return SimpleNamespace(execute=execute)

        with patch.object(client, 'rpc', side_effect=rpc), patch(
            'backend.services.gsc_service.requests.post', return_value=SimpleNamespace(ok=True)) as revoke:
            response = self.browser().get('/api/v2/connections/search-console/callback',
                query_string={'state': state, 'code': 'fixture-code'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(attempts), 1)
        self.provider.exchange.assert_called_once()
        revoke.assert_called_once()
        self.assertIsNone(self.current())
        self.assertEqual(self.agent.execute(A, 'status')['data']['connection_state'], 'disconnected')
        self.assertEqual(self.current(B), self.other)

    def test_agent_success_and_error_cannot_replace_newer_historical_consent(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                self.setUp()
                old_state = self.state()

                def exchange(*args):
                    latest = self.legacy_state()
                    with patch('backend.services.gsc_service.requests.post', return_value=self.legacy_tokens()):
                        self.legacy.handle_callback('legacy-code', latest)
                    if failed:
                        raise GSCError('reconnect_required', 'Fixture rejected old consent.', 409)
                    return self.provider.exchange.return_value

                self.provider.exchange.side_effect = exchange
                response = self.browser().get('/api/v2/connections/search-console/callback',
                    query_string={'state': old_state, 'code': 'old-code'})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.current()['refresh_token'], 'new-legacy-refresh')
                self.assertEqual(self.account_state(), 'connected')
                self.assertEqual(self.current(B), self.other)
                self.provider.exchange.assert_called_once()

    def test_historical_callback_cannot_replace_newer_agent_consent(self):
        old_state = self.legacy_state()

        def exchange(*args, **kwargs):
            latest = self.state()
            self.agent.callback(latest, 'agent-code')
            return self.legacy_tokens()

        with patch('backend.services.gsc_service.requests.post', side_effect=exchange) as post:
            with self.assertRaises(GSCError) as conflict:
                self.legacy.handle_callback('legacy-code', old_state)
        self.assertEqual(conflict.exception.code, 'gsc_invalid_state')
        post.assert_called_once()
        self.provider.exchange.assert_called_once()
        self.assertEqual(self.current()['refresh_token'], 'new-agent-refresh')
        self.assertEqual(self.account_state(), 'connected')
        self.assertEqual(self.current(B), self.other)

    def test_legacy_disconnect_during_agent_exchange_fences_late_success(self):
        state = self.state()

        def exchange(*args):
            self.legacy.disconnect(A)
            return self.provider.exchange.return_value

        self.provider.exchange.side_effect = exchange
        with patch('backend.services.gsc_service.requests.post', return_value=SimpleNamespace(ok=True)) as revoke:
            response = self.browser().get('/api/v2/connections/search-console/callback',
                query_string={'state': state, 'code': 'fixture-code'})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.current())
        self.assertEqual(self.account_state(), 'disconnected')
        self.assertEqual(self.current(B), self.other)
        self.provider.exchange.assert_called_once()
        revoke.assert_called_once()

    def test_both_refresh_paths_cannot_overwrite_new_historical_callback(self):
        for agent_refresh in (False, True):
            with self.subTest(agent_refresh=agent_refresh):
                self.setUp()
                provider = self.legacy
                if agent_refresh:
                    provider = AgentGSCProvider.__new__(AgentGSCProvider)
                    provider.connection_db = self.legacy.connection_db

                def refresh_response(*args, **kwargs):
                    self.assertEqual(kwargs['data']['refresh_token'], 'original-refresh')
                    latest = self.legacy_state()
                    with patch('backend.services.gsc_service.requests.post', return_value=self.legacy_tokens()):
                        self.legacy.handle_callback('legacy-code', latest)
                    return SimpleNamespace(ok=True, json=lambda: {'access_token': 'old-refresh-access', 'expires_in': 3600})

                with patch('backend.services.gsc_service.requests.post', side_effect=refresh_response) as post:
                    with self.assertRaises(GSCError) as conflict:
                        provider._get_valid_access_token(A)
                self.assertEqual(conflict.exception.code, 'operation_conflict')
                post.assert_called_once()
                self.assertEqual(self.current()['access_token'], 'new-legacy-access')
                self.assertEqual(self.current()['refresh_token'], 'new-legacy-refresh')
                self.assertEqual(self.account_state(), 'connected')
                self.assertEqual(self.current(B), self.other)


if __name__ == '__main__':
    unittest.main()
