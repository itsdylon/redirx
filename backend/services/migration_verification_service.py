"""Resumable included checks against an immutable deployed artifact.

Does not require a Watch subscription or alter recurring schedules. Production
network access uses the existing per-hop validation and connect-time safe DNS
resolver. Worker leases, observations and progress are persisted independently.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

from src.redirx.redirect_probe import (probe, MAX_HOPS, request_target_preserved,
    REQUEST_URL_REWRITTEN, REQUEST_URL_REWRITTEN_DETAIL)
from src.redirx.safe_fetch import create_safe_connector
from src.redirx.rate_limit import (HostRateLimiter, WATCH_NAMESPACE, WATCH_CAPACITY,
    WATCH_RATE, set_limiter, reset_limiter)
from .analytics_service import AppEvent, capture
from .inventory_policy import canonical_url_identity, InventoryPolicyError
from .migration_repository import (MigrationRepository, MigrationRepositoryError,
    MigrationNotFoundError, InvalidInputError, _strict_uuid)
from .migration_planning_service import envelope, validate_key

PROBE_DEADLINE_SECONDS = 45
logger = logging.getLogger(__name__)


class VerificationEntitlementError(MigrationRepositoryError):
    def __init__(self, code):
        self.code = code
        super().__init__({'payment_required': 'An active grant for the artifact run is required.',
                          'allowance_exhausted': 'The included check is already bound to another deployed artifact.',
                          'not_ready': 'Report installation of this artifact before verification.'}[code])


def _safe_url(url):
    try:
        # Evidence is bounded independently from the trusted artifact inputs.
        return canonical_url_identity(url)[:8192]
    except (TypeError, ValueError, InventoryPolicyError):
        return None


def assess_probe(result, expected_url):
    """Strict artifact identity; an unavailable destination cannot pass."""
    evidence = {'final_url': _safe_url(result.final_url), 'final_status': result.final_status,
                'hops': [{'url': _safe_url(h.url), 'status': h.status,
                          'location': (h.location or '')[:8192]} for h in result.hops[:MAX_HOPS + 1]],
                'error': result.error}
    # Cap evidence while retaining chain shape; do not persist untrusted bodies.
    for hop in evidence['hops']:
        hop['location'] = hop['location'][:1024]
        if hop['url']:
            hop['url'] = hop['url'][:1024]
    if result.error == REQUEST_URL_REWRITTEN:
        state, issue = 'unchecked', REQUEST_URL_REWRITTEN
        evidence['detail'] = REQUEST_URL_REWRITTEN_DETAIL
    elif result.error in ('loop', 'too_many_hops'):
        state, issue = 'failed', 'redirect_loop'
    elif result.error:
        state, issue = 'unchecked', ('blocked' if result.error == 'blocked' else 'unavailable')
    elif not request_target_preserved(expected_url):
        state, issue = 'unchecked', REQUEST_URL_REWRITTEN
        evidence['detail'] = REQUEST_URL_REWRITTEN_DETAIL
    elif result.final_status in (401, 403, 429) or (result.final_status or 0) >= 500:
        state, issue = 'unchecked', 'origin_unavailable'
    elif result.final_status in (404, 410):
        state, issue = 'failed', 'not_found'
    elif not 200 <= (result.final_status or 0) < 300:
        state, issue = 'failed', 'invalid_final_status'
    elif not result.hops:
        state, issue = 'failed', 'no_redirect'
    elif not evidence['final_url'] or evidence['final_url'] != _safe_url(expected_url):
        state, issue = 'failed', 'wrong_target'
    elif not result.all_permanent:
        state, issue = 'failed', 'temporary_redirect'
    elif len(result.hops) > 1:
        state, issue = 'failed', 'redirect_chain'
    else:
        state, issue = 'passed', None
    return state, {**evidence, 'issue': issue, 'measurement': 'unavailable' if state == 'unchecked' else 'observed'}


class MigrationVerificationService:
    def __init__(self, repository=None):
        self.repository = repository if repository is not None else MigrationRepository()
        # Per-instance dedup for the terminal capture in record(): a single
        # PivotBackgroundRunner constructs one MigrationVerificationService and
        # reuses it for the worker process's whole life (pivot_background.py),
        # so this survives every batch that worker claims. complete_verification_item
        # (048_studio_included_verification.sql) returns only a bare boolean —
        # no flag says whether THIS call was the one that flipped the row to
        # terminal — so this guard is what keeps concurrent item completions in
        # the same batch from each independently reading the just-flipped
        # status and firing the event more than once. A second worker PROCESS
        # claiming a later batch of the same oversized verification is a real,
        # accepted gap this does not close; see the capture note below.
        self._verifications_completed = set()

    def _rpc(self, name, params):
        # The base repository intentionally handles only shared errors. Map this
        # packet's entitlement codes without exposing raw PostgREST messages.
        query = self.repository.client.rpc(name, params)
        try:
            response = query.execute()
            if getattr(response, 'error', None):
                raise response.error
            return response.data
        except Exception as exc:
            code = getattr(exc, 'message', '')
            if getattr(exc, 'code', '') == 'P0001' and code in ('payment_required','allowance_exhausted','not_ready'):
                raise VerificationEntitlementError(code) from None
            # Reuse the repository boundary without issuing the query twice.
            class FailedQuery:
                def execute(self): raise exc
            return self.repository._execute(FailedQuery()).data

    def start(self, user_id, migration_id, artifact_id, deployment_id, idempotency_key):
        ids = {name: _strict_uuid(value, name)[1] for name, value in (
            ('user_id',user_id),('migration_id',migration_id),('artifact_id',artifact_id),('deployment_id',deployment_id))}
        row = self._rpc('reserve_included_verification', {**{'p_'+k:v for k,v in ids.items()}, 'p_key': validate_key(idempotency_key)})
        return self._envelope(row)

    def _owned(self, user_id, migration_id, verification_id):
        _, owner = _strict_uuid(user_id,'user_id'); _, migration = _strict_uuid(migration_id,'migration_id')
        _, verification = _strict_uuid(verification_id,'verification_id')
        rows = self.repository._execute(self.repository.client.table('migration_verifications').select('*')
            .eq('id',verification).eq('user_id',owner).eq('migration_id',migration)).data
        if not rows:
            raise MigrationNotFoundError('Verification not found.')
        return rows[0]

    @staticmethod
    def _envelope(row):
        passed, failed, unchecked, total = (row[k] for k in ('passed','failed','unchecked','total'))
        checked = passed + failed
        pending = total - checked - unchecked
        complete = pending == 0 and unchecked == 0
        outcome = 'passed' if complete and failed == 0 else 'issues_found' if complete else 'unverifiable' if row['status']=='partial' else 'in_progress'
        result = envelope(str(row['migration_id']),str(row['operation_id']),status=row['status'],
            next_action='poll' if row['status'] in ('queued','running') else 'retry' if row['status']=='partial' else 'resolve_matches' if failed else 'none',
            data={'verification_id':str(row['id']), 'artifact_id':str(row['artifact_id']), 'deployment_id':str(row['deployment_id']),
                'outcome':outcome,'total':total,'checked':checked,'passed':passed,'failed':failed,
                'unchecked':total-checked,'pending':pending,'unavailable':unchecked,
                'complete':complete,'included':True,'summary':f'{checked} of {total} redirects checked; {failed} issues; {total-checked} unchecked.'})
        result['progress']={'completed':checked,'total':total,'complete':complete}
        if row['status'] in ('queued','running'):
            result['retry_after_seconds']=10
        return result

    def status(self, user_id, migration_id, verification_id):
        return self._envelope(self._owned(user_id,migration_id,verification_id))

    def issues(self, user_id, migration_id, verification_id, *, after=-1, limit=100):
        row = self._owned(user_id,migration_id,verification_id)
        if isinstance(after,bool) or not isinstance(after,int) or after < -1 or isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=500:
            raise InvalidInputError('Use an ordinal cursor and a limit between 1 and 500.')
        items = self.repository._execute(self.repository.client.table('migration_verification_items').select('*')
            .eq('verification_id',str(row['id'])).in_('state',['failed','unchecked']).gt('ordinal',after).order('ordinal').limit(limit+1)).data
        return {'items':[{'issue_id':str(row['id'])+':'+str(item['ordinal']), 'mapping_id':item['mapping_id'],
                'source_url':item['source_url'],'expected_url':item['expected_url'],'state':item['state'],
                'evidence':item['finding'], 'artifact_id':str(row['artifact_id']),
                'suggested_action':'restore_artifact_rule' if item['state']=='failed' else 'retry_verification'} for item in items[:limit]],
                'next_cursor':items[limit-1]['ordinal'] if len(items)>limit else None}

    def claim(self, worker_id, batch_size=50):
        if not isinstance(worker_id,str) or not worker_id.strip() or len(worker_id)>100 or isinstance(batch_size,bool) or not isinstance(batch_size,int) or not 1<=batch_size<=100:
            raise InvalidInputError('Worker ID and batch size 1–100 are required.')
        return self._rpc('claim_verification_batch', {'p_worker':worker_id,'p_limit':batch_size})

    def record(self, verification_id, item, worker_id, state, finding):
        applied = self._rpc('complete_verification_item', {'p_verification':verification_id,'p_ordinal':item['ordinal'],
            'p_worker':worker_id,'p_attempt':item['attempt'],'p_state':state,'p_finding':finding})
        if applied:
            self._maybe_capture_completed(verification_id)
        return applied

    def _maybe_capture_completed(self, verification_id):
        key = str(verification_id)
        if key in self._verifications_completed:
            return
        try:
            rows = self.repository._execute(self.repository.client.table('migration_verifications')
                .select('id,user_id,migration_id,artifact_id,deployment_id,status,passed,failed,unchecked,total')
                .eq('id', key)).data
        except Exception:
            return
        if not rows:
            return
        row = rows[0]
        if row.get('status') not in ('succeeded', 'partial'):
            return
        self._verifications_completed.add(key)
        # Only 'included' verification (this JEV/pivot service) fires here.
        # The legacy Watch feature's own MIGRATION_VERIFICATION_COMPLETED call
        # (watch_service.py) is untouched; 'verification_kind' distinguishes
        # the two in the same event stream instead of adding a new enum name
        # for what is, from the funnel's point of view, the same milestone.
        capture(AppEvent.MIGRATION_VERIFICATION_COMPLETED, user_id=row.get('user_id'),
                migration_id=row.get('migration_id'), properties={
                    "verification_id": key, "verification_kind": "included",
                    "artifact_id": row.get('artifact_id'), "deployment_id": row.get('deployment_id'),
                    "status": row.get('status'), "passed": row.get('passed'),
                    "failed": row.get('failed'), "unchecked": row.get('unchecked'), "total": row.get('total'),
                })


async def run_verification_batch(service, worker_id, batch_size=50):
    """Pace included checks and recurring monitoring with the shared host budget.

    The monitoring adapter calls this same entry point. A connection semaphore
    limits simultaneous sockets but does not provide per-host rate/backoff rules.
    Restore the caller's context even when claiming, probing or cancellation fails.
    Existing limiter enablement and fail-open infrastructure policy still apply.
    """
    token = set_limiter(HostRateLimiter(namespace=WATCH_NAMESPACE,
        capacity=WATCH_CAPACITY, rate=WATCH_RATE))
    try:
        return await _run_verification_batch(service, worker_id, batch_size)
    finally:
        reset_limiter(token)


async def _run_verification_batch(service, worker_id, batch_size=50):
    """One callable worker unit; re-invoke until claim returns no available work.

    No user-supplied connector/prober injection exists in production. Tests patch
    network boundaries explicitly. A killed process leaves reclaimable leases.
    """
    batch = await asyncio.to_thread(service.claim, worker_id, batch_size)
    if not batch:
        return {'claimed':0,'recorded':0}
    semaphore = asyncio.Semaphore(10)
    async with aiohttp.ClientSession(connector=create_safe_connector(limit=10)) as session:
        writes = []

        async def one(item):
            async with semaphore:
                try:
                    canonical_url_identity(item['source_url'])
                    canonical_url_identity(item['expected_url'])
                    result = await asyncio.wait_for(probe(session,item['source_url']),timeout=PROBE_DEADLINE_SECONDS)
                    state, finding = assess_probe(result,item['expected_url'])
                except asyncio.TimeoutError:
                    state, finding = 'unchecked', {'issue':'timeout','measurement':'unavailable'}
                except Exception:
                    state, finding = 'unchecked', {'issue':'worker_error','measurement':'unavailable'}
                # Persist each finished observation, not just the end of a batch.
                # A cancelled await cannot stop a synchronous DB call. Keep its
                # task alive so shutdown can drain it without abandoning writes.
                write = asyncio.create_task(asyncio.to_thread(
                    service.record,batch['verification_id'],item,worker_id,state,finding))
                writes.append(write)
                return await asyncio.shield(write)

        tasks = [asyncio.create_task(one(item)) for item in batch['items']]
        settled = asyncio.gather(*tasks, return_exceptions=True)
        try:
            # One failed persistence call must not close the shared HTTP session
            # under sibling probes and manufacture "Session is closed" findings.
            outcomes = await asyncio.shield(settled)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            # Keep the session open until all probes have unwound, then await
            # every already-started write. Repeated shutdown signals cannot
            # interrupt this drain; cancellation is re-raised afterwards.
            for pending in (settled, asyncio.gather(*writes, return_exceptions=True)):
                while not pending.done():
                    try:
                        await asyncio.shield(pending)
                    except asyncio.CancelledError:
                        continue
            raise
    recorded = sum(not isinstance(value, BaseException) and bool(value) for value in outcomes)
    failed = len(outcomes) - recorded
    result = {'verification_id':batch['verification_id'],'claimed':len(batch['items']),'recorded':recorded}
    if failed:
        # No retry/fallback write is invented. Failed or fenced writes remain
        # governed by their original lease/attempt; an uncertain commit is not
        # called recorded. Only counts are logged, never DB/provider messages.
        logger.warning('Verification batch could not confirm %d of %d observation writes; durable work will retry',
                       failed, len(outcomes))
        result.update(record_failed=failed, retryable=True)
    return result
