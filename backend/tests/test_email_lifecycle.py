"""Email acceptance, preferences and unsubscribe authority; no real provider calls."""
import base64
import hashlib
import hmac
import inspect
import json
import os
from pathlib import Path
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from flask import Flask, request
from backend.services.email_service import EmailService, EmailPreferenceUnavailable, Config
from backend.services.email_preferences import unsubscribe_token, verify_unsubscribe_token, _key, _PURPOSE
from backend.routes.email_routes import email_blueprint, update_preferences


def client_for(data):
    client = Mock()
    query = client.table.return_value
    query.select.return_value = query
    query.eq.return_value = query
    query.maybe_single.return_value = query
    query.upsert.return_value = query
    query.execute.return_value = SimpleNamespace(data=data)
    return client


class EmailLifecycleTests(unittest.TestCase):
    def setUp(self):
        for name, value in [('RESEND_API_KEY', 'fixture-only'), ('SUPABASE_KEY', 'fixture-server-role-secret')]:
            patcher = patch.object(Config, name, value)
            patcher.start(); self.addCleanup(patcher.stop)
        self.service = EmailService()
        self.service._app_url = "https://app.fixture.invalid"
        self.log = patch.object(self.service, '_log_send').start()
        self.provider = patch('backend.services.email_service.resend.Emails.send', return_value={'id': 'accepted-1'}).start()
        self.addCleanup(patch.stopall)
        self.client = client_for(None)
        patch('backend.services.email_service.SupabaseClient.get_client', return_value=self.client).start()

    def send(self):
        return self.service.send_email('owner-a', 'a@fixture.invalid', 'mapping_complete', 'Fixture', 'pivot_mapping_update.html', {'completed': True, 'project_name': 'Fixture', 'total_mappings': 1, 'result_path': '/migrations/fixture'})

    def test_successful_empty_preference_lookup_allows_accepted_id(self):
        self.assertEqual(self.send(), 'accepted-1')
        self.assertEqual(self.provider.call_count, 1)
        self.assertEqual(self.log.call_args.kwargs['resend_message_id'], 'accepted-1')
        payload = self.provider.call_args.args[0]
        self.assertIn('token=', payload['headers']['List-Unsubscribe'])
        self.assertEqual(payload['headers']['List-Unsubscribe-Post'], 'List-Unsubscribe=One-Click')

    def test_provider_object_acknowledgment_is_accepted(self):
        self.provider.return_value = SimpleNamespace(id='accepted-object')
        self.assertEqual(self.send(), 'accepted-object')

    def test_missing_or_malformed_id_is_never_logged_as_sent(self):
        for result in (None, {}, {'id': None}, {'id': ''}, {'id': ' '}, {'id': 123}, {'id': 'x' * 257}, {'id': ' padded '}):
            with self.subTest(result=result):
                self.provider.return_value = result
                self.assertIsNone(self.send())
                self.assertEqual(self.log.call_args.kwargs['status'], 'failed')
                self.assertEqual(self.log.call_args.kwargs['error'], 'provider_acknowledgment_missing')

    def test_preference_uncertainty_never_calls_provider_and_can_recover(self):
        self.client.table.return_value.execute.side_effect = RuntimeError('credential-do-not-log')
        self.assertIsNone(self.send())
        self.provider.assert_not_called()
        self.assertEqual(self.log.call_args.kwargs['error'], 'preference_lookup_unavailable')
        self.client.table.return_value.execute.side_effect = None
        self.assertEqual(self.send(), 'accepted-1')
        self.provider.assert_called_once()

    def test_malformed_preference_response_is_not_an_opt_in(self):
        for result in (None, SimpleNamespace(data=None, error={'message': 'offline'}), SimpleNamespace(data={'opted_out': False}, error={'message': 'offline'}), SimpleNamespace(data={}), SimpleNamespace(data={'opted_out': 'false'}), SimpleNamespace(data=[])):
            with self.subTest(result=result):
                self.client.table.return_value.execute.return_value = result
                self.assertIsNone(self.send())
        self.provider.assert_not_called()

    def test_explicit_opt_out_is_skipped_and_mandatory_welcome_has_no_unsubscribe(self):
        self.client.table.return_value.execute.return_value = SimpleNamespace(data={'opted_out': True})
        self.assertIsNone(self.send())
        self.provider.assert_not_called()
        self.assertEqual(self.log.call_args.kwargs['status'], 'skipped')
        self.assertEqual(self.service.send_welcome('owner-a', 'a@fixture.invalid'), 'accepted-1')
        payload = self.provider.call_args.args[0]
        self.assertNotIn('List-Unsubscribe', payload.get('headers', {}))
        self.assertNotIn('>Unsubscribe<', payload['html'])

    def test_exception_details_are_not_persisted(self):
        self.provider.side_effect = RuntimeError('sensitive-provider-response')
        self.assertIsNone(self.send())
        self.assertEqual(self.log.call_args.kwargs['error'], 'email_send_failed')

    def test_missing_signing_key_uses_authenticated_settings_without_one_click_header(self):
        with patch.object(Config, 'SUPABASE_KEY', None):
            self.assertEqual(self.send(), 'accepted-1')
        payload = self.provider.call_args.args[0]
        self.assertIn('/settings', payload['headers']['List-Unsubscribe'])
        self.assertNotIn('List-Unsubscribe-Post', payload['headers'])

    def test_watch_copy_is_evidence_bounded(self):
        self.service.send_watch_alert('owner-a', 'a@fixture.invalid', 'Example <script>', 'session', 'watch', 'example.com', [], 2, 1, 4300)
        payload = self.provider.call_args.args[0]
        self.assertIn('2 redirect findings', payload['subject'])
        self.assertIn('recorded clicks on affected pages', payload['subject'])
        self.assertNotIn('monthly', payload['subject'])
        self.assertNotIn("weren't there when you exported", payload['html'])
        self.assertIn('only a later successful check can confirm recovery', payload['html'])
        self.assertIn('Example &lt;script&gt;', payload['html'])

    def test_real_watch_delivery_guard_waits_for_positive_provider_acknowledgment(self):
        from backend.tests.test_watch_service import service, WATCH_ID, rp
        watch_service = service({
            'watch_issues': [{'id': 'issue', 'watch_id': WATCH_ID, 'old_url': 'https://old.example/a',
                              'issue_type': rp.NOT_FOUND, 'severity': rp.CRITICAL, 'occurrences': 1,
                              'resolved_at': None, 'alerted_at': None, 'clicks_at_risk': 10}],
            'migration_sessions': [{'id': 'session', 'project_name': 'Fixture'}],
        })
        watch_service = type(watch_service)(client=watch_service.client, email_service=self.service)
        watch = {'id': WATCH_ID, 'user_id': 'owner-a', 'session_id': 'session',
                 'old_domain': 'old.example', 'alert_email': 'a@fixture.invalid'}
        self.provider.return_value = {}
        self.assertEqual(watch_service.send_alert_if_needed(watch), 0)
        self.assertIsNone(watch_service.client.tables['watch_issues'][0]['alerted_at'])
        self.provider.return_value = {'id': 'accepted'}
        self.assertEqual(watch_service.send_alert_if_needed(watch), 1)
        self.assertIsNotNone(watch_service.client.tables['watch_issues'][0]['alerted_at'])
        self.assertEqual(self.provider.call_count, 2)



class UnsubscribeTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(Config, 'SUPABASE_KEY', 'fixture-server-role-secret')
        patcher.start(); self.addCleanup(patcher.stop)
        self.app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / 'templates'))
        self.app.register_blueprint(email_blueprint, url_prefix='/api/email')
        self.http = self.app.test_client()
        self.token = unsubscribe_token('owner-a', 'watch_alert')
        self.storage = client_for([{'user_id': 'owner-a', 'email_type': 'watch_alert', 'opted_out': True}])
        patcher = patch('redirx.database.SupabaseClient.get_client', return_value=self.storage)
        patcher.start(); self.addCleanup(patcher.stop)

    def test_scanner_get_only_confirms_and_valid_post_updates_exact_scope(self):
        url = '/api/email/unsubscribe?token=' + self.token
        response = self.http.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Stop these emails?', response.data)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.storage.table.assert_not_called()
        response = self.http.post(url, data={'List-Unsubscribe': 'One-Click', 'user_id': 'victim', 'email_type': 'mapping_failed'})
        self.assertEqual(response.status_code, 200)
        self.storage.table.return_value.upsert.assert_called_once_with(
            {'user_id': 'owner-a', 'email_type': 'watch_alert', 'opted_out': True}, on_conflict='user_id,email_type')
        self.assertIn(b'Preference saved', response.data)

    def test_legacy_or_forged_tokens_never_write_and_offer_settings(self):
        for suffix in ('user_id=owner-a&type=watch_alert', 'token=' + self.token + 'x', 'type=<script>alert(1)</script>'):
            for method in (self.http.get, self.http.post):
                response = method('/api/email/unsubscribe?' + suffix)
                self.assertEqual(response.status_code, 400)
                self.assertIn(b'/settings', response.data)
                self.assertNotIn(b'<script>', response.data)
        self.storage.table.assert_not_called()

    def test_expired_future_cross_purpose_and_rotated_tokens_are_rejected(self):
        for now in (time.time() - 366 * 86400, time.time() + 600):
            with patch('backend.services.email_preferences.time.time', return_value=now):
                token = unsubscribe_token('owner-a', 'watch_alert')
            with self.assertRaises(ValueError): verify_unsubscribe_token(token)
        raw = {'v': 1, 'purpose': 'other-purpose', 'user_id': 'owner-a', 'email_type': 'watch_alert', 'issued_at': int(time.time())}
        payload = base64.urlsafe_b64encode(json.dumps(raw).encode()).decode().rstrip('=')
        signature = base64.urlsafe_b64encode(hmac.new(_key(), payload.encode(), hashlib.sha256).digest()).decode().rstrip('=')
        with self.assertRaises(ValueError): verify_unsubscribe_token(payload + '.' + signature)
        with patch.object(Config, 'SUPABASE_KEY', 'rotated-secret'):
            with self.assertRaises(ValueError): verify_unsubscribe_token(self.token)

    def test_missing_key_fails_clearly_without_writes(self):
        with patch.object(Config, 'SUPABASE_KEY', None):
            response = self.http.post('/api/email/unsubscribe?token=' + self.token)
        self.assertEqual(response.status_code, 503)
        self.storage.table.assert_not_called()
        self.assertIn(b'/settings', response.data)

    def test_failed_or_unacknowledged_storage_does_not_claim_success(self):
        for data in (None, [], [{'user_id': 'wrong', 'email_type': 'watch_alert', 'opted_out': True}]):
            self.storage.table.return_value.execute.return_value = SimpleNamespace(data=data)
            response = self.http.post('/api/email/unsubscribe?token=' + self.token)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(b'Preference saved', response.data)
        self.storage.table.return_value.execute.side_effect = RuntimeError('offline')
        self.assertEqual(self.http.post('/api/email/unsubscribe?token=' + self.token).status_code, 503)

    def test_error_plus_matching_data_cannot_acknowledge_a_preference_write(self):
        self.storage.table.return_value.execute.return_value = SimpleNamespace(
            error={'message': 'write failed'},
            data=[{'user_id': 'owner-a', 'email_type': 'watch_alert', 'opted_out': True}])
        response = self.http.post('/api/email/unsubscribe?token=' + self.token)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b'Preference saved', response.data)
        with self.app.test_request_context(json={'email_type': 'watch_alert', 'opted_out': True}):
            request.user = SimpleNamespace(id='owner-a')
            response, status = inspect.unwrap(update_preferences)()
            self.assertEqual(status, 500)
            self.assertNotIn('success', response.json)

    def test_error_plus_empty_data_cannot_claim_an_empty_preference_list(self):
        from backend.routes.email_routes import get_preferences
        self.storage.table.return_value.execute.return_value = SimpleNamespace(data=None, error={'message': 'read failed'})
        with self.app.test_request_context():
            request.user = SimpleNamespace(id='owner-a')
            response, status = inspect.unwrap(get_preferences)()
            self.assertEqual(status, 500)
            self.assertNotIn('preferences', response.json)

    def test_authenticated_updates_require_supported_type_and_boolean(self):
        for body in ([], {'email_type': 'welcome', 'opted_out': True}, {'email_type': '<script>', 'opted_out': True}, *({'email_type': 'watch_alert', 'opted_out': value} for value in ('false', 0, None, [], {}))):
            with self.app.test_request_context(json=body):
                request.user = SimpleNamespace(id='owner-a')
                response, status = inspect.unwrap(update_preferences)()
                self.assertEqual(status, 400)
        self.storage.table.assert_not_called()
        with self.app.test_request_context(json={'email_type': 'watch_alert', 'opted_out': True, 'user_id': 'victim'}):
            request.user = SimpleNamespace(id='owner-a')
            self.assertTrue(inspect.unwrap(update_preferences)().json['success'])
        self.assertEqual(self.storage.table.return_value.upsert.call_args.args[0]['user_id'], 'owner-a')


