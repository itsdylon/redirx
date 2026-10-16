"""Synthetic provider replies; no provider calls or database credentials."""
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import unittest
import base64
import json
import copy
import contextlib
import io
import warnings

import numpy as np

from backend.services.jev_runner import DurableEmbeddingCache
from src.redirx.jev.jev import ProviderUnavailable
from backend.tests.test_jev_embedding_memory import Store, key, normalized, raw_response

MODEL = 'text-embedding-3-small'


def response(vectors, **changes):
    values = dict(model=MODEL, data=[NS(index=i, embedding=v) for i,v in enumerate(vectors)],
                  usage=NS(prompt_tokens=1, total_tokens=1))
    values.update(changes)
    return raw_response(NS(**values))


class EmbeddingProviderBoundaries(unittest.TestCase):
    def setUp(self):
        self.vector = [float(i % 7 - 3) / 7 for i in range(1536)]

    def reject(self, reply):
        store = Store()
        sdk = Mock()
        sdk.embeddings.with_raw_response.create.return_value = reply
        cache = DurableEmbeddingCache(store, sdk)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with self.assertRaisesRegex(ProviderUnavailable, 'embedding_unavailable'):
                cache.embed(['a'])
        self.assertEqual(store.writes, [])
        self.assertEqual(cache.memory, {})
        self.assertEqual(len(store.reservations), 1)
        sdk.embeddings.with_raw_response.create.assert_called_once()

    def test_identity_batch_indices_and_usage_fail_before_settlement(self):
        replies = {
            'wrong model': response([self.vector], model='different-model'),
            'missing model': response([self.vector], model=None),
            'empty success': response([]),
            'missing index': response([], data=[NS(embedding=self.vector)]),
            'bool index': response([], data=[NS(index=False, embedding=self.vector)]),
            'float index': response([], data=[NS(index=0.0, embedding=self.vector)]),
            'negative index': response([], data=[NS(index=-1, embedding=self.vector)]),
            'missing usage': response([self.vector], usage=None),
            'zero usage': response([self.vector], usage=NS(prompt_tokens=0,total_tokens=0)),
            'bool usage': response([self.vector], usage=NS(prompt_tokens=True,total_tokens=1)),
            'reversed usage': response([self.vector], usage=NS(prompt_tokens=2,total_tokens=1)),
        }
        for name, reply in replies.items():
            with self.subTest(name=name): self.reject(reply)

    def test_vector_shape_type_and_float32_limits_fail_before_settlement(self):
        vectors = {
            'bool values': [True] * 1536, 'string values': ['1'] * 1536,
            'short': self.vector[:-1],
            'nan': [float('nan')] + self.vector[1:],
            'infinity': [float('inf')] + self.vector[1:],
            'overflow': [1e300] * 1536, 'zero': [0.] * 1536,
            'underflow to zero': [1e-300] * 1536,
        }
        for name, vector in vectors.items():
            with self.subTest(name=name): self.reject(response([vector]))

    def test_whole_provider_batch_validates_before_any_publication(self):
        for data in ([NS(index=0,embedding=self.vector),NS(index=0,embedding=self.vector)],
                     [NS(index=0,embedding=self.vector),NS(index=1,embedding=[True]*1536)]):
            store=Store(); sdk=Mock();sdk.embeddings.with_raw_response.create.return_value=response([],data=data)
            cache=DurableEmbeddingCache(store,sdk)
            with self.assertRaises(ProviderUnavailable):cache.embed(['a','b'])
            self.assertEqual(cache.memory,{})
            self.assertEqual(store.writes,[])
            self.assertEqual(len(store.reservations),1)
            sdk.embeddings.with_raw_response.create.assert_called_once()

    def test_corrupt_durable_hit_has_no_provider_fallback_or_partial_memory(self):
        for invalid in ([1e300]*1536,[1e-300]*1536,[0.]*1536,[True]*1536):
            with self.subTest(value=invalid[0]):
                store=Store({key('a'):self.vector,key('b'):invalid});sdk=Mock()
                cache=DurableEmbeddingCache(store,sdk)
                with self.assertRaisesRegex(ProviderUnavailable,'embedding_cache_invalid'):cache.embed(['a','b'])
                self.assertEqual(cache.memory,{})
                self.assertEqual(store.reservations,[])
                self.assertEqual(store.writes,[])
                sdk.embeddings.with_raw_response.create.assert_not_called()

    def test_extreme_representable_vectors_return_finite_unit_vectors(self):
        for value in (1e30,1e-30,1e-40):
            for cached in (True,False):
                with self.subTest(value=value,cached=cached):
                    vector=[value]*1536
                    store=Store({key('a'):vector} if cached else {})
                    sdk=Mock();sdk.embeddings.with_raw_response.create.return_value=response([vector])
                    cache=DurableEmbeddingCache(store,sdk)
                    with np.errstate(all='raise'):
                        result=cache.embed(['a'])
                    self.assertTrue(np.isfinite(result).all())
                    self.assertAlmostEqual(float(np.linalg.norm(result.astype(np.float64))),1.,places=6)
                    np.testing.assert_array_equal(cache.embed(['a']),result)

    def test_out_of_order_valid_response_preserves_duplicates_and_ordinary_bits(self):
        other=[float(i%11-5)/11 for i in range(1536)]
        store=Store();sdk=Mock()
        sdk.embeddings.with_raw_response.create.return_value=response([],data=[NS(index=1,embedding=other),NS(index=0,embedding=self.vector)])
        cache=DurableEmbeddingCache(store,sdk)
        result=cache.embed(['a','b','a'])
        np.testing.assert_array_equal(result,normalized([self.vector,other,self.vector]))
        np.testing.assert_array_equal(cache.embed(['b','a']),normalized([other,self.vector]))
        self.assertEqual(len(store.writes),1)
        self.assertEqual([e['key'] for e in store.writes[0][0]],[key('a'),key('b')])
        self.assertEqual(len(store.reservations),1)
        sdk.embeddings.with_raw_response.create.assert_called_once()

    def test_real_sdk_transport_preserves_validation_and_has_no_retries(self):
        import httpx
        from openai import OpenAI
        for name, model, index, vector, status in (
                ('valid',MODEL,0,self.vector,200),
                ('wrong model','wrong-model',0,self.vector,200),
                ('boolean index',MODEL,False,self.vector,200),
                ('boolean vector',MODEL,0,[True]*1536,200),
                ('valid base64',MODEL,0,base64.b64encode(np.asarray(self.vector,dtype='<f4').tobytes()).decode(),200),
                ('bad base64',MODEL,0,'not base64!',200),
                ('short base64',MODEL,0,base64.b64encode(b'1234').decode(),200),
                ('provider failure',MODEL,0,self.vector,500)):
            with self.subTest(name=name):
                calls=[]
                def handle(request):
                    calls.append(request)
                    self.assertEqual(json.loads(request.content)['encoding_format'],'base64')
                    return httpx.Response(status,json={'object':'list','model':model,
                        'data':[{'object':'embedding','index':index,'embedding':vector}],
                        'usage':{'prompt_tokens':1,'total_tokens':1}})
                with httpx.Client(transport=httpx.MockTransport(handle)) as http:
                    sdk=OpenAI(api_key='fixture-only',max_retries=0,http_client=http)
                    store=Store();cache=DurableEmbeddingCache(store,sdk)
                    if name in ('valid','valid base64'):
                        np.testing.assert_array_equal(cache.embed(['a']),normalized([self.vector]))
                        self.assertEqual(len(store.writes),1)
                    else:
                        with self.assertRaises(ProviderUnavailable):cache.embed(['a'])
                        self.assertEqual(store.writes,[])
                        self.assertEqual(cache.memory,{})
                    self.assertEqual(len(calls),1)
                    self.assertEqual(len(store.reservations),1)

    def test_unknown_cache_commit_never_publishes_memory_or_retries(self):
        # The store can commit then lose its acknowledgment; client state must
        # not pretend rollback or retry the paid request in this invocation.
        store=Store(fail_write=True);sdk=Mock()
        sdk.embeddings.with_raw_response.create.return_value=response([self.vector])
        cache=DurableEmbeddingCache(store,sdk)
        with self.assertRaises(ProviderUnavailable):cache.embed(['a'])
        self.assertEqual(cache.memory,{})
        self.assertEqual(len(store.writes),1)
        self.assertEqual(len(store.reservations),1)
        sdk.embeddings.with_raw_response.create.assert_called_once()

    def test_real_sdk_raw_batch_duplicate_fields_missing_rows_and_forward_metadata(self):
        import httpx
        from openai import OpenAI
        other=[-value for value in self.vector]
        good={'model':MODEL,'object':'list','data':[
            {'index':1,'embedding':other},{'index':0,'embedding':self.vector}],
            'usage':{'prompt_tokens':2,'total_tokens':2}, 'future_metadata':{'ignored':True}}
        cases={'valid shuffled':json.dumps(good),
            'duplicate model':json.dumps(good).replace('"model":', '"model":"wrong", "model":',1),
            'duplicate index field':json.dumps(good).replace('"index": 1','"index": 0, "index": 1',1),
            'duplicate row identity':json.dumps(good).replace('"index": 1','"index": 0',1),
            'missing row':json.dumps({**good,'data':good['data'][:1]}),
            'missing vector':json.dumps({**good,'data':[{'index':0},{'index':1,'embedding':other}]}),
            'nonfinite JSON':json.dumps(good).replace('"prompt_tokens": 2','"prompt_tokens": NaN',1)}
        for name,body in cases.items():
            with self.subTest(name=name):
                calls=[]
                def handle(request):
                    calls.append(request)
                    return httpx.Response(200,content=body,headers={'content-type':'application/json'})
                with httpx.Client(transport=httpx.MockTransport(handle)) as http:
                    sdk=OpenAI(api_key='fixture-only',max_retries=0,http_client=http)
                    store=Store();cache=DurableEmbeddingCache(store,sdk)
                    if name=='valid shuffled':
                        np.testing.assert_array_equal(cache.embed(['a','b','a']),normalized([self.vector,other,self.vector]))
                        self.assertEqual(len(store.writes),1)
                        self.assertNotIn('future_metadata',str(store.writes))
                    else:
                        with self.assertRaises(ProviderUnavailable):cache.embed(['a','b','a'])
                        self.assertEqual(store.writes,[])
                        self.assertEqual(cache.memory,{})
                    self.assertEqual(len(calls),1)
                    self.assertEqual(json.loads(calls[0].content)['input'],['a','b'])
                    self.assertEqual(len(store.reservations),1)


