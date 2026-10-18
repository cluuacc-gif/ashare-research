"""Time-boundary, frozen-budget and model-support checks for the runner."""
import datetime as dt
import unittest

import numpy as np
import pandas as pd

import probability_research as p
from test_price_action_research import candidate


def prediction_inputs(count=1):
    endpoints = {'p_win3': np.full(count, .85), 'p_ge3': np.full(count, .5),
                 'p_ge6': np.full(count, .2), 'p_strength3': np.full(count, .4)}
    return {
        'models': {name: {key: values.copy() for key, values in endpoints.items()} for name in p.MODELS},
        'available': {name: {key: True for key in endpoints} for name in p.MODELS},
        'disagreement': {key: np.full(count, .01) for key in endpoints},
        'feature_clip_fraction': np.zeros(count),
        'feature_missing_fraction': np.zeros(count),
    }


class ProbabilityResearchTests(unittest.TestCase):
    def test_fixed_six_policies_and_tplus1_contract(self):
        self.assertEqual(len(p.POLICIES), 6)
        self.assertFalse(p.PROTOCOL['costs_included'])
        self.assertFalse(p.PROTOCOL['D1_sale_allowed'])
        self.assertFalse(p.PROTOCOL['formal_trade_ready'])
        self.assertEqual(p.PROTOCOL['price_universe'], [5, 100])

    def test_180_date_four_way_split_purges_d3_at_every_boundary(self):
        days = [(dt.date(2025, 1, 1)+dt.timedelta(days=i)).isoformat() for i in range(183)]
        frame = pd.DataFrame({'date': days[:180], 'outcome_date': days[2:182],
                              'day3_date': days[3:183]})
        chunks = p.chronological_split(frame)
        self.assertEqual(list(chunks), ['train', 'calibration', 'selection', 'diagnostic'])
        for left, right in zip(list(chunks)[:-1], list(chunks)[1:]):
            self.assertLess(chunks[left].day3_date.max(), chunks[right].date.min())
        self.assertEqual(len(chunks['train']), 87)
        self.assertEqual(len(chunks['calibration']), 24)
        self.assertEqual(len(chunks['selection']), 24)
        self.assertEqual(len(chunks['diagnostic']), 36)
        with self.assertRaisesRegex(ValueError, '180'):
            p.chronological_split(frame.iloc[:179])

    def test_symbol_date_cap_does_not_depend_on_future_labels(self):
        frame = pd.DataFrame({'date': ['2025-01-01']*12+['2025-01-02']*12,
                              'symbol': [f'{i:06}.SZ' for i in range(12)]*2,
                              'future_return': np.arange(24)})
        first = p.date_cap(frame, maximum=3)
        frame['future_return'] = -np.arange(24)*100
        second = p.date_cap(frame.sample(frac=1, random_state=6), maximum=3)
        pd.testing.assert_frame_equal(first[['date', 'symbol']], second[['date', 'symbol']])
        self.assertEqual(first.groupby('date').size().tolist(), [3, 3])

    def test_wide_price_uses_hundred_share_lots_and_frozen_budget(self):
        source = candidate(buy=80, day1_close=80.5, day2_open=80.5,
                           day2_high=83, day2_low=79, day2_close=82)
        source['close'] = 80
        records = p.simulate(pd.DataFrame([source]))
        trade = records[0]
        self.assertEqual(trade['status'], 'closed')
        self.assertEqual(trade['frozen_order_quantity'], 100)
        self.assertEqual(trade['frozen_buy_limit_cap'], 82)
        self.assertEqual(trade['frozen_capital'], 8200)
        self.assertEqual(trade['buy_cash'], 8000)
        self.assertEqual(trade['cash_after_entry'], 22000)

    def test_cancelled_order_savings_and_replacement_are_not_reallocated(self):
        sources = [candidate(symbol='000001.SZ', buy=7.3),
                   candidate(symbol='000002.SZ', buy=5.1),
                   candidate(symbol='000003.SZ', buy=5.2),
                   candidate(symbol='000004.SZ', buy=5.2)]
        records = p.simulate(pd.DataFrame(sources))
        self.assertEqual(len(records), 3)
        self.assertEqual([q['frozen_order_quantity'] for q in records], [1300]*3)
        self.assertEqual(records[0]['status'], 'no_fill')
        self.assertNotIn('000004.SZ', [q['symbol'] for q in records])

    def test_unknown_one_price_entry_reserves_capacity_for_later_days(self):
        sources = []
        for index in range(3):
            source = candidate(symbol=f'{index:06}.SZ')
            source.update(outcome_status='unresolved', entry_one_price=True,
                          reason='one_price_entry_execution_unresolved')
            sources.append(source)
        sources.append(candidate(signal='2026-01-05', entry='2026-01-06',
                                 exit_='2026-01-07', symbol='999999.SZ'))
        records = p.simulate(pd.DataFrame(sources))
        unknown = [q for q in records if q['status'] == 'unresolved']
        self.assertEqual(len(unknown), 3)
        self.assertAlmostEqual(unknown[0]['reserved_cash'], 1300*7.17)
        self.assertEqual(records[-1]['status'], 'abstain')

    def test_invalid_open_quote_cannot_create_retrospective_cancellation(self):
        source = candidate(buy=7.3)
        source.update(entry_evidence_valid=False, outcome_status='unresolved',
                      reason='invalid_entry_evidence')
        records = p.simulate(pd.DataFrame([source]))
        self.assertEqual(records[0]['status'], 'unresolved')
        self.assertAlmostEqual(records[0]['reserved_cash'], 1300*7.17)

    def test_d2_close_proceeds_not_available_at_d2_open(self):
        sources = [candidate(symbol=f'{i:06}.SZ') for i in range(3)]
        sources.append(candidate(signal='2026-01-02', entry='2026-01-05',
                                 exit_='2026-01-06', symbol='999999.SZ'))
        records = p.simulate(pd.DataFrame(sources))
        self.assertEqual(records[-1]['status'], 'abstain')

    def test_red_d1_and_missing_d3_do_not_remove_d2_loss(self):
        source = candidate(day1_close=6.9, day2_open=6.9, day2_high=7,
                           day2_low=6.7, day2_close=6.8)
        source.update(strength3_label=None, day3_date=None)
        records = p.simulate(pd.DataFrame([source]))
        self.assertEqual(records[0]['status'], 'closed')
        self.assertFalse(records[0]['first_day_green'])
        self.assertIsNone(records[0]['strength3_label'])
        self.assertLess(records[0]['gross_return'], 0)
        self.assertEqual(records[0]['exit_date'], '2026-01-05')

    def test_high_probability_prior_without_model_support_abstains(self):
        inputs = prediction_inputs()
        inputs['available']['hgb']['p_win3'] = False
        scored = p.scored(pd.DataFrame([candidate()]), inputs, 'hgb')
        policy = {'model': 'hgb', 'gate': 'balanced'}
        self.assertFalse(p.policy_mask(scored, policy).iloc[0])
        self.assertEqual(p.simulate(scored, policy)[0]['status'], 'abstain')
        # Another fitted model remains independently eligible.
        scored = p.scored(pd.DataFrame([candidate()]), inputs, 'logistic')
        self.assertTrue(p.policy_mask(scored, {'model': 'logistic', 'gate': 'balanced'}).iloc[0])

    def test_six_percent_separate_policy_support_not_required_for_tp3_entry(self):
        inputs = prediction_inputs()
        inputs['available']['logistic']['p_ge6'] = False
        scored = p.scored(pd.DataFrame([candidate()]), inputs, 'logistic')
        self.assertTrue(p.policy_mask(scored, {'model': 'logistic', 'gate': 'balanced'}).iloc[0])

    def test_out_of_distribution_and_missing_inputs_cannot_pass_probability_gate(self):
        inputs = prediction_inputs(3)
        inputs['feature_clip_fraction'] = np.array([0., .26, 0.])
        inputs['feature_missing_fraction'] = np.array([0., 0., .11])
        scored = p.scored(pd.DataFrame([candidate(symbol=f'{i:06}.SZ') for i in range(3)]), inputs, 'logistic')
        self.assertEqual(p.policy_mask(scored, {'model': 'logistic', 'gate': 'balanced'}).tolist(), [True, False, False])


if __name__ == '__main__':
    unittest.main()
