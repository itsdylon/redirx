"""Actual durable embedding adapter, recorded vectors, stubbed store, no network."""
from __future__ import annotations
import argparse
import cProfile
import gc
from datetime import datetime, timezone
import hashlib
import io
import json
import platform
import pstats
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import tracemalloc
import types
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'scripts/research/retrieval_eager'))
from benchmark import historical  # resolved to sibling helper when run as script
import numpy as np
from src.redirx.jev.jev import encoded
from src.redirx.jev.retrieve import Retriever,path_embed_text
BASELINE='6cd132827e1a006652720b2545c0e6b68e99bb82'
SOURCE='backend/services/jev_runner.py'


def key_for(text):
    return hashlib.sha256(encoded({'model':'text-embedding-3-small','text':text})).hexdigest()


def prepare(fixture,embeddings):
    workload=historical(fixture)
    counts={}
    for page in workload[0]:counts[page.site]=counts.get(page.site,0)+1
    allowed={s for s,n in counts.items() if n<=2000}
    capped=tuple([p for p in workload[i] if (p[0].site if i==2 else p.site) in allowed] for i in range(3))
    texts=list(dict.fromkeys(path_embed_text(p) for p in workload[0]))[:2500]
    all_texts=list(dict.fromkeys(texts+[path_embed_text(p) for p in capped[0]+capped[1]]+[path_embed_text(p) for p,_ in capped[2]]))
    db=sqlite3.connect(embeddings.resolve().as_uri()+'?mode=ro',uri=True)
    vectors={}
    for text in all_texts:
        oldkey=hashlib.sha1(('text-embedding-3-small\0'+text).encode()).hexdigest()
        row=db.execute('SELECT v FROM embeddings WHERE k=?',(oldkey,)).fetchone()
        if row is None:raise RuntimeError('Recorded vector missing; no provider fallback')
        vector=np.frombuffer(row[0],dtype=np.float32)
        if vector.shape!=(1536,) or not np.isfinite(vector).all():raise ValueError('Invalid recorded vector')
        vectors[key_for(text)]=vector
    db.close()
    return texts,capped,vectors


class Store:
    """Fresh JSON-like float lists on read; no DB latency or provider writes."""
    def __init__(self,vectors):
        self.vectors=vectors
        self.calls=0
        self.nonempty_calls=0
        self.keys=0
        self.lock=threading.Lock()
    def cache_get_many(self,keys):
        with self.lock:
            self.calls+=1
            self.nonempty_calls+=bool(keys)
            self.keys+=len(keys)
        return {key:{'embedding':self.vectors[key].tolist()} for key in keys if key in self.vectors}
    def reserve(self,*args):raise AssertionError('Provider reservation forbidden in frozen benchmark')
    def cache_put_many(self,*args):raise AssertionError('Provider write forbidden in frozen benchmark')


class NoProvider:
    @property
    def embeddings(self):raise AssertionError('Provider access forbidden')


def load_module(source,arm):
    # Measure the actual staged candidate, not a parallel implementation.
    text=(ROOT/SOURCE).read_text() if arm=='arrays' else source.decode()
    module=types.ModuleType('backend.services._embedding_'+arm)
    module.__package__='backend.services'
    exec(compile(text,arm,'exec'),module.__dict__)
    return module


def payload_size(memory):
    total=sys.getsizeof(memory)+sum(sys.getsizeof(k) for k in memory)
    for value in memory.values():
        total+=sys.getsizeof(value)
        if isinstance(value,list):total+=sum(sys.getsizeof(x) for x in value)
    return total


def bounded_results(function, items, workers):
    # Match the worker's bounded queue: at most six active futures, not an
    # executor.map queue containing every page. Preserve input order for hashes.
    results=[None]*len(items)
    queue=iter(enumerate(items))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        active={}
        for _ in range(workers):
            item=next(queue,None)
            if item is not None:active[pool.submit(function,item[1])]=item[0]
        while active:
            completed,_=wait(active,return_when=FIRST_COMPLETED)
            for future in completed:
                results[active.pop(future)]=future.result()
                item=next(queue,None)
                if item is not None:active[pool.submit(function,item[1])]=item[0]
    return results


