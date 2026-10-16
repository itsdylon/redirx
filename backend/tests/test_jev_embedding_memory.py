"""Durable embedding memory representation, with positive stub-only I/O checks."""
import hashlib
import json
import math
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
from backend.services.jev_runner import DurableEmbeddingCache
from src.redirx.jev.jev import ProviderUnavailable, encoded


def key(text):
    return hashlib.sha256(encoded({'model':'text-embedding-3-small','text':text})).hexdigest()


def normalized(vectors):
    mat=np.array(vectors,dtype=np.float32)
    return mat/np.maximum(np.linalg.norm(mat,axis=1,keepdims=True),1e-9)


def raw_response(value):
    return SimpleNamespace(http_response=SimpleNamespace(content=json.dumps(value,
        default=lambda item: vars(item)).encode()))


class Store:
    def __init__(self,saved=None,fail_write=False):
        self.saved=saved or {}
        self.reads=[]
        self.reservations=[]
        self.writes=[]
        self.fail_write=fail_write
    def cache_get_many(self,keys):
        self.reads.append(list(keys))
        return {k:{'embedding':self.saved[k]} for k in keys if k in self.saved}
    def reserve(self,cost):
        self.reservations.append(cost)
        return 'lease-fenced-reservation'
    def cache_put_many(self,entries,reservation,actual):
        self.writes.append((entries,reservation,actual))
        if self.fail_write:raise RuntimeError('lease lost')


class Client:
    def __init__(self,vectors):
        self.vectors=vectors
        self.calls=[]
        self.embeddings=self
        self.with_raw_response=self
    def create(self,model,input):
        self.calls.append((model,list(input)))
        # Provider order can differ from input order; existing index validation
        # and sorting must still associate every persisted vector with its key.
        return raw_response(SimpleNamespace(model='text-embedding-3-small',data=[SimpleNamespace(index=i,embedding=self.vectors[t]) for i,t in reversed(list(enumerate(input)))],usage=SimpleNamespace(prompt_tokens=75,total_tokens=75)))


