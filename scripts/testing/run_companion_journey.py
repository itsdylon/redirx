"""Run only the opt-in native browser journey; skipped/empty suites are failures."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.tests.helpers.disposable_postgres import validate_disposable_postgres_dsn

try:
    # Pre-build validation permits the automatic launcher to choose its port later.
    dsn = os.getenv('PREFLIGHT_TEST_DATABASE_URL', '')
    if sys.argv[1:] == ['--validate-launch'] and not dsn:
        dsn = 'postgresql://postgres:fixture-only@127.0.0.1:5432/postgres'
    validate_disposable_postgres_dsn(dsn)
except ValueError as error:
    sys.exit(str(error))
if sys.argv[1:] == ['--validate-launch']:
    sys.exit(0)
os.environ.update(POSTHOG_API_KEY='', OPENAI_API_KEY='', PYTHON_DOTENV_DISABLED='1')
with patch('dotenv.load_dotenv', return_value=False):
    suite = unittest.defaultTestLoader.loadTestsFromName('backend.tests.test_companion_native_journey')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() and result.testsRun == 2 and not result.skipped else 1)
