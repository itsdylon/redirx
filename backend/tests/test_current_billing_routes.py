"""HTTP ownership and payload contracts for the current /v2 billing API."""
import unittest
from unittest.mock import Mock, patch

from flask import Flask, request

from backend.routes.current_billing_routes import create_current_billing_blueprint
from backend.services.current_billing_service import BillingNotReadyError
from backend.services.migration_repository import InvalidInputError, MigrationNotFoundError, OperationConflictError
from backend.tests.test_current_billing_service import U, OTHER, C, M, OLD, NEW

ROOT = '/v2/billing/current'


class CurrentBillingRouteTests(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.factory = Mock(return_value=self.service)
        app = Flask(__name__)
        app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
        app.register_blueprint(create_current_billing_blueprint(self.factory), url_prefix='/v2')
        self.client = app.test_client()
        self.owner = U

        def authorize():
            request.api_user_id = self.owner
            return self.owner

        auth = patch('backend.routes.v2_routes.resolve_authorization', side_effect=authorize)
        auth.start()
        self.addCleanup(auth.stop)

    def test_every_account_route_requires_auth_before_service(self):
        self.owner = None
        for method, path in [('get', ROOT), ('get', ROOT + '/usage'), ('get', ROOT + '/checkouts/' + C),
                             ('get', ROOT + '/migrations/' + M + '/quote'),
                             ('post', ROOT + '/checkouts'), ('post', ROOT + '/portal')]:
            with self.subTest(path=path):
                response = getattr(self.client, method)(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.get_json()['error']['code'], 'reconnect_required')
        self.factory.assert_not_called()

    def test_catalog_flat_json_exposes_monthly_plans(self):
        with patch('backend.routes.current_billing_routes.enabled', return_value=True):
            response = self.client.get(ROOT)
        self.assertEqual(response.status_code, 200)
        value = response.get_json()
        self.assertTrue(value['enabled'])
        self.assertEqual([(p['sku'], p['monthly_amount_cents'], p['included_old_urls']) for p in value['plans']],
                         [('studio', 7900, 5000), ('agency', 19900, 15000)])
        self.assertNotIn('data', value)
        self.factory.assert_not_called()

    def test_checkout_uses_authenticated_user_and_returns_flat_result(self):
        result = {'checkout_id': C, 'status': 'open', 'amount_cents': 1000}
        self.service.create_checkout.return_value = result
        payload = {'sku': 'payg', 'idempotency_key': 'request-key', 'migration_id': M,
                   'inventory_ids': {'old': OLD, 'new': NEW}}
        response = self.client.post(ROOT + '/checkouts', json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), result)
        self.service.create_checkout.assert_called_once_with(U, 'payg', 'request-key', migration_id=M,
            inventory_ids={'old': OLD, 'new': NEW}, recurring_consent=False)

    def test_client_amount_identity_and_counts_are_rejected(self):
        for field, value in [('user_id', OTHER), ('amount_cents', 1), ('quantity', 1), ('old_urls', 1),
                             ('price_id', 'price_old'), ('customer_id', 'cus_other')]:
            with self.subTest(field=field):
                response = self.client.post(ROOT + '/checkouts', json={
                    'sku': 'studio', 'idempotency_key': 'key', 'recurring_consent': True, field: value})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()['error']['code'], 'invalid_input')
        self.factory.assert_not_called()

    def test_malformed_non_object_and_non_json_checkout_fail_closed(self):
        for kwargs in [{'json': []}, {'json': 'studio'}, {'data': '{', 'content_type': 'application/json'},
                       {'data': '{}', 'content_type': 'text/plain'}]:
            with self.subTest(kwargs=kwargs):
                response = self.client.post(ROOT + '/checkouts', **kwargs)
                self.assertEqual(response.status_code, 400)
        self.factory.assert_not_called()

    def test_recurring_consent_is_forwarded_without_truthy_coercion(self):
        self.service.create_checkout.side_effect = InvalidInputError('Confirm monthly billing.')
        for consent in [False, 'true', 1]:
            with self.subTest(consent=consent):
                response = self.client.post(ROOT + '/checkouts', json={
                    'sku': 'studio', 'idempotency_key': 'key', 'recurring_consent': consent})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(type(self.service.create_checkout.call_args.kwargs['recurring_consent']), type(consent))
                self.assertEqual(self.service.create_checkout.call_args.kwargs['recurring_consent'], consent)

    def test_get_status_is_scoped_to_authenticated_user(self):
        self.service.get_checkout.return_value = {'checkout_id': C, 'status': 'paid'}
        response = self.client.get(ROOT + '/checkouts/' + C + '?user_id=' + OTHER)
        self.assertEqual(response.status_code, 200)
        self.service.get_checkout.assert_called_once_with(U, C)
        self.assertEqual(response.get_json(), {'checkout_id': C, 'status': 'paid'})
        self.service.create_checkout.assert_not_called()

    def test_usage_filters_provider_identity_and_internal_data(self):
        self.service.usage.return_value = {'sku': 'studio', 'used_old_urls': 51, 'extra_amount_cents': 0,
                                           'stripe_customer_id': 'cus_private', 'user_id': OTHER,
                                           'internal_debug': 'private', 'portal_available': True}
        response = self.client.get(ROOT + '/usage?user_id=' + OTHER)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'sku': 'studio', 'used_old_urls': 51,
                                              'extra_amount_cents': 0, 'portal_available': True})
        self.service.usage.assert_called_once_with(U)

    def test_quote_uses_owned_inventory_ids_and_ignores_client_amount(self):
        self.service._rpc.return_value = {'old_urls': 501, 'amount_cents': 1100, 'stripe_quantity': 501,
                                          'stripe_customer_id': 'cus_private'}
        response = self.client.get(ROOT + '/migrations/' + M + '/quote', query_string={
            'old_inventory_id': OLD, 'new_inventory_id': NEW, 'user_id': OTHER, 'amount_cents': 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'old_urls': 501, 'amount_cents': 1100, 'stripe_quantity': 501})
        self.service._rpc.assert_called_once_with('current_billing_quote', p_user_id=U,
            p_migration_id=M, p_old_inventory_id=OLD, p_new_inventory_id=NEW)

    def test_portal_ignores_client_customer_and_return_url(self):
        self.service.portal.return_value = {'url': 'https://billing.stripe.com/p/session'}
        response = self.client.post(ROOT + '/portal', json={
            'user_id': OTHER, 'customer': 'cus_attacker', 'return_url': 'https://evil.example'})
        self.assertEqual(response.status_code, 200)
        self.service.portal.assert_called_once_with(U)
        self.assertEqual(response.get_json(), self.service.portal.return_value)

    def test_webhook_is_unauthenticated_and_preserves_exact_raw_signed_bytes(self):
        self.owner = None
        self.service.handle_webhook.return_value = {'received': True}
        raw = b'{ "spacing": "is signed", "amount_total": 1 }\n'
        response = self.client.post(ROOT + '/webhook', data=raw, content_type='application/json',
                                    headers={'Stripe-Signature': 't=123,v1=fixture'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'received': True})
        self.service.handle_webhook.assert_called_once_with(raw, 't=123,v1=fixture')
        self.service.create_checkout.assert_not_called()

    def test_invalid_webhook_signature_returns_safe_400(self):
        self.owner = None
        self.service.handle_webhook.side_effect = InvalidInputError('Invalid webhook signature.')
        response = self.client.post(ROOT + '/webhook', data=b'{}')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error']['code'], 'invalid_input')
        self.service.handle_webhook.assert_called_once_with(b'{}', '')

    def test_domain_errors_map_to_failure_envelope(self):
        for error, status, code in [(InvalidInputError('Invalid input.'), 400, 'invalid_input'),
                                    (MigrationNotFoundError('Checkout not found.'), 404, 'not_found'),
                                    (OperationConflictError('Owner mismatch.'), 409, 'operation_conflict'),
                                    (BillingNotReadyError('Not ready.'), 503, 'not_ready')]:
            with self.subTest(code=code):
                self.service.get_checkout.side_effect = error
                response = self.client.get(ROOT + '/checkouts/' + C)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.get_json()['error']['code'], code)
                self.assertEqual(response.get_json()['error']['retryable'], error.retryable)

    def test_current_flag_blocks_legacy_migration_and_monthly_checkout_creation(self):
        from backend.routes.migration_checkout_routes import create_migration_checkout_blueprint
        app = Flask('legacy_migration_guards')
        app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
        factory = Mock()
        app.register_blueprint(create_migration_checkout_blueprint(factory, factory), url_prefix='/v2')
        with patch('backend.services.current_billing_service.enabled', return_value=True):
            for path in ['/v2/migrations/' + M + '/quotes/' + C + '/checkout', '/v2/billing/subscription-checkouts']:
                with self.subTest(path=path):
                    response = app.test_client().post(path, json={})
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.get_json()['error']['code'], 'operation_conflict')
        factory.assert_not_called()

    def test_current_flag_blocks_legacy_project_and_agency_checkout(self):
        from types import SimpleNamespace
        from backend.routes.billing_routes import billing_blueprint
        app = Flask('legacy_billing_guards')
        app.config.update(TESTING=True)
        app.register_blueprint(billing_blueprint, url_prefix='/api/billing')
        with patch('backend.services.current_billing_service.enabled', return_value=True), patch(
                'backend.services.auth_service.AuthService.verify_token', return_value=SimpleNamespace(id=U)), patch(
                'backend.routes.billing_routes._get_stripe_service') as provider:
            for path in ['/project/checkout', '/agency/checkout']:
                with self.subTest(path=path):
                    response = app.test_client().post('/api/billing' + path, json={},
                        headers={'Authorization': 'Bearer fixture'})
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.get_json()['code'], 'billing_catalog_changed')
            provider.assert_not_called()


if __name__ == '__main__':
    unittest.main()
