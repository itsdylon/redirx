"""Historical approval must be acknowledged by the database before UI export."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from flask import Flask

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))
from backend.routes.pipeline_routes import pipeline_blueprint

SESSION = '11111111-1111-4111-8111-111111111111'
MAPPING = '22222222-2222-4222-8222-222222222222'
OTHER = '33333333-3333-4333-8333-333333333333'


class LegacyReviewRouteTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(id='owner-a', email='fixture@example.test')
        self.sessions = Mock()
        self.sessions.get_session.return_value = {'user_id': 'owner-a'}
        self.mapping = {'id': MAPPING, 'session_id': SESSION, 'new_url': '/old-target', 'needs_review': False}
        self.mappings = Mock()
        self.mappings.get_mapping_by_id.return_value = self.mapping
        self.query = Mock()
        self.query.eq.return_value = self.query
        self.query.is_.return_value = self.query
        self.mappings.client.table.return_value.update.return_value = self.query
        self.query.execute.return_value = SimpleNamespace(data=[dict(self.mapping)], error=None)
        for target, value in [
            ('backend.services.auth_service.SupabaseClient.get_client', Mock()),
            ('backend.services.auth_service.AuthService.verify_token', self.user),
            ('backend.routes.pipeline_routes.MigrationSessionDB', self.sessions),
            ('backend.routes.pipeline_routes.URLMappingDB', self.mappings),
        ]:
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        app = Flask(__name__)
        app.config['RATELIMIT_ENABLED'] = False
        app.register_blueprint(pipeline_blueprint, url_prefix='/api')
        self.client = app.test_client()

    def save(self, body, precondition=True):
        if isinstance(body, dict) and precondition and ('approved' in body or 'new_url' in body):
            body = {'expected_new_url': self.mapping['new_url'], **body}
        return self.client.patch(f'/api/results/{SESSION}/mappings/{MAPPING}', json=body,
                                 headers={'Authorization': 'Bearer fixture-only'})

    def test_success_requires_exact_saved_decision_and_owned_session_predicate(self):
        response = self.save({'approved': True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'success': True})
        self.assertEqual(self.query.eq.call_args_list, [unittest.mock.call('id', MAPPING), unittest.mock.call('session_id', SESSION), unittest.mock.call('new_url', '/old-target')])
        self.mappings.client.table.return_value.update.assert_called_once_with({'needs_review': False})
        self.query.execute.assert_called_once()

    def test_noop_or_mismatched_database_ack_never_claims_saved(self):
        for data in [[], None, [self.mapping, self.mapping],
                     [{**self.mapping, 'id': OTHER}], [{**self.mapping, 'session_id': OTHER}],
                     [{**self.mapping, 'needs_review': True}], [{**self.mapping, 'needs_review': 0}], [{'id': MAPPING, 'session_id': SESSION}]]:
            with self.subTest(data=data):
                self.query.execute.return_value = SimpleNamespace(data=data, error=None)
                response = self.save({'approved': True})
                self.assertEqual(response.status_code, 409)
                self.assertFalse(response.get_json()['success'])
                self.assertFalse(response.get_json()['retryable'])
                self.assertEqual(response.get_json()['next_action'], 'reload_results')

    def test_errors_are_unknown_outcome_and_never_expose_database_detail(self):
        self.query.execute.return_value = SimpleNamespace(data=[self.mapping], error='database-password')
        response = self.save({'approved': True})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('database-password', response.text)
        self.query.execute.side_effect = RuntimeError('database-password')
        response = self.save({'approved': True})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('database-password', response.text)
        self.assertEqual(response.get_json()['next_action'], 'reload_results')
        self.assertFalse(response.get_json()['retryable'])

    def test_changed_target_requires_fresh_review_without_explicit_approval(self):
        self.query.execute.return_value = SimpleNamespace(data=[{**self.mapping, 'new_url': '/new-target', 'needs_review': True}], error=None)
        response = self.save({'new_url': '/new-target'})
        self.assertEqual(response.status_code, 200)
        self.mappings.client.table.return_value.update.assert_called_once_with({'new_url': '/new-target', 'needs_review': True})

    def test_same_target_keeps_approval_and_explicit_repair_acceptance_can_approve(self):
        response = self.save({'new_url': '/old-target'})
        self.assertEqual(response.status_code, 200)
        self.mappings.client.table.return_value.update.assert_called_with({'new_url': '/old-target'})
        self.query.execute.return_value = SimpleNamespace(data=[{**self.mapping, 'new_url': '/accepted', 'needs_review': False}], error=None)
        response = self.save({'new_url': '/accepted', 'approved': True})
        self.assertEqual(response.status_code, 200)
        self.mappings.client.table.return_value.update.assert_called_with({'new_url': '/accepted', 'needs_review': False})

    def test_owner_and_session_mismatch_prevent_mutation(self):
        self.sessions.get_session.return_value = {'user_id': 'other-owner'}
        self.assertEqual(self.save({'approved': True}).status_code, 403)
        self.mappings.get_mapping_by_id.assert_not_called()
        self.sessions.get_session.return_value = {'user_id': 'owner-a'}
        self.mappings.get_mapping_by_id.return_value = {**self.mapping, 'session_id': OTHER}
        self.assertEqual(self.save({'approved': True}).status_code, 404)
        self.mappings.client.table.assert_not_called()

    def test_non_object_body_is_rejected_before_mutation(self):
        for body in [['approved'], 'approved', 42]:
            with self.subTest(body=body):
                self.assertEqual(self.save(body).status_code, 400)
        self.mappings.client.table.assert_not_called()

    def test_precondition_is_required_and_wrong_raw_target_prevents_mutation(self):
        response = self.save({'approved': True}, precondition=False)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['code'], 'review_target_precondition_required')
        for target in ['/OLD-target', '/old-target?', '/old%2Dtarget', None, '']:
            with self.subTest(target=target):
                response = self.save({'approved': True, 'expected_new_url': target})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.get_json()['code'], 'review_target_conflict')
        self.mappings.client.table.assert_not_called()

    def test_null_and_empty_targets_have_distinct_atomic_predicates(self):
        for target in [None, '']:
            with self.subTest(target=target):
                self.mapping['new_url'] = target
                self.query.reset_mock()
                self.query.execute.return_value = SimpleNamespace(data=[{**self.mapping, 'new_url': '/chosen', 'needs_review': True}], error=None)
                response = self.save({'new_url': '/chosen', 'expected_new_url': target})
                self.assertEqual(response.status_code, 200)
                if target is None:
                    self.query.is_.assert_called_once_with('new_url', 'null')
                else:
                    self.query.eq.assert_any_call('new_url', '')
                    self.query.is_.assert_not_called()
                opposite = '' if target is None else None
                self.assertEqual(self.save({'approved': True, 'expected_new_url': opposite}).status_code, 409)

    def test_target_change_after_lookup_cannot_be_approved_by_stale_request(self):
        current = dict(self.mapping)
        def raced_update():
            current.update(new_url='/changed-in-another-tab', needs_review=True)
            filters = dict(call.args for call in self.query.eq.call_args_list)
            # Simulate PostgREST's actual UPDATE predicate against current state.
            affected = all(current.get(key) == value for key, value in filters.items())
            if affected:
                current['needs_review'] = False
            return SimpleNamespace(data=[dict(current)] if affected else [], error=None)
        self.query.execute.side_effect = raced_update
        response = self.save({'approved': True, 'expected_new_url': '/old-target'})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(current['needs_review'])
        self.assertEqual(current['new_url'], '/changed-in-another-tab')

    def test_explicit_approved_edit_still_requires_original_target(self):
        self.assertEqual(self.save({'new_url': '/accepted', 'approved': True, 'expected_new_url': '/stale'}).status_code, 409)
        self.mappings.client.table.assert_not_called()
        self.query.execute.return_value = SimpleNamespace(data=[{**self.mapping, 'new_url': '/accepted', 'needs_review': False}], error=None)
        self.assertEqual(self.save({'new_url': '/accepted', 'approved': True, 'expected_new_url': '/old-target'}).status_code, 200)
        self.query.eq.assert_any_call('new_url', '/old-target')

    def test_metadata_only_write_retains_compatibility(self):
        self.query.execute.return_value = SimpleNamespace(data=[{**self.mapping, 'match_type': 'manual'}], error=None)
        self.assertEqual(self.save({'match_type': 'manual'}, precondition=False).status_code, 200)
        self.query.is_.assert_not_called()
        self.assertEqual(len(self.query.eq.call_args_list), 2)

    def test_formatter_keeps_null_empty_and_raw_target_preconditions(self):
        from backend.services.results_formatter import transform_mapping_for_frontend
        for target in [None, '', 'https://New.Example/Case/%2f?x=2&x=1#Section']:
            with self.subTest(target=target):
                result = transform_mapping_for_frontend({**self.mapping, 'old_url': '/old', 'new_url': target, 'confidence_score': .5})
                self.assertEqual(result['persistedNewUrl'], target)
                self.assertEqual(result['newUrl'], '' if target is None else target)

    def test_invalid_precondition_type_is_rejected(self):
        for target in [True, 0, [], {}, 'x' * 8193]:
            with self.subTest(target_type=type(target).__name__):
                self.assertEqual(self.save({'approved': True, 'expected_new_url': target}).status_code, 400)
        self.mappings.client.table.assert_not_called()

    def test_null_target_acknowledgment_must_still_include_target_field(self):
        self.mapping['new_url'] = None
        self.query.execute.return_value = SimpleNamespace(data=[{'id': MAPPING, 'session_id': SESSION, 'needs_review': True}], error=None)
        self.assertEqual(self.save({'approved': False, 'expected_new_url': None}).status_code, 409)