class JudgmentBoundaryCharacterization(unittest.TestCase):
    def test_existing_judgment_guard_never_publishes_malformed_answer_or_usage(self):
        from src.redirx.jev.jev import JevClient, MODEL as JEV_MODEL
        questions={'target':{'type':'choice','criteria':{'c01':'fixture','none':'none'}}}
        valid={'probabilities':{'c01':.75,'none':.25},'confidence':.75}
        cases=[]
        for value in (float('nan'),float('inf'),True,'private-provider-payload',-1.,2.):
            answer=copy.deepcopy(valid);answer['probabilities']['c01']=value
            cases.append((answer,1,JEV_MODEL))
        for answer in (None,[],{'probabilities':['c01','none'],'confidence':.5},
                       {'probabilities':{'unexpected':1},'confidence':1}):
            cases.append((answer,1,JEV_MODEL))
        for tokens in (None,True,-1,float('inf')):cases.append((valid,tokens,JEV_MODEL))
        cases.append((valid,1,'wrong-model'))
        for answer,tokens,model in cases:
            with self.subTest(answer=answer,tokens=tokens,model=model):
                store=Mock();store.cache_get.return_value=None
                sdk=Mock();sdk.system_one.return_value=NS(model=model,
                    usage=NS(input_tokens=tokens,output_tokens=0),
                    answers={'target':NS(model_dump=Mock(return_value=answer))})
                logs=io.StringIO()
                with patch('src.redirx.jev.jev.time.sleep'),contextlib.redirect_stdout(logs):
                    # Structural invalidity may retain a sanitized diagnostic
                    # TypeError/AttributeError; this test only establishes the
                    # existing no-publication/no-retry authority boundary.
                    with self.assertRaises((ProviderUnavailable,TypeError,AttributeError)):
                        JevClient(store,sdk).ask({},questions)
                store.reserve.assert_called_once()
                store.cache_put.assert_not_called()
                sdk.system_one.assert_called_once()
                self.assertNotIn('private-provider-payload',logs.getvalue())


if __name__ == '__main__':unittest.main()
