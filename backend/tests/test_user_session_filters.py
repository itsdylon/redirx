import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flask import Flask


BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

from backend.routes.user_routes import user_blueprint


class _MockQuery:
    def __init__(self, data):
        self._data = data
        self.eq_calls: list[tuple[str, object]] = []

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, column, value):
        self.eq_calls.append((column, value))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args):
        return self

    def execute(self):
        return SimpleNamespace(data=self._data)


class _MockClient:
    def __init__(self, session_rows):
        self.session_query = _MockQuery(session_rows)
        self.mapping_query = _MockQuery([])

    def table(self, name):
        if name == "migration_sessions":
            return self.session_query
        if name == "url_mappings":
            return self.mapping_query
        raise AssertionError(f"Unexpected table access: {name}")


class UserSessionFilterTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(user_blueprint, url_prefix="/api/user")
        self.client = self.app.test_client()
        self.user = SimpleNamespace(id="user-1", email="user@example.com")
        self.headers = {"Authorization": "Bearer test-token"}

    def test_dashboard_uses_owner_scoped_snapshot(self):
        owner='10000000-0000-0000-0000-000000000001'
        data={'user_id':owner,'total_sessions':0,'total_redirects':0,'total_approved':0,
              'confidence_sum':0,'confidence_count':0,'confidence_breakdown':{'exact':0,'high':0,'medium':0,'low':0},'recent_sessions':[]}
        admin=Mock()
        admin.rpc.return_value.execute.return_value=SimpleNamespace(data=data,error=None)
        with patch('services.auth_service.AuthService.verify_token',return_value=SimpleNamespace(id=owner)), patch(
            'backend.routes.user_routes.SupabaseClient.get_admin_client',return_value=admin):
            response=self.client.get('/api/user/dashboard',headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['total_sessions'],0)
        admin.rpc.assert_called_once_with('legacy_dashboard_snapshot',{'p_user_id':owner})
        admin.table.assert_not_called()

    def test_dashboard_missing_migration_or_malformed_snapshot_fails_without_fake_zeros(self):
        owner='10000000-0000-0000-0000-000000000001'
        for value in (None,{}, {'user_id':'other'}, {'user_id':owner,'total_sessions':True}):
            admin=Mock()
            admin.rpc.return_value.execute.return_value=SimpleNamespace(data=value,error=None)
            with patch('services.auth_service.AuthService.verify_token',return_value=SimpleNamespace(id=owner)), patch(
                'backend.routes.user_routes.SupabaseClient.get_admin_client',return_value=admin):
                response=self.client.get('/api/user/dashboard',headers=self.headers)
            self.assertEqual(response.status_code,503)
            self.assertFalse(response.json['success'])
            self.assertNotIn('total_sessions',response.json)
            admin.table.assert_not_called()
        admin=Mock()
        admin.rpc.return_value.execute.side_effect=RuntimeError('private database exception')
        with patch('services.auth_service.AuthService.verify_token',return_value=SimpleNamespace(id=owner)), patch(
            'backend.routes.user_routes.SupabaseClient.get_admin_client',return_value=admin):
            response=self.client.get('/api/user/dashboard',headers=self.headers)
        self.assertEqual(response.status_code,503)
        self.assertNotIn('private',response.get_data(as_text=True))
        admin.rpc.return_value.execute.assert_called_once()

    def test_sessions_list_excludes_preview_sessions(self):
        mock_client = _MockClient(session_rows=[])
        mock_session_db = SimpleNamespace(client=mock_client)

        with patch("services.auth_service.AuthService.verify_token", return_value=self.user):
            with patch("backend.routes.user_routes.SupabaseClient.get_admin_client", return_value=mock_client):
                response = self.client.get("/api/user/sessions", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["sessions"], [])
        self.assertIn(("is_preview", False), mock_client.session_query.eq_calls)

    def test_delete_session_uses_one_atomic_admin_rpc(self):
        session='20000000-0000-0000-0000-000000000001'
        user=SimpleNamespace(id='10000000-0000-0000-0000-000000000001')
        admin=Mock()
        admin.rpc.return_value.execute.return_value=SimpleNamespace(data={'session_id':session,'deleted_count':2})
        with patch('services.auth_service.AuthService.verify_token',return_value=user), patch(
            'backend.routes.user_routes.SupabaseClient.get_admin_client',return_value=admin):
            response=self.client.delete('/api/user/sessions/'+session,headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['success'])
        admin.rpc.assert_called_once_with('delete_owned_legacy_session',{'p_user_id':user.id,'p_session_id':session})
        admin.table.assert_not_called()

    def test_delete_unknown_result_is_not_success_or_a_fallback_delete(self):
        session='20000000-0000-0000-0000-000000000001'
        user=SimpleNamespace(id='10000000-0000-0000-0000-000000000001')
        for result in (None,{}, {'session_id':session,'deleted_count':0}):
            admin=Mock()
            admin.rpc.return_value.execute.return_value=SimpleNamespace(data=result)
            with patch('services.auth_service.AuthService.verify_token',return_value=user), patch(
                'backend.routes.user_routes.SupabaseClient.get_admin_client',return_value=admin):
                response=self.client.delete('/api/user/sessions/'+session,headers=self.headers)
            self.assertEqual(response.status_code,503)
            self.assertFalse(response.json['success'])
            admin.table.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
