"""Required application contracts; no model calls or retrieval quality gates.

Run from the repository root: python scripts/ci/backend.py
Use a disposable loopback PostgreSQL instance with CREATE DATABASE privileges.
"""
import os
from pathlib import Path
import sys
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Deliberately select product boundaries, not the current matching implementation.
# Keep this list explicit: broad discovery also collects live/provider/capacity tests.
MODULES = (
    "test_current_billing_service",
    "test_current_billing_routes",
    "test_current_billing_settlement",
    "test_current_paid_admission.CurrentAdmissionGates",
    "test_auth_service_isolation",
    "test_mcp_delegation_service",
    "test_companion_auth",
    "test_inventory_policy",
    "test_inventory_import_service",
    "test_migration_repository",
    "test_mapping_decision_service",
    "test_redirect_export",
    "test_safe_fetch",
    "test_worker_claim_fallback",
    "test_worker_reconnect",
    "test_migration_planning",
    "test_migration_run_service",
    "test_artifact_decision_projection",
)


def run():
    dsn = os.environ.get("PREFLIGHT_TEST_DATABASE_URL", "")
    if urlsplit(dsn).hostname not in ("127.0.0.1", "localhost", "::1"):
        sys.exit("Set PREFLIGHT_TEST_DATABASE_URL to disposable loopback PostgreSQL; database tests must run.")
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
