"""Fresh-process current Jev runner resource study; no provider/database network."""
from __future__ import annotations
import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path
import platform
import resource
import sqlite3
import subprocess
import sys
import time
import tracemalloc
import weakref
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
CONTROLS = ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
            'VECLIB_MAXIMUM_THREADS', 'BLIS_NUM_THREADS', 'GOTO_NUM_THREADS', 'OPENBLAS_DEFAULT_NUM_THREADS')
FIXTURE = Path('/private/tmp/jev-pageindex-pareto-20261015/fixture-v3')
EMBEDDINGS = Path('/private/tmp/jev-pageindex-pareto-20261015/prepared/embeddings.sqlite')
ARMS = ('default', '1', '2')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def child_environment(arm):
    env = dict(os.environ)
    removed = {key: env.pop(key, None) for key in CONTROLS}
    env['PYTHONHASHSEED'] = '0'
    if arm != 'default': env['OPENBLAS_NUM_THREADS'] = arm
    return env, removed


def fixtures(kind, fixture=FIXTURE, embeddings=EMBEDDINGS):
    import numpy as np
    from src.redirx.jev.data import Page
    from src.redirx.jev.retrieve import path_embed_text
    from src.redirx.jev.jev import encoded
    inputs = json.loads((fixture/'inputs.json').read_text())
    inventories = json.loads((fixture/'inventories.json').read_text())
    jobs, excluded = [], []
    if kind == 'historical':
        for site, urls in inventories.items():
            cases = [r for r in inputs if r['site'] == site]
            if len(urls) > 2000:
                excluded.append({'site': site, 'destinations': len(urls), 'cases': len(cases)})
                continue
            seeds = list({(e['old_url'], e['new_url']): e for r in cases for e in r['examples']}.values())
            old = list(dict.fromkeys([r['old_url'] for r in cases] + [e['old_url'] for e in seeds]))
            jobs.append({'name': site, 'old_urls': old, 'new_urls': urls, 'seeds': seeds})
        texts = list(dict.fromkeys(path_embed_text(Page('', url)) for j in jobs
                                   for url in j['new_urls'] + j['old_urls']))
        recorded_texts = texts
    else:
        old = [f'https://old.example/manual/topic-{i%31}/page-{i}' for i in range(500)]
        new = [f'https://new.example/docs/topic-{i%31}/page-{i}' for i in range(2000)]
        jobs = [{'name': 'synthetic500x2000', 'old_urls': old, 'new_urls': new, 'seeds': []}]
        texts = list(dict.fromkeys(path_embed_text(Page('', u)) for u in new + old))
        recorded_texts = list(dict.fromkeys(path_embed_text(Page('', u))
                              for urls in inventories.values() for u in urls))[:len(texts)]
        assert len(recorded_texts) == len(texts) == 2500
    vectors = {}
    with sqlite3.connect(embeddings.resolve().as_uri()+'?mode=ro', uri=True) as db:
        for text, recorded in zip(texts, recorded_texts):
            oldkey = hashlib.sha1(('text-embedding-3-small\0'+recorded).encode()).hexdigest()
            row = db.execute('SELECT v FROM embeddings WHERE k=?', (oldkey,)).fetchone()
            if row is None: raise ValueError('Missing frozen vector; no fallback')
            vector = np.frombuffer(row[0], dtype=np.float32)
            assert vector.shape == (1536,) and np.isfinite(vector).all()
            key = hashlib.sha256(encoded({'model': 'text-embedding-3-small', 'text': text})).hexdigest()
            vectors[key] = vector
    for index, job in enumerate(jobs):
        job.update(id=f'00000000-0000-0000-0000-{index+1:012d}',
                   mcp_run_id=f'00000000-0000-0000-0001-{index+1:012d}', attempt_count=1)
        assert len(job['old_urls']) <= 500 and len(job['new_urls']) <= 2000
        assert len(job['seeds']) <= 100
    return jobs, vectors, excluded


class Query:
    def __init__(self, client, table): self.client, self.table_name, self.keys = client, table, []
    def select(self, *_): return self
    def eq(self, *_): return self
    def limit(self, *_): return self
    def order(self, *_): return self
    def range(self, *_): return self
    def in_(self, _, keys): self.keys = keys; return self
    def execute(self):
        if self.table_name in ('jev_proposals', 'jev_runs'): return SimpleNamespace(data=[])
        assert self.table_name == 'jev_provider_cache'
        self.client.cache_reads += 1
        self.client.cache_keys += len(self.keys)
        return SimpleNamespace(data=[{'cache_key': key, 'response': {'embedding': self.client.vectors[key].tolist()}}
                                     for key in self.keys])


