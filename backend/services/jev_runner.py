"""Worker execution adapter: durable embeddings and bounded URL judgment."""
from __future__ import annotations

import asyncio
import hashlib
import math
from uuid import UUID

from src.redirx.embedding_response import decode_embedding_response
from src.redirx.jev.jev import JevClient, ProviderUnavailable, MODEL, PROMPT_VERSION, encoded
from .jev_database_transport import database_read
from .jev_store import DurableStore


EMBEDDING_MODEL = 'text-embedding-3-small'
EMBEDDING_DIMENSIONS = 1536


def _validated_embedding(vector):
    """A JSON vector must survive the actual float32 retrieval representation."""
    import numpy as np
    if (not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSIONS
            or any(type(value) not in (int, float) for value in vector)):
        raise ValueError('Invalid embedding shape or type')
    # Provider values can be finite as Python doubles but overflow/vanish on
    # conversion. Validate before successful settlement or memory publication.
    with np.errstate(over='ignore', invalid='ignore', under='ignore'):
        converted = np.asarray(vector, dtype=np.float32)
    if not np.isfinite(converted).all() or not np.any(converted):
        raise ValueError('Embedding must remain finite and nonzero in float32')
    return converted


class DurableEmbeddingCache:
    """Original V1 retrieval interface with scoped PostgreSQL persistence."""
    def __init__(self, store, client=None):
        self.store = store
        if client is None:
            from openai import OpenAI
            client = OpenAI(max_retries=0, timeout=45)
        self.client = client
        self.memory = {}

    def embed(self, texts, batch=32):
        import numpy as np
        keys = [hashlib.sha256(encoded({'model':EMBEDDING_MODEL,'text':text})).hexdigest() for text in texts]
        absent=list(dict.fromkeys(key for key in keys if key not in self.memory))
        restored = {}
        absent_set = set(absent)
        try:
            for key,saved in self.store.cache_get_many(absent).items():
                if key not in absent_set:
                    raise ValueError('Unexpected embedding cache key')
                vector=saved.get('embedding')
                _validated_embedding(vector)
                restored[key]=vector
        except (AttributeError, TypeError, ValueError, OverflowError):
            raise ProviderUnavailable('embedding_cache_invalid') from None
        self.memory.update(restored)
        missing = list(dict((key,text) for key,text in zip(keys,texts) if key not in self.memory).items())
        for start in range(0,len(missing),batch):
            chunk = missing[start:start+batch]
            cost = max(1, math.ceil(sum(len(text.encode()) for _,text in chunk)*0.02))
            reservation=self.store.reserve(cost)
            try:
                raw_response = self.client.embeddings.with_raw_response.create(model=EMBEDDING_MODEL,input=[text for _,text in chunk])
                response = decode_embedding_response(raw_response, dimensions=EMBEDDING_DIMENSIONS, allow_base64=True)
                if (response.model != EMBEDDING_MODEL or not isinstance(response.data, list)
                        or len(response.data) != len(chunk)
                        or any(type(item.index) is not int for item in response.data)
                        or sorted(item.index for item in response.data) != list(range(len(chunk)))):
                    raise ValueError('Invalid embedding response identity')
                entries=[]
                for index, ((key,_), item) in enumerate(zip(chunk, sorted(response.data,key=lambda item:item.index))):
                    vector = item.embedding
                    _validated_embedding(vector)
                    entries.append({'key':key,'response':{'embedding':vector}})
                prompt_tokens=response.usage.prompt_tokens
                tokens=response.usage.total_tokens
                if (type(prompt_tokens) is not int or prompt_tokens < 1
                        or type(tokens) is not int or tokens < prompt_tokens):
                    raise ValueError('Invalid embedding usage')
                self.store.cache_put_many(entries,reservation,math.ceil(tokens*0.02))
                self.memory.update({entry['key']:entry['response']['embedding'] for entry in entries})
            except Exception:
                raise ProviderUnavailable('embedding_unavailable') from None
        with np.errstate(over='ignore', invalid='ignore', under='ignore'):
            mat = np.array([self.memory[key] for key in keys],dtype=np.float32)
        # Reuse this existing raw float32 conversion, before normalization.
        # Independent read-only rows avoid repeating Python-list conversion on
        # primed queries without retaining a whole batch through a row view.
        for key, row in zip(keys, mat):
            if isinstance(self.memory[key], list) and np.all(np.isfinite(row)):
                vector = row.copy()
                vector.flags.writeable = False
                self.memory[key] = vector
        # Keep ordinary vectors bit-for-bit on the original float32 path.
        # Extreme but representable nonzero rows need float64 norms: float32
        # squares can overflow or underflow despite every component being valid.
        with np.errstate(over='ignore', invalid='ignore', under='ignore'):
            norms = np.linalg.norm(mat,axis=1,keepdims=True)
            ordinary = np.isfinite(norms[:,0]) & (norms[:,0] >= 1e-9)
            if ordinary.all():
                # The common path needs no row-index copies or extra output
                # allocation; retain the original float32 expression exactly.
                return mat / np.maximum(norms,1e-9)
            result = np.empty_like(mat)
            result[ordinary] = mat[ordinary] / np.maximum(norms[ordinary],1e-9)
            if not ordinary.all():
                extreme = mat[~ordinary].astype(np.float64)
                result[~ordinary] = (extreme / np.linalg.norm(extreme,axis=1,keepdims=True)).astype(np.float32)
        return result


