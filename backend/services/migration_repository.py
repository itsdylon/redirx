"""Ownership-safe repository for the durable migration records.

This module deliberately contains no business workflow.  It is the narrow
database boundary shared by the migration services: every read is scoped to
the caller, and reservation is delegated to the transactional SQL function.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from src.redirx.database import SupabaseClient

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 500
_UUID_COLUMNS = {
    "migration_records": {"id", "user_id"},
    "inventory_snapshots": {"id", "migration_id", "user_id"},
    "migration_runs": {
        "id", "migration_id", "user_id", "old_inventory_id", "new_inventory_id",
        "legacy_session_id", "rerun_of",
    },
    "migration_artifacts": {"id", "migration_id", "user_id", "run_id"},
    "migration_operations": {"id", "migration_id", "user_id"},
    # session_id is a legacy UUID; id remains a BIGSERIAL cursor.
    "session_discovered_urls": {"inventory_id", "session_id"},
}


class MigrationRepositoryError(Exception):
    """Base error with a stable safe code for callers."""

    code = "internal_error"
    retryable = False


class InvalidInputError(MigrationRepositoryError):
    code = "invalid_input"


class MigrationNotFoundError(MigrationRepositoryError):
    code = "not_found"


class OperationConflictError(MigrationRepositoryError):
    code = "operation_conflict"


class RepositoryUnavailableError(MigrationRepositoryError):
    code = "internal_error"
    retryable = True


def canonical_request_hash(request_payload: Any) -> str:
    """Hash JSON deterministically, rejecting values JSON cannot represent."""
    try:
        _validate_json_value(request_payload)
        encoded = json.dumps(
            request_payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidInputError("request_payload must be valid JSON.") from exc
    return hashlib.sha256(encoded).hexdigest()


def _validate_json_value(value: Any) -> None:
    """Require genuine JSON values, including string-only object keys."""
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise InvalidInputError("request_payload must be valid JSON.")
    if isinstance(value, list):
        for child in value:
            _validate_json_value(child)
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise InvalidInputError("request_payload object keys must be strings.")
            _validate_json_value(child)
        return
    raise InvalidInputError("request_payload must be valid JSON.")


def _strict_uuid(value: UUID | str, field: str) -> tuple[UUID, str]:
    if isinstance(value, UUID):
        parsed = value
    elif isinstance(value, str) and len(value) == 36:
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError):
            raise InvalidInputError(f"{field} must be a UUID.") from None
        if str(parsed) != value.lower():
            raise InvalidInputError(f"{field} must be a UUID.")
    else:
        raise InvalidInputError(f"{field} must be a UUID.")
    return parsed, str(parsed)


def _page_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise InvalidInputError(f"limit must be an integer between 1 and {MAX_PAGE_SIZE}.")
    return limit


def _uuid_cursor(value: Any) -> tuple[UUID, str]:
    if isinstance(value, UUID):
        return value, str(value)
    if isinstance(value, str):
        return _strict_uuid(value, "after_id")
    raise InvalidInputError("after_id must be a UUID cursor.")


def _numeric_cursor(value: Any) -> int:
    if isinstance(value, bool):
        raise InvalidInputError("after_id must be a numeric cursor.")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and len(value) <= 19 and value.isdecimal():
        parsed = int(value)
    else:
        raise InvalidInputError("after_id must be a numeric cursor.")
    if parsed < 0 or parsed > 9_223_372_036_854_775_807:
        raise InvalidInputError("after_id must be a numeric cursor.")
    return parsed


def _row_ids(table: str, row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for column in _UUID_COLUMNS.get(table, set()):
        value = result.get(column)
        if value is None:
            continue
        try:
            # Repository results are JSON-safe. UUIDs are typed only while
            # validating inputs/cursors and while comparing page keys.
            result[column] = str(UUID(str(value)))
        except (ValueError, AttributeError, TypeError):
            raise RepositoryUnavailableError("Migration data is temporarily unavailable.") from None
    if table == "session_discovered_urls" and result.get("id") is not None:
        try:
            result["id"] = int(result["id"])
        except (ValueError, TypeError):
            raise RepositoryUnavailableError("Migration data is temporarily unavailable.") from None
    return result


def _rows(result: Any) -> list[dict[str, Any]]:
    # Only normal table SELECTs call this helper (never single/maybe_single,
    # count-only or scalar RPC responses). Absence is an explicit empty array;
    # null/missing/object responses cannot establish an empty owned result.
    data = getattr(result, "data", None)
    if isinstance(data, list):
        if not all(isinstance(row, Mapping) for row in data):
            raise RepositoryUnavailableError("Migration data is temporarily unavailable.")
        return [dict(row) for row in data]
    raise RepositoryUnavailableError("Migration data is temporarily unavailable.")


def _known_rpc_error(exc: BaseException) -> str | None:
    """Map only the schema's exact P0001 exception messages."""
    if str(getattr(exc, "code", "") or "") != "P0001":
        return None
    message = str(getattr(exc, "message", "") or str(exc)).strip().lower()
    return message if message in {"operation_conflict", "not_found", "invalid_input"} else None