class EmailCallerTests(unittest.TestCase):
    def test_pivot_cron_stays_disabled_before_database_or_provider_construction(self):
        from backend.services.email_nudge_cron import run_nudges
        with patch.dict(os.environ, {'MCP_PIVOT_ENABLED': 'true'}), patch('redirx.database.SupabaseClient.get_client') as database, patch('backend.services.email_service.EmailService') as provider:
            self.assertEqual(run_nudges(), 0)
        database.assert_not_called(); provider.assert_not_called()

    def test_welcome_flag_requires_provider_acknowledgment(self):
        from backend.routes.auth_routes import _send_welcome_if_needed
        client = Mock()
        with patch('redirx.database.SupabaseClient.get_client', return_value=client), patch('backend.services.email_service.EmailService') as service:
            for value in (None, '', ' ', 123):
                service.return_value.send_welcome.return_value = value
                _send_welcome_if_needed('owner', 'a@fixture.invalid', {'welcome_email_sent': False})
                client.table.assert_not_called()
            service.return_value.send_welcome.return_value = 'accepted'
            _send_welcome_if_needed('owner', 'a@fixture.invalid', {'welcome_email_sent': False})
            client.table.return_value.update.assert_called_once_with({'welcome_email_sent': True})

    def test_monitor_preference_uncertainty_retries_instead_of_permanent_suppression(self):
        from backend.services.migration_monitoring_service import send_monitoring_alert
        service = Mock()
        service._rpc.side_effect = [{'id': 'alert', 'attempt': 1, 'payload': {'user_id': 'owner'}}, True]
        with patch('backend.services.email_service.EmailService') as email, patch('resend.Emails.send') as provider:
            email.return_value._is_opted_out.side_effect = EmailPreferenceUnavailable('offline')
            result = send_monitoring_alert(service, 'worker')
        provider.assert_not_called()
        self.assertFalse(result['delivered']); self.assertFalse(result['suppressed'])
        self.assertIsNone(service._rpc.call_args.args[1]['p_message_id'])
        self.assertFalse(service._rpc.call_args.args[1]['p_suppressed'])


