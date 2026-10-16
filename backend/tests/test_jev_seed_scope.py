"""Initial confirmation operation identity across distinct owned Jev runs."""
import os
import unittest
from uuid import uuid4

from backend.tests import test_jev_pipeline as jev_fixture
from backend.tests.test_migration_planning import A, B, ROOT
from backend.services.jev_store import DurableStore


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'requires disposable loopback PostgreSQL')
class InitialSeedScope(unittest.TestCase):
    setUpClass = classmethod(jev_fixture.NativeJev.setUpClass.__func__)
    cleanup_db = classmethod(jev_fixture.NativeJev.cleanup_db.__func__)
    def setUp(self):
        jev_fixture.NativeJev.setUp(self)
        original = (ROOT/'database/migrations/062_jev_url_harness.sql').read_text()
        original = original[original.index('CREATE FUNCTION prepare_jev_pass('):original.index('CREATE FUNCTION reserve_jev_provider_call(')]
        self.sql(original.replace('CREATE FUNCTION','CREATE OR REPLACE FUNCTION',1))
        self.owner = str(uuid4())
        self.sql('INSERT INTO auth.users(id) VALUES(%s)',[self.owner])
        self.sql('INSERT INTO user_profiles(id) VALUES(%s)',[self.owner])
    sql = jev_fixture.NativeJev.sql
    fixture = jev_fixture.NativeJev.fixture
    claim = jev_fixture.NativeJev.claim

    def apply_fix(self):
        self.sql((ROOT/'database/migrations/069_jev_initial_seed_operation_scope.sql').read_text())

    def operations(self, owner):
        return self.sql("SELECT to_jsonb(o) row,xmin::text version FROM migration_operations o WHERE user_id=%s AND kind='resolve_matches' ORDER BY id",[owner])

    def start_confirmed(self, f, pairs=None):
        pairs = pairs if pairs is not None else [{'old_url':'https://old.example/Page/0?q=A','new_url':'https://new.example/Page/1?q=A'}]
        run = self.service.start(f['user'],f['migration'],f['old'],f['new'],f['quote'],uuid4().hex,pairs)
        job = self.claim()
        self.runs.authorize_dispatch(job,'worker-test')
        return run, DurableStore(self.native,job,'worker-test')

    def exercise(self, *, same_migration):
        first = self.fixture(owner=self.owner)
        run1,store1 = self.start_confirmed(first)
        first_state = store1.rpc('prepare_jev_pass')
        self.assertEqual(first_state['pass_seed_revision'],1)
        second = first if same_migration else self.fixture(owner=self.owner)
        run2,store2 = self.start_confirmed(second)
        self.assertNotEqual(run1['run_id'],run2['run_id'])
        with self.assertRaises(Exception) as caught: store2.rpc('prepare_jev_pass')
        self.assertEqual(getattr(caught.exception,'message',None),'operation_conflict')
        self.assertEqual(self.sql('SELECT prepared,seed_revision FROM jev_runs WHERE run_id=%s',[run2['run_id']]),
                         [{'prepared':False,'seed_revision':0}])
        self.assertEqual(self.sql('SELECT count(*) n FROM migration_mapping_decision_events WHERE run_id=%s',[run2['run_id']])[0]['n'],0)
        self.assertEqual(self.sql('SELECT count(*) n FROM url_mappings WHERE session_id=%s',[run2['session_id']])[0]['n'],0)
        self.assertEqual(store1.rpc('prepare_jev_pass'),first_state)
        if not same_migration:
            failed_job={'id':run2['session_id'],'mcp_run_id':run2['run_id'],'attempt_count':store2.context['p_attempt_count']}
            self.assertEqual(self.runs.finalize_session(failed_job,'worker-test','permanently_failed','jev_run_interrupted')['status'],'failed')
        old_operations = self.operations(self.owner)
        old_events = self.sql('SELECT * FROM migration_mapping_decision_events WHERE run_id=%s ORDER BY id',[run1['run_id']])
        self.apply_fix()
        self.assertEqual(self.operations(self.owner),old_operations)
        self.assertEqual(store1.rpc('prepare_jev_pass'),first_state)
        self.assertEqual(self.sql('SELECT * FROM migration_mapping_decision_events WHERE run_id=%s ORDER BY id',[run1['run_id']]),old_events)
        self.assertEqual(self.operations(self.owner),old_operations)
        if not same_migration:
            self.service.refine(self.owner,second['migration'],run2['run_id'],0,uuid4().hex)
            replacement=self.claim();self.runs.authorize_dispatch(replacement,'worker-test')
            with self.assertRaises(Exception):store2.rpc('prepare_jev_pass')
            store2=DurableStore(self.native,replacement,'worker-test')
        state2 = store2.rpc('prepare_jev_pass')
        self.assertEqual(state2['pass_seed_revision'],1)
        self.assertEqual(state2['seeds'][0]['new_url'],'https://new.example/Page/1?q=A')
        self.assertEqual(self.sql('SELECT count(*) n FROM migration_mapping_decision_events WHERE run_id=%s',[run2['run_id']])[0]['n'],1)
        after_operations = self.operations(self.owner)
        self.assertEqual(len(after_operations),len(old_operations)+(1 if same_migration else 2))
        self.assertTrue(any(op['row']['idempotency_key'].startswith('jev-initial:'+run2['run_id']+':') for op in after_operations))
        self.assertEqual(store2.rpc('prepare_jev_pass'),state2)
        self.apply_fix()  # Replacing the already-correct function is row-neutral too.
        self.assertEqual(store2.rpc('prepare_jev_pass'),state2)
        self.assertEqual(self.operations(self.owner),after_operations)
        print('INITIAL_SEED_COLLISION', {'same_migration':same_migration,'second_run':'operation_conflict',
            'first_run_replay':'unchanged','before_prepared':False,'before_events':0,'before_mappings':0,'after_prepared':True,'after_events':1})

    def test_cross_migration_collision_is_repaired_without_rewriting_prepared_history(self): self.exercise(same_migration=False)
    def test_cross_run_collision_is_repaired_and_new_key_replays(self): self.exercise(same_migration=True)

    def test_function_identity_guards_and_effective_grants_are_preserved(self):
        before=self.sql("""SELECT p.oid,p.proowner,p.proacl,p.prosecdef,p.proconfig,p.proargtypes::text,
            p.prorettype,p.provolatile,p.prosrc FROM pg_proc p
            WHERE p.oid='public.prepare_jev_pass(uuid,uuid,text,integer)'::regprocedure""")[0]
        self.sql('CREATE SCHEMA fixture_migration_search_path')
        try:
            self.sql('SET search_path=fixture_migration_search_path,public;'+
                     (ROOT/'database/migrations/069_jev_initial_seed_operation_scope.sql').read_text())
            self.assertEqual(self.sql("SELECT count(*) n FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='fixture_migration_search_path' AND p.proname='prepare_jev_pass'")[0]['n'],0)
        finally:
            self.sql('DROP SCHEMA fixture_migration_search_path')
        after=self.sql("""SELECT p.oid,p.proowner,p.proacl,p.prosecdef,p.proconfig,p.proargtypes::text,
            p.prorettype,p.provolatile,p.prosrc FROM pg_proc p
            WHERE p.oid='public.prepare_jev_pass(uuid,uuid,text,integer)'::regprocedure""")[0]
        old="'jev-initial:'||encode(sha256(convert_to(pair::text,'UTF8')),'hex')"
        new="'jev-initial:'||p_run_id::text||':'||encode(sha256(convert_to(pair::text,'UTF8')),'hex')"
        self.assertEqual(after.pop('prosrc'),before.pop('prosrc').replace(old,new))
        self.assertEqual(after,before)
        for role,expected in [('anon',False),('authenticated',False),('service_role',True)]:
            self.assertEqual(self.sql("SELECT has_function_privilege(%s,'public.prepare_jev_pass(uuid,uuid,text,integer)','EXECUTE') allowed",[role])[0]['allowed'],expected)
        self.assertEqual(self.sql("""SELECT count(*) n FROM pg_proc p,aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
            WHERE p.oid='public.prepare_jev_pass(uuid,uuid,text,integer)'::regprocedure AND a.grantee=0 AND a.privilege_type='EXECUTE'""")[0]['n'],0)

    def test_raw_pair_aliases_duplicate_rejection_and_other_owner(self):
        from backend.services.migration_planning_service import MigrationPlanningService
        from backend.services.inventory_import_service import InventoryImportService
        self.apply_fix()
        pairs=[{'old_url':'https://old.example/Case%2FPath?x=A%20B','new_url':'https://new.example/Case%2FPath?x=A%20B'},
               {'old_url':'https://old.example/Case/Path?x=A+B','new_url':'https://new.example/Case/Path?x=A+B'}]
        for owner in (self.owner,B):
            plan=MigrationPlanningService(self.repo).plan(owner,{'old_site':'https://old.example','new_site':'https://new.example','idempotency_key':uuid4().hex})
            f={'user':owner,'migration':plan['migration_id']}
            for side in ('old','new'):
                f[side]=InventoryImportService(self.repo).import_inventory(owner,f['migration'],side,
                    [{'url':pair[side+'_url'],'provenance':['csv']} for pair in pairs],uuid4().hex)['inventory_id']
            f['quote']=self.quotes.create_quote(owner,f['migration'],f['old'],f['new'],uuid4().hex)['quote_id']
            with self.assertRaises(Exception):
                self.service.start(owner,f['migration'],f['old'],f['new'],f['quote'],uuid4().hex,[pairs[0],pairs[0]])
            with self.assertRaises(Exception):
                self.service.start(A,f['migration'],f['old'],f['new'],f['quote'],uuid4().hex,pairs)
            run,store=self.start_confirmed(f,pairs)
            state=store.rpc('prepare_jev_pass')
            self.assertEqual(state['pass_seed_revision'],2)
            self.assertEqual(sorted((p['old_url'],p['new_url']) for p in state['seeds']),sorted((p['old_url'],p['new_url']) for p in pairs))
            operations=self.operations(owner)
            keys=[op['row']['idempotency_key'] for op in operations]
            self.assertEqual(len(keys),2);self.assertEqual(len(set(keys)),2)
            self.assertTrue(all(key.startswith('jev-initial:'+run['run_id']+':') and len(key)<=200 for key in keys))
            self.assertEqual(store.rpc('prepare_jev_pass'),state)

    def test_failed_multi_seed_prepare_rolls_back_all_rows_then_retries(self):
        self.apply_fix()
        f=self.fixture(owner=self.owner)
        pairs=[{'old_url':f'https://old.example/Page/{i}?q=A','new_url':f'https://new.example/Page/{i}?q=A'} for i in (0,1)]
        run,store=self.start_confirmed(f,pairs)
        self.sql("""CREATE FUNCTION fixture_fail_second_seed() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF EXISTS(SELECT 1 FROM url_mappings WHERE id=NEW.mapping_id AND old_url='https://old.example/Page/1?q=A')
            THEN RAISE EXCEPTION 'fixture second seed failure'; END IF; RETURN NEW; END $$;
            CREATE TRIGGER fixture_fail_second_seed BEFORE INSERT ON migration_mapping_decision_events
            FOR EACH ROW EXECUTE FUNCTION fixture_fail_second_seed()""")
        try:
            with self.assertRaises(Exception) as caught:store.rpc('prepare_jev_pass')
            self.assertIn('fixture second seed failure',str(caught.exception))
            self.assertEqual(self.operations(self.owner),[])
            for table,column in [('url_mappings','session_id'),('migration_mapping_decisions','run_id'),('migration_mapping_decision_events','run_id')]:
                self.assertEqual(self.sql(f'SELECT count(*) n FROM {table} WHERE {column}=%s',[run['session_id'] if column=='session_id' else run['run_id']])[0]['n'],0)
            self.assertEqual(self.sql('SELECT prepared,seed_revision FROM jev_runs WHERE run_id=%s',[run['run_id']]),[{'prepared':False,'seed_revision':0}])
        finally:
            self.sql('DROP TRIGGER fixture_fail_second_seed ON migration_mapping_decision_events; DROP FUNCTION fixture_fail_second_seed()')
        state=store.rpc('prepare_jev_pass')
        self.assertEqual(state['pass_seed_revision'],2)
        self.assertEqual(len(self.operations(self.owner)),2)
        self.assertEqual(store.rpc('prepare_jev_pass'),state)

    def test_attempt_reclaim_and_refinement_preserve_seed_authority(self):
        self.apply_fix()
        f=self.fixture(owner=self.owner);run,store=self.start_confirmed(f)
        bad_context={**store.context,'p_worker_id':'wrong-worker'}
        with self.assertRaises(Exception):self.native.rpc('prepare_jev_pass',bad_context).execute()
        state=store.rpc('prepare_jev_pass')
        operations=self.operations(self.owner)
        # Actual lease reclaim invalidates old attempt even though prepared=true.
        self.sql("UPDATE migration_sessions SET lease_expires_at=now()-interval '1 minute' WHERE id=%s",[run['session_id']])
        self.sql('SELECT * FROM reclaim_expired_leases(5)')
        job=self.claim();self.runs.authorize_dispatch(job,'worker-test')
        with self.assertRaises(Exception):store.rpc('prepare_jev_pass')
        current=DurableStore(self.native,job,'worker-test')
        self.assertEqual(current.rpc('prepare_jev_pass'),state)
        self.assertEqual(self.operations(self.owner),operations)
        self.runs.finalize_session(job,'worker-test','completed')
        from backend.services.mapping_decision_service import MappingDecisionService
        mapping=self.sql('SELECT id FROM url_mappings WHERE session_id=%s',[run['session_id']])[0]['id']
        decisions=MappingDecisionService(self.native)
        stale=decisions.resolve_matches(self.owner,f['migration'],run['run_id'],
            [{'mapping_id':str(mapping),'expected_revision':0,'action':'set_target','target_url':'https://new.example/Page/0?q=A'}],uuid4().hex)
        self.assertEqual(stale['outcomes'][0]['code'],'revision_conflict')
        changed=decisions.resolve_matches(self.owner,f['migration'],run['run_id'],
            [{'mapping_id':str(mapping),'expected_revision':1,'action':'set_target','target_url':'https://new.example/Page/0?q=A'}],uuid4().hex)
        self.assertEqual(changed['outcomes'][0]['code'],'ok')
        self.service.refine(self.owner,f['migration'],run['run_id'],2,uuid4().hex)
        refined_job=self.claim();self.runs.authorize_dispatch(refined_job,'worker-test')
        refined=DurableStore(self.native,refined_job,'worker-test').rpc('prepare_jev_pass')
        self.assertEqual(refined['pass'],2);self.assertEqual(refined['pass_seed_revision'],2)
        self.assertEqual(refined['seeds'][0]['new_url'],'https://new.example/Page/0?q=A')
        self.assertEqual(self.sql('SELECT count(*) n FROM migration_mapping_decision_events WHERE run_id=%s',[run['run_id']])[0]['n'],2)

    def test_lost_prepare_response_replays_without_duplicate_decisions(self):
        from types import SimpleNamespace
        self.apply_fix()
        f=self.fixture(owner=self.owner);run,store=self.start_confirmed(f)
        original=self.native.rpc('prepare_jev_pass',store.context)
        def lost():
            original.execute()
            raise TimeoutError('fixture response lost after commit')
        with self.assertRaises(TimeoutError):SimpleNamespace(execute=lost).execute()
        operations=self.operations(self.owner)
        state=store.rpc('prepare_jev_pass')
        self.assertEqual(state['pass_seed_revision'],1)
        self.assertEqual(self.operations(self.owner),operations)
        self.assertEqual(self.sql('SELECT count(*) n FROM migration_mapping_decision_events WHERE run_id=%s',[run['run_id']])[0]['n'],1)
