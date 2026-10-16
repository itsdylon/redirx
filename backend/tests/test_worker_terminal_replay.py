"""Late pivot completion must respect the terminal outcome observed by SQL."""
import os
import unittest
from unittest.mock import patch
from backend.tests import test_artifact_decision_projection as artifact_fixture
from backend.tests import test_atomic_session_delete as delete_fixture


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'requires disposable loopback PostgreSQL')
class PivotTerminalReplay(unittest.TestCase):
    cleanup_db = classmethod(artifact_fixture.ArtifactDecisionProjection.cleanup_db.__func__)
    sql = artifact_fixture.ArtifactDecisionProjection.sql
    claim = artifact_fixture.ArtifactDecisionProjection.claim
    setUp = artifact_fixture.ArtifactDecisionProjection.setUp
    completed_run = artifact_fixture.ArtifactDecisionProjection.completed_run

    @classmethod
    def setUpClass(cls):
        delete_fixture.AtomicSessionDelete.setUpClass.__func__(cls)

    def test_stale_worker_cannot_report_success_after_newer_attempt_failed(self):
        import asyncio
        from contextlib import ExitStack
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from backend.worker import RedirxWorker
        from backend.tests.helpers.pivot_native_client import json_value
        saved = {}
        def leave_processing(job, *_):
            saved.update(job)
            return {'status': 'running'}
        with patch.object(self.runs, 'finalize_session', side_effect=leave_processing):
            mid, rid, _ = self.completed_run(unmatched_root=False)
        job = json_value(saved)
        owner_test = self
        class Pipeline:
            total_stages = 1
            current_stage_index = 1
            stage_names = ['Fixture']
            async def iterate(self):
                owner_test.sql("UPDATE migration_sessions SET lease_expires_at=now()-interval '1 minute' WHERE id=%s", [job['id']])
                owner_test.sql('SELECT reclaim_expired_leases(5)')
                rows = owner_test.sql("SELECT * FROM claim_next_job('new-worker',now()+interval '10 minutes')")
                owner_test.assertEqual(len(rows), 1)
                newer = json_value(rows[0])
                owner_test.assertEqual(newer['attempt_count'], job['attempt_count'] + 1)
                owner_test.runs.authorize_dispatch(newer, 'new-worker')
                owner_test.runs.finalize_session(newer, 'new-worker', 'permanently_failed', 'fixture failure')
                yield None
        worker = RedirxWorker.__new__(RedirxWorker)
        worker.worker_id, worker.jobs_processed = 'worker-test', 0
        worker.session_db = Mock()
        worker._lease_extension_loop = AsyncMock()
        worker._apply_usage_accounting = Mock()
        with ExitStack() as stack:
            stack.enter_context(patch('backend.worker.MigrationRunService', return_value=self.runs))
            stack.enter_context(patch('backend.services.jev_pipeline_service.JevService.state', return_value=None))
            stack.enter_context(patch('backend.worker.Pipeline', return_value=Pipeline()))
            stack.enter_context(patch('backend.worker.DeepPreviewService'))
            stack.enter_context(patch('backend.worker.Config.validate_embeddings'))
            stack.enter_context(patch('backend.worker.URLMappingDB')).return_value.get_mappings_by_session.return_value = []
            stack.enter_context(patch('backend.worker.SupabaseClient.get_client', return_value=self.native))
            email = stack.enter_context(patch('backend.services.email_service.EmailService'))
            result = asyncio.run(worker.process_job(job))
        actual = self.sql('SELECT status,attempt_count FROM migration_sessions WHERE id=%s', [job['id']])[0]
        self.assertEqual(actual['status'], 'permanently_failed')
        self.assertEqual(actual['attempt_count'], job['attempt_count'] + 1)
        self.assertFalse(result)
        self.assertEqual(worker.jobs_processed, 0)
        email.assert_not_called()
        worker._apply_usage_accounting.assert_not_called()
