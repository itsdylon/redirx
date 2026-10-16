"""Read-only catalog verification of staged history RPCs 065–067, not deployment.

No migration discovery, application, RPC invocation or automatic retry. A matching
ledger alone is not proof that current function definitions and grants are intact.
"""
from pathlib import Path

if __package__:
    from .rpc_schema_catalog import check_rpc_schema, reviewed_rpc_contracts, catalog_cli, run_cli
else:
    from rpc_schema_catalog import check_rpc_schema, reviewed_rpc_contracts, catalog_cli, run_cli

ROOT = Path(__file__).resolve().parents[2]
SCOPE = 'staged_history_rpc_contracts_065_067'
# Explicit reviewed source allowlist. Never enumerate through deferred 059–061.
CONTRACTS = (
    ('065_atomic_legacy_session_delete.sql',
     '5feed830c62eb88dbf7085c59d553e22262f808b4c254edc1b0eafad84b89f52',
     'delete_owned_legacy_session', 'uuid,uuid', 'p_user_id uuid, p_session_id uuid', 'plpgsql', 'v'),
    ('066_legacy_dashboard_snapshot.sql',
     '7fe00809da0368c1303d0a50a5291b85018862ff875801d9696d883440f184fd',
     'legacy_dashboard_snapshot', 'uuid', 'p_user_id uuid', 'sql', 's'),
    ('067_legacy_session_pages.sql',
     '01ef94a9f5699c74cb9aa0e0a4b67925ecac34926fbf45998777fa77057151bb',
     'legacy_session_page', 'uuid,text,timestamp with time zone,uuid,integer',
     "p_user_id uuid, p_search text DEFAULT ''::text, p_after_created_at timestamp with time zone DEFAULT NULL::timestamp with time zone, p_after_id uuid DEFAULT NULL::uuid, p_limit integer DEFAULT 50", 'plpgsql', 's'),
)


def reviewed_contracts(migration_dir=ROOT / 'database/migrations'):
    return reviewed_rpc_contracts(CONTRACTS, migration_dir)


def check_history_schema(conn, *, expected_owner, migration_dir=ROOT / 'database/migrations'):
    return check_rpc_schema(conn, expected_owner=expected_owner,
        contracts=reviewed_contracts(migration_dir), scope=SCOPE)


def main():
    return catalog_cli(check_history_schema, description=__doc__)


if __name__ == '__main__':
    run_cli(check_history_schema, description=__doc__, scope=SCOPE)
