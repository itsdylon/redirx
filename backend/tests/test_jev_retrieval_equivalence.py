"""Exact arithmetic regressions for fixed-corpus retrieval; no model quality gate."""
import math
import random
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import numpy as np

from src.redirx.jev.data import Page
from src.redirx.jev.retrieve import BM25, Retriever


def reference_scores(docs, query, k1=1.5, b=.75):
    """Original scalar formula, independently rebuilt from documents."""
    n = len(docs)
    dl = np.array([len(d) for d in docs], dtype=float)
    avgdl = float(dl.mean()) if n else 1.0
    out = np.zeros(n)
    frequencies = [Counter(doc) for doc in docs]
    for token, qtf in Counter(query).items():
        df = sum(token in counts for counts in frequencies)
        if not df:
            continue
        idf = math.log(1.0 + (n - df + .5) / (df + .5))
        for i, counts in enumerate(frequencies):
            tf = counts[token]
            if tf:
                denom = tf + k1 * (1 - b + b * dl[i] / avgdl)
                out[i] += idf * tf * (k1 + 1) / denom * min(qtf, 2)
    return out


class RetrievalEquivalence(unittest.TestCase):
    def test_random_scores_are_bit_identical_including_query_order(self):
        rng = random.Random(55102)
        words = ['common', 'rare', 'Case', '%2F', 'é', '日本語', 'one', 'two']
        for _ in range(80):
            docs = [rng.choices(words, k=rng.randrange(20)) for _ in range(rng.randrange(60))]
            query = rng.choices(words + ['absent'], k=rng.randrange(30))
            k1, b = rng.choice([.1, 1.5, 3.0]), rng.choice([0., .75, 1.])
            index = BM25(docs, k1, b)
            for q in (query, query[::-1], ['absent'], [], ['common'] * 40):
                expected = reference_scores(docs, q, k1, b)
                actual = index.scores(q)
                np.testing.assert_array_equal(actual, expected, strict=True)
                self.assertEqual(actual.tobytes(), expected.tobytes())

    def test_empty_and_all_empty_documents(self):
        for docs in ([], [[]], [[], [], []]):
            np.testing.assert_array_equal(BM25(docs).scores(['missing']), np.zeros(len(docs)))

    def test_repeated_and_concurrent_queries_do_not_mutate_index(self):
        docs = [['x', 'x', 'y'], ['y'], [], ['unique']]
        index = BM25(docs)
        queries = [['x'], ['y', 'x', 'y'], ['unique'], ['missing']] * 50
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(index.scores, queries))
        for q, result in zip(queries, results):
            np.testing.assert_array_equal(result, reference_scores(docs, q))
        results[0][:] = 99
        np.testing.assert_array_equal(index.scores(['x']), reference_scores(docs, ['x']))

    def test_calibration_calls_all_voters_once_per_eligible_seed(self):
        class Embeddings:
            def embed(self, texts):
                return np.ones((len(texts), 1))
        pages = [Page('a', '/Case%2FPath'), Page('a', '/日本語'), Page('a', '/other')]
        retriever = Retriever(pages, True, embedding_cache=Embeddings())
        seed = [Page('a', '/old-a', true_new_url=pages[0].url),
                Page('a', '/old-b', true_new_url=pages[1].url),
                Page('missing', '/old', true_new_url='/target'), Page('a', '/unconfirmed')]
        votes = [{'bm25_path': [0], 'dense_path': [2]},
                 {'bm25_path': [1], 'dense_path': [1]}]
        with patch.object(retriever, 'voter_lists', side_effect=votes) as voter_lists:
            result = retriever.calibrate(seed, k=1)
        self.assertEqual(voter_lists.call_count, 2)
        self.assertEqual(result, {'n_seed': 2, 'recall_at_k': {'bm25_path': 1., 'dense_path': .5},
                                  'weights': {'bm25_path': 1., 'dense_path': 0.}})
        self.assertEqual([p.url for p in pages], ['/Case%2FPath', '/日本語', '/other'])


if __name__ == '__main__':
    unittest.main()
