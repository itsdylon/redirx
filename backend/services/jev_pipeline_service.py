"""Owned Jev admission, refinement and review state; no worker execution."""
from __future__ import annotations

import os
import json

from src.redirx.jev.jev import MODEL, PROMPT_VERSION
from .analytics_service import AppEvent, capture
from .migration_repository import MigrationRepository, InvalidInputError, OperationConflictError, RepositoryUnavailableError
from .migration_planning_service import validate_key
from .migration_run_service import MigrationRunService, _uuid
from .jev_store import read_jev_state
from .jev_database_transport import database_read

PAID_LIMITS = {'old_pages': 15000, 'new_pages': 20000, 'inventory_bytes': 16777216, 'passes': 3, 'days': 30}

LIMITS = {'old_pages': 500, 'new_pages': 2000, 'inventory_bytes': 2097152, 'passes': 3, 'new_runs_per_24h': 5}


def enabled():
    return os.getenv('JEV_MVP_ENABLED', 'false').lower() == 'true'


def current_billing_enabled():
    return os.getenv('REDIRX_CURRENT_BILLING_ENABLED', 'false').lower() == 'true'


from .migration_repository import MigrationRepositoryError


class ExtraApprovalRequired(MigrationRepositoryError):
    code = 'approval_required'
    next_action = 'approve_extra_urls'

    def __init__(self, details):
        self.details = details
        super().__init__('Approve the total period extra charge before starting this run.')


