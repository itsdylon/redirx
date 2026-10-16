from __future__ import annotations

import unittest
import hashlib
from unittest.mock import patch
from uuid import UUID

from backend.services.migration_artifact_service import (
    DeploymentConflictError, EntitlementRequiredError, MigrationArtifactService,
    PartialArtifactError,
)
from backend.services.migration_repository import (InvalidInputError, MigrationNotFoundError,
    RepositoryUnavailableError)

OWNER = str(UUID("00000000-0000-0000-0000-000000000001"))
OTHER = str(UUID("00000000-0000-0000-0000-000000000002"))
MIGRATION = str(UUID("10000000-0000-0000-0000-000000000001"))
RUN = str(UUID("30000000-0000-0000-0000-000000000001"))
ARTIFACT = str(UUID("40000000-0000-0000-0000-000000000001"))
DEPLOYMENT = str(UUID("50000000-0000-0000-0000-000000000001"))


class Result:
    def __init__(self, data=None, error=None):
        self.data, self.error = data, error


class Query:
    def __init__(self, client, table):
        self.client, self.table, self.filters = client, table, []

    def select(self, _): return self
    def eq(self, key, value): self.filters.append((key, value)); return self

    def execute(self):
        self.client.calls.append((self.table, tuple(self.filters)))
        rows = self.client.rows.get(self.table, [])
        for row in rows:
            if all(str(row.get(k)) == str(v) for k, v in self.filters): return Result(row)
        return Result([])


class Rpc:
    def __init__(self, client, name, payload): self.client, self.name, self.payload = client, name, payload
    def execute(self):
        self.client.rpcs.append((self.name, self.payload))
        if self.client.rpc_exception: raise self.client.rpc_exception
        if self.client.rpc_error: return Result(error=self.client.rpc_error)
        if self.name == "publish_migration_artifact":
            result = {"id": ARTIFACT, **self.payload["p_artifact"],
                      "storage_key": f"db://migration_artifact_contents/{ARTIFACT}", "replayed": False}
            return Result(result)
        result = {"id": DEPLOYMENT, **self.payload["p_deployment"], "replayed": False}
        return Result(result)


class Client:
    def __init__(self, rows=None):
        self.rows, self.calls, self.rpcs, self.rpc_error = rows or {}, [], [], None
        self.rpc_exception = None
    def table(self, name): return Query(self, name)
    def rpc(self, name, payload): return Rpc(self, name, payload)


class Repository:
    def __init__(self, client, artifact=None): self.client, self.artifact = client, artifact
    def get_run(self, user, migration, run):
        if user != OWNER: raise MigrationNotFoundError("not found")
        return {"id": run, "migration_id": migration, "user_id": user}
    def get_export_grant(self, user, migration, run): return {"authority": "quote_grant", "state": "active"} if user == OWNER else {"authority": "quote_grant", "state": "revoked"}
    def get_export_selection(self, user, migration, run, revision):
        return {"selection_revision": revision, "target_origins": ["https://new.example"],
                "mappings": [{"id": "map-1", "old_url": "https://old.example/a", "new_url": "https://new.example/b"}]}
    def get_artifact(self, user, migration, artifact):
        if user != OWNER or artifact != ARTIFACT: raise MigrationNotFoundError("not found")
        return self.artifact or {"id": ARTIFACT, "migration_id": migration, "user_id": user,
            "content_hash": "a" * 64, "decision_revision": "rev-1", "format": "json",
            "target_origins": ["https://new.example"], "included_count": 1, "excluded_count": 0,
            "destination_mapping": {}, "verification_inputs": {"redirects": [{
                "mapping_id": "map-1", "source_url": "https://old.example/a", "expected_url": "https://new.example/b"}],
                "artifact_content_hash": "a" * 64, "decision_revision": "rev-1"}}
    def get_migration(self, user, migration): return {"id": migration, "user_id": user, "old_origin": "https://old.example"}
    def get_artifact_download_grant(self, user, migration, artifact): return {"authority": "quote_grant", "state": "active"}


