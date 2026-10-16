"""Durable verification SQL + controlled local HTTP; no customer scans."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import aiohttp
from aiohttp import web

from backend.services.migration_verification_service import (MigrationVerificationService,
    VerificationEntitlementError, assess_probe, run_verification_batch)
from backend.services.migration_repository import MigrationRepository, MigrationNotFoundError, OperationConflictError
from backend.tests import test_migration_run_service as run_fixture
from backend.tests.test_migration_planning import A, B
from backend.tests.test_migration_planning import Query, DBClient
from src.redirx import redirect_probe as rp
from src.redirx.safe_fetch import validate_public_url, SSRFBlockedError, SafeResolver, create_safe_connector

ROOT = Path(__file__).resolve().parents[2]

class VerificationQuery(Query):
    def in_(self,key,values): self.states=key,values; return self
    def gt(self,key,value): self.after=key,value; return self
    def execute(self):
        if not self.rpc_name and not hasattr(self,'states'):
            return super().execute()
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        with psycopg.connect(self.client.dsn,row_factory=dict_row) as conn:
            conn.execute('SET ROLE service_role')
            if self.rpc_name:
                args=[psycopg.types.json.Jsonb(v) if isinstance(v,(dict,list)) else v for v in self.params.values()]
                stmt=sql.SQL('SELECT {}({}) AS result').format(sql.Identifier(self.rpc_name),sql.SQL(',').join(sql.Placeholder() for _ in args))
                try:data=conn.execute(stmt,args).fetchone()['result']
                except psycopg.Error as exc:
                    exc.code,exc.message=exc.sqlstate,exc.diag.message_primary
                    raise
            else:
                key,values=self.states
                stmt=sql.SQL('SELECT * FROM {} WHERE {}=ANY(%s)').format(sql.Identifier(self.table_name),sql.Identifier(key)); args=[values]
                for key,value in self.filters:
                    stmt+=sql.SQL(' AND {}=%s').format(sql.Identifier(key));args.append(value)
                key,value=self.after
                stmt+=sql.SQL(' AND {}>%s ORDER BY ordinal LIMIT %s').format(sql.Identifier(key));args.extend([value,self.max_rows])
                data=conn.execute(stmt,args).fetchall()
        return SimpleNamespace(data=data,error=None)

class VerificationClient(DBClient):
    def table(self,name):return VerificationQuery(self,table=name)
    def rpc(self,name,params):return VerificationQuery(self,rpc=name,params=params)



class ClassificationTests(unittest.TestCase):
    def result(self, final='https://new.example/a', status=200, hops=None, error=None):
        return rp.ProbeResult('https://old.example/a', hops if hops is not None else [rp.Hop('https://old.example/a',301,final)], final, status,error)

    def test_final_destination_identity_is_strict(self):
        for target in ('http://new.example/a','https://www.new.example/a','https://new.example/a/',
                       'https://new.example/a?x=1','https://new.example/A','https://new.example/a;param'):
            state,finding=assess_probe(self.result(final=target),'https://new.example/a')
            self.assertEqual((state,finding['issue']),('failed','wrong_target'))
        self.assertEqual(assess_probe(self.result(),'https://new.example/a')[0],'passed')

    def test_missing_unavailable_temporary_and_chains_are_never_passed(self):
        fixtures=[(self.result(status=404),'failed','not_found'),
                  (self.result(status=503),'unchecked','origin_unavailable'),
                  (self.result(status=403),'unchecked','origin_unavailable'),
                  (self.result(status=None,error='timeout'),'unchecked','unavailable'),
                  (self.result(status=None,error='blocked'),'unchecked','blocked'),
                  (self.result(status=None,error='loop'),'failed','redirect_loop'),
                  (self.result(hops=[]),'failed','no_redirect'),
                  (self.result(hops=[rp.Hop('https://old.example/a',302,'https://new.example/a')]),'failed','temporary_redirect'),
                  (self.result(hops=[rp.Hop('https://old.example/a',301,'https://new.example/b'),rp.Hop('https://new.example/b',301,'https://new.example/a')]),'failed','redirect_chain')]
        for result,state,issue in fixtures:
            measured,evidence=assess_probe(result,'https://new.example/a')
            self.assertEqual((measured,evidence['issue']),(state,issue))


class LocalHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.methods=[]
        async def handler(request):
            self.methods.append((request.path,request.method))
            if request.path=='/head' and request.method=='HEAD': return web.Response(status=405)
            if request.path in ('/one','/head'): return web.Response(status=301,headers={'Location':'/ok'})
            if request.path=='/chain': return web.Response(status=301,headers={'Location':'/one'})
            if request.path=='/temp': return web.Response(status=302,headers={'Location':'/ok'})
            if request.path=='/missing': return web.Response(status=404)
            if request.path=='/blocked': return web.Response(status=301,headers={'Location':'http://169.254.169.254/latest/meta-data'})
            if request.path=='/loop': return web.Response(status=301,headers={'Location':'/loop'})
            if request.path=='/timeout': await asyncio.sleep(.1)
            return web.Response(text='fixture')
        app=web.Application(); app.router.add_route('*','/{path}',handler)
        self.runner=web.AppRunner(app); await self.runner.setup()
        self.site=web.TCPSite(self.runner,'127.0.0.1',0); await self.site.start()
        self.origin='http://127.0.0.1:'+str(self.runner.addresses[0][1])
        def local_only_guard(url):
            if url.startswith(self.origin+'/'): return
            return validate_public_url(url)
        self.guard=patch.object(rp,'validate_public_url',side_effect=local_only_guard); self.guard.start()
        self.session=aiohttp.ClientSession()

    async def asyncTearDown(self):
        self.guard.stop(); await self.session.close(); await self.runner.cleanup()

    async def test_actual_head_get_chain_status_and_ssrf(self):
        for path,want in [('/one','passed'),('/head','passed'),('/chain','failed'),('/temp','failed'),('/missing','failed'),('/loop','failed'),('/blocked','unchecked')]:
            result=await rp.probe(self.session,self.origin+path)
            self.assertEqual(assess_probe(result,self.origin+'/ok')[0],want,path)
        self.assertIn(('/head','GET'),self.methods)
        self.assertFalse(any('meta-data' in path for path,_ in self.methods))

    async def test_production_connector_and_original_guard_remain_strict(self):
        self.guard.stop()
        connector=create_safe_connector()
        self.assertIsInstance(connector._resolver,SafeResolver)
        await connector.close()
        result=await rp.probe(self.session,self.origin+'/one')
        self.assertEqual(result.error,'blocked')
        self.assertEqual(self.methods,[])

    async def test_real_worker_persists_each_observation_and_timeout_is_unchecked(self):
        items=[{'ordinal':i,'attempt':1,'source_url':self.origin+path,'expected_url':self.origin+'/ok'} for i,path in enumerate(('/one','head','/blocked','/timeout'))]
        items[1]['source_url']=self.origin+'/head'
        recorded=[]
        service=SimpleNamespace(claim=lambda worker,size:{'verification_id':'fixture','items':items},
            record=lambda vid,item,worker,state,finding: recorded.append((item['ordinal'],state,finding)) or True)
        with patch('backend.services.migration_verification_service.create_safe_connector',side_effect=lambda **kw:aiohttp.TCPConnector(**kw)),patch('backend.services.migration_verification_service.PROBE_DEADLINE_SECONDS',.03):
            result=await run_verification_batch(service,'worker',4)
        self.assertEqual(result['recorded'],4)
        states={ordinal:state for ordinal,state,_ in recorded}
        self.assertEqual(states,{0:'passed',1:'passed',2:'unchecked',3:'unchecked'})


class BatchLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Real session/probe lifecycle; HTTP responses and persistence are local fakes."""

    def items(self, count=2):
        return [{'ordinal': i, 'attempt': 1, 'source_url': f'https://old.example/{i}',
                 'expected_url': f'https://new.example/{i}'} for i in range(count)]

    async def persistence_failure(self, monitoring=False):
        from backend.services.migration_monitoring_service import run_monitoring_batch
        loop = asyncio.get_running_loop()
        slow_started, release_slow, write_failed = (asyncio.Event() for _ in range(3))
        sessions, records = [], []

        async def request(session, method, url):
            sessions.append(session)
            if url == 'https://old.example/1':
                slow_started.set()
                await release_slow.wait()
            elif url == 'https://old.example/0':
                await slow_started.wait()
            if session.closed:
                raise RuntimeError('Session is closed')
            if url.startswith('https://old.example/'):
                return SimpleNamespace(status=301, headers={'Location': url.replace('old.', 'new.')}, release=lambda: None)
            return SimpleNamespace(status=200, headers={}, release=lambda: None)

        def record(vid, item, worker, state, finding):
            if item['ordinal'] == 0:
                loop.call_soon_threadsafe(write_failed.set)
                raise RuntimeError('private persistence error must not become a site finding')
            records.append((item['ordinal'], state, finding))
            return True

        service = SimpleNamespace(claim=lambda *_: {'verification_id': 'fixture', 'items': self.items()},
                                  record=record, schedule_due=lambda: 1)
        run = run_monitoring_batch if monitoring else run_verification_batch
        with patch.object(rp, '_request', side_effect=request), patch.object(rp, 'get_limiter', return_value=None):
            task = asyncio.create_task(run(service, 'worker', 2))
            try:
                await asyncio.wait_for(write_failed.wait(), 2)
                await asyncio.sleep(.02)
                self.assertFalse(task.done(), 'one failed write must not close the batch around an active probe')
                self.assertTrue(sessions)
                self.assertFalse(any(session.closed for session in sessions))
                release_slow.set()
                result = await asyncio.wait_for(task, 2)
                self.assertEqual((result['claimed'], result['recorded'], result['record_failed']), (2, 1, 1))
                self.assertTrue(result['retryable'])
                self.assertEqual([(i, state) for i, state, _ in records], [(1, 'passed')])
                self.assertEqual(records[0][2]['error'], None)
                self.assertTrue(all(session.closed for session in sessions))
                if monitoring:
                    self.assertEqual(result['scheduled'], 1)
            finally:
                release_slow.set()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.sleep(.02)

    async def test_failed_record_does_not_close_session_under_sibling_probe(self):
        await self.persistence_failure()

    async def test_monitoring_uses_the_same_failure_isolation(self):
        await self.persistence_failure(monitoring=True)

    async def test_cancellation_drains_probe_and_started_record_without_fabricated_finding(self):
        loop = asyncio.get_running_loop()
        write_started, probe_started, probe_finished = (asyncio.Event() for _ in range(3))
        release_write, write_finished = threading.Event(), threading.Event()
        sessions, recorded = [], []

        async def request(session, method, url):
            sessions.append(session)
            if url == 'https://old.example/1':
                probe_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.assertFalse(session.closed, 'probe must unwind before session closes')
                    probe_finished.set()
            elif url == 'https://old.example/0':
                await probe_started.wait()
            if url.startswith('https://old.example/'):
                return SimpleNamespace(status=301, headers={'Location': url.replace('old.', 'new.')}, release=lambda: None)
            return SimpleNamespace(status=200, headers={}, release=lambda: None)

        def record(vid, item, worker, state, finding):
            loop.call_soon_threadsafe(write_started.set)
            if not release_write.wait(3):
                raise AssertionError('test write was not released')
            recorded.append((item['ordinal'], state))
            write_finished.set()
            return True

        service = SimpleNamespace(claim=lambda *_: {'verification_id': 'fixture', 'items': self.items()}, record=record)
        with patch.object(rp, '_request', side_effect=request), patch.object(rp, 'get_limiter', return_value=None):
            task = asyncio.create_task(run_verification_batch(service, 'worker', 2))
            try:
                await asyncio.wait_for(write_started.wait(), 2)
                task.cancel()
                await asyncio.wait_for(probe_finished.wait(), 2)
                await asyncio.sleep(.02)
                self.assertFalse(task.done(), 'a started synchronous write must drain before returning cancellation')
                self.assertFalse(any(session.closed for session in sessions))
                task.cancel()  # repeated shutdown must not interrupt the drain
                await asyncio.sleep(.02)
                self.assertFalse(task.done())
                release_write.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                self.assertTrue(write_finished.is_set())
                self.assertEqual(recorded, [(0, 'passed')])
                self.assertTrue(all(session.closed for session in sessions))
            finally:
                release_write.set()
                await asyncio.gather(task, return_exceptions=True)


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'),'requires disposable local PostgreSQL')
class DatabaseTests(unittest.TestCase):
    cleanup_db=classmethod(run_fixture.RunDatabaseAcceptance.cleanup_db.__func__)
    sql=run_fixture.RunDatabaseAcceptance.sql
    fixture=run_fixture.RunDatabaseAcceptance.fixture
    start_run=run_fixture.RunDatabaseAcceptance.start
    pay=run_fixture.RunDatabaseAcceptance.pay
    claim_run=run_fixture.RunDatabaseAcceptance.claim

    @classmethod
    def setUpClass(cls):
        run_fixture.RunDatabaseAcceptance.setUpClass.__func__(cls)
        import psycopg
        artifact_path=Path(os.getenv('VERIFICATION_ARTIFACT_SQL',str(ROOT/'database/migrations/041_artifact_deployments.sql')))
        with psycopg.connect(cls.dsn,autocommit=True) as conn:
            conn.execute(artifact_path.read_text())
            conn.execute((ROOT/'database/migrations/043_included_verification.sql').read_text())
        cls.service=MigrationVerificationService(MigrationRepository(VerificationClient(cls.dsn)))

    def setUp(self):
        run_fixture.RunDatabaseAcceptance.setUp(self)
        # Test histories stay in place but unrelated queues cannot steal claims.
        self.sql("UPDATE migration_verifications SET status='partial' WHERE status IN ('queued','running')")

    def deployment(self,count=3,installed=True):
        import psycopg
        from psycopg.types.json import Jsonb
        f=self.fixture(count)
        if count>500: self.pay(f)
        # This capacity fixture measures verification only, never the engine.
        with patch('backend.services.migration_run_service.PIVOT_CONTENT_MAX_OLD_URLS',max(5000,count+1)):
            run=self.start_run(f)
            job=self.claim_run(); self.runs.authorize_dispatch(job,'worker-test')
        self.runs.finalize_session(job,'worker-test','completed')
        redirects=[{'mapping_id':str(uuid4()),'source_url':f'https://old.example/Page/{i}?q=A','expected_url':'https://new.example/Page/0?q=A'} for i in range(count)]
        payload={'redirects':redirects,'artifact_content_hash':'a'*64,'decision_revision':'1'}
        with psycopg.connect(self.dsn) as conn:
            artifact=conn.execute('''INSERT INTO migration_artifacts(user_id,migration_id,run_id,decision_revision,format,content_hash,storage_key,included_count,verification_inputs)
             VALUES(%s,%s,%s,'1','json',%s,'fixture',%s,%s) RETURNING id''',[A,f['migration'],run['run_id'],'a'*64,count,Jsonb(payload)]).fetchone()[0]
            dep=conn.execute('''INSERT INTO artifact_deployments(user_id,migration_id,artifact_id,live_origin,status,artifact_content_hash,decision_revision,format,included_count,excluded_count,target_origins,destination_mapping,verification_inputs,installation_reported_at)
             VALUES(%s,%s,%s,%s,%s,%s,'1','json',%s,0,'[]','{}',%s,CASE WHEN %s THEN now() ELSE NULL END) RETURNING id''',
             [A,f['migration'],artifact,'https://live-'+uuid4().hex+'.example','installation_reported' if installed else 'generated','a'*64,count,Jsonb(payload),installed]).fetchone()[0]
        return {'migration':f['migration'],'artifact':str(artifact),'deployment':str(dep),'grant':run['grant_id']}

    def begin(self,d,key=None):
        return self.service.start(A,d['migration'],d['artifact'],d['deployment'],key or uuid4().hex)

    def test_free_check_concurrent_reservation_is_one_allowance(self):
        d=self.deployment(); key=uuid4().hex
        with ThreadPoolExecutor(max_workers=6) as pool: results=list(pool.map(lambda _:self.begin(d,key),range(6)))
        self.assertEqual(len({r['data']['verification_id'] for r in results}),1)
        self.assertEqual(self.begin(d,'alias')['data']['verification_id'],results[0]['data']['verification_id'])
        other=self.deployment()
        with self.assertRaises(OperationConflictError):self.begin(other,'alias')
        self.assertEqual(self.sql('SELECT count(*) AS n FROM migration_verifications WHERE grant_id=%s',[d['grant']])[0]['n'],1)
        self.assertFalse(results[0]['data']['complete']); self.assertEqual(results[0]['data']['unchecked'],3)

    def test_owned_installation_and_changed_key_binding_required(self):
        d=self.deployment(installed=False)
        with self.assertRaises(VerificationEntitlementError) as e:self.begin(d)
        self.assertEqual(e.exception.code,'not_ready')
        with self.assertRaises(MigrationNotFoundError):self.service.start(B,d['migration'],d['artifact'],d['deployment'],'steal')
        self.sql("UPDATE artifact_deployments SET status='installation_reported',installation_reported_at=now() WHERE id=%s",[d['deployment']])
        self.begin(d,'bound')
        d2=self.deployment()
        with self.assertRaises(OperationConflictError):self.begin(d2,'bound')

    def test_interrupted_lease_reclaims_and_stale_worker_cannot_publish(self):
        d=self.deployment(); started=self.begin(d); vid=started['data']['verification_id']
        old=self.service.claim('old',2)
        self.assertEqual(len(old['items']),2)
        tail=self.service.claim('other',2)
        self.assertEqual(len(tail['items']),1)
        self.assertIsNone(self.service.claim('none',2))
        self.sql("UPDATE migration_verification_items SET lease_expires_at=now()-interval '1 second' WHERE verification_id=%s AND worker_id='old'",[vid])
        fresh=self.service.claim('fresh',2)
        self.assertEqual(fresh['items'][0]['attempt'],2)
        self.assertFalse(self.service.record(vid,old['items'][0],'old','passed',{}))
        for item in fresh['items']:self.assertTrue(self.service.record(vid,item,'fresh','passed',{'measurement':'observed'}))
        self.assertTrue(self.service.record(vid,tail['items'][0],'other','failed',{'issue':'wrong_target'}))
        status=self.service.status(A,d['migration'],vid)
        self.assertTrue(status['data']['complete']); self.assertEqual(status['data']['outcome'],'issues_found')
        self.assertEqual(status['data']['checked'],3)
        issues=self.service.issues(A,d['migration'],vid)
        self.assertEqual(len(issues['items']),1); self.assertEqual(issues['items'][0]['evidence']['issue'],'wrong_target')
        with self.assertRaises(MigrationNotFoundError):self.service.status(B,d['migration'],vid)

    def test_unavailable_is_partial_and_retry_reuses_allowance_and_checked_rows(self):
        d=self.deployment(); started=self.begin(d,'retry'); vid=started['data']['verification_id']
        batch=self.service.claim('worker',3)
        for index,item in enumerate(batch['items']):self.service.record(vid,item,'worker','unchecked' if index==2 else 'passed',{'issue':'timeout' if index==2 else None})
        status=self.service.status(A,d['migration'],vid)
        self.assertEqual(status['status'],'partial'); self.assertEqual(status['data']['unchecked'],1)
        self.assertEqual(status['data']['outcome'],'unverifiable')
        self.assertEqual(self.sql('SELECT status FROM artifact_deployments WHERE id=%s',[d['deployment']])[0]['status'],'installation_reported')
        replay=self.begin(d,'retry'); self.assertEqual(replay['data']['verification_id'],vid)
        retry=self.service.claim('retry',100); self.assertEqual(len(retry['items']),1)
        self.service.record(vid,retry['items'][0],'retry','passed',{})
        self.assertEqual(self.service.status(A,d['migration'],vid)['data']['outcome'],'passed')
        self.assertEqual(self.sql('SELECT status FROM artifact_deployments WHERE id=%s',[d['deployment']])[0]['status'],'live_verified')
        self.assertEqual(self.sql('SELECT count(*) AS n FROM migration_verifications WHERE grant_id=%s',[d['grant']])[0]['n'],1)

    def test_concurrent_claims_have_no_overlap_and_scope_is_immutable(self):
        import psycopg
        d=self.deployment(20); vid=self.begin(d)['data']['verification_id']
        with ThreadPoolExecutor(max_workers=8) as pool:
            batches=list(pool.map(lambda i:self.service.claim('concurrent-'+str(i),3),range(8)))
        ordinals=[item['ordinal'] for batch in batches if batch for item in batch['items']]
        while True:
            batch=self.service.claim('drain',3)
            if not batch:break
            ordinals.extend(item['ordinal'] for item in batch['items'])
        self.assertEqual(sorted(ordinals),list(range(20)))
        with psycopg.connect(self.dsn) as conn:
            with self.assertRaises(psycopg.errors.RaiseException):
                conn.execute("UPDATE migration_verification_items SET expected_url='https://attacker.example/' WHERE verification_id=%s",[vid])
        with psycopg.connect(self.dsn) as conn:
            with self.assertRaises(psycopg.errors.RaiseException):
                conn.execute('UPDATE migration_verifications SET total=total+1 WHERE id=%s',[vid])

    def test_revoked_grant_stops_unclaimed_work(self):
        d=self.deployment(); vid=self.begin(d)['data']['verification_id']
        self.sql("UPDATE migration_purchase_grants SET state='revoked' WHERE id=%s",[d['grant']])
        self.assertIsNone(self.service.claim('worker',10))
        status=self.service.status(A,d['migration'],vid)
        self.assertEqual(status['status'],'partial'); self.assertEqual(status['data']['checked'],0)
        with self.assertRaises(VerificationEntitlementError):self.begin(d)

    def test_paid_fifteen_thousand_scope_completes_in_bounded_batches(self):
        import psycopg
        from psycopg.types.json import Jsonb
        d=self.deployment(15000); started=self.begin(d); vid=started['data']['verification_id']
        seen=[]
        with psycopg.connect(self.dsn) as conn:
            while True:
                batch=self.service.claim('capacity',100)
                if not batch:break
                self.assertLessEqual(len(batch['items']),100)
                seen.extend(item['ordinal'] for item in batch['items'])
                rows=conn.execute('''SELECT complete_verification_item(%s,i.ordinal,'capacity',i.attempt,
                  CASE WHEN i.ordinal=14999 THEN 'failed' ELSE 'passed' END,
                  CASE WHEN i.ordinal=14999 THEN '{"issue":"wrong_target"}'::jsonb ELSE '{}'::jsonb END)
                  FROM jsonb_to_recordset(%s) AS i(ordinal integer,attempt integer)''',[vid,Jsonb(batch['items'])]).fetchall()
                self.assertTrue(all(row[0] for row in rows)); conn.commit()
        self.assertEqual(seen,list(range(15000)))
        result=self.service.status(A,d['migration'],vid)
        self.assertEqual((result['data']['checked'],result['data']['failed'],result['data']['unchecked']),(15000,1,0))
        self.assertTrue(result['data']['complete']); self.assertEqual(result['data']['outcome'],'issues_found')
        self.assertEqual(self.service.issues(A,d['migration'],vid)['items'][0]['issue_id'],vid+':14999')

    def test_account_deletion_cascades_verification_and_replay_keys(self):
        import psycopg
        d=self.deployment(); vid=self.begin(d)['data']['verification_id']; self.begin(d,'alias-cleanup')
        with psycopg.connect(self.dsn) as conn:
            conn.execute('DELETE FROM auth.users WHERE id=%s',[A])
            self.assertEqual(conn.execute('SELECT count(*) FROM migration_verifications WHERE id=%s',[vid]).fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT count(*) FROM migration_verification_items WHERE verification_id=%s',[vid]).fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT count(*) FROM migration_verification_request_keys WHERE user_id=%s',[A]).fetchone()[0],0)
            conn.rollback()

    def test_roles_cannot_reserve_or_read_others_work(self):
        import psycopg
        for role in ('anon','authenticated'):
            with psycopg.connect(self.dsn) as conn:
                conn.execute('SET ROLE '+role)
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):conn.execute('SELECT * FROM migration_verifications')
            with psycopg.connect(self.dsn) as conn:
                conn.execute('SET ROLE '+role)
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):conn.execute("SELECT claim_verification_batch('bad',1)")
