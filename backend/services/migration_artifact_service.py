"""Owned immutable export artifacts and deployment handoff.

Selection and entitlement are server-side readers: callers provide IDs and
idempotency keys, never export rows or counts.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any, Callable
from uuid import UUID
from urllib.parse import urlsplit

from .analytics_service import AppEvent, capture
from .inventory_policy import normalize_origin
from .migration_artifact_readers import MigrationArtifactReaders
from .migration_repository import (InvalidInputError, MigrationNotFoundError,
    MigrationRepository, MigrationRepositoryError, RepositoryUnavailableError,
    _strict_uuid)
from .redirect_export import build_export, rehost, select_export_mappings


class ArtifactServiceError(MigrationRepositoryError):
    pass


class PartialArtifactError(ArtifactServiceError):
    code = "inventory_incomplete"
    next_action = "provide_inventory"


class EntitlementRequiredError(ArtifactServiceError):
    code = "payment_required"
    next_action = "complete_payment"


class DeploymentConflictError(ArtifactServiceError):
    code = "revision_conflict"


_HASH = re.compile(r"^[0-9a-f]{64}$")
_FORMATS = {"apache", "nginx", "wordpress", "vercel", "cloudflare", "shopify", "csv", "json"}


def _json_value(value: Any, depth: int = 0) -> None:
    if depth > 16:
        raise InvalidInputError("artifact metadata is too deeply nested.")
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise InvalidInputError("artifact metadata must be valid JSON.")
        return
    if isinstance(value, list):
        if len(value) > 50_000:
            raise InvalidInputError("artifact metadata is too large.")
        for child in value:
            _json_value(child, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 10_000:
            raise InvalidInputError("artifact metadata is too large.")
        for key, child in value.items():
            if not isinstance(key, str):
                raise InvalidInputError("artifact metadata keys must be strings.")
            _json_value(child, depth + 1)
        return
    raise InvalidInputError("artifact metadata must be valid JSON.")


def _nonblank(value: Any, field: str, max_length: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise InvalidInputError(f"{field} must be a bounded nonblank string.")
    if any(ord(char) < 0x20 or ord(char) == 0x7F or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise InvalidInputError(f"{field} contains unsupported characters.")
    return value.strip()


def _safe_row(result: Any) -> dict[str, Any]:
    data = getattr(result, "data", None)
    if isinstance(data, list):
        data = data[0] if len(data) == 1 else None
    if not isinstance(data, Mapping):
        raise RepositoryUnavailableError("Artifact data is temporarily unavailable.")
    return dict(data)


def _rpc_row(result: Any) -> dict[str, Any]:
    error = getattr(result, "error", None)
    if error:
        message = str(getattr(error, "message", "") or "").lower()
        if "operation_conflict" in message:
            raise DeploymentConflictError("The idempotency key conflicts with an existing request.")
        if "not_found" in message:
            raise MigrationNotFoundError("Artifact or migration record not found.")
        if "invalid_input" in message:
            raise InvalidInputError("The artifact operation input is invalid.")
        raise RepositoryUnavailableError("Artifact operation is temporarily unavailable.")
    return _safe_row(result)


def _redirects(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 50_000:
        raise InvalidInputError("verification redirects must be a bounded list.")
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise InvalidInputError("verification redirects must be objects.")
        result.append({"mapping_id": _nonblank(item.get("mapping_id"), "mapping_id"),
                       "source_url": _nonblank(item.get("source_url"), "source_url", 8_192),
                       "expected_url": _nonblank(item.get("expected_url"), "expected_url", 8_192)})
    return result


def _origins(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 100:
        raise InvalidInputError("target_origins must be a non-empty bounded list.")
    result: list[str] = []
    for item in value:
        origin = normalize_origin(_nonblank(item, "target_origin", 2_048))
        if origin not in result:
            result.append(origin)
    return result


def _exclusion_reasons(value: Any) -> dict[str, Any]:
    if not isinstance(value, list):
        raise InvalidInputError("selection exclusions must be a list.")
    counts: dict[str, int] = {}
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise InvalidInputError("selection exclusions must be objects.")
        reason = _nonblank(item.get("reason"), "exclusion reason", 100)
        counts[reason] = counts.get(reason, 0) + 1
        items.append({"index": item.get("index"), "reason": reason})
    return {"by_reason": counts, "items": items}


def _absolute_verification_url(value: Any, field: str) -> str:
    """Keep verification inputs byte-comparable with the authoritative DB row."""
    text = _nonblank(value, field, 8_192)
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise RepositoryUnavailableError(
            "Authoritative export selection contains a non-absolute verification URL."
        )
    return text


class MigrationArtifactService:
    def __init__(self, repository: MigrationRepository | None = None, *,
                 selection_reader: Callable[..., Mapping[str, Any]] | None = None,
                 grant_reader: Callable[..., Any] | None = None,
                 download_grant_reader: Callable[..., Any] | None = None):
        self.repository = repository if repository is not None else MigrationRepository()
        self.client = self.repository.client
        default_readers = MigrationArtifactReaders(self.repository)
        self.selection_reader = selection_reader or getattr(self.repository, "get_export_selection", None) or default_readers.get_export_selection
        self.grant_reader = grant_reader or getattr(self.repository, "get_export_grant", None) or default_readers.get_export_grant
        self.download_grant_reader = download_grant_reader or getattr(self.repository, "get_artifact_download_grant", None) or default_readers.get_artifact_download_grant

    def _selection(self, owner: str, migration: str, run: str, revision: str) -> dict[str, Any]:
        reader = self.selection_reader or getattr(self.repository, "get_export_selection", None)
        if not callable(reader):
            raise RepositoryUnavailableError("Authoritative export selection is unavailable.")
        try:
            value = reader(owner, migration, run, revision)
        except MigrationRepositoryError:
            raise
        except Exception:
            raise RepositoryUnavailableError("Authoritative export selection is unavailable.") from None
        if not isinstance(value, Mapping) or value.get("selection_revision") != revision:
            raise DeploymentConflictError("The requested selection revision is no longer current.")
        result = dict(value)
        _json_value(result)
        return result

    def _require_grant(self, owner: str, migration: str, run: str) -> None:
        reader = self.grant_reader or getattr(self.repository, "get_export_grant", None)
        if not callable(reader):
            raise EntitlementRequiredError("An owned export grant is required before creating an artifact.")
        try:
            grant = reader(owner, migration, run)
        except MigrationRepositoryError:
            raise
        except Exception:
            raise RepositoryUnavailableError("Export entitlement is temporarily unavailable.") from None
        state = grant.get("state") if isinstance(grant, Mapping) else getattr(grant, "state", None)
        authority = grant.get("authority") if isinstance(grant, Mapping) else getattr(grant, "authority", None)
        if state not in {"active", "succeeded"} or authority not in {"quote_grant", "studio", "current_billing"}:
            raise EntitlementRequiredError("An owned export grant is required before creating an artifact.")

    def _replay_artifact(self, owner: str, migration: str, run: str, key: str,
                         fmt: str, revision: str, partial_policy: str) -> dict[str, Any] | None:
        """Return an exact persisted artifact replay without rereading selection."""
        try:
            result = (self.client.table("migration_artifact_mutations").select("*")
                      .eq("user_id", owner).eq("migration_id", migration)
                      .eq("kind", "artifact").eq("idempotency_key", key).execute())
        except Exception:
            raise RepositoryUnavailableError("Artifact operation is temporarily unavailable.") from None
        if getattr(result, "error", None):
            raise RepositoryUnavailableError("Artifact operation is temporarily unavailable.")
        data = getattr(result, "data", None)
        if isinstance(data, list):
            if not data:
                return None
            if len(data) != 1:
                raise RepositoryUnavailableError("Artifact operation is temporarily unavailable.")
            data = data[0]
        if not isinstance(data, Mapping):
            raise RepositoryUnavailableError("Artifact operation is temporarily unavailable.")
        stored = data.get("result")
        if not isinstance(stored, Mapping):
            raise RepositoryUnavailableError("Artifact operation is temporarily unavailable.")
        if (str(stored.get("user_id")) != owner
                or str(stored.get("migration_id")) != migration
                or str(stored.get("run_id")) != run
                or stored.get("format") != fmt
                or stored.get("decision_revision") != revision
                or stored.get("partial_policy") != partial_policy):
            raise DeploymentConflictError("The idempotency key conflicts with an existing artifact request.")
        replay = dict(stored)
        replay["replayed"] = True
        return self._artifact_summary(replay)

    def create_artifact(self, user_id: UUID | str, migration_id: UUID | str, run_id: UUID | str,
                        *, idempotency_key: str, fmt: str, selection_revision: str,
                        partial_policy: str = "deny") -> dict[str, Any]:
        _, owner = _strict_uuid(user_id, "user_id")
        _, migration = _strict_uuid(migration_id, "migration_id")
        _, run = _strict_uuid(run_id, "run_id")
        key = _nonblank(idempotency_key, "idempotency_key")
        revision = _nonblank(selection_revision, "selection_revision")
        if fmt not in _FORMATS:
            raise InvalidInputError("format is unsupported.")
        if partial_policy not in {"deny", "allow"}:
            raise InvalidInputError("partial_policy must be 'deny' or 'allow'.")
        self.repository.get_run(owner, migration, run)
        self._require_grant(owner, migration, run)
        replay = self._replay_artifact(owner, migration, run, key, fmt, revision, partial_policy)
        if replay is not None:
            return replay
        snapshot = self._selection(owner, migration, run, revision)
        rows = snapshot.get("mappings")
        if not isinstance(rows, list):
            raise RepositoryUnavailableError("Authoritative export selection is unavailable.")
        url_format = snapshot.get("url_format", "paths")
        selected = select_export_mappings(rows, url_format=url_format,
                                          old_domain=snapshot.get("old_domain"),
                                          new_domain=snapshot.get("new_domain"))
        excluded = selected["excluded"]
        if excluded and partial_policy != "allow":
            raise PartialArtifactError("Unresolved or excluded mappings require an explicit partial-export policy.")
        if not selected["mappings"]:
            raise PartialArtifactError("The authoritative selection contains no exportable mappings.")
        content = build_export(rows, fmt, url_format=url_format,
                               old_domain=snapshot.get("old_domain"), new_domain=snapshot.get("new_domain"))
        targets = _origins(snapshot.get("target_origins"))
        redirects: list[dict[str, str]] = []
        for item in selected["mappings"]:
            row = item.get("row")
            if not isinstance(row, Mapping):
                raise RepositoryUnavailableError("Authoritative export selection is unavailable.")
            redirects.append({"mapping_id": _nonblank(row.get("mapping_id", row.get("id")), "mapping_id"),
                               "source_url": _absolute_verification_url(item["old_url"], "source_url"),
                               "expected_url": _absolute_verification_url(item["new_url"], "expected_url")})
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        verification = {"redirects": redirects, "artifact_content_hash": content_hash,
                        "decision_revision": revision}
        destination = snapshot.get("destination_mapping", {})
        if not isinstance(destination, Mapping):
            raise RepositoryUnavailableError("Authoritative export selection is unavailable.")
        artifact = {"migration_id": migration, "user_id": owner, "run_id": run,
                    "decision_revision": revision, "format": fmt, "content_hash": content_hash,
                    "target_origins": targets, "included_count": len(redirects),
                    "excluded_count": len(excluded), "exclusion_reasons": _exclusion_reasons(excluded),
                    "destination_mapping": dict(destination), "verification_inputs": verification,
                    "partial_policy": partial_policy}
        try:
            result = self.client.rpc("publish_migration_artifact", {
                "p_user_id": owner, "p_migration_id": migration, "p_run_id": run,
                "p_idempotency_key": key, "p_artifact": artifact, "p_content": content}).execute()
            row = _rpc_row(result)
            if (row.get("content_hash") != content_hash
                    or row.get("included_count") != len(redirects)
                    or row.get("excluded_count") != len(excluded)
                    or row.get("verification_inputs") != verification
                    or str(row.get("migration_id")) != migration
                    or str(row.get("user_id")) != owner):
                raise RepositoryUnavailableError("Artifact operation is temporarily unavailable.")
            summary = self._artifact_summary(row)
            # Fired here, not through entitlement_service.record_export(): the
            # JEV path (this method) never calls record_export, which belongs
            # to the legacy paid-export usage ledger. This is the only export
            # completion event on the JEV path, gated on the RPC's own
            # 'replayed' flag so a re-submitted idempotency key never re-fires.
            capture(AppEvent.REDIRECT_ARTIFACT_EXPORTED, user_id=owner, migration_id=migration, properties={
                "run_id": run, "artifact_id": summary["id"], "format": fmt,
                "included_count": summary["included_count"], "excluded_count": summary["excluded_count"],
            })
            return summary
        except MigrationRepositoryError:
            raise
        except Exception:
            raise RepositoryUnavailableError("Artifact operation is temporarily unavailable.") from None

    def authorize_download(self, user_id: UUID | str, migration_id: UUID | str,
                           artifact_id: UUID | str) -> dict[str, Any]:
        _, owner = _strict_uuid(user_id, "user_id")
        _, migration = _strict_uuid(migration_id, "migration_id")
        _, artifact = _strict_uuid(artifact_id, "artifact_id")
        row = self.repository.get_artifact(owner, migration, artifact)
        authority = self.download_grant_reader(owner, migration, artifact)
        state = authority.get("state") if isinstance(authority, Mapping) else getattr(authority, "state", None)
        authority_kind = authority.get("authority") if isinstance(authority, Mapping) else getattr(authority, "authority", None)
        if state not in {"active", "succeeded"} or authority_kind not in {"quote_grant", "studio", "current_billing"}:
            raise EntitlementRequiredError("An owned export grant is required to download this artifact.")
        result = (self.client.table("migration_artifact_contents").select("*")
                  .eq("artifact_id", str(artifact)).eq("migration_id", migration)
                  .eq("user_id", owner).execute())
        if getattr(result, "error", None):
            raise RepositoryUnavailableError("Artifact content is temporarily unavailable.")
        content = _safe_row(result)
        if content.get("content_hash") != row.get("content_hash") or not isinstance(content.get("content"), str):
            raise RepositoryUnavailableError("Artifact content is temporarily unavailable.")
        return {"resource_type": "artifact_download", "artifact_id": str(artifact),
                "migration_id": str(migration), "content_hash": row.get("content_hash"),
                "format": row.get("format"), "content": content["content"]}

    def report_installation(self, user_id: UUID | str, migration_id: UUID | str,
                            artifact_id: UUID | str, live_origin: str, *,
                            idempotency_key: str, origin_rewrites: Mapping[str, str] | None = None,
                            installation_report: Mapping[str, Any] | None = None) -> dict[str, Any]:
        _, owner = _strict_uuid(user_id, "user_id")
        _, migration = _strict_uuid(migration_id, "migration_id")
        _, artifact = _strict_uuid(artifact_id, "artifact_id")
        key = _nonblank(idempotency_key, "idempotency_key")
        artifact_row = self.repository.get_artifact(owner, migration, artifact)
        migration_row = self.repository.get_migration(owner, migration)
        try:
            live = normalize_origin(live_origin)
        except ValueError:
            raise InvalidInputError("live_origin must be a valid origin.") from None
        report = dict(installation_report or {})
        _json_value(report)
        stored = artifact_row.get("verification_inputs")
        if not isinstance(stored, Mapping):
            raise RepositoryUnavailableError("Artifact verification data is temporarily unavailable.")
        redirects = _redirects(stored.get("redirects"))
        targets = _origins(artifact_row.get("target_origins"))
        rewrites: dict[str, str] = {}
        if origin_rewrites is not None:
            if not isinstance(origin_rewrites, Mapping):
                raise InvalidInputError("origin_rewrites must be an object.")
            for source, destination in origin_rewrites.items():
                rewrites[normalize_origin(_nonblank(source, "origin_rewrite_source", 2_048))] = normalize_origin(_nonblank(destination, "origin_rewrite_destination", 2_048))
        elif len(targets) == 1:
            rewrites[targets[0]] = live
        else:
            raise InvalidInputError("explicit origin_rewrites are required for multi-origin artifacts.")
        if len(targets) > 1 and set(rewrites) != set(targets):
            raise InvalidInputError("origin_rewrites must cover each declared destination origin exactly.")
        old_origin = normalize_origin(_nonblank(migration_row.get("old_origin"), "old_origin", 2_048))
        content_hash = artifact_row.get("content_hash")
        revision = _nonblank(artifact_row.get("decision_revision"), "decision_revision")
        if not isinstance(content_hash, str) or not _HASH.fullmatch(content_hash):
            raise RepositoryUnavailableError("Artifact verification data is temporarily unavailable.")
        pinned = []
        for item in redirects:
            expected = item["expected_url"]
            parsed = urlsplit(expected)
            source_origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}" if parsed.scheme and parsed.netloc else None
            if source_origin in rewrites:
                expected = rehost(expected, rewrites[source_origin])
            pinned.append({"mapping_id": item["mapping_id"], "source_url": rehost(item["source_url"], old_origin), "expected_url": expected})
        verification = {"redirects": pinned, "artifact_content_hash": content_hash,
                        "decision_revision": revision}
        payload = {"migration_id": migration, "user_id": owner, "artifact_id": str(artifact),
                   "live_origin": live, "status": "installation_reported",
                   "artifact_content_hash": content_hash,
                   "decision_revision": revision, "format": artifact_row.get("format"),
                   "included_count": artifact_row.get("included_count"), "excluded_count": artifact_row.get("excluded_count"),
                   "target_origins": targets, "destination_mapping": artifact_row.get("destination_mapping", {}),
                   "verification_inputs": verification, "installation_report": report}
        try:
            result = self.client.rpc("publish_artifact_deployment", {
                "p_user_id": owner, "p_migration_id": migration, "p_artifact_id": str(artifact),
                "p_idempotency_key": key, "p_deployment": payload}).execute()
            row = _rpc_row(result)
            if (str(row.get("migration_id")) != migration or str(row.get("user_id")) != owner
                    or str(row.get("artifact_id")) != str(artifact)
                    or row.get("live_origin") != live or row.get("verification_inputs") != verification):
                raise RepositoryUnavailableError("Deployment operation is temporarily unavailable.")
            summary = self._deployment_summary(row, owner, migration, str(artifact))
            if not summary.get("replayed"):
                capture(AppEvent.MIGRATION_ARTIFACT_INSTALLED, user_id=owner, migration_id=migration, properties={
                    "artifact_id": str(artifact), "deployment_id": summary["deployment_id"],
                    "status": summary["status"],
                })
            return summary
        except MigrationRepositoryError:
            raise
        except Exception:
            raise RepositoryUnavailableError("Deployment operation is temporarily unavailable.") from None

    def get_verification_inputs(self, user_id: UUID | str, migration_id: UUID | str,
                                artifact_id: UUID | str, deployment_id: UUID | str) -> dict[str, Any]:
        _, owner = _strict_uuid(user_id, "user_id")
        _, migration = _strict_uuid(migration_id, "migration_id")
        _, artifact = _strict_uuid(artifact_id, "artifact_id")
        _, deployment = _strict_uuid(deployment_id, "deployment_id")
        result = (self.client.table("artifact_deployments").select("*").eq("id", str(deployment))
                  .eq("user_id", owner).eq("migration_id", migration)
                  .eq("artifact_id", str(artifact)).execute())
        if getattr(result, "error", None):
            raise RepositoryUnavailableError("Deployment data is temporarily unavailable.")
        if isinstance(getattr(result, "data", None), list) and not result.data:
            raise MigrationNotFoundError("Deployment not found.")
        value = _safe_row(result).get("verification_inputs")
        if not isinstance(value, Mapping):
            raise RepositoryUnavailableError("Deployment verification data is temporarily unavailable.")
        return dict(value)

    @staticmethod
    def _artifact_summary(row: Mapping[str, Any]) -> dict[str, Any]:
        result = {key: row.get(key) for key in ("id", "migration_id", "user_id", "run_id", "decision_revision", "format", "content_hash", "storage_key", "target_origins", "included_count", "excluded_count", "partial_policy", "verification_inputs")}
        result["replayed"] = row.get("replayed", False)
        return result

    @staticmethod
    def _deployment_summary(row: Mapping[str, Any], owner: str, migration: str, artifact: str) -> dict[str, Any]:
        try:
            deployment = str(UUID(str(row.get("id", row.get("deployment_id")))))
        except (ValueError, TypeError, AttributeError):
            raise RepositoryUnavailableError("Deployment data is temporarily unavailable.") from None
        status = row.get("status")
        if status not in {"generated", "installation_reported", "live_verified"}:
            raise RepositoryUnavailableError("Deployment data is temporarily unavailable.")
        return {"deployment_id": deployment, "migration_id": migration, "user_id": owner,
                "artifact_id": artifact, "live_origin": row.get("live_origin"), "status": status,
                "verification_inputs": row.get("verification_inputs"), "replayed": row.get("replayed", False)}
