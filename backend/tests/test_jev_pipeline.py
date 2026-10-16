"""Jev production adapter: no provider/network calls; native production SQL chain."""
import asyncio
import json
import os
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from backend.tests import test_artifact_decision_projection as fixture
from backend.tests import test_migration_run_service as run_fixture
from backend.tests.test_migration_planning import A,B,ROOT
from backend.services.jev_pipeline_service import JevService
from backend.services.jev_store import DurableStore
from backend.services.jev_runner import JevPipelineRunner
from backend.services.mapping_decision_service import MappingDecisionService


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'),'disposable PostgreSQL required')
class NativeJev(unittest.TestCase):
    cleanup_db=classmethod(run_fixture.RunDatabaseAcceptance.cleanup_db.__func__)
    sql=run_fixture.RunDatabaseAcceptance.sql
    fixture=run_fixture.RunDatabaseAcceptance.fixture
    claim=run_fixture.RunDatabaseAcceptance.claim

    @classmethod
    def setUpClass(cls):
        fixture.ArtifactDecisionProjection.setUpClass.__func__(cls)
        cls.sql(cls,(ROOT/'database/migrations/058_free_migration_rolling_limit.sql').read_text())
        cls.sql(cls,(ROOT/'database/migrations/062_jev_url_harness.sql').read_text())

    def setUp(self):
        run_fixture.RunDatabaseAcceptance.setUp(self)
        self.sql("UPDATE migration_runs SET created_at=clock_timestamp()-interval '2 days'")
        self.sql('DELETE FROM jev_provider_daily_budget')
        self.service=JevService(self.repo)
        from backend.services.migration_quote_service import MigrationQuoteService
        self.quotes=MigrationQuoteService(self.repo)

    def start(self,f,key=None):
        return self.service.start(f['user'],f['migration'],f['old'],f['new'],f['quote'],key or uuid4().hex)

    def prepared(self):
        f=self.fixture();r=self.start(f);job=self.claim();self.runs.authorize_dispatch(job,'worker-test')
        store=DurableStore(self.native,job,'worker-test');state=store.rpc('prepare_jev_pass')
        return f,r,job,store,state

    def proposal(self,store,f,state,index=0):
        return store.rpc('save_jev_proposal',p_pass=state['pass'],p_seed_revision=state['pass_seed_revision'],
            p_old_url=f'https://old.example/Page/{index}?q=A',p_proposal={'target_url':f'https://new.example/Page/{index}?q=A','confidence':0.9,'confirmation_required':True})

    def test_chain_free_marker_replay_ownership_and_existing_quota(self):
        f=self.fixture();key=uuid4().hex;r=self.start(f,key)
        self.assertEqual(self.start(f,key)['run_id'],r['run_id'])
        self.assertEqual(self.service.state(r['run_id'])['pass'],1)
        for _ in range(4):self.start(f)
        with self.assertRaises(Exception) as error:self.start(f)
        self.assertIn('Five new free',str(error.exception))
        with self.assertRaises(Exception):self.service.refine(B,f['migration'],r['run_id'],0,'wrong-owner')

    def test_review_seed_refine_immutable_engine_and_idempotency(self):
        f,r,job,store,state=self.prepared();mid=self.proposal(store,f,state)
        self.proposal(store,f,state,1)
        self.runs.finalize_session(job,'worker-test','completed')
        decisions=MappingDecisionService(self.native)
        decision={'mapping_id':mid,'expected_revision':0,'action':'set_target','target_url':'https://new.example/Page/0?q=A'}
        outcome=decisions.resolve_matches(A,f['migration'],r['run_id'],[decision],uuid4().hex)
        self.assertEqual(outcome['outcomes'][0]['code'],'ok')
        self.assertEqual(self.service.state(r['run_id'])['seed_revision'],1)
        key=uuid4().hex;second=self.service.refine(A,f['migration'],r['run_id'],1,key)
        self.assertEqual(second['status'],'queued')
        self.assertTrue(self.service.refine(A,f['migration'],r['run_id'],1,key)['replayed'])
        self.assertEqual(self.service.state(r['run_id'])['pass'],2)
        job2=self.claim();self.assertGreater(job2['attempt_count'],job['attempt_count']);self.runs.authorize_dispatch(job2,'worker-test');store2=DurableStore(self.native,job2,'worker-test');state2=store2.rpc('prepare_jev_pass')
        self.assertEqual(state2['seeds'][0]['new_url'],decision['target_url'])
        # New inference cannot replace a user decision or immutable engine row.
        store2.rpc('save_jev_proposal',p_pass=2,p_seed_revision=1,p_old_url='https://old.example/Page/0?q=A',p_proposal={'target_url':'https://new.example/Page/1?q=A','confidence':0.8})
        rows=self.sql('SELECT new_url,needs_review FROM url_mappings WHERE id=%s',[mid])
        self.assertEqual(rows[0]['new_url'],decision['target_url']);self.assertTrue(rows[0]['needs_review'])
        self.assertEqual(self.sql('SELECT target_url FROM migration_mapping_decisions WHERE mapping_id=%s',[mid])[0]['target_url'],decision['target_url'])
        with self.assertRaises(Exception):self.proposal(store,f,state,1) # old lease fenced

    def test_free_http_plan_import_run_status_review_and_refine(self):
        from flask import Flask,request
        from contextlib import ExitStack
        from backend.routes.v2_routes import v2_blueprint
        from backend.routes.migration_mapping_routes import create_migration_mapping_blueprint
        from backend.services.migration_planning_service import MigrationPlanningService
        app=Flask(__name__);app.config.update(TESTING=True,RATELIMIT_ENABLED=False)
        app.register_blueprint(v2_blueprint,url_prefix='/api/v2')
        app.register_blueprint(create_migration_mapping_blueprint(lambda:MappingDecisionService(self.native)),url_prefix='/api/v2')
        def authorized():request.api_user_id=A;return A
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ,{'JEV_MVP_ENABLED':'true'}))
            stack.enter_context(patch('backend.routes.v2_routes.resolve_authorization',side_effect=authorized))
            stack.enter_context(patch('backend.routes.v2_routes.MigrationPlanningService',side_effect=lambda:MigrationPlanningService(self.repo)))
            stack.enter_context(patch('backend.routes.v2_routes.MigrationQuoteService',return_value=self.quotes))
            # Preserve real methods while injecting only the transport repository.
            service_type=JevService
            stack.enter_context(patch('backend.services.jev_pipeline_service.JevService',side_effect=lambda repository=None:service_type(repository or self.repo)))
            client=app.test_client()
            response=client.post('/api/v2/migrations',json={'old_site':'https://old.example','new_site':'https://new.example','idempotency_key':uuid4().hex})
            self.assertEqual(response.status_code,200,response.json);mid=response.json['migration_id']
            self.assertEqual(self.sql("SELECT count(*) n FROM migration_operations WHERE migration_id=%s AND kind='discover_inventory'",[mid])[0]['n'],0)
            ids={}
            for side in ('old','new'):
                response=client.post(f'/api/v2/migrations/{mid}/inventories',json={'side':side,'rows':[{'url':f'https://{side}.example/a','provenance':['csv']}],'idempotency_key':uuid4().hex})
                self.assertEqual(response.status_code,200,response.json);ids[side]=response.json['data']['inventory']['id']
            response=client.post(f'/api/v2/migrations/{mid}/runs',json={'inventory_ids':ids,'idempotency_key':uuid4().hex})
            self.assertEqual(response.status_code,200,response.json);rid=response.json['data']['run_id']
            self.assertTrue(response.json['data']['free']);self.assertNotIn('amount_cents',response.json['data'])
            job=self.claim();self.runs.authorize_dispatch(job,'worker-test');store=DurableStore(self.native,job,'worker-test');state=store.rpc('prepare_jev_pass')
            mapping=store.rpc('save_jev_proposal',p_pass=1,p_seed_revision=0,p_old_url='https://old.example/a',p_proposal={'target_url':'https://new.example/a','confidence':.9,'confirmation_required':True})
            self.runs.finalize_session(job,'worker-test','completed')
            response=client.get(f'/api/v2/migrations/{mid}/runs/{rid}/matches')
            self.assertEqual(response.status_code,200,response.json);self.assertFalse(response.json['data']['items'][0]['jev_proposal']['stale'])
            from backend.services.migration_artifact_service import MigrationArtifactService,PartialArtifactError
            with self.assertRaises(PartialArtifactError):
                MigrationArtifactService(self.repo).create_artifact(A,mid,rid,idempotency_key=uuid4().hex,fmt='nginx',selection_revision='0',partial_policy='deny')
            response=client.patch(f'/api/v2/migrations/{mid}/runs/{rid}/matches',json={'idempotency_key':uuid4().hex,'decisions':[{'mapping_id':mapping,'expected_revision':0,'action':'set_target','target_url':'https://new.example/a'}]})
            self.assertEqual(response.status_code,200,response.json)
            artifact=MigrationArtifactService(self.repo).create_artifact(A,mid,rid,idempotency_key=uuid4().hex,fmt='nginx',selection_revision='1',partial_policy='deny')
            self.assertEqual(artifact['included_count'],1)
            response=client.get(f'/api/v2/migrations/{mid}')
            self.assertEqual(response.status_code,200,response.json);self.assertEqual(response.json['data']['jev']['seed_revision'],1)
            response=client.post(f'/api/v2/migrations/{mid}/runs/{rid}/refine',json={'expected_seed_revision':1,'idempotency_key':uuid4().hex})
            self.assertEqual(response.status_code,200,response.json);self.assertEqual(response.json['next_action'],'poll')

    def test_initial_confirmations_are_audited_and_payload_bound(self):
        f=self.fixture();key=uuid4().hex
        pairs=[{'old_url':'https://old.example/Page/0?q=A','new_url':'https://new.example/Page/1?q=A'}]
        r=self.service.start(A,f['migration'],f['old'],f['new'],f['quote'],key,pairs)
        with self.assertRaises(Exception):self.service.start(A,f['migration'],f['old'],f['new'],f['quote'],key,[])
        job=self.claim();self.runs.authorize_dispatch(job,'worker-test');store=DurableStore(self.native,job,'worker-test')
        state=store.rpc('prepare_jev_pass')
        self.assertEqual(state['pass_seed_revision'],1)
        self.assertEqual(state['seeds'][0]['new_url'],pairs[0]['new_url'])
        self.assertEqual(self.sql('SELECT count(*) n FROM migration_mapping_decision_events WHERE run_id=%s',[r['run_id']])[0]['n'],1)
        store.rpc('prepare_jev_pass')
        self.assertEqual(self.sql('SELECT count(*) n FROM migration_mapping_decision_events WHERE run_id=%s',[r['run_id']])[0]['n'],1)

    def test_failed_same_pass_resume_never_reuses_old_attempt_authority(self):
        f,r,job,old_store,state=self.prepared()
        self.runs.finalize_session(job,'worker-test','permanently_failed','provider_unavailable')
        self.service.refine(A,f['migration'],r['run_id'],0,uuid4().hex)
        resumed=self.claim();self.runs.authorize_dispatch(resumed,'worker-test')
        self.assertGreater(resumed['attempt_count'],job['attempt_count'])
        current=DurableStore(self.native,resumed,'worker-test')
        self.assertEqual(current.rpc('prepare_jev_pass')['pass'],state['pass'])
        for write in (
            lambda:old_store.reserve(100),
            lambda:old_store.cache_put('c'*64,{'stale':True}),
            lambda:self.proposal(old_store,f,state),
        ):
            with self.assertRaises(Exception) as error:write()
            self.assertEqual(getattr(error.exception,'message',None),'operation_conflict')
        current.cache_put('d'*64,{'current':True})
        self.assertEqual(current.cache_get('d'*64),{'current':True})

    def test_parallel_budget_reservation_and_previous_day_settlement(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        first=self.prepared();second=self.prepared();barrier=Barrier(2)
        def reserve(item):
            barrier.wait()
            try:return item[3].rpc('reserve_jev_provider_call',p_daily_limit=10000,p_reservation_micro_usd=60000,p_budget_micro_usd=100000)
            except Exception:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(reserve,[first,second]))
        self.assertEqual(sum(bool(x) for x in results),1)
        index=0 if results[0] else 1;store=[first,second][index][3];reservation=results[index]
        # Simulated UTC boundary: yesterday's in-flight call must not refund today's budget.
        self.sql("UPDATE jev_provider_daily_budget SET day=day-1")
        self.sql("UPDATE jev_provider_reservations SET day=day-1 WHERE id=%s",[reservation])
        store.rpc('reserve_jev_provider_call',p_daily_limit=10000,p_reservation_micro_usd=700,p_budget_micro_usd=100000)
        store.cache_put('b'*64,{'ok':True},reservation,50)
        rows=self.sql('SELECT reserved_micro_usd FROM jev_provider_daily_budget ORDER BY day')
        self.assertEqual([row['reserved_micro_usd'] for row in rows],[50,700])

    def test_budget_settlement_unknown_charge_and_restart_cache(self):
        f,r,job,store,state=self.prepared()
        reservation=store.rpc('reserve_jev_provider_call',p_daily_limit=10000,p_reservation_micro_usd=90000,p_budget_micro_usd=100000)
        with self.assertRaises(Exception):store.rpc('reserve_jev_provider_call',p_daily_limit=10000,p_reservation_micro_usd=20000,p_budget_micro_usd=100000)
        store.cache_put('a'*64,{'answers':{}},reservation,100)
        self.assertEqual(self.sql('SELECT reserved_micro_usd FROM jev_provider_daily_budget')[0]['reserved_micro_usd'],100)
        self.assertEqual(DurableStore(self.native,job,'worker-test').cache_get('a'*64),{'answers':{}})
        store.cache_put('a'*64,{'answers':{}},reservation,100) # settlement replay no second refund
        self.assertEqual(self.sql('SELECT reserved_micro_usd FROM jev_provider_daily_budget')[0]['reserved_micro_usd'],100)
        store.rpc('reserve_jev_provider_call',p_daily_limit=10000,p_reservation_micro_usd=500,p_budget_micro_usd=1000000)
        self.assertEqual(self.sql('SELECT reserved_micro_usd FROM jev_provider_daily_budget')[0]['reserved_micro_usd'],600)

    def test_zero_seed_runner_persists_and_resumes_without_content(self):
        import numpy as np
        from types import SimpleNamespace
        class Embeddings:
            def __init__(self,store):pass
            def embed(self,texts):return np.tile(np.array([1.,0.],dtype=np.float32),(len(texts),1))
        class Provider:
            def __init__(self,store):self.store=store
            def ask(self,state,questions):
                assert set(state['old_page']) <= {'url','path'}
                target=next(iter(state['candidates']))
                answers={'target':{'probabilities':{key:1. if key==target else 0. for key in [*state['candidates'],'none']},'confidence':1.},'has_destination':{'noul':1.}}
                for key in state['candidates']:answers['relation_'+key]={'probabilities':{'3':1.},'score':3.,'confidence':1.}
                return {'answers':answers,'usage':{'input_tokens':10},'latency':.01,'cached':False}
        f=self.fixture();r=self.start(f);job=self.claim();self.runs.authorize_dispatch(job,'worker-test')
        runner=JevPipelineRunner(self.native,Provider,Embeddings)
        runner._run(job,'worker-test',lambda *args:None)
        self.assertEqual(self.sql('SELECT count(*) n FROM jev_proposals WHERE run_id=%s',[r['run_id']])[0]['n'],3)
        runner._run(job,'worker-test',lambda *args:None)
        rows=self.sql('SELECT * FROM url_mappings WHERE session_id=%s',[job['id']]);self.assertEqual(len(rows),3)
        self.assertTrue(all(row['needs_review'] for row in rows))
        self.assertEqual(self.sql('SELECT count(*) n FROM webpage_embeddings WHERE session_id=%s',[job['id']])[0]['n'],0)

