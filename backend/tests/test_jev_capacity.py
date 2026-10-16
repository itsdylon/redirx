"""500x2000 capacity fixture: real durable adapter, synthetic provider responses.

Measures imported-worker RSS and adapter memory at approved inventory bounds.
Does not measure provider latency, accuracy, network reliability or billed cost.
Set JEV_RUN_CAPACITY_FIXTURE=1 plus local PREFLIGHT_TEST_DATABASE_URL explicitly.
"""
import json
import math
import os
import resource
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from backend.tests.test_jev_pipeline import NativeJev
from backend.tests.test_migration_planning import A
from backend.services.migration_planning_service import MigrationPlanningService
from backend.services.inventory_import_service import InventoryImportService
from backend.services.jev_runner import JevPipelineRunner,DurableEmbeddingCache
from src.redirx.jev.jev import JevClient,MODEL

class FakeOpenAI:
    def __init__(self):self.embeddings=self;self.with_raw_response=self
    def create(self,model,input):
        from backend.tests.test_jev_embedding_memory import raw_response
        tokens=sum(max(1,len(text)//4) for text in input)
        return raw_response(SimpleNamespace(model=model,data=[SimpleNamespace(index=i,embedding=[math.sin(j+len(text)) for j in range(1536)]) for i,text in enumerate(input)],usage=SimpleNamespace(prompt_tokens=tokens,total_tokens=tokens)))

class FakeTypeSafe:
    def system_one(self,state,questions,model):
        answers={};selected=next(iter(state['candidates']))
        for key,q in questions.items():
            if q['type']=='noul':value={'noul':1.}
            elif q['type']=='choice':value={'confidence':.9,'probabilities':{k:1. if k==selected else 0. for k in q['criteria']}}
            else:value={'confidence':.9,'score':3.,'probabilities':{'0':0.,'1':0.,'2':0.,'3':1.}}
            answers[key]=SimpleNamespace(model_dump=lambda mode,v=value:v)
        return SimpleNamespace(model=MODEL,answers=answers,usage=SimpleNamespace(input_tokens=100,output_tokens=0))

@unittest.skipUnless(os.getenv('JEV_RUN_CAPACITY_FIXTURE')=='1','explicit capacity fixture opt-in required')
class Capacity(NativeJev):
    # Run only the capacity method; inherited native tests stay in their own suite.
    def test_capacity(self):
        import backend.worker
        rss0=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        mid=MigrationPlanningService(self.repo).plan(A,{'old_site':'https://old.example','new_site':'https://new.example','idempotency_key':uuid4().hex})['migration_id']
        ids={}
        for side,n in [('old',500),('new',2000)]:
            ids[side]=InventoryImportService(self.repo).import_inventory(A,mid,side,[{'url':f'https://{side}.example/documentation/product-{i%40}/configuration/task-{i}?lang=en','provenance':['csv']} for i in range(n)],uuid4().hex)['inventory_id']
        q=self.quotes.create_quote(A,mid,ids['old'],ids['new'],uuid4().hex)['quote_id']
        run=self.service.start(A,mid,ids['old'],ids['new'],q,uuid4().hex)
        job=self.claim();self.runs.authorize_dispatch(job,'worker-test')
        runner=JevPipelineRunner(self.native,lambda store:JevClient(store,FakeTypeSafe()),lambda store:DurableEmbeddingCache(store,FakeOpenAI()))
        begin=time.monotonic()
        with patch('src.redirx.jev.jev.time.sleep',lambda _:None):runner._run(job,'worker-test',lambda *args:None)
        self.runs.finalize_session(job,'worker-test','completed')
        count=self.sql('SELECT count(*) n FROM url_mappings WHERE session_id=%s',[job['id']])[0]['n']
        self.assertEqual(count,500)
        self.assertEqual(self.sql('SELECT count(*) n FROM webpage_embeddings WHERE session_id=%s',[job['id']])[0]['n'],0)
        scale=1 if sys.platform=='darwin' else 1024
        report={'kind':'synthetic-provider capacity fixture','old_pages':500,'new_pages':2000,'mappings':count,
                'worker_import_peak_rss_bytes':rss0*scale,'process_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*scale,
                'wall_seconds':round(time.monotonic()-begin,2),'concurrency':1,'provider_network':False,'provider_cost_measured':False}
        print('JEV_CAPACITY_RECEIPT '+json.dumps(report))
