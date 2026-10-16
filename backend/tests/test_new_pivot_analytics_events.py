"""
New JEV pivot-funnel analytics events (posthog-coverage-matrix.md gap 1).

Each test patches `capture` where the call site imports it (matching this
repo's own module-import convention: `from .analytics_service import
AppEvent, capture`), and uses lightweight Mock/fake clients rather than the
disposable-PostgreSQL harness — these are the call sites' own business-logic
branches (replay vs. not, terminal vs. not), not the RPC/SQL layer itself,
which the native journey tests already cover end to end.

No network, no real Postgres, no provider calls.
"""
from __future__ import annotations

import os
import sys
import unittest
from collections.abc import Mapping
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")

from backend.services.analytics_service import AppEvent

OWNER = str(UUID("00000000-0000-0000-0000-000000000001"))
MIGRATION = str(UUID("10000000-0000-0000-0000-000000000001"))
RUN = str(UUID("30000000-0000-0000-0000-000000000001"))


class PlanCreatedTests(unittest.TestCase):
    """MigrationPlanningService.plan() — v2_routes.py's JEV-only branch."""

    def _service(self, replayed):
        from backend.services.migration_planning_service import MigrationPlanningService
        repo = Mock()
        repo._execute.return_value = Mock(data={
            "migration_id": MIGRATION, "operation_id": str(uuid4()), "replayed": replayed,
        })
        repo.get_migration.return_value = {
            "id": MIGRATION, "old_origin": "https://old.example", "new_origin": "https://new.example",
            "name": None, "site_aliases": {},
        }
        repo.latest_inventory.return_value = None
        return MigrationPlanningService(repo)

    def test_fires_once_on_a_genuinely_new_plan(self):
        service = self._service(replayed=False)
        with patch("backend.services.migration_planning_service.capture") as capture:
            result = service.plan(OWNER, {"old_site": "https://old.example", "new_site": "https://new.example",
                                           "idempotency_key": "plan-1"})
        capture.assert_called_once()
        args, kwargs = capture.call_args
        self.assertEqual(args[0], AppEvent.MIGRATION_PLAN_CREATED)
        self.assertEqual(kwargs["user_id"], OWNER)
        self.assertEqual(kwargs["migration_id"], MIGRATION)
        self.assertEqual(kwargs["properties"]["engine"], "jev-url-v1")
        self.assertEqual(result["data"]["replayed"], False)

    def test_does_not_fire_on_a_replayed_idempotency_key(self):
        service = self._service(replayed=True)
        with patch("backend.services.migration_planning_service.capture") as capture:
            service.plan(OWNER, {"old_site": "https://old.example", "new_site": "https://new.example",
                                  "idempotency_key": "plan-1"})
        capture.assert_not_called()


class InventoryImportCompletedTests(unittest.TestCase):
    def _service(self):
        from backend.services.inventory_import_service import InventoryImportService
        repo = Mock()
        repo.get_migration.return_value = {"old_origin": "https://old.example", "new_origin": "https://new.example"}
        return InventoryImportService(repo), repo

    def _rpc_result(self, *, replayed, status="complete"):
        return Mock(error=None, data={
            "operation_id": str(uuid4()), "inventory_id": str(uuid4()), "migration_id": MIGRATION,
            "side": "old", "status": status, "page_count": 1,
            "content_hash": "a" * 64, "replayed": replayed,
        })

    def test_fires_on_a_new_terminal_import(self):
        service, repo = self._service()
        repo.client.rpc.return_value.execute.return_value = self._rpc_result(replayed=False)
        with patch("backend.services.inventory_import_service.capture") as capture, \
             patch("backend.services.inventory_import_service.preflight_inventory",
                   return_value={"status": "complete", "coverage": {"unique_count": 1}, "content_hash": "a" * 64}):
            service.import_inventory(OWNER, MIGRATION, "old", ["https://old.example/a"], "import-1")
        capture.assert_called_once()
        args, kwargs = capture.call_args
        self.assertEqual(args[0], AppEvent.INVENTORY_IMPORT_COMPLETED)
        self.assertEqual(kwargs["properties"]["status"], "complete")

    def test_does_not_fire_on_replay(self):
        service, repo = self._service()
        repo.client.rpc.return_value.execute.return_value = self._rpc_result(replayed=True)
        with patch("backend.services.inventory_import_service.capture") as capture, \
             patch("backend.services.inventory_import_service.preflight_inventory",
                   return_value={"status": "complete", "coverage": {"unique_count": 1}, "content_hash": "a" * 64}):
            service.import_inventory(OWNER, MIGRATION, "old", ["https://old.example/a"], "import-1")
        capture.assert_not_called()