class JevProviderValidation(unittest.TestCase):
    def test_unknown_usage_retains_reservation_and_no_cache_or_refund(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from src.redirx.jev.jev import JevClient,ProviderUnavailable,MODEL
        for usage in (None,-1,True):
            store=Mock();store.cache_get.return_value=None
            response=SimpleNamespace(model=MODEL,usage=SimpleNamespace(input_tokens=usage,output_tokens=0),answers={})
            provider=Mock();provider.system_one.return_value=response
            with self.assertRaises(ProviderUnavailable):JevClient(store,provider).ask({}, {})
            store.reserve.assert_called_once();store.cache_put.assert_not_called()
        store=Mock();store.cache_get.return_value=None;provider=Mock();provider.system_one.side_effect=TimeoutError('private URL')
        with self.assertRaises(ProviderUnavailable) as error:JevClient(store,provider).ask({}, {})
        self.assertNotIn('private URL',str(error.exception));store.cache_put.assert_not_called()

    def test_cached_response_must_match_requested_schema(self):
        from unittest.mock import Mock
        from src.redirx.jev.jev import JevClient,ProviderUnavailable,MODEL
        store=Mock();store.cache_get.return_value={'model':MODEL,'usage':{'input_tokens':5},'answers':{}}
        provider=Mock()
        with self.assertRaises(ProviderUnavailable):JevClient(store,provider).ask({}, {'target':{'type':'choice','criteria':{'x':'x'}}})
        provider.system_one.assert_not_called();store.reserve.assert_not_called()

class JevWorkerRouting(unittest.IsolatedAsyncioTestCase):
    async def test_marker_routes_before_content_hooks_and_pause_fails_closed(self):
        from contextlib import ExitStack
        from unittest.mock import Mock,AsyncMock
        from backend.worker import RedirxWorker
        for enabled,outcome in ((True,'succeeded'),(True,'failed'),(False,'failed')):
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ,{'JEV_MVP_ENABLED':str(enabled).lower()}))
                worker=RedirxWorker.__new__(RedirxWorker);worker.worker_id='w';worker.jobs_processed=0
                worker._lease_extension_loop=AsyncMock();worker.session_db=Mock()
                job={'id':str(uuid4()),'mcp_run_id':str(uuid4()),'attempt_count':1,'pipeline_type':'content'}
                authority=stack.enter_context(patch('backend.worker.MigrationRunService')).return_value
                authority.finalize_session.return_value={'status': outcome}
                stack.enter_context(patch('backend.worker.DeepPreviewService'))
                jev=stack.enter_context(patch('backend.services.jev_pipeline_service.JevService')).return_value
                jev.state.return_value={'pass':1}
                stack.enter_context(patch('backend.services.jev_database_transport.jev_worker_database'))
                runner=stack.enter_context(patch('backend.services.jev_runner.JevPipelineRunner')).return_value;runner.run=AsyncMock()
                pipeline=stack.enter_context(patch('backend.worker.Pipeline',side_effect=AssertionError('content must never run')))
                budget=stack.enter_context(patch('backend.worker.reserve_pivot_temporary_capacity'))
                self.assertEqual(await worker.process_job(job),enabled and outcome == 'succeeded')
                self.assertEqual(worker.jobs_processed,int(enabled and outcome == 'succeeded'))
                pipeline.assert_not_called();budget.assert_not_called()
                if enabled:
                    runner.run.assert_awaited_once()
                    progress = runner.run.call_args.args[2]
                    progress(UUID(job['id']), 2, 'Jev: propose URL mappings', 3)
                    worker.session_db.update_session_progress_for_attempt.assert_called_once_with(
                        UUID(job['id']), 2, 'Jev: propose URL mappings', 3, worker_id='w', attempt_count=1)
                    worker.session_db.update_session_progress.assert_not_called()
                else:runner.run.assert_not_awaited()
                self.assertEqual(authority.finalize_session.call_args.args[2],'completed' if enabled else 'permanently_failed')

