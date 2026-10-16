"""Current admission through real SQL and the real worker adapter; provider fixtures only."""
import os
import unittest
from unittest.mock import patch, Mock
from uuid import uuid4

from backend.services.jev_pipeline_service import JevService
from backend.services.migration_run_service import MigrationRunService
from backend.services.migration_quote_service import QuoteNotReadyError
from backend.tests.test_jev_pipeline import NativeJev
from backend.tests.test_migration_planning import A, ROOT


class CurrentAdmissionGates(unittest.TestCase):
    def test_default_off_rejects_before_rpc(self):
        repo=Mock()
        with patch.dict(os.environ,{'REDIRX_CURRENT_BILLING_ENABLED':'false','JEV_MVP_ENABLED':'true'}):
            with self.assertRaises(QuoteNotReadyError):JevService(repo).start_current(A,A,A,A,'key')
        repo.client.rpc.assert_not_called()

    def test_marker_outage_never_falls_back(self):
        repo=Mock()
        with patch('backend.services.migration_run_service.read_jev_state',side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):MigrationRunService(repo)._dispatch_activation({'mcp_run_id':A})
        with patch('backend.services.migration_run_service.read_jev_state',return_value={'billing_version':'redirx_2026_10'}),patch.dict(os.environ,{'REDIRX_CURRENT_BILLING_ENABLED':'false','MCP_PIVOT_ENABLED':'true','MCP_PIVOT_ACTIVATION':'test_only'}):
            with self.assertRaises(QuoteNotReadyError):MigrationRunService(repo)._dispatch_activation({'mcp_run_id':A})


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'),'disposable PostgreSQL required')
class PaidAdmission(unittest.TestCase):
    cleanup_db=classmethod(NativeJev.cleanup_db.__func__)
    sql=NativeJev.sql
    claim=NativeJev.claim

    @classmethod
    def setUpClass(cls):
        NativeJev.setUpClass.__func__(cls)
        for file in ('063_current_billing.sql','064_paid_jev_admission.sql'):
            cls.sql(cls,(ROOT/'database/migrations'/file).read_text())

    def test_paid_worker_retry_refine_and_permanent_export(self):
        from backend.services.migration_planning_service import MigrationPlanningService
        from backend.services.inventory_import_service import InventoryImportService
        from backend.services.jev_runner import JevPipelineRunner,DurableEmbeddingCache
        from src.redirx.jev.jev import JevClient
        from backend.tests.test_jev_capacity import FakeOpenAI,FakeTypeSafe
        from backend.services.migration_artifact_service import MigrationArtifactService
        import json
        with patch.dict(os.environ,{'REDIRX_CURRENT_BILLING_ENABLED':'true','REDIRX_STRIPE_MODE':'test','JEV_MVP_ENABLED':'true','MCP_PIVOT_ENABLED':'false'}):
            mid=MigrationPlanningService(self.repo).plan(A,{'old_site':'https://old.example','new_site':'https://new.example','idempotency_key':uuid4().hex})['migration_id']
            ids={}
            for side,n in [('old',501),('new',2)]:
                ids[side]=InventoryImportService(self.repo).import_inventory(A,mid,side,[{'url':f'https://{side}.example/page-{i}','provenance':['csv']} for i in range(n)],uuid4().hex)['inventory_id']
            c=self.sql('SELECT reserve_current_checkout(%s,\'payg\',%s,%s,%s,%s,false,\'price_payg\',false) c',[A,mid,ids['old'],ids['new'],uuid4().hex])[0]['c']
            self.sql("SELECT apply_current_checkout_payment(%s,'cs_worker','cus_worker',NULL,1100,'usd','evt_worker')",[c['id']])
            service=JevService(self.repo);key=uuid4().hex
            r=service.start_current(A,mid,ids['old'],ids['new'],key,checkout_id=c['id'])
            self.assertEqual(service.start_current(A,mid,ids['old'],ids['new'],key,checkout_id=c['id'])['run_id'],r['run_id'])
            job=self.claim();self.assertEqual(len(job['old_urls']),501)
            self.runs.authorize_dispatch(job,'worker-test')
            runner=JevPipelineRunner(self.native,lambda store:JevClient(store,FakeTypeSafe()),lambda store:DurableEmbeddingCache(store,FakeOpenAI()))
            with patch('src.redirx.jev.jev.time.sleep',lambda _:None):runner._run(job,'worker-test',lambda *args:None)
            reserved=self.sql('SELECT provider_requests FROM jev_runs WHERE run_id=%s',[r['run_id']])[0]['provider_requests']
            self.assertGreater(reserved,0)
            self.assertEqual(self.sql('SELECT count(*) n FROM jev_proposals WHERE run_id=%s',[r['run_id']])[0]['n'],501)
            runner._run(job,'worker-test',lambda *args:None)
            self.assertEqual(self.sql('SELECT provider_requests FROM jev_runs WHERE run_id=%s',[r['run_id']])[0]['provider_requests'],reserved)
            self.runs.finalize_session(job,'worker-test','completed')
            self.assertEqual(service.enrich(r['run_id'],{})['jev']['completed_proposals'],501)
            mapping=str(self.sql('SELECT id FROM url_mappings WHERE session_id=%s ORDER BY id LIMIT 1',[job['id']])[0]['id'])
            decision=[{'mapping_id':mapping,'expected_revision':0,'action':'set_target','target_url':'https://new.example/page-0','rationale':'Explicit user review'}]
            revision=self.sql('SELECT resolve_migration_match_decisions(%s,%s,%s,%s,%s,%s::jsonb) r',[A,mid,r['run_id'],A,uuid4().hex,json.dumps(decision)])[0]['r']['selection_revision']
            # Expiring execution must not expire completed output, manual edits or downloads.
            self.sql("UPDATE redirx_billing_usage SET expires_at=now()-interval '1 day' WHERE run_id=%s",[r['run_id']])
            with self.assertRaises(Exception):service.refine(A,mid,r['run_id'],1,'expired-refine')
            artifacts=MigrationArtifactService(self.repo)
            artifact=artifacts.create_artifact(A,mid,r['run_id'],idempotency_key=uuid4().hex,fmt='nginx',selection_revision=str(revision),partial_policy='allow')
            self.assertGreater(artifact['included_count'],0)
            download=artifacts.authorize_download(A,mid,artifact['id'])
            self.assertEqual(download['resource_type'],'artifact_download')
            self.assertEqual(self.sql('SELECT count(*) n FROM redirx_billing_usage WHERE checkout_id=%s',[c['id']])[0]['n'],1)
            import psycopg
            with self.assertRaisesRegex(psycopg.Error,'immutable'):
                self.sql('DELETE FROM migration_mapping_decision_events WHERE user_id=%s RETURNING id',[A])
            self.sql('DELETE FROM auth.users WHERE id=%s RETURNING id',[A])
            for table in ('migration_records','migration_runs','migration_artifacts','redirx_billing_checkouts','redirx_billing_usage'):
                self.assertEqual(self.sql(f'SELECT count(*) n FROM {table} WHERE user_id=%s',[A])[0]['n'],0,table)
            self.assertEqual(self.sql('SELECT count(*) n FROM redirx_billing_events WHERE checkout_id=%s',[c['id']])[0]['n'],0)
            print('CURRENT_PAID_WORKER_VERIFIED: 501 proposals, retry zero new provider calls, expired-run export/download succeeds, account graph cleanup succeeds')