class RefineStartedTests(unittest.TestCase):
    def _service(self, rpc_data):
        from backend.services.jev_pipeline_service import JevService
        repo = Mock()
        repo.client.rpc.return_value.execute.return_value = Mock(data=rpc_data)
        return JevService(repo)

    def test_fires_on_a_new_refine_pass(self):
        service = self._service({"migration_id": MIGRATION, "run_id": RUN,
                                  "operation_id": str(uuid4()), "status": "queued", "replayed": False})
        with patch("backend.services.jev_pipeline_service.capture") as capture:
            service.refine(OWNER, MIGRATION, RUN, 3, "refine-1")
        capture.assert_called_once()
        args, kwargs = capture.call_args
        self.assertEqual(args[0], AppEvent.MIGRATION_REFINE_STARTED)
        self.assertEqual(kwargs["properties"]["run_id"], RUN)
        self.assertEqual(kwargs["properties"]["expected_seed_revision"], 3)

    def test_does_not_fire_on_replay(self):
        service = self._service({"migration_id": MIGRATION, "run_id": RUN,
                                  "operation_id": str(uuid4()), "status": "queued", "replayed": True})
        with patch("backend.services.jev_pipeline_service.capture") as capture:
            service.refine(OWNER, MIGRATION, RUN, 3, "refine-1")
        capture.assert_not_called()


