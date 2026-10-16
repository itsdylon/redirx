"""Current billing authority tests. No credentials, network, or database required."""
import copy
import hashlib
import hmac
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch, create_autospec

import stripe

from backend.services.current_billing_service import CurrentBillingService, BillingNotReadyError
from backend.services.current_pricing import PLANS, VERSION, STRIPE_API_VERSION, usage_cents, validate_price
from backend.services.migration_repository import (
    InvalidInputError, OperationConflictError, MigrationNotFoundError, RepositoryUnavailableError,
)

U = '10000000-0000-0000-0000-000000000001'
OTHER = '10000000-0000-0000-0000-000000000002'
C = '70000000-0000-0000-0000-000000000001'
M = '40000000-0000-0000-0000-000000000001'
OLD = '50000000-0000-0000-0000-000000000001'
NEW = '50000000-0000-0000-0000-000000000002'
SECRET = 'whsec_local_fixture_only'


def sdk(value):
    """Exercise SDK nested object conversion, not just hand-written dict responses."""
    return stripe.StripeObject.construct_from(copy.deepcopy(value), None)


def sdk_provider():
    """Constrain every mocked method to the installed Stripe SDK's real signature.

    Constructing a client with a local dummy key performs no requests. Services
    remain mocks; a removed/renamed SDK method fails during fixture construction.
    """
    real = stripe.StripeClient('sk_test_fixture')
    root = Mock(spec_set=['v1'])
    root.v1 = Mock(spec_set=['accounts', 'prices', 'checkout', 'payment_intents',
                             'charges', 'subscriptions', 'invoices', 'invoice_items', 'billing_portal'])
    def service(actual, methods, children=()):
        result = Mock(spec_set=list(methods) + list(children))
        for name in methods:
            setattr(result, name, create_autospec(getattr(actual, name)))
        return result
    root.v1.accounts = service(real.v1.accounts, ['retrieve_current'])
    for name in ['prices', 'payment_intents', 'charges', 'subscriptions', 'invoices']:
        setattr(root.v1, name, service(getattr(real.v1, name), ['retrieve']))
    root.v1.invoice_items = service(real.v1.invoice_items, ['create', 'list'])
    root.v1.checkout = Mock(spec_set=['sessions'])
    root.v1.checkout.sessions = service(real.v1.checkout.sessions, ['create', 'retrieve', 'list'], ['line_items'])
    root.v1.checkout.sessions.line_items = service(real.v1.checkout.sessions.line_items, ['list'])
    root.v1.billing_portal = Mock(spec_set=['sessions', 'configurations'])
    root.v1.billing_portal.configurations = service(real.v1.billing_portal.configurations, ['retrieve'])
    root.v1.billing_portal.sessions = service(real.v1.billing_portal.sessions, ['create'])
    return root


def signed(kind='checkout.session.completed', identifier='cs_test_fixture', **overrides):
    event = {'object': 'event', 'id': 'evt_fixture', 'type': kind, 'livemode': False,
             'data': {'object': {'object': kind.split('.')[0], 'id': identifier}}}
    event.update(overrides)
    raw = json.dumps(event, separators=(',', ':')).encode()
    ts = str(int(time.time()))
    digest = hmac.new(SECRET.encode(), ts.encode() + b'.' + raw, hashlib.sha256).hexdigest()
    return raw, f't={ts},v1={digest}'


def price(sku='payg'):
    return {'id': 'price_' + sku, 'object': 'price', 'livemode': False, 'active': True,
            'currency': 'usd', 'unit_amount': PLANS[sku].amount_cents,
            'billing_scheme': 'per_unit', 'tax_behavior': 'exclusive', 'product': 'prod_fixture',
            'recurring': {'interval': 'month', 'interval_count': 1, 'usage_type': 'licensed'}
            if sku in {'studio', 'agency'} else None,
            'transform_quantity': None if sku in {'studio', 'agency'} else {'divide_by': 50, 'round': 'up'}}