class Client:
    def __init__(self, job, vectors):
        self.job, self.vectors = job, vectors
        self.saved, self.requests = {}, {}
        self.cache_reads = self.cache_keys = 0
        self.stub_seconds = []
    def table(self, name): return Query(self, name)
    def rpc(self, name, args):
        from src.redirx.jev.jev import MODEL, PROMPT_VERSION
        assert args['p_run_id'] == self.job['mcp_run_id'] and args['p_worker_id'] == 'offline-study'
        if name == 'prepare_jev_pass':
            data = dict(model=MODEL, prompt_version=PROMPT_VERSION, seeds=self.job['seeds'],
                        **{'pass': 1, 'pass_seed_revision': 0})
        else:
            assert name == 'save_jev_proposal', 'Unexpected mutation/provider budget attempt'
            assert args['p_old_url'] not in self.saved
            self.saved[args['p_old_url']] = args['p_proposal']
            data = {}
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))


class NoEmbeddingProvider:
    @property
    def embeddings(self): raise AssertionError('No network embedding calls allowed')


class JudgmentStub:
    def __init__(self, store): self.client = store.client
    def ask(self, state, questions):
        start = time.perf_counter()
        stage = 'wide' if 'has_destination' in questions else 'narrow'
        self.client.requests[(state['old_page']['url'], stage)] = (state, questions)
        target = next(iter(state['candidates']))
        answers = {'target': {'probabilities': {key: float(key == target)
                    for key in [*state['candidates'], 'none']}, 'confidence': 1.},
                   'has_destination': {'noul': 1.}}
        for key in state['candidates']:
            answers['relation_'+key] = {'probabilities': {'3': 1.}, 'score': 3., 'confidence': 1.}
        response = {'answers': answers, 'usage': {'input_tokens': 0}, 'latency': 0., 'cached': True}
        self.client.stub_seconds.append(time.perf_counter()-start)
        return response


def execute(jobs, vectors, validation=False, memory=False):
    from backend.services.jev_runner import JevPipelineRunner, DurableEmbeddingCache
    from src.redirx.jev.retrieve import Retriever
    full_results, rows, cache_refs = [], [], []
    if memory: tracemalloc.start()
    for job in jobs:
        client = Client(job, vectors)
        milestones, calibration, ranking = {}, [], {}
        def progress(_, phase, *_args): milestones[phase] = (time.perf_counter(), time.process_time())
        def embed_factory(store):
            cache = DurableEmbeddingCache(store, NoEmbeddingProvider())
            cache_refs.append(weakref.ref(cache))
            return cache
        original_calibrate, original_ranked = Retriever.calibrate, Retriever.ranked
        def calibrate(instance, *args, **kwargs):
            before = time.perf_counter()
            result = original_calibrate(instance, *args, **kwargs)
            calibration.append((time.perf_counter()-before, result))
            return result
        def ranked(instance, old, *args, **kwargs):
            result = original_ranked(instance, old, *args, **kwargs)
            ranking[old.url] = ([p.url for p in result[0]], result[1])
            return result
        runner = JevPipelineRunner(client, JudgmentStub, embed_factory)
        # Wrapper creation/imports are outside the boundary; actual prepare RPC,
        # indexing, prime, calibration, provider flow and destruction are inside.
        with patch.object(Retriever, 'calibrate', calibrate):
            ranking_patch = patch.object(Retriever, 'ranked', ranked) if validation else None
            if ranking_patch: ranking_patch.start()
            try:
                start, cpu_start = time.perf_counter(), time.process_time()
                runner._run(job, 'offline-study', progress)
                end, cpu_end = time.perf_counter(), time.process_time()
            finally:
                if ranking_patch: ranking_patch.stop()
        assert all(ref() is None for ref in cache_refs), 'Actual runner retained cache after return'
        expected = set(job['old_urls']) - {e['old_url'] for e in job['seeds']}
        assert set(client.saved) == expected and len(client.requests) == 2*len(expected)
        result = {'proposals': client.saved, 'requests': [[*key, value] for key, value in sorted(client.requests.items())],
                  'calibration': [value for _, value in calibration], 'rankings': ranking}
        full_results.append(result)
        rows.append({'site': job['name'], 'old_count': len(job['old_urls']), 'new_count': len(job['new_urls']),
            'seeds': len(job['seeds']), 'proposals': len(expected), 'stub_calls': len(client.requests),
            'cache_reads': client.cache_reads, 'cache_keys': client.cache_keys,
            'prepare_seconds': milestones[1][0]-start,
            'setup_prime_calibration_seconds': milestones[2][0]-milestones[1][0],
            'calibration_seconds': sum(sec for sec, _ in calibration),
            'page_seconds': milestones[3][0]-milestones[2][0], 'teardown_seconds': end-milestones[3][0],
            'full_seconds': end-start, 'cpu_seconds': cpu_end-cpu_start,
            'stub_thread_summed_seconds': sum(client.stub_seconds), 'output_sha256': digest(result)})
        # Measurement ends with actual _run local teardown. Client fixture outputs
        # are retained for parity, not production cache objects.
    mem = None
    if memory:
        retained, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
        mem = {'retained_bytes': retained, 'peak_bytes': peak}
    return {'jobs': rows, 'output_sha256': digest(full_results), 'tracemalloc': mem,
            'full_seconds': sum(r['full_seconds'] for r in rows),
            'cpu_seconds': sum(r['cpu_seconds'] for r in rows),
            'cache_released': all(ref() is None for ref in cache_refs), 'outputs': full_results if validation else None}


