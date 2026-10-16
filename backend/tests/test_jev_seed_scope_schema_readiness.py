"""Catalog evidence for069; native fixtures never contact providers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

import psycopg

from backend.tests import test_jev_pipeline as jev_fixture
from backend.tests.test_migration_planning import ROOT
from backend.services.jev_store import DurableStore
from scripts.release.check_jev_seed_scope_schema import (
    check_jev_seed_scope_schema, reviewed_contracts, SCOPE,
)


class ReviewedSeedScopeSources(unittest.TestCase):
    def test_exact_source_and_separate_release_scopes(self):
        from scripts.release.check_history_schema import reviewed_contracts as history
        from scripts.release.check_gsc_disconnect_schema import reviewed_contracts as disconnect
        from scripts.release.apply_pivot_migrations import load_packet
        self.assertEqual([r['migration'][:3] for r in reviewed_contracts()], ['069'])
        self.assertEqual([r['migration'][:3] for r in history()], ['065', '066', '067'])
        self.assertEqual([r['migration'][:3] for r in disconnect()], ['068'])
        old = load_packet(ROOT/'docs/release-evidence/application-schema-rehearsal.json', ROOT/'database/migrations')
        self.assertEqual([int(r['name'][:3]) for r in old], [32, *range(34, 57)])
        with tempfile.TemporaryDirectory() as directory:
            name = reviewed_contracts()[0]['migration']
            Path(directory, name).write_bytes((ROOT/'database/migrations'/name).read_bytes() + b'\n-- changed\n')
            with self.assertRaises(ValueError): reviewed_contracts(Path(directory))

    def test_cli_failure_is_nonzero_and_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory, 'dsn')
            credential.write_text('password=private-fixture invalid-fragment')
            credential.chmod(0o600)
            result = subprocess.run([sys.executable, str(ROOT/'scripts/release/check_jev_seed_scope_schema.py'),
                '--dsn-file', str(credential), '--ca-file', str(Path(directory, 'unused-ca')),
                '--target-file', str(Path(directory, 'unused-target')), '--expected-owner', 'postgres'],
                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        output = json.loads(result.stdout)
        self.assertEqual(output['scope'], SCOPE)
        self.assertFalse(output['matches'])
        self.assertEqual(result.stderr, '')
        self.assertNotIn('private-fixture', result.stdout)
        self.assertNotIn('invalid-fragment', result.stdout)


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'requires disposable loopback PostgreSQL')
class SeedScopeSchemaReadiness(unittest.TestCase):
    setUpClass = classmethod(jev_fixture.NativeJev.setUpClass.__func__)
    cleanup_db = classmethod(jev_fixture.NativeJev.cleanup_db.__func__)
    sql = jev_fixture.NativeJev.sql
    fixture = jev_fixture.NativeJev.fixture
    claim = jev_fixture.NativeJev.claim

    def setUp(self):
        jev_fixture.NativeJev.setUp(self)
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(self.conn.close)
        self.signature = reviewed_contracts()[0]['signature']
        self.definition = self.conn.execute('SELECT pg_get_functiondef(%s::regprocedure)', [self.signature]).fetchone()[0]
        self.addCleanup(self.restore)

    def restore(self):
        self.conn.execute('RESET ROLE')
        self.conn.execute(self.definition)
        self.conn.execute(f'REVOKE ALL ON FUNCTION {self.signature} FROM PUBLIC,anon,authenticated,service_role')
        self.conn.execute(f'GRANT EXECUTE ON FUNCTION {self.signature} TO service_role')

    def apply_fix(self):
        self.conn.execute((ROOT/'database/migrations/069_jev_initial_seed_operation_scope.sql').read_text())

    def check(self):
        return check_jev_seed_scope_schema(self.conn, expected_owner='postgres')

    def snapshot(self):
        return [self.conn.execute(f'SELECT tableoid,xmin,ctid,* FROM {table} ORDER BY ctid').fetchall()
                for table in ('jev_runs', 'migration_operations', 'migration_mapping_decisions',
                              'migration_mapping_decision_events', 'url_mappings')]

    def test_062_body_fails_then_069_passes_without_reading_or_mutating_confirmations(self):
        before = self.check()
        self.assertFalse(before['matches'])
        self.assertEqual(before['checks'][0]['failures'], ['body_mismatch'])
        self.apply_fix()
        f = self.fixture()
        run = self.service.start(f['user'], f['migration'], f['old'], f['new'], f['quote'], uuid4().hex,
            [{'old_url':'https://old.example/Page/0?q=A', 'new_url':'https://new.example/Page/1?q=A'}])
        job = self.claim()
        self.runs.authorize_dispatch(job, 'worker-test')
        store = DurableStore(self.native, job, 'worker-test')
        state = store.rpc('prepare_jev_pass')
        self.assertEqual(state['seed_revision'], 1)
        self.assertEqual(self.sql('SELECT count(*) n FROM migration_mapping_decision_events WHERE run_id=%s',
                                 [run['run_id']])[0]['n'], 1)
        rows = self.snapshot()
        auditor = 'fixture_seed_auditor_' + uuid4().hex
        self.conn.execute(f'CREATE ROLE {auditor} NOLOGIN NOINHERIT')
        try:
            self.conn.execute(f'SET ROLE {auditor}')
            self.assertFalse(self.conn.execute("SELECT has_function_privilege(current_user,%s,'EXECUTE')", [self.signature]).fetchone()[0])
            self.assertFalse(self.conn.execute("SELECT has_table_privilege(current_user,'jev_runs','SELECT')").fetchone()[0])
            result = self.check()
            self.assertTrue(result['matches'], result)
            self.assertTrue(result['read_only'])
            self.assertEqual(result['scope'], SCOPE)
            self.assertEqual(len(result['checks']), 1)
        finally:
            self.conn.execute('RESET ROLE')
            self.conn.execute(f'DROP ROLE {auditor}')
        self.assertEqual(self.snapshot(), rows)
        self.assertEqual(store.rpc('prepare_jev_pass'), state)
        self.assertNotIn(run['run_id'], json.dumps(result))

    def test_replacement_preserves_bad_grants_but_catalog_check_rejects_them(self):
        # CREATE OR REPLACE correctly preserves ACLs; it cannot repair preexisting drift.
        self.conn.execute(f'GRANT EXECUTE ON FUNCTION {self.signature} TO authenticated')
        self.apply_fix()
        result = self.check()
        self.assertFalse(result['matches'])
        self.assertIn('browser_execute_mismatch', result['checks'][0]['failures'])
        self.assertIn('unexpected_execute_grant', result['checks'][0]['failures'])
        self.conn.execute(f'REVOKE EXECUTE ON FUNCTION {self.signature} FROM authenticated')
        self.assertTrue(self.check()['matches'])
        self.conn.execute(f'REVOKE EXECUTE ON FUNCTION {self.signature} FROM service_role')
        self.assertIn('service_execute_mismatch', self.check()['checks'][0]['failures'])


if __name__ == '__main__': unittest.main()
