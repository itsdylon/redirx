"""Read-only catalog verification of staged GSC disconnect RPC 068 only.

Checks the reviewed function definition and authority; does not invoke the
secret-bearing RPC, inspect credential rows, apply SQL, or retry.
"""
from pathlib import Path

if __package__:
    from .rpc_schema_catalog import check_rpc_schema, reviewed_rpc_contracts, catalog_cli, run_cli
else:
    from rpc_schema_catalog import check_rpc_schema, reviewed_rpc_contracts, catalog_cli, run_cli

ROOT = Path(__file__).resolve().parents[2]
SCOPE = 'staged_gsc_disconnect_rpc_contract_068'
CONTRACTS = (
    ('068_atomic_legacy_gsc_disconnect.sql',
     '69d4d9b7ac0d361405da1feab9f11d802a3f294fceb7e3ec9e186c9305561a99',
     'disconnect_legacy_gsc_connection', 'uuid', 'p_user_id uuid', 'plpgsql', 'v'),
)


def reviewed_contracts(migration_dir=ROOT / 'database/migrations'):
    return reviewed_rpc_contracts(CONTRACTS, migration_dir)


def check_gsc_disconnect_schema(conn, *, expected_owner, migration_dir=ROOT / 'database/migrations'):
    return check_rpc_schema(conn, expected_owner=expected_owner,
        contracts=reviewed_contracts(migration_dir), scope=SCOPE)


def main():
    return catalog_cli(check_gsc_disconnect_schema, description=__doc__)


if __name__ == '__main__':
    run_cli(check_gsc_disconnect_schema, description=__doc__, scope=SCOPE)
