"""Atomic release SQL/history and safe replay against disposable native PostgreSQL."""
import hashlib
import os
from pathlib import Path
import unittest
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from backend.tests.helpers.disposable_postgres import validate_disposable_postgres_dsn

from scripts.release.apply_pivot_migrations import ROOT, apply_one, apply_packet, history_state, load_packet, plan_progress, source_body
from scripts.release.fingerprint_existing_data import fingerprint_tables, mismatched_tables


class ReviewedPacket(unittest.TestCase):
    def test_all_reviewed_files_have_valid_hashes_and_single_transaction(self):
        packet = load_packet(ROOT / "docs/release-evidence/application-schema-rehearsal.json", ROOT / "database/migrations")
        self.assertEqual(len(packet), 24)
        self.assertEqual(packet[0]["name"], "032_durable_migrations")
        self.assertEqual(packet[-1]["name"], "056_exact_long_url_storage")

    def test_transaction_escape_or_unwrapped_sql_is_rejected(self):
        for source in ("SELECT 1; BEGIN; SELECT 2; COMMIT;", "BEGIN;\nCOMMIT;\nSELECT 3;",
                       "BEGIN;\nCREATE TABLE a(id int);\nCOMMIT;\nBEGIN;\nCOMMIT;"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                source_body(source)


@unittest.skipUnless(os.getenv("PREFLIGHT_TEST_DATABASE_URL"), "requires disposable local PostgreSQL")
class AtomicMigrationHistory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_dsn = os.environ["PREFLIGHT_TEST_DATABASE_URL"]
        validate_disposable_postgres_dsn(cls.admin_dsn)
        cls.database = "redirx_release_test_" + uuid4().hex
        with psycopg.connect(cls.admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(cls.cleanup_db)
        cls.dsn = make_conninfo(cls.admin_dsn, dbname=cls.database)
        with psycopg.connect(cls.dsn, autocommit=True) as conn:
            conn.execute("CREATE SCHEMA supabase_migrations; CREATE TABLE supabase_migrations.schema_migrations "
                         "(version text PRIMARY KEY,name text,statements text[],CHECK(name<>'036_history_failure'))")
            conn.execute("CREATE TABLE public.migration_sessions(id int PRIMARY KEY,status text)")
            conn.execute("CREATE TABLE public.user_profiles(id int PRIMARY KEY)")

    @classmethod
    def cleanup_db(cls):
        with psycopg.connect(cls.admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(cls.database)))

    def setUp(self):
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(self.conn.close)

    @staticmethod
    def migration(name, body):
        source = "-- reviewed fixture\nBEGIN;\n" + body + "\nCOMMIT;\n"
        return {"name": name, "source": source, "body": source_body(source),
                "sha256": hashlib.sha256(source.encode()).hexdigest()}

    def absent(self, table, name):
        self.assertIsNone(self.conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM supabase_migrations.schema_migrations WHERE name=%s", (name,)).fetchone()[0], 0)

    def test_apply_commits_schema_and_exact_source_and_retry_reports_original_version(self):
        migration = self.migration("032_success", "CREATE TABLE release_success(id int);")
        self.assertEqual(apply_one(self.conn, migration, "20260920010032"), ("applied", "20260920010032"))
        self.assertIsNotNone(self.conn.execute("SELECT to_regclass('release_success')").fetchone()[0])
        self.assertEqual(apply_one(self.conn, migration, "20260920020032"), ("already_applied", "20260920010032"))
        self.assertEqual(self.conn.execute("SELECT statements FROM supabase_migrations.schema_migrations WHERE name=%s", (migration["name"],)).fetchone()[0], [migration["source"]])

    def test_sql_failure_rolls_back_ddl_and_leaves_no_history(self):
        migration = self.migration("034_sql_failure", "CREATE TABLE release_sql_failure(id int); SELECT 1/0;")
        with self.assertRaises(psycopg.errors.DivisionByZero):
            apply_one(self.conn, migration, "20260920010034")
        self.absent("release_sql_failure", migration["name"])

    def test_history_failure_also_rolls_back_completed_ddl(self):
        migration = self.migration("036_history_failure", "CREATE TABLE release_history_failure(id int);")
        with self.assertRaises(psycopg.errors.CheckViolation):
            apply_one(self.conn, migration, "20260920010036")
        self.absent("release_history_failure", migration["name"])

    def test_changed_source_cannot_reuse_recorded_name(self):
        old = self.migration("037_changed", "CREATE TABLE release_original(id int);")
        apply_one(self.conn, old, "20260920010037")
        changed = self.migration("037_changed", "CREATE TABLE release_changed(id int);")
        with self.assertRaises(ValueError):
            apply_one(self.conn, changed, "20260920020037")
        self.assertIsNone(self.conn.execute("SELECT to_regclass('release_changed')").fetchone()[0])

    def test_reading_dry_run_state_does_not_execute_sql(self):
        migration = self.migration("038_dry", "CREATE TABLE release_dry(id int);")
        self.assertEqual(history_state(self.conn, migration, "20260920010038"), ("pending", "20260920010038"))
        self.absent("release_dry", migration["name"])

    def test_version_collision_aborts_before_sql(self):
        first = self.migration("039_first", "CREATE TABLE release_first(id int);")
        apply_one(self.conn, first, "20260920010039")
        collision = self.migration("040_collision", "CREATE TABLE release_collision(id int);")
        with self.assertRaises(ValueError):
            apply_one(self.conn, collision, "20260920010039")
        self.absent("release_collision", collision["name"])

    def test_noncontiguous_history_is_rejected(self):
        first = self.migration("041_gap", "SELECT 1;")
        later = self.migration("042_already", "SELECT 2;")
        apply_one(self.conn, later, "20260920010042")
        with self.assertRaises(ValueError):
            plan_progress(self.conn, [first, later], "202609200200")

    def test_new_queue_work_stops_before_ddl_or_history(self):
        migration = self.migration("043_active", "CREATE TABLE release_active(id int);")
        self.conn.execute("INSERT INTO migration_sessions VALUES(1,'pending')")
        try:
            with self.assertRaises(ValueError):
                apply_one(self.conn, migration, "20260920010043")
            self.absent("release_active", migration["name"])
        finally:
            self.conn.execute("DELETE FROM migration_sessions WHERE id=1")

    def test_whole_packet_failure_rolls_back_earlier_file_and_history(self):
        first = self.migration('044_packet_first', 'CREATE TABLE release_packet_first(id int);')
        last = self.migration('045_packet_failure', 'SELECT 1/0;')
        with self.assertRaises(psycopg.errors.DivisionByZero):
            apply_packet(self.conn, [first, last], '202609200300')
        self.absent('release_packet_first', first['name'])
        self.assertEqual(history_state(self.conn, last, '20260920030045')[0], 'pending')

    def test_whole_packet_success_commits_all_files_and_replays_exactly(self):
        first = self.migration('046_packet_ok', 'CREATE TABLE release_packet_ok(id int);')
        last = self.migration('047_packet_ok', 'INSERT INTO release_packet_ok VALUES(9);')
        records = apply_packet(self.conn, [first, last], '202609200300')
        self.assertEqual([r['state'] for r in records], ['applied', 'applied'])
        self.assertEqual(self.conn.execute('SELECT id FROM release_packet_ok').fetchall(), [(9,)])
        self.assertEqual([r['state'] for r in apply_packet(self.conn, [first,last], '202609200400')],
                         ['already_applied', 'already_applied'])

    def test_packet_keeps_intermediate_schema_hidden_and_legacy_writes_locked(self):
        first = self.migration('048_visibility', 'CREATE TABLE release_not_visible(id int);')
        last = self.migration('049_visibility', 'SELECT pg_sleep(0.5);')
        def apply():
            with psycopg.connect(self.dsn, autocommit=True, application_name='release_visibility_fixture') as conn:
                return apply_packet(conn,[first,last],'202609200500')
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(apply)
            deadline = time.monotonic()+5
            while time.monotonic()<deadline:
                sleeping = self.conn.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name='release_visibility_fixture' AND wait_event='PgSleep'").fetchone()[0]
                if sleeping:
                    break
                time.sleep(0.01)
            self.assertEqual(sleeping,1,'Packet must positively reach its second file before probing')
            self.absent('release_not_visible',first['name'])
            with self.conn.transaction():
                self.conn.execute("SET LOCAL lock_timeout='50ms'")
                with self.assertRaises(psycopg.errors.LockNotAvailable):
                    with self.conn.transaction():
                        self.conn.execute("INSERT INTO migration_sessions VALUES(99,'pending')")
            self.assertEqual([r['state'] for r in future.result(timeout=5)],['applied','applied'])
        self.assertIsNotNone(self.conn.execute("SELECT to_regclass('release_not_visible')").fetchone()[0])

    def test_fingerprint_ignores_additive_columns_but_detects_edits_and_duplicates(self):
        self.conn.execute('CREATE TABLE release_fingerprint(id int,value text)')
        self.conn.execute("INSERT INTO release_fingerprint VALUES(1,'private fixture'),(2,NULL)")
        before = {'tables': fingerprint_tables(self.conn)}
        self.conn.execute('ALTER TABLE release_fingerprint ADD COLUMN added text DEFAULT \'new\'')
        self.assertEqual(mismatched_tables(before, {'tables': fingerprint_tables(self.conn,before)}), [])
        self.conn.execute("UPDATE release_fingerprint SET value='changed' WHERE id=1")
        after = {'tables': fingerprint_tables(self.conn,before)}
        self.assertEqual(mismatched_tables(before,after), ['release_fingerprint'])
        self.conn.execute("UPDATE release_fingerprint SET value='private fixture' WHERE id=1")
        self.conn.execute('INSERT INTO release_fingerprint(id,value) SELECT id,value FROM release_fingerprint WHERE id=2')
        self.assertEqual(mismatched_tables(before, {'tables': fingerprint_tables(self.conn,before)}), ['release_fingerprint'])

    def test_fingerprint_rejects_missing_original_column(self):
        self.conn.execute('CREATE TABLE release_fingerprint_drop(id int,value text)')
        before = {'tables': fingerprint_tables(self.conn)}
        self.conn.execute('ALTER TABLE release_fingerprint_drop DROP COLUMN value')
        with self.assertRaises(ValueError):
            fingerprint_tables(self.conn,before)


if __name__ == "__main__":
    unittest.main()
