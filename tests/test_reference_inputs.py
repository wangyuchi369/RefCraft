"""Reference attribution must not leak irrelevant donors into request-only tests."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from refcraft.evaluation.runner import reference_images


class ReferenceInputTests(unittest.TestCase):
    def setUp(self):
        self.task = {'ref_screenshots': ['data/2/a.png', 'data/2/b.png'],
                     'ref_aspect_source': {'palette': 'A'},
                     'ref_mix': [{'label': 'A', 'handle': 'red page', 'shot_range': [1, 1]},
                                 {'label': 'B', 'handle': 'blue page', 'shot_range': [2, 2]}]}

    def test_request_only_property_omits_references(self):
        self.assertEqual(reference_images(self.task, {'ref_aspect': 'palette', 'axis': 'specify'}), [])

    def test_negative_attribution_includes_wrong_and_assigned_source(self):
        images = reference_images(self.task, {'ref_aspect': 'palette', 'question': 'Do not copy 「B」',
                                              'seed_kind': 'attribution_negative'})
        self.assertEqual([p.name for _, p in images], ['a.png', 'b.png'])

    def test_assigned_source_only(self):
        images = reference_images(self.task, {'ref_aspect': 'palette', 'question': 'Follow the requested palette'})
        self.assertEqual([p.name for _, p in images], ['a.png'])


if __name__ == '__main__':
    unittest.main()