class JevSdkOffline(unittest.TestCase):
    def test_real_sdk_transport_shape_and_no_hidden_retry(self):
        try:
            import httpx2
            from typesafe_sdk import TypeSafeClient,RetryPolicy
        except ImportError:self.skipTest('pinned provider SDK not installed in this test environment')
        from unittest.mock import Mock
        from src.redirx.jev.jev import JevClient,MODEL,ProviderUnavailable
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx2.Response(200,json={'model':MODEL,'answers':{'target':{'type':'choice','choice':'c01','confidence':.9,'probabilities':{'c01':1.,'none':0.}}},'usage':{'input_tokens':120,'output_tokens':0}})
        provider=TypeSafeClient(api_key='fixture-only',model=MODEL,retry=RetryPolicy(max_retries=0),transport=httpx2.MockTransport(handler))
        store=Mock();store.cache_get.return_value=None
        result=JevClient(store,provider).ask({'url':'https://example.test/a'},{'target':{'type':'choice','criteria':{'c01':'/a','none':'none'}}})
        self.assertEqual(len(calls),1);self.assertEqual(result['usage']['input_tokens'],120)
        self.assertEqual(store.cache_put.call_args.args[3],6)
        count=[]
        def limited(request):count.append(request);return httpx2.Response(429,headers={'retry-after':'60'},json={'detail':'rate limited'})
        provider=TypeSafeClient(api_key='fixture-only',model=MODEL,retry=RetryPolicy(max_retries=0),transport=httpx2.MockTransport(limited))
        store=Mock();store.cache_get.return_value=None
        with self.assertRaises(ProviderUnavailable):JevClient(store,provider).ask({}, {'target':{'type':'choice','criteria':{'x':'x'}}})
        self.assertEqual(len(count),1);store.cache_put.assert_not_called()


