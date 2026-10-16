"""Offline terminal-usage recovery tests with real SDK method signatures."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, create_autospec

import stripe

from backend.tests import test_current_billing_service as fixtures
from backend.services.current_billing_service import BillingNotReadyError
from backend.services.migration_repository import OperationConflictError


class TerminalSettlementTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.CurrentBillingServiceTests()
        self.f.setUp()
        self.f.subscription()
        self.f.usage_snapshot()
        self.f.sub.update(status='canceled', default_payment_method='pm_fixture')
        self.saved = self.f.rpc_results['close_current_usage']
        self.saved.update(checkout_id=fixtures.C, stripe_subscription_id='sub_fixture',
                          stripe_customer_id='cus_fixture', livemode=False,
                          terminal_settlement=True, usage_invoice_id=None)
        self.f.rpc_results['reserve_current_terminal_usage'] = self.saved
        self.f.rpc_results['attach_current_terminal_usage_invoice'] = self.saved
        self.metadata = {**self.f.metadata, 'redirx_period_id': self.saved['id'],
                         'redirx_usage_settlement': 'terminal'}
        self.invoice = dict(id='in_fixture', livemode=False, customer='cus_fixture',
                            subscription=None, metadata=self.metadata, currency='usd',
                            collection_method='charge_automatically', status='draft',
                            total=0, subtotal=0, amount_due=0,
                            lines={'data': [], 'has_more': False})
        real = stripe.StripeClient('sk_test_fixture').v1.invoices
        api = Mock(spec_set=['create', 'list', 'retrieve', 'finalize_invoice'])
        for method in ['create', 'list', 'retrieve', 'finalize_invoice']:
            setattr(api, method, create_autospec(getattr(real, method)))
        self.api = self.f.provider.v1.invoices = api
        api.list.return_value = fixtures.sdk({'data': [], 'has_more': False})
        api.create.side_effect = lambda **kw: fixtures.sdk(self.invoice)
        api.retrieve.side_effect = lambda *a, **kw: fixtures.sdk(self.invoice)
        api.finalize_invoice.side_effect = self.finalize
        self.f.provider.v1.invoice_items.create.side_effect = self.add_item
        original_rpc = self.f.rpc

        def rpc(name, params):
            if name == 'attach_current_terminal_usage_invoice':
                self.saved['usage_invoice_id'] = params['p_invoice_id']
            if name == 'attach_current_usage_invoice_item':
                self.saved['stripe_invoice_item_id'] = params['p_item_id']
            return original_rpc(name, params)
        self.f.db.rpc.side_effect = rpc

    def add_item(self, **kwargs):
        self.invoice.update(total=200, subtotal=200, amount_due=200)
        self.invoice['lines']['data'] = [{'invoice_item': 'ii_fixture', 'amount': 200,
            'quantity': 51, 'price': fixtures.price('additional_urls')}]
        return fixtures.sdk(self.f.item)

    def finalize(self, *args, **kwargs):
        self.invoice['status'] = 'open'
        return fixtures.sdk(self.invoice)

    def settle(self, event='evt_cancel'):
        return self.f.webhook('customer.subscription.deleted', 'sub_fixture', id=event)

    def test_cancellation_collects_exact_extras_and_replays_without_new_invoice(self):
        self.assertEqual(self.settle(), {'received': True})
        params = self.api.create.call_args.kwargs['params']
        self.assertEqual(params['currency'], 'usd')
        self.assertEqual(params['pending_invoice_items_behavior'], 'exclude')
        self.assertFalse(params['auto_advance'])
        self.assertEqual(params['default_payment_method'], 'pm_fixture')
        self.assertEqual(params['discounts'], '')
        self.assertEqual(self.f.calls('reserve_current_terminal_usage')[0]['p_event_id'], 'evt_cancel:terminal')
        self.assertEqual(self.api.finalize_invoice.call_args.kwargs['params'], {'auto_advance': True})
        self.settle('evt_cancel_again')
        self.assertEqual(self.api.create.call_count, 1)
        self.assertEqual(self.f.provider.v1.invoice_items.create.call_count, 1)
        self.assertEqual(self.api.finalize_invoice.call_count, 1)
        self.f.assert_no_grant()

    def test_zero_and_missing_period_do_not_create_invoices(self):
        for missing in (False, True):
            self.saved.update(extra_old_urls=0, extra_amount_cents=0, missing_period=missing)
            self.settle()
        self.api.create.assert_not_called()
        self.api.list.assert_not_called()

    def test_cycle_owned_interrupted_item_write_is_repaired_without_standalone(self):
        self.saved.update(terminal_settlement=False, usage_invoice_id='in_fixture')
        self.invoice['subscription'] = 'sub_fixture'
        with self.assertRaises(BillingNotReadyError):
            self.settle()
        self.api.create.assert_not_called()
        self.api.finalize_invoice.assert_not_called()
        self.assertEqual(self.f.provider.v1.invoice_items.create.call_count, 1)

    def test_cycle_owned_missing_item_on_paid_invoice_fails_instead_of_acknowledging(self):
        self.saved.update(terminal_settlement=False, usage_invoice_id='in_fixture')
        self.invoice.update(subscription='sub_fixture', status='paid')
        with self.assertRaises(BillingNotReadyError):
            self.settle()
        self.api.create.assert_not_called()

    def test_wrong_currency_rejected_before_attach_or_finalize(self):
        self.invoice['currency'] = 'eur'
        with self.assertRaises(OperationConflictError):
            self.settle()
        self.assertEqual(self.f.calls('attach_current_terminal_usage_invoice'), [])
        self.api.finalize_invoice.assert_not_called()

    def test_lost_create_response_recovers_existing_invoice_after_24_hours(self):
        self.api.create.side_effect = RuntimeError('lost response')
        with self.assertRaises(BillingNotReadyError):
            self.settle()
        self.saved['closed_at'] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        self.api.list.return_value = fixtures.sdk({'data': [self.invoice], 'has_more': False})
        # Existing item is also reconciled beyond the delivery window.
        self.add_item()
        self.f.provider.v1.invoice_items.list.return_value = fixtures.sdk({'data': [self.f.item], 'has_more': False})
        self.settle()
        self.assertEqual(self.api.create.call_count, 1)
        self.f.provider.v1.invoice_items.create.assert_not_called()

    def test_expired_unknown_create_never_recreates(self):
        self.saved['closed_at'] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        with self.assertRaises(BillingNotReadyError):
            self.settle()
        self.api.create.assert_not_called()

    def test_incomplete_or_duplicate_invoice_reconciliation_fails_closed(self):
        for data, more in [([], True), ([self.invoice, self.invoice], False)]:
            self.api.list.return_value = fixtures.sdk({'data': data, 'has_more': more})
            with self.assertRaises((BillingNotReadyError, OperationConflictError)):
                self.settle()
        self.api.create.assert_not_called()

    def test_lost_item_response_is_recovered_without_duplicate_item(self):
        def lost(**kwargs):
            self.add_item(**kwargs)
            raise RuntimeError('lost item response')
        self.f.provider.v1.invoice_items.create.side_effect = lost
        with self.assertRaises(BillingNotReadyError):
            self.settle()
        self.f.provider.v1.invoice_items.list.return_value = fixtures.sdk({'data': [self.f.item], 'has_more': False})
        self.settle()
        self.assertEqual(self.f.provider.v1.invoice_items.create.call_count, 1)
        self.assertEqual(self.api.create.call_count, 1)

    def test_lost_finalize_response_is_recovered_without_new_invoice(self):
        def lost(*args, **kwargs):
            self.finalize(*args, **kwargs)
            raise RuntimeError('lost finalize response')
        self.api.finalize_invoice.side_effect = lost
        with self.assertRaises(BillingNotReadyError):
            self.settle()
        self.settle()
        self.assertEqual(self.api.finalize_invoice.call_count, 1)
        self.assertEqual(self.api.create.call_count, 1)

    def test_extra_invoice_line_blocks_finalization(self):
        def unrelated(**kwargs):
            result = self.add_item(**kwargs)
            self.invoice['lines']['data'].append(copy.deepcopy(self.invoice['lines']['data'][0]))
            return result
        self.f.provider.v1.invoice_items.create.side_effect = unrelated
        with self.assertRaises(OperationConflictError):
            self.settle()
        self.api.finalize_invoice.assert_not_called()

    def test_standalone_paid_callback_does_not_grant_capacity(self):
        self.invoice['status'] = 'paid'
        self.assertEqual(self.f.webhook('invoice.paid', 'in_fixture'), {'received': True, 'ignored': True})
        self.f.assert_no_grant()

    def cycle_paid(self):
        self.saved.update(terminal_settlement=False, usage_invoice_id='in_fixture',
                          stripe_invoice_item_id='ii_fixture')
        self.invoice = copy.deepcopy(self.f.invoice)
        self.invoice['lines']['data'].append({'invoice_item': 'ii_fixture', 'amount': 200,
            'quantity': 51, 'price': fixtures.price('additional_urls')})
        for key in ['total', 'subtotal', 'amount_due', 'amount_paid']:
            self.invoice[key] = 8100
        self.f.intent.update(amount=8100, amount_received=8100)
        self.f.charge['amount'] = 8100

    def test_cycle_paid_requires_exact_invoice_and_charge_without_new_invoice(self):
        self.cycle_paid()
        self.assertEqual(self.settle(), {'received': True})
        self.api.create.assert_not_called()
        self.api.finalize_invoice.assert_not_called()
        self.f.provider.v1.invoice_items.create.assert_not_called()
        self.f.provider.v1.charges.retrieve.assert_called_once()
        self.f.assert_no_grant()

    def test_cycle_unpaid_or_unverified_payment_requires_reconciliation(self):
        self.cycle_paid()
        original = copy.deepcopy(self.invoice)
        for change in [{'status': 'open'}, {'status': 'draft'}, {'paid': False},
                       {'paid_out_of_band': True}, {'amount_paid': 7900}, {'total': 8101},
                       {'amount_remaining': 200}, {'lines': {'data': [], 'has_more': False}}]:
            with self.subTest(change=change):
                self.invoice = {**original, **change}
                with self.assertRaises(BillingNotReadyError):
                    self.settle()
        self.invoice = original
        self.f.charge['disputed'] = True
        with self.assertRaises(BillingNotReadyError):
            self.settle()
        self.api.create.assert_not_called()
        self.api.finalize_invoice.assert_not_called()
        self.f.assert_no_grant()

    def test_cycle_paid_wrong_extra_item_cannot_acknowledge_settlement(self):
        self.cycle_paid()
        self.invoice['lines']['data'][1]['invoice_item'] = 'ii_other'
        with self.assertRaises(BillingNotReadyError):
            self.settle()
        self.api.create.assert_not_called()

    def test_create_timeout_retry_reuses_identical_invoice_parameters_and_key(self):
        self.api.create.side_effect = [RuntimeError('unknown outcome'), fixtures.sdk(self.invoice)]
        with self.assertRaises(BillingNotReadyError):
            self.settle()
        self.settle()
        first, second = self.api.create.call_args_list
        self.assertEqual(first, second)
        self.assertEqual(first.kwargs['options']['idempotency_key'],
                         fixtures.VERSION + ':terminal-invoice:period_fixture')
        self.assertEqual(self.api.finalize_invoice.call_args.kwargs['options']['idempotency_key'],
                         fixtures.VERSION + ':terminal-finalize:period_fixture')

    def test_exact_terminal_totals_and_line_bindings_before_finalization(self):
        cases = [('total', 201), ('subtotal', 199), ('amount_due', True),
                 ('invoice_item', 'ii_other'), ('quantity', 50), ('amount', 199),
                 ('price', fixtures.price('payg')), ('has_more', True)]
        for field, wrong in cases:
            with self.subTest(field=field):
                # Independent reservations keep prior rejected snapshots from masking this case.
                self.setUp()
                def bad_item(**kwargs):
                    result = self.add_item(**kwargs)
                    if field in {'total', 'subtotal', 'amount_due'}:
                        self.invoice[field] = wrong
                    elif field == 'has_more':
                        self.invoice['lines'][field] = wrong
                    else:
                        self.invoice['lines']['data'][0][field] = wrong
                    return result
                self.f.provider.v1.invoice_items.create.side_effect = bad_item
                with self.assertRaises(OperationConflictError):
                    self.settle()
                self.api.finalize_invoice.assert_not_called()
                self.f.assert_no_grant()

    def test_finalize_response_rechecks_invoice_identity_and_totals(self):
        for field, wrong in [('id', 'in_other'), ('total', 201), ('customer', 'cus_other')]:
            with self.subTest(field=field):
                self.setUp()
                def changed(*args, **kwargs):
                    self.invoice.update(status='open')
                    self.invoice[field] = wrong
                    return fixtures.sdk(self.invoice)
                self.api.finalize_invoice.side_effect = changed
                with self.assertRaises(OperationConflictError):
                    self.settle()
                self.f.assert_no_grant()

    def test_reservation_wrong_owner_is_rejected_before_any_invoice_io(self):
        self.saved['stripe_customer_id'] = 'cus_other'
        with self.assertRaises(OperationConflictError):
            self.settle()
        self.assertEqual(self.api.mock_calls, [])

    def test_retrieved_terminal_invoice_wrong_metadata_is_not_attached(self):
        self.saved['usage_invoice_id'] = 'in_fixture'
        self.invoice['metadata'] = {**self.metadata, 'redirx_user_id': fixtures.OTHER}
        with self.assertRaises(OperationConflictError):
            self.settle()
        self.assertEqual(self.f.calls('attach_current_terminal_usage_invoice'), [])
        self.api.finalize_invoice.assert_not_called()


if __name__ == '__main__':
    unittest.main()
