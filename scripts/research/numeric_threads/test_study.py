import os
import unittest
from unittest.mock import patch
from scripts.research.numeric_threads.study import CONTROLS, child_environment, execute

class NumericStudyTests(unittest.TestCase):
    def test_child_controls_are_explicit_and_parent_unchanged(self):
        with patch.dict(os.environ, {key: '99' for key in CONTROLS}):
            for arm in ('default', '1', '2'):
                env, removed = child_environment(arm)
                self.assertTrue(all(value == '99' for value in removed.values()))
                self.assertEqual(env.get('OPENBLAS_NUM_THREADS'), None if arm == 'default' else arm)
                self.assertTrue(all(key not in env for key in CONTROLS if key != 'OPENBLAS_NUM_THREADS'))
                self.assertEqual(env['PYTHONHASHSEED'], '0')
                self.assertTrue(all(os.environ[key] == '99' for key in CONTROLS))

    def test_actual_runner_uses_frozen_vectors_preserves_raw_urls_and_releases_cache(self):
        import numpy as np
        import hashlib
        from src.redirx.jev.data import Page
        from src.redirx.jev.retrieve import path_embed_text
        from src.redirx.jev.jev import encoded
        old = ['https://old.example/Seed?x=%2F', 'https://old.example/Page?x=%2F']
        new = ['https://new.example/Seed?x=%2F', 'https://new.example/Page?x=%2F']
        job = dict(name='tiny', old_urls=old, new_urls=new,
            seeds=[{'old_url': old[0], 'new_url': new[0]}],
            id='00000000-0000-0000-0000-000000000001',
            mcp_run_id='00000000-0000-0000-0001-000000000001', attempt_count=1)
        vectors = {}
        for i, url in enumerate(old + new):
            text = path_embed_text(Page('', url))
            key = hashlib.sha256(encoded({'model':'text-embedding-3-small', 'text': text})).hexdigest()
            v = np.zeros(1536, dtype=np.float32); v[i] = 1.; vectors[key] = v
        result = execute([job], vectors, validation=True)
        self.assertTrue(result['cache_released'])
        self.assertEqual(result['jobs'][0]['proposals'], 1)
        self.assertEqual(result['jobs'][0]['stub_calls'], 2)
        self.assertEqual(set(result['outputs'][0]['proposals']), {old[1]})
        self.assertEqual(result['outputs'][0]['calibration'][0]['n_seed'], 1)
        self.assertIn(old[1], result['outputs'][0]['rankings'])
        self.assertTrue(result['full_seconds'] > 0)
        with self.assertRaises(KeyError): execute([job], {})

if __name__ == '__main__': unittest.main()
