"""Apply the entire reviewed pivot packet and its history atomically.

Dry-run by default. No connection strings or SQL error details reach output.
Completed files are accepted only when their recorded source is byte-identical.
An error stops the packet; this process never retries a failed commit.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus

if __package__:
    from .connection_target import validate_release_target
else:
    from connection_target import validate_release_target


ROOT = Path(__file__).resolve().parents[2]
LOCK = (724319, 32)


def source_body(source: str) -> str:
    """Only remove the two reviewed file-level transaction delimiters."""
    markers = list(re.finditer(r"(?m)^\s*(BEGIN|COMMIT|ROLLBACK|START TRANSACTION)\s*;\s*$", source))
    if len(markers) != 2 or [m.group(1) for m in markers] != ["BEGIN", "COMMIT"]:
        raise ValueError("Expected exactly one file-level BEGIN/COMMIT pair")
    if source[markers[-1].end():].strip():
        raise ValueError("Unexpected SQL after COMMIT")
    prefix = source[:markers[0].start()]
    if re.sub(r"(?m)^\s*--.*$", "", prefix).strip():
        raise ValueError("Unexpected SQL before BEGIN")
    return source[markers[0].end():markers[-1].start()]


def load_packet(manifest_path: Path, migration_dir: Path):
    manifest = json.loads(manifest_path.read_text())
    rows = manifest["migrations"]
    expected = [32, *range(34, 57)]
    if len(rows) != len(expected):
        raise ValueError("Unexpected migration count")
    packet = []
    for number, row in zip(expected, rows):
        name = row["file"]
        if not re.fullmatch(rf"{number:03d}_[a-z0-9_]+\.sql", name):
            raise ValueError("Unexpected migration order or filename")
        raw = (migration_dir / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ValueError("Reviewed migration hash mismatch")
        source = raw.decode("utf-8")
        packet.append({"name": name[:-4], "source": source,
                       "body": source_body(source), "sha256": row["sha256"]})
    return packet


def history_state(conn, migration, version):
    rows = conn.execute(
        "SELECT version,name,statements FROM supabase_migrations.schema_migrations "
        "WHERE version=%s OR name=%s", (version, migration["name"]),
    ).fetchall()
    if not rows:
        return "pending", version
    if len(rows) != 1 or rows[0][1] != migration["name"] or rows[0][2] != [migration["source"]]:
        raise ValueError("Existing migration history does not match the reviewed source")
    # A prior successful attempt may have used a different timestamp prefix.
    return "already_applied", rows[0][0]


def apply_one(conn, migration, version):
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Migration runner requires an idle autocommit connection")
    with conn.transaction():
        conn.execute("SET LOCAL lock_timeout='5s'")
        conn.execute("SET LOCAL statement_timeout='60s'")
        conn.execute("SELECT pg_advisory_xact_lock(%s,%s)", LOCK)
        # Keep queue insertion/claim/finalization out of this file's DDL window.
        # Between files, a newly arrived legacy job stops the next file cleanly.
        conn.execute("LOCK TABLE public.migration_sessions IN SHARE MODE")
        if conn.execute("SELECT count(*) FROM public.migration_sessions WHERE status IN ('pending','processing')").fetchone()[0]:
            raise ValueError("A legacy job arrived; stop the rollout until it drains")
        state, recorded_version = history_state(conn, migration, version)
        if state == "already_applied":
            return state, recorded_version
        conn.execute(migration["body"])
        conn.execute(
            "INSERT INTO supabase_migrations.schema_migrations(version,name,statements) VALUES(%s,%s,%s)",
            (version, migration["name"], [migration["source"]]),
        )
    # The transaction context has received COMMIT success before reporting it.
    return "applied", version


def plan_progress(conn, packet, version_prefix):
    plan = []
    seen_pending = False
    for migration in packet:
        proposed = version_prefix + f"{int(migration['name'][:3]):02d}"
        state, version = history_state(conn, migration, proposed)
        if state == "pending":
            seen_pending = True
        elif seen_pending:
            raise ValueError("Migration history is not a contiguous prefix of the reviewed packet")
        plan.append((migration, state, version))
    return plan


def apply_packet(conn, packet, version_prefix):
    """Expose no intermediate schema and keep legacy admission out until commit."""
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Migration runner requires an idle autocommit connection")
    records = []
    with conn.transaction():
        conn.execute("SET LOCAL lock_timeout='5s'")
        conn.execute("SET LOCAL statement_timeout='60s'")
        conn.execute("SELECT pg_advisory_xact_lock(%s,%s)", LOCK)
        conn.execute("LOCK TABLE public.migration_sessions,public.user_profiles IN SHARE MODE")
        if conn.execute("SELECT count(*) FROM public.migration_sessions WHERE status IN ('pending','processing')").fetchone()[0]:
            raise ValueError("Legacy work is active; stop until it drains")
        # Re-read the entire ledger while holding the serialization lock.
        plan = plan_progress(conn, packet, version_prefix)
        existing = conn.execute("SELECT to_regclass('public.migration_records') IS NOT NULL").fetchone()[0]
        if existing and plan[0][1] == 'pending':
            raise ValueError("Pivot schema exists without matching history")
        for migration, state, version in plan:
            if state == 'pending':
                conn.execute(migration['body'])
                conn.execute(
                    "INSERT INTO supabase_migrations.schema_migrations(version,name,statements) VALUES(%s,%s,%s)",
                    (version, migration['name'], [migration['source']]),
                )
                state = 'applied'
            records.append({'name': migration['name'], 'sha256': migration['sha256'],
                            'version': version, 'state': state})
        # PostgreSQL delivers this only if the whole packet commits.
        conn.execute("NOTIFY pgrst, 'reload schema'")
    return records


def preflight(conn):
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        owner, database, trigger, readonly = conn.execute(
            "SELECT current_user,current_database(),"
            "has_table_privilege(current_user,'auth.users','TRIGGER'),"
            "current_setting('transaction_read_only')"
        ).fetchone()
        active = conn.execute("SELECT count(*) FROM public.migration_sessions WHERE status IN ('pending','processing')").fetchone()[0]
        owned, missing_dates = conn.execute(
            "SELECT count(*),count(*) FILTER(WHERE s.created_at IS NULL) "
            "FROM public.migration_sessions s JOIN public.user_profiles p ON p.id::text=s.user_id"
        ).fetchone()
        history = conn.execute("SELECT name FROM supabase_migrations.schema_migrations WHERE name='033_internal_billing_rls'").fetchall()
    if owner != "postgres" or database != "postgres" or not trigger or readonly != "on":
        raise ValueError("Unexpected deployment-owner capabilities")
    if active or missing_dates or len(history) != 1:
        raise ValueError("Active jobs, missing timestamps, or missing 033 prerequisite")
    return {"active_jobs": active, "owned_legacy_sessions": owned, "null_timestamps": missing_dates}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-file", type=Path, required=True)
    parser.add_argument("--ca-file", type=Path, required=True)
    parser.add_argument("--target-file", type=Path, required=True, help="Reviewed preflight JSON containing connection_target")
    parser.add_argument("--manifest", type=Path, default=ROOT / "docs/release-evidence/application-schema-rehearsal.json")
    parser.add_argument("--version-prefix", default=datetime.now(timezone.utc).strftime("%Y%m%d%H%M"))
    parser.add_argument("--journal", type=Path, help="New exclusive local JSONL evidence file; required for apply")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]{12}", args.version_prefix):
        raise ValueError("Version prefix must contain 12 digits")
    packet = load_packet(args.manifest, ROOT / "database/migrations")
    if args.dsn_file.stat().st_mode & 0o077:
        raise ValueError("Credential file must be private (0600)")
    dsn = args.dsn_file.read_text().strip()
    expected = json.loads(args.target_file.read_text())['connection_target']
    validate_release_target(dsn, expected)
    if args.apply and not args.journal:
        raise ValueError("Apply requires a new journal file")
    journal = None
    try:
        if args.apply:
            fd = os.open(args.journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            journal = os.fdopen(fd, "w")
        with psycopg.connect(dsn, autocommit=True, sslmode="verify-full", sslrootcert=str(args.ca_file),
                             connect_timeout=15, prepare_threshold=None) as conn:
            print(json.dumps({"preflight": preflight(conn), "apply": args.apply}), flush=True)
            plan = plan_progress(conn, packet, args.version_prefix)
            existing = conn.execute("SELECT to_regclass('public.migration_records') IS NOT NULL").fetchone()[0]
            if existing and plan[0][1] == "pending":
                raise ValueError("Pivot schema exists without its matching migration history")
            if args.apply:
                records = apply_packet(conn, packet, args.version_prefix)
            else:
                records = [{"name": m['name'], "sha256": m['sha256'], "version": v, "state": s}
                           for m, s, v in plan]
            for record in records:
                if journal:
                    journal.write(json.dumps(record) + "\n")
                    journal.flush()
                    os.fsync(journal.fileno())
                print(json.dumps(record), flush=True)
            if args.apply:
                print(json.dumps({"packet_committed": True, "schema_cache_reload_requested": True}), flush=True)
    finally:
        if journal:
            journal.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # libpq/DDL errors may contain connection strings or customer values.
        print(json.dumps({"failed": True, "error_type": type(exc).__name__, "sqlstate": getattr(exc, "sqlstate", None),
                          "message": "Stopped without retry. Inspect migration history before resuming; error details withheld."}), flush=True)
        raise SystemExit(1)
