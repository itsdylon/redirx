"""Read-only release catalog evidence; all drift is confined to disposable PG."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

import psycopg

from backend.tests import test_history_pages as fixture
from scripts.release.apply_pivot_migrations import history_state, load_packet, ROOT
from scripts.release.check_history_schema import check_history_schema, reviewed_contracts


class ReviewedHistorySources(unittest.TestCase):
    def test_explicit_source_sets_exclude_deferred_billing_and_do_not_expand_old_packet(self):
        old = load_packet(ROOT/'docs/release-evidence/application-schema-rehearsal.json', ROOT/'database/migrations')
        self.assertEqual([int(row['name'][:3]) for row in old], [32, *range(34,57)])
        self.assertEqual([row['migration'][:3] for row in reviewed_contracts()], ['065','066','067'])
        with tempfile.TemporaryDirectory() as directory:
            for row in reviewed_contracts():
                Path(directory, row['migration']).write_bytes((ROOT/'database/migrations'/row['migration']).read_bytes())
            with Path(directory, '067_legacy_session_pages.sql').open('a') as stream:
                stream.write('\n-- unreviewed change\n')
            with self.assertRaises(ValueError):
                reviewed_contracts(Path(directory))

    def test_cli_unknown_connection_error_is_nonzero_without_secret_or_raw_error(self):
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory, 'fixture-dsn')
            credential.write_text('password=fixture-secret invalid-connection-fragment')
            credential.chmod(0o600)
            result = subprocess.run([sys.executable, str(ROOT/'scripts/release/check_history_schema.py'),
                '--dsn-file', str(credential), '--ca-file', str(Path(directory,'unused-ca')),
                '--target-file', str(Path(directory,'unused-target')), '--expected-owner', 'postgres'],
                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)['matches'])
        self.assertEqual(result.stderr, '')
        self.assertNotIn('fixture-secret', result.stdout)
        self.assertNotIn('invalid-connection-fragment', result.stdout)


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'requires disposable loopback PostgreSQL')
class HistorySchemaReadiness(unittest.TestCase):
    cleanup_db = classmethod(fixture.HistoryPages.cleanup_db.__func__)
    sql = fixture.HistoryPages.sql

    @classmethod
    def setUpClass(cls):
        fixture.HistoryPages.setUpClass.__func__(cls)
        cls.sql(cls, '''CREATE SCHEMA supabase_migrations;
            CREATE TABLE supabase_migrations.schema_migrations(version text PRIMARY KEY,name text,statements text[]);
            CREATE TABLE fixture_catalog_canary(id integer PRIMARY KEY);
            INSERT INTO fixture_catalog_canary VALUES(42)''')

    def setUp(self):
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(self.conn.close)
        self.saved = []
        for contract in reviewed_contracts():
            definition = self.conn.execute('SELECT pg_get_functiondef(%s::regprocedure)', [contract['signature']]).fetchone()[0]
            self.saved.append((contract['signature'], definition))
        self.addCleanup(self.restore)

    def restore(self):
        self.conn.execute('RESET ROLE')
        for signature, definition in self.saved:
            self.conn.execute(definition)
            self.conn.execute(f'ALTER FUNCTION {signature} OWNER TO postgres')
            self.conn.execute(f'REVOKE ALL ON FUNCTION {signature} FROM PUBLIC,anon,authenticated,service_role')
            self.conn.execute(f'GRANT EXECUTE ON FUNCTION {signature} TO service_role')

    def check(self):
        return check_history_schema(self.conn, expected_owner='postgres')

    def failures(self):
        return {code for row in self.check()['checks'] for code in row['failures']}

    def test_positive_under_unprivileged_auditor_and_no_rpc_or_data_write(self):
        auditor = 'fixture_auditor_' + uuid4().hex
        self.conn.execute(f'CREATE ROLE {auditor} NOLOGIN NOINHERIT')
        def cleanup_role():
            self.conn.execute('RESET ROLE')
            self.conn.execute(f'DROP ROLE {auditor}')
        self.addCleanup(cleanup_role)
        before = self.conn.execute('SELECT tableoid,xmin,ctid,id FROM fixture_catalog_canary').fetchall()
        self.conn.execute(f'SET ROLE {auditor}')
        for signature, _ in self.saved:
            self.assertFalse(self.conn.execute("SELECT has_function_privilege(current_user,%s,'EXECUTE')", [signature]).fetchone()[0])
        result = self.check()
        self.assertTrue(result['matches'], result)
        self.assertTrue(result['read_only'])
        self.assertEqual(len(result['checks']), 3)
        self.assertTrue(all(row['matches'] for row in result['checks']))
        self.conn.execute('RESET ROLE')
        self.assertEqual(self.conn.execute('SELECT tableoid,xmin,ctid,id FROM fixture_catalog_canary').fetchall(), before)

    def test_original_history_accepts_drift_but_new_check_rejects_body_without_leaking_it(self):
        contract = reviewed_contracts()[1]
        migration = {'name': contract['migration'][:-4],
                     'source': (ROOT/'database/migrations'/contract['migration']).read_text()}
        self.conn.execute('INSERT INTO supabase_migrations.schema_migrations VALUES(%s,%s,%s)',
                          ['fixture-064',migration['name'],[migration['source']]])
        self.addCleanup(lambda: self.conn.execute("DELETE FROM supabase_migrations.schema_migrations WHERE version='fixture-064'"))
        self.conn.execute("""CREATE OR REPLACE FUNCTION public.legacy_dashboard_snapshot(p_user_id uuid)
            RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=public,pg_temp
            AS $$ SELECT '{"private_fixture_value":true}'::jsonb $$""")
        self.assertEqual(history_state(self.conn,migration,'fixture-064'), ('already_applied','fixture-064'))
        result = self.check()
        self.assertFalse(result['matches'])
        self.assertIn('body_mismatch', result['checks'][1]['failures'])
        self.assertNotIn('private_fixture_value', json.dumps(result))

    def test_service_public_browser_and_inherited_grants_are_authoritative(self):
        signature = self.saved[1][0]
        cases = [
            (f'REVOKE EXECUTE ON FUNCTION {signature} FROM service_role', 'service_execute_mismatch'),
            (f'GRANT EXECUTE ON FUNCTION {signature} TO PUBLIC', 'unexpected_execute_grant'),
            (f'GRANT EXECUTE ON FUNCTION {signature} TO authenticated', 'browser_execute_mismatch'),
            (f'GRANT EXECUTE ON FUNCTION {signature} TO service_role WITH GRANT OPTION', 'unexpected_execute_grant'),
        ]
        for statement, code in cases:
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
        self.assertTrue(self.check()['matches'])

    def test_names_existing_cannot_hide_missing_wrong_namespace_or_overloads(self):
        signature = self.saved[2][0]
        self.conn.execute('CREATE SCHEMA fixture_wrong_namespace')
        self.conn.execute(f'ALTER FUNCTION {signature} SET SCHEMA fixture_wrong_namespace')
        try:
            self.assertIn('function_missing', self.failures())
        finally:
            self.conn.execute(f'ALTER FUNCTION {signature.replace("public.", "fixture_wrong_namespace.")} SET SCHEMA public')
            self.conn.execute('DROP SCHEMA fixture_wrong_namespace')
        self.conn.execute("CREATE FUNCTION public.legacy_session_page(text) RETURNS jsonb LANGUAGE sql AS $$ SELECT '{}'::jsonb $$")
        try:
            self.assertIn('missing_or_ambiguous_signature', self.failures())
        finally:
            self.conn.execute('DROP FUNCTION public.legacy_session_page(text)')
        self.assertTrue(self.check()['matches'])

    def test_security_path_owner_and_defaults_drift_fail_closed(self):
        signature = self.saved[2][0]
        for statement, code in [
            (f'ALTER FUNCTION {signature} SECURITY INVOKER', 'prosecdef_mismatch'),
            (f'ALTER FUNCTION {signature} SET search_path=public', 'proconfig_mismatch'),
            (f'ALTER FUNCTION {signature} OWNER TO service_role', 'owner_mismatch'),
            (self.saved[2][1].replace('DEFAULT 50','DEFAULT 25'), 'arguments_mismatch'),
        ]:
            with self.subTest(code=code):
                self.conn.execute(statement)
                self.assertIn(code,self.failures())
                self.restore()
                self.assertTrue(self.check()['matches'])
        self.assertFalse(check_history_schema(self.conn, expected_owner='service_role')['matches'])

    def test_busy_or_closed_connection_is_not_silently_reused(self):
        with self.conn.transaction():
            with self.assertRaises(ValueError): self.check()
        closed = psycopg.connect(self.dsn, autocommit=True)
        closed.close()
        with self.assertRaises(ValueError):
            check_history_schema(closed, expected_owner='postgres')


if __name__ == '__main__':
    unittest.main()
