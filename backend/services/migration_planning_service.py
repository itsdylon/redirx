"""Durable planning and bounded summaries; explicit imports never imply a crawl."""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone

from .analytics_service import AppEvent, capture
from .inventory_policy import InventoryPolicyError, normalize_origin, _exclusion_original
from .migration_repository import (
    InvalidInputError, MigrationRepository, RepositoryUnavailableError, _strict_uuid,
)

CONTRACT = json.loads((Path(__file__).resolve().parents[2] / 'contracts/pivot-v1.json').read_text())


def envelope(migration_id=None, operation_id=None, *, status='needs_input',
             next_action='provide_inventory', data=None, error=None):
    if status not in CONTRACT['operation_statuses'] or next_action not in CONTRACT['next_actions']:
        raise ValueError('Invalid operation envelope')
    return dict(contract_version=CONTRACT['contract_version'], migration_id=migration_id,
                operation_id=operation_id, status=status, next_action=next_action,
                data=data or {}, error=error)


def validate_key(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 200
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise InvalidInputError('idempotency_key must be a nonblank string of at most 200 characters.')
    return value


def validate_plan(body):
    allowed = {'old_site', 'new_site', 'name', 'site_aliases', 'idempotency_key'}
    if not isinstance(body, dict) or set(body) - allowed:
        raise InvalidInputError('Unsupported planning fields.')
    key = validate_key(body.get('idempotency_key'))
    name = body.get('name')
    if name is not None and (not isinstance(name, str) or len(name) > 200
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in name)):
        raise InvalidInputError('name must be a string of at most 200 characters.')
    aliases = body.get('site_aliases', {'old': [], 'new': []})
    if not isinstance(aliases, dict) or set(aliases) - {'old', 'new'}:
        raise InvalidInputError('site_aliases must contain old and/or new origin arrays.')
    result = {'name': name, 'site_aliases': {}}
    try:
        for side in ('old', 'new'):
            result[side + '_site'] = normalize_origin(body.get(side + '_site'))
            values = aliases.get(side, [])
            if not isinstance(values, list) or len(values) > 100:
                raise InvalidInputError('Each side accepts at most 100 explicit aliases.')
            result['site_aliases'][side] = sorted(set(normalize_origin(v) for v in values))
    except InventoryPolicyError:
        raise InvalidInputError('Sites and aliases must be HTTP(S) origins without credentials, paths, queries or fragments.') from None
    return key, result


# Persisted policy messages are mapped to finite public codes, never reflected.
_INPUT_ISSUE_CODES = {
    'URL origin was not explicitly declared.': 'undeclared_origin',
    'url must be a valid HTTP(S) URL.': 'invalid_url',
    'url must not contain credentials.': 'invalid_url',
    'url has an invalid hostname.': 'invalid_url',
    'inventory rows must be URL strings or objects.': 'invalid_row',
    'inventory rows must contain a URL string.': 'invalid_row',
    'inventory provenance must contain nonblank strings.': 'invalid_provenance',
    'inventory metadata exceeds bounded JSON limits.': 'invalid_metadata',
    'inventory metadata must be JSON-safe.': 'invalid_metadata',
}


def _input_issues(row):
    coverage = row.get('coverage') or {}
    if not isinstance(coverage, dict) or coverage.get('kind') != 'explicit_import':
        return None
    exclusions = row.get('exclusions')
    available = isinstance(exclusions, list)
    reported = coverage.get('excluded_count')
    reported = reported if type(reported) is int and reported >= 0 else None
    entries = exclusions if available else []
    counts, examples = {}, []
    for entry in entries:
        entry = entry if isinstance(entry, dict) else {}
        reason = entry.get('reason')
        code = _INPUT_ISSUE_CODES.get(reason, 'invalid_input') if isinstance(reason, str) else 'invalid_input'
        counts[code] = counts.get(code, 0) + 1
        if len(examples) < 10:
            # Same privacy policy as persisted exclusions; retain exact path
            # spelling but never queries, fragments, credentials or long hints.
            hint = _exclusion_original(entry.get('original_url'))
            examples.append({'code': code, 'url_hint': hint if hint is not None and len(hint) <= 512 else None})
    total = reported if reported is not None else len(entries) if available else None
    return {'total_excluded': total, 'diagnostic_count': len(entries),
            'diagnostics_complete': available and (reported is None or reported == len(entries)),
            'reason_counts': counts, 'examples': examples,
            'truncated': len(entries) > len(examples) or total is not None and total > len(examples)}


def inventory_summary(row):
    if row is None:
        return None
    result = {key: row.get(key) for key in
              ('id', 'side', 'status', 'page_count', 'content_hash', 'policy_version', 'coverage', 'completed_at')}
    if isinstance(result['completed_at'], datetime):
        result['completed_at'] = result['completed_at'].astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    issues = _input_issues(row)
    if issues is not None:
        result['input_issues'] = issues
    return result


