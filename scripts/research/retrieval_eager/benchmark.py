"""Offline, paired retrieval benchmark; never imports a provider or reads labels.

Run from the repository root. Historical data is optional and stays external.
The baseline is loaded from a pinned Git object, not a reimplementation.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import numpy as np
from src.redirx.jev import retrieve as optimized
from src.redirx.jev.data import Page

BASELINE = '763822a7dcc2e2dfa1cd7b5a42faf921113cd3a4'
SOURCE = 'src/redirx/jev/retrieve.py'


class OfflineEmbeddings:
    """Deterministic normalized vectors, not a quality or paid-cache simulation."""
    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        data = np.array([list(hashlib.sha256(s.encode()).digest()) for s in texts], dtype=float)
        return data / np.linalg.norm(data, axis=1, keepdims=True)


def synthetic():
    pages = [Page('contract', f'https://new.example/docs/v2/topic-{i % 31}/page-{i}?lang=en') for i in range(2000)]
    seeds = [Page('contract', f'https://old.example/manual/v1/topic-{i % 31}/page-{i}?lang=en',
                  true_new_url=pages[i].url) for i in range(100)]
    examples = [{'old_url': p.url, 'new_url': p.true_new_url} for p in seeds[:8]]
    cases = [(Page('contract', f'https://old.example/manual/v1/topic-{i % 31}/page-{i + 100}?lang=en'), examples)
             for i in range(500)]
    return pages, seeds, cases


def historical(path):
    inputs = json.loads((path / 'inputs.json').read_text())
    inventories = json.loads((path / 'inventories.json').read_text())
    pages = [Page(site, url) for site, urls in inventories.items() for url in urls]
    pairs = {}
    cases = []
    for row in inputs:
        cases.append((Page(row['site'], row['old_url']), row['examples']))
        for ex in row['examples']:
            pairs[(row['site'], ex['old_url'], ex['new_url'])] = Page(row['site'], ex['old_url'], true_new_url=ex['new_url'])
    return pages, list(pairs.values()), cases


def execute(module, workload, dense):
    pages, seeds, cases = workload
    cache = OfflineEmbeddings() if dense else None
    start = time.perf_counter()
    retriever = module.Retriever(pages, dense, embedding_cache=cache)
    built = time.perf_counter()
    calibration = retriever.calibrate(seeds)
    calibrated = time.perf_counter()
    outputs = []
    for old, examples in cases:
        ranked, diagnostic = retriever.ranked(old, examples)
        outputs.append(([p.url for p in ranked], diagnostic))
    ended = time.perf_counter()
    digest = hashlib.sha256(json.dumps([calibration, outputs], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    arrays = sum(indices.nbytes + values.nbytes for index in retriever.bm25_path.values()
                 for indices, values in getattr(index, '_term_scores', {}).values())
    return {'build_seconds': built - start, 'calibration_seconds': calibrated - built,
            'query_seconds': ended - calibrated, 'total_seconds': ended - start,
            'output_sha256': digest, 'embedding_calls': cache.calls if cache else 0,
            'additional_score_array_bytes': arrays}


def index_memory(module, workload):
    gc.collect()
    tracemalloc.start()
    retriever = module.Retriever(workload[0], False)
    retained, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del retriever
    return {'tracemalloc_index_retained_bytes': retained, 'tracemalloc_index_peak_bytes': peak}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--repetitions', type=int, default=3)
    args = parser.parse_args()
    if args.out.exists():
        parser.error('Refusing to overwrite a benchmark receipt')
    if args.repetitions < 2:
        parser.error('At least two alternating repetitions are required')
    old_source = subprocess.check_output(['git', 'show', f'{BASELINE}:{SOURCE}'], cwd=ROOT)
    baseline = types.ModuleType('src.redirx.jev._benchmark_baseline')
    baseline.__package__ = 'src.redirx.jev'
    exec(compile(old_source, f'{BASELINE}:{SOURCE}', 'exec'), baseline.__dict__)
    workloads = {'contract-500x2000': synthetic()}
    if args.fixture:
        workloads['historical-417'] = historical(args.fixture)
    report = {'baseline_commit': BASELINE, 'baseline_sha256': hashlib.sha256(old_source).hexdigest(),
              'optimized_sha256': hashlib.sha256((ROOT / SOURCE).read_bytes()).hexdigest(),
              'python': sys.version, 'numpy': np.__version__, 'platform': platform.platform(),
              'model_calls': 0, 'embedding_fixture': 'offline sha256 vectors; no quality claim',
              'workloads': {}, 'fixture_hashes': {}}
    if args.fixture:
        report['fixture_hashes'] = {name: hashlib.sha256((args.fixture / name).read_bytes()).hexdigest()
                                    for name in ('inputs.json', 'inventories.json')}
    for name, workload in workloads.items():
        for dense in (False, True):
            key = f'{name}-dense-{dense}'
            rows = []
            # Warm the Python/NumPy paths using a separate tiny workload. Indexes
            # and query outputs in every measured run are constructed fresh.
            tiny = (workload[0][:20], [], workload[2][:1])
            execute(baseline, tiny, dense)
            execute(optimized, tiny, dense)
            for repetition in range(args.repetitions):
                order = [('baseline', baseline), ('optimized', optimized)]
                if repetition % 2:
                    order.reverse()
                for arm, module in order:
                    gc.collect()
                    row = execute(module, workload, dense)
                    row.update({'arm': arm, 'repetition': repetition, 'order': [x[0] for x in order]})
                    rows.append(row)
                    print(key, arm, repetition, round(row['total_seconds'], 4), flush=True)
            if len({r['output_sha256'] for r in rows}) != 1:
                raise AssertionError(f'Candidate/diagnostic/calibration mismatch: {key}')
            medians = {arm: {metric: statistics.median(r[metric] for r in rows if r['arm'] == arm)
                             for metric in ('build_seconds', 'calibration_seconds', 'query_seconds', 'total_seconds')}
                       for arm in ('baseline', 'optimized')}
            report['workloads'][key] = {'new_pages': len(workload[0]), 'seed_pages': len(workload[1]),
                                        'queries': len(workload[2]), 'exact_output_match': True,
                                        'runs': rows, 'medians': medians,
                                        'total_speedup': medians['baseline']['total_seconds'] / medians['optimized']['total_seconds']}
        report['workloads'][f'{name}-memory'] = {arm: index_memory(module, workload)
                                                for arm, module in [('baseline', baseline), ('optimized', optimized)]}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + '\n')
    print('Exact baseline equivalence passed; receipt:', args.out)


if __name__ == '__main__':
    main()
