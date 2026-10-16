"""Authenticated current catalog checkout; raw signed provider callbacks."""
from flask import Blueprint, jsonify, request
from werkzeug.exceptions import BadRequest, UnsupportedMediaType, RequestEntityTooLarge

from backend.extensions import limiter
from backend.routes.v2_routes import authenticated, account_limit_key, body, failure
from backend.services.current_pricing import PLANS, VERSION
from backend.services.current_billing_service import CurrentBillingService, enabled
from backend.services.migration_repository import MigrationRepositoryError, _strict_uuid


def create_current_billing_blueprint(service_factory=CurrentBillingService):
    bp = Blueprint('current_billing', __name__)

    @bp.record_once
    def init_limiter(state):
        if 'limiter' not in state.app.extensions:
            limiter.init_app(state.app)

    @bp.errorhandler(MigrationRepositoryError)
    def billing_error(exc):
        status = {'not_found': 404, 'invalid_input': 400, 'operation_conflict': 409,
                  'capacity_exceeded': 413, 'inventory_incomplete': 409}.get(exc.code, 503)
        return failure(exc.code, str(exc), status, exc.retryable)

    @bp.errorhandler(BadRequest)
    @bp.errorhandler(UnsupportedMediaType)
    def invalid_json(exc):
        return failure('invalid_input', 'Provide a JSON object.', 400)

    @bp.errorhandler(RequestEntityTooLarge)
    def too_large(exc):
        return failure('capacity_exceeded', 'The request is too large.', 413)

    @bp.get('/billing/current')
    @authenticated
    def catalog():
        return jsonify({'enabled': enabled(), 'version': VERSION, 'plans': [
            {'sku': sku, 'name': plan.name, 'monthly_amount_cents': plan.amount_cents,
             'included_old_urls': plan.included_old_urls}
            for sku, plan in PLANS.items() if sku in {'studio', 'agency'}]})

    @bp.post('/billing/current/checkouts')
    @authenticated
    @limiter.limit('15 per minute', key_func=account_limit_key)
    def checkout():
        value = body({'sku', 'migration_id', 'inventory_ids', 'idempotency_key', 'recurring_consent'})
        return jsonify(service_factory().create_checkout(request.api_user_id, value.get('sku'),
            value.get('idempotency_key'), migration_id=value.get('migration_id'),
            inventory_ids=value.get('inventory_ids'), recurring_consent=value.get('recurring_consent', False)))

    @bp.get('/billing/current/checkouts/<checkout_id>')
    @authenticated
    @limiter.limit('120 per minute', key_func=account_limit_key)
    def checkout_status(checkout_id):
        return jsonify(service_factory().get_checkout(request.api_user_id, checkout_id))

    @bp.get('/billing/current/usage')
    @authenticated
    @limiter.limit('120 per minute', key_func=account_limit_key)
    def usage():
        result = service_factory().usage(request.api_user_id)
        public = {'sku', 'included_old_urls', 'used_old_urls', 'extra_old_urls', 'extra_amount_cents',
                  'period_start', 'period_end', 'portal_available', 'subscription_status', 'subscription_id'}
        return jsonify({key: value for key, value in result.items() if key in public})

    @bp.get('/billing/current/migrations/<migration_id>/quote')
    @authenticated
    @limiter.limit('30 per minute', key_func=account_limit_key)
    def quote(migration_id):
        ids = {name: _strict_uuid(value, name)[1] for name, value in {
            'p_user_id': request.api_user_id, 'p_migration_id': migration_id,
            'p_old_inventory_id': request.args.get('old_inventory_id'),
            'p_new_inventory_id': request.args.get('new_inventory_id')}.items()}
        result = service_factory()._rpc('current_billing_quote', **ids)
        return jsonify({key: result[key] for key in ('old_urls', 'amount_cents', 'stripe_quantity')})

    @bp.post('/billing/current/portal')
    @authenticated
    @limiter.limit('15 per minute', key_func=account_limit_key)
    def portal():
        return jsonify(service_factory().portal(request.api_user_id))

    @bp.post('/billing/current/webhook')
    def webhook():
        raw = request.stream.read(1024 * 1024 + 1)
        return jsonify(service_factory().handle_webhook(raw, request.headers.get('Stripe-Signature', '')))

    return bp
