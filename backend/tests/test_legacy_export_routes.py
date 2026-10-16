"""Authenticated historical preview and download share the real renderer."""
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
class LegacyExportRouteTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(id='owner-a', email='fixture@example.test')
        self.sessions = Mock(); self.sessions.get_session.return_value = {'user_id':'owner-a'}
        self.quota = Mock(); self.quota.get_plan.return_value = 'free'
        self.mappings = Mock(); self.rows = [
            {'id':'a','old_url':'https://old.example/A?x=1&x=2','new_url':'https://new.example/B?y=2&y=1','confidence_score':.9,'needs_review':False},
            {'id':'held','old_url':'/held','new_url':'/no','confidence_score':.9,'needs_review':True},
            {'id':'low','old_url':'/low','new_url':'/yes','confidence_score':.4,'needs_review':False},
        ]
        self.mappings.get_mappings_by_session.return_value = self.rows
        self.allowed = SimpleNamespace(allowed=True)
        for target, value in [
            ('backend.services.auth_service.SupabaseClient.get_client',Mock()),
            ('backend.services.auth_service.AuthService.verify_token',self.user),
            ('backend.routes.pipeline_routes.MigrationSessionDB',self.sessions),
            ('backend.routes.pipeline_routes.UserQuotaDB',self.quota),
            ('backend.routes.pipeline_routes.URLMappingDB',self.mappings),
            ('backend.routes.pipeline_routes.entitlement_service.check_export',self.allowed),
        ]:
            patcher=patch(target, return_value=value); patcher.start(); self.addCleanup(patcher.stop)
        recorder=patch('backend.routes.pipeline_routes.entitlement_service.record_export');self.record=recorder.start();self.addCleanup(recorder.stop)
        app=Flask(__name__);app.config['RATELIMIT_ENABLED']=False;app.register_blueprint(pipeline_blueprint,url_prefix='/api');self.client=app.test_client()
    def get(self, query='', headers=True):
        return self.client.get(f'/api/results/{SESSION}/export?{query}',headers={'Authorization':'Bearer fixture-only'} if headers else {})
    def test_preview_and_download_use_same_bytes_and_exact_selection_counts(self):
        args='format=csv&url_format=full&confidence_levels=high'
        response=self.get(args+'&preview=true');self.assertEqual(response.status_code,200)
        preview=response.get_json();self.assertEqual(preview['included_count'],1);self.assertEqual(preview['excluded_count'],1)
        self.assertEqual(preview['excluded'],[{'mapping_id':'held','reason':'held_or_rejected'}]);self.record.assert_not_called()
        download=self.get(args);self.assertEqual(download.status_code,200);self.assertEqual(download.text,preview['content'])
        self.assertIn('/A?x=1&x=2',download.text);self.assertIn('/B?y=2&y=1',download.text)
        self.assertEqual(download.headers['X-Redirect-Count'],'1');self.assertEqual(download.headers['X-Redirect-Excluded-Count'],'1');self.assertIn('redirects.csv',download.headers['Content-Disposition']);self.record.assert_called_once()
    def test_legacy_default_and_min_confidence_remain_compatible(self):
        self.assertEqual(self.get('preview=true').get_json()['included_count'],2)
        self.assertEqual(self.get('preview=true&min_confidence=0.85').get_json()['included_count'],1)
    def test_arbitrary_confidence_subset_does_not_include_middle_band(self):
        self.rows.append({'id':'medium','old_url':'/m','new_url':'/n','confidence_score':.7})
        preview=self.get('preview=true&confidence_levels=high,low').get_json()
        self.assertEqual(preview['included_count'],2);self.assertNotIn('/m,',preview['content'])
        self.rows.append({'id':'boundary','old_url':'/boundary','new_url':'/destination','confidence_score':.62})
        self.assertEqual(self.get('preview=true&confidence_levels=low').get_json()['included_count'],2)
    def test_preview_is_authenticated_owned_and_entitled_before_rows_are_read(self):
        self.assertEqual(self.get('preview=true',headers=False).status_code,401)
        self.sessions.get_session.return_value={'user_id':'other'};self.assertEqual(self.get('preview=true').status_code,403);self.mappings.get_mappings_by_session.assert_not_called()
        self.sessions.get_session.return_value={'user_id':'owner-a'}
        denied=SimpleNamespace(allowed=False,code='export_required',user_message='Existing entitlement required',next_action='review_plan',extra={})
        with patch('backend.routes.pipeline_routes.entitlement_service.check_export',return_value=denied): self.assertEqual(self.get('preview=true').status_code,402)
        self.mappings.get_mappings_by_session.assert_not_called();self.record.assert_not_called()
    def test_unsupported_query_sensitive_platform_returns_recoverable_422(self):
        response=self.get('format=apache&preview=true');self.assertEqual(response.status_code,422);self.assertEqual(response.get_json()['code'],'unsupported_export_input');self.assertIn('query',response.get_json()['error']);self.record.assert_not_called()
    def test_custom_domain_uses_shared_renderer_and_rejects_credentials_and_paths(self):
        response=self.get('preview=true&format=csv&url_format=custom&new_domain=https://live.example')
        self.assertEqual(response.status_code,200);self.assertIn('https://live.example/B?y=2&y=1',response.get_json()['content'])
        for origin in ['https://name:secret@live.example','https://live.example/path','javascript:alert(1)','https://live.example:bad', 'https://live.example\x7f', 'https://bad_host.example', 'https://-bad.example', 'https://bad..example', 'https://live.example:', 'https://live.example?', 'https://live.example#', 'https://live.example/\\bad']:
            with self.subTest(origin=origin): self.assertEqual(self.client.get(f'/api/results/{SESSION}/export',query_string={'preview':'true','url_format':'custom','new_domain':origin},headers={'Authorization':'Bearer fixture-only'}).status_code,400)
    def test_bad_selection_is_not_silently_reinterpreted(self):
        for query in ['confidence_levels=', 'confidence_levels=high,unknown','min_confidence=nan','url_format=custom','format=invalid']:
            with self.subTest(query=query): self.assertEqual(self.get(query).status_code,400)

    def test_custom_origin_validation_keeps_raw_host_and_url_suffix(self):
        self.rows[0]['new_url'] = 'https://new.example/Case/%2f?x=2&x=1#Section'
        response = self.client.get(f'/api/results/{SESSION}/export', query_string={
            'preview': 'true', 'format': 'csv', 'url_format': 'custom',
            'new_domain': 'https://LIVE.Example:443/',
        }, headers={'Authorization': 'Bearer fixture-only'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('https://LIVE.Example:443/Case/%2f?x=2&x=1#Section', response.get_json()['content'])
        self.record.assert_not_called()

    def test_historical_undecided_rows_remain_exportable(self):
        self.rows[:] = [{'old_url': '/old', 'new_url': '/new', 'confidence_score': .9}]
        preview = self.get('preview=true').get_json()
        self.assertEqual(preview['included_count'], 1)
        self.assertEqual(preview['excluded_count'], 0)
        self.assertIn('/old,/new', preview['content'])
