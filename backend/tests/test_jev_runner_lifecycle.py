"""Offline runner boundary integration; native accounting requires disposable PG."""
import math
import os
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
from uuid import uuid4

import numpy as np
from backend.services.jev_runner import DurableEmbeddingCache, JevPipelineRunner
from backend.tests import test_jev_pipeline as jev_fixture
from backend.tests.test_jev_embedding_memory import Store, Client, key, normalized, raw_response
from backend.tests.test_jev_capacity import FakeTypeSafe
from src.redirx.jev.jev import JevClient, ProviderUnavailable


class RunnerMemoryIntegration(unittest.TestCase):
    def test_real_runner_json_restoration_order_preserves_complete_outputs(self):
        import json
        from scripts.research.numeric_threads import study
        from src.redirx.jev.data import Page
        from src.redirx.jev.retrieve import path_embed_text
        job = dict(name='restore-order', id=str(uuid4()), mcp_run_id=str(uuid4()), attempt_count=1,
            old_urls=[f'https://old.example/Case/{i}?Q=A%2FB' for i in range(12)],
            new_urls=[f'https://new.example/Docs/{i}?Q=A%2FB' for i in range(8)], seeds=[])
        job['seeds'] = [{'old_url': job['old_urls'][0], 'new_url': job['new_urls'][0]}]
        vectors = {key(path_embed_text(Page('', url))): np.array(
            [math.sin(i+j*.17) for j in range(1536)], dtype=np.float32)
            for i, url in enumerate(job['new_urls']+job['old_urls'])}
        class RestoredClient(study.Client):
            def table(self, name):
                query = super().table(name)
                execute = query.execute
                def restored():
                    result = execute()
                    if name == 'jev_provider_cache':
                        result.data = json.loads(json.dumps(list(reversed(result.data)), allow_nan=False))
                    return result
                query.execute = restored
                return query
        cold = study.execute([job], vectors, validation=True)
        with patch.object(study, 'Client', RestoredClient):
            restored = study.execute([job], vectors, validation=True)
        self.assertEqual(cold['outputs'], restored['outputs'])
        self.assertEqual(cold['output_sha256'], restored['output_sha256'])
        self.assertTrue(cold['cache_released'] and restored['cache_released'])
        self.assertEqual(restored['jobs'][0]['proposals'], 11)
        self.assertEqual(restored['jobs'][0]['stub_calls'], 22)

    def test_mixed_ordinary_and_extreme_rows_keep_ordinary_bits(self):
        ordinary = [math.sin(i*.13) for i in range(1536)]
        small, large = [1e-30]*1536, [1e30]*1536
        cache = DurableEmbeddingCache(Store(), Client(dict(a=ordinary, b=small, c=large)))
        result = cache.embed(['a', 'b', 'c', 'a'])
        np.testing.assert_array_equal(result[[0, 3]], normalized([ordinary, ordinary]))
        np.testing.assert_allclose(np.linalg.norm(result.astype(np.float64), axis=1), 1, rtol=1e-6)
        np.testing.assert_array_equal(cache.embed(['c', 'a', 'b']), result[[2, 0, 1]])

    def test_archived_adversarial_fixture_is_intentionally_rejected_unchanged(self):
        from scripts.research.retrieval_determinism.audit import adversarial, NoProvider
        _, vectors = adversarial()
        zero_keys = [key for key, value in vectors.items() if not np.any(value)]
        self.assertTrue(zero_keys)
        # Exercise the actual archived retrieval workload without replacing its
        # zero vectors or pretending its earlier admission policy was unchanged.
        from scripts.research.retrieval_determinism.audit import evaluate
        workload, _ = adversarial()
        with self.assertRaisesRegex(ProviderUnavailable, 'embedding_cache_invalid'):
            evaluate(workload, vectors, 'current', 6, 'restored')
        near = np.zeros(1536, dtype=np.float32); near[0] = 1
        near[1] = np.nextafter(np.float32(0), np.float32(1))
        cache = DurableEmbeddingCache(Store({key('near'): near.tolist()}), NoProvider())
        np.testing.assert_array_equal(cache.embed(['near']), normalized([near]))