class TestMigrationArtifactService(unittest.TestCase):
    def make(self, **kwargs):
        client = Client(kwargs.pop("rows", None))
        return MigrationArtifactService(Repository(client, kwargs.pop("artifact", None))), client

    def test_service_renderer_preserves_all_format_bytes_and_selected_counts(self):
        from backend.services.redirect_export import FORMATS, build_export
        rows = [
            {"id": "a", "old_url": "https://old.example/Case", "new_url": "https://new.example/Target#Anchor", "action": "approve"},
            {"id": "b", "old_url": "https://old.example/Held", "new_url": "https://new.example/Other", "needs_review": True},
            {"id": "c", "old_url": "bad", "new_url": "https://new.example/Invalid"},
            {"id": "d", "old_url": "https://old.example/Case", "new_url": "https://new.example/Target#Anchor", "action": "approve"},
        ]
        for fmt in FORMATS:
            with self.subTest(fmt=fmt):
                service, client = self.make()
                service.selection_reader = lambda *_: {"selection_revision": "1", "target_origins": ["https://new.example"],
                    "mappings": rows, "old_domain": "https://old.example", "new_domain": "https://new.example"}
                result = service.create_artifact(OWNER, MIGRATION, RUN, idempotency_key="all-format", fmt=fmt,
                    selection_revision="1", partial_policy="allow")
                payload = client.rpcs[0][1]
                expected = build_export(rows, fmt, old_domain="https://old.example", new_domain="https://new.example")
                self.assertEqual(payload["p_content"], expected)
                self.assertEqual(result["content_hash"], hashlib.sha256(expected.encode()).hexdigest())
                self.assertEqual((result["included_count"], result["excluded_count"]), (1, 3))
                self.assertEqual(payload["p_artifact"]["verification_inputs"]["redirects"], [{
                    "mapping_id": "a", "source_url": rows[0]["old_url"], "expected_url": rows[0]["new_url"]}])

    def test_reused_selection_keeps_platform_refusal_before_publication(self):
        service, client = self.make()
        service.selection_reader = lambda *_: {"selection_revision": "1", "target_origins": ["https://new.example"],
            "mappings": [{"id": "raw", "old_url": "https://old.example/A%2FB", "new_url": "https://new.example/C"}]}
        with self.assertRaisesRegex(InvalidInputError, "normalizes encoded paths"):
            service.create_artifact(OWNER, MIGRATION, RUN, idempotency_key="unsupported", fmt="nginx", selection_revision="1")
        self.assertEqual(client.rpcs, [])

    def test_authoritative_snapshot_grant_and_rpc_bind_content_counts(self):
        service, client = self.make()
        result = service.create_artifact(OWNER, MIGRATION, RUN, idempotency_key="artifact-1", fmt="json", selection_revision="rev-1")
        self.assertEqual(result["id"], ARTIFACT)
        name, payload = client.rpcs[0]
        self.assertEqual(name, "publish_migration_artifact")
        self.assertEqual(payload["p_artifact"]["included_count"], 1)
        self.assertEqual(payload["p_artifact"]["excluded_count"], 0)
        self.assertEqual(payload["p_artifact"]["verification_inputs"]["artifact_content_hash"], result["content_hash"])
        self.assertNotIn("selection", payload)

    def test_service_replays_persisted_artifact_before_current_selection(self):
        service, client = self.make()
        first = service.create_artifact(OWNER, MIGRATION, RUN, idempotency_key="artifact-replay",
                                        fmt="json", selection_revision="rev-1")
        client.rows["migration_artifact_mutations"] = [{"user_id": OWNER, "migration_id": MIGRATION,
            "kind": "artifact", "idempotency_key": "artifact-replay", "result": first}]
        service.selection_reader = lambda *_: (_ for _ in ()).throw(DeploymentConflictError("stale"))
        replay = service.create_artifact(OWNER, MIGRATION, RUN, idempotency_key="artifact-replay",
                                         fmt="json", selection_revision="rev-1")
        self.assertEqual(replay["id"], first["id"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(DeploymentConflictError):
            service.create_artifact(OWNER, MIGRATION, RUN, idempotency_key="artifact-replay",
                                    fmt="apache", selection_revision="rev-1")
        service.grant_reader = lambda *_: {"authority": "quote_grant", "state": "revoked"}
        with self.assertRaises(EntitlementRequiredError):
            service.create_artifact(OWNER, MIGRATION, RUN, idempotency_key="artifact-replay",
                                    fmt="json", selection_revision="rev-1")

    def test_missing_grant_is_fail_closed_and_wrong_revision_is_rejected(self):
        service, client = self.make()
        service.grant_reader = lambda *_: {"authority": "quote_grant", "state": "revoked"}
        with self.assertRaises(EntitlementRequiredError):
            service.create_artifact(OWNER, MIGRATION, RUN, idempotency_key="x", fmt="json", selection_revision="rev-1")
        service, _ = self.make()
        service.selection_reader = lambda *_: {"selection_revision": "other", "target_origins": ["https://new.example"], "mappings": []}
        with self.assertRaises(DeploymentConflictError):
            service.create_artifact(OWNER, MIGRATION, RUN, idempotency_key="x", fmt="json", selection_revision="wrong")

    def test_excluded_rows_require_partial_policy_and_counts_are_not_caller_supplied(self):
        service, _ = self.make()
        service.selection_reader = lambda *_: {"selection_revision": "rev-1", "target_origins": ["https://new.example"],
            "mappings": [{"id": "m", "old_url": "https://old.example/a", "new_url": "https://old.example/a"}]}
        with self.assertRaises(PartialArtifactError):
            service.create_artifact(OWNER, MIGRATION, RUN, idempotency_key="x", fmt="json", selection_revision="rev-1")

    def test_review_blocks_do_not_publish_or_instruct_an_inventory_reimport(self):
        for rows, policy in (([{'id': 'm', 'old_url': 'https://old.example/a',
                               'new_url': 'https://old.example/a'}], 'deny'), ([], 'allow')):
            with self.subTest(policy=policy):
                service, client = self.make()
                service.selection_reader = lambda *_: {'selection_revision': 'rev-1',
                    'target_origins': ['https://new.example'], 'mappings': rows}
                with self.assertRaises(PartialArtifactError) as caught:
                    service.create_artifact(OWNER, MIGRATION, RUN, idempotency_key='unchanged',
                        fmt='json', selection_revision='rev-1', partial_policy=policy)
                self.assertEqual(caught.exception.next_action, 'resolve_matches')
                self.assertIn('Review', str(caught.exception))
                self.assertEqual(client.rpcs, [])

    def test_relative_verification_sources_fail_closed(self):
        service, _ = self.make()
        service.selection_reader = lambda *_: {"selection_revision": "rev-1", "target_origins": ["https://new.example"],
            "mappings": [{"id": "m", "old_url": "/relative", "new_url": "https://new.example/a"}]}
        with self.assertRaises(RepositoryUnavailableError):
            service.create_artifact(OWNER, MIGRATION, RUN, idempotency_key="x", fmt="json", selection_revision="rev-1")

    def test_download_is_owner_scoped_and_returns_persisted_content(self):
        content = '{"url":"https://new.example/Café?x=%2F#Section"}'
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        service, client = self.make(rows={"migration_artifact_contents": [{"artifact_id": ARTIFACT,
            "migration_id": MIGRATION, "user_id": OWNER, "content": content, "content_hash": digest}]})
        artifact = service.repository.get_artifact(OWNER, MIGRATION, ARTIFACT)
        service.repository.artifact = {**artifact, "content_hash": digest}
        download = service.authorize_download(OWNER, MIGRATION, ARTIFACT)
        self.assertEqual(download["content"], content)
        self.assertEqual(download["content_hash"], digest)
        with self.assertRaises(MigrationNotFoundError): service.authorize_download(OTHER, MIGRATION, ARTIFACT)
        self.assertEqual(client.calls[-1][1], (("artifact_id", ARTIFACT), ("migration_id", MIGRATION), ("user_id", OWNER)))

    def test_download_rejects_changed_bytes_even_when_stored_hash_fields_match(self):
        content = '{"old":"/source","new":"/approved"}'
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        for changed in [content + '\n', content.replace('/approved', '/other'), '']:
            with self.subTest(changed=changed):
                service, client = self.make(rows={"migration_artifact_contents": [{"artifact_id": ARTIFACT,
                    "migration_id": MIGRATION, "user_id": OWNER, "content": changed, "content_hash": digest}]})
                artifact = service.repository.get_artifact(OWNER, MIGRATION, ARTIFACT)
                service.repository.artifact = {**artifact, "content_hash": digest}
                with self.assertRaises(RepositoryUnavailableError):
                    service.authorize_download(OWNER, MIGRATION, ARTIFACT)
                self.assertFalse(client.rpcs)

    def test_installation_requires_explicit_multi_origin_rehosting_and_is_idempotent_rpc(self):
        artifact = {"id": ARTIFACT, "migration_id": MIGRATION, "user_id": OWNER, "content_hash": "a" * 64,
            "decision_revision": "rev-1", "format": "json", "target_origins": ["https://a.example", "https://b.example"],
            "included_count": 2, "excluded_count": 0, "destination_mapping": {}, "verification_inputs": {
                "redirects": [{"mapping_id": "a", "source_url": "https://old.example/a", "expected_url": "https://a.example/a"},
                              {"mapping_id": "b", "source_url": "https://old.example/b", "expected_url": "https://b.example/b"}],
                "artifact_content_hash": "a" * 64, "decision_revision": "rev-1"}}
        service, client = self.make(artifact=artifact)
        with self.assertRaises(InvalidInputError): service.report_installation(OWNER, MIGRATION, ARTIFACT, "https://live.example", idempotency_key="d")
        result = service.report_installation(OWNER, MIGRATION, ARTIFACT, "https://live.example", idempotency_key="d",
            origin_rewrites={"https://a.example": "https://live-a.example", "https://b.example": "https://live-b.example"})
        redirects = client.rpcs[0][1]["p_deployment"]["verification_inputs"]["redirects"]
        self.assertEqual([r["expected_url"] for r in redirects], ["https://live-a.example/a", "https://live-b.example/b"])
        self.assertEqual(result["status"], "installation_reported")

    def test_real_postgrest_exception_codes_are_translated_for_both_publications(self):
        from postgrest.exceptions import APIError
        for code, error_type in (('operation_conflict', DeploymentConflictError),
                                 ('not_found', MigrationNotFoundError), ('invalid_input', InvalidInputError)):
            for operation in ('artifact', 'installation'):
                with self.subTest(code=code, operation=operation):
                    service, client = self.make()
                    client.rpc_exception = APIError({'code': 'P0001', 'message': code, 'details': 'private detail', 'hint': None})
                    with self.assertRaises(error_type) as caught:
                        if operation == 'artifact':
                            service.create_artifact(OWNER, MIGRATION, RUN, idempotency_key='same', fmt='json', selection_revision='rev-1')
                        else:
                            service.report_installation(OWNER, MIGRATION, ARTIFACT, 'https://new.example', idempotency_key='same')
                    self.assertNotIn('private', str(caught.exception))
                    self.assertEqual(len(client.rpcs), 1)

    def test_unknown_or_lookalike_error_is_unconfirmed_with_readback_and_no_retry(self):
        from postgrest.exceptions import APIError
        errors = [TimeoutError('private payload'),
            APIError({'code': 'P0001', 'message': 'private operation_conflict extra', 'details': None, 'hint': None}),
            APIError({'code': '23514', 'message': 'operation_conflict', 'details': None, 'hint': None})]
        for error in errors:
            for returned in (False, True):
                with self.subTest(error=type(error), returned=returned):
                    service, client = self.make()
                    if returned: client.rpc_error = error
                    else: client.rpc_exception = error
                    with patch('backend.services.migration_artifact_service.capture') as capture:
                        with self.assertRaises(RepositoryUnavailableError) as caught:
                            service.create_artifact(OWNER, MIGRATION, RUN, idempotency_key='same', fmt='json', selection_revision='rev-1')
                    self.assertNotIn('private', str(caught.exception))
                    self.assertEqual(caught.exception.next_action, 'poll')
                    self.assertIn('same idempotency key', str(caught.exception))
                    self.assertEqual(len(client.rpcs), 1)
                    capture.assert_not_called()


if __name__ == "__main__": unittest.main()
