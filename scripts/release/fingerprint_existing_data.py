"""Read-only aggregate fingerprints of existing public tables for a release.

No row contents leave PostgreSQL. Compare only the original columns so additive
schema changes do not create false mismatches. These hashes are evidence, not a
backup or a writer pause; any mismatch needs investigation before activation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import psycopg
from psycopg import sql


if __package__:
    from .connection_target import validate_release_target
else:
    from connection_target import validate_release_target


def fingerprint_tables(conn, baseline=None):
    catalog = conn.execute(
        "SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid=c.oid "
        "WHERE n.nspname='public' AND c.relkind IN ('r','p') "
        "AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum"
    ).fetchall()
    available = {}
    for table, column, datatype in catalog:
        available.setdefault(table, []).append([column, datatype])
    requested = available if baseline is None else {
        table: entry['columns'] for table, entry in baseline['tables'].items()
    }
    result = {}
    for table, columns in requested.items():
        current = dict(available.get(table, []))
        if not columns or any(current.get(name) != datatype for name, datatype in columns):
            raise ValueError('An original table or column is absent or changed type')
        query = sql.SQL(
            "SELECT count(*),encode(sha256(convert_to(COALESCE("
            "string_agg(row_hash,'' ORDER BY row_hash),''),'UTF8')),'hex') "
            "FROM (SELECT encode(sha256(convert_to(row_to_json(ROW({}))::text,"
            "'UTF8')),'hex') AS row_hash FROM public.{}) AS row_hashes"
        ).format(sql.SQL(',').join(sql.Identifier(name) for name, _ in columns), sql.Identifier(table))
        count, digest = conn.execute(query).fetchone()
        result[table] = {'columns': columns, 'rows': count, 'sha256': digest}
    return result


def mismatched_tables(before, after):
    return sorted(table for table, expected in before['tables'].items()
                  if after['tables'].get(table) != expected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dsn-file', type=Path, required=True)
    parser.add_argument('--ca-file', type=Path, required=True)
    parser.add_argument('--target-file', type=Path, required=True)
    parser.add_argument('--before', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.dsn_file.stat().st_mode & 0o077:
        raise ValueError('Credential file must be private')
    dsn = args.dsn_file.read_text().strip()
    expected = json.loads(args.target_file.read_text())['connection_target']
    validate_release_target(dsn, expected)
    baseline = json.loads(args.before.read_text()) if args.before else None
    # Exclusive output preflight before database work; credentials never go here.
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as out:
        with psycopg.connect(dsn, autocommit=True, sslmode='verify-full', sslrootcert=str(args.ca_file),
                             connect_timeout=15, prepare_threshold=None) as conn:
            with conn.transaction():
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                conn.execute("SET LOCAL statement_timeout='60s'")
                conn.execute("SET LOCAL lock_timeout='5s'")
                conn.execute("SET LOCAL timezone='UTC'")
                conn.execute("SET LOCAL extra_float_digits=3")
                data = {'observed_at': datetime.now(timezone.utc).isoformat(),
                        'transaction_read_only': conn.execute("SHOW transaction_read_only").fetchone()[0],
                        'tables': fingerprint_tables(conn, baseline)}
        differences = mismatched_tables(baseline, data) if baseline else []
        data['mismatched_tables'] = differences
        json.dump(data, out, indent=2)
        out.write('\n')
        out.flush()
        os.fsync(out.fileno())
    print(json.dumps({'read_only': data['transaction_read_only'], 'tables': len(data['tables']),
                      'rows': sum(t['rows'] for t in data['tables'].values()),
                      'compared': baseline is not None, 'mismatched_tables': differences}))
    return 1 if differences else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'failed': True, 'error_type': type(exc).__name__,
                          'sqlstate': getattr(exc, 'sqlstate', None), 'message': 'Read-only fingerprint failed; details withheld.'}))
        raise SystemExit(1)
