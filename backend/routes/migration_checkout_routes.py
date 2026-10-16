"""Opt-in P06 routes; root registers this separate blueprint under /api/v2."""
from flask import Blueprint, jsonify, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge, UnsupportedMediaType

from backend.extensions import limiter
from backend.routes.v2_routes import authenticated, account_limit_key, body, failure
from backend.services.migration_checkout_service import MigrationCheckoutService
from backend.services.migration_subscription_checkout_service import MigrationSubscriptionCheckoutService
from backend.services.migration_planning_service import envelope
from backend.services.migration_repository import MigrationRepositoryError


def create_migration_checkout_blueprint(service_factory=MigrationCheckoutService, subscription_service_factory=MigrationSubscriptionCheckoutService):
    blueprint = Blueprint('migration_test_checkout', __name__)

    @blueprint.record_once
    def init_limiter(state):
        if 'limiter' not in state.app.extensions:
            limiter.init_app(state.app)

    @blueprint.errorhandler(MigrationRepositoryError)
    def repository_error(exc):
        status = {'not_found': 404, 'invalid_input': 400, 'operation_conflict': 409, 'quote_expired': 409, 'payment_required': 402, 'allowance_exhausted': 402}.get(exc.code, 503)
        return failure(exc.code, str(exc), status, exc.retryable, getattr(exc, 'next_action', 'retry' if exc.retryable else 'none'))

    @blueprint.errorhandler(BadRequest)
    @blueprint.errorhandler(UnsupportedMediaType)
    def invalid_json(exc):
        return failure('invalid_input', 'Provide a JSON object.', 400)

    @blueprint.errorhandler(RequestEntityTooLarge)
    def too_large(exc):
        return failure('capacity_exceeded', 'The request exceeds the available capacity.', 413)

    @blueprint.errorhandler(429)
    def rate_limited(exc):
        payload, status = failure('rate_limited', 'Retry after the rate limit window.', 429, True, 'retry')
        return payload, status, {'Retry-After': '60'}

    def present(service, owner, result):
        quote = service.quotes.get_quote(owner, result['migration_id'], result['quote_id'])
        status = 'needs_input' if result['status'] == 'paid' else (
            'payment_required' if result['status'] in {'reserved', 'open'} else 'failed')
        return jsonify(envelope(result['migration_id'], result['operation_id'], status=status,
            next_action=result['next_action'], data={**quote, **result}))

    @blueprint.post('/migrations/<migration_id>/quotes/<quote_id>/checkout')
    @authenticated
    @limiter.limit('30 per minute', key_func=account_limit_key)
    def create_checkout(migration_id, quote_id):
        from backend.services.current_billing_service import enabled
        if enabled():
            return failure('operation_conflict', 'Use current billing for new purchases.', 409)
        value = body({'operation_id', 'idempotency_key'})
        service = service_factory()
        return present(service, request.api_user_id, service.create_checkout(request.api_user_id,
            migration_id, quote_id, value.get('operation_id'), value.get('idempotency_key')))

    @blueprint.get('/migrations/<migration_id>/checkouts/<checkout_id>')
    @authenticated
    @limiter.limit('120 per minute', key_func=account_limit_key)
    def get_checkout(migration_id, checkout_id):
        service = service_factory()
        return present(service, request.api_user_id, service.get_checkout(request.api_user_id, migration_id, checkout_id))

    @blueprint.post('/billing/stripe-test/webhook')
    def webhook():
        # Read a bounded raw body exactly once; JSON parsing before signature verification is forbidden.
        raw = request.stream.read(1024 * 1024 + 1)
        return jsonify(service_factory().handle_webhook(raw, request.headers.get('Stripe-Signature', '')))

    def subscription_response(result):
        status = 'succeeded' if result['state'] == 'complete' else 'payment_required'
        return jsonify(envelope(status=status, next_action=result['next_action'], data=result))

    @blueprint.post('/billing/subscription-checkouts')
    @authenticated
    @limiter.limit('15 per minute', key_func=account_limit_key)
    def create_subscription_checkout():
        from backend.services.current_billing_service import enabled
        if enabled():
            return failure('operation_conflict', 'Use current billing for new monthly plans.', 409)
        value = body({'sku', 'deployment_id', 'idempotency_key', 'recurring_consent'})
        return subscription_response(subscription_service_factory().create_checkout(request.api_user_id,
            value.get('sku'), value.get('idempotency_key'), recurring_consent=value.get('recurring_consent'),
            deployment_id=value.get('deployment_id')))

    @blueprint.get('/billing/subscription-checkouts/<checkout_id>')
    @blueprint.get('/billing/subscription-checkouts/<checkout_id>/return')
    @authenticated
    @limiter.limit('120 per minute', key_func=account_limit_key)
    def get_subscription_checkout(checkout_id):
        # Browser success/cancel/session/subscription query assertions are inert.
        return subscription_response(subscription_service_factory().get_checkout(request.api_user_id, checkout_id))

    @blueprint.post('/billing/stripe-test/subscriptions/webhook')
    def subscription_webhook():
        raw = request.stream.read(1024 * 1024 + 1)
        return jsonify(subscription_service_factory().handle_webhook(raw, request.headers.get('Stripe-Signature', '')))

    return blueprint