def run(module,texts,workload,vectors,kind,threads,memory=False):
    if memory:tracemalloc.start()
    store=Store(vectors)
    cache=module.DurableEmbeddingCache(store,NoProvider())
    started_utc=datetime.now(timezone.utc).isoformat()
    start=time.perf_counter()
    if kind=='cache2500':
        index=cache.embed(texts[:2000])
        primed=cache.embed(texts[2000:])
        built=time.perf_counter()
        calibrated=built
        if threads==1:results=[cache.embed([text]) for text in texts[2000:]]
        else:
            results=bounded_results(lambda text:cache.embed([text]),texts[2000:],threads)
        ended=time.perf_counter()
        digest=hashlib.sha256(index.tobytes()+primed.tobytes()+b''.join(result.tobytes() for result in results)).hexdigest()
        assert index.dtype==primed.dtype==np.float32 and all(r.dtype==np.float32 for r in results)
    else:
        retriever=Retriever(workload[0],True,embedding_cache=cache)
        retriever.prime_queries([p for p,_ in workload[2]]+workload[1])
        built=time.perf_counter()
        calibration=retriever.calibrate(workload[1])
        calibrated=time.perf_counter()
        def query(case):
            ranked,diagnostic=retriever.ranked(*case)
            return [p.url for p in ranked],diagnostic
        if threads==1:results=[query(case) for case in workload[2]]
        else:
            results=bounded_results(query,workload[2],threads)
        ended=time.perf_counter()
        digest=hashlib.sha256(json.dumps([calibration,results],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    row={'started_utc':started_utc,'ended_utc':datetime.now(timezone.utc).isoformat(),'setup_prime_seconds':built-start,'calibration_seconds':calibrated-built,'query_seconds':ended-calibrated,'total_seconds':ended-start,
         'output_sha256':digest,'store_calls':store.calls,'nonempty_store_calls':store.nonempty_calls,'store_keys':store.keys,
         'memory_entries':len(cache.memory),'cache_payload_bytes':payload_size(cache.memory),'provider_calls':0}
    if memory:
        row['tracemalloc_retained_bytes'],row['tracemalloc_peak_bytes']=tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return row


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--fixture',type=Path,default=Path('/private/tmp/jev-pageindex-pareto-20261015/fixture-v3'))
    p.add_argument('--embeddings',type=Path,default=Path('/private/tmp/jev-pageindex-pareto-20261015/prepared/embeddings.sqlite'))
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--profile',action='store_true')
    p.add_argument('--profile-arm',choices=('baseline','arrays'),default='baseline')
    p.add_argument('--repetitions',type=int,default=8)
    args=p.parse_args()
    if args.out.exists():p.error('Refusing to overwrite receipt')
    if args.repetitions<4:p.error('Use at least four balanced repetitions')
    source=subprocess.check_output(['git','show',f'{BASELINE}:{SOURCE}'],cwd=ROOT)
    modules={a:load_module(source,a) for a in ('baseline','arrays')}
    texts,workload,vectors=prepare(args.fixture,args.embeddings)
    if args.profile:
        profile=cProfile.Profile()
        result=profile.runcall(run,modules[args.profile_arm],texts,workload,vectors,'cache2500',1)
        out=io.StringIO();pstats.Stats(profile,stream=out).strip_dirs().sort_stats('cumulative').print_stats(30)
        args.out.write_text(out.getvalue())
        print(out.getvalue());print(result)
        return
    report={'baseline_commit':BASELINE,'baseline_source_sha256':hashlib.sha256(source).hexdigest(),'candidate_source_sha256':hashlib.sha256((ROOT/SOURCE).read_bytes()).hexdigest(),'python':sys.version,'numpy':np.__version__,
            'platform':platform.platform(),'fixture_hashes':{n:hashlib.sha256((args.fixture/n).read_bytes()).hexdigest() for n in ('inputs.json','inventories.json')},
            'embedding_sha256':hashlib.sha256(args.embeddings.read_bytes()).hexdigest(),'historical_destinations':len(workload[0]),'historical_queries':len(workload[2]),
            'historical_seeds':len(workload[1]),'recorded_unique_vectors':len(vectors),'provider_calls':0,'workloads':{}}
    for kind in ('cache2500','historical-capped'):
        for threads in (1,6):
            name=f'{kind}-threads{threads}';rows=[]
            for repetition in range(args.repetitions):
                order=['baseline','arrays'] if repetition%2==0 else ['arrays','baseline']
                for arm in order:
                    gc.collect()
                    row=run(modules[arm],texts,workload,vectors,kind,threads)
                    row.update(arm=arm,repetition=repetition,order=order);rows.append(row)
                    print(name,arm,repetition,round(row['total_seconds'],4),flush=True)
            assert len({r['output_sha256'] for r in rows})==1,name
            assert len({(r['store_calls'],r['nonempty_store_calls'],r['store_keys'],r['memory_entries']) for r in rows})==1,name
            memory={}
            for arm,module in modules.items():
                gc.collect();memory[arm]=run(module,texts,workload,vectors,kind,threads,True)
            assert {r['output_sha256'] for r in memory.values()}=={rows[0]['output_sha256']}
            report['workloads'][name]={'runs':rows,'memory':memory,'exact_output_match':True,
                'medians':{arm:{m:statistics.median(r[m] for r in rows if r['arm']==arm) for m in ('setup_prime_seconds','calibration_seconds','query_seconds','total_seconds')} for arm in modules}}
    args.out.write_text(json.dumps(report,indent=2)+'\n')
    print('Exact output and call-count equality:',args.out)
if __name__=='__main__':main()
