"""Read-only verification of the staged069 Jev seed-operation RPC contract.

Checks only the reviewed function and permissions. Does not call the RPC,
apply migrations, inspect customer rows, or establish whole-schema readiness.
"""
from pathlib import Path

if __package__:
    from .rpc_schema_catalog import check_rpc_schema, reviewed_rpc_contracts, catalog_cli, run_cli
else:
    from rpc_schema_catalog import check_rpc_schema, reviewed_rpc_contracts, catalog_cli, run_cli

ROOT = Path(__file__).resolve().parents[2]
SCOPE = 'staged_jev_seed_scope_rpc_contract_069'
CONTRACTS = (
    ('069_jev_initial_seed_operation_scope.sql',
     '0ccf9a88203d197deef8263dfd75f72c42503731be4a75bb1f2e5c184e55fe26',
     'prepare_jev_pass', 'uuid,uuid,text,integer',
     'p_session_id uuid, p_run_id uuid, p_worker_id text, p_attempt_count integer',
     'plpgsql', 'v'),
)


def reviewed_contracts(migration_dir=ROOT / 'database/migrations'):
    return reviewed_rpc_contracts(CONTRACTS, migration_dir)


def check_jev_seed_scope_schema(conn, *, expected_owner, migration_dir=ROOT / 'database/migrations'):
    return check_rpc_schema(conn, expected_owner=expected_owner,
        contracts=reviewed_contracts(migration_dir), scope=SCOPE)


def main():
    return catalog_cli(check_jev_seed_scope_schema, description=__doc__)


if __name__ == '__main__':
    run_cli(check_jev_seed_scope_schema, description=__doc__, scope=SCOPE)