class EmbeddingMemory(unittest.TestCase):
    def setUp(self):
        self.a=[math.sin(i*.13)+.000000017 for i in range(1536)]
        # Zero vectors are now rejected at the provider/cache boundary. Retain
        # the memory representation assertions with a valid nonzero fixture.
        self.b=[1.0]+[0.0]*1535

    def test_durable_hit_exact_order_dtype_raw_keys_and_independent_outputs(self):
        texts=['path: Café /%2f?Case=A','path: Café /%2F?Case=a']
        store=Store({key(texts[0]):self.a,key(texts[1]):self.b})
        provider=Client({})
        cache=DurableEmbeddingCache(store,provider)
        result=cache.embed([texts[0],texts[1],texts[0]])
        np.testing.assert_array_equal(result,normalized([self.a,self.b,self.a]))
        self.assertEqual(result.dtype,np.float32)
        self.assertEqual(store.reads,[[key(t) for t in texts]])
        self.assertEqual(provider.calls,[])
        self.assertEqual(store.reservations,[])
        self.assertEqual(store.writes,[])
        self.assertEqual(set(cache.memory),{key(t) for t in texts})
        for memory_key, vector in cache.memory.items():
            self.assertIsInstance(vector,np.ndarray)
            self.assertEqual(vector.dtype,np.float32)
            self.assertFalse(vector.flags.writeable)
            self.assertIsNone(vector.base)
            np.testing.assert_array_equal(vector,np.array(store.saved[memory_key],dtype=np.float32))
        result[:]=42
        again=cache.embed([texts[0],texts[1]])
        np.testing.assert_array_equal(again,normalized([self.a,self.b]))
        self.assertEqual(store.reads[-1],[])

    def test_provider_success_persists_lists_before_arrays_with_same_accounting(self):
        store=Store()
        provider=Client({'a':self.a,'b':self.b})
        cache=DurableEmbeddingCache(store,provider)
        result=cache.embed(['a','b','a'])
        np.testing.assert_array_equal(result,normalized([self.a,self.b,self.a]))
        self.assertEqual(provider.calls,[('text-embedding-3-small',['a','b'])])
        self.assertEqual(store.reservations,[1])
        self.assertEqual(len(store.writes),1)
        entries,reservation,actual=store.writes[0]
        self.assertEqual(reservation,'lease-fenced-reservation')
        self.assertEqual(actual,2)
        self.assertEqual([e['key'] for e in entries],[key('a'),key('b')])
        self.assertTrue(all(isinstance(e['response']['embedding'],list) for e in entries))
        self.assertTrue(all(isinstance(v,np.ndarray) and not v.flags.writeable for v in cache.memory.values()))
        cache.embed(['b','a'])
        self.assertEqual(len(provider.calls),1)
        self.assertEqual(len(store.writes),1)

    def test_failed_durable_write_does_not_publish_provider_memory(self):
        store=Store(fail_write=True)
        provider=Client({'a':self.a})
        cache=DurableEmbeddingCache(store,provider)
        with self.assertRaisesRegex(ProviderUnavailable,'embedding_unavailable'):cache.embed(['a'])
        self.assertEqual(cache.memory,{})
        self.assertEqual(len(store.writes),1)
        self.assertEqual(len(store.reservations),1)
        self.assertEqual(len(provider.calls),1)

    def test_invalid_durable_vectors_are_rejected_before_provider_or_arrays(self):
        invalid=[self.a[:-1],tuple(self.a),[True]+self.a[1:],[float('nan')]+self.a[1:],[float('inf')]+self.a[1:],['1']+self.a[1:]]
        for vector in invalid:
            with self.subTest(kind=type(vector),first=vector[0]):
                store=Store({key('bad'):vector})
                provider=Client({})
                cache=DurableEmbeddingCache(store,provider)
                with self.assertRaisesRegex(ProviderUnavailable,'embedding_cache_invalid'):cache.embed(['bad'])
                self.assertEqual(cache.memory,{})
                self.assertEqual(provider.calls,[])
                self.assertEqual(store.reservations,[])

    def test_float32_overflow_is_now_rejected_before_cache_publication(self):
        # Supersedes the earlier representation-only overflow characterization.
        # NaN retrieval output is not an admissible embedding response.
        huge=[1e300]+self.a[1:]
        for provider_path in (False,True):
            for overflow in ('warn','raise'):
                with self.subTest(provider=provider_path,overflow=overflow):
                    store=Store({} if provider_path else {key('valid'):self.a,key('huge'):huge})
                    provider=Client({'valid':self.a,'huge':huge})
                    cache=DurableEmbeddingCache(store,provider)
                    with np.errstate(over=overflow,invalid='raise'):
                        with self.assertRaises(ProviderUnavailable):cache.embed(['valid','huge'])
                    self.assertEqual(cache.memory,{})
                    self.assertEqual(len(store.reads),1)
                    self.assertEqual(len(provider.calls),int(provider_path))
                    self.assertEqual(len(store.reservations),int(provider_path))
                    self.assertEqual(len(store.writes),0)

    def test_tiny_values_retain_original_strict_underflow_behavior(self):
        tiny=[1e-300]+self.a[1:]
        for provider_path in (False,True):
            store=Store({} if provider_path else {key('tiny'):tiny})
            cache=DurableEmbeddingCache(store,Client({'tiny':tiny}))
            with np.errstate(under='raise'):
                expected=normalized([tiny])
                actual=cache.embed(['tiny'])
            np.testing.assert_array_equal(actual,expected)
            self.assertEqual(len(store.writes),int(provider_path))

    def test_six_cold_same_key_durable_hits_are_consistent_without_provider(self):
        barrier=threading.Barrier(6)
        class ConcurrentStore(Store):
            def cache_get_many(self,keys):
                result=super().cache_get_many(keys)
                barrier.wait(timeout=5)
                return result
        store=ConcurrentStore({key('a'):self.a})
        provider=Client({})
        cache=DurableEmbeddingCache(store,provider)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda _:cache.embed(['a','a']),range(6)))
        for result in results:np.testing.assert_array_equal(result,normalized([self.a,self.a]))
        self.assertEqual(store.reads,[[key('a')]]*6)
        self.assertEqual(provider.calls,[])
        self.assertEqual(store.writes,[])
        self.assertEqual(store.reservations,[])
        self.assertFalse(cache.memory[key('a')].flags.writeable)

    def test_six_primed_workers_return_exact_independent_arrays_without_io(self):
        store=Store({key('a'):self.a,key('b'):self.b})
        provider=Client({})
        cache=DurableEmbeddingCache(store,provider)
        cache.embed(['a','b'])
        before={k:v.tobytes() for k,v in cache.memory.items()}
        identities={k:id(v) for k,v in cache.memory.items()}
        barrier=threading.Barrier(6)
        def query(i):
            barrier.wait(timeout=5)
            result=cache.embed(['a','b','a'])
            np.testing.assert_array_equal(result,normalized([self.a,self.b,self.a]))
            result[:]=i
            return threading.get_ident(),result
        with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(query,range(6)))
        self.assertEqual(len({worker for worker,_ in results}),6)
        self.assertEqual({k:v.tobytes() for k,v in cache.memory.items()},before)
        self.assertEqual({k:id(v) for k,v in cache.memory.items()},identities)
        self.assertEqual(sum(bool(keys) for keys in store.reads),1)
        self.assertEqual(len(store.reads),7)
        self.assertEqual(provider.calls,[])
        self.assertEqual(store.writes,[])
        self.assertEqual(store.reservations,[])
        for i,(_,result) in enumerate(results):self.assertTrue(np.all(result==i))

if __name__=='__main__':unittest.main()
