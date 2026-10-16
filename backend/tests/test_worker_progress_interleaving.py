"""Native historical progress writes across queue reclaim, with real status HTTP readback."""
import asyncio
from contextlib import ExitStack
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID, uuid4

from flask import Flask
from backend.tests import test_migration_run_service as fixture
from backend.tests.helpers.pivot_native_client import NativeClient, json_value
from backend.tests.test_migration_planning import A
from backend.worker import RedirxWorker
from src.redirx.database import MigrationSessionDB


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'requires disposable loopback PostgreSQL')
class WorkerProgressInterleaving(unittest.TestCase):
    cleanup_db = classmethod(fixture.RunDatabaseAcceptance.cleanup_db.__func__)
    sql = fixture.RunDatabaseAcceptance.sql
    setUp = fixture.RunDatabaseAcceptance.setUp

    @classmethod
    def setUpClass(cls):
        fixture.RunDatabaseAcceptance.setUpClass.__func__(cls)
        cls.sql(cls, 'ALTER TABLE migration_sessions ADD COLUMN total_mappings integer DEFAULT 0')

    def status(self, sid, client):
        from backend.routes.user_routes import user_blueprint
        app = Flask(__name__)
        app.config['RATELIMIT_ENABLED'] = False
        app.register_blueprint(user_blueprint, url_prefix='/api/user')
        with patch('services.auth_service.AuthService.verify_token', return_value=SimpleNamespace(id=A)), patch(
            'backend.routes.user_routes.MigrationSessionDB', return_value=SimpleNamespace(client=client)):
            response = app.test_client().get(f'/api/user/sessions/{sid}/status', headers={'Authorization': 'Bearer fixture-only'})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def exercise(self, *, newer_completed=False, same_worker=False, missing_context=False):
        sid = str(uuid4())
        self.sql("""INSERT INTO migration_sessions(id,user_id,status,old_urls,new_urls,pipeline_type)
            VALUES(%s,%s,'pending','["https://old.example/a"]','["https://new.example/a"]','url_only')""", [sid,A])
        job = json_value(self.sql("SELECT * FROM claim_next_job('worker-a',now()+interval '10 minutes')")[0])
        self.assertEqual(job['id'], sid)
        if missing_context: job.pop('attempt_count')
        native = NativeClient(self.dsn)
        session_db = MigrationSessionDB.__new__(MigrationSessionDB)
        session_db.client = native
        owner = self
        snapshots = []
        class PausedPipeline:
            total_stages = 4
            current_stage_index = 1
            stage_names = ['Old attempt started', 'Old attempt stale stage', 'Middle', 'Finish']
            async def iterate(self):
                initial = owner.status(sid, native)
                owner.assertEqual(initial['current_stage'], None if missing_context else 1)
                owner.sql("UPDATE migration_sessions SET lease_expires_at=now()-interval '1 minute' WHERE id=%s", [sid])
                owner.sql('SELECT * FROM reclaim_expired_leases(3)')
                replacement = json_value(owner.sql('SELECT * FROM claim_next_job(%s,now()+interval \'10 minutes\')',
                    ['worker-a' if same_worker else 'worker-b'])[0])
                owner.assertEqual(replacement['attempt_count'], 2)
                owner.assertTrue(session_db.update_session_progress_for_attempt(UUID(sid), 4, 'Replacement current stage', 4,
                    worker_id='worker-a' if same_worker else 'worker-b', attempt_count=2))
                if newer_completed:
                    newer = RedirxWorker.__new__(RedirxWorker)
                    newer.worker_id = 'worker-a' if same_worker else 'worker-b'
                    owner.assertTrue(await newer.release_lease(UUID(sid), 'completed', attempt_count=2))
                snapshots.append(owner.status(sid, native))
                yield {'done': True}  # Resumes old worker's actual progress callback.
        worker = RedirxWorker.__new__(RedirxWorker)
        worker.worker_id = 'worker-a'
        worker.jobs_processed = 0
        worker.session_db = session_db
        worker._lease_extension_loop = AsyncMock()
        worker._apply_usage_accounting = Mock()
        with ExitStack() as stack:
            stack.enter_context(patch('backend.worker.SupabaseClient.get_client', return_value=native))
            stack.enter_context(patch('backend.worker.Pipeline', return_value=PausedPipeline()))
            stack.enter_context(patch('backend.worker.DeepPreviewService'))
            stack.enter_context(patch('backend.worker.URLMappingDB')).return_value.get_mappings_by_session.return_value = []
            stack.enter_context(patch('backend.worker.MatchRepairService')).return_value.repair_session.return_value = SimpleNamespace(as_dict=lambda: {})
            self.assertFalse(asyncio.run(worker.process_job(job)))
        after = self.status(sid, native)
        self.assertEqual(snapshots[0]['current_stage'], 4)
        self.assertEqual(after['status'], 'completed' if newer_completed else 'processing')
        self.assertEqual(after['current_stage'], 4)
        self.assertEqual(after['stage_name'], 'Replacement current stage')
        self.assertEqual(worker.jobs_processed, 0)
        worker._apply_usage_accounting.assert_not_called()
        print('PROGRESS_INTERLEAVING', {'completed':newer_completed,'same_worker':same_worker,
            'before':{k:snapshots[0][k] for k in ('status','current_stage','stage_name')},
            'after':{k:after[k] for k in ('status','current_stage','stage_name')}})

    def test_stale_progress_cannot_overwrite_replacement_while_processing(self): self.exercise()
    def test_stale_progress_cannot_overwrite_terminal_metadata(self): self.exercise(newer_completed=True)
    def test_same_worker_name_different_attempt_is_also_fenced(self): self.exercise(same_worker=True)
    def test_worker_missing_claim_context_never_uses_compatibility_writer(self): self.exercise(missing_context=True)

    def test_native_expiry_and_positive_receipt(self):
        sid = uuid4()
        self.sql("""INSERT INTO migration_sessions(id,user_id,status,locked_by,attempt_count,lease_expires_at)
            VALUES(%s,%s,'processing','current',1,now()-interval '1 minute')""", [sid,A])
        db = MigrationSessionDB.__new__(MigrationSessionDB); db.client = NativeClient(self.dsn)
        self.assertFalse(db.update_session_progress_for_attempt(sid,2,'Expired',4,worker_id='current',attempt_count=1))
        self.assertIsNone(self.status(sid,db.client)['current_stage'])
        self.sql("UPDATE migration_sessions SET lease_expires_at=now()+interval '10 minutes' WHERE id=%s", [sid])
        self.assertTrue(db.update_session_progress_for_attempt(sid,3,'Current',4,worker_id='current',attempt_count=1))
        self.assertEqual(self.status(sid,db.client)['stage_name'],'Current')



