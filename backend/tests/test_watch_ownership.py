"""Existing Watch replay must authorize before readback or reactivation."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'src'))
from backend.services.watch_service import WatchService
from backend.tests import test_migration_run_service as fixture
from backend.tests.helpers.pivot_native_client import NativeClient
from backend.tests.test_migration_planning import A, B, ROOT


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'requires disposable loopback PostgreSQL')
class WatchOwnership(unittest.TestCase):
    cleanup_db = classmethod(fixture.RunDatabaseAcceptance.cleanup_db.__func__)
    sql = fixture.RunDatabaseAcceptance.sql

    @classmethod
    def setUpClass(cls):
        fixture.RunDatabaseAcceptance.setUpClass.__func__(cls)
        # Actual 001 columns omitted by the minimal pre-032 shared fixture.
        cls.sql(cls, 'ALTER TABLE migration_sessions ADD COLUMN old_site_domain TEXT, ADD COLUMN new_site_domain TEXT')
        cls.sql(cls, (ROOT/'database/migrations/029_add_post_cutover_watch.sql').read_text())
        cls.sql(cls, 'GRANT ALL ON redirect_watches,watch_checks,watch_issues TO service_role')

    def setUp(self):
        self.service = WatchService(client=NativeClient(self.dsn))
        from backend.routes.watch_routes import watch_blueprint
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
        self.app.register_blueprint(watch_blueprint, url_prefix='/api/watches')
        capture_patch = patch('backend.services.watch_service.capture')
        self.capture = capture_patch.start()
        self.addCleanup(capture_patch.stop)

    def session(self, owner):
        sid = str(uuid4())
        self.sql('''INSERT INTO migration_sessions(id,user_id,status,old_site_domain,new_site_domain)
            VALUES(%s,%s,'completed','old.example','new.example')''', [sid,owner])
        return sid

    def watch(self, session_owner, status, *, watch_owner=None):
        sid = self.session(session_owner)
        wid = str(uuid4())
        self.sql('''INSERT INTO redirect_watches(id,user_id,session_id,old_domain,status,alert_email,next_check_at)
            VALUES(%s,%s,%s,'private-old.example',%s,'private-alert@example.invalid','2030-01-01')''',
            [wid,watch_owner or session_owner,sid,status])
        return sid,wid

    def post(self, sid, owner=A, plan='agency'):
        with patch('backend.services.auth_service.AuthService.verify_token', return_value=SimpleNamespace(id=owner)), patch(
            'backend.routes.watch_routes.WatchService', return_value=self.service), patch(
            'backend.routes.watch_routes.UserQuotaDB', return_value=SimpleNamespace(get_plan=lambda _: plan)):
            return self.app.test_client().post('/api/watches', json={'session_id':sid},
                headers={'Authorization':'Bearer fixture-only'})

    def persisted(self, wid):
        return self.sql('SELECT * FROM redirect_watches WHERE id=%s', [wid])[0]

    def test_browser_cannot_replay_or_reactivate_another_owners_watch(self):
        for status in ('active','paused','ended'):
            with self.subTest(status=status):
                sid,wid = self.watch(B,status)
                before = self.persisted(wid)
                response = self.post(sid)
                self.assertEqual(response.status_code,404,response.json)
                self.assertEqual(response.json,{'success':False,'error':'Migration not found.'})
                self.assertEqual(self.persisted(wid),before)
                self.capture.assert_not_called()
        self.assertEqual(self.sql('SELECT count(*) AS n FROM watch_checks')[0]['n'],0)

    def test_service_replay_requires_both_session_and_watch_owner(self):
        for status in ('active','paused','ended'):
            with self.subTest(status=status):
                # A session/watch owner inconsistency must not become a readback
                # or authorization bypass even though the caller owns the session.
                sid,wid = self.watch(A,status,watch_owner=B)
                before = self.persisted(wid)
                with self.assertRaisesRegex(ValueError,'^session_not_found$'):
                    self.service.create_watch(A,sid)
                self.assertEqual(self.persisted(wid),before)
                self.capture.assert_not_called()

    def test_rightful_owner_active_replay_and_paused_ended_resume_are_preserved(self):
        for status in ('active','paused','ended'):
            with self.subTest(status=status):
                sid,wid = self.watch(A,status)
                before = self.persisted(wid)
                response = self.post(sid)
                self.assertEqual(response.status_code,201,response.json)
                self.assertEqual(response.json['watch']['id'],wid)
                self.assertEqual(response.json['watch']['user_id'],A)
                self.assertEqual(response.json['watch']['status'],'active')
                self.assertEqual(response.json['watch']['alert_email'],'private-alert@example.invalid')
                after = self.persisted(wid)
                if status == 'active':
                    self.assertEqual(before,after)
                    self.capture.assert_not_called()
                else:
                    self.assertEqual(after['status'],'active')
                    self.assertLess(after['next_check_at'],before['next_check_at'])
                    self.capture.assert_called_once()
                    self.assertEqual(self.capture.call_args.kwargs['user_id'],A)
                    self.assertEqual(self.capture.call_args.kwargs['properties'],{'activation':'reactivated'})
                self.assertEqual(self.sql('SELECT count(*) AS n FROM redirect_watches WHERE session_id=%s',[sid])[0]['n'],1)
                self.capture.reset_mock()

    def test_new_watch_and_missing_or_foreign_session_preserve_authority(self):
        sid = self.session(A)
        response = self.post(sid)
        self.assertEqual(response.status_code,201,response.json)
        watch = response.json['watch']
        self.assertEqual(watch['user_id'],A)
        self.assertEqual(watch['session_id'],sid)
        self.assertEqual(self.persisted(watch['id'])['status'],'active')
        self.capture.assert_called_once()
        self.capture.reset_mock()
        for missing_or_foreign in (str(uuid4()),self.session(B)):
            denied = self.post(missing_or_foreign)
            self.assertEqual(denied.status_code,404,denied.json)
            self.assertEqual(denied.json,{'success':False,'error':'Migration not found.'})
            self.assertEqual(self.sql('SELECT count(*) AS n FROM redirect_watches WHERE session_id=%s',[missing_or_foreign])[0]['n'],0)
            self.capture.assert_not_called()

    def test_existing_paid_activation_policy_is_not_bypassed_by_replay(self):
        sid,wid = self.watch(A,'paused')
        before = self.persisted(wid)
        response = self.post(sid,plan='free')
        self.assertEqual(response.status_code,403,response.json)
        self.assertEqual(response.json['code'],'watch_requires_paid_plan')
        self.assertEqual(self.persisted(wid),before)
        self.capture.assert_not_called()


if __name__ == '__main__':
    unittest.main()
