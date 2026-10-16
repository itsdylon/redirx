"""Pure validation plus opt-in HTTP/SQL acceptance against a disposable local DB.

Set PREFLIGHT_TEST_DATABASE_URL to a local PostgreSQL admin database. The suite
creates its own random database, applies only fixtures, and removes it afterward.
Never accepts non-loopback database hosts. No production credentials are needed.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from flask import Flask
from backend.services.migration_planning_service import MigrationPlanningService, validate_plan
from backend.services.migration_repository import InvalidInputError, MigrationRepository
from backend.routes.v2_routes import v2_blueprint

ROOT = Path(__file__).resolve().parents[2]
A, B = '10000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000002'
PLAN = {'old_site': 'https://old.example', 'new_site': 'http://192.168.1.15:8080',
        'idempotency_key': 'plan', 'site_aliases': {'new': ['https://staging.internal']}}


class ValidationTests(unittest.TestCase):
    def test_private_staging_is_syntactically_valid_without_network(self):
        key, result = validate_plan(PLAN)
        self.assertEqual(result['new_site'], 'http://192.168.1.15:8080')
        self.assertEqual(result['site_aliases'], {'old': [], 'new': ['https://staging.internal']})

    def test_app_factory_flag_defaults_off_and_explicitly_enables_pivot_routes(self):
        from backend.app import create_app
        with patch.dict(os.environ):
            os.environ.pop('MCP_PIVOT_ENABLED', None)
            app = create_app()
            self.assertFalse(any(r.rule.startswith('/api/v2') for r in app.url_map.iter_rules()))
            os.environ['MCP_PIVOT_ENABLED'] = 'true'
            app = create_app()
            paths = {r.rule for r in app.url_map.iter_rules()}
            self.assertTrue({'/api/v2/migrations', '/api/v2/migrations/<migration_id>/runs',
                '/api/v2/migrations/<migration_id>/quotes/<quote_id>/checkout',
                '/api/v2/billing/stripe-test/webhook',
                '/api/v2/connections/search-console/actions'} <= paths)

    def test_invalid_shape_and_ambiguous_origins(self):
        for changes in ({'old_site': 'https://u:p@old.example'}, {'old_site': 'https://old.example/a'},
                        {'new_site': 'ftp://example.com'}, {'name': 1}, {'idempotency_key': 'x\n'},
                        {'idempotency_key': 'x' * 201}, {'inventories': {}},
                        {'site_aliases': ['https://evil.example']}, {'site_aliases': {'old': 'bad'}}):
            with self.subTest(changes=changes), self.assertRaises(InvalidInputError):
                validate_plan({**PLAN, **changes})


class Query:
    """Tiny PostgREST transport adapter; executes real PostgreSQL, not fake state."""
    def __init__(self, client, table=None, rpc=None, params=None):
        self.client, self.table_name, self.rpc_name, self.params = client, table, rpc, params
        self.filters, self.orders, self.max_rows = [], [], None
        self.columns = "*"
    def select(self, columns):
        self.columns = columns
        return self
    def eq(self, key, value):
        self.filters.append((key, value)); return self
    def order(self, key, desc=False):
        self.orders.append((key, desc)); return self
    def limit(self, count):
        self.max_rows = count; return self
    def execute(self):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        with psycopg.connect(self.client.dsn, row_factory=dict_row) as conn:
            conn.execute('SET ROLE service_role')
            if self.rpc_name:
                args = list(self.params.values())
                args = [psycopg.types.json.Jsonb(v) if isinstance(v, dict) else v for v in args]
                stmt = sql.SQL('SELECT {}({}) AS result').format(sql.Identifier(self.rpc_name),
                           sql.SQL(',').join(sql.Placeholder() for _ in args))
                try:
                    data = conn.execute(stmt, args).fetchone()['result']
                except psycopg.Error as exc:
                    # Match the PostgREST error boundary without changing SQL errors.
                    exc.code, exc.message = exc.sqlstate, exc.diag.message_primary
                    raise
            else:
                fields = sql.SQL('*') if self.columns == '*' else sql.SQL(',').join(
                    sql.Identifier(name.strip()) for name in self.columns.split(','))
                stmt = sql.SQL('SELECT {} FROM {}').format(fields, sql.Identifier(self.table_name))
                args = []
                if self.filters:
                    def column(name):
                        if '->>' in name:
                            field, key = name.split('->>', 1)
                            return sql.SQL('{}->>{}').format(sql.Identifier(field), sql.Literal(key))
                        return sql.Identifier(name)
                    stmt += sql.SQL(' WHERE ') + sql.SQL(' AND ').join(
                        sql.SQL('{} = %s').format(column(k)) for k, _ in self.filters)
                    args = [v for _, v in self.filters]
                if self.orders:
                    stmt += sql.SQL(' ORDER BY ') + sql.SQL(',').join(
                        sql.SQL('{} {}').format(sql.Identifier(k), sql.SQL('DESC' if desc else 'ASC'))
                        for k, desc in self.orders)
                if self.max_rows:
                    stmt += sql.SQL(' LIMIT %s'); args.append(self.max_rows)
                data = conn.execute(stmt, args).fetchall()
            return SimpleNamespace(data=data, error=None)


class DBClient:
    def __init__(self, dsn): self.dsn = dsn
    def table(self, name): return Query(self, table=name)
    def rpc(self, name, params): return Query(self, rpc=name, params=params)


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'requires explicit disposable local PostgreSQL')
class DatabaseHTTPAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The current status route reads run/quote authority through migration 037.
        # Reuse the queue fixture instead of maintaining a second partial schema.
        from backend.tests.test_migration_run_service import RunDatabaseAcceptance
        RunDatabaseAcceptance.setUpClass.__func__(cls)
        cls.repository = MigrationRepository(DBClient(cls.dsn))

    @classmethod
    def cleanup_db(cls):
        import psycopg
        from psycopg import sql
        with psycopg.connect(cls.admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(cls.database)))

    def setUp(self):
        activation = patch.dict(os.environ, {"MCP_PIVOT_ENABLED": "true"})
        activation.start(); self.addCleanup(activation.stop)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, MAX_CONTENT_LENGTH=2_000_000, RATELIMIT_ENABLED=True)
        self.app.register_blueprint(v2_blueprint, url_prefix='/api/v2')
        from backend.extensions import limiter
        self.addCleanup(setattr, limiter, "enabled", limiter.enabled)
        limiter.enabled = False
        self.http = self.app.test_client()
        self.patches = [
            patch('backend.routes.v2_routes.MigrationPlanningService', side_effect=lambda: MigrationPlanningService(self.repository)),
            patch('backend.routes.v2_routes.ApiKeyService.resolve', side_effect=lambda t: A if t == 'rdx_test_A' else B if t == 'rdx_test_B' else None),
            patch('backend.routes.v2_routes.MCPDelegationService.resolve', side_effect=lambda t: A if t == 'delegation-A' else None),
        ]
        for p in self.patches: p.start(); self.addCleanup(p.stop)
        self.key = uuid4().hex

    def post(self, path, data, user='rdx_test_A'):
        return self.http.post('/api/v2' + path, json=data, headers={'Authorization': 'Bearer ' + user})
    def get(self, path, user='rdx_test_A'):
        return self.http.get('/api/v2' + path, headers={'Authorization': 'Bearer ' + user})
    def plan(self, key=None):
        response = self.post('/migrations', {**PLAN, 'idempotency_key': key or self.key})
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()
    def sql(self, statement, args=()):
        import psycopg
        with psycopg.connect(self.dsn) as conn:
            return conn.execute(statement, args).fetchall()

    def test_http_persists_replays_and_conflicts_without_orphans(self):
        first = self.plan()
        self.assertEqual((first['status'], first['next_action']), ('needs_input', 'provide_inventory'))
        self.assertEqual(first['data']['inventories'], {'old': None, 'new': None})
        self.assertEqual(first['migration_id'], self.plan()['migration_id'])
        before = self.sql('SELECT count(*) FROM migration_records')
        response = self.post('/migrations', {**PLAN, 'idempotency_key': self.key, 'name': 'changed'})
        self.assertEqual((response.status_code, response.json['error']['code']), (409, 'operation_conflict'))
        self.assertEqual(before, self.sql('SELECT count(*) FROM migration_records'))
        self.assertEqual(self.get('/migrations/' + first['migration_id']).json['migration_id'], first['migration_id'])
        self.assertEqual(self.get('/operations/' + first['operation_id']).json['status'], 'needs_input')

    def test_auth_ownership_and_malformed_http(self):
        first = self.plan()
        for url in ('/migrations/' + first['migration_id'], '/operations/' + first['operation_id']):
            self.assertEqual(self.get(url, 'rdx_test_B').status_code, 404)
            self.assertEqual(self.get(url, 'bad-token').status_code, 401)
            self.assertEqual(self.get(url, 'delegation-A').status_code, 200)
        self.assertEqual(self.http.get('/api/v2/migrations/' + first['migration_id']).status_code, 401)
        response = self.post('/migrations/' + first['migration_id'] + '/inventories',
                             {'side':'old','rows':['https://old.example/a'],'idempotency_key':uuid4().hex}, 'rdx_test_B')
        self.assertEqual(response.status_code, 404)
        for invalid in (None, [], {}, {**PLAN, 'old_site': 'https://a/path'}, {**PLAN, 'user_id': B}):
            self.assertEqual(self.post('/migrations', invalid).status_code, 400)
        self.assertEqual(self.get('/migrations/not-a-uuid').status_code, 400)
        self.assertEqual(self.get('/operations/not-a-uuid').status_code, 400)
        malformed = self.http.post('/api/v2/migrations', data='{', content_type='application/json', headers={'Authorization':'Bearer rdx_test_A'})
        self.assertEqual(malformed.json['error']['code'], 'invalid_input')

    def test_explicit_import_partial_alias_scope_and_recovery(self):
        first = self.plan(); mid = first['migration_id']
        url = '/migrations/' + mid + '/inventories'
        old = {'side':'old','rows':['https://old.example/Page?q=A', 'https://unrelated.example/a'], 'idempotency_key':uuid4().hex}
        partial = self.post(url, old)
        self.assertEqual(partial.status_code, 200, partial.json)
        self.assertEqual(partial.json['status'], 'partial')
        inventory = partial.json['data']['inventory']
        self.assertEqual(inventory['page_count'], 1)
        self.assertFalse(inventory['coverage']['complete'])
        self.assertFalse(inventory['coverage']['network_checked'])
        self.assertNotIn('exclusions', inventory)
        self.assertEqual(inventory['input_issues']['total_excluded'], 1)
        self.assertEqual(inventory['input_issues']['examples'], [
            {'code': 'undeclared_origin', 'url_hint': 'https://unrelated.example/a'}])
        self.assertTrue(inventory['input_issues']['diagnostics_complete'])
        self.assertEqual(self.post(url, old).json['data']['inventory']['id'], inventory['id'])
        conflict = self.post(url, {**old, 'rows':['https://old.example/different']})
        self.assertEqual(conflict.status_code, 409)
        forbidden = self.post(url, {**old, 'host_aliases':['https://unrelated.example']})
        self.assertEqual(forbidden.status_code, 400)
        new = {'side':'new', 'rows':['http://192.168.1.15:8080/Page?q=A', 'https://staging.internal/Other'], 'idempotency_key':uuid4().hex}
        response = self.post(url, new)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['data']['inventory']['page_count'], 2)
        self.assertEqual(self.get('/migrations/' + mid).json['status'], 'needs_input')
        old.update(rows=['https://old.example/Page?q=A'], idempotency_key=uuid4().hex)
        self.assertEqual(self.post(url, old).status_code, 200)
        ready = self.get('/migrations/' + mid).json
        self.assertEqual((ready['status'], ready['next_action']), ('succeeded','run_migration'))
        self.assertEqual(self.get('/operations/' + first['operation_id']).json['status'], 'succeeded')
        self.assertEqual(self.sql('SELECT status FROM migration_operations WHERE id=%s', [first['operation_id']]), [('succeeded',)])
        self.assertTrue(ready['data']['inventories']['old']['completed_at'].endswith('Z'))
        self.assertEqual(self.get('/operations/' + partial.json['operation_id']).json['status'], 'partial')
        self.assertEqual(self.sql('SELECT count(*) FROM migration_sessions'), [(0,)])
        old.update(rows=[], idempotency_key=uuid4().hex)
        self.assertEqual(self.post(url, old).json['status'], 'partial')
        self.assertEqual(self.get('/migrations/' + mid).json['status'], 'needs_input')
        self.assertEqual(self.sql('SELECT status FROM migration_operations WHERE id=%s', [first['operation_id']]), [('needs_input',)])

    def test_capacity_and_database_errors_have_safe_envelopes(self):
        first = self.plan()
        response = self.post('/migrations/' + first['migration_id'] + '/inventories', {
            'side': 'old', 'rows': ['https://old.example/a'] * 50_001,
            'idempotency_key': uuid4().hex,
        })
        self.assertEqual((response.status_code, response.json['error']['code']), (413, 'capacity_exceeded'))
        self.assertEqual(self.sql('SELECT count(*) FROM inventory_snapshots WHERE migration_id=%s', [first['migration_id']]), [(0,)])
        from backend.services.migration_repository import RepositoryUnavailableError
        with patch.object(self.repository, 'get_migration', side_effect=RepositoryUnavailableError('Migration data is temporarily unavailable.')):
            response = self.get('/migrations/' + first['migration_id'])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json['error']['code'], 'internal_error')
        self.assertTrue(response.json['error']['retryable'])
        self.assertEqual(response.json['next_action'], 'retry')

    def test_real_connections_race_same_and_changed_requests(self):
        key, payload = validate_plan({**PLAN, 'idempotency_key':self.key})
        from threading import Barrier
        barrier = Barrier(8)
        def create(i):
            barrier.wait()
            return self.repository._execute(self.repository.client.rpc('plan_migration', {
                'p_user_id':A, 'p_idempotency_key':key, 'p_request':payload})).data
        before = self.sql('SELECT count(*) FROM migration_records')[0][0]
        with ThreadPoolExecutor(max_workers=8) as pool: results = list(pool.map(create, range(8)))
        self.assertEqual(len({r['migration_id'] for r in results}), 1)
        self.assertEqual(sum(not r['replayed'] for r in results), 1)
        self.assertEqual(self.sql('SELECT count(*) FROM migration_records')[0][0], before + 1)
        barrier = Barrier(2)
        key += '-conflict'
        def changed(i):
            barrier.wait()
            try:
                return self.repository._execute(self.repository.client.rpc('plan_migration', {
                    'p_user_id': A, 'p_idempotency_key': key, 'p_request': {**payload,'name':str(i)}})).data
            except Exception as exc: return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(changed, range(2)))
        self.assertEqual(sum(r == 'operation_conflict' for r in results), 1)
        self.assertEqual(self.sql('SELECT count(*) FROM migration_records')[0][0], before + 2)

    def test_sql_role_boundaries_account_key_scope_and_invalid_rollback(self):
        import psycopg
        key, payload = validate_plan({**PLAN, 'idempotency_key':self.key})
        for role in ('anon', 'authenticated'):
            with psycopg.connect(self.dsn) as conn:
                conn.execute('SET ROLE ' + role)
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    conn.execute('SELECT plan_migration(%s,%s,%s)', [A,key,psycopg.types.json.Jsonb(payload)])
        before = self.sql('SELECT count(*) FROM migration_records')[0][0]
        for owner in (A,B):
            self.repository._execute(self.repository.client.rpc('plan_migration', {'p_user_id':owner,'p_idempotency_key':key,'p_request':payload}))
        self.assertEqual(self.sql('SELECT count(*) FROM migration_records')[0][0], before + 2)
        for invalid in ({}, {**payload,'old_site':'https://u:p@host'}, {**payload,'site_aliases':{'old':[]}},
                        {**payload,'site_aliases':{'old':[None],'new':[]}}):
            with self.assertRaises(InvalidInputError):
                self.repository._execute(self.repository.client.rpc('plan_migration', {'p_user_id':A,'p_idempotency_key':key+'bad','p_request':invalid}))
        self.assertEqual(self.sql('SELECT count(*) FROM migration_records')[0][0], before + 2)
        # Additive SQL is repeatable and leaves the original identity intact.
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute((ROOT / 'database/migrations/035_atomic_migration_planning.sql').read_text())

    def test_rate_limit_is_shared_by_account_and_returns_timing(self):
        self.app.config['RATELIMIT_ENABLED'] = True
        from backend.extensions import limiter
        limiter.enabled = True
        limiter.reset()
        try:
            response = None
            for index in range(31):
                response = self.post('/migrations', {**PLAN,'idempotency_key':self.key},
                                     'rdx_test_A' if index % 2 else 'delegation-A')
            self.assertEqual(response.status_code, 429, response.json)
            self.assertEqual(response.json['error']['code'], 'rate_limited')
            self.assertGreater(response.json['retry_after_seconds'], 0)
            self.assertIn('Retry-After', response.headers)
        finally:
            limiter.enabled = False
            limiter.reset()


if __name__ == '__main__': unittest.main()