class ProgressAttemptContract(unittest.TestCase):
    def fixture(self, **changes):
        from datetime import datetime, timedelta, timezone
        from backend.tests.test_worker_finalization import Client
        sid = str(uuid4())
        row = {'id':sid,'locked_by':'worker-a','attempt_count':1,'status':'processing',
            'lease_expires_at':(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat(),
            'current_stage':1,'stage_name':'Original','total_stages':4,**changes}
        db = MigrationSessionDB.__new__(MigrationSessionDB)
        db.client = Client(row)
        return db, sid

    def report(self, db, sid, **context):
        return db.update_session_progress_for_attempt(UUID(sid),2,'Next',4,
            **({'worker_id':'worker-a','attempt_count':1} | context))

    def test_current_attempt_requires_positive_exact_readback(self):
        db,sid = self.fixture()
        self.assertTrue(self.report(db,sid))
        self.assertEqual(db.client.session['stage_name'],'Next')
        self.assertEqual(len(db.client.calls),1)

    def test_missing_invalid_context_never_issues_unfenced_write(self):
        for context in ({'worker_id':None},{'worker_id':''},{'worker_id':' '},
                        {'attempt_count':None},{'attempt_count':True},{'attempt_count':0},{'attempt_count':'1'}):
            with self.subTest(context=context):
                db,sid=self.fixture()
                self.assertFalse(self.report(db,sid,**context))
                self.assertEqual(db.client.calls,[])
                self.assertEqual(db.client.session['stage_name'],'Original')

    def test_noncurrent_terminal_expired_and_deleted_rows_cannot_change(self):
        for change in ({'locked_by':'worker-b'},{'attempt_count':2},{'status':'completed'},
                       {'status':'pending'},{'lease_expires_at':'2000-01-01T00:00:00+00:00'}):
            with self.subTest(change=change):
                db,sid=self.fixture(**change); before=dict(db.client.session)
                self.assertFalse(self.report(db,sid))
                self.assertEqual(db.client.session,before)
        db,sid=self.fixture();db.client.session=None
        self.assertFalse(self.report(db,sid))

    def test_missing_malformed_or_wrong_ack_is_not_confirmed_or_retried(self):
        for response in (SimpleNamespace(data=[]), SimpleNamespace(data=None), SimpleNamespace(data={},error=None),
                         SimpleNamespace(data=[{'id':'wrong'}]), SimpleNamespace(data=[],error='fixture-error')):
            with self.subTest(response=response):
                db,sid=self.fixture();db.client.response=lambda _:response
                self.assertFalse(self.report(db,sid))
                self.assertEqual(db.client.session['stage_name'],'Next')  # May already be committed.
                self.assertEqual(len(db.client.calls),1)

    def test_response_lost_after_write_is_not_retried(self):
        db,sid=self.fixture()
        def lost(_): raise TimeoutError('fixture acknowledgement lost')
        db.client.response=lost
        with self.assertRaises(TimeoutError): self.report(db,sid)
        self.assertEqual(db.client.session['stage_name'],'Next')
        self.assertEqual(len(db.client.calls),1)

    def test_explicit_nonworker_compatibility_helper_retains_four_argument_api(self):
        db,sid=self.fixture(status='completed')
        db.update_session_progress(UUID(sid),3,'Explicit compatibility writer',4)
        self.assertEqual(db.client.session['stage_name'],'Explicit compatibility writer')