class EmbeddingSDK:
    def __init__(self, mode='valid'):
        self.mode = mode; self.calls = []; self.embeddings = self.with_raw_response = self
    def create(self, model, input):
        self.calls.append(list(input))
        vectors = [[math.sin(i+len(text)) for i in range(1536)] for text in input]
        if self.mode == 'malformed': vectors[-1][0] = True
        tokens = 100000 if self.mode == 'accounting' else 1
        return raw_response(NS(model=model, data=[NS(index=i, embedding=vectors[i])
            for i in reversed(range(len(input)))], usage=NS(prompt_tokens=tokens, total_tokens=tokens)))


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'disposable PostgreSQL required')
class RunnerNativeLifecycle(unittest.TestCase):
    # Reuse only setup/helpers, not the original class's entire test suite.
    setUpClass = classmethod(jev_fixture.NativeJev.setUpClass.__func__)
    cleanup_db = classmethod(jev_fixture.NativeJev.cleanup_db.__func__)
    setUp = jev_fixture.NativeJev.setUp
    sql = jev_fixture.NativeJev.sql
    fixture = jev_fixture.NativeJev.fixture
    claim = jev_fixture.NativeJev.claim
    start = jev_fixture.NativeJev.start

    def job(self, count=2):
        data = self.fixture(count=count); run = self.start(data); job = self.claim()
        self.runs.authorize_dispatch(job, 'worker-test')
        return data, run, job

    def run_fixture(self, job, sdk, client=None, judge=None):
        stages = []
        runner = JevPipelineRunner(client or self.native,
            lambda store: JevClient(store, judge or FakeTypeSafe()),
            lambda store: DurableEmbeddingCache(store, sdk))
        with patch('src.redirx.jev.jev.time.sleep', lambda _: None):
            runner._run(job, 'worker-test', lambda _, phase, *args: stages.append(phase))
        return stages

    def test_cold_provider_concurrent_rows_and_restored_run_have_exact_proposals(self):
        _, run, job = self.job(count=12)
        barrier = threading.Barrier(6)
        class ConcurrentJudge(FakeTypeSafe):
            def __init__(self): self.local = threading.local(); self.threads = set(); self.calls = 0; self.lock = threading.Lock()
            def system_one(self, *args, **kwargs):
                with self.lock: self.threads.add(threading.get_ident()); self.calls += 1
                if not getattr(self.local, 'started', False):
                    self.local.started = True; barrier.wait(timeout=10)
                return super().system_one(*args, **kwargs)
        sdk = EmbeddingSDK(); judge = ConcurrentJudge()
        self.assertEqual(self.run_fixture(job, sdk, judge=judge), [1, 2, 3])
        self.assertEqual(len(judge.threads), 6)
        rows = self.sql('SELECT old_url,proposal FROM jev_proposals WHERE run_id=%s ORDER BY old_url', [run['run_id']])
        self.assertEqual(len(rows), len(job['old_urls']))
        self.assertEqual(judge.calls, 2*len(rows))
        calls = len(sdk.calls)
        accounting = self.sql('SELECT * FROM jev_provider_reservations WHERE run_id=%s ORDER BY id', [run['run_id']])
        self.assertTrue(accounting); self.assertTrue(all(row['actual_micro_usd'] is not None for row in accounting))
        self.assertEqual(self.run_fixture(job, sdk), [1, 2, 3])
        self.assertEqual(len(sdk.calls), calls)
        self.assertEqual(self.sql('SELECT old_url,proposal FROM jev_proposals WHERE run_id=%s ORDER BY old_url', [run['run_id']]), rows)
        self.assertEqual(self.sql('SELECT * FROM jev_provider_reservations WHERE run_id=%s ORDER BY id', [run['run_id']]), accounting)
        self.assertTrue(all(row['needs_review'] for row in self.sql('SELECT needs_review FROM url_mappings WHERE session_id=%s', [job['id']])))

    def test_malformed_and_accounting_discrepancy_preserve_unsettled_spend(self):
        for mode in ('malformed', 'accounting'):
            with self.subTest(mode=mode):
                _, run, job = self.job()
                sdk = EmbeddingSDK(mode)
                with self.assertRaisesRegex(ProviderUnavailable, 'embedding_unavailable'):
                    self.run_fixture(job, sdk)
                self.assertEqual(len(sdk.calls), 1)
                reservations = self.sql('SELECT * FROM jev_provider_reservations WHERE run_id=%s', [run['run_id']])
                self.assertEqual(len(reservations), 1)
                self.assertIsNone(reservations[0]['actual_micro_usd'])
                self.assertGreater(reservations[0]['reserved_micro_usd'], 0)
                for table in ('jev_provider_cache', 'jev_proposals'):
                    self.assertEqual(self.sql(f'SELECT count(*) n FROM {table} WHERE run_id=%s', [run['run_id']])[0]['n'], 0)
                # Explicitly exercise worker error finalization; the adapter
                # itself raises and does not declare completion or refund.
                self.runs.finalize_session(job, 'worker-test', 'permanently_failed', 'embedding_unavailable')
                self.assertEqual(self.sql('SELECT status FROM migration_sessions WHERE id=%s', [job['id']])[0]['status'], 'permanently_failed')
                self.assertIsNone(self.sql('SELECT actual_micro_usd FROM jev_provider_reservations WHERE run_id=%s', [run['run_id']])[0]['actual_micro_usd'])

    def test_committed_cache_lost_ack_stops_then_explicit_refine_reuses_receipt(self):
        data, run, job = self.job()
        native = self.native
        class LostAck:
            def __init__(self): self.batches = 0
            def table(self, *args): return native.table(*args)
            def rpc(self, name, args):
                operation = native.rpc(name, args)
                def execute():
                    response = operation.execute()
                    if name == 'cache_jev_response_batch':
                        self.batches += 1
                        raise TimeoutError('synthetic acknowledgment loss after commit')
                    return response
                return NS(execute=execute)
        transport = LostAck(); sdk = EmbeddingSDK()
        with self.assertRaisesRegex(ProviderUnavailable, 'embedding_unavailable'):
            self.run_fixture(job, sdk, client=transport)
        self.assertEqual(transport.batches, 1); self.assertEqual(len(sdk.calls), 1)
        reservation = self.sql('SELECT * FROM jev_provider_reservations WHERE run_id=%s', [run['run_id']])[0]
        self.assertEqual(reservation['actual_micro_usd'], 1)  # Commit happened: do not claim rollback.
        self.assertEqual(self.sql('SELECT count(*) n FROM jev_proposals WHERE run_id=%s', [run['run_id']])[0]['n'], 0)
        self.runs.finalize_session(job, 'worker-test', 'permanently_failed', 'embedding_unavailable')
        self.service.refine(data['user'], data['migration'], run['run_id'], 0, uuid4().hex)
        resumed = self.claim(); self.runs.authorize_dispatch(resumed, 'worker-test')
        self.assertGreater(resumed['attempt_count'], job['attempt_count'])
        self.assertEqual(self.run_fixture(resumed, sdk), [1, 2, 3])
        # The source fragment may need a distinct path embedding; whichever
        # texts are missing are allowed, but committed texts must never recur.
        first = set(sdk.calls[0])
        self.assertTrue(all(not first.intersection(batch) for batch in sdk.calls[1:]))
        self.assertEqual(self.sql('SELECT actual_micro_usd FROM jev_provider_reservations WHERE id=%s', [reservation['id']])[0]['actual_micro_usd'], 1)
        self.assertEqual(self.sql('SELECT count(*) n FROM jev_proposals WHERE run_id=%s', [run['run_id']])[0]['n'], len(resumed['old_urls']))