class MigrationRepository:
    """Read durable migration records and reserve idempotent operations."""

    def __init__(self, client=None):
        self._client = client if client is not None else SupabaseClient.get_admin_client()

    @property
    def client(self):
        return self._client

    def _execute(self, query):
        try:
            result = query.execute()
            error = getattr(result, "error", None)
            if error:
                known_error = _known_rpc_error(error)
                if known_error == "operation_conflict":
                    raise OperationConflictError("The operation idempotency key conflicts with an existing request.")
                if known_error == "not_found":
                    raise MigrationNotFoundError("Migration record not found.")
                if known_error == "invalid_input":
                    raise InvalidInputError("The migration operation input is invalid.")
                raise RepositoryUnavailableError("Migration data is temporarily unavailable.")
            return result
        except (MigrationRepositoryError, InvalidInputError):
            raise
        except Exception as exc:
            known_error = _known_rpc_error(exc)
            if known_error == "operation_conflict":
                raise OperationConflictError("The operation idempotency key conflicts with an existing request.") from None
            if known_error == "not_found":
                raise MigrationNotFoundError("Migration record not found.") from None
            if known_error == "invalid_input":
                raise InvalidInputError("The migration operation input is invalid.") from None
            # Do not put PostgREST/DB messages, hints, or connection details
            # into the outward-facing error or routine application logs.
            logger.warning("migration repository database operation failed: %s", type(exc).__name__)
            raise RepositoryUnavailableError("Migration data is temporarily unavailable.") from None

    def _get_owned(self, table: str, user_id: str, object_id: str, extra_filters: dict[str, Any]) -> dict[str, Any]:
        query = self.client.table(table).select("*").eq("id", object_id).eq("user_id", user_id)
        for column, value in extra_filters.items():
            query = query.eq(column, value)
        rows = _rows(self._execute(query))
        if not rows:
            raise MigrationNotFoundError("Migration record not found.")
        return _row_ids(table, rows[0])

    def get_migration(self, user_id: UUID | str, migration_id: UUID | str) -> dict[str, Any]:
        _, owner = _strict_uuid(user_id, "user_id")
        _, object_id = _strict_uuid(migration_id, "migration_id")
        return self._get_owned("migration_records", owner, object_id, {})

    def get_operation(self, user_id, operation_id, migration_id=None):
        _, owner = _strict_uuid(user_id, "user_id")
        _, object_id = _strict_uuid(operation_id, "operation_id")
        filters = {}
        if migration_id is not None:
            _, filters["migration_id"] = _strict_uuid(migration_id, "migration_id")
        return self._get_owned("migration_operations", owner, object_id, filters)

    def latest_inventory(self, user_id, migration_id, side):
        _, owner = _strict_uuid(user_id, "user_id")
        _, migration = _strict_uuid(migration_id, "migration_id")
        if side not in ("old", "new"):
            raise InvalidInputError("Invalid inventory side.")
        query = (self.client.table("inventory_snapshots").select("*")
                 .eq("user_id", owner).eq("migration_id", migration).eq("side", side)
                 .order("created_at", desc=True).order("id", desc=True).limit(1))
        rows = _rows(self._execute(query))
        return _row_ids("inventory_snapshots", rows[0]) if rows else None

    def get_inventory(self, user_id: UUID | str, migration_id: UUID | str, inventory_id: UUID | str) -> dict[str, Any]:
        _, owner = _strict_uuid(user_id, "user_id")
        _, migration = _strict_uuid(migration_id, "migration_id")
        _, inventory = _strict_uuid(inventory_id, "inventory_id")
        return self._get_owned(
            "inventory_snapshots", owner, inventory, {"migration_id": migration}
        )

    def get_run(self, user_id: UUID | str, migration_id: UUID | str, run_id: UUID | str) -> dict[str, Any]:
        _, owner = _strict_uuid(user_id, "user_id")
        _, migration = _strict_uuid(migration_id, "migration_id")
        _, run = _strict_uuid(run_id, "run_id")
        return self._get_owned("migration_runs", owner, run, {"migration_id": migration})

    def get_artifact(self, user_id: UUID | str, migration_id: UUID | str, artifact_id: UUID | str) -> dict[str, Any]:
        _, owner = _strict_uuid(user_id, "user_id")
        _, migration = _strict_uuid(migration_id, "migration_id")
        _, artifact = _strict_uuid(artifact_id, "artifact_id")
        return self._get_owned("migration_artifacts", owner, artifact, {"migration_id": migration})

    def _page(
        self,
        table: str,
        filters: dict[str, Any],
        after_id: Any,
        limit: int,
        cursor_kind: str,
    ) -> dict[str, Any]:
        limit = _page_limit(limit)
        if cursor_kind == "uuid":
            cursor = None if after_id is None else _uuid_cursor(after_id)
            scan_cursor = cursor[0] if cursor else None
        else:
            scan_cursor = None if after_id is None else _numeric_cursor(after_id)

        items: list[dict[str, Any]] = []
        while len(items) <= limit:
            # Ask for at most one page-sized response. If the server caps it
            # lower, continue from the last received key until the page is
            # full or the database returns no more rows.
            request_count = min(MAX_PAGE_SIZE, limit + 1 - len(items))
            query = self.client.table(table).select("*")
            for column, value in filters.items():
                query = query.eq(column, value)
            if scan_cursor is not None:
                query = query.gt("id", str(scan_cursor) if cursor_kind == "uuid" else scan_cursor)
            query = query.order("id").range(0, request_count - 1)
            batch = [_row_ids(table, row) for row in _rows(self._execute(query))]
            if not batch:
                break

            progressed = False
            for row in batch:
                row_id = row.get("id")
                if cursor_kind == "uuid":
                    try:
                        typed_id = UUID(str(row_id))
                    except (ValueError, TypeError, AttributeError):
                        raise RepositoryUnavailableError("Migration data is temporarily unavailable.") from None
                else:
                    try:
                        typed_id = int(row_id)
                    except (ValueError, TypeError):
                        raise RepositoryUnavailableError("Migration data is temporarily unavailable.") from None
                if scan_cursor is not None and typed_id <= scan_cursor:
                    continue
                items.append(row)
                scan_cursor = typed_id
                progressed = True
                if len(items) > limit:
                    break
            if not progressed:
                raise RepositoryUnavailableError("Migration data is temporarily unavailable.")

        has_more = len(items) > limit
        visible = items[:limit]
        next_cursor = visible[-1].get("id") if has_more and visible else None
        return {"items": visible, "next_cursor": next_cursor}

    def list_migrations(self, user_id: UUID | str, after_id: Any = None, limit: int = 100) -> dict[str, Any]:
        _, owner = _strict_uuid(user_id, "user_id")
        return self._page("migration_records", {"user_id": owner}, after_id, limit, "uuid")

    def list_inventory_urls(
        self,
        user_id: UUID | str,
        migration_id: UUID | str,
        inventory_id: UUID | str,
        after_id: Any = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        _, owner = _strict_uuid(user_id, "user_id")
        _, migration = _strict_uuid(migration_id, "migration_id")
        _, inventory = _strict_uuid(inventory_id, "inventory_id")
        # The discovery table is intentionally scoped through its owned,
        # migration-bound inventory because it has no user_id column.
        self.get_inventory(owner, migration, inventory)
        return self._page("session_discovered_urls", {"inventory_id": inventory}, after_id, limit, "numeric")

    def reserve_operation(
        self,
        user_id: UUID | str,
        migration_id: UUID | str,
        kind: str,
        idempotency_key: str,
        request_payload: Any,
    ) -> dict[str, Any]:
        _, owner = _strict_uuid(user_id, "user_id")
        _, migration = _strict_uuid(migration_id, "migration_id")
        if not isinstance(kind, str) or not kind.strip():
            raise InvalidInputError("kind must be a non-empty string.")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise InvalidInputError("idempotency_key must be a non-empty string.")
        request_hash = canonical_request_hash(request_payload)
        try:
            result = self._execute(self.client.rpc(
                "reserve_migration_operation",
                {
                    "p_user_id": owner,
                    "p_migration_id": migration,
                    "p_kind": kind,
                    "p_idempotency_key": idempotency_key,
                    "p_request_hash": request_hash,
                },
            ))
        except OperationConflictError:
            raise
        data = getattr(result, "data", None)
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, Mapping):
            raise RepositoryUnavailableError("Migration operation is temporarily unavailable.")
        response = _row_ids("migration_operations", data)
        if "replayed" not in response:
            raise RepositoryUnavailableError("Migration operation is temporarily unavailable.")
        return response