class NudgeDedupTests(unittest.TestCase):
    def test_only_an_accepted_nudge_suppresses_a_later_attempt(self):
        from backend.services.email_nudge_cron import run_nudges
        class Query:
            def __init__(self, table, status): self.table_name, self.status, self.filters = table, status, {}
            def select(self, *args, **kwargs): return self
            def gte(self, *args): return self
            def lt(self, *args): return self
            def eq(self, key, value): self.filters[key] = value; return self
            def like(self, *args): return self
            def limit(self, *args): return self
            def maybe_single(self): return self
            def execute(self):
                if self.table_name == 'user_profiles': return SimpleNamespace(data=[{'id': 'owner', 'email': 'a@fixture.invalid'}])
                if self.table_name == 'email_log': return SimpleNamespace(data=[{'id': 'log'}] if self.filters.get('status') == self.status else [])
                if self.table_name == 'migration_sessions': return SimpleNamespace(data=[], count=0)
                return SimpleNamespace(data=None)
        for status, expected in [('failed', 3), ('skipped', 3), ('sent', 0)]:
            client = Mock()
            client.table.side_effect = lambda table: Query(table, status)
            with self.subTest(status=status), patch.dict(os.environ, {'MCP_PIVOT_ENABLED': 'false'}), patch('redirx.database.SupabaseClient.get_client', return_value=client), patch('backend.services.email_service.EmailService') as email:
                email.return_value.send_nudge.return_value = 'accepted'
                self.assertEqual(run_nudges(), expected)
                self.assertEqual(email.return_value.send_nudge.call_count, expected)
