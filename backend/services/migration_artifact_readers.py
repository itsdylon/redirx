"""Authoritative readers for artifact creation.

These readers are intentionally database-backed.  They do not accept mapping
rows or an entitlement decision from the caller.  Mapping pages come from the
038 scoped RPC and grants come from the 036/037 owner bindings.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from .migration_repository import (
    InvalidInputError, MigrationNotFoundError, MigrationRepository,
    RepositoryUnavailableError, _strict_uuid,
)
from .redirect_export import select_export_mappings


def _safe_error(result: Any) -> None:
    error = getattr(result, "error", None)
    if error:
        message = str(getattr(error, "message", "") or "").lower()
        if "not_found" in message:
            raise MigrationNotFoundError("Migration record not found.")
        if "invalid_input" in message:
            raise InvalidInputError("Migration reader input is invalid.")
        if "inventory_incomplete" in message:
            raise RepositoryUnavailableError("Migration selection is not ready.")
        raise RepositoryUnavailableError("Migration data is temporarily unavailable.")


def _data(result: Any) -> Any:
    _safe_error(result)
    data = getattr(result, "data", None)
    if isinstance(data, list) and len(data) == 1:
        return data[0]
    return data


def _origin(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    return None


class MigrationArtifactReaders:
    """Concrete owner-scoped readers used by ``MigrationArtifactService``."""

    def __init__(self, repository: MigrationRepository):
        self.repository = repository
        self.client = repository.client

    def _one(self, table: str, filters: Mapping[str, Any], columns: str = "*") -> dict[str, Any] | None:
        query = self.client.table(table).select(columns)
        for column, value in filters.items():
            query = query.eq(column, value)
        try:
            result = query.execute()
        except Exception:
            raise RepositoryUnavailableError("Migration authority is temporarily unavailable.") from None
        value = _data(result)
        if value is None or value == []:
            return None
        if not isinstance(value, Mapping):
            raise RepositoryUnavailableError("Migration authority is temporarily unavailable.")
        return dict(value)

    def get_export_selection(self, user_id: str, migration_id: str, run_id: str,
                             selection_revision: str) -> dict[str, Any]:
        _, owner = _strict_uuid(user_id, "user_id")
        _, migration = _strict_uuid(migration_id, "migration_id")
        _, run = _strict_uuid(run_id, "run_id")
        if not isinstance(selection_revision, str) or not selection_revision.isdecimal():
            raise InvalidInputError("selection_revision must be a nonnegative integer.")
        requested = int(selection_revision)
        if requested > 2_147_483_647:
            raise InvalidInputError("selection_revision is out of range.")
        migration_row = self.repository.get_migration(owner, migration)
        self.repository.get_run(owner, migration, run)
        initial_revision = self._selection_revision(owner, migration, run)
        if initial_revision != requested:
            raise RepositoryUnavailableError("Migration selection revision is no longer current.")
        items: list[dict[str, Any]] = []
        cursor: dict[str, Any] | None = None
        seen_cursors: set[str] = set()
        while True:
            result = self.client.rpc("list_migration_matches", {
                "p_user_id": owner, "p_migration_id": migration, "p_run_id": run,
                "p_filter": "all", "p_cursor": cursor, "p_limit": 500,
            }).execute()
            page = _data(result)
            if not isinstance(page, Mapping) or not isinstance(page.get("items"), list):
                raise RepositoryUnavailableError("Migration selection is temporarily unavailable.")
            for item in page["items"]:
                if not isinstance(item, Mapping):
                    raise RepositoryUnavailableError("Migration selection is temporarily unavailable.")
                row = dict(item)
                revision = row.get("revision")
                if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                    raise RepositoryUnavailableError("Migration selection is temporarily unavailable.")
                # 038 exposes the audited action as `decision`; decision_action
                # is only an internal SQL alias. Engine evidence stays immutable,
                # so project the current decision into this export-only copy.
                decision = row.get("decision")
                if decision is not None and decision not in {
                    "approve", "set_target", "accept_repair", "reject", "defer", "intentional_removal",
                }:
                    raise RepositoryUnavailableError("Migration selection is temporarily unavailable.")
                if decision in {"approve", "set_target", "accept_repair"}:
                    target = row.get("decision_target")
                    if (revision == 0 or row.get("review_status") != decision
                            or not isinstance(target, str) or not target.strip()):
                        raise RepositoryUnavailableError("Migration selection is temporarily unavailable.")
                    row["new_url"] = target
                    # Only the superseded matcher hold is cleared. Independent
                    # held/rejected states and URL safety checks still apply.
                    row["needs_review"] = False
                row["action"] = decision
                items.append(row)
            if self._selection_revision(owner, migration, run) != initial_revision:
                raise RepositoryUnavailableError("Migration selection changed while it was being read.")
            next_cursor = page.get("next_cursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, Mapping):
                raise RepositoryUnavailableError("Migration selection is temporarily unavailable.")
            marker = repr(sorted(next_cursor.items()))
            if marker in seen_cursors:
                raise RepositoryUnavailableError("Migration selection pagination is temporarily unavailable.")
            seen_cursors.add(marker)
            cursor = dict(next_cursor)
            if len(items) > 50_000:
                raise RepositoryUnavailableError("Migration selection exceeds the supported artifact bound.")
        if self._selection_revision(owner, migration, run) != initial_revision:
            raise RepositoryUnavailableError("Migration selection revision is no longer current.")
        origins: list[str] = []
        emitted = select_export_mappings(
            items, url_format="paths", old_domain=migration_row.get("old_origin"),
            new_domain=migration_row.get("new_origin"),
        )["mappings"]
        for item in emitted:
            destination = item.get("new_url")
            destination_origin = _origin(destination)
            if destination_origin and destination_origin not in origins:
                origins.append(destination_origin)
            elif not destination_origin and isinstance(migration_row.get("new_origin"), str):
                fallback = migration_row["new_origin"].rstrip("/")
                if fallback not in origins:
                    origins.append(fallback)
        return {
            "selection_revision": str(requested), "mappings": items,
            "target_origins": origins, "old_domain": migration_row.get("old_origin"),
            "new_domain": migration_row.get("new_origin"), "url_format": "paths",
            "destination_mapping": {},
        }

    def _selection_revision(self, owner: str, migration: str, run: str) -> int:
        value = self._rpc_json("get_migration_selection_revision", {
            "p_user_id": owner, "p_migration_id": migration, "p_run_id": run,
        }).get("selection_revision")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RepositoryUnavailableError("Migration selection revision is temporarily unavailable.")
        return value

    def get_export_grant(self, user_id: str, migration_id: str, run_id: str) -> dict[str, Any]:
        _, owner = _strict_uuid(user_id, "user_id")
        _, migration = _strict_uuid(migration_id, "migration_id")
        _, run = _strict_uuid(run_id, "run_id")
        run_row = self.repository.get_run(owner, migration, run)
        if run_row.get("current_billing_usage_id") is not None:
            authority = self._rpc_json("current_run_export_authority", {
                "p_user_id": owner, "p_migration_id": migration, "p_run_id": run,
            })
            if (authority.get("eligible") is not True or str(authority.get("run_id")) != run
                    or str(authority.get("usage_id")) != run_row["current_billing_usage_id"]):
                raise MigrationNotFoundError("Completed current migration authority not found.")
            return authority
        grant_id = run_row.get("grant_id")
        quote_id = run_row.get("quote_id")
        if not isinstance(quote_id, str):
            raise MigrationNotFoundError("Migration grant not found.")

        # 046 Studio authority is independent of the 036 purchase-grant
        # lifecycle.  It is usable for completed output even after an
        # ordinary subscription lapse, but never after explicit revocation.
        studio_reservation = run_row.get("studio_reservation_id")
        if studio_reservation is not None:
            authority = self._rpc_json("studio_run_entitlement", {
                "p_user_id": owner, "p_migration_id": migration, "p_run_id": run,
            })
            if authority.get("eligible") is not True:
                raise MigrationNotFoundError("Migration Studio authority is not eligible.")
            if (str(authority.get("run_id")) != run
                    or str(authority.get("reservation_id")) != studio_reservation
                    or str(authority.get("quote_id")) != quote_id
                    or str(authority.get("operation_id")) != str(run_row.get("operation_id"))):
                raise RepositoryUnavailableError("Migration Studio authority is temporarily unavailable.")
            return {**authority, "authority": "studio", "state": "succeeded",
                    "grant_id": studio_reservation, "run_id": run}

        if not isinstance(grant_id, str):
            raise MigrationNotFoundError("Migration grant not found.")
        grant = self._one("migration_purchase_grants", {
            "id": grant_id, "migration_id": migration, "user_id": owner, "quote_id": quote_id,
        }, "id,user_id,migration_id,quote_id,source,state,created_at,rerun_expires_at")
        if not grant or grant.get("state") != "active":
            raise MigrationNotFoundError("Migration grant is not active.")
        # rerun_expires_at limits creation of another run, not download of a
        # completed paid artifact. Explicit state=revoked still denies access.
        self._require_completed_session(owner, run_row, run)
        grant["authority"] = "quote_grant"
        grant["run_id"] = run
        return grant

    def get_artifact_download_grant(self, user_id: str, migration_id: str,
                                    artifact_id: str) -> dict[str, Any]:
        _, owner = _strict_uuid(user_id, "user_id")
        _, migration = _strict_uuid(migration_id, "migration_id")
        _, artifact = _strict_uuid(artifact_id, "artifact_id")
        artifact_row = self.repository.get_artifact(owner, migration, artifact)
        run_id = artifact_row.get("run_id")
        if not isinstance(run_id, str):
            raise MigrationNotFoundError("Artifact authority not found.")
        run_row = self.repository.get_run(owner, migration, run_id)
        if run_row.get("studio_reservation_id") is not None:
            authority = self._rpc_json("studio_artifact_entitlement", {
                "p_user_id": owner, "p_migration_id": migration, "p_artifact_id": str(artifact),
            })
            if authority.get("eligible") is not True:
                raise MigrationNotFoundError("Artifact Studio authority is not eligible.")
            return {**authority, "authority": "studio", "state": "succeeded"}
        return self.get_export_grant(owner, migration, run_id)

    def _rpc_json(self, name: str, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            result = self.client.rpc(name, dict(params)).execute()
        except Exception:
            raise RepositoryUnavailableError("Migration authority is temporarily unavailable.") from None
        value = _data(result)
        if not isinstance(value, Mapping):
            raise RepositoryUnavailableError("Migration authority is temporarily unavailable.")
        return dict(value)

    def _require_completed_session(self, owner: str, run_row: Mapping[str, Any], run: str) -> None:
        session_id = run_row.get("legacy_session_id")
        authorized_attempt = run_row.get("authorized_attempt")
        if not isinstance(session_id, str) or authorized_attempt is None:
            raise MigrationNotFoundError("Completed migration run not found.")
        session = self._one("migration_sessions", {
            "id": session_id, "user_id": owner, "mcp_run_id": run,
        }, "id,user_id,mcp_run_id,status,attempt_count")
        if not session or session.get("status") != "completed" or session.get("attempt_count") != authorized_attempt:
            raise MigrationNotFoundError("Completed migration run not found.")
