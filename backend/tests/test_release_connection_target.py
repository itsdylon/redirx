"""Release target routing is verified offline, before any connection attempt."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.pq import Conninfo

from scripts.release import apply_pivot_migrations, fingerprint_existing_data, rpc_schema_catalog
from scripts.release import check_history_schema, check_gsc_disconnect_schema
from scripts.release.connection_target import ROUTING_ENVIRONMENT, validate_release_target

ROOT = Path(__file__).resolve().parents[2]
TARGET = {'host': 'reviewed.example.invalid', 'port': '5432', 'user': 'fixture-owner', 'dbname': 'fixture-db'}
DSN = make_conninfo(**TARGET, password='fixture-private-password')


class ReachedConnection(Exception):
    pass


class ReleaseTargetValidation(unittest.TestCase):
    def test_previous_explicit_check_missed_hostaddr_and_service_defaults_without_connecting(self):
        supplied = conninfo_to_dict(DSN)
        self.assertTrue(all(supplied.get(k) == v for k, v in TARGET.items()))
        self.assertFalse(supplied.get('hostaddr') or supplied.get('service'))
        with tempfile.TemporaryDirectory() as directory:
            service = Path(directory, 'fixture-service.conf')
            service.write_text('[fixture]\nhostaddr=203.0.113.17\n')
            for environment in ({'PGHOSTADDR': '203.0.113.17'},
                                {'PGSERVICE': 'fixture', 'PGSERVICEFILE': str(service)}):
                with self.subTest(kind=sorted(environment)), patch.dict(os.environ, environment, clear=True):
                    defaults = {row.keyword.decode(): row.val.decode() if row.val else None
                                for row in Conninfo.get_defaults()}
                    self.assertEqual(defaults['hostaddr'], '203.0.113.17')
                    with self.assertRaises(ValueError): validate_release_target(DSN, TARGET)

    def test_exact_explicit_hostname_and_ipv6_targets_are_accepted_without_dns(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(validate_release_target(DSN, TARGET))
            self.assertIsNone(validate_release_target(
                'postgresql://fixture-owner:fixture-private-password@reviewed.example.invalid:5432/fixture-db', TARGET))
            ipv6 = {**TARGET, 'host': '::1'}
            self.assertIsNone(validate_release_target(make_conninfo(**ipv6), ipv6))
            self.assertIsNone(validate_release_target(DSN, TARGET, environment={'PGHOSTADDR': ''}))

    def test_every_ambient_routing_or_service_source_is_refused(self):
        for key in ROUTING_ENVIRONMENT:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'Inherited'):
                validate_release_target(DSN, TARGET, environment={key: 'fixture-ambient-value'})

    def test_explicit_overrides_nested_uri_and_mismatched_targets_are_refused(self):
        alternatives = [
            DSN + ' hostaddr=203.0.113.17', DSN + " hostaddr=''", DSN + ' service=fixture',
            DSN + ' host=other.example.invalid', DSN + ' port=6432',
            DSN + ' user=another-owner', DSN + ' dbname=another-db',
            'postgresql://fixture-owner:fixture-private-password@reviewed.example.invalid:5432/fixture-db?host=other.example.invalid',
            'postgresql://fixture-owner:fixture-private-password@reviewed.example.invalid:5432/fixture-db?host%61ddr=203.0.113.17',
            'password=fixture-private-password invalid-connection-fragment',
        ]
        for dsn in alternatives:
            with self.subTest(index=alternatives.index(dsn)), self.assertRaises(ValueError) as failed:
                validate_release_target(dsn, TARGET, environment={})
            self.assertNotIn('fixture-private-password', str(failed.exception))
            self.assertNotIn('other.example.invalid', str(failed.exception))

    def test_review_file_cannot_authorize_implicit_socket_or_multihost_routing(self):
        for target in ({}, {**TARGET, 'host': ''}, {**TARGET, 'user': ''}, {**TARGET, 'dbname': ''},
                       {**TARGET, 'host': '/private/tmp/socket'}, {**TARGET, 'host': '@abstract'},
                       {**TARGET, 'host': 'first.example.invalid,second.example.invalid'},
                       {**TARGET, 'port': '5432,6432'}, {**TARGET, 'port': '0'},
                       {**TARGET, 'port': '65536'}, {**TARGET, 'port': 5432}, {**TARGET, 'extra': 'field'}):
            with self.subTest(target_keys=sorted(target)), self.assertRaises(ValueError):
                validate_release_target(DSN, target, environment={})

    def entrypoints(self, directory):
        common = ['--dsn-file', str(directory/'dsn'), '--ca-file', str(directory/'ca.pem'),
                  '--target-file', str(directory/'target.json')]
        return [
            (apply_pivot_migrations, apply_pivot_migrations.main, common),
            (fingerprint_existing_data, fingerprint_existing_data.main, common + ['--output', str(directory/'fingerprint.json')]),
            (rpc_schema_catalog, check_history_schema.main, common + ['--expected-owner', 'fixture-owner']),
            (rpc_schema_catalog, check_gsc_disconnect_schema.main, common + ['--expected-owner', 'fixture-owner']),
        ]

    def files(self, directory):
        (directory/'dsn').write_text(DSN)
        (directory/'dsn').chmod(0o600)
        (directory/'target.json').write_text(json.dumps({'connection_target': TARGET}))

    def test_all_actual_entrypoints_refuse_ambient_and_explicit_overrides_before_connect(self):
        for environment, suffix in (({'PGHOSTADDR': '203.0.113.17'}, ''),
                                    ({'PGSERVICE': 'fixture', 'PGSERVICEFILE': '/unused/fixture'}, ''),
                                    ({}, ' hostaddr=203.0.113.17')):
            with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, environment, clear=True):
                directory = Path(temp); self.files(directory)
                (directory/'dsn').write_text(DSN + suffix)
                for module, main, arguments in self.entrypoints(directory):
                    with self.subTest(entry=main.__module__, ambient=sorted(environment)), patch.object(
                        module.psycopg, 'connect', side_effect=AssertionError('No connection permitted')) as connect, patch.object(
                        sys, 'argv', ['fixture-command', *arguments]):
                        with self.assertRaises(ValueError): main()
                        connect.assert_not_called()
                self.assertFalse((directory/'fingerprint.json').exists())
                with patch.object(apply_pivot_migrations.psycopg, 'connect') as connect, patch.object(
                    sys, 'argv', ['fixture-command', *self.entrypoints(directory)[0][2], '--apply', '--journal', str(directory/'journal.jsonl')]):
                    with self.assertRaises(ValueError): apply_pivot_migrations.main()
                    connect.assert_not_called()
                self.assertFalse((directory/'journal.jsonl').exists())

    def test_all_actual_entrypoints_reach_one_connection_with_unchanged_tls_for_reviewed_target(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            directory = Path(temp); self.files(directory)
            for module, main, arguments in self.entrypoints(directory):
                with self.subTest(entry=main.__module__), patch.object(module.psycopg, 'connect', side_effect=ReachedConnection) as connect, patch.object(
                    sys, 'argv', ['fixture-command', *arguments]):
                    with self.assertRaises(ReachedConnection): main()
                    connect.assert_called_once_with(DSN, autocommit=True, sslmode='verify-full',
                        sslrootcert=str(directory/'ca.pem'), connect_timeout=15, prepare_threshold=None)

    def test_actual_cli_refusals_are_nonzero_and_do_not_print_targets_or_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp); self.files(directory)
            for script, extra in [('apply_pivot_migrations.py', []),
                                  ('fingerprint_existing_data.py', ['--output', str(directory/'fingerprint.json')]),
                                  ('check_history_schema.py', ['--expected-owner', 'fixture-owner']),
                                  ('check_gsc_disconnect_schema.py', ['--expected-owner', 'fixture-owner'])]:
                args = self.entrypoints(directory)[0][2]
                environment = {key: value for key, value in os.environ.items() if key not in ROUTING_ENVIRONMENT}
                environment.update(PYTHON_DOTENV_DISABLED='1', PGHOSTADDR='203.0.113.17')
                result = subprocess.run([sys.executable, str(ROOT/'scripts/release'/script), *args, *extra],
                                        env=environment, capture_output=True, text=True, timeout=10)
                with self.subTest(script=script):
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stderr, '')
                    self.assertEqual(json.loads(result.stdout)['error_type'], 'ValueError')
                    for private in ('fixture-private-password', 'reviewed.example.invalid', '203.0.113.17'):
                        self.assertNotIn(private, result.stdout)
            self.assertFalse((directory/'fingerprint.json').exists())


if __name__ == '__main__': unittest.main()
