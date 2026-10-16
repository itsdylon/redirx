import os
"""Opt-in durable planning and explicit inventory resources."""
from functools import wraps
from hashlib import sha256

from flask import Blueprint, jsonify, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge, UnsupportedMediaType

from backend.extensions import limiter
from backend.services.analytics_service import AppEvent, capture
from backend.services.api_key_service import ApiKeyService, looks_like_api_key
from backend.services.mcp_delegation_service import MCPDelegationService
from backend.services.companion_auth_service import resolve_companion_session
from backend.services.inventory_import_service import InventoryImportService
from backend.services.migration_repository import InvalidInputError, MigrationRepositoryError
from backend.services.migration_planning_service import MigrationPlanningService, envelope
from backend.services.migration_planning_service import validate_key
from backend.services.migration_quote_service import MigrationQuoteService
from backend.services.migration_run_service import MigrationRunService, FreeMigrationRateLimitedError
from backend.services.migration_discovery_workflow import plan_and_start_discovery, migration_discovery_summary
from backend.services.migration_status_service import MigrationStatusService

v2_blueprint = Blueprint('v2', __name__)


@v2_blueprint.record_once
def init_limiter(state):
    if 'limiter' not in state.app.extensions:
        limiter.init_app(state.app)


def failure(code, message, http_status, retryable=False, next_action='none'):
    return jsonify(envelope(status='failed', next_action=next_action, error={
        'code': code, 'message': message, 'retryable': retryable, 'next_action': next_action,
    })), http_status


def resolve_authorization():
    # Flask-Limiter can evaluate a route limit before its view decorator runs.
    # Resolve once per request so rotating delegations still share account limits.
    if getattr(request, '_v2_auth_resolved', False):
        return request.api_user_id
    request._v2_auth_resolved = True
    header = request.headers.get('Authorization', '')
    token = header[7:].strip() if header.startswith('Bearer ') else ''
    user = None
    if token:
        user = ApiKeyService().resolve(token) if looks_like_api_key(token) else MCPDelegationService().resolve(token)
        if user is None and not looks_like_api_key(token):
            user = resolve_companion_session(token)
    request.api_user_id = user
    return user


def account_limit_key():
    return resolve_authorization() or request.remote_addr or 'unknown'


