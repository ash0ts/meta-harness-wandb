# SPDX-License-Identifier: MIT
import copy
import unittest
import analyze_campaign as analysis
from tests.helpers import make_manifest


class PairedAnalysisTests(unittest.TestCase):
    def arm(self, flags):
        return {'small': {i: {'example_sha256': str(i), 'was_correct': flag}
                          for i, flag in enumerate(flags)}}

    def test_equal_arms_have_exact_zero_interval(self):
        arm = self.arm([False, True, True])
        result = analysis.paired(arm, arm, repeats=200)
        self.assertEqual(result['macro_delta_ci95'], [0.0, 0.0])
        self.assertEqual(result['datasets']['small']['both_correct'], 2)

    def test_gains_and_losses_are_paired_not_independent(self):
        result = analysis.paired(self.arm([True, False, False, True]),
                                 self.arm([False, True, False, True]), repeats=200)
        self.assertEqual(result['macro_delta'], 0)
        self.assertEqual([result['datasets']['small'][k] for k in
                          ('gain', 'loss', 'both_correct', 'both_wrong')], [1, 1, 1, 1])

    def test_macro_uses_equal_dataset_weights(self):
        left = self.arm([False])
        right = self.arm([True])
        left['large'] = right['large'] = {i: {'example_sha256': str(i), 'was_correct': False} for i in range(100)}
        result = analysis.paired(left, right, repeats=200)
        self.assertEqual(result['macro_delta'], .5)
        self.assertEqual(result['macro_delta_ci95'], [.5, .5])

    def test_missing_outcome_or_different_identity_stops(self):
        for value in (None, 0):
            with self.assertRaises(ValueError):
                analysis.paired(self.arm([True]), self.arm([value]), repeats=2)
        left, right = self.arm([True]), self.arm([True])
        right['small'][0]['example_sha256'] = 'other'
        with self.assertRaises(ValueError):
            analysis.paired(left, right, repeats=2)

    def test_campaign_names_can_differ_but_solver_and_data_cannot(self):
        left = make_manifest()
        right = copy.deepcopy(left); right['campaign'] = 'b'
        analysis.compatible_campaigns(left, right)
        right['provider']['wire_model'] = 'other'
        with self.assertRaisesRegex(ValueError, 'provider.wire_model'):
            analysis.compatible_campaigns(left, right)
        right = copy.deepcopy(left); right['upstream']['tree_sha256'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'upstream.tree_sha256'):
            analysis.compatible_campaigns(left, right)


if __name__ == '__main__':
    unittest.main()
