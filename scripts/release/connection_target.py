"""Offline destination validation for explicitly reviewed release connections.

This is not a network probe or a production authorization. Call immediately
before opening a connection; do not mutate the process environment to make it pass.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

from psycopg.conninfo import conninfo_to_dict

# Even an explicit host does not override the separate PGHOSTADDR parameter.
# Services can populate unspecified fields from per-user or system files.
ROUTING_ENVIRONMENT = (
    'PGHOST', 'PGHOSTADDR', 'PGPORT', 'PGDATABASE', 'PGUSER',
    'PGSERVICE', 'PGSERVICEFILE', 'PGSYSCONFDIR',
)
TARGET_KEYS = frozenset(('host', 'port', 'user', 'dbname'))


def validate_release_target(dsn: str, expected: Mapping[str, str], *, environment=None) -> None:
    """Require one explicit reviewed TCP host and no ambient routing inputs.

    Password/authentication fields and non-routing connection options are left
    intact. Callers continue to force their reviewed TLS/CA and timeout options.
    Errors deliberately omit all connection strings and target values.
    """
    environment = os.environ if environment is None else environment
    if any(environment.get(key) for key in ROUTING_ENVIRONMENT):
        raise ValueError('Inherited release connection routing is unsupported')
    if not isinstance(expected, Mapping) or set(expected) != TARGET_KEYS or any(
        not isinstance(value, str) or not value or '\x00' in value for value in expected.values()
    ):
        raise ValueError('An explicit reviewed connection target is required')
    host, port = expected['host'], expected['port']
    if host.startswith(('/', '@')) or ',' in host or '/' in host or any(c.isspace() for c in host):
        raise ValueError('A single reviewed TCP host is required')
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError('A single explicit reviewed TCP port is required')
    try:
        supplied = conninfo_to_dict(dsn)
    except Exception:
        raise ValueError('Invalid release connection parameters') from None
    if 'hostaddr' in supplied or 'service' in supplied:
        raise ValueError('Release connection target override is unsupported')
    if any(supplied.get(key) != expected[key] for key in TARGET_KEYS):
        raise ValueError('Connection target differs from reviewed target')
