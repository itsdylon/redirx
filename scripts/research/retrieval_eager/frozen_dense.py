"""Verify complete rankings using recorded 1536-dimensional embeddings, offline.

SQLite is opened read-only. A missing vector is a hard failure; this script has no
provider import or network fallback. Index construction/cache reads are timed.
"""
import argparse
import gc
import hashlib
import json
import sqlite3
import statistics
import subprocess
import time
import types
from pathlib import Path

from benchmark import BASELINE, ROOT, SOURCE, historical, np, optimized


class FrozenEmbeddings:
    def __init__(self, path):
        self.db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        self.memory = {}
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        keys = [hashlib.sha1(('text-embedding-3-small\0' + text).encode()).hexdigest() for text in texts]
        missing = list(dict.fromkeys(key for key in keys if key not in self.memory))
        for start in range(0, len(missing), 500):
            chunk = missing[start:start + 500]
            for key, value in self.db.execute('SELECT k,v FROM embeddings WHERE k IN (' + ','.join('?' for _ in chunk) + ')', chunk):
                vector = np.frombuffer(value, dtype=np.float32)
                if vector.shape != (1536,) or not np.isfinite(vector).all():
                    raise ValueError('Invalid frozen vector')
                self.memory[key] = vector
        if any(key not in self.memory for key in keys):
            raise RuntimeError('Frozen embedding cache miss; no network fallback')
        mat = np.array([self.memory[key] for key in keys], dtype=np.float32)
        return mat / np.maximum(np.linalg.norm(mat, axis=1, keepdims=True), 1e-9)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', required=True, type=Path)
    parser.add_argument('--embeddings', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    if args.out.exists():
        parser.error('Refusing to overwrite receipt')
    workload = historical(args.fixture)
    source = subprocess.check_output(['git', 'show', f'{BASELINE}:{SOURCE}'], cwd=ROOT)
    baseline = types.ModuleType('src.redirx.jev._benchmark_baseline')
    baseline.__package__ = 'src.redirx.jev'
    exec(compile(source, f'{BASELINE}:{SOURCE}', 'exec'), baseline.__dict__)
    rows = []
    for repetition in range(3):
        order = [('baseline', baseline), ('optimized', optimized)]
        if repetition % 2:
            order.reverse()
        for arm, module in order:
            gc.collect()
            cache = FrozenEmbeddings(args.embeddings)
            start = time.perf_counter()
            retriever = module.Retriever(workload[0], True, embedding_cache=cache)
            retriever.prime_queries([old for old, _ in workload[2]] + workload[1])
            built = time.perf_counter()
            calibration = retriever.calibrate(workload[1])
            calibrated = time.perf_counter()
            outputs = []
            for old, examples in workload[2]:
                ranked, diagnostic = retriever.ranked(old, examples)
                outputs.append(([p.url for p in ranked], diagnostic))
            end = time.perf_counter()
            digest = hashlib.sha256(json.dumps([calibration, outputs], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            row = {'arm': arm, 'repetition': repetition, 'order': [a for a, _ in order],
                   'build_and_prime_seconds': built - start, 'calibration_seconds': calibrated - built,
                   'query_seconds': end - calibrated, 'total_seconds': end - start,
                   'output_sha256': digest, 'embedding_cache_calls': cache.calls}
            cache.db.close()
            del retriever, cache
            rows.append(row)
            print(arm, repetition, round(row['total_seconds'], 4), flush=True)
    assert len({r['output_sha256'] for r in rows}) == 1, 'Rankings/diagnostics/calibration changed'
    medians = {arm: {metric: statistics.median(r[metric] for r in rows if r['arm'] == arm)
                      for metric in ('build_and_prime_seconds', 'calibration_seconds', 'query_seconds', 'total_seconds')}
               for arm in ('baseline', 'optimized')}
    report = {'baseline_commit': BASELINE, 'baseline_sha256': hashlib.sha256(source).hexdigest(),
              'optimized_sha256': hashlib.sha256((ROOT / SOURCE).read_bytes()).hexdigest(),
              'fixture_hashes': {name: hashlib.sha256((args.fixture / name).read_bytes()).hexdigest() for name in ('inputs.json', 'inventories.json')},
              'embedding_sha256': hashlib.sha256(args.embeddings.read_bytes()).hexdigest(),
              'new_pages': len(workload[0]), 'queries': len(workload[2]), 'seed_pages': len(workload[1]),
              'model_calls': 0, 'dimensions': 1536, 'dtype': 'float32', 'exact_output_match': True,
              'runs': rows, 'medians': medians,
              'total_speedup': medians['baseline']['total_seconds'] / medians['optimized']['total_seconds']}
    args.out.write_text(json.dumps(report, indent=2) + '\n')
    print('All frozen dense rankings match:', args.out)


if __name__ == '__main__':
    main()