class CurrentPricingTests(unittest.TestCase):
    def test_integer_rounding_and_payg_minimum(self):
        for count, cents in [(0, 0), (1, 100), (49, 100), (50, 100), (51, 200),
                             (499, 1000), (500, 1000), (501, 1100), (550, 1100), (551, 1200)]:
            with self.subTest(count=count):
                self.assertEqual(usage_cents(count), cents)
                self.assertEqual(usage_cents(count, minimum=True), max(1000, cents))

    def test_invalid_counts_do_not_coerce_bool_float_or_string(self):
        for value in [True, False, -1, 500.0, '500', None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                usage_cents(value)

    def test_all_four_catalog_prices_validate(self):
        for sku in PLANS:
            with self.subTest(sku=sku):
                validate_price(price(sku), sku, 'price_' + sku, False)

    def test_price_rejects_old_rate_wrong_environment_archive_and_transform(self):
        for sku in PLANS:
            changes = [{'livemode': True}, {'unit_amount': 4900}, {'unit_amount': True},
                       {'active': False}, {'currency': 'eur'}, {'id': 'price_old'},
                       {'billing_scheme': 'tiered'}, {'tax_behavior': 'inclusive'}]
            changes += ([{'recurring': {'interval': 'year', 'interval_count': 1, 'usage_type': 'licensed'}},
                         {'recurring': {'interval': 'month', 'interval_count': 12, 'usage_type': 'licensed'}},
                         {'recurring': {'interval': 'month', 'interval_count': 1, 'usage_type': 'metered'}},
                         {'transform_quantity': {'divide_by': 50, 'round': 'up'}}]
                        if sku in {'studio', 'agency'} else
                        [{'transform_quantity': None}, {'transform_quantity': {'divide_by': 50, 'round': 'down'}},
                         {'transform_quantity': {'divide_by': 100, 'round': 'up'}},
                         {'recurring': {'interval': 'month'}}])
            for change in changes:
                with self.subTest(sku=sku, change=change), self.assertRaises(ValueError):
                    validate_price({**price(sku), **change}, sku, 'price_' + sku, False)

    def test_live_catalog_requires_exact_product(self):
        for sku, plan in PLANS.items():
            live = {**price(sku), 'livemode': True, 'id': plan.live_price_id, 'product': plan.live_product_id}
            validate_price(live, sku, plan.live_price_id, True)
            with self.assertRaises(ValueError):
                validate_price({**live, 'product': 'prod_wrong'}, sku, plan.live_price_id, True)


class CurrentBillingServiceTests(unittest.TestCase):
    def setUp(self):
        self.config = {'REDIRX_CURRENT_BILLING_ENABLED': 'true', 'REDIRX_STRIPE_MODE': 'test',
                       'REDIRX_STRIPE_SECRET_KEY': 'sk_test_fixture', 'REDIRX_STRIPE_ACCOUNT_ID': 'acct_fixture',
                       'REDIRX_STRIPE_WEBHOOK_SECRET': SECRET,
                       'REDIRX_STRIPE_PORTAL_CONFIGURATION_ID': 'bpc_fixture',
                       'MCP_CHECKOUT_COMPANION_ORIGIN': 'https://companion.example',
                       **{'REDIRX_STRIPE_' + sku.upper() + '_PRICE_ID': 'price_' + sku for sku in PLANS}}
        self.db = Mock()
        self.provider = sdk_provider()
        self.service = CurrentBillingService(SimpleNamespace(client=self.db), stripe_client=self.provider,
                                             config=self.config)
        self.row = {'id': C, 'user_id': U, 'sku': 'payg', 'status': 'reserved', 'livemode': False,
                    'migration_id': M, 'old_urls': 1, 'quantity': 500, 'amount_cents': 1000,
                    'stripe_price_id': 'price_payg', 'stripe_session_id': None,
                    'stripe_subscription_id': None, 'stripe_customer_id': None,
                    'expires_at': (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()}
        self.metadata = self.service._metadata(self.row)
        self.session = {'id': 'cs_test_fixture', 'object': 'checkout.session', 'livemode': False,
                        'mode': 'payment', 'metadata': self.metadata, 'client_reference_id': C,
                        'currency': 'usd', 'amount_total': 1000, 'amount_subtotal': 1000,
                        'expires_at': int(datetime.fromisoformat(self.row['expires_at']).timestamp()),
                        'url': 'https://checkout.stripe.com/c/pay/cs_test_fixture',
                        'status': 'complete', 'payment_status': 'paid', 'payment_intent': 'pi_fixture',
                        'customer': 'cus_fixture', 'subscription': None}
        self.lines = {'object': 'list', 'has_more': False, 'data': [
            {'object': 'item', 'id': 'li_fixture', 'price': price(), 'quantity': 500}]}
        self.intent = {'id': 'pi_fixture', 'object': 'payment_intent', 'livemode': False,
                       'metadata': self.metadata, 'status': 'succeeded', 'currency': 'usd',
                       'amount': 1000, 'amount_received': 1000, 'customer': 'cus_fixture',
                       'latest_charge': 'ch_fixture', 'invoice': None}
        self.charge = {'id': 'ch_fixture', 'object': 'charge', 'livemode': False,
                       'payment_intent': 'pi_fixture', 'customer': 'cus_fixture', 'paid': True,
                       'currency': 'usd', 'amount': 1000, 'amount_refunded': 0, 'disputed': False}
        self.portal_configuration = {'id': 'bpc_fixture', 'object': 'billing_portal.configuration',
                                     'livemode': False, 'active': True, 'features': {
                                         'subscription_update': {'enabled': False},
                                         'subscription_cancel': {'enabled': True, 'mode': 'at_period_end'}}}
        self.provider.v1.billing_portal.configurations.retrieve.side_effect = lambda *a, **kw: sdk(self.portal_configuration)
        self.rpc_results = {}
        self.db.rpc.side_effect = self.rpc
        self.db.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.side_effect = (
            lambda: SimpleNamespace(data=copy.deepcopy(self.row), error=None))
        self.provider.v1.accounts.retrieve_current.return_value = sdk({'id': 'acct_fixture'})
        self.provider.v1.prices.retrieve.side_effect = lambda identifier, **kw: sdk(price(identifier[6:]))
        self.provider.v1.checkout.sessions.create.side_effect = lambda **kw: sdk(self.session)
        self.provider.v1.checkout.sessions.retrieve.side_effect = lambda *a, **kw: sdk(self.session)
        self.provider.v1.checkout.sessions.line_items.list.side_effect = lambda *a, **kw: sdk(self.lines)
        self.provider.v1.payment_intents.retrieve.side_effect = lambda *a, **kw: sdk(self.intent)
        self.provider.v1.charges.retrieve.side_effect = lambda *a, **kw: sdk(self.charge)

    def rpc(self, name, params):
        if name in self.rpc_results:
            result = copy.deepcopy(self.rpc_results[name])
        elif name in {'reserve_current_checkout', 'get_current_checkout'}:
            result = copy.deepcopy(self.row)
        elif name == 'close_current_usage':
            result = {'extra_old_urls': 0, 'extra_amount_cents': 0, 'stripe_invoice_item_id': None}
        elif name == 'attach_current_checkout':
            result = {**self.row, 'status': 'open', 'checkout_url': params['p_url']}
        elif name in {'apply_current_checkout_payment', 'record_current_subscription_period',
                      'revoke_current_billing', 'attach_current_usage_invoice_item'}:
            result = {'received': True}
        else:
            raise AssertionError('Unexpected RPC: ' + name)
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=result, error=None))

    def calls(self, name):
        return [c.args[1] for c in self.db.rpc.call_args_list if c.args[0] == name]

    def assert_no_grant(self):
        self.assertEqual(self.calls('apply_current_checkout_payment'), [])
        self.assertEqual(self.calls('record_current_subscription_period'), [])

    def create(self, **kwargs):
        return self.service.create_checkout(U, 'payg', 'stable-request', migration_id=M,
                                            inventory_ids={'old': OLD, 'new': NEW}, **kwargs)

    def webhook(self, kind='checkout.session.completed', identifier='cs_test_fixture', **kwargs):
        return self.service.handle_webhook(*signed(kind, identifier, **kwargs))

    def subscription(self):
        self.row.update(sku='studio', amount_cents=7900, quantity=1, migration_id=None,
                        stripe_price_id='price_studio', stripe_subscription_id='sub_fixture',
                        stripe_customer_id='cus_fixture', stripe_session_id='cs_test_fixture', status='paid')
        self.metadata = self.service._metadata(self.row)
        self.session.update(mode='subscription', metadata=self.metadata, amount_total=7900,
                            amount_subtotal=7900, subscription='sub_fixture')
        self.lines['data'][0].update(price=price('studio'), quantity=1)
        start = int(time.time()) - 86400
        end = start + 30 * 86400
        self.sub = {'id': 'sub_fixture', 'object': 'subscription', 'livemode': False,
                    'metadata': self.metadata, 'customer': 'cus_fixture', 'status': 'active',
                    'latest_invoice': 'in_fixture', 'current_period_start': start, 'current_period_end': end,
                    'items': {'object': 'list', 'has_more': False, 'data': [
                        {'id': 'si_fixture', 'object': 'subscription_item', 'quantity': 1, 'price': price('studio')}]}}
        self.invoice = {'id': 'in_fixture', 'object': 'invoice', 'livemode': False,
                        'subscription': 'sub_fixture', 'customer': 'cus_fixture', 'status': 'paid',
                        'paid': True, 'paid_out_of_band': False, 'currency': 'usd', 'amount_remaining': 0,
                        'total': 7900, 'subtotal': 7900, 'amount_due': 7900, 'amount_paid': 7900,
                        'payment_intent': 'pi_fixture', 'billing_reason': 'subscription_cycle',
                        'period_end': start, 'lines': {'object': 'list', 'has_more': False, 'data': [
                            {'id': 'il_base', 'object': 'line_item', 'subscription_item': 'si_fixture',
                             'type': 'subscription', 'proration': False, 'amount': 7900, 'quantity': 1,
                             'price': price('studio'), 'period': {'start': start, 'end': end}}]}}
        self.intent.update(amount=7900, amount_received=7900, invoice='in_fixture')
        self.charge.update(amount=7900)
        self.provider.v1.subscriptions.retrieve.side_effect = lambda *a, **kw: sdk(self.sub)
        self.provider.v1.invoices.retrieve.side_effect = lambda *a, **kw: sdk(self.invoice)

    def usage_snapshot(self):
        self.rpc_results['close_current_usage'] = {
            'id': 'period_fixture', 'extra_old_urls': 51, 'extra_amount_cents': 200,
            'stripe_invoice_item_id': None, 'closed_at': datetime.now(timezone.utc).isoformat(),
            'period_start': datetime.fromtimestamp(self.sub['current_period_start'] - 30 * 86400, timezone.utc).isoformat(),
            'period_end': datetime.fromtimestamp(self.sub['current_period_start'], timezone.utc).isoformat()}
        self.item = {'id': 'ii_fixture', 'object': 'invoiceitem', 'livemode': False,
                     'invoice': 'in_fixture', 'customer': 'cus_fixture', 'amount': 200, 'currency': 'usd',
                     'price': price('additional_urls'), 'quantity': 51,
                     'metadata': {'redirx_catalog': VERSION, 'redirx_period_id': 'period_fixture'}}
        self.provider.v1.invoice_items.list.return_value = sdk({'object': 'list', 'has_more': False, 'data': []})
        self.provider.v1.invoice_items.create.side_effect = lambda **kw: sdk(self.item)

    def test_create_uses_server_snapshot_counts_minimum_and_scoped_rpc(self):
        for count, quantity, amount in [(1, 500, 1000), (500, 500, 1000), (501, 501, 1100),
                                        (550, 550, 1100), (551, 551, 1200)]:
            with self.subTest(count=count):
                self.row.update(old_urls=count, quantity=quantity, amount_cents=amount)
                self.session.update(amount_total=amount, amount_subtotal=amount)
                self.assertEqual(self.create()['amount_cents'], amount)
                params = self.provider.v1.checkout.sessions.create.call_args.kwargs['params']
                self.assertEqual(params['line_items'], [{'price': 'price_payg', 'quantity': quantity}])
                self.assertEqual(params['payment_intent_data']['metadata'], self.metadata)
                self.assertFalse(params['allow_promotion_codes'])
                self.assertFalse(params['automatic_tax']['enabled'])
        self.assertEqual(self.calls('reserve_current_checkout')[-1], {
            'p_user_id': U, 'p_sku': 'payg', 'p_migration_id': M, 'p_old_inventory_id': OLD,
            'p_new_inventory_id': NEW, 'p_key': 'stable-request', 'p_recurring_consent': False,
            'p_price_id': 'price_payg', 'p_livemode': False})
        self.assert_no_grant()

    def test_monthly_requires_literal_explicit_consent_before_any_io(self):
        for sku in ['studio', 'agency']:
            for consent in [False, None, 1, 'true']:
                with self.subTest(sku=sku, consent=consent), self.assertRaises(InvalidInputError):
                    self.service.create_checkout(U, sku, 'stable-request', recurring_consent=consent)
        self.assertEqual(self.provider.mock_calls, [])
        self.db.rpc.assert_not_called()

    def test_monthly_checkout_records_consent_and_fixed_quantity(self):
        self.subscription()
        self.row.update(status='reserved', stripe_subscription_id=None)
        result = self.service.create_checkout(U, 'studio', 'stable-request', recurring_consent=True)
        self.assertEqual(result['status'], 'open')
        params = self.provider.v1.checkout.sessions.create.call_args.kwargs['params']
        self.assertEqual(params['mode'], 'subscription')
        self.assertEqual(params['line_items'], [{'price': 'price_studio', 'quantity': 1}])
        self.assertIn('$79 USD each month', params['custom_text']['submit']['message'])
        self.assertTrue(self.calls('reserve_current_checkout')[0]['p_recurring_consent'])
        self.assert_no_grant()

    def test_account_mismatch_prevents_reservation(self):
        self.provider.v1.accounts.retrieve_current.return_value = sdk({'id': 'acct_other'})
        with self.assertRaises(OperationConflictError):
            self.create()
        self.db.rpc.assert_not_called()
        self.provider.v1.checkout.sessions.create.assert_not_called()

    def test_reservation_owner_mismatch_prevents_provider_checkout(self):
        self.row['user_id'] = OTHER
        with self.assertRaises(RepositoryUnavailableError):
            self.create()
        self.provider.v1.checkout.sessions.create.assert_not_called()

    def test_provider_unknown_result_retry_preserves_parameters_and_idempotency(self):
        self.provider.v1.checkout.sessions.create.side_effect = [RuntimeError('secret provider detail'), sdk(self.session)]
        with self.assertRaises(BillingNotReadyError) as caught:
            self.create()
        self.assertNotIn('secret provider detail', str(caught.exception))
        self.assertEqual(self.calls('attach_current_checkout'), [])
        self.assertEqual(self.create()['status'], 'open')
        first, second = self.provider.v1.checkout.sessions.create.call_args_list
        self.assertEqual(first, second)
        self.assertEqual(first.kwargs['options'], {'stripe_version': STRIPE_API_VERSION,
                                                 'idempotency_key': f'{VERSION}:checkout:{C}'})
        self.assert_no_grant()

    def test_read_uses_authenticated_owner_and_never_grants(self):
        self.assertEqual(self.service.get_checkout(U, C)['checkout_id'], C)
        self.assertEqual(self.calls('get_current_checkout'), [{'p_user_id': U, 'p_checkout_id': C}])
        self.assertEqual(self.provider.mock_calls, [])
        self.assert_no_grant()
        self.row['livemode'] = True
        with self.assertRaises(MigrationNotFoundError):
            self.service.get_checkout(U, C)

    def test_invalid_signature_body_and_event_environment_have_no_io(self):
        raw, signature = signed()
        for body, sig in [(raw + b' ', signature), (raw, 'invalid'), (b'x' * (1024 * 1024 + 1), signature),
                          signed(livemode=True), signed(account='acct_other')]:
            with self.subTest(signature=sig[:15]), self.assertRaises(InvalidInputError):
                self.service.handle_webhook(body, sig)
        self.assertEqual(self.provider.mock_calls, [])
        self.assertEqual(self.db.mock_calls, [])

    def test_signed_paid_payg_retrieves_provider_truth_before_grant(self):
        # Payload amount/ownership are deliberately false; only the retrieved object is authoritative.
        result = self.webhook(data={'object': {'id': 'cs_test_fixture', 'amount_total': 1, 'customer': 'cus_attacker'}})
        self.assertEqual(result, {'received': True})
        self.assertEqual(self.calls('apply_current_checkout_payment'), [{
            'p_checkout_id': C, 'p_session_id': 'cs_test_fixture', 'p_customer_id': 'cus_fixture',
            'p_subscription_id': None, 'p_amount_cents': 1000, 'p_currency': 'usd', 'p_event_id': 'evt_fixture'}])
        self.provider.v1.checkout.sessions.retrieve.assert_called_once()
        self.provider.v1.checkout.sessions.line_items.list.assert_called_once()
        self.provider.v1.payment_intents.retrieve.assert_called_once()
        self.provider.v1.charges.retrieve.assert_called_once()

    def test_retrieved_session_ownership_totals_currency_and_identity_must_match(self):
        original = copy.deepcopy(self.session)
        changes = [{'metadata': {**self.metadata, 'redirx_user_id': OTHER}}, {'client_reference_id': M},
                   {'amount_total': 999}, {'amount_subtotal': 999}, {'amount_total': True},
                   {'currency': 'eur'}, {'mode': 'subscription'}, {'livemode': True},
                   {'id': 'cs_wrong'}, {'expires_at': original['expires_at'] + 1}]
        for change in changes:
            with self.subTest(change=change):
                self.session = {**original, **change}
                with self.assertRaises(OperationConflictError):
                    self.webhook()
                self.assert_no_grant()

    def test_line_price_quantity_cardinality_and_pagination_are_verified(self):
        original = copy.deepcopy(self.lines)
        variants = [{**original, 'has_more': True}, {**original, 'data': []},
                    {**original, 'data': original['data'] * 2},
                    {**original, 'data': [{**original['data'][0], 'quantity': 1}]},
                    {**original, 'data': [{**original['data'][0], 'price': {'id': 'price_old'}}]}]
        for lines in variants:
            with self.subTest(lines=lines), self.assertRaises(OperationConflictError):
                self.lines = lines
                self.webhook()
        self.assert_no_grant()

    def test_unpaid_session_is_pending_without_grant_or_intent_verification(self):
        self.session['payment_status'] = 'unpaid'
        self.assertTrue(self.webhook()['pending'])
        self.provider.v1.payment_intents.retrieve.assert_not_called()
        self.assert_no_grant()

    def test_intent_and_charge_must_prove_exact_unreversed_payment(self):
        for target, changes in [(self.intent, [{'customer': 'cus_other'}, {'amount_received': 999},
                                               {'status': 'processing'}, {'metadata': {}}]),
                                (self.charge, [{'payment_intent': 'pi_other'}, {'customer': 'cus_other'},
                                               {'amount': 999}, {'paid': False}, {'amount_refunded': 1},
                                               {'disputed': True}])]:
            original = copy.deepcopy(target)
            for change in changes:
                with self.subTest(change=change):
                    target.update(change)
                    with self.assertRaises(OperationConflictError):
                        self.webhook()
                    self.assert_no_grant()
                    target.clear()
                    target.update(copy.deepcopy(original))

    def test_renewal_verifies_current_paid_invoice_and_charge(self):
        self.subscription()
        self.assertEqual(self.webhook('invoice.paid', 'in_fixture'), {'received': True})
        recorded = self.calls('record_current_subscription_period')
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]['p_invoice_id'], 'in_fixture')
        self.assertEqual(recorded[0]['p_amount_cents'], 7900)
        self.assertEqual(self.calls('apply_current_checkout_payment'), [])
        self.provider.v1.charges.retrieve.assert_called_once()

    def test_initial_subscription_requires_completed_checkout_before_period_grant(self):
        self.subscription()
        self.row.update(stripe_subscription_id=None, status='open')
        self.invoice['billing_reason'] = 'subscription_create'
        self.session['payment_status'] = 'unpaid'
        with self.assertRaises(OperationConflictError):
            self.webhook('invoice.paid', 'in_fixture')
        self.assert_no_grant()
        self.session['payment_status'] = 'paid'
        self.webhook('invoice.paid', 'in_fixture')
        names = [c.args[0] for c in self.db.rpc.call_args_list]
        self.assertLess(names.index('apply_current_checkout_payment'), names.index('record_current_subscription_period'))
        self.assertEqual(self.calls('apply_current_checkout_payment')[0]['p_subscription_id'], 'sub_fixture')

    def test_unpaid_out_of_band_wrong_invoice_period_and_wrong_charge_reject_renewal(self):
        self.subscription()
        original = copy.deepcopy(self.invoice)
        for change in [{'paid': False}, {'paid_out_of_band': True}, {'customer': 'cus_other'},
                       {'subscription': 'sub_other'}, {'amount_paid': 7800}, {'amount_remaining': 1}]:
            with self.subTest(change=change), self.assertRaises(OperationConflictError):
                self.invoice = {**original, **change}
                self.webhook('invoice.paid', 'in_fixture')
        self.invoice = copy.deepcopy(original)
        self.invoice['lines']['data'][0]['period']['end'] += 1
        with self.assertRaises(OperationConflictError):
            self.webhook('invoice.paid', 'in_fixture')
        self.invoice = original
        self.charge['amount_refunded'] = 1
        with self.assertRaises(OperationConflictError):
            self.webhook('invoice.paid', 'in_fixture')
        self.assert_no_grant()

    def test_renewal_extra_line_requires_saved_item_price_amount_and_total(self):
        self.subscription()
        self.usage_snapshot()
        self.rpc_results['close_current_usage']['stripe_invoice_item_id'] = 'ii_fixture'
        extra = {'id': 'il_extra', 'type': 'invoiceitem', 'invoice_item': 'ii_fixture',
                 'amount': 200, 'price': price('additional_urls')}
        self.invoice['lines']['data'].append(extra)
        for key in ['total', 'subtotal', 'amount_due', 'amount_paid']:
            self.invoice[key] = 8100
        self.intent.update(amount=8100, amount_received=8100)
        self.charge['amount'] = 8100
        for field, bad in [('invoice_item', 'ii_wrong'), ('amount', 100), ('price', price('payg'))]:
            good = extra[field]
            extra[field] = bad
            with self.subTest(field=field), self.assertRaises(OperationConflictError):
                self.webhook('invoice.paid', 'in_fixture')
            self.assert_no_grant()
            extra[field] = good
        self.webhook('invoice.paid', 'in_fixture')
        self.assertEqual(len(self.calls('record_current_subscription_period')), 1)
        self.assertEqual(self.calls('record_current_subscription_period')[0]['p_amount_cents'], 7900)

    def test_usage_retry_recovers_existing_item_without_creating_duplicate(self):
        self.subscription()
        self.usage_snapshot()
        self.invoice['status'] = 'draft'
        self.provider.v1.invoice_items.list.return_value = sdk({'data': [self.item], 'has_more': False})
        self.webhook('invoice.created', 'in_fixture')
        self.provider.v1.invoice_items.create.assert_not_called()
        self.assertEqual(self.calls('attach_current_usage_invoice_item'), [{
            'p_period_id': 'period_fixture', 'p_invoice_id': 'in_fixture', 'p_item_id': 'ii_fixture'}])

    def test_usage_create_uses_frozen_counts_stable_key_and_limited_retry_window(self):
        self.subscription()
        self.usage_snapshot()
        self.invoice['status'] = 'draft'
        self.provider.v1.invoice_items.create.side_effect = [RuntimeError('unknown result'), sdk(self.item)]
        with self.assertRaises(BillingNotReadyError):
            self.webhook('invoice.created', 'in_fixture')
        self.webhook('invoice.created', 'in_fixture')
        first, second = self.provider.v1.invoice_items.create.call_args_list
        self.assertEqual(first, second)
        self.assertEqual(first.kwargs['params']['quantity'], 51)
        self.assertEqual(first.kwargs['options']['idempotency_key'], f'{VERSION}:extra:period_fixture')
        self.provider.v1.invoice_items.create.reset_mock()
        self.rpc_results['close_current_usage']['closed_at'] = (
            datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with self.assertRaises(BillingNotReadyError):
            self.webhook('invoice.created', 'in_fixture')
        self.provider.v1.invoice_items.create.assert_not_called()

    def test_finalized_invoice_recovers_existing_usage_item_without_create(self):
        self.subscription()
        self.usage_snapshot()
        self.provider.v1.invoice_items.list.return_value = sdk({'data': [self.item], 'has_more': False})
        self.webhook('invoice.created', 'in_fixture')
        self.provider.v1.invoice_items.create.assert_not_called()
        self.assertEqual(len(self.calls('attach_current_usage_invoice_item')), 1)

    def test_wrong_refund_or_dispute_owner_does_not_revoke(self):
        self.row['stripe_customer_id'] = 'cus_fixture'
        for kind in ['charge.refunded', 'charge.dispute.created']:
            for change in [{'customer': 'cus_other'}, {'amount_refunded': 0, 'disputed': False}]:
                with self.subTest(kind=kind, change=change):
                    self.charge.update(customer='cus_fixture', amount_refunded=1000, disputed=False)
                    self.charge.update(change)
                    event_data = {'object': {'id': 'dp_fixture', 'charge': 'ch_fixture'}} if 'dispute' in kind else {'object': {'id': 'ch_fixture'}}
                    with self.assertRaises(OperationConflictError):
                        self.webhook(kind, 'ch_fixture', data=event_data)
        self.assertEqual(self.calls('revoke_current_billing'), [])

    def test_matching_refund_revokes_only_verified_purchase(self):
        self.row['stripe_customer_id'] = 'cus_fixture'
        self.charge['amount_refunded'] = 1000
        self.webhook('charge.refunded', 'ch_fixture')
        revoked = self.calls('revoke_current_billing')
        self.assertEqual(len(revoked), 1)
        self.assertEqual(revoked[0]['p_checkout_id'], C)
        self.assertEqual(revoked[0]['p_reason'], 'refunded')
        self.assert_no_grant()

    def test_paid_historical_invoice_backfills_its_period_not_latest_invoice(self):
        self.subscription()
        self.sub['latest_invoice'] = 'in_later'
        self.sub['current_period_start'] += 30 * 86400
        self.sub['current_period_end'] += 30 * 86400
        self.webhook('invoice.paid', 'in_fixture')
        recorded = self.calls('record_current_subscription_period')[0]
        self.assertEqual(recorded['p_invoice_id'], 'in_fixture')
        expected = datetime.fromtimestamp(self.invoice['lines']['data'][0]['period']['start'], timezone.utc).isoformat()
        self.assertEqual(recorded['p_period_start'], expected)
        self.assertEqual([c.args[0] for c in self.provider.v1.invoices.retrieve.call_args_list], ['in_fixture'])

    def test_renewal_recovers_missing_usage_attachment_before_period_grant(self):
        self.subscription()
        self.usage_snapshot()
        self.invoice['lines']['data'].append({'id': 'il_extra', 'type': 'invoiceitem',
            'invoice_item': 'ii_fixture', 'amount': 200, 'price': price('additional_urls')})
        for key in ['total', 'subtotal', 'amount_due', 'amount_paid']:
            self.invoice[key] = 8100
        self.intent.update(amount=8100, amount_received=8100)
        self.charge['amount'] = 8100
        self.provider.v1.invoice_items.list.return_value = sdk({'data': [self.item], 'has_more': False})
        def attach_then_read(name, params):
            if name == 'attach_current_usage_invoice_item':
                self.rpc_results['close_current_usage']['stripe_invoice_item_id'] = params['p_item_id']
            return self.rpc(name, params)
        self.db.rpc.side_effect = attach_then_read
        self.webhook('invoice.paid', 'in_fixture')
        self.provider.v1.invoice_items.create.assert_not_called()
        self.assertEqual(len(self.calls('attach_current_usage_invoice_item')), 1)
        self.assertEqual(len(self.calls('record_current_subscription_period')), 1)

    def test_missing_approved_extra_line_never_grants_renewal(self):
        self.subscription()
        self.usage_snapshot()
        self.rpc_results['close_current_usage']['stripe_invoice_item_id'] = 'ii_fixture'
        with self.assertRaises(OperationConflictError):
            self.webhook('invoice.paid', 'in_fixture')
        self.assert_no_grant()

    def test_usage_recovery_rejects_duplicate_paginated_or_wrong_bound_items(self):
        self.subscription()
        self.usage_snapshot()
        for data, has_more, error in [([self.item, self.item], False, OperationConflictError),
                                      ([self.item], True, BillingNotReadyError),
                                      ([{**self.item, 'customer': 'cus_other'}], False, OperationConflictError),
                                      ([{**self.item, 'quantity': 50}], False, OperationConflictError),
                                      ([{**self.item, 'currency': 'eur'}], False, OperationConflictError)]:
            with self.subTest(data=data, has_more=has_more):
                self.provider.v1.invoice_items.list.return_value = sdk({'data': data, 'has_more': has_more})
                with self.assertRaises(error):
                    self.webhook('invoice.created', 'in_fixture')
        self.assertEqual(self.calls('attach_current_usage_invoice_item'), [])
        self.provider.v1.invoice_items.create.assert_not_called()

    def test_refund_wrong_intent_charge_link_cannot_revoke_matching_customer(self):
        self.row['stripe_customer_id'] = 'cus_fixture'
        self.charge['amount_refunded'] = 1000
        for field, wrong in [('latest_charge', 'ch_unrelated'), ('customer', 'cus_other')]:
            original = self.intent[field]
            with self.subTest(field=field), self.assertRaises(OperationConflictError):
                self.intent[field] = wrong
                self.webhook('charge.refunded', 'ch_fixture')
            self.intent[field] = original
        self.assertEqual(self.calls('revoke_current_billing'), [])

    def test_subscription_refund_wrong_invoice_intent_cannot_revoke(self):
        self.subscription()
        self.charge['amount_refunded'] = 7900
        for target, field, wrong in [(self.invoice, 'payment_intent', 'pi_other'),
                                      (self.invoice, 'customer', 'cus_other'),
                                      (self.sub, 'customer', 'cus_other'),
                                      (self.row, 'stripe_subscription_id', 'sub_other')]:
            original = target[field]
            with self.subTest(field=field, wrong=wrong), self.assertRaises(OperationConflictError):
                target[field] = wrong
                self.webhook('charge.refunded', 'ch_fixture')
            target[field] = original
        self.assertEqual(self.calls('revoke_current_billing'), [])

    def test_subscription_refund_scopes_revocation_to_verified_invoice(self):
        self.subscription()
        self.charge['amount_refunded'] = 7900
        self.webhook('charge.refunded', 'ch_fixture')
        self.assertEqual(self.calls('revoke_current_billing'), [{
            'p_checkout_id': C, 'p_subscription_id': 'sub_fixture', 'p_event_id': 'evt_fixture',
            'p_reason': 'refunded', 'p_invoice_id': 'in_fixture'}])
        self.assert_no_grant()

    def test_configuration_fails_closed_before_provider_or_database(self):
        for change in [{'REDIRX_CURRENT_BILLING_ENABLED': 'false'},
                       {'REDIRX_STRIPE_SECRET_KEY': 'sk_live_wrong_mode'},
                       {'REDIRX_STRIPE_WEBHOOK_SECRET': ''}, {'REDIRX_STRIPE_ACCOUNT_ID': ''},
                       {'REDIRX_STRIPE_AGENCY_PRICE_ID': 'price_studio'},
                       {'MCP_CHECKOUT_COMPANION_ORIGIN': 'https://user:pass@example.com'},
                       {'MCP_CHECKOUT_COMPANION_ORIGIN': 'http://evil.example'},
                       {'MCP_CHECKOUT_COMPANION_ORIGIN': 'https://companion.example/redirect?url=evil'}]:
            with self.subTest(change=change), self.assertRaises(BillingNotReadyError):
                CurrentBillingService(SimpleNamespace(client=self.db), stripe_client=self.provider,
                                      config={**self.config, **change})
        self.assertEqual(self.provider.mock_calls, [])
        self.assertEqual(self.db.mock_calls, [])

    def test_portal_customer_and_return_url_are_server_owned(self):
        self.rpc_results['get_current_billing_usage'] = {'stripe_customer_id': 'cus_fixture'}
        self.provider.v1.billing_portal.sessions.create.return_value = sdk({'url': 'https://billing.stripe.com/p/session'})
        self.assertEqual(self.service.portal(U), {'url': 'https://billing.stripe.com/p/session'})
        self.assertEqual(self.calls('get_current_billing_usage'), [{'p_user_id': U, 'p_livemode': False}])
        self.provider.v1.billing_portal.sessions.create.assert_called_once_with(
            params={'customer': 'cus_fixture', 'configuration': 'bpc_fixture',
                    'return_url': 'https://companion.example/billing/current'},
            options={'stripe_version': STRIPE_API_VERSION})
        self.provider.v1.billing_portal.configurations.retrieve.assert_called_once_with(
            'bpc_fixture', options={'stripe_version': STRIPE_API_VERSION})
        self.assert_no_grant()

    def test_portal_rejects_untrusted_provider_redirect(self):
        self.rpc_results['get_current_billing_usage'] = {'stripe_customer_id': 'cus_fixture'}
        for url in ['http://billing.stripe.com/p/session', 'https://billing.stripe.com.evil.example',
                    'https://evil.example', 'https://user:pass@billing.stripe.com/p/session']:
            self.provider.v1.billing_portal.sessions.create.return_value = sdk({'url': url})
            with self.subTest(url=url), self.assertRaises(BillingNotReadyError):
                self.service.portal(U)

    def test_closed_checkout_retry_does_not_create_another_provider_session(self):
        self.row.update(status='paid', stripe_session_id='cs_test_fixture', checkout_url='https://checkout.stripe.com/old')
        result = self.create()
        self.assertEqual(result['status'], 'paid')
        self.assertIsNone(result['checkout_url'])
        self.provider.v1.checkout.sessions.create.assert_not_called()
        self.assert_no_grant()

    def test_disputed_charge_revokes_verified_purchase_without_refund(self):
        self.row['stripe_customer_id'] = 'cus_fixture'
        self.charge['disputed'] = True
        self.webhook('charge.dispute.created', 'dp_fixture',
                     data={'object': {'id': 'dp_fixture', 'charge': 'ch_fixture'}})
        self.assertEqual(len(self.calls('revoke_current_billing')), 1)
        self.assertEqual(self.calls('revoke_current_billing')[0]['p_reason'], 'disputed')
        self.assert_no_grant()

    def test_unsupported_signed_event_is_acknowledged_without_io(self):
        self.assertEqual(self.webhook('customer.created', 'cus_fixture'), {'received': True, 'ignored': True})
        self.assertEqual(self.provider.mock_calls, [])
        self.assertEqual(self.db.mock_calls, [])

    def test_subscription_quantity_bool_and_metadata_owner_do_not_prove_consent(self):
        self.subscription()
        self.sub['items']['data'][0]['quantity'] = True
        with self.assertRaises(OperationConflictError):
            self.webhook('invoice.paid', 'in_fixture')
        self.sub['items']['data'][0]['quantity'] = 1
        self.sub['metadata'] = {**self.metadata, 'redirx_user_id': OTHER}
        with self.assertRaises(OperationConflictError):
            self.webhook('invoice.paid', 'in_fixture')
        self.assert_no_grant()

    def test_missing_portal_configuration_hides_portal_but_allows_checkout_and_usage(self):
        config = dict(self.config)
        config.pop('REDIRX_STRIPE_PORTAL_CONFIGURATION_ID')
        self.service = CurrentBillingService(SimpleNamespace(client=self.db), stripe_client=self.provider, config=config)
        self.rpc_results['get_current_billing_usage'] = {'stripe_customer_id': 'cus_fixture',
                                                       'portal_available': True, 'used_old_urls': 51}
        usage = self.service.usage(U)
        self.assertFalse(usage['portal_available'])
        self.assertEqual(usage['used_old_urls'], 51)
        self.assertEqual(self.provider.mock_calls, [])
        with self.assertRaises(BillingNotReadyError):
            self.service.portal(U)
        self.assertEqual(self.provider.mock_calls, [])
        self.assertEqual(self.create()['status'], 'open')

    def test_portal_checks_configuration_identity_environment_and_features_before_creation(self):
        original = copy.deepcopy(self.portal_configuration)
        cases = [({'id': 'bpc_other'}, OperationConflictError),
                 ({'livemode': True}, OperationConflictError),
                 ({'active': False}, BillingNotReadyError),
                 ({'active': 1}, BillingNotReadyError),
                 ({'features': None}, BillingNotReadyError),
                 ({'features': {}}, BillingNotReadyError)]
        for update in [{'enabled': True}, {'enabled': 0}, {}]:
            cases.append(({'features': {**original['features'], 'subscription_update': update}}, BillingNotReadyError))
        for cancel in [{'enabled': False, 'mode': 'at_period_end'},
                       {'enabled': True, 'mode': 'immediately'},
                       {'enabled': 1, 'mode': 'at_period_end'}, {'enabled': True}, {}]:
            cases.append(({'features': {**original['features'], 'subscription_cancel': cancel}}, BillingNotReadyError))
        for change, error in cases:
            with self.subTest(change=change), self.assertRaises(error):
                self.portal_configuration = {**original, **change}
                self.service.portal(U)
        self.provider.v1.billing_portal.sessions.create.assert_not_called()
        self.db.rpc.assert_not_called()

    def test_portal_configuration_retrieval_failure_never_uses_default(self):
        self.provider.v1.billing_portal.configurations.retrieve.side_effect = RuntimeError('provider unavailable')
        with self.assertRaises(BillingNotReadyError):
            self.service.portal(U)
        self.provider.v1.billing_portal.sessions.create.assert_not_called()
        self.db.rpc.assert_not_called()

    def test_configured_usage_preserves_database_portal_eligibility_without_provider_io(self):
        for available in [False, True]:
            with self.subTest(available=available):
                self.rpc_results['get_current_billing_usage'] = {'portal_available': available}
                self.assertIs(self.service.usage(U)['portal_available'], available)
        self.assertEqual(self.provider.mock_calls, [])


if __name__ == '__main__':
    unittest.main()
