"""Required application contracts; no model calls or retrieval quality gates.

Run from the repository root: python scripts/ci/backend.py
Use a disposable loopback PostgreSQL instance with CREATE DATABASE privileges.
"""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.tests.helpers.disposable_postgres import validate_disposable_postgres_dsn

# Deliberately select product boundaries, not the current matching implementation.
# Keep this list explicit: broad discovery also collects live/provider/capacity tests.
MODULES = (
    "test_current_billing_service",
    "test_current_billing_routes",
    "test_current_billing_settlement",
    "test_current_paid_admission.CurrentAdmissionGates",
    "test_companion_database_guard",
    "test_native_database_guard_entrypoints",
    "test_auth_service_isolation",
    "test_mcp_delegation_service",
    "test_companion_auth",
    "test_inventory_policy",
    "test_inventory_guidance",
    "test_inventory_import_service",
    "test_ingestion_service",
    "test_discovery_gsc_choice",
    "test_legacy_gsc_refresh",
    "test_legacy_gsc_disconnect",
    "test_legacy_gsc_callback",
    "test_gsc_lifecycle_interleavings",
    "test_migration_repository",
    "test_owned_read_acknowledgments",
    "test_status_row_identity",
    "test_mapping_decision_service",
    "test_redirect_export",
    "test_export_route_outcomes",
    "test_legacy_export_routes",
    "test_legacy_review_routes",
    "test_email_lifecycle",
    "test_redirect_probe",
    "test_redirect_probe_live",
    "test_http_client_security",
    "test_migration_verification.ClassificationTests",
    "test_migration_verification.BatchLifecycleTests",
    "test_verification_politeness",
    "test_watch_ownership",
    "test_safe_fetch",
    "test_worker_claim_fallback",
    "test_worker_reconnect",
    "test_worker_polling_reclaim",
    "test_worker_polling_reclaim_native",
    "test_worker_finalization",
    "test_worker_progress_interleaving",
    "test_jev_seed_scope",
    "test_jev_confidence_boundary",
    "test_worker_terminal_replay",
    "test_migration_planning",
    "test_migration_run_service",
    "test_artifact_decision_projection",
    "test_artifact_interleavings",
    "test_migration_artifact_service",
    "test_migration_artifact_readers",
    "test_migration_artifact_routes",
    "test_atomic_session_delete",
    "test_user_session_filters",
    "test_dashboard_snapshot",
    "test_history_pages",
    "test_history_schema_readiness",
    "test_gsc_disconnect_schema_readiness",
    "test_jev_seed_scope_schema_readiness",
    "test_release_connection_target",
    "test_read_pagination_consistency",
)


def run():
    dsn = os.environ.get("PREFLIGHT_TEST_DATABASE_URL", "")
    try:
        validate_disposable_postgres_dsn(dsn)
    except ValueError as error:
        sys.exit(str(error))
    # Never import credentials from a developer's .env; CI needs no service secrets.
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["POSTHOG_API_KEY"] = ""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for module in MODULES:
        tests = loader.loadTestsFromName("backend.tests." + module)
        if tests.countTestCases() == 0:
            sys.exit(f"No tests collected from {module}")
        suite.addTests(tests)
    if loader.errors:
        sys.exit("\n".join(loader.errors))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print("Required tests skipped:", result.skipped, file=sys.stderr)
    passed = result.wasSuccessful() and result.testsRun > 0 and not result.skipped
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    # python-dotenv 1.0 does not honor PYTHON_DOTENV_DISABLED.
    with patch("dotenv.load_dotenv", return_value=False):
        run()
