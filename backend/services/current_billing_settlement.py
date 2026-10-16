"""Collect frozen, explicitly approved final usage without granting a new period."""
import time
from datetime import datetime

from .current_pricing import VERSION
from .migration_checkout_service import _dict
from .migration_repository import OperationConflictError


class TerminalUsageSettlement:
    def _settle_terminal_usage(self, sub, row, event_id):
        from .current_billing_service import BillingNotReadyError

        # SQL serializes this reservation with admissions and cycle invoice binding.
        # It disables new admissions even when there are no extras to collect.
        saved = self._rpc('reserve_current_terminal_usage',
                          p_subscription_id=sub['id'], p_event_id=event_id + ':terminal')
        if not saved.get('extra_old_urls'):
            return {'received': True}
        if (saved.get('checkout_id') != row['id']
                or saved.get('stripe_subscription_id') != sub['id']
                or saved.get('stripe_customer_id') != sub.get('customer')
                or saved.get('livemode') is not self.livemode):
            raise OperationConflictError('The final usage reservation has a different owner.')
        if not saved.get('terminal_settlement'):
            # A cycle already owns collection, including an interrupted item write.
            # Repair that invoice; never create a second standalone charge.
            invoice = self._retrieve(self.stripe.v1.invoices, saved.get('usage_invoice_id'), 'in_')
            if (invoice.get('customer') != sub['customer'] or invoice.get('subscription') != sub['id']
                    or invoice.get('currency') != 'usd'
                    or invoice.get('status') not in {'draft', 'open', 'paid'}):
                raise OperationConflictError('The cycle usage invoice requires reconciliation.')
            item_id = self._attach_usage_item(invoice, sub['customer'], saved)
            invoice = self._retrieve(self.stripe.v1.invoices, invoice['id'], 'in_')
            expected = row['amount_cents'] + saved['extra_amount_cents']
            if (invoice.get('customer') != sub['customer'] or invoice.get('subscription') != sub['id']
                    or invoice.get('currency') != 'usd' or invoice.get('status') != 'paid'
                    or invoice.get('paid') is not True or invoice.get('paid_out_of_band') is not False
                    or invoice.get('amount_remaining') != 0
                    or any(type(invoice.get(k)) is not int or invoice[k] != expected
                           for k in ('total', 'subtotal', 'amount_due', 'amount_paid'))):
                raise BillingNotReadyError('The cycle usage invoice is not verifiably paid; reconcile collection.')
            lines = _dict(invoice.get('lines'))
            data = lines.get('data')
            if not isinstance(data, list) or lines.get('has_more') is not False or len(data) != 2:
                raise BillingNotReadyError('The cycle usage invoice lines require reconciliation.')
            base = [line for line in data if line.get('type') == 'subscription'
                    and line.get('subscription_item') == sub['items']['data'][0]['id']
                    and line.get('amount') == row['amount_cents'] and line.get('quantity') == 1
                    and line.get('proration') is False
                    and _dict(line.get('price')).get('id') == row['stripe_price_id']]
            extra = [line for line in data if line.get('invoice_item') == item_id
                     and line.get('amount') == saved['extra_amount_cents']
                     and line.get('quantity') == saved['extra_old_urls']
                     and _dict(line.get('price')).get('id') == self.prices['additional_urls']]
            if len(base) != 1 or len(extra) != 1 or base[0] is extra[0]:
                raise BillingNotReadyError('The cycle usage invoice does not prove the agreed usage payment.')
            try:
                self._intent(invoice.get('payment_intent'), expected,
                             customer=sub['customer'], invoice_id=invoice['id'])
            except OperationConflictError:
                raise BillingNotReadyError('The cycle usage payment requires reconciliation.') from None
            return {'received': True}
        metadata = {**self._metadata(row), 'redirx_period_id': saved['id'],
                    'redirx_usage_settlement': 'terminal'}
        invoice_id = saved.get('usage_invoice_id')
        if invoice_id:
            invoice = self._retrieve(self.stripe.v1.invoices, invoice_id, 'in_')
        else:
            # A lost create response must be recovered before another create, even
            # after the provider idempotency window. Bound the scan; never guess.
            result = self._provider(self.stripe.v1.invoices.list, params={
                'customer': sub['customer'], 'limit': 100,
                'created': {'gte': int(datetime.fromisoformat(
                    saved['closed_at'].replace('Z', '+00:00')).timestamp()) - 300}})
            if not isinstance(result.get('data'), list) or result.get('has_more') is not False:
                raise BillingNotReadyError('Final usage invoices require reconciliation.')
            matches = [i for i in result['data'] if _dict(i.get('metadata')) == metadata]
            if len(matches) > 1:
                raise OperationConflictError('Duplicate final usage invoices require reconciliation.')
            if matches:
                invoice = self._retrieve(self.stripe.v1.invoices, matches[0].get('id'), 'in_')
            else:
                if datetime.fromisoformat(saved['closed_at'].replace('Z', '+00:00')).timestamp() < time.time() - 23 * 3600:
                    raise BillingNotReadyError('The final usage delivery window expired; reconcile before retrying.')
                params = {'customer': sub['customer'], 'metadata': metadata, 'currency': 'usd',
                          'collection_method': 'charge_automatically', 'auto_advance': False,
                          'pending_invoice_items_behavior': 'exclude',
                          'automatic_tax': {'enabled': False}, 'default_tax_rates': [], 'discounts': ''}
                # Checkout subscriptions can store the payment method on the
                # subscription rather than the customer's invoice settings.
                for key in ('default_payment_method', 'default_source'):
                    if sub.get(key):
                        params[key] = sub[key]
                invoice = self._provider(self.stripe.v1.invoices.create, params=params,
                                         key=f'{VERSION}:terminal-invoice:{saved["id"]}')
        self._validate_terminal_invoice(invoice, saved, metadata)
        self._rpc('attach_current_terminal_usage_invoice',
                  p_period_id=saved['id'], p_invoice_id=invoice['id'])
        saved = {**saved, 'usage_invoice_id': invoice['id']}
        item_id = self._attach_usage_item(invoice, sub['customer'], saved)
        # Retrieve after the item write; never finalize an unchecked total or
        # unrelated pending items. This also recovers an unknown finalize result.
        invoice = self._retrieve(self.stripe.v1.invoices, invoice['id'], 'in_')
        self._validate_terminal_invoice(invoice, saved, metadata)
        self._validate_terminal_contents(invoice, saved, item_id)
        if invoice.get('status') == 'draft':
            invoice = self._provider(self.stripe.v1.invoices.finalize_invoice, invoice['id'],
                                     params={'auto_advance': True},
                                     key=f'{VERSION}:terminal-finalize:{saved["id"]}')
            self._validate_terminal_invoice(invoice, saved, metadata)
            self._validate_terminal_contents(invoice, saved, item_id)
        if invoice.get('status') not in {'open', 'paid'}:
            raise BillingNotReadyError('Final usage collection requires reconciliation.')
        return {'received': True}

    def _validate_terminal_contents(self, invoice, saved, item_id):
        lines = _dict(invoice.get('lines'))
        data = lines.get('data')
        if (lines.get('has_more') is not False or not isinstance(data, list) or len(data) != 1
                or data[0].get('invoice_item') != item_id
                or data[0].get('amount') != saved['extra_amount_cents']
                or data[0].get('quantity') != saved['extra_old_urls']
                or _dict(data[0].get('price')).get('id') != self.prices['additional_urls']
                or any(type(invoice.get(k)) is not int or invoice[k] != saved['extra_amount_cents']
                       for k in ('total', 'subtotal', 'amount_due'))):
            raise OperationConflictError('The final invoice does not match approved extra usage.')

    def _validate_terminal_invoice(self, invoice, saved, metadata):
        if (not isinstance(invoice.get('id'), str) or not invoice['id'].startswith('in_')
                or invoice.get('livemode') is not self.livemode
                or invoice.get('customer') != saved['stripe_customer_id']
                or invoice.get('subscription') is not None
                or invoice.get('metadata') != metadata or invoice.get('currency') != 'usd'
                or invoice.get('collection_method') != 'charge_automatically'
                or (saved.get('usage_invoice_id') and saved['usage_invoice_id'] != invoice['id'])):
            raise OperationConflictError('The final usage invoice has an invalid binding.')