class RunCompletedTests(unittest.TestCase):
    """MigrationRunService.finalize_session() — the shared JEV+legacy choke point."""

    def _service(self, job, *, pre_status, jev_state=None):
        from backend.services.migration_run_service import MigrationRunService
        repo = Mock()
        # First .table(...) call: the pre-read this unit added, to detect a
        # session that is already terminal (finalize_migration_run_session's
        # own early-return branch carries no distinguishing flag).
        table_result = Mock()
        table_result.select.return_value = table_result
        table_result.eq.return_value = table_result
        table_result.limit.return_value = table_result
        table_result.execute.return_value = Mock(data=[{"status": pre_status}] if pre_status else [])
        repo.client.table.return_value = table_result
        # _call()'s own validation requires the RPC's run_id/session_id to
        # echo the request's p_run_id/p_session_id exactly.
        repo.client.rpc.return_value.execute.return_value = Mock(error=None, data={
            "run_id": job["mcp_run_id"], "session_id": job["id"], "operation_id": str(uuid4()),
            "migration_id": MIGRATION, "status": "succeeded",
        })
        service = MigrationRunService(repo)
        # Explicit either way: the shared mock client's .table(...) does not
        # distinguish 'migration_sessions' (this test's own pre-read) from
        # 'jev_runs' (JevService.state()'s read) by table name, so leaving
        # this unpatched for the non-JEV case would let the pre-read's rows
        # leak into JevService.state() and misclassify the engine.
        patcher = patch("backend.services.migration_run_service.read_jev_state", return_value=jev_state)
        patcher.start()
        self.addCleanup(patcher.stop)
        return service

    def _job(self):
        return {"id": str(uuid4()), "mcp_run_id": RUN, "user_id": OWNER, "attempt_count": 1}

    def test_fires_once_when_a_processing_session_reaches_a_terminal_status(self):
        job = self._job()
        service = self._service(job, pre_status="processing")
        with patch("backend.services.migration_run_service.capture") as capture, \
             patch("backend.services.migration_run_service._activation", return_value="test_only"):
            service.finalize_session(job, "worker-1", "completed")
        capture.assert_called_once()
        args, kwargs = capture.call_args
        self.assertEqual(args[0], AppEvent.MIGRATION_RUN_COMPLETED)
        self.assertEqual(kwargs["migration_id"], MIGRATION)
        self.assertEqual(kwargs["properties"]["engine"], "deep_match")

    def test_different_observed_terminal_outcome_is_not_this_requests_completion(self):
        job = self._job()
        service = self._service(job, pre_status="processing")
        service.repository.client.rpc.return_value.execute.return_value.data['status'] = 'failed'
        with patch("backend.services.migration_run_service.capture") as capture, \
             patch("backend.services.migration_run_service._activation", return_value="test_only"):
            result = service.finalize_session(job, "worker-1", "completed")
        self.assertEqual(result['status'], 'failed')
        capture.assert_not_called()

    def test_does_not_fire_again_for_an_already_terminal_session(self):
        # The retried-worker-call scenario: finalize_migration_run_session's
        # early-return branch would return the same JSON shape either way.
        job = self._job()
        service = self._service(job, pre_status="completed")
        with patch("backend.services.migration_run_service.capture") as capture, \
             patch("backend.services.migration_run_service._activation", return_value="test_only"):
            service.finalize_session(job, "worker-1", "completed")
        capture.assert_not_called()

    def test_does_not_fire_on_a_pending_requeue(self):
        job = self._job()
        service = self._service(job, pre_status="processing")
        with patch("backend.services.migration_run_service.capture") as capture:
            service.finalize_session(job, "worker-1", "pending", "transient")
        capture.assert_not_called()

    def test_jev_run_is_tagged_with_engine_and_pass(self):
        job = self._job()
        service = self._service(job, pre_status="processing", jev_state={"pass": 2})
        with patch("backend.services.migration_run_service.capture") as capture, \
             patch("backend.services.migration_run_service._activation", return_value="test_only"):
            service.finalize_session(job, "worker-1", "completed")
        _, kwargs = capture.call_args
        self.assertEqual(kwargs["properties"]["engine"], "jev-url-v1")
        self.assertEqual(kwargs["properties"]["pass"], 2)


class MappingDecisionsAppliedTests(unittest.TestCase):
    """migration_mapping_routes.py's resolve_matches — counts per action."""

    def _app(self, outcomes, replayed=False):
        from flask import Flask
        from backend.routes.migration_mapping_routes import create_migration_mapping_blueprint

        class FakeService:
            def resolve_matches(self, user_id, migration_id, run_id, decisions, key, **_):
                return {"migration_id": migration_id, "run_id": run_id, "operation_id": str(uuid4()),
                        "outcomes": outcomes, "replayed": replayed, "selection_revision": 1}

        app = Flask("mapping-test")
        app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
        app.register_blueprint(create_migration_mapping_blueprint(service_factory=FakeService), url_prefix="/api/v2")

        @app.before_request
        def auth():
            from flask import request
            request.api_user_id = OWNER
            request._v2_auth_resolved = True

        return app

    def _decisions(self, n):
        return [{"mapping_id": str(uuid4()), "expected_revision": 0, "action": "approve"} for _ in range(n)]

    def test_counts_are_tallied_per_action_not_per_row(self):
        outcomes = ([{"mapping_id": str(uuid4()), "code": "ok", "action": "approve", "revision": 1, "target_url": "x"}] * 3
                    + [{"mapping_id": str(uuid4()), "code": "ok", "action": "reject", "revision": 1, "target_url": None}]
                    + [{"mapping_id": str(uuid4()), "code": "revision_conflict"}])
        app = self._app(outcomes)
        with patch("backend.routes.migration_mapping_routes.capture") as capture:
            with app.test_client() as client:
                client.patch(f"/api/v2/migrations/{MIGRATION}/runs/{RUN}/matches", json={
                    "decisions": self._decisions(5), "idempotency_key": "resolve-1",
                })
        capture.assert_called_once()
        args, kwargs = capture.call_args
        self.assertEqual(args[0], AppEvent.MAPPING_DECISIONS_APPLIED)
        self.assertEqual(kwargs["properties"]["applied"], 4)
        self.assertEqual(kwargs["properties"]["not_applied"], 1)
        self.assertEqual(kwargs["properties"]["by_action"], {"approve": 3, "reject": 1})

    def test_does_not_fire_on_replay(self):
        outcomes = [{"mapping_id": str(uuid4()), "code": "ok", "action": "approve", "revision": 1, "target_url": "x"}]
        app = self._app(outcomes, replayed=True)
        with patch("backend.routes.migration_mapping_routes.capture") as capture:
            with app.test_client() as client:
                client.patch(f"/api/v2/migrations/{MIGRATION}/runs/{RUN}/matches", json={
                    "decisions": self._decisions(1), "idempotency_key": "resolve-1",
                })
        capture.assert_not_called()

    def test_does_not_fire_when_every_decision_fails(self):
        outcomes = [{"mapping_id": str(uuid4()), "code": "revision_conflict"}]
        app = self._app(outcomes)
        with patch("backend.routes.migration_mapping_routes.capture") as capture:
            with app.test_client() as client:
                client.patch(f"/api/v2/migrations/{MIGRATION}/runs/{RUN}/matches", json={
                    "decisions": self._decisions(1), "idempotency_key": "resolve-1",
                })
        capture.assert_not_called()


