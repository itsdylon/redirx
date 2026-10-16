"""Shared read-only catalog comparison for explicitly pinned RPC contracts.

Callers supply a fixed reviewed source allowlist and separate scope. This module
never discovers migrations, invokes a checked RPC, or applies schema changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row


if __package__:
    from .connection_target import validate_release_target
else:
    from connection_target import validate_release_target


def reviewed_rpc_contracts(definitions, migration_dir):
    contracts = []
    for filename, digest, name, types, arguments, language, volatility in definitions:
        raw = (migration_dir / filename).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Reviewed migration source changed')
        # The explicitly pinned files each contain exactly one dollar-quoted body.
        parts = raw.decode('utf-8').split('$$')
        if len(parts) != 3:
            raise ValueError('Unexpected reviewed function body')
        contracts.append({'migration': filename, 'sha256': digest, 'name': name,
            'signature': f'public.{name}({types})', 'arguments': arguments,
            'language': language, 'volatility': volatility, 'body': parts[1]})
    return contracts


def check_rpc_schema(conn, *, expected_owner, contracts, scope):
    """Return finite diagnostic codes in a repeatable-read catalog transaction.

    The owner is an explicit reviewed deployment prerequisite, not guessed from
    the local fixture. Unknown failures raise; callers must not report success.
    """
    if not isinstance(expected_owner, str) or not expected_owner or len(expected_owner) > 63 or '\x00' in expected_owner:
        raise ValueError('A reviewed function owner is required')
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError('An idle autocommit connection is required')
    checks = []
    with conn.transaction():
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.execute("SET LOCAL statement_timeout='10s'")
        # Catalog rendering must not vary with a caller's search_path.
        conn.execute('SET LOCAL search_path=pg_catalog')
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT current_setting('transaction_read_only') AS read_only, current_setting('server_version_num') AS server_version_num")
            metadata = cur.fetchone()
            cur.execute('SELECT rolname FROM pg_roles WHERE rolname=ANY(%s)',
                        [[expected_owner, 'service_role', 'anon', 'authenticated']])
            roles = {row['rolname'] for row in cur.fetchall()}
            missing_roles = not {expected_owner, 'service_role', 'anon', 'authenticated'} <= roles
            for contract in contracts:
                failures = []
                cur.execute('''SELECT p.oid, pg_get_userbyid(p.proowner) AS owner,
                    pg_get_function_arguments(p.oid) AS arguments,
                    pg_get_function_result(p.oid) AS result, l.lanname AS language,
                    p.prosrc AS body, p.prosecdef, p.provolatile, p.proisstrict,
                    p.proleakproof, p.proparallel, p.prokind, p.proconfig, p.prosupport::oid AS prosupport
                    FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang
                    WHERE p.oid=to_regprocedure(%s)''', [contract['signature']])
                row = cur.fetchone()
                cur.execute('''SELECT count(*) AS n FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                    WHERE n.nspname='public' AND p.proname=%s''', [contract['name']])
                if cur.fetchone()['n'] != 1:
                    failures.append('missing_or_ambiguous_signature')
                if missing_roles:
                    failures.append('required_role_missing')
                if row is None:
                    failures.append('function_missing')
                else:
                    expected = {'owner': expected_owner, 'arguments': contract['arguments'],
                        'result': 'jsonb', 'language': contract['language'], 'body': contract['body'],
                        'prosecdef': True, 'provolatile': contract['volatility'], 'proisstrict': False,
                        'proleakproof': False, 'proparallel': 'u', 'prokind': 'f',
                        'proconfig': ['search_path=public, pg_temp'], 'prosupport': 0}
                    for key, value in expected.items():
                        if row[key] != value:
                            failures.append(key + '_mismatch')
                    # Effective checks include role inheritance and PUBLIC, unlike
                    # merely inspecting named ACL entries. Also reject extra direct
                    # grantees/PUBLIC and service-role grant options.
                    if not missing_roles:
                        cur.execute("""SELECT
                            has_schema_privilege('service_role','public','USAGE') AS schema_usage,
                            has_function_privilege('service_role',%s,'EXECUTE') AS service_execute,
                            has_function_privilege('anon',%s,'EXECUTE') AS anon_execute,
                            has_function_privilege('authenticated',%s,'EXECUTE') AS browser_execute""", [row['oid']]*3)
                        grants = cur.fetchone()
                        for key, value in {'schema_usage': True, 'service_execute': True,
                                           'anon_execute': False, 'browser_execute': False}.items():
                            if grants[key] != value:
                                failures.append(key + '_mismatch')
                    cur.execute('''SELECT count(*) AS n FROM pg_proc p,
                        LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
                        LEFT JOIN pg_roles r ON r.oid=a.grantee
                        WHERE p.oid=%s AND (a.grantee=0 OR
                          (a.grantee<>p.proowner AND (r.rolname IS DISTINCT FROM 'service_role' OR a.is_grantable)))''', [row['oid']])
                    if cur.fetchone()['n']:
                        failures.append('unexpected_execute_grant')
                checks.append({'migration': contract['migration'], 'source_sha256': contract['sha256'],
                    'signature': contract['signature'], 'matches': not failures, 'failures': failures})
    return {'scope': scope,
        'matches': all(row['matches'] for row in checks), 'read_only': metadata['read_only'] == 'on',
        'server_version_num': metadata['server_version_num'], 'checks': checks}


def catalog_cli(checker, *, description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--dsn-file', type=Path, required=True)
    parser.add_argument('--ca-file', type=Path, required=True)
    parser.add_argument('--target-file', type=Path, required=True,
                        help='Reviewed JSON containing connection_target')
    parser.add_argument('--expected-owner', required=True, help='Reviewed owner of these SECURITY DEFINER functions')
    args = parser.parse_args()
    if args.dsn_file.stat().st_mode & 0o077:
        raise ValueError('Credential file must be private (0600)')
    dsn = args.dsn_file.read_text().strip()
    expected = json.loads(args.target_file.read_text())['connection_target']
    validate_release_target(dsn, expected)
    with psycopg.connect(dsn, autocommit=True, sslmode='verify-full', sslrootcert=str(args.ca_file),
                         connect_timeout=15, prepare_threshold=None) as conn:
        result = checker(conn, expected_owner=args.expected_owner)
    print(json.dumps(result), flush=True)
    return 0 if result['matches'] and result['read_only'] else 1


def run_cli(checker, *, description, scope):
    try:
        raise SystemExit(catalog_cli(checker, description=description))
    except Exception as exc:
        # No DSNs, supplied function bodies, credentials or database error text.
        print(json.dumps({'scope': scope, 'matches': False,
            'error_type': type(exc).__name__, 'sqlstate': getattr(exc, 'sqlstate', None),
            'message': 'Check failed without retry; inspect the reviewed schema prerequisites.'}), flush=True)
        raise SystemExit(1)
