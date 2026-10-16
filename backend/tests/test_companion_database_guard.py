"""No-connection regression tests for fixture destination validation."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from psycopg.conninfo import conninfo_to_dict
from backend.tests.helpers.disposable_postgres import ERROR, ROUTING_ENV, validate_disposable_postgres_dsn

ROOT = Path(__file__).resolve().parents[2]
BASE = 'postgresql://postgres:private-fixture-password@127.0.0.1:55432/disposable'


class CompanionDatabaseGuard(unittest.TestCase):
    def test_original_url_check_misses_libpq_override_offline(self):
        dsn = BASE + '?host=example.invalid'
        self.assertEqual(urlsplit(dsn).hostname, '127.0.0.1')
        self.assertEqual(conninfo_to_dict(dsn)['host'], 'example.invalid')
        with self.assertRaisesRegex(ValueError, 'routing overrides'):
            validate_disposable_postgres_dsn(dsn, {})

    def test_explicit_numeric_loopback_with_safe_options(self):
        with patch('psycopg.connect', side_effect=AssertionError('must never connect')):
            for dsn in (BASE, BASE.replace('postgresql:', 'postgres:'),
                        BASE.replace('127.0.0.1', '[::1]'),
                        BASE + '?sslmode=disable&application_name=fixture',
                        BASE.replace('/disposable', '/fixture%20database')):
                with self.subTest(dsn=dsn.replace('private-fixture-password', 'redacted')):
                    self.assertIsNone(validate_disposable_postgres_dsn(dsn, {}))

    def test_query_routing_and_encoded_overrides_are_rejected(self):
        for query in ('host=example.invalid', 'hostaddr=192.0.2.1', 'service=external',
                      'servicefile=/tmp/fixture', 'host=/tmp/socket', 'host=127.0.0.1,192.0.2.1',
                      '%68ost=example.invalid', 'host%61ddr=192.0.2.1', 'host=%2Ftmp',
                      'host=127.0.0.1&host=example.invalid', 'host=', 'port=55432', 'dbname=other'):
            with self.subTest(query=query), self.assertRaises(ValueError) as caught:
                validate_disposable_postgres_dsn(BASE + '?' + query, {})
            self.assertEqual(str(caught.exception), ERROR)

    def test_authority_routing_is_explicit_and_unambiguous(self):
        for dsn in (BASE.replace('127.0.0.1', 'localhost'), BASE.replace('127.0.0.1', '192.0.2.1'),
                    BASE.replace('127.0.0.1', '127.0.0.1,example.invalid'),
                    BASE.replace('127.0.0.1', '%2Ftmp'), BASE.replace('127.0.0.1', '%31%32%37.0.0.1'),
                    BASE.replace('127.0.0.1', '[::ffff:127.0.0.1]'),
                    BASE.replace(':55432', ''), BASE.replace(':55432', ':0'),
                    BASE.replace(':55432', ':65536'), BASE.replace('/disposable', '/'),
                    BASE + '#ignored', ' ' + BASE, 'host=127.0.0.1 port=55432 dbname=fixture'):
            with self.subTest(dsn=dsn.replace('private-fixture-password', 'redacted')), self.assertRaises(ValueError):
                validate_disposable_postgres_dsn(dsn, {})

    def test_ambient_routing_is_refused(self):
        for name in ROUTING_ENV:
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_disposable_postgres_dsn(BASE, {name: 'override'})
        validate_disposable_postgres_dsn(BASE, {name: '' for name in ROUTING_ENV})

    def test_libpq_parse_errors_do_not_disclose_dsn(self):
        for dsn in (BASE + '?unknown=private-query-value', BASE.replace(':55432', ':secret-port')):
            with self.assertRaises(ValueError) as caught:
                validate_disposable_postgres_dsn(dsn, {})
            self.assertEqual(str(caught.exception), ERROR)
            self.assertNotIn('private', str(caught.exception))

    def test_python_entrypoint_rejects_before_fixture_import(self):
        env = {key: value for key, value in os.environ.items() if key not in ROUTING_ENV}
        env['PREFLIGHT_TEST_DATABASE_URL'] = BASE + '?host=example.invalid'
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/testing/run_companion_journey.py')],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), ERROR)
        self.assertEqual(result.stdout, '')

    def test_prebuild_mode_accepts_automatic_and_explicit_destinations_without_connecting(self):
        env = {key: value for key, value in os.environ.items() if key not in ROUTING_ENV}
        for dsn in ('', BASE):
            with self.subTest(automatic=not dsn):
                env['PREFLIGHT_TEST_DATABASE_URL'] = dsn
                result = subprocess.run([sys.executable, str(ROOT / 'scripts/testing/run_companion_journey.py'),
                                         '--validate-launch'], env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, '')
        env['PGHOSTADDR'] = '192.0.2.1'
        env['PREFLIGHT_TEST_DATABASE_URL'] = ''
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/testing/run_companion_journey.py'),
                                 '--validate-launch'], env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), ERROR)
