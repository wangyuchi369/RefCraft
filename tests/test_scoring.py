"""Regression checks for fixed populations, unknowns, and frozen reference groups."""
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from refcraft.evaluation.scoring import task_score


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.defs = {
            'checklists': {'2': [
                {'id': 1, 'importance': 'high', 'depth': 'correctness'},
                {'id': 'fine', 'importance': 'low', 'depth': 'precision'},
                {'id': 3, 'importance': 'critical', 'depth': 'existence'}]},
            'cores': {'2': {'outcomes': [{'key': 'save', 'test_cases': ['invalid', 'valid']}]}},
            'references': {'2': [
                {'items': [{'id': 1, 'depth': 'correctness'}]},
                {'items': [{'id': 'fine', 'depth': 'precision'}, {'id': 3, 'depth': 'existence'}]}]},
        }
        self.record = {'items': [{'id': 1, 'score': 2, 'status': 'assessed'},
                                  {'id': 'fine', 'score': 0, 'status': 'assessed'},
                                  {'id': 3, 'score': 0, 'status': 'assessed'}],
                       'cases': [{'outcome_key': 'save', 'case_index': 0, 'status': 'pass'},
                                 {'outcome_key': 'save', 'case_index': 1, 'status': 'pass'}]}

    def test_reference_group_mean_is_not_flat_item_mean(self):
        result = task_score(2, self.record, self.defs)
        self.assertEqual(result['R']['score'], 50)
        self.assertEqual(result['B']['rational'][0], '100/3')
        self.assertAlmostEqual(result['Overall']['score'], 100 / 3)

    def test_unknown_keeps_denominator(self):
        self.record['items'][0].update(status='unresolved', score=None)
        result = task_score(2, self.record, self.defs)
        self.assertIsNone(result['Overall']['score'])
        self.assertEqual(result['Overall']['rational'], ['0', '100/3'])

    def test_known_zero_can_bound_unknown_nonbinding_component(self):
        self.record['items'][0].update(status='unresolved', score=None)
        for case in self.record['cases']:
            case['status'] = 'fail'
        self.assertEqual(task_score(2, self.record, self.defs)['Overall']['score'], 0)

    def test_invalid_populations_and_ratings_fail(self):
        for mutate in [lambda r: r['items'].pop(), lambda r: r['items'].append(r['items'][0]),
                       lambda r: r['items'][0].update(score=3),
                       lambda r: r['items'][0].update(score=True),
                       lambda r: r['cases'].pop()]:
            r = deepcopy(self.record)
            mutate(r)
            with self.assertRaises(ValueError):
                task_score(2, r, self.defs)


if __name__ == '__main__':
    unittest.main()