class JevFailureDiagnostics(unittest.TestCase):
    def test_cache_failure_reports_only_allowlisted_stage_and_type(self):
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from unittest.mock import Mock
        from src.redirx.jev.jev import JevClient, MODEL
        store=Mock();store.cache_get.return_value=None
        store.cache_put.side_effect=TypeError('private-token secret-url request-payload')
        provider=Mock();provider.system_one.return_value=SimpleNamespace(
            model=MODEL,usage=SimpleNamespace(input_tokens=1,output_tokens=0),answers={})
        output=io.StringIO()
        with redirect_stdout(output),self.assertRaises(TypeError):
            JevClient(store,provider).ask({}, {})
        line=output.getvalue().strip();record=json.loads(line.removeprefix('JEV_DIAGNOSTIC '))
        self.assertEqual(record,{'stage':'cache_publish','exception':'TypeError','status':None,'code':None})
        for secret in ('private-token','secret-url','request-payload'):self.assertNotIn(secret,line)
        self.assertEqual(provider.system_one.call_count,1);self.assertEqual(store.reserve.call_count,1)

    def test_unknown_diagnostic_fields_cannot_emit_provider_payload(self):
        from src.redirx.jev.jev import safe_failure_diagnostic
        evil=type('SecretExceptionName', (Exception,), {'status_code':'secret-status','status':'secret-status','code':'secret-code'})('secret-message')
        self.assertEqual(safe_failure_diagnostic(evil,'secret-stage'),
            {'stage':'unknown','exception':'OtherException','status':None,'code':None})
        known=RuntimeError('secret-message');known.status_code=429;known.code='PGRST202'
        self.assertEqual(safe_failure_diagnostic(known,'budget_reservation'),
            {'stage':'budget_reservation','exception':'RuntimeError','status':429,'code':'PGRST202'})
