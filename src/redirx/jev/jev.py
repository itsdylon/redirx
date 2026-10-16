"""Production adapter for the V1 Jev two-question-pass protocol.

Durable, run-scoped cache replaces the experiment's SQLite cache. The outer
worker owns retries; SDK retries are disabled so every billable attempt first
reserves budget. Provider exceptions never expose request URLs or keys.
"""
from __future__ import annotations
import hashlib
import json
import math
import time
import threading

_RATE_LOCK = threading.Lock()
_NEXT_REQUEST = 0.0

MODEL = 'jev-1.13.0'
PROMPT_VERSION = 'redirx-jev-url-v1'
PRICE_IN_PER_MTOK = 0.042
MAX_REQUEST_BYTES = 32000
MAX_BILLABLE_INPUT_TOKENS = 64000


class ProviderUnavailable(RuntimeError):
    def __init__(self, code='provider_unavailable'):
        self.code = code
        super().__init__(code + ': saved progress can resume with refine_matches; no additional free allowance is consumed.')


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def validate_response(result, questions):
    usage=result.get('usage',{})
    if type(usage.get('input_tokens')) is not int or usage['input_tokens']<0 or result.get('model') != MODEL:
        raise ProviderUnavailable('provider_accounting_unavailable')
    answers=result.get('answers',{})
    if set(answers) != set(questions): raise ProviderUnavailable('provider_response_invalid')
    for key, question in questions.items():
        answer=answers[key]
        if question['type']=='noul':
            value=answer.get('noul')
            if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1:
                raise ProviderUnavailable('provider_response_invalid')
        else:
            expected=set(question['criteria']) if question['type']=='choice' else {str(i) for i in range(len(question['criteria']))}
            probs=answer.get('probabilities',{})
            if set(probs)!=expected or any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in probs.values()) or abs(sum(probs.values())-1)>.02:
                raise ProviderUnavailable('provider_response_invalid')
            for field in (('confidence',) if question['type']=='choice' else ('confidence','score')):
                value=answer.get(field)
                if (type(value) not in (int,float) or not math.isfinite(value)
                        or (field=='confidence' and not 0<=value<=1)):
                    raise ProviderUnavailable('provider_response_invalid')


_DIAGNOSTIC_STAGES = frozenset({'request_prepare','cache_lookup','budget_reservation',
    'pacing','provider_request','response_validation','cache_publish'})
_DIAGNOSTIC_CLASSES = frozenset({'AssertionError','AttributeError','KeyError','NameError',
    'TypeError','ValueError','RuntimeError','TimeoutError','ReadError','ConnectError',
    'RemoteProtocolError','LocalProtocolError','APIError','ValidationError',
    'ProviderUnavailable','TypeSafeError','TypeSafeAPIError','TypeSafeBadRequestError',
    'TypeSafeAuthenticationError','TypeSafePermissionDeniedError','TypeSafeNotFoundError',
    'TypeSafeUnprocessableEntityError','TypeSafeRateLimitError','TypeSafeInternalServerError',
    'TypeSafeAPIConnectionError','TypeSafeAPITimeoutError','TypeSafeAPIResponseValidationError'})
_DIAGNOSTIC_CODES = frozenset({'PGRST202','PGRST203','PGRST301','42501','40001','40P01',
    '57014','P0001','operation_conflict','provider_accounting_discrepancy','invalid_input',
    'provider_unavailable','provider_rate_limited','provider_credit_exhausted',
    'provider_accounting_unavailable','provider_response_invalid','provider_budget_exhausted',
    'request_capacity_exceeded'})


def safe_failure_diagnostic(exc, stage):
    """Finite allowlists only: never format the exception or inspect its payload."""
    name=type(exc).__name__
    status=getattr(exc,'status_code',None)
    if type(status) is not int: status=getattr(exc,'status',None)
    code=getattr(exc,'code',None)
    return {'stage':stage if stage in _DIAGNOSTIC_STAGES else 'unknown',
            'exception':name if name in _DIAGNOSTIC_CLASSES else 'OtherException',
            'status':status if type(status) is int and status in {400,401,402,403,404,408,409,422,429,500,502,503,504} else None,
            'code':code if type(code) is str and code in _DIAGNOSTIC_CODES else None}


def _report_failure(exc, stage):
    print('JEV_DIAGNOSTIC '+json.dumps(safe_failure_diagnostic(exc,stage),sort_keys=True),flush=True)


class JevClient:
    def __init__(self, store, client=None):
        self.store = store
        self.model = MODEL
        self.live_requests = self.cached_requests = self.input_tokens = 0
        if client is None:
            from typesafe_sdk import TypeSafeClient, RetryPolicy
            client = TypeSafeClient(model=MODEL, timeout=45,
                                   retry=RetryPolicy(max_retries=0, timeout=45))
        self.client = client

    def ask(self, state, questions):
        # A request-local trace avoids cross-thread stage attribution.
        trace={'stage':'request_prepare'}
        try:
            return self._ask(state,questions,trace)
        except Exception as exc:
            _report_failure(exc,trace['stage'])
            raise

    def _ask(self, state, questions, trace):
        request = {'m': self.model, 'prompt': PROMPT_VERSION, 's': state, 'q': questions}
        blob = encoded(request)
        if len(blob) > MAX_REQUEST_BYTES:
            raise ProviderUnavailable('request_capacity_exceeded')
        key = hashlib.sha256(blob).hexdigest()
        trace['stage']='cache_lookup'
        hit = self.store.cache_get(key)
        if hit is not None:
            trace['stage']='response_validation'
            validate_response(hit,questions)
            self.cached_requests += 1
            return {**hit, 'cached': True}
        # Reserve the model's documented total-input ceiling, then settle to
        # measured input tokens. Unknown/crashed calls retain this reservation.
        trace['stage']='budget_reservation'
        reservation = self.store.reserve(math.ceil(MAX_BILLABLE_INPUT_TOKENS * PRICE_IN_PER_MTOK))
        trace['stage']='pacing'
        global _NEXT_REQUEST
        with _RATE_LOCK:
            time.sleep(max(0,_NEXT_REQUEST-time.monotonic()))
            _NEXT_REQUEST=time.monotonic()+0.15
        start = time.perf_counter()
        trace['stage']='provider_request'
        try:
            response = self.client.system_one(state, questions, model=self.model)
            result = {'answers': {key: value.model_dump(mode='json') for key, value in response.answers.items()},
                      'usage': {'input_tokens': response.usage.input_tokens,
                                'output_tokens': response.usage.output_tokens or 0},
                      'latency': time.perf_counter() - start, 'model': response.model}
        except Exception as exc:
            _report_failure(exc,'provider_request')
            status = getattr(exc, 'status_code', None)
            code = 'provider_rate_limited' if status == 429 else 'provider_credit_exhausted' if status == 402 else 'provider_unavailable'
            raise ProviderUnavailable(code) from None
        trace['stage']='response_validation'
        validate_response(result,questions)
        trace['stage']='cache_publish'
        self.store.cache_put(key, result, reservation, math.ceil(result['usage']['input_tokens']*PRICE_IN_PER_MTOK))
        self.live_requests += 1
        self.input_tokens += result['usage']['input_tokens']
        return {**result, 'cached': False}