class MigrationPlanningService:
    def __init__(self, repository=None):
        self.repository = repository if repository is not None else MigrationRepository()

    def plan(self, user_id, body):
        _, owner = _strict_uuid(user_id, 'user_id')
        key, payload = validate_plan(body)
        result = self.repository._execute(self.repository.client.rpc('plan_migration', {
            'p_user_id': owner, 'p_idempotency_key': key, 'p_request': payload,
        })).data
        if isinstance(result, list):
            result = result[0] if len(result) == 1 else None
        if not isinstance(result, dict) or set(result) != {'migration_id', 'operation_id', 'replayed'}:
            raise RepositoryUnavailableError('Migration planning is temporarily unavailable.')
        try:
            _, migration_id = _strict_uuid(result['migration_id'], 'migration_id')
            _, operation_id = _strict_uuid(result['operation_id'], 'operation_id')
            if not isinstance(result['replayed'], bool):
                raise InvalidInputError('Invalid result.')
        except InvalidInputError:
            raise RepositoryUnavailableError('Migration planning is temporarily unavailable.') from None
        # Replay refers to the original operation, with current persisted readiness.
        summary = self.get(owner, migration_id)
        summary['operation_id'] = operation_id
        summary['data']['replayed'] = result['replayed']
        if not result['replayed']:
            # This is the JEV-only branch of plan_migration (v2_routes.py never
            # reaches here for the legacy discovery path), so a plan created via
            # this method is always a JEV pivot migration. A replay is the same
            # idempotency key resubmitted, not a new plan — never fire twice for
            # one plan.
            capture(AppEvent.MIGRATION_PLAN_CREATED, user_id=owner, migration_id=migration_id,
                    properties={"operation_id": operation_id, "engine": "jev-url-v1"})
        return summary

    def get(self, user_id, migration_id):
        migration = self.repository.get_migration(user_id, migration_id)
        inventories = {side: inventory_summary(self.repository.latest_inventory(user_id, migration_id, side))
                       for side in ('old', 'new')}
        ready = all(row and row['status'] == 'complete' for row in inventories.values())
        return envelope(migration['id'], status='succeeded' if ready else 'needs_input',
                        next_action='run_migration' if ready else 'provide_inventory', data={
            'summary': 'Both supplied inventories passed syntax and origin checks; full site coverage and reachability are not verified.' if ready else 'Provide complete old and new inventories; no network discovery has run.',
            'old_site': migration['old_origin'], 'new_site': migration['new_origin'],
            'name': migration.get('name'), 'site_aliases': migration.get('site_aliases', {}),
            'inventories': inventories,
        })

    def operation(self, user_id, operation_id, migration_id=None):
        operation = self.repository.get_operation(user_id, operation_id, migration_id)
        migration_id = operation['migration_id']
        if operation['kind'] == 'plan_migration':
            from .migration_discovery_workflow import migration_discovery_summary
            result = migration_discovery_summary(user_id, migration_id, repository=self.repository)
            result['operation_id'] = operation['id']
            return result
        if operation['kind'] == 'discover_inventory':
            from .migration_discovery_service import MigrationDiscoveryService
            return MigrationDiscoveryService(self.repository).get(user_id, migration_id, operation['id'])
        if operation['kind'] == 'resolve_matches':
            saved = operation.get('result') or {}
            outcomes = saved.get('outcomes', [])
            applied = sum(item.get('code') == 'ok' for item in outcomes)
            failed = len(outcomes) - applied
            return envelope(migration_id, operation['id'], status='partial' if failed else 'succeeded',
                next_action='resolve_matches', data={'run_id': saved.get('run_id'), 'outcomes': outcomes,
                    'applied': applied, 'not_applied': failed, 'replayed': True})
        if operation['kind'] == 'import_inventory':
            result = operation.get('result') or {}
            inventory = self.repository.get_inventory(user_id, migration_id, result.get('inventory_id'))
            partial = inventory['status'] != 'complete'
            return envelope(migration_id, operation['id'], status='partial' if partial else 'succeeded',
                            next_action='provide_inventory' if partial else self.get(user_id, migration_id)['next_action'],
                            data={'summary': ('Correct the original URL list using input_issues and import again with a new idempotency key; rejected rows were not silently accepted.' if partial else 'Supplied inventory passed syntax and origin checks; full site coverage and reachability are not verified.'), 'inventory': inventory_summary(inventory)})
        # Other packets own their result schemas; never expose arbitrary DB payloads.
        status = 'queued' if operation['status'] == 'reserved' else operation['status']
        response = envelope(migration_id, operation['id'], status=status,
                            next_action='poll' if status in ('queued', 'running') else 'none',
                            data={'kind': operation['kind']})
        if response['next_action'] == 'poll':
            response['retry_after_seconds'] = 10
        return response
