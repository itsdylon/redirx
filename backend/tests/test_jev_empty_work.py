"""Native no-work and partial-work admission; synthetic providers only."""
import json
import os
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from backend.services.jev_runner import DurableEmbeddingCache, JevPipelineRunner
from backend.services.jev_store import DurableStore
from backend.services.mapping_decision_service import MappingDecisionService
from backend.tests import test_jev_pipeline as fixture
from backend.tests.test_jev_capacity import FakeTypeSafe
from backend.tests.test_jev_runner_lifecycle import EmbeddingSDK
from src.redirx.jev.jev import JevClient, ProviderUnavailable


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'disposable PostgreSQL required')
class EmptyWorkNative(unittest.TestCase):
    setUpClass = classmethod(fixture.NativeJev.setUpClass.__func__)
    cleanup_db = classmethod(fixture.NativeJev.cleanup_db.__func__)
    setUp = fixture.NativeJev.setUp
    sql = fixture.NativeJev.sql
    claim = fixture.NativeJev.claim
    fixture = fixture.NativeJev.fixture

    def start_job(self, count=2, confirmed=()):
        data = self.fixture(count=count)
        run = self.service.start(data['user'], data['migration'], data['old'], data['new'],
            data['quote'], uuid4().hex, list(confirmed))
        job = self.claim(); self.runs.authorize_dispatch(job, 'worker-test')
        return data, run, job

    def run_fixture(self, job, *, forbid=False):
        sdk = EmbeddingSDK(); judge = FakeTypeSafe(); calls = []; stages = []
        def embeddings(store):
            calls.append('embedding_factory')
            if forbid: raise AssertionError('No embedding client needed for zero pending work')
            return DurableEmbeddingCache(store, sdk)
        def provider(store):
            calls.append('provider_factory')
            if forbid: raise AssertionError('No judgment client needed for zero pending work')
            return JevClient(store, judge)
        with patch('src.redirx.jev.jev.time.sleep', lambda _: None):
            JevPipelineRunner(self.native, provider, embeddings)._run(job, 'worker-test',
                lambda _, phase, label, total: stages.append((phase, label, total)))
        return calls, stages, sdk

    def counts(self, run_id):
        return {table: self.sql(f'SELECT count(*) n FROM {table} WHERE run_id=%s', [run_id])[0]['n']
            for table in ('jev_proposals', 'jev_provider_cache', 'jev_provider_reservations', 'migration_mapping_decision_events')}

    def assert_stages(self, stages):
        self.assertEqual([row[0] for row in stages], [1, 2, 3])
        self.assertTrue(all(row[2] == 3 for row in stages))
        self.assertEqual(stages[-1][1], 'Jev: ready for explicit review')

    def test_all_confirmed_initial_run_prepares_audit_without_provider_cost(self):
        old = ['https://old.example/Page/0?q=A', 'https://old.example/Page/1?q=A',
               'https://old.example/Page/0?q=A#part']
        pairs = [{'old_url': url, 'new_url': 'https://new.example/Page/0?q=A'} for url in old]
        _, run, job = self.start_job(confirmed=pairs)
        calls, stages, sdk = self.run_fixture(job)
        state = self.sql('SELECT prepared,pass,seed_revision,pass_seed_revision,seeds FROM jev_runs WHERE run_id=%s', [run['run_id']])[0]
        self.assertTrue(state['prepared']); self.assertEqual(state['seed_revision'], 3)
        self.assertEqual(state['pass_seed_revision'], 3); self.assertEqual(len(state['seeds']), 3)
        counts = self.counts(run['run_id'])
        self.assertEqual(counts['migration_mapping_decision_events'], 3)
        self.assertEqual(counts['jev_proposals'], 0)
        self.assert_stages(stages)
        print('JEV_EMPTY_WORK '+json.dumps({'case': 'all_confirmed', 'factory_calls': calls,
            'embedding_requests': len(sdk.calls), **counts}), flush=True)
        self.assertEqual(counts['jev_provider_reservations'], 0)
        self.assertEqual(counts['jev_provider_cache'], 0)
        self.assertEqual(calls, []); self.assertEqual(sdk.calls, [])
        # The branch also works without any configured provider constructors.
        self.assert_stages(self.run_fixture(job, forbid=True)[1])
        self.assertEqual(self.counts(run['run_id']), counts)

    def test_same_pass_fully_proposed_replay_skips_clients_and_preserves_review(self):
        _, run, job = self.start_job()
        self.run_fixture(job)
        before = self.counts(run['run_id'])
        self.assertEqual(before['jev_proposals'], len(job['old_urls']))
        self.assertEqual(before['migration_mapping_decision_events'], 0)
        calls, stages, sdk = self.run_fixture(job)
        self.assertEqual(self.counts(run['run_id']), before)
        self.assert_stages(stages)
        print('JEV_EMPTY_WORK '+json.dumps({'case': 'fully_proposed_replay',
            'factory_calls': calls, 'embedding_requests': len(sdk.calls), **before}), flush=True)
        self.assertEqual(calls, []); self.assertEqual(sdk.calls, [])
        self.assert_stages(self.run_fixture(job, forbid=True)[1])
        self.assertTrue(all(row['needs_review'] for row in self.sql('SELECT needs_review FROM url_mappings WHERE session_id=%s', [job['id']])))

    def test_partial_pending_work_and_new_pass_are_not_skipped(self):
        # Distinct from all-confirmed fixture: SQL062's pair-only initial
        # decision key currently collides across migrations for the same owner.
        pair = {'old_url': 'https://old.example/Page/0?q=A', 'new_url': 'https://new.example/Page/1?q=A'}
        data, run, job = self.start_job(count=3, confirmed=[pair])
        store = DurableStore(self.native, job, 'worker-test')
        state = store.rpc('prepare_jev_pass')
        store.rpc('save_jev_proposal', p_pass=1, p_seed_revision=state['pass_seed_revision'],
            p_old_url='https://old.example/Page/1?q=A',
            p_proposal={'target_url': 'https://new.example/Page/1?q=A', 'confidence': .9, 'confirmation_required': True})
        calls, stages, sdk = self.run_fixture(job)
        self.assertEqual(calls, ['embedding_factory', 'provider_factory']); self.assertTrue(sdk.calls)
        self.assert_stages(stages)
        self.assertEqual(self.counts(run['run_id'])['jev_proposals'], len(job['old_urls'])-1)
        self.assertEqual(self.counts(run['run_id'])['migration_mapping_decision_events'], 1)
        self.runs.finalize_session(job, 'worker-test', 'completed')
        mapping = self.sql('SELECT id FROM url_mappings WHERE session_id=%s AND old_url=%s',
            [job['id'], 'https://old.example/Page/1?q=A'])[0]['id']
        result = MappingDecisionService(self.native).resolve_matches(data['user'], data['migration'], run['run_id'],
            [{'mapping_id': str(mapping), 'expected_revision': 0, 'action': 'set_target',
              'target_url': 'https://new.example/Page/0?q=A'}], uuid4().hex)
        self.assertEqual(result['outcomes'][0]['code'], 'ok')
        revision = self.service.state(run['run_id'])['seed_revision']
        self.service.refine(data['user'], data['migration'], run['run_id'], revision, uuid4().hex)
        refined = self.claim(); self.runs.authorize_dispatch(refined, 'worker-test')
        calls, stages, _ = self.run_fixture(refined)
        self.assertEqual(calls, ['embedding_factory', 'provider_factory']); self.assert_stages(stages)
        current = self.sql('SELECT pass,pass_seed_revision FROM jev_runs WHERE run_id=%s', [run['run_id']])[0]
        self.assertEqual(current['pass'], 2); self.assertEqual(current['pass_seed_revision'], revision)
        self.assertEqual(self.sql('SELECT count(*) n FROM jev_proposals WHERE run_id=%s AND pass=2', [run['run_id']])[0]['n'], len(job['old_urls'])-2)

    def test_no_work_does_not_bypass_stale_attempt_authority(self):
        data, run, job = self.start_job()
        self.run_fixture(job)
        self.runs.finalize_session(job, 'worker-test', 'permanently_failed', 'fixture_interruption')
        self.service.refine(data['user'], data['migration'], run['run_id'], 0, uuid4().hex)
        resumed = self.claim(); self.runs.authorize_dispatch(resumed, 'worker-test')
        self.assertGreater(resumed['attempt_count'], job['attempt_count'])
        with self.assertRaises(Exception) as error: self.run_fixture(job, forbid=True)
        self.assertEqual(getattr(error.exception, 'message', ''), 'operation_conflict')
        self.assert_stages(self.run_fixture(resumed, forbid=True)[1])


class EmptyWorkAdmission(unittest.TestCase):
    def test_capacity_and_engine_version_checks_still_precede_empty_branch(self):
        job = dict(id=str(uuid4()), mcp_run_id=str(uuid4()), attempt_count=1, old_urls=[], new_urls=['https://new.example/a'])
        embedding_factory = Mock(); provider_factory = Mock(); client = Mock(); progress = Mock()
        client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
        runner = JevPipelineRunner(client, provider_factory, embedding_factory)
        with self.assertRaises(ValueError): runner._run(job, 'fixture', progress)
        client.rpc.assert_not_called()
        job['old_urls'] = ['https://old.example/a']
        client.rpc.return_value.execute.return_value.data = {'model': 'wrong-version', 'prompt_version': 'wrong'}
        with self.assertRaisesRegex(ProviderUnavailable, 'engine_version_unavailable'):
            runner._run(job, 'fixture', progress)
        self.assertTrue(all(call.args == ('jev_runs',) for call in client.table.call_args_list)); progress.assert_not_called()
        embedding_factory.assert_not_called(); provider_factory.assert_not_called()
