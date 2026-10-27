"""Time-boundary, frozen-budget and model-support checks for the runner."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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


def research_panel(*,qualifying=True):
    """Small complete panel with only past labels and unknown current outcomes."""
    history=[]
    for index in range(240):
        day=dt.date(2025,1,1)+dt.timedelta(days=index)
        source={key:.01 for key in p.FEATURES}
        source.update(candidate(signal=day.isoformat(),
                                entry=(day+dt.timedelta(days=1)).isoformat(),
                                exit_=(day+dt.timedelta(days=2)).isoformat()))
        source.update(day3_date=(day+dt.timedelta(days=3)).isoformat(),
                      policy_label_status='closed',label_profit3=1)
        history.append(source)
    live=dict(history[-1],date='2026-01-15',entry_date=None,outcome_date=None,
              day3_date=None,policy_label_status='unresolved',outcome_status='unresolved',
              **{key:None for key in p.TARGETS.values()})
    if not qualifying:live['market_breadth']=.20
    return pd.DataFrame(history),pd.DataFrame([live]),{'000001.SZ':'Example'},240


class ProbabilityResearchTests(unittest.TestCase):
    def test_original_protocol_hash_is_unchanged(self):
        encoded=json.dumps(p.PROTOCOL,sort_keys=True,separators=(',',':')).encode()
        self.assertEqual(hashlib.sha256(encoded).hexdigest(),
                         '56a512356f8cba4ace57e23b48dc5db1cd012ad0157ed9c852451b51235cc7e1')

    def test_funnel_identifies_individual_and_cumulative_failures_without_labels(self):
        source=research_panel()[1].iloc[0].to_dict()
        failures={'median_amount20':19_999_999,'volatility20':.051,'ret1':.061,
                  'ma20_gap':.151,'close_location':.951,'market_breadth':.299,
                  'market_mean_return':-.021}
        frame=pd.DataFrame([source]+[{**source,key:value} for key,value in failures.items()])
        result=p.input_funnel(frame)
        self.assertEqual(result['input_rows'],8)
        self.assertEqual(result['qualifying_rows'],1)
        self.assertEqual([step['individual_pass'] for step in result['steps']],[7]*7)
        self.assertEqual([step['individual_fail'] for step in result['steps']],[1]*7)
        self.assertEqual([step['cumulative_pass'] for step in result['steps']],[7,6,5,4,3,2,1])
        self.assertEqual([step['rejected_at_step'] for step in result['steps']],[1]*7)
        self.assertEqual(result['qualifying_rows'],int(p.causal_entry_filter(frame).sum()))
        self.assertEqual(result['market_context']['market_breadth'],
                         {'min':.299,'max':.6,'finite_rows':8})
        frame['label_profit3']=1;frame['strength3_label']=0
        frame['entry_evidence_valid']=False;frame['outcome_status']='cancelled'
        self.assertEqual(p.input_funnel(frame),result)

    def test_entry_filter_retains_original_inclusive_boundaries(self):
        source=research_panel()[1].iloc[0].to_dict()
        source.update(median_amount20=20_000_000,volatility20=.05,ret1=-.04,
                      ma20_gap=-.06,close_location=.25,market_breadth=.30,
                      market_mean_return=-.02)
        upper={**source,'ret1':.06,'ma20_gap':.15,'close_location':.95}
        self.assertEqual(p.causal_entry_filter(pd.DataFrame([source,upper])).tolist(),[True,True])

    def test_filtered_empty_live_is_identity_complete_inactive_evidence(self):
        inputs=research_panel(qualifying=False)
        history_before=inputs[0].copy(deep=True);live_before=inputs[1].copy(deep=True)
        with tempfile.TemporaryDirectory() as directory:
            database=Path(directory)/'source.db';database.write_bytes(b'frozen-test-input')
            digest=p.gross._hash(database);output=Path(directory)/'probability'
            with (patch.object(p,'fit_bundle',side_effect=AssertionError('must not fit')),
                  patch.object(p,'predict_bundle',side_effect=AssertionError('must not predict'))):
                summary=p.run(database,'2026-01-15',output,digest,panel_data=inputs)
            self.assertEqual(summary['status'],p.NO_CURRENT_INPUTS)
            self.assertFalse(summary['policy']['active']);self.assertIsNone(summary['policy']['chosen'])
            self.assertFalse(summary['training_performed']);self.assertFalse(summary['selection_performed'])
            self.assertIsNone(summary['actual_win_rate']);self.assertFalse(summary['formal_trade_ready'])
            self.assertEqual(summary['base_trade_date'],'2026-01-15')
            self.assertEqual(summary['target_trade_date'],'2026-01-16')
            self.assertEqual(summary['input_database_sha256'],digest)
            self.assertEqual(summary['current_watchlist'],[])
            self.assertEqual(summary['formal_candidates'],[])
            self.assertEqual(summary['diagnostic'],{})
            self.assertTrue(all(value is False for endpoints in summary['endpoint_availability'].values()
                                for value in endpoints.values()))
            self.assertEqual({path.name for path in output.iterdir()},
                             {'summary.json','protocol.json','input_funnel.json','predictions.json'})
            self.assertEqual(json.loads((output/'summary.json').read_text(encoding='utf-8')),summary)
            self.assertEqual(json.loads((output/'protocol.json').read_text(encoding='utf-8')),p.PROTOCOL)
            evidence=json.loads((output/'input_funnel.json').read_text(encoding='utf-8'))
            self.assertEqual(evidence['input_database_sha256'],digest)
            self.assertEqual(evidence['target_trade_date'],'2026-01-16')
            self.assertEqual(evidence['current']['qualifying_rows'],0)
            breadth=evidence['current']['steps'][5]
            self.assertEqual(breadth['individual_pass'],0);self.assertEqual(breadth['rejected_at_step'],1)
            self.assertEqual(evidence['current']['market_context']['market_breadth']['min'],.2)
            self.assertEqual(json.loads((output/'predictions.json').read_text(encoding='utf-8'))['rows'],[])
        pd.testing.assert_frame_equal(history_before,inputs[0]);pd.testing.assert_frame_equal(live_before,inputs[1])

    def test_qualifying_live_still_runs_the_original_model_path(self):
        inputs=research_panel()
        with tempfile.TemporaryDirectory() as directory:
            database=Path(directory)/'source.db';database.write_bytes(b'frozen-test-input')
            digest=p.gross._hash(database);output=Path(directory)/'probability'
            with (patch.object(p,'fit_bundle',return_value={'metadata':{'fixture':True}}) as fit,
                  patch.object(p,'predict_bundle',side_effect=lambda bundle,frame:prediction_inputs(len(frame))),
                  patch.object(p,'evaluate_predictions',return_value={})):
                summary=p.run(database,'2026-01-15',output,digest,panel_data=inputs)
            fit.assert_called_once()
            self.assertNotIn('status',summary)
            self.assertTrue(summary['training_performed']);self.assertTrue(summary['selection_performed'])
            self.assertEqual(summary['coverage']['causal_live_candidates'],1)
            self.assertEqual(len(summary['selection']),6)
            self.assertTrue((output/'models.joblib').exists())
            predictions=json.loads((output/'predictions.json').read_text(encoding='utf-8'))
            self.assertEqual(len(predictions['rows']),1)
            self.assertEqual(predictions['rows'][0]['p_win3'],.85)
            self.assertEqual(summary['strategy_definition_sha256'],
                             '56a512356f8cba4ace57e23b48dc5db1cd012ad0157ed9c852451b51235cc7e1')

    def test_empty_current_does_not_swallow_bad_panel_or_hash_errors(self):
        for case in ('raw_live_empty','raw_history_empty','nonfinite_live','nonfinite_history',
                     'short_history','wrong_live_date','wrong_hash'):
            with self.subTest(case=case),tempfile.TemporaryDirectory() as directory:
                inputs=list(research_panel(qualifying=False))
                if case=='raw_live_empty':inputs[1]=inputs[1].iloc[:0]
                elif case=='raw_history_empty':inputs[0]=inputs[0].iloc[:0]
                elif case=='nonfinite_live':inputs[1].loc[0,'market_breadth']=np.nan
                elif case=='nonfinite_history':inputs[0].loc[0,'ret1']=np.inf
                elif case=='short_history':inputs[0]=inputs[0].iloc[:179]
                elif case=='wrong_live_date':inputs[1].loc[0,'date']='2026-01-14'
                database=Path(directory)/'source.db';database.write_bytes(b'frozen-test-input')
                digest='0'*64 if case=='wrong_hash' else p.gross._hash(database)
                output=Path(directory)/'probability'
                with self.assertRaises(ValueError):p.run(database,'2026-01-15',output,digest,panel_data=inputs)
                self.assertFalse(output.exists())

    def test_training_exception_and_changed_source_remain_hard_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            database=Path(directory)/'source.db';database.write_bytes(b'frozen-test-input')
            digest=p.gross._hash(database);output=Path(directory)/'probability'
            with patch.object(p,'fit_bundle',side_effect=RuntimeError('training hard failure')):
                with self.assertRaisesRegex(RuntimeError,'training hard failure'):
                    p.run(database,'2026-01-15',output,digest,panel_data=research_panel())
            self.assertFalse(output.exists())
            with patch.object(p.gross,'_hash',side_effect=[digest,'f'*64]):
                with self.assertRaisesRegex(ValueError,'database changed'):
                    p.run(database,'2026-01-15',output,digest,panel_data=research_panel(qualifying=False))
            self.assertFalse(output.exists())

    def test_shared_pa_stage_finishes_with_historical_diagnostics_and_empty_current(self):
        import pa_trade_research as pa
        from test_pa_trade_engine import breakout
        inputs=research_panel(qualifying=False)
        # The caller owns enriched columns that probability.run must not drop.
        for frame in inputs[:2]:
            for key,value in breakout().items():
                if key not in frame:frame[key]=value
        with tempfile.TemporaryDirectory() as directory:
            database=Path(directory)/'source.db';database.write_bytes(b'frozen-test-input')
            digest=p.gross._hash(database);output=Path(directory)/'pa'
            probability_output=Path(directory)/'probability'
            with patch.object(pa,'panel',return_value=inputs):
                summary=pa.run(database,'2026-01-15',output,digest,probability_output=probability_output)
            self.assertEqual(summary['coverage']['live_candidates'],0)
            self.assertEqual(summary['current_watchlist'],[])
            self.assertEqual(summary['setup_observations'],[])
            self.assertGreater(summary['diagnostic']['baseline_fixed_3pct_2pct']['n_attempts'],0)
            self.assertTrue((output/'summary.json').exists())
            study=json.loads((probability_output/'summary.json').read_text(encoding='utf-8'))
            self.assertEqual(study['status'],p.NO_CURRENT_INPUTS)
            self.assertIn('previous_close',inputs[0])

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