class JevPipelineRunner:
    def __init__(self, client, provider_factory=JevClient, embedding_factory=DurableEmbeddingCache):
        self.client=client; self.provider_factory=provider_factory; self.embedding_factory=embedding_factory

    async def run(self, job, worker_id, progress):
        # A blocking SDK call runs off the event loop so lease heartbeats remain live.
        await asyncio.to_thread(self._run,job,worker_id,progress)

    def _run(self, job, worker_id, progress):
        from src.redirx.jev.data import Page
        from src.redirx.jev.examples import ExampleBank
        from src.redirx.jev.retrieve import Retriever
        from src.redirx.jev.pipeline import Config, map_page
        old_urls=job['old_urls'];new_urls=job['new_urls']
        from .jev_pipeline_service import current_billing_enabled, LIMITS, PAID_LIMITS
        from .jev_store import read_jev_state
        marker=read_jev_state(self.client,job['mcp_run_id'])
        current=marker and marker.get('billing_version')=='redirx_2026_10'
        if current and not current_billing_enabled():
            raise ProviderUnavailable('current_billing_paused')
        limits=PAID_LIMITS if current and marker.get('billing_sku') in ('payg','studio','agency') else LIMITS
        if (not 1 <= len(old_urls) <= limits['old_pages'] or not 1 <= len(new_urls) <= limits['new_pages']
                or sum(len(url.encode()) for url in old_urls+new_urls)>limits['inventory_bytes']):
            raise ValueError('Jev inventory capacity exceeded.')
        store=DurableStore(self.client,job,worker_id)
        state=store.rpc('prepare_jev_pass')
        if state['model'] != MODEL or state['prompt_version'] != PROMPT_VERSION:
            raise ProviderUnavailable('engine_version_unavailable')
        site=store.run_id
        old=[Page(site=site,url=url,id=url) for url in old_urls]
        new=[Page(site=site,url=url) for url in new_urls]
        # Ground truth exists ONLY for explicit confirmations, never imported labels.
        seeds=[Page(site=site,url=row['old_url'],true_new_url=row['new_url']) for row in state['seeds']]
        progress(UUID(str(job['id'])),1,'Jev: prepare URL candidates',3)
        rows=[]
        for offset in range(0,limits['old_pages'],500):
            page=database_read(lambda:self.client.table('jev_proposals').select('old_url,pass').eq('run_id',site).order('old_url_hash').range(offset,offset+499).execute().data)
            rows.extend(page)
            if len(page)<500: break
        done={row['old_url'] for row in rows if row['pass']==state['pass']}
        confirmed={row['old_url'] for row in state['seeds']}
        pending=[page for page in old if page.url not in done and page.url not in confirmed]
        if not pending:
            # Preparation above still fences this attempt and records audited
            # confirmations. Existing proposals remain suggestions for review.
            progress(UUID(str(job['id'])),2,'Jev: no pending URL proposals',3)
            progress(UUID(str(job['id'])),3,'Jev: ready for explicit review',3)
            return
        retriever=Retriever(new,True,embedding_cache=self.embedding_factory(store))
        retriever.prime_queries(old)
        if seeds: retriever.calibrate(seeds)
        bank=ExampleBank(seeds);provider=self.provider_factory(store)
        progress(UUID(str(job['id'])),2,'Jev: propose URL mappings',3)
        def map_one(page):
            examples=bank.nearest(page,3)
            pool,_=retriever.candidates(page,20,examples=examples)
            # Bound unusually long URLs while retaining raw originals. The model
            # wrapper enforces its actual serialized input limit independently.
            while len(pool)>1 and sum(len(p.url.encode()) for p in pool)*2+len(page.url.encode())+sum(len(encoded(e)) for e in examples)>20000:
                pool.pop()
            record=map_page(page,pool,provider,Config(),examples)
            proposal={'target_url':record['decision'],'confidence':record['p_decision'],
                      'confidence_kind':'model_estimate','candidates':record['stage1']['top'],
                      'input_tokens':record['tokens'],'provider_seconds':record['latency'],
                      'model':MODEL,'prompt_version':PROMPT_VERSION,'confirmation_required':True}
            store.rpc('save_jev_proposal',p_pass=state['pass'],p_seed_revision=state['pass_seed_revision'],p_old_url=page.url,p_proposal=proposal)
        from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
        # Shared read-only retrieval/embedding matrix, bounded six-page provider
        # concurrency. Global client pacing remains below the documented limit.
        with ThreadPoolExecutor(max_workers=6,thread_name_prefix='jev-page') as pool:
            queue=iter(pending)
            active={pool.submit(map_one,page) for page in [next(queue,None) for _ in range(6)] if page is not None}
            try:
                while active:
                    completed,active=wait(active,return_when=FIRST_COMPLETED)
                    for future in completed:
                        future.result()
                        page=next(queue,None)
                        if page is not None:active.add(pool.submit(map_one,page))
            except BaseException:
                for future in active:future.cancel()
                raise
        progress(UUID(str(job['id'])),3,'Jev: ready for explicit review',3)

