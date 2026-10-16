"""Current catalog Checkout and retrieved payment verification.

Database RPCs own inventory counts, consent, idempotency and allowance mutation.
Browser returns never grant rights. This module never changes Stripe settings.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

import stripe

from .current_pricing import PLANS, VERSION, STRIPE_API_VERSION, validate_price
from .current_billing_settlement import TerminalUsageSettlement
from .migration_checkout_service import _dict, _hosted_url
from .migration_planning_service import validate_key
from .migration_repository import (
    MigrationRepository, MigrationRepositoryError, InvalidInputError,
    MigrationNotFoundError, OperationConflictError, RepositoryUnavailableError, _strict_uuid,
)
from .inventory_import_service import ImportCapacityExceededError
from .migration_quote_service import InventoryIncompleteError


def enabled():
    return os.getenv('REDIRX_CURRENT_BILLING_ENABLED', 'false').lower() == 'true'


class BillingNotReadyError(MigrationRepositoryError):
    code = 'not_ready'
    retryable = True
    next_action = 'retry'


def _uuid(value):
    return _strict_uuid(value, 'identifier')[1]


def _timestamp(value):
    if type(value) is not int or value <= 0:
        raise OperationConflictError('Invalid payment period.')
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def public_checkout(row):
    return {
        'checkout_id': row['id'], 'sku': row['sku'], 'status': row['status'],
        'checkout_url': row.get('checkout_url') if row['status'] == 'open' else None,
        'amount_cents': row['amount_cents'], 'currency': 'usd',
        'migration_id': row.get('migration_id'), 'expires_at': row['expires_at'],
    }


class CurrentBillingService(TerminalUsageSettlement):
    def __init__(self, repository=None, *, stripe_client=None, config=None):
        env = os.environ if config is None else config
        if env.get('REDIRX_CURRENT_BILLING_ENABLED', 'false').lower() != 'true':
            raise BillingNotReadyError('Paid checkout is not enabled.')
        mode = env.get('REDIRX_STRIPE_MODE', 'test')
        key = env.get('REDIRX_STRIPE_SECRET_KEY', '')
        if mode not in {'test', 'live'} or not key.startswith('sk_' + mode + '_'):
            raise BillingNotReadyError('The Stripe environment is not configured.')
        self.livemode = mode == 'live'
        self.account_id = env.get('REDIRX_STRIPE_ACCOUNT_ID', 'acct_1T1fwzReyTnSoYHg' if self.livemode else '')
        self.secret = env.get('REDIRX_STRIPE_WEBHOOK_SECRET', '')
        self.portal_configuration_id = env.get('REDIRX_STRIPE_PORTAL_CONFIGURATION_ID', '')
        if not self.account_id.startswith('acct_') or not self.secret.startswith('whsec_'):
            raise BillingNotReadyError('Stripe account and webhook verification must be configured.')
        self.prices = {sku: env.get('REDIRX_STRIPE_' + sku.upper() + '_PRICE_ID',
                                   plan.live_price_id if self.livemode else '') for sku, plan in PLANS.items()}
        if any(not value.startswith('price_') for value in self.prices.values()) or len(set(self.prices.values())) != 4:
            raise BillingNotReadyError('Four distinct current catalog prices must be configured.')
        self.origin = env.get('MCP_CHECKOUT_COMPANION_ORIGIN', '').rstrip('/')
        parsed = urlsplit(self.origin)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
                or (parsed.scheme != 'https' and not (not self.livemode and parsed.scheme == 'http'
                    and parsed.hostname in {'localhost', '127.0.0.1', '::1'}))):
            raise BillingNotReadyError('A trusted checkout return origin is required.')
        self.repository = repository if repository is not None else MigrationRepository()
        self.stripe = stripe_client if stripe_client is not None else stripe.StripeClient(
            key, stripe_version=STRIPE_API_VERSION, max_network_retries=2)
        self._account_checked = False

    def _provider(self, method, *args, params=None, key=None):
        options = {'stripe_version': STRIPE_API_VERSION}
        if key:
            options['idempotency_key'] = key
        try:
            kwargs = {'options': options}
            if params is not None:
                kwargs['params'] = params
            return _dict(method(*args, **kwargs))
        except Exception:
            raise BillingNotReadyError('Payment verification is unavailable. Retry the same request.') from None

    def _account(self):
        if not self._account_checked:
            account = self._provider(self.stripe.v1.accounts.retrieve_current)
            if account.get('id') != self.account_id:
                raise OperationConflictError('The Stripe key belongs to a different account.')
            self._account_checked = True

    def _retrieve(self, resource, identifier, prefix):
        if not isinstance(identifier, str) or not identifier.startswith(prefix):
            raise OperationConflictError('Missing payment provider identity.')
        value = self._provider(resource.retrieve, identifier)
        if value.get('id') != identifier or value.get('livemode') is not self.livemode:
            raise OperationConflictError('The payment belongs to a different environment.')
        return value

    def _rpc(self, name, **params):
        try:
            result = self.repository.client.rpc(name, params).execute()
            if getattr(result, 'error', None):
                raise result.error
            value = result.data
            if isinstance(value, list) and len(value) == 1:
                value = value[0]
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except Exception as exc:
            code = str(getattr(exc, 'message', '') or str(exc))
            if getattr(exc, 'code', None) == 'P0001':
                cls = {'not_found': MigrationNotFoundError, 'invalid_input': InvalidInputError,
                       'operation_conflict': OperationConflictError,
                       'subscription_exists': OperationConflictError,
                       'inventory_incomplete': InventoryIncompleteError,
                       'capacity_exceeded': ImportCapacityExceededError,
                       'checkout_expired': OperationConflictError}.get(code)
                if cls:
                    raise cls('The billing request could not be completed: ' + code.replace('_', ' ') + '.') from None
            raise RepositoryUnavailableError('Billing storage is temporarily unavailable.') from None

    def _row(self, checkout_id):
        try:
            result = self.repository.client.table('redirx_billing_checkouts').select('*').eq(
                'id', _uuid(checkout_id)).maybe_single().execute()
            row = result.data
            if getattr(result, 'error', None) or not isinstance(row, dict):
                raise ValueError()
        except Exception:
            raise RepositoryUnavailableError('The payment reservation is unavailable.') from None
        if row.get('livemode') is not self.livemode:
            raise OperationConflictError('The checkout belongs to a different environment.')
        return row

    @staticmethod
    def _metadata(row):
        return {'redirx_catalog': VERSION, 'redirx_checkout_id': row['id'],
                'redirx_user_id': row['user_id'], 'redirx_sku': row['sku']}

    def _price(self, sku, price_id=None):
        identifier = price_id or self.prices[sku]
        value = self._retrieve(self.stripe.v1.prices, identifier, 'price_')
        try:
            validate_price(value, sku, identifier, self.livemode)
        except ValueError as exc:
            raise OperationConflictError(str(exc)) from None
        return value

    def create_checkout(self, user_id, sku, key, *, migration_id=None, inventory_ids=None, recurring_consent=False):
        user = _uuid(user_id)
        if sku not in {'payg', 'studio', 'agency'} or type(recurring_consent) is not bool:
            raise InvalidInputError('Choose pay as you go, Studio or Agency.')
        if sku != 'payg' and recurring_consent is not True:
            raise InvalidInputError('Confirm the monthly subscription before continuing.')
        old = new = migration = None
        if sku == 'payg':
            if not isinstance(inventory_ids, dict) or set(inventory_ids) != {'old', 'new'}:
                raise InvalidInputError('Select the old and new URL inventories for this purchase.')
            migration, old, new = map(_uuid, (migration_id, inventory_ids['old'], inventory_ids['new']))
        elif migration_id is not None or inventory_ids is not None:
            raise InvalidInputError('Monthly plans are shared across your account.')
        self._account()
        self._price(sku)
        row = self._rpc('reserve_current_checkout', p_user_id=user, p_sku=sku, p_migration_id=migration,
                        p_old_inventory_id=old, p_new_inventory_id=new, p_key=validate_key(key),
                        p_recurring_consent=recurring_consent, p_price_id=self.prices[sku], p_livemode=self.livemode)
        if row.get('user_id') != user or row.get('sku') != sku or row.get('livemode') is not self.livemode:
            raise RepositoryUnavailableError('The billing reservation has an invalid owner.')
        if row['status'] != 'reserved':
            return public_checkout(row)
        expiry = int(datetime.fromisoformat(row['expires_at'].replace('Z', '+00:00')).timestamp())
        if expiry <= time.time() + 1800:
            raise BillingNotReadyError('This checkout expired. Start a new request.')
        metadata = self._metadata(row)
        returns = self.origin + '/billing/current/return?' + urlencode({'checkout_id': row['id']})
        params = {'mode': 'payment' if sku == 'payg' else 'subscription',
                  'line_items': [{'price': row['stripe_price_id'], 'quantity': row['quantity']}],
                  'client_reference_id': row['id'], 'metadata': metadata,
                  'payment_method_types': ['card'], 'allow_promotion_codes': False,
                  'automatic_tax': {'enabled': False}, 'expires_at': expiry,
                  'success_url': returns, 'cancel_url': returns}
        if sku == 'payg':
            params['payment_intent_data'] = {'metadata': metadata}
            params['customer_creation'] = 'always'
        else:
            params['subscription_data'] = {'metadata': metadata}
            params['custom_text'] = {'submit': {'message':
                f'${PLANS[sku].amount_cents // 100} USD each month until canceled. Extra URLs require your approval.'}}
        session = self._provider(self.stripe.v1.checkout.sessions.create, params=params,
                                 key=f'{VERSION}:checkout:{row["id"]}')
        self._validate_session(session, row)
        saved = self._rpc('attach_current_checkout', p_checkout_id=row['id'],
                          p_session_id=session['id'], p_url=_hosted_url(session.get('url')))
        return public_checkout(saved)

    def _validate_session(self, session, row):
        expected_mode = 'payment' if row['sku'] == 'payg' else 'subscription'
        if (session.get('livemode') is not self.livemode or session.get('mode') != expected_mode
                or not isinstance(session.get('id'), str) or not session['id'].startswith('cs_')
                or session.get('metadata') != self._metadata(row) or session.get('client_reference_id') != row['id']
                or session.get('currency') != 'usd'
                or session.get('expires_at') != int(datetime.fromisoformat(row['expires_at'].replace('Z', '+00:00')).timestamp())
                or any(type(session.get(k)) is not int or session[k] != row['amount_cents']
                       for k in ('amount_total', 'amount_subtotal'))
                or (row.get('stripe_session_id') and session['id'] != row['stripe_session_id'])):
            raise OperationConflictError('The checkout does not match its saved purchase.')

    def get_checkout(self, user_id, checkout_id):
        row = self._rpc('get_current_checkout', p_user_id=_uuid(user_id), p_checkout_id=_uuid(checkout_id))
        if row.get('livemode') is not self.livemode:
            raise MigrationNotFoundError('Checkout not found.')
        return public_checkout(row)

    def usage(self, user_id):
        result = self._rpc('get_current_billing_usage', p_user_id=_uuid(user_id), p_livemode=self.livemode)
        if not self.portal_configuration_id.startswith('bpc_'):
            result = {**result, 'portal_available': False}
        return result

    def portal(self, user_id):
        if not self.portal_configuration_id.startswith('bpc_'):
            raise BillingNotReadyError('A safe billing portal configuration is required.')
        self._account()
        configuration = self._retrieve(self.stripe.v1.billing_portal.configurations,
                                       self.portal_configuration_id, 'bpc_')
        features = configuration.get('features')
        update = features.get('subscription_update') if isinstance(features, dict) else None
        cancel = features.get('subscription_cancel') if isinstance(features, dict) else None
        if (configuration.get('active') is not True
                or not isinstance(update, dict) or update.get('enabled') is not False
                or not isinstance(cancel, dict) or cancel.get('enabled') is not True
                or cancel.get('mode') != 'at_period_end'):
            raise BillingNotReadyError('The billing portal configuration must disable plan changes and allow cancellation at period end.')
        usage = self.usage(user_id)
        customer = usage.get('stripe_customer_id')
        if not isinstance(customer, str) or not customer.startswith('cus_'):
            raise InvalidInputError('No subscription customer is available.')
        session = self._provider(self.stripe.v1.billing_portal.sessions.create,
                                 params={'customer': customer, 'configuration': self.portal_configuration_id,
                                         'return_url': self.origin + '/billing/current'})
        url = session.get('url', '')
        if urlsplit(url).scheme != 'https' or urlsplit(url).netloc != 'billing.stripe.com':
            raise BillingNotReadyError('The billing portal is unavailable.')
        return {'url': url}

    def _lines(self, session, row):
        result = self._provider(self.stripe.v1.checkout.sessions.line_items.list, session['id'], params={'limit': 2})
        data = result.get('data')
        if (result.get('has_more') is not False or not isinstance(data, list) or len(data) != 1
                or data[0].get('quantity') != row['quantity']
                or _dict(data[0].get('price')).get('id') != row['stripe_price_id']):
            raise OperationConflictError('The payment line does not match the saved purchase.')

    def _intent(self, intent_id, amount, *, customer, metadata=None, invoice_id=None):
        intent = self._retrieve(self.stripe.v1.payment_intents, intent_id, 'pi_')
        if (intent.get('status') != 'succeeded' or intent.get('currency') != 'usd'
                or intent.get('customer') != customer
                or any(type(intent.get(k)) is not int or intent[k] != amount for k in ('amount', 'amount_received'))
                or (metadata is not None and intent.get('metadata') != metadata)
                or (invoice_id is not None and intent.get('invoice') != invoice_id)):
            raise OperationConflictError('The payment intent does not prove this payment.')
        charge = self._retrieve(self.stripe.v1.charges, intent.get('latest_charge'), 'ch_')
        if (charge.get('payment_intent') != intent['id'] or charge.get('customer') != customer
                or charge.get('paid') is not True or charge.get('currency') != 'usd'
                or charge.get('amount') != amount or charge.get('amount_refunded') != 0
                or charge.get('disputed') is not False):
            raise OperationConflictError('The payment is refunded, disputed or unverified.')

    def _subscription(self, sub, row):
        if sub.get('metadata') != self._metadata(row):
            raise OperationConflictError('The subscription has no matching purchase consent.')
        if row.get('stripe_subscription_id') and row['stripe_subscription_id'] != sub['id']:
            raise OperationConflictError('The subscription identity does not match.')
        if row.get('stripe_customer_id') and row['stripe_customer_id'] != sub.get('customer'):
            raise OperationConflictError('The subscription customer does not match.')
        items = _dict(sub.get('items'))
        data = items.get('data')
        if not isinstance(data, list) or len(data) != 1 or items.get('has_more') is not False:
            raise OperationConflictError('A single monthly subscription item is required.')
        price = _dict(data[0].get('price'))
        try:
            # Existing subscriptions may reference a subsequently archived price.
            validate_price({**price, 'active': True}, row['sku'], row['stripe_price_id'], self.livemode)
        except ValueError as exc:
            raise OperationConflictError(str(exc)) from None
        if type(data[0].get('quantity')) is not int or data[0]['quantity'] != 1:
            raise OperationConflictError('The monthly subscription quantity must be one.')
        return data[0]

    def _renewal(self, sub, row, event_id, invoice=None):
        item = self._subscription(sub, row)
        if sub.get('status') == 'canceled':
            return self._settle_terminal_usage(sub, row, event_id)
        if sub.get('status') != 'active':
            return self._rpc('revoke_current_billing', p_checkout_id=row['id'], p_subscription_id=sub['id'],
                             p_event_id=event_id, p_reason='canceled' if sub.get('status') == 'canceled' else 'payment_failed')
        invoice = invoice or self._retrieve(self.stripe.v1.invoices, sub.get('latest_invoice'), 'in_')
        if (invoice.get('subscription') == sub['id'] and invoice.get('customer') == sub.get('customer')
                and invoice.get('status') in {'open', 'uncollectible', 'void'}):
            return self._rpc('revoke_current_billing', p_checkout_id=row['id'], p_subscription_id=sub['id'],
                             p_event_id=event_id, p_reason='payment_failed')
        if (invoice.get('subscription') != sub['id'] or invoice.get('customer') != sub.get('customer')
                or invoice.get('status') != 'paid' or invoice.get('paid') is not True
                or invoice.get('paid_out_of_band') is not False or invoice.get('currency') != 'usd'
                or invoice.get('amount_remaining') != 0):
            raise OperationConflictError('A paid subscription invoice is required.')
        lines = _dict(invoice.get('lines'))
        data = lines.get('data')
        if not isinstance(data, list) or lines.get('has_more') is not False:
            raise OperationConflictError('The invoice lines require reconciliation.')
        base = [line for line in data if line.get('subscription_item') == item['id'] and line.get('type') == 'subscription']
        if (len(base) != 1 or base[0].get('proration') is not False or base[0].get('amount') != row['amount_cents']
                or base[0].get('quantity') != 1 or _dict(base[0].get('price')).get('id') != row['stripe_price_id']):
            raise OperationConflictError('The subscription invoice does not contain the agreed monthly plan.')
        period = _dict(base[0].get('period'))
        if (type(period.get('start')) is not int or type(period.get('end')) is not int
                or not 0 < period['start'] < period['end']
                or period['end'] > sub.get('current_period_end', 0)):
            raise OperationConflictError('The paid invoice has an invalid subscription period.')
        # Extra lines must be the single durable prior-period invoice item we created.
        extra = [line for line in data if line is not base[0]]
        expected = row['amount_cents']
        saved = None
        if invoice.get('billing_reason') == 'subscription_cycle':
            saved = self._rpc('close_current_usage', p_subscription_id=sub['id'],
                              p_period_end=_timestamp(period['start']), p_invoice_id=invoice['id'])
            if saved.get('extra_old_urls', 0) and not saved.get('stripe_invoice_item_id'):
                # Recover a successful provider write whose response/DB attachment was lost,
                # even after this invoice has finalized. Never add a new item to a paid invoice.
                self._invoice_usage(invoice, sub, row)
                saved = self._rpc('close_current_usage', p_subscription_id=sub['id'],
                                  p_period_end=_timestamp(period['start']), p_invoice_id=invoice['id'])
            if saved.get('extra_old_urls', 0) > 0 and not extra:
                raise OperationConflictError('Approved extra usage is missing from the paid invoice.')
        if extra:
            if len(extra) != 1 or saved is None:
                raise OperationConflictError('Unexpected subscription invoice lines.')
            if (extra[0].get('invoice_item') != saved.get('stripe_invoice_item_id')
                    or extra[0].get('amount') != saved.get('extra_amount_cents')
                    or _dict(extra[0].get('price')).get('id') != self.prices['additional_urls']):
                raise OperationConflictError('The extra usage invoice does not match approved usage.')
            expected += saved['extra_amount_cents']
        if any(type(invoice.get(k)) is not int or invoice[k] != expected
               for k in ('total', 'subtotal', 'amount_due', 'amount_paid')):
            raise OperationConflictError('The invoice total does not match the agreed plan and usage.')
        self._intent(invoice.get('payment_intent'), expected, customer=sub['customer'], invoice_id=invoice['id'])
        if not row.get('stripe_subscription_id'):
            if row.get('stripe_session_id'):
                session = self._retrieve(self.stripe.v1.checkout.sessions, row['stripe_session_id'], 'cs_')
            else:
                sessions = self._provider(self.stripe.v1.checkout.sessions.list,
                                          params={'subscription': sub['id'], 'limit': 2})
                data = sessions.get('data')
                if not isinstance(data, list) or len(data) != 1 or sessions.get('has_more') is not False:
                    raise BillingNotReadyError('The initial subscription checkout is not ready to reconcile.')
                session = self._retrieve(self.stripe.v1.checkout.sessions, data[0].get('id'), 'cs_')
            self._validate_session(session, row)
            self._lines(session, row)
            if (session.get('subscription') != sub['id'] or session.get('customer') != sub['customer']
                    or session.get('payment_status') != 'paid' or session.get('status') != 'complete'):
                raise OperationConflictError('The subscription has no completed purchase consent.')
            self._rpc('apply_current_checkout_payment', p_checkout_id=row['id'], p_session_id=session['id'],
                      p_customer_id=sub['customer'], p_subscription_id=sub['id'], p_amount_cents=row['amount_cents'],
                      p_currency='usd', p_event_id=event_id + ':checkout')
        return self._rpc('record_current_subscription_period', p_checkout_id=row['id'],
                         p_subscription_id=sub['id'], p_customer_id=sub['customer'], p_invoice_id=invoice['id'],
                         p_period_start=_timestamp(period['start']), p_period_end=_timestamp(period['end']),
                         p_amount_cents=row['amount_cents'], p_event_id=event_id)

    def _invoice_usage(self, invoice, sub, row):
        self._subscription(sub, row)
        if invoice.get('billing_reason') != 'subscription_cycle':
            return {'received': True, 'ignored': True}
        if invoice.get('customer') != sub.get('customer') or invoice.get('subscription') != sub['id']:
            raise OperationConflictError('The usage invoice has a different customer.')
        saved = self._rpc('close_current_usage', p_subscription_id=sub['id'],
                          p_period_end=_timestamp(invoice.get('period_end')), p_invoice_id=invoice['id'])
        self._attach_usage_item(invoice, sub['customer'], saved)
        return {'received': True}

    def _attach_usage_item(self, invoice, customer, saved):
        if not saved.get('extra_old_urls') or saved.get('stripe_invoice_item_id'):
            return saved.get('stripe_invoice_item_id')
        items = self._provider(self.stripe.v1.invoice_items.list,
                               params={'invoice': invoice['id'], 'limit': 100})
        if not isinstance(items.get('data'), list) or items.get('has_more') is not False:
            raise BillingNotReadyError('Usage invoice items require reconciliation.')
        matches = [item for item in items['data']
                   if (item.get('metadata') or {}).get('redirx_period_id') == saved['id']]
        if len(matches) > 1:
            raise OperationConflictError('Duplicate usage invoice items require reconciliation.')
        if not matches and invoice.get('status') != 'draft':
            raise BillingNotReadyError('Extra usage requires reconciliation before invoice finalization.')
        if not matches and datetime.fromisoformat(saved['closed_at'].replace('Z', '+00:00')).timestamp() < time.time() - 23 * 3600:
            raise BillingNotReadyError('The usage delivery window expired; reconcile before retrying.')
        if not matches:
            self._price('additional_urls')
        # A frozen database snapshot and deterministic key survive delivery races.
        created = matches[0] if matches else self._provider(self.stripe.v1.invoice_items.create, params={
            'customer': customer, 'invoice': invoice['id'], 'price': self.prices['additional_urls'],
            'quantity': saved['extra_old_urls'],
            'metadata': {'redirx_catalog': VERSION, 'redirx_period_id': saved['id']},
            'period': {'start': int(datetime.fromisoformat(saved['period_start'].replace('Z', '+00:00')).timestamp()),
                       'end': int(datetime.fromisoformat(saved['period_end'].replace('Z', '+00:00')).timestamp())}},
            key=f'{VERSION}:extra:{saved["id"]}')
        if (created.get('invoice') != invoice['id'] or created.get('customer') != customer
                or created.get('livemode') is not self.livemode or created.get('amount') != saved['extra_amount_cents']
                or created.get('currency') != 'usd' or created.get('quantity') != saved['extra_old_urls']
                or _dict(created.get('price')).get('id') != self.prices['additional_urls']):
            raise OperationConflictError('The usage invoice item has an invalid binding.')
        self._rpc('attach_current_usage_invoice_item', p_period_id=saved['id'],
                  p_invoice_id=invoice['id'], p_item_id=created['id'])
        return created['id']

    def handle_webhook(self, raw, signature):
        if not isinstance(raw, bytes) or len(raw) > 1024 * 1024 or not isinstance(signature, str):
            raise InvalidInputError('Invalid webhook body.')
        try:
            event = _dict(stripe.Webhook.construct_event(raw, signature, self.secret, tolerance=300))
        except Exception:
            raise InvalidInputError('Invalid webhook signature.') from None
        if event.get('livemode') is not self.livemode or event.get('account') is not None:
            raise InvalidInputError('The webhook belongs to a different Stripe environment.')
        kind = event.get('type')
        supported = {'checkout.session.completed', 'checkout.session.async_payment_succeeded',
                     'invoice.created', 'invoice.paid', 'invoice.payment_failed',
                     'customer.subscription.updated', 'customer.subscription.deleted',
                     'charge.refunded', 'charge.dispute.created'}
        if kind not in supported:
            return {'received': True, 'ignored': True}
        event_id = event.get('id')
        if not isinstance(event_id, str) or not event_id.startswith('evt_'):
            raise InvalidInputError('Invalid webhook identity.')
        self._account()
        obj = _dict(_dict(event.get('data')).get('object'))
        if kind.startswith('checkout.'):
            session = self._retrieve(self.stripe.v1.checkout.sessions, obj.get('id'), 'cs_')
            metadata = _dict(session.get('metadata'))
            if metadata.get('redirx_catalog') != VERSION:
                return {'received': True, 'ignored': True}
            row = self._row(metadata.get('redirx_checkout_id'))
            self._validate_session(session, row)
            self._lines(session, row)
            if session.get('status') != 'complete' or session.get('payment_status') != 'paid':
                return {'received': True, 'pending': True}
            if row['sku'] == 'payg':
                self._intent(session.get('payment_intent'), row['amount_cents'],
                             customer=session.get('customer'), metadata=self._metadata(row))
                self._rpc('apply_current_checkout_payment', p_checkout_id=row['id'], p_session_id=session['id'],
                          p_customer_id=session['customer'], p_subscription_id=None,
                          p_amount_cents=row['amount_cents'], p_currency='usd', p_event_id=event_id)
            else:
                sub = self._retrieve(self.stripe.v1.subscriptions, session.get('subscription'), 'sub_')
                if sub.get('customer') != session.get('customer'):
                    raise OperationConflictError('The subscription belongs to a different customer.')
                self._renewal(sub, row, event_id)
            return {'received': True}
        if kind.startswith('charge.'):
            charge_id = obj.get('charge') if kind == 'charge.dispute.created' else obj.get('id')
            charge = self._retrieve(self.stripe.v1.charges, charge_id, 'ch_')
            if not (charge.get('amount_refunded', 0) > 0 or charge.get('disputed') is True):
                raise OperationConflictError('The charge does not prove a payment reversal.')
            intent = self._retrieve(self.stripe.v1.payment_intents, charge.get('payment_intent'), 'pi_')
            metadata = _dict(intent.get('metadata'))
            if intent.get('invoice'):
                invoice = self._retrieve(self.stripe.v1.invoices, intent['invoice'], 'in_')
                sub = self._retrieve(self.stripe.v1.subscriptions, invoice.get('subscription'), 'sub_')
                metadata = _dict(sub.get('metadata'))
            else:
                sub = None
            if metadata.get('redirx_catalog') != VERSION:
                return {'received': True, 'ignored': True}
            row = self._row(metadata.get('redirx_checkout_id'))
            if metadata != self._metadata(row) or charge.get('customer') != row.get('stripe_customer_id'):
                raise OperationConflictError('The reversal has a different purchase owner.')
            if (intent.get('latest_charge') != charge['id']
                    or intent.get('customer') != charge.get('customer')):
                raise OperationConflictError('The reversal does not match its payment intent.')
            if sub is not None and (
                    invoice.get('payment_intent') != intent['id']
                    or invoice.get('customer') != charge.get('customer')
                    or sub.get('customer') != charge.get('customer')
                    or sub['id'] != row.get('stripe_subscription_id')):
                raise OperationConflictError('The reversal does not match its subscription invoice.')
            self._rpc('revoke_current_billing', p_checkout_id=row['id'], p_subscription_id=sub['id'] if sub else None,
                      p_event_id=event_id, p_reason='disputed' if charge.get('disputed') else 'refunded',
                      p_invoice_id=invoice['id'] if sub else None)
            return {'received': True}
        invoice = None
        if kind.startswith('invoice.'):
            invoice = self._retrieve(self.stripe.v1.invoices, obj.get('id'), 'in_')
            if not invoice.get('subscription'):
                return {'received': True, 'ignored': True}
            sub_id = invoice['subscription']
        else:
            sub_id = obj.get('id')
        sub = self._retrieve(self.stripe.v1.subscriptions, sub_id, 'sub_')
        metadata = _dict(sub.get('metadata'))
        if metadata.get('redirx_catalog') != VERSION:
            return {'received': True, 'ignored': True}
        row = self._row(metadata.get('redirx_checkout_id'))
        if kind == 'invoice.created':
            return self._invoice_usage(invoice, sub, row)
        self._renewal(sub, row, event_id, invoice=invoice if kind == 'invoice.paid' else None)
        return {'received': True}
