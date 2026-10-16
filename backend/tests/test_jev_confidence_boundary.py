"""Confidence range at the actual pinned SDK boundary; no provider network calls."""
import math
import os
from unittest.mock import Mock, patch
import unittest

import httpx2
from typesafe_sdk import RetryPolicy, TypeSafeClient
from src.redirx.jev.jev import JevClient, MODEL, ProviderUnavailable, MAX_BILLABLE_INPUT_TOKENS, PRICE_IN_PER_MTOK


def question(kind):
    return {'answer':{'type':kind,'criteria':{'yes':'yes','no':'no'} if kind=='choice' else ['low','high']}}


def response(kind, confidence):
    answer={'type':kind,'confidence':confidence}
    if kind=='choice':answer.update(choice='yes',probabilities={'yes':.75,'no':.25})
    else:answer.update(score=.75,legend={'0':'low','1':'high'},probabilities={'0':.25,'1':.75})
    return {'model':MODEL,'answers':{'answer':answer},'usage':{'input_tokens':120,'output_tokens':0}}


class ConfidenceBoundary(unittest.TestCase):
    def provider(self, body, calls):
        def handle(request):
            calls.append(request)
            return httpx2.Response(200,json=body)
        return TypeSafeClient(api_key='fixture-only',model=MODEL,retry=RetryPolicy(max_retries=0),
                              transport=httpx2.MockTransport(handle))

    def test_actual_sdk_out_of_range_confidence_is_not_cached_or_settled(self):
        for kind in ('choice','score'):
            for confidence in (-1., -1e-10, 1.0000000001, 100.):
                with self.subTest(kind=kind,confidence=confidence):
                    calls=[];store=Mock();store.cache_get.return_value=None
                    client=JevClient(store,self.provider(response(kind,confidence),calls))
                    with patch('src.redirx.jev.jev.time.sleep'),self.assertRaises(ProviderUnavailable) as caught:
                        client.ask({},question(kind))
                    self.assertEqual(caught.exception.code,'provider_response_invalid')
                    self.assertEqual(len(calls),1)
                    store.reserve.assert_called_once_with(math.ceil(MAX_BILLABLE_INPUT_TOKENS*PRICE_IN_PER_MTOK))
                    store.cache_put.assert_not_called()
                    self.assertEqual((client.live_requests,client.cached_requests,client.input_tokens),(0,0,0))

    def test_actual_sdk_accepts_both_confidence_endpoints_unchanged(self):
        for kind in ('choice','score'):
            for confidence in (0.,1.):
                with self.subTest(kind=kind,confidence=confidence):
                    calls=[];store=Mock();store.cache_get.return_value=None
                    client=JevClient(store,self.provider(response(kind,confidence),calls))
                    with patch('src.redirx.jev.jev.time.sleep'):result=client.ask({},question(kind))
                    self.assertEqual(result['answers']['answer']['confidence'],confidence)
                    self.assertEqual(len(calls),1);self.assertFalse(result['cached'])
                    store.cache_put.assert_called_once()
                    self.assertEqual(store.cache_put.call_args.args[3],math.ceil(120*PRICE_IN_PER_MTOK))
                    self.assertEqual((client.live_requests,client.cached_requests,client.input_tokens),(1,0,120))

    def test_corrupt_cached_confidence_has_no_provider_or_reservation_fallback(self):
        for kind in ('choice','score'):
            for confidence in (-1.,1.01,100.,True,'0.5',None,float('nan'),float('inf')):
                with self.subTest(kind=kind,confidence=confidence):
                    store=Mock();store.cache_get.return_value=response(kind,confidence);provider=Mock()
                    client=JevClient(store,provider)
                    with self.assertRaises(ProviderUnavailable) as caught:client.ask({},question(kind))
                    self.assertEqual(caught.exception.code,'provider_response_invalid')
                    provider.system_one.assert_not_called();store.reserve.assert_not_called();store.cache_put.assert_not_called()
                    self.assertEqual((client.live_requests,client.cached_requests,client.input_tokens),(0,0,0))

    def test_valid_cached_endpoints_stay_cache_hits(self):
        for kind in ('choice','score'):
            for confidence in (0,1):
                with self.subTest(kind=kind,confidence=confidence):
                    store=Mock();store.cache_get.return_value=response(kind,confidence);provider=Mock()
                    client=JevClient(store,provider);result=client.ask({},question(kind))
                    self.assertTrue(result['cached']);self.assertEqual(client.cached_requests,1)
                    self.assertEqual(result['answers']['answer']['confidence'],confidence)
                    provider.system_one.assert_not_called();store.reserve.assert_not_called();store.cache_put.assert_not_called()

    def test_one_invalid_answer_prevents_whole_response_publication(self):
        body=response('choice',.5)
        body['answers']['rating']=response('score',100.)['answers']['answer']
        questions={**question('choice'),'rating':question('score')['answer']}
        calls=[];store=Mock();store.cache_get.return_value=None
        client=JevClient(store,self.provider(body,calls))
        with patch('src.redirx.jev.jev.time.sleep'),self.assertRaises(ProviderUnavailable):client.ask({},questions)
        self.assertEqual(len(calls),1);store.reserve.assert_called_once();store.cache_put.assert_not_called()


from backend.tests import test_jev_pipeline as native_fixture


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'),'requires disposable loopback PostgreSQL')
class NativeConfidenceReservation(unittest.TestCase):
    setUpClass=classmethod(native_fixture.NativeJev.setUpClass.__func__)
    cleanup_db=classmethod(native_fixture.NativeJev.cleanup_db.__func__)
    setUp=native_fixture.NativeJev.setUp
    sql=native_fixture.NativeJev.sql
    fixture=native_fixture.NativeJev.fixture
    claim=native_fixture.NativeJev.claim
    start=native_fixture.NativeJev.start
    prepared=native_fixture.NativeJev.prepared

    def test_invalid_confidence_retains_actual_unknown_spend_reservation(self):
        _,run,_,store,_=self.prepared()
        calls=[]
        def handle(request):
            calls.append(request)
            return httpx2.Response(200,json=response('choice',100.))
        provider=TypeSafeClient(api_key='fixture-only',model=MODEL,retry=RetryPolicy(max_retries=0),transport=httpx2.MockTransport(handle))
        with patch('src.redirx.jev.jev.time.sleep'),self.assertRaises(ProviderUnavailable):
            JevClient(store,provider).ask({},question('choice'))
        reserved=math.ceil(MAX_BILLABLE_INPUT_TOKENS*PRICE_IN_PER_MTOK)
        self.assertEqual(len(calls),1)
        self.assertEqual(self.sql('SELECT reserved_micro_usd,actual_micro_usd FROM jev_provider_reservations WHERE run_id=%s',[run['run_id']]),
                         [{'reserved_micro_usd':reserved,'actual_micro_usd':None}])
        self.assertEqual(self.sql('SELECT provider_requests FROM jev_runs WHERE run_id=%s',[run['run_id']])[0]['provider_requests'],1)
        self.assertEqual(self.sql('SELECT requests,reserved_micro_usd FROM jev_provider_daily_budget'),[{'requests':1,'reserved_micro_usd':reserved}])
        self.assertEqual(self.sql('SELECT count(*) n FROM jev_provider_cache WHERE run_id=%s',[run['run_id']])[0]['n'],0)
