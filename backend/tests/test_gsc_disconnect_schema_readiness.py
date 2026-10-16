"""Catalog-only evidence for independently staged 068; no disconnect RPC calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

import psycopg

from backend.tests import test_migration_gsc as fixture
from backend.tests.test_migration_planning import A, ROOT
from scripts.release.apply_pivot_migrations import load_packet
from scripts.release.check_history_schema import reviewed_contracts as history_contracts
from scripts.release.check_gsc_disconnect_schema import (
    check_gsc_disconnect_schema, reviewed_contracts, SCOPE,
)


class ReviewedDisconnectSources(unittest.TestCase):
    def test_allowlists_stay_separate_and_changed_source_is_rejected(self):
        self.assertEqual([r['migration'][:3] for r in reviewed_contracts()], ['068'])
        self.assertEqual([r['migration'][:3] for r in history_contracts()], ['065', '066', '067'])
        old = load_packet(ROOT/'docs/release-evidence/application-schema-rehearsal.json', ROOT/'database/migrations')
        self.assertEqual([int(r['name'][:3]) for r in old], [32, *range(34, 57)])
        with tempfile.TemporaryDirectory() as directory:
            contract = reviewed_contracts()[0]
            source = ROOT/'database/migrations'/contract['migration']
            Path(directory, contract['migration']).write_bytes(source.read_bytes() + b'\n-- drift\n')
            with self.assertRaises(ValueError): reviewed_contracts(Path(directory))

    def test_cli_bad_connection_is_nonzero_and_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory, 'dsn')
            credential.write_text('password=fixture-secret invalid-connection-fragment')
            credential.chmod(0o600)
            result = subprocess.run([sys.executable, str(ROOT/'scripts/release/check_gsc_disconnect_schema.py'),
                '--dsn-file', str(credential), '--ca-file', str(Path(directory, 'unused-ca')),
                '--target-file', str(Path(directory, 'unused-target')), '--expected-owner', 'postgres'],
                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['scope'], SCOPE)
        self.assertFalse(json.loads(result.stdout)['matches'])
        self.assertEqual(result.stderr, '')
        self.assertNotIn('fixture-secret', result.stdout)
        self.assertNotIn('invalid-connection-fragment', result.stdout)


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'requires disposable loopback PostgreSQL')
class DisconnectSchemaReadiness(unittest.TestCase):
    cleanup_db = classmethod(fixture.PostgreSQLTests.cleanup_db.__func__)

    @classmethod
    def setUpClass(cls):
        fixture.PostgreSQLTests.setUpClass.__func__(cls)
        with psycopg.connect(cls.dsn, autocommit=True) as conn:
            conn.execute((ROOT/'database/migrations/068_atomic_legacy_gsc_disconnect.sql').read_text())
            conn.execute("INSERT INTO gsc_connections(user_id,access_token,refresh_token) VALUES(%s,'fixture-access','fixture-refresh')", [A])
            conn.execute("INSERT INTO gsc_agent_accounts(user_id,state,epoch) VALUES(%s,'connected',7)", [A])

    def setUp(self):
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(self.conn.close)
        self.signature = reviewed_contracts()[0]['signature']
        self.definition = self.conn.execute('SELECT pg_get_functiondef(%s::regprocedure)', [self.signature]).fetchone()[0]
        self.addCleanup(self.restore)

    def restore(self):
        self.conn.execute('RESET ROLE')
        # PostgreSQL cannot remove argument defaults with CREATE OR REPLACE.
        # This disposable fixture has no callers depending on the function OID.
        self.conn.execute(f'DROP FUNCTION IF EXISTS {self.signature}')
        self.conn.execute(self.definition)
        self.conn.execute(f'ALTER FUNCTION {self.signature} OWNER TO postgres')
        self.conn.execute(f'REVOKE ALL ON FUNCTION {self.signature} FROM PUBLIC,anon,authenticated,service_role')
        self.conn.execute(f'GRANT EXECUTE ON FUNCTION {self.signature} TO service_role')

    def check(self):
        return check_gsc_disconnect_schema(self.conn, expected_owner='postgres')

    def failures(self):
        return set(self.check()['checks'][0]['failures'])

    def snapshot(self):
        return [self.conn.execute(f'SELECT tableoid,xmin,ctid,* FROM {table}').fetchall()
                for table in ('gsc_connections', 'gsc_agent_accounts', 'gsc_agent_operations')]

    def test_positive_unprivileged_auditor_cannot_execute_or_read_credentials(self):
        auditor = 'fixture_auditor_' + uuid4().hex
        self.conn.execute(f'CREATE ROLE {auditor} NOLOGIN NOINHERIT')
        def cleanup_role():
            self.conn.execute('RESET ROLE')
            self.conn.execute(f'DROP ROLE {auditor}')
        self.addCleanup(cleanup_role)
        before = self.snapshot()
        self.conn.execute(f'SET ROLE {auditor}')
        self.assertFalse(self.conn.execute("SELECT has_function_privilege(current_user,%s,'EXECUTE')", [self.signature]).fetchone()[0])
        self.assertFalse(self.conn.execute("SELECT has_table_privilege(current_user,'gsc_connections','SELECT')").fetchone()[0])
        result = self.check()
        self.assertTrue(result['matches'], result)
        self.assertTrue(result['read_only'])
        self.assertEqual(result['scope'], SCOPE)
        self.assertEqual(len(result['checks']), 1)
        self.conn.execute('RESET ROLE')
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn('fixture-refresh', json.dumps(result))

    def test_changed_body_is_rejected_without_exposing_body(self):
        self.conn.execute("""CREATE OR REPLACE FUNCTION public.disconnect_legacy_gsc_connection(p_user_id uuid)
            RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp
            AS $$ BEGIN RETURN '{"private_fixture_value":true}'::jsonb; END $$""")
        result = self.check()
        self.assertFalse(result['matches'])
        self.assertIn('body_mismatch', result['checks'][0]['failures'])
        self.assertNotIn('private_fixture_value', json.dumps(result))

    def test_missing_moved_and_overloaded_signatures_fail_closed(self):
        self.conn.execute(f'DROP FUNCTION {self.signature}')
        self.assertIn('function_missing', self.failures())
        self.restore()
        self.conn.execute('CREATE SCHEMA fixture_wrong_namespace')
        self.conn.execute(f'ALTER FUNCTION {self.signature} SET SCHEMA fixture_wrong_namespace')
        try:
            self.assertIn('function_missing', self.failures())
        finally:
            self.conn.execute(f'ALTER FUNCTION {self.signature.replace("public.", "fixture_wrong_namespace.")} SET SCHEMA public')
            self.conn.execute('DROP SCHEMA fixture_wrong_namespace')
        self.conn.execute("CREATE FUNCTION public.disconnect_legacy_gsc_connection(text) RETURNS jsonb LANGUAGE sql AS $$ SELECT '{}'::jsonb $$")
        try:
            self.assertIn('missing_or_ambiguous_signature', self.failures())
        finally:
            self.conn.execute('DROP FUNCTION public.disconnect_legacy_gsc_connection(text)')
        self.assertTrue(self.check()['matches'])

    def test_owner_security_path_defaults_and_volatility_drift_fail_closed(self):
        for statement, code in [
            (f'ALTER FUNCTION {self.signature} OWNER TO service_role', 'owner_mismatch'),
            (f'ALTER FUNCTION {self.signature} SECURITY INVOKER', 'prosecdef_mismatch'),
            (f'ALTER FUNCTION {self.signature} SET search_path=public', 'proconfig_mismatch'),
            (f'ALTER FUNCTION {self.signature} STABLE', 'provolatile_mismatch'),
            (self.definition.replace('p_user_id uuid)', 'p_user_id uuid DEFAULT NULL::uuid)'), 'arguments_mismatch'),
        ]:
            with self.subTest(code=code):
                self.conn.execute(statement)
                self.assertIn(code, self.failures())
                self.restore()
                self.assertTrue(self.check()['matches'])
        self.assertFalse(check_gsc_disconnect_schema(self.conn, expected_owner='service_role')['matches'])

    def test_effective_service_browser_public_inherited_and_extra_grants(self):
        for statement, code in [
            (f'REVOKE EXECUTE ON FUNCTION {self.signature} FROM service_role', 'service_execute_mismatch'),
            (f'GRANT EXECUTE ON FUNCTION {self.signature} TO PUBLIC', 'unexpected_execute_grant'),
            (f'GRANT EXECUTE ON FUNCTION {self.signature} TO anon', 'anon_execute_mismatch'),
            (f'GRANT EXECUTE ON FUNCTION {self.signature} TO authenticated', 'browser_execute_mismatch'),
            (f'GRANT EXECUTE ON FUNCTION {self.signature} TO service_role WITH GRANT OPTION', 'unexpected_execute_grant'),
        ]:
            with self.subTest(code=code):
                self.conn.execute(statement)
                self.assertIn(code, self.failures())
                self.restore()
                self.assertTrue(self.check()['matches'])
        self.conn.execute('GRANT service_role TO authenticated')
        try:
            self.assertIn('browser_execute_mismatch', self.failures())
        finally:
            self.conn.execute('REVOKE service_role FROM authenticated')
        extra = 'fixture_extra_' + uuid4().hex
        self.conn.execute(f'CREATE ROLE {extra} NOLOGIN')
        try:
            self.conn.execute(f'GRANT EXECUTE ON FUNCTION {self.signature} TO {extra}')
            self.assertIn('unexpected_execute_grant', self.failures())
        finally:
            self.conn.execute(f'REVOKE ALL ON FUNCTION {self.signature} FROM {extra}')
            self.conn.execute(f'DROP ROLE {extra}')
        self.assertTrue(self.check()['matches'])

    def test_busy_closed_or_missing_owner_never_reports_ready(self):
        with self.conn.transaction():
            with self.assertRaises(ValueError): self.check()
        for owner in ('', None):
            with self.assertRaises(ValueError): check_gsc_disconnect_schema(self.conn, expected_owner=owner)
        closed = psycopg.connect(self.dsn, autocommit=True)
        closed.close()
        with self.assertRaises(ValueError): check_gsc_disconnect_schema(closed, expected_owner='postgres')


if __name__ == '__main__': unittest.main()
