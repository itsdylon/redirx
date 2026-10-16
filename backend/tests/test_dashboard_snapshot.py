"""Historical dashboard reads across native SQL and a capped PostgREST transport."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from flask import Flask
from backend.tests import test_atomic_session_delete as fixture
from backend.tests.helpers.pivot_native_client import NativeClient, NativeQuery
from backend.tests.test_migration_planning import A, B, ROOT


class CappedQuery(NativeQuery):
    def execute(self):
        if not self.rpc_name and self.mode == 'select':
            self.max_rows = min(self.max_rows or 1000, 1000)
        return super().execute()


class CappedClient(NativeClient):
    def table(self, name): return CappedQuery(self, table=name)
    def rpc(self, name, params): return CappedQuery(self, rpc=name, params=params)


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'requires disposable loopback PostgreSQL')
class DashboardSnapshot(unittest.TestCase):
    cleanup_db = classmethod(fixture.AtomicSessionDelete.cleanup_db.__func__)
    sql = fixture.AtomicSessionDelete.sql

    @classmethod
    def setUpClass(cls):
        fixture.AtomicSessionDelete.setUpClass.__func__(cls)
        # Exact pre-032 columns from001/018, omitted by the minimal pivot fixture.
        cls.sql(cls, '''ALTER TABLE migration_sessions
            ADD COLUMN total_mappings INTEGER DEFAULT 0,
            ADD COLUMN approved_mappings INTEGER DEFAULT 0,
            ADD COLUMN is_tutorial BOOLEAN NOT NULL DEFAULT FALSE''')
        cls.sql(cls, 'CREATE INDEX idx_migration_sessions_user_id ON migration_sessions(user_id)')
        cls.sql(cls, (ROOT/'database/migrations/066_legacy_dashboard_snapshot.sql').read_text())

    def setUp(self):
        self.owner = str(uuid4())
        self.sql('INSERT INTO auth.users(id) VALUES(%s)', [self.owner])
        self.sql('INSERT INTO user_profiles(id) VALUES(%s)', [self.owner])
        from backend.routes.user_routes import user_blueprint
        self.app = Flask(__name__)
        self.app.config['RATELIMIT_ENABLED'] = False
        self.app.register_blueprint(user_blueprint, url_prefix='/api/user')
        self.native = CappedClient(self.dsn)

    def request(self):
        with patch('services.auth_service.AuthService.verify_token', return_value=SimpleNamespace(id=self.owner)), patch(
            'backend.routes.user_routes.MigrationSessionDB', return_value=SimpleNamespace(client=self.native)), patch(
            'backend.routes.user_routes.SupabaseClient.get_admin_client', return_value=self.native):
            return self.app.test_client().get('/api/user/dashboard', headers={'Authorization':'Bearer fixture-only'})

    def history(self, count, *, owner=None, tutorial=False, preview=False):
        return self.sql('''INSERT INTO migration_sessions(user_id,project_name,status,total_mappings,approved_mappings,is_tutorial,is_preview,old_urls,new_urls,created_at)
            SELECT %s,'history '||n,'completed',4,2,%s,%s,
             jsonb_build_array('https://old.example/'||repeat('x',4096)),
             jsonb_build_array('https://new.example/'||repeat('y',4096)),
             '2026-01-01'::timestamptz FROM generate_series(1,%s) n RETURNING id''',
            [owner or self.owner,tutorial,preview,count])

    def test_large_history_counts_all_rows_beyond_transport_cap(self):
        self.history(1205)
        self.sql('''INSERT INTO url_mappings(session_id,old_url,new_url,confidence_score,match_type)
           SELECT s.id,'https://old.example/'||v.kind,'https://new.example/'||v.kind,v.score,v.kind
           FROM migration_sessions s CROSS JOIN (VALUES ('exact_url',1.0),('high',0.85),('medium',0.65),('low',0.4)) v(kind,score)
           WHERE s.user_id=%s''', [self.owner])
        response = self.request()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['total_sessions'], 1205)
        self.assertEqual(response.json['total_redirects'], 4820)
        self.assertEqual(response.json['approval_progress'], 50)
        self.assertEqual(response.json['average_confidence'], 63.3)
        self.assertEqual(response.json['confidence_breakdown'], {'exact':1205,'high':1205,'medium':1205,'low':1205})
        self.assertEqual(len(response.json['recent_sessions']), 5)

    def test_empty_owner_has_positive_empty_snapshot(self):
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {'success':True,'total_sessions':0,'total_redirects':0,
            'approval_progress':0,'average_confidence':0,'confidence_breakdown':{'high':0,'medium':0,'low':0,'exact':0},'recent_sessions':[]})

    def test_owner_preview_tutorial_filters_and_stable_recent_ties(self):
        own = self.history(8)
        self.history(3, owner=B)
        self.history(2, tutorial=True)
        self.history(2, preview=True)
        # Actual MCP admission: these sessions were never excluded by dashboard.
        from backend.tests import test_migration_run_service as runs
        with patch.dict(os.environ, runs.ACTIVATION):
            f = runs.RunDatabaseAcceptance.fixture(self, owner=self.owner)
            run = self.runs.start_run(self.owner,f['migration'],f['old'],f['new'],f['quote'],uuid4().hex)
        self.sql('UPDATE migration_sessions SET total_mappings=4,approved_mappings=2 WHERE id=%s', [run['session_id']])
        response = self.request()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['total_sessions'], 9)
        self.assertEqual(response.json['total_redirects'], 36)
        expected = [run['session_id']] + sorted((str(row['id']) for row in own), reverse=True)[:4]
        self.assertEqual([row['id'] for row in response.json['recent_sessions']], expected)
        for row in response.json['recent_sessions']:
            self.assertEqual(set(row), {'id','project_name','created_at','total_mappings','approved_mappings','status','pipeline_type','source_session_id'})
        self.assertEqual(self.request().json, response.json)

    def test_null_historical_metrics_are_zero_without_changing_thresholds(self):
        sid = self.history(1)[0]['id']
        self.sql('UPDATE migration_sessions SET total_mappings=NULL,approved_mappings=NULL WHERE id=%s', [sid])
        self.sql('''INSERT INTO url_mappings(session_id,old_url,new_url,confidence_score,match_type)
          SELECT %s,'https://old.example/'||n,'https://new.example/'||n,score,kind
          FROM (VALUES(1,0.85,'semantic'),(2,0.65,'semantic'),(3,0.649,'semantic'),
           (4,NULL::double precision,NULL::text),(5,NULL::double precision,'exact_url')) v(n,score,kind)''',[sid])
        data=self.request().json
        self.assertEqual(data['total_redirects'],0)
        self.assertEqual(data['approval_progress'],0)
        self.assertEqual(data['confidence_breakdown'],{'exact':1,'high':1,'medium':1,'low':2})
        self.assertEqual(data['average_confidence'],round((0.85+0.65+0.649)/4*100,1))

    def test_sql_snapshot_does_not_mix_a_concurrent_insert_into_totals(self):
        import time
        from concurrent.futures import ThreadPoolExecutor
        import psycopg
        self.history(1)
        key = 640064
        with psycopg.connect(self.dsn, autocommit=True) as gate:
            gate.execute('SELECT pg_advisory_lock(%s)', [key])
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.sql, '''SELECT legacy_dashboard_snapshot(%s) AS snapshot
                    FROM (SELECT pg_advisory_xact_lock(%s)) AS blocked_dashboard''', [self.owner,key])
                try:
                    deadline=time.monotonic()+5
                    while time.monotonic()<deadline:
                        waiting=self.sql("SELECT count(*) AS n FROM pg_stat_activity WHERE datname=current_database() AND wait_event='advisory'")[0]['n']
                        if waiting: break
                        time.sleep(0.01)
                    self.assertTrue(waiting,'fixture must observe blocked reader before inserting')
                    self.history(1)
                finally:
                    gate.execute('SELECT pg_advisory_unlock(%s)', [key])
                snapshot=pending.result(timeout=5)[0]['snapshot']
        self.assertEqual(snapshot['total_sessions'],1)
        self.assertEqual(len(snapshot['recent_sessions']),1)
        self.assertEqual(self.request().json['total_sessions'],2)

    def test_only_service_role_can_read_snapshot(self):
        for role in ('anon','authenticated'):
            self.assertFalse(self.sql("SELECT has_function_privilege(%s,'public.legacy_dashboard_snapshot(uuid)','EXECUTE') AS allowed", [role])[0]['allowed'])
        self.assertTrue(self.sql("SELECT has_function_privilege('service_role','public.legacy_dashboard_snapshot(uuid)','EXECUTE') AS allowed")[0]['allowed'])
