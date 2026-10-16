"""Offline fresh-process retrieval determinism; no timing or quality assertions."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import types

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts.research.embedding_materialization.benchmark import prepare, key_for, NoProvider, bounded_results
import numpy as np
from src.redirx.jev import retrieve as current_retrieval
from src.redirx.jev.data import Page
from src.redirx.jev.examples import ExampleBank
from backend.services.jev_runner import DurableEmbeddingCache

RETRIEVAL_BASE = '763822a7dcc2e2dfa1cd7b5a42faf921113cd3a4'
CACHE_BASE = '6cd132827e1a006652720b2545c0e6b68e99bb82'
RETRIEVAL_PATH = 'src/redirx/jev/retrieve.py'
CACHE_PATH = 'backend/services/jev_runner.py'
SEEDS = ('0', '1', '42', '4294967295')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def file_sha(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''): value.update(chunk)
    return value.hexdigest()


def baseline(commit, path, package):
    source = subprocess.check_output(['git', 'show', f'{commit}:{path}'], cwd=ROOT)
    module = types.ModuleType(package + '._determinism_baseline')
    module.__package__ = package
    exec(compile(source, f'{commit}:{path}', 'exec'), module.__dict__)
    return module


class ReadOnlyStore:
    def __init__(self, vectors, mode):
        self.vectors, self.mode = vectors, mode
        self.calls = self.keys = 0
        self.lock = threading.Lock()
    def cache_get_many(self, keys):
        with self.lock:
            self.calls += 1
            self.keys += len(keys)
        result = {}
        # Reverse result insertion order in restored mode: key lookup must not
        # depend on the storage response's row order.
        for key in (reversed(keys) if self.mode == 'restored' else keys):
            if key not in self.vectors: raise AssertionError('Frozen cache miss; provider forbidden')
            values = self.vectors[key].tolist()
            if self.mode == 'restored': values = json.loads(json.dumps(values, allow_nan=False))
            result[key] = {'embedding': values}
        return result
    def reserve(self, *_): raise AssertionError('Provider reservation forbidden')
    def cache_put_many(self, *_): raise AssertionError('Durable writes forbidden')


def contract(recorded):
    new = [Page('contract', f'https://new.example/Docs/topic-{i % 31}/page-{i}?X=A%2FB') for i in range(2000)]
    old = [Page('contract', f'https://old.example/manual/topic-{i % 31}/page-{i}?X=A%2FB') for i in range(500)]
    seeds = [Page(p.site, p.url, true_new_url=new[i].url) for i, p in enumerate(old[:100])]
    vectors = {key_for(current_retrieval.path_embed_text(page)): recorded[i] for i, page in enumerate(new + old)}
    return (new, seeds, [(page, []) for page in old]), vectors


def adversarial():
    roots = ['/Docs/A%2FB?X=One', '/docs/a%2fb?x=one', '/Docs/A/B?X=One',
             '/Caf%C3%A9/?q=A%20B', '/Café/?q=A+B', '/a-a-a-b-b-c', '/index.html', '/?X=%2FKeep']
    new = [Page('ties', 'https://new.example' + path + '&alias=' + str(i))
           for i in range(8) for path in roots]
    old = [Page('ties', 'https://old.example' + path) for path in roots]
    seeds = [Page(page.site, page.url, true_new_url=new[i].url) for i, page in enumerate(old[:6])]
    cases = [(page, []) for page in old] + [(Page('ties', 'https://old.example/Absent/Path'), []),
                                         (Page('missing', '/not-in-site'), [])]
    zero = np.zeros(1536, dtype=np.float32)
    one = zero.copy(); one[0] = 1
    near = one.copy(); near[1] = np.nextafter(np.float32(0), np.float32(1))
    vectors = {key_for(current_retrieval.path_embed_text(page)): (zero, one, one, near)[i % 4]
               for i, page in enumerate(new + old + [p for p, _ in cases])}
    return (new, seeds, cases), vectors


def evaluate(workload, vectors, arm, threads, mode):
    retrieval = current_retrieval if arm == 'current' else baseline(RETRIEVAL_BASE, RETRIEVAL_PATH, 'src.redirx.jev')
    cache_type = DurableEmbeddingCache if arm == 'current' else baseline(CACHE_BASE, CACHE_PATH, 'backend.services').DurableEmbeddingCache
    store = ReadOnlyStore(vectors, mode)
    cache = cache_type(store, NoProvider())
    pages, seeds, cases = workload
    retriever = retrieval.Retriever(pages, True, embedding_cache=cache)
    if mode == 'primed': retriever.prime_queries([page for page, _ in cases] + seeds)
    calibration = retriever.calibrate(seeds)
    # ExampleBank uses the same BM25 implementation as its corresponding arm.
    from src.redirx.jev import examples as examples_module
    original = examples_module.BM25
    try:
        examples_module.BM25 = retrieval.BM25
        bank = ExampleBank(seeds)
    finally: examples_module.BM25 = original
    sample_indices = sorted({0, len(cases) - 1, *range(min(6, len(cases)))})
    sampled_urls = {cases[index][0].url for index in sample_indices}
    def query(case):
        old, frozen_examples = case
        selected = bank.nearest(old, 3)
        examples = frozen_examples or selected
        ranked, diag = retriever.ranked(old, examples)
        ranked_urls = [p.url for p in ranked]
        candidates = ranked_urls[:20]
        excluded = [url for url in ranked_urls if url != pages[0].url][:20]
        if old.url in sampled_urls:
            actual, actual_diag = retriever.candidates(old, 20, examples=examples)
            actual_excluded, _ = retriever.candidates(old, 20, exclude_url=pages[0].url, examples=examples)
            if [p.url for p in actual] != candidates or actual_diag != diag or [p.url for p in actual_excluded] != excluded:
                raise AssertionError('Candidate API differs from full-ranking projection')
        vector = cache.embed([current_retrieval.path_embed_text(old)])[0]
        return {'raw_query': old.url, 'ranked': [p.url for p in ranked], 'diagnostic': diag,
                'candidates': candidates, 'candidate_diagnostic': diag,
                'excluded_candidates': excluded, 'examples': selected,
                'normalized_vector_sha256': hashlib.sha256(vector.tobytes()).hexdigest()}
    outputs = [query(case) for case in cases] if threads == 1 else bounded_results(query, cases, threads)
    # Repeat shared reads after full population, then mutate only returned arrays.
    warm = bounded_results(query, [cases[index] for index in sample_indices], threads)
    if [outputs[index] for index in sample_indices] != warm:
        raise AssertionError('Cold/warm sampled outputs differ within process')
    raw_before = {key: np.array(value, dtype=np.float32).tobytes() for key, value in cache.memory.items()}
    sample = current_retrieval.path_embed_text(cases[0][0])
    mutated = cache.embed([sample]); mutated[:] = 42
    if any(np.array(cache.memory[key], dtype=np.float32).tobytes() != value for key, value in raw_before.items()):
        raise AssertionError('Returned output aliases retained cache')
    if arm == 'current' and not all(isinstance(value, np.ndarray) and value.base is None and not value.flags.writeable
                                     for value in cache.memory.values()):
        raise AssertionError('Compact cache no longer owns independent read-only rows')
    return {'complete_sha256': digest([calibration, outputs]), 'calibration_sha256': digest(calibration),
            'per_query_sha256': [digest(row) for row in outputs], 'new_pages': len(pages), 'queries': len(cases),
            'seeds': len(seeds), 'store_calls': store.calls, 'store_keys': store.keys,
            'candidate_api_and_warm_sample_indices': sample_indices,
            'candidate_api_and_warm_sample_query_sha256': [digest([cases[index][0].site, cases[index][0].url]) for index in sample_indices],
            'memory_entries': len(cache.memory), 'warm_repeat_exact': True, 'cache_ownership_checked': True}


def cold_collision(arm):
    """Force six initially missing same-key durable reads to overlap."""
    cache_type = DurableEmbeddingCache if arm == 'current' else baseline(CACHE_BASE, CACHE_PATH, 'backend.services').DurableEmbeddingCache
    vector = np.arange(1536, dtype=np.float32) / np.float32(1536)
    vector.flags.writeable = False
    barrier = threading.Barrier(6)
    class CollisionStore(ReadOnlyStore):
        def cache_get_many(self, keys):
            rows = super().cache_get_many(keys)
            barrier.wait(timeout=15)
            return rows
    store = CollisionStore({key_for('same raw fixture'): vector}, 'restored')
    cache = cache_type(store, NoProvider())
    with ThreadPoolExecutor(max_workers=6) as executor:
        outputs = list(executor.map(lambda _: cache.embed(['same raw fixture', 'same raw fixture']), range(6)))
    hashes = {hashlib.sha256(output.tobytes()).hexdigest() for output in outputs}
    if len(hashes) != 1: raise AssertionError('Concurrent cold durable reads disagree')
    if len({id(output) for output in outputs}) != 6: raise AssertionError('Returned arrays shared across callers')
    for output in outputs: output[:] = 42
    retained = np.array(cache.memory[key_for('same raw fixture')], dtype=np.float32)
    if not np.array_equal(retained, vector): raise AssertionError('Caller mutation changed raw cache')
    return {'normalized_float32_sha256': hashes.pop(), 'durable_calls': store.calls,
            'durable_keys': store.keys, 'isolated_outputs': True}


def child(args):
    _, historical, frozen = prepare(args.fixture, args.embeddings)
    recorded = list(frozen.values())[:2500]
    work = {'historical-capped': (historical, frozen), 'contract-500x2000': contract(recorded), 'raw-alias-ties': adversarial()}
    result = {name: evaluate(workload, vectors, args.arm, args.threads, args.mode) for name, (workload, vectors) in work.items()}
    result['cold-six-same-key'] = {'complete_sha256': digest(cold_collision(args.arm))}
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--fixture', type=Path, default=Path('/private/tmp/jev-pageindex-pareto-20261015/fixture-v3'))
    p.add_argument('--embeddings', type=Path, default=Path('/private/tmp/jev-pageindex-pareto-20261015/prepared/embeddings.sqlite'))
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--child', action='store_true')
    p.add_argument('--blas-threads', type=int, choices=(1, 2, 4), default=1, help='BLAS threads in fresh audit children; no product setting change')
    p.add_argument('--arm', choices=('current', 'baseline'))
    p.add_argument('--threads', type=int, choices=(1, 6))
    p.add_argument('--mode', choices=('cold', 'primed', 'restored'))
    args = p.parse_args()
    if args.out.exists(): p.error('Refusing to overwrite receipt')
    if args.child:
        args.out.write_text(json.dumps(child(args), indent=2) + '\n'); return
    paths = [args.fixture / 'inputs.json', args.fixture / 'inventories.json', args.embeddings]
    before = {path.name: file_sha(path) for path in paths}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for seed in SEEDS:
        for arm in ('baseline', 'current'):
            for threads in (1, 6):
                for mode in ('cold', 'primed', 'restored'):
                    output = args.out.parent / f'{args.out.stem}-{seed}-{arm}-{threads}-{mode}.json'
                    env = {**os.environ, 'PYTHONHASHSEED': seed, 'PYTHON_DOTENV_DISABLED': '1', 'POSTHOG_API_KEY': '',
                           'OPENBLAS_NUM_THREADS': str(args.blas_threads)}
                    subprocess.run([sys.executable, str(Path(__file__).resolve()), '--child', '--fixture', str(args.fixture),
                                    '--embeddings', str(args.embeddings), '--out', str(output), '--arm', arm,
                                    '--threads', str(threads), '--mode', mode], env=env, check=True)
                    result = json.loads(output.read_text())
                    rows.append({'hashseed': seed, 'arm': arm, 'threads': threads, 'mode': mode, 'workloads': result})
                    print(seed, arm, threads, mode, {name: value['complete_sha256'][:12] for name, value in result.items()}, flush=True)
    after = {path.name: file_sha(path) for path in paths}
    if before != after: raise AssertionError('Read-only fixture changed')
    groups = {name: sorted({row['workloads'][name]['complete_sha256'] for row in rows}) for name in rows[0]['workloads']}
    report = {'python': sys.version, 'numpy': np.__version__, 'platform': platform.platform(),
              'baseline_retrieval_commit': RETRIEVAL_BASE, 'baseline_cache_commit': CACHE_BASE,
              'baseline_source_sha256': {path: hashlib.sha256(subprocess.check_output(['git', 'show', f'{commit}:{path}'], cwd=ROOT)).hexdigest()
                                         for commit, path in ((RETRIEVAL_BASE, RETRIEVAL_PATH), (CACHE_BASE, CACHE_PATH))},
              'thread_environment': {**{key: os.environ.get(key) for key in ('OMP_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'MKL_NUM_THREADS')},
                                     'OPENBLAS_NUM_THREADS': str(args.blas_threads)},
              'numpy_build': np.show_config(mode='dicts'),
              'current_source_sha256': {path: file_sha(ROOT / path) for path in (RETRIEVAL_PATH, CACHE_PATH)},
              'fixture_sha256': before, 'fixtures_unchanged': True, 'provider_calls': 0,
              'scope': 'All full rankings, numeric inputs and calibration exact; all candidate/exclusion projections hashed; candidate API and warm replay checked only at recorded fixed boundary samples. One Python/NumPy/BLAS stack, no latency or quality claim',
              'unique_complete_digests': groups, 'runs': rows}
    args.out.write_text(json.dumps(report, indent=2) + '\n')
    if any(len(values) != 1 for values in groups.values()): raise SystemExit('Output differences found; inspect retained per-process receipts')
    print('All complete outputs equal across fresh processes, arms, hash seeds, cache modes and thread counts.')

if __name__ == '__main__': main()
