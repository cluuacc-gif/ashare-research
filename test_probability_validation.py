import datetime as dt
import json
import unittest

import numpy as np
import pandas as pd

import probability_validation as v


def trades(returns, *, stocks_per_date=1):
    records = []
    for index, value in enumerate(returns):
        day = (dt.date(2025, 1, 1)+dt.timedelta(days=index)).isoformat()
        for stock in range(stocks_per_date):
            records.append({'entry_date': day, 'signal_date': day,
                            'symbol': str(stock), 'status': 'closed',
                            'gross_return': value})
    return records


class ProbabilityValidationTests(unittest.TestCase):
    def test_proper_scores_and_fixed_reliability_use_known_labels_only(self):
        frame = pd.DataFrame({'date': ['2025-01-01']*3, 'p': [.8, .2, .99],
                              'label': [1, 0, None]})
        result = v.evaluate_predictions(frame, {'p': 'label'}, baseline_probabilities={'p': .5})
        endpoint = result['endpoints']['p']
        self.assertAlmostEqual(endpoint['brier_score'], .04)
        self.assertAlmostEqual(endpoint['log_loss'], -np.log(.8))
        self.assertEqual(endpoint['n_unknown_labels'], 1)
        self.assertEqual(endpoint['n_scored'], 2)
        self.assertEqual(len(endpoint['reliability_fixed_ten_bins']), 10)
        self.assertAlmostEqual(endpoint['brier_skill_vs_training_baseline'], .84)

    def test_evaluation_does_not_estimate_its_own_baseline(self):
        result = v.evaluate_predictions(pd.DataFrame({'p': [.8], 'label': [1]}), {'p': 'label'})
        self.assertIsNone(result['endpoints']['p']['training_only_baseline_probability'])
        self.assertIsNone(result['endpoints']['p']['brier_skill_vs_training_baseline'])

    def test_equal_date_weighting_prevents_big_date_dominating_score(self):
        frame = pd.DataFrame({'date': ['2025-01-01']*9+['2025-01-02'],
                              'p': [.9]*10, 'label': [1]*9+[0]})
        endpoint = v.evaluate_predictions(frame, {'p': 'label'})['endpoints']['p']
        self.assertAlmostEqual(endpoint['brier_score'], .09)
        self.assertAlmostEqual(endpoint['equal_date_weighted']['brier_score'], .41)
        self.assertEqual(endpoint['entry_date_clusters'], 2)

    def test_impossible_probabilities_and_nonbinary_labels_rejected(self):
        with self.assertRaises(ValueError):
            v.evaluate_predictions(pd.DataFrame({'p': [1.1], 'y': [1]}), {'p': 'y'})
        with self.assertRaises(ValueError):
            v.evaluate_predictions(pd.DataFrame({'p': [.5], 'y': [2]}), {'p': 'y'})
        with self.assertRaises(ValueError):
            v.evaluate_predictions(pd.DataFrame({'p': [.5], 'y': [1]}), {'p': 'y'},
                                    baseline_probabilities={'p': -1})

    def test_zero_one_probabilities_have_finite_log_loss(self):
        frame = pd.DataFrame({'p': [0, 1], 'y': [1, 0]})
        result = v.evaluate_predictions(frame, {'p': 'y'})['endpoints']['p']
        self.assertTrue(np.isfinite(result['log_loss']))
        self.assertEqual(result['reliability_fixed_ten_bins'][-1]['n'], 1)

    def test_risk_coverage_keeps_unknown_labels_and_missing_probabilities(self):
        frame = pd.DataFrame({'date': ['2025-01-01']*4, 'p': [.9, .8, .7, None],
                              'y': [1, None, 0, 1]})
        result = v.risk_coverage(frame, 'p', 'y', thresholds=(.8,))
        point = result['thresholds'][0]
        self.assertEqual(point['coverage'], .5)
        self.assertEqual(point['conservative_hit_rate'], .5)
        self.assertEqual(point['known_label_hit_rate'], 1)
        self.assertEqual(point['n_unknown_labels'], 1)
        self.assertFalse(result['used_for_parameter_selection'])

    def test_unresolved_attempts_cannot_disappear_from_win_denominator(self):
        records = trades([.04, -.02])
        records.append({'entry_date': '2025-01-03', 'status': 'unresolved'})
        records.append({'entry_date': '2025-01-04', 'status': 'no_fill'})
        records.append({'signal_date': '2025-01-05', 'status': 'abstain'})
        result = v.evaluate_trade_metrics(records)
        self.assertEqual(result['n_attempts'], 3)
        self.assertEqual(result['n_cancelled'], 1)
        self.assertAlmostEqual(result['win_rate'], 1/3)
        self.assertEqual(result['closed_win_rate'], .5)
        self.assertEqual(result['n_unresolved'], 1)
        self.assertIsNone(result['mean_gross_return'])
        self.assertIn('unresolved_selected_attempts', result['selection_blockers'])
        self.assertFalse(result['selection_gate'])

    def test_cluster_resampling_keeps_replicated_stocks_together(self):
        returns = [(.04 if index % 3 else -.03) for index in range(50)]
        single = v.evaluate_trade_metrics(trades(returns))
        repeated = v.evaluate_trade_metrics(trades(returns, stocks_per_date=50))
        self.assertEqual(repeated['entry_date_clusters'], 50)
        self.assertEqual(single['date_cluster_win_ci95'], repeated['date_cluster_win_ci95'])
        self.assertEqual(single['moving_block_win_ci95'], repeated['moving_block_win_ci95'])
        np.testing.assert_allclose(single['moving_block_mean_ci95'], repeated['moving_block_mean_ci95'])

    def test_moving_blocks_retain_short_run_dependence(self):
        records = trades([.04]*20+[-.02]*20+[.04]*20+[-.02]*20)
        result = v.evaluate_trade_metrics(records)
        cluster = result['date_cluster_mean_ci95']
        moving = result['moving_block_mean_ci95']
        self.assertGreater(moving[1]-moving[0], cluster[1]-cluster[0])
        self.assertTrue(result['bootstrap']['moving_blocks_require_local_stationarity_and_weak_dependence'])

    def test_more_trials_never_increases_the_selection_lower_bound(self):
        records = trades([.04 if index % 3 else -.02 for index in range(60)])
        one = v.evaluate_trade_metrics(records, trials=1)
        six = v.evaluate_trade_metrics(records, trials=6)
        self.assertLessEqual(six['familywise_lower_mean_return'], one['familywise_lower_mean_return'])
        self.assertAlmostEqual(six['bootstrap']['one_sided_alpha_per_policy'], .05/6)
        self.assertFalse(six['bootstrap']['covers_past_research_trials'])

    def test_many_stocks_on_one_date_do_not_pass_date_minimum(self):
        result = v.evaluate_trade_metrics(trades([.03], stocks_per_date=500))
        self.assertEqual(result['n_closed'], 500)
        self.assertFalse(result['selection_gate'])
        self.assertIn('fewer_than_10_entry_date_clusters', result['selection_blockers'])

    def test_sufficient_positive_research_sample_still_never_certifies_eighty_percent(self):
        result = v.evaluate_trade_metrics(trades([.03]*220), forward_only=True,
                                          actual_execution=True, pit_verified=True)
        self.assertTrue(result['selection_gate'])
        self.assertFalse(result['formal_80_gate'])
        self.assertFalse(result['formal_trade_ready'])
        self.assertIsNone(result['actual_win_rate'])
        self.assertIn('approximate_historical_inference_does_not_certify_80_percent',
                      result['formal_80_blockers'])

    def test_missing_entry_dates_block_activation_even_with_high_win_rate(self):
        records = trades([.03]*25)
        del records[0]['entry_date']
        result = v.evaluate_trade_metrics(records)
        self.assertEqual(result['n_missing_entry_dates'], 1)
        self.assertFalse(result['selection_gate'])
        self.assertIn('missing_entry_dates', result['selection_blockers'])

    def test_invalid_closed_returns_become_unresolved_not_losses_or_omissions(self):
        records = trades([.03]*25)
        records[0]['gross_return'] = float('nan')
        result = v.evaluate_trade_metrics(records)
        self.assertEqual(result['n_attempts'], 25)
        self.assertEqual(result['n_closed'], 24)
        self.assertEqual(result['n_unresolved'], 1)
        self.assertAlmostEqual(result['win_rate'], 24/25)
        self.assertFalse(result['selection_gate'])

    def test_empty_inputs_are_json_serializable_and_do_not_activate(self):
        trades_result = v.evaluate_trade_metrics([])
        predictions = v.evaluate_predictions(pd.DataFrame(), {'p': 'y'})
        self.assertFalse(trades_result['selection_gate'])
        self.assertIsNone(trades_result['win_rate'])
        self.assertEqual(predictions['endpoints']['p']['n_scored'], 0)
        json.dumps({'trade': trades_result, 'predictions': predictions}, allow_nan=False)

    def test_bootstrap_sample_tail_resolution_is_explicit(self):
        result = v.evaluate_trade_metrics(trades([.03]*25), bootstrap_samples=100, trials=6)
        self.assertFalse(result['selection_gate'])
        self.assertIn('insufficient_bootstrap_tail_resolution', result['selection_blockers'])

    def test_configurations_rejected_before_computation(self):
        for arguments in ({'trials': 0}, {'trials': 1.5}, {'bootstrap_samples': -1},
                          {'block_length': 0}, {'trials': True}):
            with self.assertRaises(ValueError):
                v.evaluate_trade_metrics([], **arguments)


if __name__ == '__main__':
    unittest.main()
