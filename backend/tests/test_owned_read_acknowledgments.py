"""Actual PostgREST decoding + Flask owned-read contracts, entirely offline.

MockTransport replaces only HTTP storage; auth and grant fixtures are explicit.
No model, PostgreSQL, external PostgREST, or customer credentials are used.
"""
import hashlib
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from flask import Flask
from postgrest import SyncPostgrestClient

from backend.routes.v2_routes import v2_blueprint
from backend.routes.migration_artifact_routes import create_migration_artifact_blueprint
from backend.services.migration_artifact_service import MigrationArtifactService
from backend.services.migration_planning_service import MigrationPlanningService
from backend.services.migration_repository import MigrationRepository, RepositoryUnavailableError, _rows

OWNER = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
MIGRATION = '33333333-3333-4333-8333-333333333333'
ARTIFACT = '44444444-4444-4444-8444-444444444444'
CONTENT = '/Old?q=A%2FB,/New?q=A%2FB,301\n'


class OwnedReadAcknowledgments(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.responses = {}
        self.tables = {
            'migration_records': [{'id': MIGRATION, 'user_id': OWNER, 'old_origin': 'https://old.example',
                                   'new_origin': 'https://new.example', 'name': 'fixture'}],
            'migration_artifacts': [{'id': ARTIFACT, 'user_id': OWNER, 'migration_id': MIGRATION,
                                     'content_hash': hashlib.sha256(CONTENT.encode()).hexdigest(), 'format': 'csv'}],
            'migration_artifact_contents': [{'artifact_id': ARTIFACT, 'user_id': OWNER, 'migration_id': MIGRATION,
                                            'content_hash': hashlib.sha256(CONTENT.encode()).hexdigest(), 'content': CONTENT}],
        }
        def respond(request):
            self.requests.append(request)
            table = request.url.path.rsplit('/', 1)[-1]
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url.params.get('user_id'), f'eq.{self.owner}')
            if table in self.responses:
                status, content = self.responses[table]
                return httpx.Response(status, content=content, headers={'Content-Type': 'application/json'})
            rows = self.tables.get(table, [])
            for key, value in request.url.params.multi_items():
                if value.startswith('eq.'):
                    rows = [row for row in rows if str(row.get(key)) == value[3:]]
                if value.startswith('gt.'):
                    rows = [row for row in rows if str(row.get(key, '')) > value[3:]]
            return httpx.Response(200, json=rows)
        self.owner = OWNER
        http = httpx.Client(base_url='https://fixture.invalid', transport=httpx.MockTransport(respond))
        self.addCleanup(http.close)
        self.sdk = SyncPostgrestClient('https://fixture.invalid', http_client=http)
        self.repository = MigrationRepository(self.sdk)
        self.grant = Mock(return_value={'state': 'active', 'authority': 'quote_grant'})
        self.artifacts = MigrationArtifactService(self.repository, download_grant_reader=self.grant)
        app = Flask(__name__)
        app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
        app.register_blueprint(v2_blueprint, url_prefix='/api/v2')
        app.register_blueprint(create_migration_artifact_blueprint(lambda: self.artifacts), url_prefix='/api/v2')
        self.client = app.test_client()
        for p in (
            patch.dict(os.environ, {'MCP_PIVOT_ENABLED': 'true', 'JEV_MVP_ENABLED': 'false'}),
            patch('backend.routes.v2_routes.MCPDelegationService.resolve', side_effect=lambda *_: self.owner),
            patch('backend.routes.v2_routes.MigrationPlanningService', side_effect=lambda: MigrationPlanningService(self.repository)),
        ):
            p.start(); self.addCleanup(p.stop)
        self.headers = {'Authorization': 'Bearer fixture-delegation'}
        self.root = f'/api/v2/migrations/{MIGRATION}'
        self.download = self.root + f'/artifacts/{ARTIFACT}'

    def get(self, path):
        return self.client.get(path, headers=self.headers)

    def unavailable(self, response):
        self.assertEqual(response.status_code, 503, response.get_json())
        self.assertEqual(response.json['status'], 'failed')
        self.assertEqual(response.json['error']['code'], 'internal_error')
        self.assertTrue(response.json['error']['retryable'])
        self.assertEqual(response.json['next_action'], 'retry')
        self.assertEqual(response.json['data'], {})
        self.assertNotIn('secret diagnostic', response.get_data(as_text=True))

    def test_actual_sdk_null_is_not_a_missing_owned_migration_or_empty_history(self):
        self.responses['migration_records'] = (200, b'null')
        for path in (self.root, '/api/v2/migrations'):
            with self.subTest(path=path):
                self.requests.clear()
                self.unavailable(self.get(path))
                self.assertEqual(len(self.requests), 1)

    def test_null_artifact_metadata_is_unavailable_and_never_reaches_grant_or_content(self):
        self.responses['migration_artifacts'] = (200, b'null')
        self.unavailable(self.get(self.download))
        self.grant.assert_not_called()
        self.assertEqual([r.url.path for r in self.requests], ['/migration_artifacts'])

    def test_null_latest_run_does_not_publish_planning_success(self):
        self.responses['migration_runs'] = (200, b'null')
        self.unavailable(self.get(self.root))
        self.assertEqual(sum(r.url.path == '/migration_runs' for r in self.requests), 1)
        self.assertFalse(any(r.url.path == '/migration_price_quotes' for r in self.requests))

    def test_malformed_table_row_shapes_fail_closed_in_root_and_optional_reads(self):
        for payload in ({}, False, 0, '', [None], ['secret diagnostic'], {'id': MIGRATION}):
            for table in ('migration_records', 'migration_runs'):
                with self.subTest(payload=payload, table=table):
                    self.responses = {table: (200, json.dumps(payload).encode())}
                    self.unavailable(self.get(self.root))

    def test_real_empty_array_preserves_empty_history_and_ownership_not_found(self):
        self.tables.clear()
        response = self.get('/api/v2/migrations')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['data'], {'items': [], 'next_cursor': None})
        for path in (self.root, self.download):
            response = self.get(path)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json['error']['code'], 'not_found')
        self.grant.assert_not_called()

    def test_owned_success_missing_children_and_wrong_owner_remain_distinct(self):
        response = self.get(self.root)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['next_action'], 'provide_inventory')
        self.assertIsNone(response.json['data']['run'])
        response = self.get('/api/v2/migrations')
        self.assertEqual([r['id'] for r in response.json['data']['items']], [MIGRATION])
        response = self.get(self.download)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['data']['content'], CONTENT)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.grant.assert_called_once_with(OWNER, MIGRATION, ARTIFACT)
        self.grant.reset_mock()
        self.owner = OTHER
        for path in (self.root, self.download):
            self.assertEqual(self.get(path).status_code, 404)
        self.grant.assert_not_called()
        self.assertEqual(self.get('/api/v2/migrations').json['data']['items'], [])

    def test_existing_error_and_content_guards_remain_unavailable_not_empty(self):
        # 400 exercises real SDK APIError with no retry-eligible HTTP status.
        self.responses['migration_records'] = (400, b'{"code":"XX000","message":"secret diagnostic","details":null,"hint":null}')
        self.unavailable(self.get(self.root))
        self.assertEqual(len(self.requests), 1)
        self.responses = {'migration_artifact_contents': (200, b'null')}
        self.unavailable(self.get(self.download))

    def test_missing_result_data_is_not_an_authoritative_empty_array(self):
        for value in (None, SimpleNamespace(), SimpleNamespace(data=None)):
            with self.subTest(value=value), self.assertRaises(RepositoryUnavailableError):
                _rows(value)
        self.assertEqual(_rows(SimpleNamespace(data=[])), [])
