"""Atomic historical deletion against production SQL guards, disposable DB only."""
import os
import unittest
from uuid import uuid4
from backend.tests.test_migration_planning import A, B, ROOT
from backend.tests import test_migration_run_service as run_fixture


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'requires disposable loopback PostgreSQL')
class AtomicSessionDelete(unittest.TestCase):
    cleanup_db=classmethod(run_fixture.RunDatabaseAcceptance.cleanup_db.__func__)
    sql=run_fixture.RunDatabaseAcceptance.sql
    fixture=run_fixture.RunDatabaseAcceptance.fixture
    setUp=run_fixture.RunDatabaseAcceptance.setUp

    @classmethod
    def setUpClass(cls):
        from backend.tests.test_artifact_decision_projection import ArtifactDecisionProjection
        ArtifactDecisionProjection.setUpClass.__func__(cls)
        for migration in ('058_free_migration_rolling_limit.sql', '062_jev_url_harness.sql', '065_atomic_legacy_session_delete.sql'):
            cls.sql(cls,(ROOT/'database/migrations'/migration).read_text())
        cls.sql(cls,'ALTER TABLE migration_sessions ADD CONSTRAINT fixture_preview_parent FOREIGN KEY(source_session_id) REFERENCES migration_sessions(id) ON DELETE CASCADE')

    def legacy(self, owner=A, parent=None):
        sid=str(uuid4())
        self.sql("INSERT INTO migration_sessions(id,user_id,status,source_session_id) VALUES(%s,%s,'completed',%s)",[sid,owner,parent])
        self.sql("INSERT INTO url_mappings(session_id,old_url,new_url) VALUES(%s,'https://old.example/a','https://new.example/a')",[sid])
        self.sql("INSERT INTO webpage_embeddings(session_id,url,site_type,embedding) VALUES(%s,'https://old.example/a','old','[]')",[sid])
        return sid

    def delete(self, sid, owner=A):
        return self.native.rpc('delete_owned_legacy_session',{'p_user_id':owner,'p_session_id':sid}).execute().data

    def counts(self,sid):
        return [self.sql(f'SELECT count(*) AS n FROM {table} WHERE {column}=%s',[sid])[0]['n']
                for table,column in [('migration_sessions','id'),('url_mappings','session_id'),('webpage_embeddings','session_id')]]

    def test_owned_nested_previews_delete_together_and_other_owner_survives(self):
        parent=self.legacy(); child=self.legacy(parent=parent); grandchild=self.legacy(parent=child); other=self.legacy(B)
        self.assertEqual(self.delete(parent),{'session_id':parent,'deleted_count':3})
        for sid in (parent,child,grandchild): self.assertEqual(self.counts(sid),[0,0,0])
        self.assertEqual(self.counts(other),[1,1,1])

    def test_late_foreign_key_failure_rolls_back_earlier_evidence_deletes(self):
        sid=self.legacy()
        self.sql('CREATE TABLE IF NOT EXISTS fixture_delete_blocker(session_id uuid REFERENCES migration_sessions(id))')
        self.sql('INSERT INTO fixture_delete_blocker VALUES(%s)',[sid])
        with self.assertRaises(Exception):self.delete(sid)
        self.assertEqual(self.counts(sid),[1,1,1])
        self.sql('DELETE FROM fixture_delete_blocker WHERE session_id=%s',[sid])
        self.assertEqual(self.delete(sid)['deleted_count'],1)

    def test_wrong_owner_and_cross_owner_descendant_never_delete(self):
        sid=self.legacy()
        with self.assertRaises(Exception):self.delete(sid,B)
        child=self.legacy(B,parent=sid)
        with self.assertRaises(Exception):self.delete(sid)
        self.assertEqual(self.counts(sid),[1,1,1]); self.assertEqual(self.counts(child),[1,1,1])

    def test_pivot_session_is_not_a_legacy_cleanup_bypass(self):
        f=self.fixture();run=self.runs.start_run(A,f['migration'],f['old'],f['new'],f['quote'],uuid4().hex)
        with self.assertRaises(Exception):self.delete(run['session_id'])
        self.assertEqual(self.counts(run['session_id'])[0],1)
        self.assertEqual(self.sql('SELECT count(*) AS n FROM migration_runs WHERE id=%s',[run['run_id']])[0]['n'],1)

    def test_only_service_role_may_execute(self):
        for role in ('anon','authenticated'):
            self.assertFalse(self.sql("SELECT has_function_privilege(%s,'public.delete_owned_legacy_session(uuid,uuid)','EXECUTE') AS allowed",[role])[0]['allowed'])
        self.assertTrue(self.sql("SELECT has_function_privilege('service_role','public.delete_owned_legacy_session(uuid,uuid)','EXECUTE') AS allowed")[0]['allowed'])

    def test_suppressed_final_delete_is_not_reported_as_success(self):
        sid=self.legacy()
        self.sql("CREATE FUNCTION fixture_suppress_delete() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$")
        self.sql("CREATE TRIGGER fixture_suppress_delete BEFORE DELETE ON migration_sessions FOR EACH ROW EXECUTE FUNCTION fixture_suppress_delete()")
        try:
            with self.assertRaises(Exception):self.delete(sid)
            self.assertEqual(self.counts(sid),[1,1,1])
        finally:
            self.sql('DROP TRIGGER fixture_suppress_delete ON migration_sessions')
            self.sql('DROP FUNCTION fixture_suppress_delete()')

    def test_http_positive_result_missing_owner_and_protected_session(self):
        from flask import Flask
        from types import SimpleNamespace
        from unittest.mock import patch
        from backend.routes.user_routes import user_blueprint
        from backend.extensions import limiter
        app=Flask(__name__);app.config['RATELIMIT_ENABLED']=False
        app.register_blueprint(user_blueprint,url_prefix='/api/user')
        sid=self.legacy(); other=self.legacy(B)
        f=self.fixture();run=self.runs.start_run(A,f['migration'],f['old'],f['new'],f['quote'],uuid4().hex)
        with patch('services.auth_service.AuthService.verify_token',return_value=SimpleNamespace(id=A)), patch(
            'backend.routes.user_routes.SupabaseClient.get_admin_client',return_value=self.native),patch.object(limiter,'enabled',False):
            client=app.test_client(); headers={'Authorization':'Bearer fixture-only'}
            response=client.delete('/api/user/sessions/'+sid,headers=headers)
            self.assertEqual(response.status_code,200);self.assertTrue(response.json['success'])
            self.assertEqual(self.counts(sid),[0,0,0])
            self.assertEqual(client.delete('/api/user/sessions/'+sid,headers=headers).status_code,404)
            self.assertEqual(client.delete('/api/user/sessions/'+other,headers=headers).status_code,404)
            self.assertEqual(client.delete('/api/user/sessions/'+run['session_id'],headers=headers).status_code,409)
        self.assertEqual(self.counts(other),[1,1,1])

    def test_processing_delete_and_reclaimed_attempt_fence_late_worker_completion(self):
        import asyncio
        from unittest.mock import patch
        from uuid import UUID
        from backend.worker import RedirxWorker
        worker = RedirxWorker.__new__(RedirxWorker)
        worker.worker_id = 'fixture-worker'
        def running():
            sid = self.legacy()
            self.sql("UPDATE migration_sessions SET status='processing', locked_by='fixture-worker', attempt_count=1, lease_expires_at=now()+interval '10 minutes' WHERE id=%s", [sid])
            return sid
        deleted, reclaimed, current, expired = (running() for _ in range(4))
        self.assertEqual(self.delete(deleted)['deleted_count'], 1)
        self.sql("UPDATE migration_sessions SET attempt_count=2 WHERE id=%s", [reclaimed])
        self.sql("UPDATE migration_sessions SET lease_expires_at=now()-interval '1 minute' WHERE id=%s", [expired])
        with patch('backend.worker.SupabaseClient.get_client', return_value=self.native):
            for sid in (deleted, reclaimed, expired):
                self.assertFalse(asyncio.run(worker.release_lease(UUID(sid), 'completed', attempt_count=1)))
            self.assertTrue(asyncio.run(worker.extend_lease(UUID(current), attempt_count=1)))
            self.assertTrue(asyncio.run(worker.release_lease(UUID(current), 'completed', attempt_count=1)))
        self.assertEqual(self.counts(deleted), [0, 0, 0])
        for sid in (reclaimed, expired):
            self.assertEqual(self.sql('SELECT status FROM migration_sessions WHERE id=%s', [sid])[0]['status'], 'processing')
        actual = self.sql('SELECT status,locked_by,lease_expires_at,completed_at FROM migration_sessions WHERE id=%s', [current])[0]
        self.assertEqual(actual['status'], 'completed')
        self.assertIsNone(actual['locked_by'])
        self.assertIsNone(actual['lease_expires_at'])
        self.assertIsNotNone(actual['completed_at'])
