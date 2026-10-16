"""Small offline audit contracts; these are not model-quality gates."""
import unittest
from scripts.research.retrieval_determinism.audit import adversarial, cold_collision, digest, evaluate


class AuditContracts(unittest.TestCase):
    def test_hash_keeps_raw_case_encoding_order_and_numeric_differences(self):
        base = ['https://new.example/A%2FB', 0.5]
        for changed in (['https://new.example/a%2fb', 0.5], list(reversed(base)),
                        ['https://new.example/A/B', 0.5], ['https://new.example/A%2FB', 0.5000000000000001]):
            self.assertNotEqual(digest(base), digest(changed))

    def test_adversarial_full_outputs_match_all_arms_cache_states_and_threads(self):
        workload, vectors = adversarial()
        hashes = set()
        for arm in ('baseline', 'current'):
            for threads in (1, 6):
                for mode in ('cold', 'primed', 'restored'):
                    with self.subTest(arm=arm, threads=threads, mode=mode):
                        result = evaluate(workload, vectors, arm, threads, mode)
                        hashes.add(result['complete_sha256'])
                        self.assertTrue(result['warm_repeat_exact'])
        self.assertEqual(len(hashes), 1)

    def test_synchronized_cold_read_collision_preserves_float32_output_and_raw_memory(self):
        self.assertEqual(cold_collision('baseline'), cold_collision('current'))
