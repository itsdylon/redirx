"""Decode supported OpenAI raw responses before SDK numeric type coercion.

No SDK/client construction or retries. Callers retain model, dimensions,
normalization, cache and accounting authority.
"""
import base64
import json
from types import SimpleNamespace


def decode_embedding_response(response, *, dimensions, allow_base64=False):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate embedding response field')
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError('Non-finite JSON constant')

    payload = json.loads(response.http_response.content,
                         object_pairs_hook=object_pairs, parse_constant=invalid_constant)
    if (not isinstance(payload, dict) or not isinstance(payload.get('data'), list)
            or not isinstance(payload.get('usage'), dict)):
        raise ValueError('Incomplete embedding response')
    rows = []
    for item in payload['data']:
        if not isinstance(item, dict):
            raise ValueError('Invalid embedding response row')
        vector = item.get('embedding')
        if isinstance(vector, str) and allow_base64:
            import numpy as np
            raw = base64.b64decode(vector, validate=True)
            if len(raw) != dimensions * 4:
                raise ValueError('Invalid encoded embedding dimensions')
            vector = np.frombuffer(raw, dtype='<f4').tolist()
        rows.append(SimpleNamespace(index=item.get('index'), embedding=vector))
    usage = payload['usage']
    return SimpleNamespace(model=payload.get('model'), data=rows,
        usage=SimpleNamespace(prompt_tokens=usage.get('prompt_tokens'), total_tokens=usage.get('total_tokens')))