class VerificationCompletedDedupTests(unittest.TestCase):
    """migration_verification_service.py's record() — the batch-worker choke point."""

    def _service(self, *, rpc_returns, status_rows):
        from backend.services.migration_verification_service import MigrationVerificationService
        repo = Mock()
        repo._rpc = None  # not used directly; service._rpc wraps repository.client.rpc
        repo.client.rpc.return_value.execute.return_value = Mock(error=None, data=rpc_returns[0] if len(rpc_returns) == 1 else rpc_returns)
        # `_rpc` in the service calls repository.client.rpc(name, params).execute(),
        # so make each successive call return the next item when called multiple times.
        call_iter = iter(rpc_returns)
        def rpc(name, params):
            return Mock(execute=lambda: Mock(error=None, data=next(call_iter)))
        repo.client.rpc.side_effect = rpc
        table_result = Mock()
        table_result.select.return_value = table_result
        table_result.eq.return_value = table_result
        repo._execute = Mock(side_effect=lambda q: q.execute())
        rows_iter = iter(status_rows)
        def table(name):
            m = Mock()
            m.select.return_value = m
            m.eq.return_value = m
            m.execute = lambda: Mock(data=next(rows_iter))
            return m
        repo.client.table.side_effect = table
        return MigrationVerificationService(repo)

    def _item(self, ordinal):
        return {"ordinal": ordinal, "attempt": 1}

    def test_fires_once_when_the_last_item_completes(self):
        service = self._service(
            rpc_returns=[True],
            status_rows=[[{"id": "v1", "user_id": OWNER, "migration_id": MIGRATION, "artifact_id": "a1",
                           "deployment_id": "d1", "status": "succeeded", "passed": 2, "failed": 0,
                           "unchecked": 0, "total": 2}]],
        )
        with patch("backend.services.migration_verification_service.capture") as capture:
            applied = service.record("v1", self._item(1), "worker-1", "passed", {})
        self.assertTrue(applied)
        capture.assert_called_once()
        args, kwargs = capture.call_args
        self.assertEqual(args[0], AppEvent.MIGRATION_VERIFICATION_COMPLETED)
        self.assertEqual(kwargs["properties"]["verification_kind"], "included")
        self.assertEqual(kwargs["properties"]["status"], "succeeded")

    def test_does_not_fire_twice_for_the_same_verification_on_one_instance(self):
        service = self._service(
            rpc_returns=[True, True],
            status_rows=[
                [{"id": "v1", "user_id": OWNER, "migration_id": MIGRATION, "artifact_id": "a1",
                  "deployment_id": "d1", "status": "succeeded", "passed": 2, "failed": 0, "unchecked": 0, "total": 2}],
                [{"id": "v1", "user_id": OWNER, "migration_id": MIGRATION, "artifact_id": "a1",
                  "deployment_id": "d1", "status": "succeeded", "passed": 2, "failed": 0, "unchecked": 0, "total": 2}],
            ],
        )
        with patch("backend.services.migration_verification_service.capture") as capture:
            service.record("v1", self._item(1), "worker-1", "passed", {})
            service.record("v1", self._item(2), "worker-1", "passed", {})
        capture.assert_called_once()

    def test_does_not_fire_while_still_running(self):
        service = self._service(
            rpc_returns=[True],
            status_rows=[[{"id": "v1", "user_id": OWNER, "migration_id": MIGRATION, "artifact_id": "a1",
                           "deployment_id": "d1", "status": "running", "passed": 1, "failed": 0,
                           "unchecked": 0, "total": 2}]],
        )
        with patch("backend.services.migration_verification_service.capture") as capture:
            service.record("v1", self._item(1), "worker-1", "passed", {})
        capture.assert_not_called()

    def test_does_not_fire_when_the_rpc_write_did_not_apply(self):
        service = self._service(rpc_returns=[False], status_rows=[])
        with patch("backend.services.migration_verification_service.capture") as capture:
            applied = service.record("v1", self._item(1), "stale-worker", "passed", {})
        self.assertFalse(applied)
        capture.assert_not_called()


