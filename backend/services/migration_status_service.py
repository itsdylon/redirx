"""Bounded owned lifecycle read model; never creates rights, jobs or exports."""
from copy import deepcopy
from datetime import datetime, timezone
import os
from uuid import UUID

from .migration_repository import MigrationRepository, MigrationNotFoundError, RepositoryUnavailableError, InvalidInputError, _strict_uuid, _rows
from .migration_planning_service import MigrationPlanningService, envelope
from .migration_verification_service import MigrationVerificationService
from .migration_monitoring_service import MigrationMonitoringService


def _json(value):
    if isinstance(value, UUID): return str(value)
    if isinstance(value, datetime): return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    if isinstance(value, dict): return {key: _json(item) for key, item in value.items()}
    if isinstance(value, list): return [_json(item) for item in value]
    return value


class MigrationStatusService:
    def __init__(self, repository=None):
        # Master-disabled callers do not even construct the database client.
        self.repository = repository

    def _one(self, table, columns, owner, migration=None, **filters):
        # The projection is an acknowledgment, including explicit nullable fields.
        # Select scope columns too so a malformed/misrouted row cannot describe
        # a different owner, migration or requested child. Never return them just
        # because validation needed them.
        projected = columns.split(',')
        scope = {'user_id': owner}
        if migration is not None: scope['migration_id'] = migration
        scope.update(filters)
        selected = list(dict.fromkeys([*projected, *scope]))
        query = self.repository.client.table(table).select(','.join(selected))
        for key, value in scope.items(): query = query.eq(key, str(value))
        rows = _rows(self.repository._execute(query.order('created_at', desc=True).order('id', desc=True).limit(1)))
        if not rows: return None
        if len(rows) != 1 or any(key not in rows[0] for key in selected):
            raise RepositoryUnavailableError('Migration status is temporarily unavailable.')
        row = rows[0]
        try:
            # Nullable historical links remain null; supplied links must be UUIDs
            # before they can be used in another lookup or published as a route.
            _strict_uuid(row['id'], 'id')
            for key in selected:
                if (key.endswith('_id') or key == 'rerun_of') and row[key] is not None:
                    _strict_uuid(row[key], key)
            for key, expected in scope.items():
                if key == 'id' or key.endswith('_id'):
                    matches = _strict_uuid(row[key], key)[0] == _strict_uuid(expected, key)[0]
                else:
                    matches = row[key] == expected
                if not matches:
                    raise RepositoryUnavailableError('Migration status is temporarily unavailable.')
        except InvalidInputError:
            raise RepositoryUnavailableError('Migration status is temporarily unavailable.') from None
        return {key: row[key] for key in projected}

    def get(self, user_id, migration_id, *, run_id=None, base_summary=None):
        """Pass root's discovery summary as base_summary to retain its progress.

        run_id explicitly selects an owned historical run; otherwise latest run
        is selected. Children are scoped to that run's latest artifact/deployment,
        so a previous run's healthy result cannot describe a new artifact.
        """
        if os.getenv('MCP_PIVOT_ENABLED', 'false').lower() != 'true':
            return envelope(status='failed', next_action='retry', error={
                'code': 'unavailable', 'message': 'Migration status is unavailable.',
                'retryable': True, 'next_action': 'retry'})
        _, owner = _strict_uuid(user_id, 'user_id'); _, migration = _strict_uuid(migration_id, 'migration_id')
        if run_id is not None: _, run_id = _strict_uuid(run_id, 'run_id')
        if self.repository is None: self.repository = MigrationRepository()
        self.repository.get_migration(owner, migration)
        result = deepcopy(base_summary) if base_summary is not None else MigrationPlanningService(self.repository).get(owner, migration)
        data = result['data']
        data.update(run=None, artifact=None, deployment=None, verification=None, monitoring=None,
                    run_id=None, artifact_id=None, deployment_id=None, verification_id=None, monitoring_id=None)
        run = self._one('migration_runs', 'id,operation_id,quote_id,legacy_session_id,old_inventory_id,new_inventory_id,rerun_of,created_at',
                        owner, migration, **({'id': run_id} if run_id else {}))
        if run_id and run is None: raise MigrationNotFoundError('Migration run not found.')
        quote = self._one('migration_price_quotes', 'id,operation_id,kind,amount_cents,currency,old_pages,expires_at,created_at',
                          owner, migration, **({'id': run['quote_id']} if run and run.get('quote_id') else {}))
        if run and not run.get('quote_id'): quote = None
        data['quote'] = _json({**quote, 'quote_id': str(quote['id'])}) if quote else None
        data['quote_id'] = str(quote['id']) if quote else None
        if not run:
            pending = self._one('migration_operations', 'id,status,created_at', owner, migration, kind='run_migration')
            if pending and pending['status'] == 'payment_required':
                result.update(operation_id=str(pending['id']), status='payment_required', next_action='complete_payment')
                data['summary'] = 'The migration requires an existing grant or payment before dispatch.'
            return _json(result)
        operation = self._one('migration_operations', 'id,status,created_at', owner, migration, id=run['operation_id']) if run.get('operation_id') else None
        session = self._one('migration_sessions', 'id,status,current_stage,total_stages,last_error,created_at', owner,
                            id=run['legacy_session_id']) if run.get('legacy_session_id') else None
        status = operation['status'] if operation else {'completed': 'succeeded', 'processing': 'running', 'pending': 'queued',
                  'permanently_failed': 'failed', 'failed': 'failed'}.get(session['status'] if session else '', 'needs_input')
        if status == 'reserved': status = 'queued'
        progress = {'current_stage': session.get('current_stage') if session else None,
                    'total_stages': session.get('total_stages') if session else None, 'complete': status == 'succeeded'}
        data['run'] = _json({'run_id': run['id'], 'operation_id': run.get('operation_id'), 'quote_id': run.get('quote_id'),
            'status': status, 'progress': progress, 'inventory_ids': {'old': run['old_inventory_id'], 'new': run['new_inventory_id']},
            'rerun_of': run['rerun_of'], 'created_at': run['created_at']})
        data['run_id'] = str(run['id'])
        if os.getenv('JEV_MVP_ENABLED','false').lower() == 'true':
            from .jev_pipeline_service import JevService
            JevService(self.repository).enrich(str(run['id']),data)
            if data.get('jev'):
                data['run']['engine']='jev-url-v1'
        result.update(operation_id=str(run['operation_id']) if run.get('operation_id') else None, status=status,
                      next_action='poll' if status in ('queued', 'running') else 'resolve_matches' if status == 'succeeded' else 'retry', progress=progress)
        data['summary'] = 'Migration run is complete; review its mappings before export.' if status == 'succeeded' else 'Migration run progress is persisted.'
        if status in ('queued', 'running'):
            result['retry_after_seconds'] = 10
            return _json(result)
        result.pop('retry_after_seconds', None)
        if status != 'succeeded':
            if status == 'failed' and data.get('jev'):
                result['next_action']='refine_matches'
                reason=(session.get('last_error') or '').split(':',1)[0] if session else ''
                if reason in {'provider_rate_limited','provider_credit_exhausted','provider_unavailable','provider_budget_exhausted','embedding_unavailable','engine_version_unavailable','jev_paused','request_capacity_exceeded','jev_run_interrupted'}:
                    data['jev']['pause_reason']=reason
                if reason=='provider_budget_exhausted':
                    now=datetime.now(timezone.utc)
                    result['retry_after_seconds']=86400-(now.hour*3600+now.minute*60+now.second)

                data['summary']='Jev paused with saved progress. Resolve provider availability or budget, then resume with refine_matches and current seed_revision.'
            return _json(result)
        artifact = self._one('migration_artifacts', 'id,run_id,decision_revision,format,content_hash,included_count,excluded_count,created_at',
                             owner, migration, run_id=run['id'])
        if not artifact: return _json(result)
        data['artifact'] = _json({**artifact, 'artifact_id': artifact['id'],
            'download_url': f'/api/v2/migrations/{migration}/artifacts/{artifact["id"]}'})
        data['artifact_id'] = str(artifact['id'])
        result.update(status='needs_input', next_action='install_artifact')
        data['summary'] = 'The immutable artifact is ready. Report installation before checking live redirects.'
        deployment = self._one('artifact_deployments', 'id,artifact_id,live_origin,status,installation_reported_at,live_verified_at,created_at',
                               owner, migration, artifact_id=artifact['id'])
        if not deployment: return _json(result)
        data['deployment'] = _json({**deployment, 'deployment_id': deployment['id']})
        data['deployment_id'] = str(deployment['id'])
        if deployment['status'] == 'generated': return _json(result)
        result['next_action'] = 'verify_redirects'
        data['summary'] = 'Installation was reported. A live check is still required.'
        if os.getenv('MCP_PIVOT_VERIFICATION_ENABLED', 'false').lower() == 'true':
            verification = self._one('migration_verifications', 'id,operation_id,migration_id,artifact_id,deployment_id,status,total,passed,failed,unchecked,created_at',
                                     owner, migration, deployment_id=deployment['id'], kind='included')
            if verification:
                measured = MigrationVerificationService._envelope(verification)
                data['verification'] = measured['data']; data['verification_id'] = str(verification['id'])
                result.update(status=measured['status'], next_action=measured['next_action'], operation_id=str(verification['operation_id']), progress=measured['progress'])
                data['summary'] = measured['data']['summary']
                if measured['next_action'] == 'poll': result['retry_after_seconds'] = 10
        if os.getenv('MCP_PIVOT_MONITORING_ENABLED', 'false').lower() == 'true':
            monitor = self._one('migration_monitors', 'id,created_at', owner, migration, deployment_id=deployment['id'])
            if monitor:
                tracked = MigrationMonitoringService(self.repository).status(owner, migration, str(monitor['id']))
                data['monitoring'] = tracked['data']; data['monitoring_id'] = str(monitor['id'])
                # Do not overwrite an in-progress/failed included verification
                # with a schedule's existence or a healthy historical sweep.
                if data['verification'] and data['verification']['outcome'] == 'passed':
                    result.update(status=tracked['status'], next_action=tracked['next_action'])
                    data['summary'] = tracked['data']['summary']
        return _json(result)