def child(args):
    child_start = time.perf_counter()
    import numpy as np
    from threadpoolctl import threadpool_info
    pools = threadpool_info()
    expected = None if args.arm == 'default' else int(args.arm)
    assert any(p['internal_api'] == 'openblas' for p in pools), 'This experiment controls OpenBLAS only'
    if expected is not None: assert all(p['num_threads'] == expected for p in pools if p['internal_api'] == 'openblas')
    jobs, vectors, excluded = fixtures(args.workload, args.fixture, args.embeddings)
    loaded = time.perf_counter()
    rss_scale = 1 if sys.platform == 'darwin' else 1024
    before_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * rss_scale
    report = execute(jobs, vectors, args.validation, args.memory)
    report.update(arm=args.arm, workload=args.workload, validation=args.validation, memory=args.memory,
        excluded=excluded, platform=platform.platform(), python=sys.version, numpy=np.__version__,
        threadpools_before=pools, threadpools_after=threadpool_info(), cpu_count=os.cpu_count(),
        controls={key: os.getenv(key) for key in CONTROLS}, hash_seed=os.getenv('PYTHONHASHSEED'),
        preload_seconds=loaded-child_start, child_seconds=time.perf_counter()-child_start,
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * rss_scale,
        pre_service_highwater_rss_bytes=before_rss, provider_network_calls=0)
    args.out.write_text(json.dumps(report, indent=2)+'\n')


def orchestrate(args):
    args.out.mkdir(parents=True, exist_ok=False)
    manifest = {'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'sources': {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in [
            'backend/services/jev_runner.py', 'src/redirx/jev/retrieve.py', 'src/redirx/jev/pipeline.py',
            'src/redirx/jev/examples.py', 'scripts/research/numeric_threads/study.py']},
        'fixture_hashes': {p: hashlib.sha256((args.fixture/p).read_bytes()).hexdigest() for p in ['inputs.json', 'inventories.json']},
        'embedding_sha256': hashlib.sha256(args.embeddings.read_bytes()).hexdigest(), 'runs': []}
    permutations = list(itertools.permutations(ARMS))
    for workload in ('historical', 'synthetic'):
        rounds = [(0, ARMS)] if args.validation else list(enumerate(permutations))
        if args.memory: rounds = [(0, ARMS)]
        for rep, order in rounds:
            for arm in order:
                name = f'{workload}-{rep}-{arm}.json'
                env, removed = child_environment(arm)
                command = [sys.executable, str(Path(__file__).resolve()), '--child', '--arm', arm,
                    '--workload', workload, '--fixture', str(args.fixture), '--embeddings', str(args.embeddings),
                    '--out', str(args.out/name)]
                if args.validation: command.append('--validation')
                if args.memory: command.append('--memory')
                start = time.perf_counter()
                try:
                    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=300)
                except subprocess.TimeoutExpired as error:
                    result = SimpleNamespace(returncode=124, stderr=f'Child exceeded300seconds: {error}')
                entry = dict(file=name, repetition=rep, order=order, arm=arm, workload=workload,
                    parent_elapsed_seconds=time.perf_counter()-start, removed_inherited_controls=removed,
                    returncode=result.returncode, stderr=result.stderr)
                manifest['runs'].append(entry)
                (args.out/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
                if result.returncode: raise RuntimeError(result.stderr)
                data = json.loads((args.out/name).read_text())
                print(name, round(data['full_seconds'], 4), round(data['cpu_seconds'], 4), flush=True)
    for workload in ('historical', 'synthetic'):
        records = [json.loads((args.out/r['file']).read_text()) for r in manifest['runs'] if r['workload'] == workload]
        assert len({r['output_sha256'] for r in records}) == 1, f'Exact output mismatch: {workload}'
    manifest['exact_output_match'] = True
    (args.out/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--fixture', type=Path, default=FIXTURE)
    p.add_argument('--embeddings', type=Path, default=EMBEDDINGS)
    p.add_argument('--child', action='store_true'); p.add_argument('--validation', action='store_true')
    p.add_argument('--memory', action='store_true'); p.add_argument('--arm', choices=ARMS)
    p.add_argument('--workload', choices=('historical', 'synthetic'))
    args = p.parse_args()
    if args.child:
        if args.out.exists(): p.error('Refusing overwrite')
        child(args)
    else: orchestrate(args)

if __name__ == '__main__': main()