class JevService:
    def __init__(self, repository=None):
        self.repository = repository or MigrationRepository()
        self.client = self.repository.client

    def state(self, run_id):
        return read_jev_state(self.client, run_id)

    def start(self, user_id, migration_id, old_id, new_id, quote_id, key, confirmed_pairs=None):
        if confirmed_pairs is not None and (not isinstance(confirmed_pairs,list) or len(confirmed_pairs)>100 or any(
            not isinstance(pair,dict) or set(pair)!={'old_url','new_url'} or any(not isinstance(value,str) or not 1<=len(value)<=8192 for value in pair.values())
            for pair in confirmed_pairs)):
            raise InvalidInputError('confirmed_pairs must contain at most 100 explicit old_url/new_url pairs.')
        # The reused service validates the standard run envelope and safe RPC errors.
        params = dict(p_user_id=_uuid(user_id,'user_id'), p_migration_id=_uuid(migration_id,'migration_id'),
                      p_old_inventory_id=_uuid(old_id,'old_inventory_id'), p_new_inventory_id=_uuid(new_id,'new_inventory_id'),
                      p_quote_id=_uuid(quote_id,'quote_id'), p_idempotency_key=validate_key(key),p_confirmed_pairs=confirmed_pairs or [])
        return MigrationRunService(self.repository)._call('reserve_jev_run', params)

    def start_current(self, user_id, migration_id, old_id, new_id, key, confirmed_pairs=None,
                      checkout_id=None, subscription_id=None, approve_extra_urls=False, max_extra_amount_cents=0):
        from .migration_quote_service import _ERRORS, QuoteNotReadyError
        from .inventory_import_service import ImportCapacityExceededError
        if not current_billing_enabled() or not enabled():
            raise QuoteNotReadyError('Current paid admission is not enabled.')
        mode = os.getenv('REDIRX_STRIPE_MODE', 'test')
        if mode not in ('test', 'live'):
            raise QuoteNotReadyError('The billing environment is not configured.')
        if (type(approve_extra_urls) is not bool or type(max_extra_amount_cents) is not int
                or not 0 <= max_extra_amount_cents <= 2147483647
                or (checkout_id is not None and subscription_id is not None)):
            raise InvalidInputError('Invalid payment selection or extra-charge approval.')
        if confirmed_pairs is not None and (not isinstance(confirmed_pairs,list) or len(confirmed_pairs)>100 or any(
            not isinstance(pair,dict) or set(pair)!={'old_url','new_url'} or any(not isinstance(value,str) or not 1<=len(value)<=8192 for value in pair.values())
            for pair in confirmed_pairs)):
            raise InvalidInputError('confirmed_pairs must contain at most 100 explicit old_url/new_url pairs.')
        params = dict(p_user_id=_uuid(user_id,'user_id'),p_migration_id=_uuid(migration_id,'migration_id'),
            p_old_inventory_id=_uuid(old_id,'old_inventory_id'),p_new_inventory_id=_uuid(new_id,'new_inventory_id'),
            p_idempotency_key=validate_key(key),p_confirmed_pairs=confirmed_pairs or [],
            p_checkout_id=_uuid(checkout_id,'checkout_id') if checkout_id is not None else None,
            p_subscription_id=_uuid(subscription_id,'subscription_id') if subscription_id is not None else None,p_approve_extra_urls=approve_extra_urls,
            p_max_extra_amount_cents=max_extra_amount_cents,p_livemode=mode=='live')
        try:
            response = self.client.rpc('reserve_current_jev_run', params).execute()
            if getattr(response,'error',None): raise response.error
            result = response.data
        except Exception as exc:
            code = str(getattr(exc,'message','') or str(exc)).strip()
            if str(getattr(exc,'code','')) == 'P0001':
                if code == 'approval_required':
                    try:
                        details=json.loads(getattr(exc,'details',None) or exc.diag.message_detail)
                        details={k:details[k] for k in ('extra_old_urls','extra_amount_cents')}
                        if any(type(v) is not int or v<0 for v in details.values()): raise ValueError()
                    except (AttributeError,TypeError,ValueError,KeyError): details={}
                    raise ExtraApprovalRequired(details) from None
                if code == 'capacity_exceeded':
                    raise ImportCapacityExceededError('Paid runs support 15000 old URLs, 20000 new URLs and 16 MiB of URL text.') from None
                if code in _ERRORS:
                    cls,message=_ERRORS[code]; raise cls(message) from None
            raise RepositoryUnavailableError('Current run admission is temporarily unavailable.') from None
        if isinstance(result,list) and len(result)==1: result=result[0]
        try:
            for field in ('migration_id','operation_id','run_id','session_id'): _uuid(result[field],field)
            if (result['billing_version']!='redirx_2026_10' or result['sku'] not in ('trial','payg','studio','agency')
                    or result['status']!='queued' or type(result['replayed']) is not bool): raise ValueError()
        except (KeyError,TypeError,ValueError,InvalidInputError):
            raise RepositoryUnavailableError('Current run admission returned an invalid result.') from None
        return {k:result[k] for k in ('migration_id','operation_id','run_id','session_id','status','replayed','billing_version','sku','free','checkout_id','period_id','extra_amount_cents')}

    def refine(self, user_id, migration_id, run_id, revision, key):
        state = self.state(run_id)
        if state and state.get('billing_version') == 'redirx_2026_10' and not current_billing_enabled():
            from .migration_quote_service import QuoteNotReadyError
            raise QuoteNotReadyError('Current paid admission is paused.')
        if type(revision) is not int or revision < 0:
            raise InvalidInputError('expected_seed_revision must be a nonnegative integer.')
        params = dict(p_user_id=_uuid(user_id,'user_id'), p_migration_id=_uuid(migration_id,'migration_id'),
                      p_run_id=_uuid(run_id,'run_id'), p_expected_seed_revision=revision, p_idempotency_key=validate_key(key))
        try:
            result = self.client.rpc('refine_jev_run', params).execute().data
        except Exception as exc:
            code = getattr(exc, 'message', '')
            if code == 'payment_required':
                from .migration_quote_service import PaymentRequiredError
                raise PaymentRequiredError('The run entitlement has expired or was revoked.') from None
            if code == 'refinement_limit':
                raise InvalidInputError('At most three passes are available; confirm or correct examples before another pass.') from None
            if code == 'operation_conflict':
                raise OperationConflictError('Refresh status and seed_revision before refinement; a pass may still be running.') from None
            if code == 'not_found':
                from .migration_repository import MigrationNotFoundError
                raise MigrationNotFoundError('Migration run not found.') from None
            raise RepositoryUnavailableError('Refinement is temporarily unavailable.') from None
        if isinstance(result, dict) and not result.get('replayed'):
            # refine_jev_run's own idempotency key covers exact re-submission;
            # a replay must not look like a second refine pass starting.
            capture(AppEvent.MIGRATION_REFINE_STARTED, user_id=user_id, migration_id=migration_id, properties={
                "run_id": run_id, "operation_id": result.get('operation_id'),
                "expected_seed_revision": revision,
            })
        return result

    def enrich(self, run_id, result):
        state = self.state(run_id)
        if not state:
            return result
        result['jev'] = {'engine':'jev-url-v1','model':MODEL,'prompt_version':PROMPT_VERSION,
                         'pass':state['pass'],'seed_revision':state['seed_revision'], 'limits':PAID_LIMITS if state.get('billing_sku') in ('payg','studio','agency') else LIMITS,
                         'provider_requests_reserved':state['provider_requests']}
        counted=[]
        for offset in range(0,15000,500):
            page=database_read(lambda: self.client.table('jev_proposals').select('old_url_hash,pass').eq('run_id',str(run_id)).order('old_url_hash').range(offset,offset+499).execute().data)
            counted.extend(page)
            if len(page)<500: break
        result['jev']['completed_proposals']=sum(row['pass']==state['pass'] for row in counted)
        result['jev']['confirmed_examples']=len(state['seeds'])
        ids = [item['mapping_id'] for item in result.get('items', [])]
        if ids:
            rows = database_read(lambda: self.client.table('jev_proposals').select('*').eq('run_id',str(run_id)).in_('mapping_id',ids).execute().data)
            by_id = {str(row['mapping_id']):row for row in rows}
            for item in result['items']:
                row = by_id.get(str(item['mapping_id']))
                if row:
                    item['jev_proposal'] = {**row['proposal'], 'pass':row['pass'], 'seed_revision':row['seed_revision'],
                                            'stale':row['seed_revision'] != state['seed_revision']}
        return result

