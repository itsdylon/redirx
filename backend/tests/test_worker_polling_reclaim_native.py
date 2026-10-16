"""Actual queue SQL restores both marked Jev and historical work in polling mode."""
import asyncio
from contextlib import ExitStack, nullcontext
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from backend.tests import test_jev_pipeline as jev_fixture
from backend.tests.helpers.pivot_native_client import NativeClient, json_value
from backend.tests.test_migration_planning import A
from backend.services.jev_store import DurableStore
from backend.worker import RedirxWorker, WORKER_MAX_ATTEMPTS


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'requires disposable loopback PostgreSQL')
class NativePollingReclaim(unittest.TestCase):
    setUpClass = classmethod(jev_fixture.NativeJev.setUpClass.__func__)
    cleanup_db = classmethod(jev_fixture.NativeJev.cleanup_db.__func__)
    setUp = jev_fixture.NativeJev.setUp
    sql = jev_fixture.NativeJev.sql
    fixture = jev_fixture.NativeJev.fixture
    claim = jev_fixture.NativeJev.claim
    start = jev_fixture.NativeJev.start
    prepared = jev_fixture.NativeJev.prepared
    proposal = jev_fixture.NativeJev.proposal

    def exercise(self, enabled, *, lost_reclaim=False):
        fixture, run, old_job, old_store, _ = self.prepared()
        # A provider reservation whose response/work outcome is unknown remains charged.
        reservation = old_store.reserve(100)
        self.sql("UPDATE migration_sessions SET lease_expires_at=now()-interval '1 minute' WHERE id=%s", [old_job['id']])
        historical, exhausted = str(uuid4()), str(uuid4())
        for sid, attempt in ((historical, 1), (exhausted, WORKER_MAX_ATTEMPTS)):
            self.sql("""INSERT INTO migration_sessions(id,user_id,status,old_urls,new_urls,pipeline_type,
                attempt_count,locked_by,lease_expires_at) VALUES(%s,%s,'processing',
                '["https://old.example/legacy"]','["https://new.example/legacy"]','url_only',
                %s,'dead-worker',now()-interval '1 minute')""", [sid, A, attempt])
        owner = self
        calls = []
        class QueueClient(NativeClient):
            def rpc(self, name, params):
                # Adapt SETOF RPC wire shape only; execute actual queue SQL.
                if name == 'reclaim_expired_leases':
                    def execute():
                        calls.append(name)
                        rows = owner.sql('SELECT * FROM reclaim_expired_leases(%s)', [params['p_max_attempts']])
                        if lost_reclaim: raise TimeoutError('fixture response lost after commit')
                        return SimpleNamespace(data=json_value(rows))
                    return SimpleNamespace(execute=execute)
                if name == 'claim_next_job':
                    return SimpleNamespace(execute=lambda: SimpleNamespace(data=json_value(owner.sql(
                        'SELECT * FROM claim_next_job(%s,%s)', [params['p_worker_id'], params['p_lease_expires_at']]))))
                return super().rpc(name, params)
        client = QueueClient(self.dsn)
        worker = RedirxWorker.__new__(RedirxWorker)
        worker.worker_id = 'polling-recovery'
        worker.jobs_processed = 0
        worker.max_concurrent = 1
        worker.in_flight_tasks = set()
        worker.running = False
        worker.session_db = Mock()
        worker.session_db.get_session.return_value = None
        worker._lease_extension_loop = AsyncMock()
        worker._watch_tick = AsyncMock()
        worker._await_watch_task = AsyncMock()
        worker._apply_usage_accounting = Mock()
        worker._claim_job_via_postgres = Mock(return_value=None)  # No direct DB/LISTEN channel.
        finished = []
        async def process(job):
            result = await RedirxWorker.process_job(worker, job)
            finished.append((job, result))
            if len(finished) == 2:
                worker.running = False
            return result
        worker.process_job = process
        sleeps = 0
        async def idle_sleep(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2: worker.running = False  # Before-fix bounded failure.
        class HistoricalPipeline:
            total_stages = current_stage_index = 1
            stage_names = ['Offline historical fixture']
            async def iterate(self): yield None
        async def jev_run(job, worker_id, _progress):
            store = DurableStore(owner.native, job, worker_id)
            state = store.rpc('prepare_jev_pass')
            for index in (0, 1): owner.proposal(store, fixture, state, index)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'JEV_MVP_ENABLED': 'true' if enabled else 'false'}))
            stack.enter_context(patch('backend.worker.SupabaseClient.get_client', return_value=client))
            stack.enter_context(patch('backend.worker.MigrationRunService', return_value=self.runs))
            stack.enter_context(patch('backend.services.jev_database_transport.jev_worker_database', return_value=nullcontext(self.native)))
            jev = stack.enter_context(patch('backend.services.jev_runner.JevPipelineRunner')).return_value
            jev.run = AsyncMock(side_effect=jev_run)
            pipeline = stack.enter_context(patch('backend.worker.Pipeline', return_value=HistoricalPipeline()))
            stack.enter_context(patch('backend.worker.DeepPreviewService'))
            stack.enter_context(patch('backend.worker.URLMappingDB')).return_value.get_mappings_by_session.return_value = []
            stack.enter_context(patch('backend.worker.MatchRepairService')).return_value.repair_session.return_value = SimpleNamespace(as_dict=lambda: {})
            email = stack.enter_context(patch('backend.services.email_service.EmailService'))
            stack.enter_context(patch('backend.worker.asyncio.sleep', side_effect=idle_sleep))
            asyncio.run(worker.polling_loop())
        self.assertEqual(len(finished), 2)
        self.assertEqual(calls, ['reclaim_expired_leases'])
        self.assertEqual({str(job['id']) for job, _ in finished}, {str(old_job['id']), historical})
        self.assertTrue(all(job['attempt_count'] == 2 for job, _ in finished))
        self.assertEqual(pipeline.call_count, 1)
        self.assertEqual(str(pipeline.call_args.kwargs['session_id']), historical)
        self.assertEqual(jev.run.await_count, int(enabled))
        self.assertEqual(worker.jobs_processed, 2 if enabled else 1)
        self.assertEqual(worker._apply_usage_accounting.call_count, 1)
        email.assert_not_called()
        rows = {str(row['id']): row for row in self.sql(
            'SELECT id,status,attempt_count FROM migration_sessions WHERE id=ANY(%s::uuid[])',
            [[str(old_job['id']), historical, exhausted]])}
        self.assertEqual(rows[historical]['status'], 'completed')
        self.assertEqual(rows[exhausted]['status'], 'permanently_failed')
        self.assertEqual(rows[exhausted]['attempt_count'], WORKER_MAX_ATTEMPTS)
        self.assertEqual(rows[str(old_job['id'])]['status'], 'completed' if enabled else 'permanently_failed')
        self.assertEqual(self.sql('SELECT o.status FROM migration_runs r JOIN migration_operations o ON o.id=r.operation_id WHERE r.id=%s', [run['run_id']])[0]['status'],
                         'succeeded' if enabled else 'failed')
        self.assertEqual(self.sql('SELECT count(*) n FROM migration_mapping_decision_events WHERE run_id=%s', [run['run_id']])[0]['n'], 0)
        self.assertEqual(self.sql('SELECT count(*) n FROM migration_artifacts WHERE run_id=%s', [run['run_id']])[0]['n'], 0)
        self.assertEqual(self.sql('SELECT provider_requests FROM jev_runs WHERE run_id=%s', [run['run_id']])[0]['provider_requests'], 1)
        self.assertEqual(self.sql('SELECT reserved_micro_usd FROM jev_provider_reservations WHERE id=%s', [reservation])[0]['reserved_micro_usd'], 100)
        with self.assertRaises(Exception) as caught: old_store.reserve(100)
        self.assertEqual(getattr(caught.exception, 'message', None), 'operation_conflict')

    def test_polling_reclaims_marked_and_historical_jobs_without_auto_approval(self):
        self.exercise(True)

    def test_paused_marker_after_reclaim_never_falls_back_to_historical_engine(self):
        self.exercise(False)

    def test_lost_reclaim_response_does_not_retry_mutation_or_block_readback(self):
        self.exercise(True, lost_reclaim=True)
