"""Exercise actual destructive fixture entry points with every connection blocked."""
import importlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import unittest
from unittest.mock import patch

from psycopg import pq
from psycopg.conninfo import conninfo_to_dict

from backend.tests.helpers.disposable_postgres import (
    ERROR, ROUTING_ENV, validate_disposable_postgres_config,
)

ROOT = Path(__file__).resolve().parents[2]
DSN = 'postgresql://postgres:private-fixture-password@127.0.0.1:55432/postgres'
CONFIG = {'host': '127.0.0.1', 'port': 55432, 'dbname': 'postgres',
          'user': 'postgres', 'password': 'local-fixture-only'}
# The three setup roots plus representative inherited backend, MCP and browser paths.
SETUPS = (
    ('test_migration_run_service', 'RunDatabaseAcceptance'),
    ('test_rate_limit_loops', 'LimiterPoolOwnership'),
    ('test_pivot_migration_release', 'AtomicMigrationHistory'),
    ('test_migration_planning', 'DatabaseHTTPAcceptance'),
    ('test_pivot_product_journey', 'NativeProductJourney'),
    ('test_artifact_decision_projection', 'ArtifactDecisionProjection'),
    ('test_jev_pipeline', 'NativeJev'),
    ('test_companion_native_journey', 'CompanionNativeJourney'),
    ('test_history_pages', 'HistoryPages'),
    ('test_migration_monitoring', 'MonitoringTests'),
)
PROBES = ('subscription_runtime_probe', 'subscription_checkout_runtime_probe')


class ConnectionReached(Exception):
    """Positive sentinel; no real connector is ever invoked."""


class NativeDatabaseGuardEntrypoints(unittest.TestCase):
    def setUp(self):
        clean = {key: value for key, value in os.environ.items() if key not in ROUTING_ENV}
        clean.update(PREFLIGHT_TEST_DATABASE_URL=DSN, PYTHON_DOTENV_DISABLED='1', POSTHOG_API_KEY='')
        self.enterContext(patch.dict(os.environ, clean, clear=True))
        self.enterContext(patch('dotenv.load_dotenv', return_value=False))
        self.connect = self.enterContext(patch('psycopg.connect', side_effect=ConnectionReached))

    @staticmethod
    def setup_class(module, name):
        fixture = getattr(importlib.import_module('backend.tests.' + module), name)
        # Keep actual fixture classes pristine for the full native suite after this test.
        isolated = type('GuardProbe', (fixture,), {})
        isolated.setUpClass()

    def test_libpq_ambient_hostaddr_bypasses_original_explicit_host_check_offline(self):
        with patch.dict(os.environ, {'PGHOSTADDR': '192.0.2.1'}):
            parsed = conninfo_to_dict(DSN)
            defaults = {entry.keyword: entry.val for entry in pq.Conninfo.get_defaults()}
            self.assertEqual(parsed['host'], '127.0.0.1')
            self.assertNotIn('hostaddr', parsed)
            self.assertEqual(defaults[b'hostaddr'], b'192.0.2.1')
        self.connect.assert_not_called()

    def test_every_setup_refuses_query_overrides_before_connect(self):
        for module, name in SETUPS:
            for query in ('host=example.invalid', '%68ost=example.invalid',
                          'hostaddr=192.0.2.1', 'service=external', 'host=127.0.0.1,example.invalid'):
                with self.subTest(module=module, query=query), patch.dict(os.environ, {
                    'PREFLIGHT_TEST_DATABASE_URL': DSN + '?' + query,
                }), self.assertRaises(ValueError) as caught:
                    self.setup_class(module, name)
                self.assertEqual(str(caught.exception), ERROR)
        self.connect.assert_not_called()

    def test_every_setup_refuses_inherited_routing_before_connect(self):
        for module, name in SETUPS:
            for setting in ROUTING_ENV:
                with self.subTest(module=module, setting=setting), patch.dict(os.environ, {
                    setting: 'external-setting',
                }), self.assertRaises(ValueError) as caught:
                    self.setup_class(module, name)
                self.assertEqual(str(caught.exception), ERROR)
        self.connect.assert_not_called()

    def test_every_setup_accepts_normal_ci_uri_until_fake_connector(self):
        for module, name in SETUPS:
            self.connect.reset_mock()
            with self.subTest(module=module), self.assertRaises(ConnectionReached):
                self.setup_class(module, name)
            self.connect.assert_called_once_with(DSN, autocommit=True)

    def test_required_runner_refuses_before_test_collection(self):
        runner = importlib.import_module('scripts.ci.backend')
        for settings in ({'PREFLIGHT_TEST_DATABASE_URL': DSN + '?host=example.invalid'},
                         {'PGHOSTADDR': '192.0.2.1'}, {'PGSERVICE': 'external'}):
            with self.subTest(settings=list(settings)), patch.dict(os.environ, settings), \
                    patch.object(runner.unittest, 'TestLoader') as loader, self.assertRaises(SystemExit) as caught:
                runner.run()
            self.assertEqual(str(caught.exception), ERROR)
            loader.assert_not_called()
        self.connect.assert_not_called()

    def test_actual_ci_script_refuses_without_secret_or_traceback(self):
        env = {**os.environ, 'PREFLIGHT_TEST_DATABASE_URL': DSN + '?hostaddr=192.0.2.1'}
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/ci/backend.py')],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), ERROR)
        self.assertEqual(result.stdout, '')

    def test_generated_subscription_config_uses_exact_caller_shape(self):
        validate_disposable_postgres_config(CONFIG, {})
        validate_disposable_postgres_config({**CONFIG, 'host': '::1'}, {})
        for change in ({'hostaddr': '192.0.2.1'}, {'service': 'external'}, {'host': 'localhost'},
                       {'host': '127.0.0.1,example.invalid'}, {'host': '/tmp/socket'},
                       {'host': '127.0.0.1:55432/postgres'}, {'port': True}, {'port': '55432'}):
            with self.subTest(keys=list(change)), self.assertRaises(ValueError) as caught:
                validate_disposable_postgres_config({**CONFIG, **change}, {})
            self.assertEqual(str(caught.exception), ERROR)
        self.connect.assert_not_called()

    def test_actual_subscription_probes_refuse_override_before_connect(self):
        for module in PROBES:
            for config, environment in (({**CONFIG, 'hostaddr': '192.0.2.1'}, {}),
                                        ({**CONFIG, 'service': 'external'}, {}),
                                        (CONFIG, {'PGHOSTADDR': '192.0.2.1'}),
                                        (CONFIG, {'PGSERVICE': 'external'})):
                with self.subTest(module=module, keys=list(environment or config)), patch.dict(os.environ, {
                    'SUBSCRIPTION_FIXTURE_CONNECTION': json.dumps(config),
                    'SUBSCRIPTION_FIXTURE_SCOPE': '{}', **environment,
                }), self.assertRaises(ValueError) as caught:
                    runpy.run_module('backend.tests.' + module, run_name='__fixture_guard__')
                self.assertEqual(str(caught.exception), ERROR)
        self.connect.assert_not_called()

    def test_actual_subscription_probes_accept_generated_config_until_fake_connector(self):
        for module in PROBES:
            self.connect.reset_mock()
            with self.subTest(module=module), patch.dict(os.environ, {
                'SUBSCRIPTION_FIXTURE_CONNECTION': json.dumps(CONFIG), 'SUBSCRIPTION_FIXTURE_SCOPE': '{}',
            }), self.assertRaises(ConnectionReached):
                runpy.run_module('backend.tests.' + module, run_name='__fixture_guard__')
            self.connect.assert_called_once_with(**CONFIG, autocommit=True)
