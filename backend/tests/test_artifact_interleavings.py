"""Adversarial ordering across real service reads and authoritative publication SQL."""
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.services.migration_artifact_service import MigrationArtifactService, DeploymentConflictError
from backend.services.migration_repository import MigrationNotFoundError
from backend.tests import test_artifact_decision_projection as artifact_fixture
from backend.tests import test_atomic_session_delete as delete_fixture
from backend.tests.test_migration_planning import A, B


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'requires disposable loopback PostgreSQL')
class ArtifactInterleavings(unittest.TestCase):
    cleanup_db = classmethod(artifact_fixture.ArtifactDecisionProjection.cleanup_db.__func__)
    sql = artifact_fixture.ArtifactDecisionProjection.sql
    claim = artifact_fixture.ArtifactDecisionProjection.claim
    completed_run = artifact_fixture.ArtifactDecisionProjection.completed_run
    decide = artifact_fixture.ArtifactDecisionProjection.decide
    export = artifact_fixture.ArtifactDecisionProjection.export

    @classmethod
    def setUpClass(cls):
        # Current pinned schema through063, including Jev and immutable authority.
        delete_fixture.AtomicSessionDelete.setUpClass.__func__(cls)

    def setUp(self):
        artifact_fixture.ArtifactDecisionProjection.setUp(self)
        # Preserve the actual five-run allowance; each scenario owns a fresh
        # synthetic account rather than disabling or clearing its quota.
        owner = str(uuid4())
        self.sql("INSERT INTO auth.users(id,email,email_confirmed_at) VALUES(%s,'artifact@fixture.invalid',now())", [owner])
        self.sql('INSERT INTO user_profiles(id) VALUES(%s) ON CONFLICT DO NOTHING', [owner])
        for target in (__name__ + '.A', 'backend.tests.test_artifact_decision_projection.A'):
            scope = patch(target, owner)
            scope.start()
            self.addCleanup(scope.stop)

    def test_concurrent_same_key_publication_emits_one_completion_event(self):
        mid, rid, _ = self.completed_run(unmatched_root=False)
        key, barrier = uuid4().hex, Barrier(2)
        def create(_):
            service = MigrationArtifactService(self.repo)
            original = service._replay_artifact
            def simultaneous_miss(*args):
                result = original(*args)
                self.assertIsNone(result)
                barrier.wait(timeout=5)
                return result
            service._replay_artifact = simultaneous_miss
            return service.create_artifact(A, mid, rid, idempotency_key=key,
                fmt='csv', selection_revision='0')
        with patch('backend.services.migration_artifact_service.capture') as capture:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(create, range(2)))
        self.assertEqual(len({row['id'] for row in results}), 1)
        self.assertEqual(sorted(row['replayed'] for row in results), [False, True])
        self.assertEqual(self.sql('SELECT count(*) AS n FROM migration_artifacts WHERE run_id=%s', [rid])[0]['n'], 1)
        self.assertEqual(capture.call_count, 1)

    def test_decision_changed_after_snapshot_is_nonretryable_conflict_not_storage_failure(self):
        mid, rid, ids = self.completed_run(unmatched_root=False)
        service = MigrationArtifactService(self.repo)
        original = service.selection_reader
        def edit_after_read(*args):
            snapshot = original(*args)
            self.decide(mid, rid, ids['/'], 'reject')
            return snapshot
        service.selection_reader = edit_after_read
        with patch('backend.services.migration_artifact_service.capture') as capture:
            with self.assertRaises(DeploymentConflictError) as caught:
                service.create_artifact(A, mid, rid, idempotency_key=uuid4().hex,
                    fmt='csv', selection_revision='0')
        self.assertFalse(caught.exception.retryable)
        capture.assert_not_called()
        self.assertEqual(self.sql('SELECT count(*) AS n FROM migration_artifacts WHERE run_id=%s', [rid])[0]['n'], 0)

    def test_grant_revocation_after_preflight_is_rechecked_at_publication(self):
        mid, rid, _ = self.completed_run(unmatched_root=False)
        service = MigrationArtifactService(self.repo)
        original = service.selection_reader
        def revoke_after_read(*args):
            snapshot = original(*args)
            self.sql("UPDATE migration_purchase_grants SET state='revoked' WHERE id=(SELECT grant_id FROM migration_runs WHERE id=%s)", [rid])
            return snapshot
        service.selection_reader = revoke_after_read
        with self.assertRaises(MigrationNotFoundError):
            service.create_artifact(A, mid, rid, idempotency_key=uuid4().hex,
                fmt='csv', selection_revision='0')
        self.assertEqual(self.sql('SELECT count(*) AS n FROM migration_artifacts WHERE run_id=%s', [rid])[0]['n'], 0)

    def test_owned_artifact_stays_immutable_after_decision_edit_and_denies_other_owner(self):
        mid, rid, ids = self.completed_run(unmatched_root=False)
        service = MigrationArtifactService(self.repo)
        first = service.create_artifact(A, mid, rid, idempotency_key=uuid4().hex,
            fmt='csv', selection_revision='0')
        before = service.authorize_download(A, mid, first['id'])
        self.decide(mid, rid, ids['/'], 'reject')
        after = service.authorize_download(A, mid, first['id'])
        self.assertEqual(before, after)
        self.assertIn('https://new.example/', after['content'])
        with self.assertRaises(MigrationNotFoundError):
            service.authorize_download(B, mid, first['id'])
        self.sql("UPDATE migration_purchase_grants SET state='revoked' WHERE id=(SELECT grant_id FROM migration_runs WHERE id=%s)", [rid])
        with self.assertRaises(MigrationNotFoundError):
            service.authorize_download(A, mid, first['id'])


    def test_committed_publication_with_lost_response_replays_same_bytes_without_new_event(self):
        from backend.services.migration_artifact_service import ArtifactOperationUnconfirmedError
        mid, rid, ids = self.completed_run(unmatched_root=False)
        service, key = MigrationArtifactService(self.repo), uuid4().hex
        rpc = self.native.rpc
        class LostResponse:
            def __init__(self, query): self.query = query
            def execute(self):
                self.query.execute()
                raise TimeoutError('fixture lost response after commit')
        def lose_publication(name, params):
            query = rpc(name, params)
            return LostResponse(query) if name == 'publish_migration_artifact' else query
        with patch('backend.services.migration_artifact_service.capture') as capture:
            with patch.object(self.native, 'rpc', side_effect=lose_publication):
                with self.assertRaises(ArtifactOperationUnconfirmedError) as caught:
                    service.create_artifact(A, mid, rid, idempotency_key=key, fmt='csv', selection_revision='0')
            self.assertEqual(caught.exception.next_action, 'poll')
            self.decide(mid, rid, ids['/'], 'reject')
            replay = service.create_artifact(A, mid, rid, idempotency_key=key, fmt='csv', selection_revision='0')
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.sql('SELECT count(*) AS n FROM migration_artifacts WHERE run_id=%s', [rid])[0]['n'], 1)
        self.assertIn('https://new.example/', service.authorize_download(A, mid, replay['id'])['content'])
        capture.assert_not_called()

    def test_already_stale_revision_stops_before_publication_with_conflict(self):
        from backend.services.migration_artifact_readers import SelectionRevisionConflictError
        mid, rid, ids = self.completed_run(unmatched_root=False)
        self.decide(mid, rid, ids['/'], 'reject')
        with self.assertRaises(SelectionRevisionConflictError) as caught:
            self.export(mid, rid, '0')
        self.assertEqual(caught.exception.code, 'revision_conflict')
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.sql('SELECT count(*) AS n FROM migration_artifacts WHERE run_id=%s', [rid])[0]['n'], 0)