def authenticated(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not resolve_authorization():
            return failure('reconnect_required', 'Sign in or reconnect your agent.', 401, next_action='reconnect')
        return fn(*args, **kwargs)
    return wrapped


@v2_blueprint.errorhandler(MigrationRepositoryError)
def repository_error(exc):
    if isinstance(exc, FreeMigrationRateLimitedError):
        payload = envelope((request.view_args or {}).get('migration_id'), status='failed', next_action='retry',
            error={'code': 'rate_limited', 'message': str(exc), 'retryable': True, 'next_action': 'retry'})
        payload['retry_after_seconds'] = exc.retry_after_seconds
        return jsonify(payload), 429, {'Retry-After': str(exc.retry_after_seconds)}
    if exc.code == 'payment_required' and os.getenv('REDIRX_CURRENT_BILLING_ENABLED','false').lower() == 'true':
        from urllib.parse import urlencode, urlsplit
        value=request.get_json(silent=True) or {}
        inventories=value.get('inventory_ids',{})
        origin=os.getenv('MCP_CHECKOUT_COMPANION_ORIGIN','').rstrip('/')
        parsed=urlsplit(origin)
        data={}
        if (parsed.hostname and not parsed.username and not parsed.password and not parsed.path and not parsed.query and not parsed.fragment
                and (parsed.scheme=='https' or (os.getenv('REDIRX_STRIPE_MODE','test')=='test' and parsed.scheme=='http' and parsed.hostname in ('localhost','127.0.0.1','::1')))
                and isinstance(inventories,dict) and set(inventories)=={'old','new'}):
            data['billing_url']=origin+'/billing/current?'+urlencode({'migration_id':(request.view_args or {}).get('migration_id'),
                'old_inventory_id':inventories['old'],'new_inventory_id':inventories['new']})
        return jsonify(envelope((request.view_args or {}).get('migration_id'),status='payment_required',next_action='complete_payment',data=data,
            error={'code':exc.code,'message':str(exc),'retryable':False,'next_action':'complete_payment'})),402
    if exc.code == 'approval_required':
        return jsonify(envelope((request.view_args or {}).get('migration_id'), status='needs_input',
            next_action='approve_extra_urls', data=exc.details,
            error={'code': exc.code, 'message': str(exc), 'retryable': False, 'next_action': 'approve_extra_urls'})), 409
    code = 'operation_conflict' if exc.code == 'inventory_busy' else exc.code
    http_status = {'not_found': 404, 'invalid_input': 400, 'operation_conflict': 409,
                   'capacity_exceeded': 413, 'quote_expired': 409,
                   'inventory_incomplete': 409, 'payment_required': 402, 'approval_required': 409}.get(code, 503)
    return failure(code, str(exc), http_status, exc.retryable,
                   getattr(exc, 'next_action', 'retry' if exc.retryable else 'none'))


@v2_blueprint.errorhandler(BadRequest)
@v2_blueprint.errorhandler(UnsupportedMediaType)
def invalid_json(exc):
    return failure('invalid_input', 'Provide a JSON object.', 400)


@v2_blueprint.errorhandler(RequestEntityTooLarge)
def too_large(exc):
    return failure('capacity_exceeded', 'The request exceeds the available capacity.', 413)


@v2_blueprint.errorhandler(429)
def rate_limited(exc):
    payload = envelope(status='failed', next_action='retry', error={
        'code': 'rate_limited', 'message': 'Retry after the rate limit window.',
        'retryable': True, 'next_action': 'retry',
    })
    payload['retry_after_seconds'] = 60
    return jsonify(payload), 429, {'Retry-After': '60'}


def body(allowed=None):
    value = request.get_json()
    if not isinstance(value, dict) or (allowed is not None and set(value) - allowed):
        raise InvalidInputError('Provide a JSON object with supported fields.')
    return value


@v2_blueprint.post('/migrations')
@authenticated
@limiter.limit('30 per minute', key_func=account_limit_key)
def plan_migration():
    service = MigrationPlanningService()
    if os.getenv('JEV_MVP_ENABLED','false').lower() == 'true':
        return jsonify(service.plan(request.api_user_id,body()))
    return jsonify(plan_and_start_discovery(request.api_user_id, body(), repository=service.repository))


@v2_blueprint.get('/migrations')
@authenticated
@limiter.limit('120 per minute', key_func=account_limit_key)
def list_migrations():
    raw_limit = request.args.get('limit', '20')
    if not raw_limit.isascii() or not raw_limit.isdecimal() or len(raw_limit) > 3:
        raise InvalidInputError('limit must be an integer from 1 to 500.')
    page = MigrationPlanningService().repository.list_migrations(
        request.api_user_id, request.args.get('cursor') or None, int(raw_limit))
    # Expose only useful account history; request hashes and internal policy
    # metadata belong to the service, not the browser list contract.
    fields = ('id', 'name', 'old_origin', 'new_origin', 'status', 'created_at', 'updated_at')
    page['items'] = [{key: row.get(key) for key in fields} for row in page['items']]
    return jsonify(envelope(status='succeeded', next_action='none', data=page))


@v2_blueprint.get('/migrations/<migration_id>')
@authenticated
@limiter.limit('120 per minute', key_func=account_limit_key)
def get_migration(migration_id):
    service = MigrationPlanningService()
    if request.args.get('operation_id'):
        return jsonify(service.operation(request.api_user_id, request.args['operation_id'], migration_id))
    summary = migration_discovery_summary(request.api_user_id, migration_id, repository=service.repository)
    return jsonify(MigrationStatusService(service.repository).get(
        request.api_user_id, migration_id, run_id=request.args.get('run_id'), base_summary=summary))


@v2_blueprint.post('/migrations/<migration_id>/inventories')
@authenticated
@limiter.limit('30 per minute', key_func=account_limit_key)
def import_inventory(migration_id):
    value = body({'side', 'rows', 'idempotency_key', 'host_aliases'})
    service = MigrationPlanningService()
    migration = service.repository.get_migration(request.api_user_id, migration_id)
    side = value.get('side')
    if side not in ('old', 'new'):
        raise InvalidInputError("side must be 'old' or 'new'.")
    # Aliases must be explicitly declared for THIS owned migration at planning.
    # A request cannot silently expand the side's target scope during import.
    aliases = migration.get('site_aliases', {}).get(side, [])
    if 'host_aliases' in value and value['host_aliases'] != aliases:
        raise InvalidInputError('Import aliases must match the migration declaration.')
    result = InventoryImportService(service.repository).import_inventory(
        request.api_user_id, migration_id, side, value.get('rows'), value.get('idempotency_key'), aliases)
    response = service.operation(request.api_user_id, result['operation_id'], migration_id)
    response['data']['replayed'] = result['replayed']
    return jsonify(response)


@v2_blueprint.get('/operations/<operation_id>')
@authenticated
@limiter.limit('120 per minute', key_func=account_limit_key)
def operation_status(operation_id):
    return jsonify(MigrationPlanningService().operation(request.api_user_id, operation_id))


@v2_blueprint.post('/migrations/<migration_id>/runs')
@authenticated
@limiter.limit('30 per minute', key_func=account_limit_key)
def run_migration(migration_id):
    value = body({'inventory_ids', 'quote_id', 'grant_id', 'subscription_id', 'rerun_of', 'idempotency_key','confirmed_pairs','checkout_id','approve_extra_urls','max_extra_amount_cents'})
    if value.get('confirmed_pairs') is not None and os.getenv('JEV_MVP_ENABLED','false').lower() != 'true':
        raise InvalidInputError('Confirmed examples require the Jev workflow.')
    if value.get('grant_id') is not None and value.get('subscription_id') is not None:
        raise InvalidInputError('Choose either a purchase grant or a Studio subscription.')
    key = validate_key(value.get('idempotency_key'))
    inventories = value.get('inventory_ids')
    if not isinstance(inventories, dict) or set(inventories) != {'old', 'new'}:
        raise InvalidInputError('inventory_ids must contain exactly old and new snapshot IDs.')
    from backend.services.jev_pipeline_service import current_billing_enabled, PAID_LIMITS
    if current_billing_enabled():
        from backend.services.jev_pipeline_service import JevService, LIMITS
        if any(value.get(field) is not None for field in ('quote_id','grant_id','rerun_of')):
            raise InvalidInputError('Current runs use checkout_id or subscription_id; refine an existing run to retry it.')
        result = JevService().start_current(request.api_user_id,migration_id,inventories['old'],inventories['new'],key,
            value.get('confirmed_pairs'),value.get('checkout_id'),value.get('subscription_id'),
            value.get('approve_extra_urls',False),value.get('max_extra_amount_cents',0))
        if not result['replayed']:
            capture(AppEvent.MIGRATION_RUN_STARTED,user_id=request.api_user_id,migration_id=migration_id,
                properties={'run_id':result['run_id'],'operation_id':result['operation_id'],'engine':'jev-url-v1','metered':not result['free']})
        return jsonify(envelope(migration_id,result['operation_id'],status=result['status'],next_action='poll',
            data={**result,'engine':'jev-url-v1','limits':LIMITS if result['free'] else PAID_LIMITS}))
    if any(value.get(field) is not None for field in ('checkout_id','approve_extra_urls','max_extra_amount_cents')):
        raise InvalidInputError('Current paid admission is not enabled.')
    quotes = MigrationQuoteService()
    # Keep automatic quote identity stable across payment and retries. A caller
    # cannot silently replace its reserved run's quote by omitting quote_id.
    quote = (quotes.get_quote(request.api_user_id, migration_id, value['quote_id'])
             if value.get('quote_id') else quotes.create_quote(
                 request.api_user_id, migration_id, inventories['old'], inventories['new'],
                 'run:' + sha256(key.encode()).hexdigest()))
    if quote['inventory_ids'] != inventories:
        raise InvalidInputError('The quote must match both requested inventory snapshots.')
    if quote['kind'] == 'custom' and os.getenv('JEV_MVP_ENABLED','false').lower() != 'true':
        return jsonify(envelope(migration_id, quote['operation_id'], status='needs_input',
                                next_action='request_custom_quote', data=quote))
    from backend.services.jev_pipeline_service import JevService, enabled as jev_enabled, LIMITS
    if jev_enabled():
        if any(value.get(field) is not None for field in ('grant_id','subscription_id','rerun_of')):
            raise InvalidInputError('Jev V1 is free; omit paid grant/subscription/rerun fields. Use refine_matches for an existing Jev run.')
        if quote['kind'] != 'free':
            raise InvalidInputError('Jev V1 supports at most 500 old pages, 2000 new pages, and 2 MiB of URL text. No paid upgrade is required or available.')
        result = JevService(quotes.repository).start(request.api_user_id,migration_id,inventories['old'],inventories['new'],quote['quote_id'],key,value.get('confirmed_pairs'))
        if not result.get('replayed'):
            # Fired directly here rather than via record_deep_match_run(): that
            # helper also writes to the legacy free-run usage ledger, which is
            # the Deep Match abuse ceiling and does not apply to Jev's own
            # LIMITS (500/2000/2MiB/5 runs per day, enforced above and in the
            # reserve_jev_run RPC). The legacy pivot branch below still calls
            # record_deep_match_run and remains the only other MIGRATION_RUN_STARTED
            # source — one event, two call sites, never both for one run.
            capture(AppEvent.MIGRATION_RUN_STARTED, user_id=request.api_user_id, migration_id=migration_id,
                    properties={"run_id": result.get('run_id'), "operation_id": result['operation_id'],
                                "engine": "jev-url-v1", "metered": False})
        return jsonify(envelope(migration_id,result['operation_id'],status=result['status'],
            next_action='poll' if result['status'] in ('queued','running') else 'resolve_matches',
            data={**result,'engine':'jev-url-v1','free':True,'limits':LIMITS}))
    from backend.services.migration_subscription_service import (
        MigrationSubscriptionService, SubscriptionAllowanceExhaustedError,
        SubscriptionPaymentRequiredError,
    )
    selection = None
    result = None
    if value.get('grant_id') is None:
        subscriptions = MigrationSubscriptionService(quotes.repository)
        selection = subscriptions.select_run_subscription(request.api_user_id, migration_id,
            inventories['old'], inventories['new'], quote['quote_id'], key,
            subscription_id=value.get('subscription_id'))
        if selection['use_studio']:
            try:
                result = subscriptions.start_studio_run(request.api_user_id, selection['subscription_id'],
                    migration_id, inventories['old'], inventories['new'], quote['quote_id'], key,
                    rerun_of=value.get('rerun_of'))
            except (SubscriptionAllowanceExhaustedError, SubscriptionPaymentRequiredError) as exc:
                # Another request may consume the last slot or revoke entitlement
                # after selection. The atomic reservation remains authoritative.
                selection = {**selection, 'use_studio': False, 'reason': exc.code,
                             'next_action': 'complete_payment'}
        if (result is None and value.get('subscription_id') is not None
                and selection['reason'] not in {'free_quote', 'purchased_quote'}):
            code = 'allowance_exhausted' if selection['reason'] == 'allowance_exhausted' else 'payment_required'
            return jsonify(envelope(migration_id, status='payment_required', next_action='complete_payment',
                data={**quote, 'quote_operation_id': quote['operation_id'], 'operation_id': None,
                      'run_id': None, 'session_id': None, 'subscription_selection': selection},
                error={'code': code, 'message': 'The selected Studio subscription cannot start this migration. Choose an available subscription or use the migration quote.',
                       'retryable': False, 'next_action': 'complete_payment'})), 402
    if result is None:
        # No subscription purchase or overage occurs here. Missing quote rights
        # reserve the same durable payment-required operation for later retry.
        result = MigrationRunService().start_run(request.api_user_id, migration_id,
            inventories['old'], inventories['new'], quote['quote_id'], key,
            grant_id=value.get('grant_id'), rerun_of=value.get('rerun_of'))
    if selection is not None:
        result['subscription_selection'] = selection
    next_action = {'payment_required': 'complete_payment', 'queued': 'poll', 'running': 'poll',
                   'succeeded': 'resolve_matches', 'failed': 'retry'}[result['status']]
    return jsonify(envelope(migration_id, result['operation_id'], status=result['status'],
                            next_action=next_action, data={**quote, **result}))

@v2_blueprint.post('/migrations/<migration_id>/runs/<run_id>/refine')
@authenticated
@limiter.limit('10 per minute', key_func=account_limit_key)
def refine_matches(migration_id, run_id):
    from backend.services.jev_pipeline_service import JevService, enabled as jev_enabled
    if not jev_enabled():
        raise InvalidInputError('Jev refinement is currently paused. Existing results remain available.')
    value=body({'expected_seed_revision','idempotency_key'})
    result=JevService().refine(request.api_user_id,migration_id,run_id,value.get('expected_seed_revision'),value.get('idempotency_key'))
    return jsonify(envelope(migration_id,result['operation_id'],status=result['status'],next_action='poll',data=result))
