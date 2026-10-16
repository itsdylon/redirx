"""Opt-in native PostgreSQL run/queue acceptance, plus worker dispatch guards.

Uses the same disposable loopback-only database harness as planning acceptance.
No matching quality, external network, Stripe or full-capacity claim is made.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from threading import Barrier
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
from backend.tests.helpers.disposable_postgres import validate_disposable_postgres_dsn

from backend.services.migration_run_service import MigrationRunService
from backend.services.migration_quote_service import MigrationQuoteService, PaymentRequiredError, QuoteNotReadyError
from backend.services.migration_repository import MigrationRepository, OperationConflictError, MigrationNotFoundError
from backend.tests.test_migration_planning import DBClient, A, B, ROOT

ACTIVATION = {'MCP_PIVOT_ENABLED':'true','MCP_PIVOT_ACTIVATION':'test_only'}


class ActivationTests(unittest.TestCase):
    def test_disabled_and_non_test_activation_fail_before_rpc(self):
        repo=Mock()
        for env in ({'MCP_PIVOT_ENABLED':'false','MCP_PIVOT_ACTIVATION':'test_only'},
                    {'MCP_PIVOT_ENABLED':'true','MCP_PIVOT_ACTIVATION':'live'},
                    {'MCP_PIVOT_ENABLED':'true','MCP_PIVOT_ACTIVATION':''}):
            with patch.dict(os.environ,env), self.assertRaises(QuoteNotReadyError):
                MigrationRunService(repo).start_run(A,*([str(uuid4())]*4),'key')
        repo.client.rpc.assert_not_called()


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'requires explicit disposable local PostgreSQL')
class RunDatabaseAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        cls.admin_dsn=os.environ['PREFLIGHT_TEST_DATABASE_URL']
        validate_disposable_postgres_dsn(cls.admin_dsn)
        parsed=urlsplit(cls.admin_dsn)
        cls.database='redirx_run_test_'+uuid4().hex
        with psycopg.connect(cls.admin_dsn,autocommit=True) as conn:
            conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(cls.database)))
        cls.dsn=urlunsplit(parsed._replace(path='/'+cls.database))
        cls.addClassCleanup(cls.cleanup_db)
        with psycopg.connect(cls.dsn,autocommit=True) as conn:
            fixture=(ROOT/'database/tests/legacy-fixture.sql').read_text()
            for role in ('anon','authenticated','service_role'):
                if conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',[role]).fetchone():
                    fixture=fixture.replace(f'CREATE ROLE {role} NOLOGIN'+(' BYPASSRLS' if role=='service_role' else '')+';','')
            conn.execute(fixture)
            # Earlier queue/progress shape, before applying the real027 claim SQL.
            conn.execute('''ALTER TABLE migration_sessions ADD COLUMN locked_at timestamptz,
              ADD COLUMN locked_by text, ADD COLUMN lease_expires_at timestamptz,
              ADD COLUMN attempt_count integer DEFAULT 0, ADD COLUMN last_error text,
              ADD COLUMN current_stage integer, ADD COLUMN stage_name text, ADD COLUMN total_stages integer,
              ADD COLUMN is_preview boolean NOT NULL DEFAULT false''')
            for name in ('006_add_idempotency_keys.sql','009_add_pipeline_type.sql',
                         '019_auth_user_delete_cleanup.sql','026_add_traffic_baseline_and_url_sources.sql',
                         '027_add_job_timing_and_priority.sql','031_add_account_usage_events.sql',
                         '032_durable_migrations.sql','034_atomic_inventory_import.sql',
                         '035_atomic_migration_planning.sql','036_migration_quotes_grants.sql',
                         '037_entitled_migration_runs.sql'):
                conn.execute((ROOT/'database/migrations'/name).read_text())
            reclaim=(ROOT/'database/migrations/005_add_lease_columns_fixed_v2.sql').read_text()
            reclaim=reclaim[reclaim.index('CREATE FUNCTION reclaim_expired_leases('):]
            reclaim=reclaim[:reclaim.index('$$ LANGUAGE plpgsql;')+len('$$ LANGUAGE plpgsql;')]
            conn.execute(reclaim)
            conn.execute('INSERT INTO auth.users(id) VALUES (%s),(%s)',[A,B])
            conn.execute('INSERT INTO user_profiles(id) VALUES (%s),(%s)',[A,B])
        cls.repo=MigrationRepository(DBClient(cls.dsn))
        cls.runs=MigrationRunService(cls.repo)
        cls.quotes=MigrationQuoteService(cls.repo)

    @classmethod
    def cleanup_db(cls):
        import psycopg
        from psycopg import sql
        with psycopg.connect(cls.admin_dsn,autocommit=True) as conn:
            conn.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(cls.database)))

    def setUp(self):
        self.activation=patch.dict(os.environ,ACTIVATION); self.activation.start(); self.addCleanup(self.activation.stop)
        # Keep each test's authoritative queue isolated without deleting history.
        self.sql("UPDATE migration_sessions SET status='permanently_failed' WHERE status IN ('pending','processing')")

    def sql(self, statement, args=None):
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(self.dsn,row_factory=dict_row) as conn:
            cur=conn.execute(statement,args)
            return cur.fetchall() if cur.description else []

    def fixture(self, count=2, owner=A):
        from backend.services.migration_planning_service import MigrationPlanningService
        from backend.services.inventory_import_service import InventoryImportService
        plan=MigrationPlanningService(self.repo).plan(owner, {'old_site':'https://old.example',
            'new_site':'https://new.example','idempotency_key':uuid4().hex})
        f={'user':owner,'migration':plan['migration_id']}
        for side, n in [('old',count),('new',2)]:
            rows=[{'url':f'https://{side}.example/Page/{i}?q=A','provenance':['csv','sitemap']} for i in range(n)]
            if side=='old': rows.append({'url':'https://old.example/Page/0?q=A#part','provenance':['gsc']})
            imported=InventoryImportService(self.repo).import_inventory(owner,f['migration'],side,rows,uuid4().hex)
            f[side]=imported['inventory_id']
        f['quote']=self.quotes.create_quote(owner,f['migration'],f['old'],f['new'],uuid4().hex)['quote_id']
        return f

    def start(self,f,key=None,**kw):
        return self.runs.start_run(f['user'],f['migration'],f['old'],f['new'],f['quote'],key or uuid4().hex,**kw)

    def pay(self,f):
        q=self.quotes.get_quote(f['user'],f['migration'],f['quote']); suffix=uuid4().hex
        return self.quotes.record_verified_test_payment(f['user'],f['migration'],f['quote'],
          stripe_session_id='cs_test_'+suffix,stripe_payment_intent_id='pi_'+suffix,stripe_event_id='evt_'+suffix,
          amount_cents=q['amount_cents'],currency='usd',livemode=False)

    def claim(self):
        rows=self.sql("SELECT * FROM claim_next_job('worker-test',now()+interval '10 minutes')")
        self.assertEqual(len(rows),1)
        job=rows[0]; job['id']=str(job['id']); job['mcp_run_id']=str(job['mcp_run_id']) if job['mcp_run_id'] else None
        return job

    def test_free_queue_bridge_preserves_originals_provenance_and_retries(self):
        f=self.fixture(); key=uuid4().hex; run=self.start(f,key)
        self.assertEqual(run['status'],'queued'); self.assertFalse(run['replayed'])
        replay=self.start(f,key); self.assertEqual(replay['run_id'],run['run_id']); self.assertTrue(replay['replayed'])
        session=self.sql('SELECT * FROM migration_sessions WHERE id=%s',[run['session_id']])[0]
        self.assertEqual(session['pipeline_type'],'content'); self.assertFalse(session['is_preview'])
        self.assertEqual(len(session['old_urls']),3); self.assertIn('https://old.example/Page/0?q=A#part',session['old_urls'])
        stored=self.sql('SELECT side,url,count_key,sources FROM session_discovered_urls WHERE session_id=%s ORDER BY side,url',[run['session_id']])
        original=self.sql('SELECT side,url,count_key,sources FROM session_discovered_urls WHERE inventory_id IN (%s,%s) ORDER BY side,url',[f['old'],f['new']])
        self.assertEqual(stored,original)
        job=self.claim(); self.assertEqual(job['mcp_run_id'],run['run_id'])
        self.runs.authorize_dispatch(job,'worker-test')
        self.runs.finalize_session(job,'worker-test','pending','temporary failure')
        self.assertEqual(self.start(f,key)['status'],'queued')
        again=self.claim(); self.assertEqual(again['attempt_count'],2)
        with self.assertRaises(OperationConflictError): self.runs.finalize_session(job,'worker-test','completed')
        self.runs.authorize_dispatch(again,'worker-test')
        self.assertEqual(self.runs.finalize_session(again,'worker-test','completed')['status'],'succeeded')
        self.assertEqual(self.start(f,key)['run_id'],run['run_id'])
        rerun=self.start(f,rerun_of=run['run_id']); self.assertNotEqual(rerun['run_id'],run['run_id'])
        self.assertEqual(self.sql('SELECT count(*) AS n FROM migration_purchase_grants WHERE quote_id=%s',[f['quote']])[0]['n'],1)

    def test_http_automatic_quote_payment_resume_ownership_and_retry(self):
        from flask import Flask, request
        from backend.routes.v2_routes import v2_blueprint
        app = Flask(__name__)
        app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
        app.register_blueprint(v2_blueprint, url_prefix='/api/v2')
        http = app.test_client()
        def authenticated_owner():
            request.api_user_id = A
            return A
        f = self.fixture(501)
        path = '/api/v2/migrations/' + f['migration'] + '/runs'
        body = {'inventory_ids': {'old': f['old'], 'new': f['new']}, 'idempotency_key': uuid4().hex}
        with patch('backend.routes.v2_routes.resolve_authorization', return_value=None):
            self.assertEqual(http.post(path, json=body).status_code, 401)
        # This 037-only database isolates purchase runs; the real 049 selector is
        # exercised against the complete schema by test_studio_run_http.
        with patch('backend.services.migration_subscription_service.MigrationSubscriptionService.select_run_subscription',
                   return_value={'use_studio': False, 'subscription_id': None,
                                 'reason': 'no_subscription', 'next_action': 'complete_payment'}), \
             patch('backend.routes.v2_routes.resolve_authorization', side_effect=authenticated_owner), \
             patch('backend.routes.v2_routes.MigrationQuoteService', return_value=self.quotes), \
             patch('backend.routes.v2_routes.MigrationRunService', return_value=self.runs):
            for invalid in ({**body, 'paid': True}, {**body, 'inventory_ids': {'old': f['old']}},
                            {**body, 'idempotency_key': None}):
                self.assertEqual(http.post(path, json=invalid).status_code, 400)
            first = http.post(path, json=body)
            self.assertEqual(first.status_code, 200, first.json)
            self.assertEqual((first.json['status'], first.json['next_action']), ('payment_required', 'complete_payment'))
            self.assertIsNone(first.json['data']['session_id'])
            replay = http.post(path + '?payment_return=success&paid=true', json=body).json
            self.assertEqual(replay['operation_id'], first.json['operation_id'])
            self.assertEqual(replay['data']['quote_id'], first.json['data']['quote_id'])
            self.assertEqual(replay['status'], 'payment_required')
            f['quote'] = first.json['data']['quote_id']
            self.pay(f)
            resumed = http.post(path, json=body).json
            self.assertEqual((resumed['status'], resumed['next_action']), ('queued', 'poll'))
            self.assertEqual(resumed['operation_id'], first.json['operation_id'])
            repeated = http.post(path, json={**body, 'quote_id': f['quote']}).json
            self.assertEqual(repeated['data']['run_id'], resumed['data']['run_id'])
            self.assertEqual(self.sql('SELECT count(*) AS n FROM migration_runs WHERE migration_id=%s', [f['migration']])[0]['n'], 1)
            foreign = self.fixture(owner=B)
            denied = http.post('/api/v2/migrations/' + foreign['migration'] + '/runs', json={
                'inventory_ids': {'old': foreign['old'], 'new': foreign['new']}, 'idempotency_key': uuid4().hex})
            self.assertEqual(denied.status_code, 404)

    def test_paid_reserves_before_payment_and_same_operation_resumes_once(self):
        f=self.fixture(501); key=uuid4().hex
        pending=self.start(f,key)
        self.assertEqual(pending['status'],'payment_required'); self.assertIsNone(pending['run_id']); self.assertIsNone(pending['session_id'])
        self.assertEqual(self.start(f,key)['operation_id'],pending['operation_id'])
        self.assertEqual(self.sql('SELECT count(*) AS n FROM migration_runs WHERE migration_id=%s',[f['migration']])[0]['n'],0)
        grant=self.pay(f)
        queued=self.start(f,key)
        self.assertEqual(queued['operation_id'],pending['operation_id']); self.assertEqual(queued['grant_id'],grant['grant_id'])
        job=self.claim()
        with self.assertRaises(QuoteNotReadyError): self.runs.finalize_session(job,'worker-test','completed')
        self.runs.authorize_dispatch(job,'worker-test')
        self.runs.finalize_session(job,'worker-test','completed')
        first=self.quotes.get_grant(A,f['migration'],grant['grant_id'])
        completed=self.sql('SELECT completed_at FROM migration_sessions WHERE id=%s',[queued['session_id']])[0]['completed_at']
        self.assertEqual(datetime.fromisoformat(first['first_successful_paid_run_at']),completed)
        self.assertEqual(self.quotes.record_success(A,grant['grant_id'],queued['run_id']),first)
        other_quote=self.quotes.create_quote(A,f['migration'],f['old'],f['new'],uuid4().hex)['quote_id']
        other_grant=self.pay({**f,'quote':other_quote})
        with self.assertRaises(OperationConflictError):
            self.quotes.record_success(A,other_grant['grant_id'],queued['run_id'])
        self.runs.finalize_session(job,'worker-test','completed')
        rerun=self.start(f,rerun_of=queued['run_id']); job2=self.claim()
        self.runs.authorize_dispatch(job2,'worker-test'); self.runs.finalize_session(job2,'worker-test','completed')
        self.assertEqual(self.quotes.get_grant(A,f['migration'],grant['grant_id']),first)

    def test_concurrent_grant_issuance_and_reservation_wait_without_deadlock(self):
        import psycopg
        import time
        f=self.fixture()
        with ThreadPoolExecutor(max_workers=1) as pool, psycopg.connect(self.dsn) as holder:
            holder.execute('SELECT id FROM migration_price_quotes WHERE id=%s FOR UPDATE',[f['quote']])
            pending=pool.submit(self.start,f)
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                waiting=self.sql("""SELECT count(*) AS n FROM pg_stat_activity
                  WHERE datname=current_database() AND wait_event_type='Lock'
                    AND query LIKE 'SELECT "reserve_migration_run"%'""")
                if waiting[0]['n']>0: break
                time.sleep(0.01)
            else: self.fail('Did not observe the reservation waiting on its locked quote')
            # This needs a migration KEY SHARE while the waiting reservation
            # owns NO KEY UPDATE. An exclusive parent lock would deadlock here.
            grant=holder.execute('SELECT issue_free_migration_grant(%s,%s,%s)',[A,f['migration'],f['quote']]).fetchone()[0]
            holder.commit()
            run=pending.result(timeout=3)
        self.assertEqual(run['grant_id'],grant['grant_id'])
        self.assertEqual(run['status'],'queued')

    def test_real_connections_concurrent_free_and_paid_retries(self):
        for count in (2,501):
            f=self.fixture(count); key=uuid4().hex
            if count>500:
                pending=self.start(f,key); self.pay(f)
            barrier=Barrier(6)
            def start(_): barrier.wait(); return self.start(f,key)
            with ThreadPoolExecutor(max_workers=6) as pool: values=list(pool.map(start,range(6)))
            self.assertEqual(len({r['run_id'] for r in values}),1)
            self.assertEqual(len({r['session_id'] for r in values}),1)
            self.assertEqual(len({r['operation_id'] for r in values}),1)
            if count>500: self.assertEqual(values[0]['operation_id'],pending['operation_id'])
            self.assertEqual(self.sql('SELECT count(*) AS n FROM migration_runs WHERE migration_id=%s',[f['migration']])[0]['n'],1)

    def test_owner_input_conflicts_and_revoked_dispatch_fail_closed(self):
        f=self.fixture(); key=uuid4().hex; run=self.start(f,key)
        with self.assertRaises(MigrationNotFoundError): self.start({**f,'user':B},key)
        other=self.fixture()
        with self.assertRaises(OperationConflictError): self.start({**f,'old':other['old']},key)
        with self.assertRaises(OperationConflictError): self.start(f,key,rerun_of=run['run_id'])
        job=self.claim()
        with patch.dict(os.environ,{'MCP_PIVOT_ENABLED':'false'}), self.assertRaises(QuoteNotReadyError):
            self.runs.authorize_dispatch(job,'worker-test')
        self.sql("UPDATE migration_purchase_grants SET state='revoked' WHERE id=%s",[run['grant_id']])
        with self.assertRaises(PaymentRequiredError): self.runs.authorize_dispatch(job,'worker-test')
        self.runs.finalize_session(job,'worker-test','permanently_failed','grant unavailable')
        self.assertEqual(self.start(f,key)['status'],'failed')
        self.assertIsNone(self.sql('SELECT dispatch_authorized_at FROM migration_runs WHERE id=%s',[run['run_id']])[0]['dispatch_authorized_at'])

    def test_capacity_precedes_payment_and_rpc_roles_are_restricted(self):
        import psycopg
        from backend.services.inventory_import_service import ImportCapacityExceededError
        f=self.fixture(501)
        with patch('backend.services.migration_run_service.PIVOT_CONTENT_MAX_OLD_URLS',500), self.assertRaises(ImportCapacityExceededError):
            self.start(f)
        self.assertEqual(self.sql("SELECT count(*) AS n FROM migration_operations WHERE migration_id=%s AND kind='run_migration'",[f['migration']])[0]['n'],0)
        for role in ('anon','authenticated'):
            with psycopg.connect(self.dsn) as conn:
                conn.execute('SET ROLE '+role)
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    conn.execute("SELECT reserve_migration_run(%s,%s,%s,%s,%s,%s,p_activation=>'test_only')",
                                 [A,f['migration'],f['old'],f['new'],f['quote'],uuid4().hex])
        with self.assertRaises(OperationConflictError): self.start(f,grant_id=str(uuid4()))

    def test_expired_grant_blocks_fresh_reservation_and_queued_dispatch(self):
        # A historical paid grant/run is seeded using normal constraints; no
        # clock or immutability trigger is disabled to simulate expired rights.
        f=self.fixture(501); suffix=uuid4().hex
        grant=self.sql("""INSERT INTO migration_purchase_grants(user_id,migration_id,quote_id,source,
          stripe_session_id,stripe_payment_intent_id,stripe_event_id,created_at)
          VALUES(%s,%s,%s,'stripe_test',%s,%s,%s,now()-interval '40 days') RETURNING id""",
          [A,f['migration'],f['quote'],'cs_test_'+suffix,'pi_'+suffix,'evt_'+suffix])[0]['id']
        key=uuid4().hex; first=self.start(f,key); job=self.claim()
        self.runs.authorize_dispatch(job,'worker-test')
        queued=self.start(f,rerun_of=first['run_id'])
        self.sql("""UPDATE migration_sessions SET status='completed',started_at=now()-interval '36 days',
          completed_at=now()-interval '35 days',locked_by=NULL,locked_at=NULL,lease_expires_at=NULL WHERE id=%s""",[first['session_id']])
        self.quotes.record_success(A,str(grant),first['run_id'])
        self.sql("UPDATE migration_operations SET status='succeeded' WHERE id=%s",[first['operation_id']])
        self.assertEqual(self.quotes.get_grant(A,f['migration'],str(grant))['state'],'expired')
        self.assertEqual(self.start(f,key)['status'],'succeeded')
        with self.assertRaises(PaymentRequiredError): self.start(f,rerun_of=first['run_id'])
        second=self.claim(); self.assertEqual(second['mcp_run_id'],queued['run_id'])
        with self.assertRaises(PaymentRequiredError): self.runs.authorize_dispatch(second,'worker-test')

    def test_reclaim_reuses_original_session_and_mirrors_terminal_failure(self):
        f=self.fixture(); key=uuid4().hex; run=self.start(f,key); job=self.claim()
        self.runs.authorize_dispatch(job,'worker-test')
        self.sql("UPDATE migration_sessions SET lease_expires_at=now()-interval '1 second' WHERE id=%s",[run['session_id']])
        self.assertEqual(self.sql('SELECT * FROM reclaim_expired_leases(2)')[0]['reclaimed_count'],1)
        self.assertEqual(self.start(f,key)['status'],'queued')
        second=self.claim(); self.assertEqual(second['id'],job['id']); self.assertEqual(second['attempt_count'],2)
        self.runs.authorize_dispatch(second,'worker-test')
        self.sql("UPDATE migration_sessions SET lease_expires_at=now()-interval '1 second' WHERE id=%s",[run['session_id']])
        self.sql('SELECT * FROM reclaim_expired_leases(2)')
        self.assertEqual(self.start(f,key)['status'],'failed')
        self.assertEqual(self.sql('SELECT count(*) AS n FROM migration_runs WHERE migration_id=%s',[f['migration']])[0]['n'],1)

    def test_changed_quote_race_and_parent_cleanup_leave_no_orphan(self):
        f=self.fixture(); second_quote=self.quotes.create_quote(A,f['migration'],f['old'],f['new'],uuid4().hex)['quote_id']
        key=uuid4().hex; barrier=Barrier(2)
        def start(quote):
            barrier.wait()
            try: return self.start({**f,'quote':quote},key)
            except OperationConflictError: return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            values=list(pool.map(start,[f['quote'],second_quote]))
        self.assertEqual(values.count('conflict'),1)
        winner=next(v for v in values if isinstance(v,dict))
        self.assertEqual(self.sql('SELECT count(*) AS n FROM migration_runs WHERE migration_id=%s',[f['migration']])[0]['n'],1)
        self.sql('DELETE FROM migration_records WHERE id=%s',[f['migration']])
        self.assertEqual(self.sql('SELECT count(*) AS n FROM migration_sessions WHERE id=%s',[winner['session_id']])[0]['n'],0)

    def test_bound_session_and_run_are_immutable_and_legacy_bridge_remains(self):
        import psycopg
        f=self.fixture(); run=self.start(f)
        for statement,args in [
            ("UPDATE migration_sessions SET old_urls='[]' WHERE id=%s",[run['session_id']]),
            ('UPDATE migration_sessions SET mcp_run_id=NULL WHERE id=%s',[run['session_id']]),
            ('UPDATE migration_runs SET quote_id=NULL WHERE id=%s',[run['run_id']]),
        ]:
            with self.assertRaises(psycopg.Error): self.sql(statement,args)
        session=str(uuid4())
        self.sql('INSERT INTO migration_sessions(id,user_id) VALUES(%s,%s)',[session,A])
        with self.assertRaises(psycopg.Error):
            self.sql('INSERT INTO migration_runs(migration_id,user_id,legacy_session_id) VALUES(%s,%s,%s)',[f['migration'],A,session])
        legacy=self.sql("INSERT INTO migration_records(user_id,status) VALUES(%s,'legacy_unverified') RETURNING id",[A])[0]['id']
        self.sql('INSERT INTO migration_runs(migration_id,user_id,legacy_session_id) VALUES(%s,%s,%s)',[legacy,A,session])
        with psycopg.connect(self.dsn,autocommit=True) as conn:
            conn.execute((ROOT/'database/migrations/037_entitled_migration_runs.sql').read_text())
        self.assertEqual(self.sql('SELECT count(*) AS n FROM migration_runs WHERE id=%s',[run['run_id']])[0]['n'],1)


class WorkerDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_hook_uses_pivot_finalization_and_skips_legacy_metering(self):
        # Hook ordering only. The unit fixture is not a matching-quality test.
        from backend.worker import RedirxWorker
        from types import SimpleNamespace
        worker=RedirxWorker.__new__(RedirxWorker); worker.worker_id='worker-test'; worker.jobs_processed=0
        worker._lease_extension_loop=AsyncMock(); worker.release_lease=AsyncMock(); worker.session_db=Mock()
        worker._apply_usage_accounting=Mock()
        events=[]
        service=Mock()
        def authorize(*_):
            events.append('authorize')
            return {'migration_id': str(uuid4())}
        service.authorize_dispatch.side_effect=authorize
        service.finalize_session.side_effect=lambda *_: (events.append('finalize') or {'status': 'succeeded'})
        async def steps(): yield None
        pipeline=SimpleNamespace(total_stages=1,stage_names=['fixture'],current_stage_index=1,iterate=steps)
        def construct(**kwargs): events.append('pipeline'); return pipeline
        client=Mock(); client.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value=SimpleNamespace(data=None)
        mappings=Mock(); mappings.get_mappings_by_session.return_value=[]
        job={'id':str(uuid4()),'mcp_run_id':str(uuid4()),'attempt_count':1,'pipeline_type':'content',
             'user_id':A,'old_urls':['https://old.example/a'],'new_urls':['https://new.example/a']}
        with patch('backend.worker.MigrationRunService',return_value=service), patch('backend.worker.DeepPreviewService'), \
             patch('backend.worker.Config.validate_embeddings'), patch('backend.worker.URLMappingDB',return_value=mappings), \
             patch('backend.worker.MatchRepairService'), patch('backend.worker.SupabaseClient.get_client',return_value=client), \
             patch('backend.worker.Pipeline',side_effect=construct), \
             patch('backend.services.jev_pipeline_service.JevService') as jev:
            jev.return_value.state.return_value = None  # This fixture exercises the historical engine.
            self.assertTrue(await worker.process_job(job))
        self.assertEqual(events,['authorize','pipeline','finalize'])
        worker._apply_usage_accounting.assert_not_called(); worker.release_lease.assert_not_called()
        self.assertEqual(service.finalize_session.call_args.args[2],'completed')

    async def test_denied_dispatch_never_constructs_pipeline_or_uses_legacy_accounting(self):
        from backend.worker import RedirxWorker
        worker=RedirxWorker.__new__(RedirxWorker); worker.worker_id='worker-test'
        worker._lease_extension_loop=AsyncMock(); worker.release_lease=AsyncMock()
        service=Mock(); service.authorize_dispatch.side_effect=PaymentRequiredError('Grant unavailable.')
        job={'id':str(uuid4()),'mcp_run_id':str(uuid4()),'attempt_count':1,'pipeline_type':'content',
             'old_urls':['https://old.example/a'],'new_urls':['https://new.example/a']}
        with patch('backend.worker.MigrationRunService',return_value=service), patch('backend.worker.DeepPreviewService'), patch('backend.worker.traceback.print_exc'), patch('backend.worker.Pipeline') as pipeline:
            self.assertFalse(await worker.process_job(job))
        pipeline.assert_not_called(); worker.release_lease.assert_not_called()
        service.finalize_session.assert_called_once()
        self.assertEqual(service.finalize_session.call_args.args[2],'pending')


if __name__=='__main__': unittest.main()