class ArtifactExportAndInstallTests(unittest.TestCase):
    """Reuses migration_artifact_service.py's own fixtures for create_artifact/report_installation."""

    def _fixtures(self):
        from backend.tests.test_migration_artifact_service import Client, Repository, ARTIFACT, MIGRATION as M, RUN as R, OWNER as O
        return Client, Repository, ARTIFACT, M, R, O

    def test_export_fires_once_for_a_new_artifact(self):
        from backend.services.migration_artifact_service import MigrationArtifactService
        Client, Repository, ARTIFACT, M, R, O = self._fixtures()
        client = Client()
        service = MigrationArtifactService(Repository(client))
        with patch("backend.services.migration_artifact_service.capture") as capture:
            service.create_artifact(O, M, R, idempotency_key="artifact-1", fmt="json", selection_revision="rev-1")
        capture.assert_called_once()
        args, kwargs = capture.call_args
        self.assertEqual(args[0], AppEvent.REDIRECT_ARTIFACT_EXPORTED)
        self.assertEqual(kwargs["migration_id"], M)

    def test_export_does_not_fire_on_replay(self):
        from backend.services.migration_artifact_service import MigrationArtifactService
        Client, Repository, ARTIFACT, M, R, O = self._fixtures()
        client = Client()
        service = MigrationArtifactService(Repository(client))
        first = service.create_artifact(O, M, R, idempotency_key="artifact-replay", fmt="json", selection_revision="rev-1")
        client.rows["migration_artifact_mutations"] = [{"user_id": O, "migration_id": M, "kind": "artifact",
            "idempotency_key": "artifact-replay", "result": first}]
        with patch("backend.services.migration_artifact_service.capture") as capture:
            replay = service.create_artifact(O, M, R, idempotency_key="artifact-replay", fmt="json", selection_revision="rev-1")
        self.assertTrue(replay["replayed"])
        capture.assert_not_called()

    def test_installation_fires_once_for_a_new_deployment(self):
        from backend.services.migration_artifact_service import MigrationArtifactService
        Client, Repository, ARTIFACT, M, R, O = self._fixtures()
        client = Client()
        service = MigrationArtifactService(Repository(client))
        with patch("backend.services.migration_artifact_service.capture") as capture:
            service.report_installation(O, M, ARTIFACT, "https://live.example", idempotency_key="deploy-1")
        capture.assert_called_once()
        args, kwargs = capture.call_args
        self.assertEqual(args[0], AppEvent.MIGRATION_ARTIFACT_INSTALLED)
        self.assertEqual(kwargs["properties"]["artifact_id"], ARTIFACT)


if __name__ == "__main__":
    unittest.main()
