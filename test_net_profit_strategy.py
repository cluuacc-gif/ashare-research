"""Synthetic causal and portfolio boundaries, never strategy performance evidence."""
import copy
import datetime as dt
import hashlib
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import net_profit_strategy as model
from strategy_validation import CostConfig, calculate_trade, validate_strategy


def bars(n=330):
    return [{'trade_date': (dt.date(2020, 1, 1) + dt.timedelta(days=i)).isoformat(),
             'open': 6 + i * .001, 'close': 6.02 + i * .001,
             'high': 6.2 + i * .001, 'low': 5.8 + i * .001,
             'volume': 100000 + i, 'amount': 50000000 + i, 'conflict': 0} for i in range(n)]


def portfolio_row(day, symbol='a', probability=.9, status='closed'):
    entry = day + dt.timedelta(days=1); exit_ = day + dt.timedelta(days=2)
    return {'date': day.isoformat(), 'symbol': symbol, 'entry_date': entry.isoformat(),
            'outcome_date': exit_.isoformat(), 'outcome_status': status,
            'reference_buy_price': 6., 'reference_sell_price': 6.5,
            'probability_net_profit': probability, 'reason': 'fixture',
            'median_amount20': 50000000., 'ret1': .01, 'ret5': .03,
            'market_breadth': .6, 'market_mean_return': .01}


class NetFeatureTests(unittest.TestCase):
    def test_future_price_and_amount_cannot_change_prior_inputs(self):
        rows = bars(); dates = [r['trade_date'] for r in rows]
        first = model.symbol_features(rows, 'fixture', dates)
        changed = copy.deepcopy(rows); changed[-1].update(open=8, close=8.1, high=8.3, amount=999999999)
        second = model.symbol_features(changed, 'fixture', dates)
        columns = model.price_model.FEATURES + ['median_amount20']
        pd.testing.assert_frame_equal(first.iloc[:-1][columns], second.iloc[:-1][columns])
        self.assertNotEqual(first.iloc[-3].net_return, second.iloc[-3].net_return)

    def test_labels_buy_next_open_and_sell_following_close(self):
        rows = bars(); dates = [r['trade_date'] for r in rows]
        f = model.symbol_features(rows, 'fixture', dates)
        row = f.iloc[0]; pos = dates.index(row.date)
        self.assertEqual(row.entry_date, dates[pos + 1]); self.assertEqual(row.outcome_date, dates[pos + 2])
        self.assertEqual(row.reference_buy_price, rows[pos + 1]['open'])
        self.assertEqual(row.reference_sell_price, rows[pos + 2]['close'])
        direct = calculate_trade(row.reference_buy_price, row.reference_sell_price, int(row.shares), row.entry_date, row.outcome_date)
        self.assertAlmostEqual(row.net_return, direct['net_return'])
        self.assertFalse({'P1', 'P2', 'forward_return'} & set(f.columns))

    def test_missing_future_and_one_price_keep_unresolved_not_negative(self):
        rows = bars(); dates = [r['trade_date'] for r in rows]
        rows[-1].update(open=7, high=7, low=7, close=7)
        f = model.symbol_features(rows, 'fixture', dates)
        self.assertEqual(f.iloc[-3].outcome_status, 'unresolved')
        self.assertTrue(pd.isna(f.iloc[-3].net_profit_label))
        self.assertEqual(f.iloc[-1].outcome_status, 'unresolved')
        missing = model.symbol_features(rows[:-2] + rows[-1:], 'fixture', dates)
        earlier = missing[missing.date == dates[-3]].iloc[0]
        self.assertEqual(earlier.outcome_status, 'unresolved')
        self.assertTrue(pd.isna(earlier.net_return))

    def test_budget_includes_costs_and_whole_lots(self):
        for price in (5, 6.5, 10, 10000):
            shares = model.shares_within_budget(price, CostConfig())
            self.assertEqual(shares % 100, 0)
            if shares:
                trade = calculate_trade(price, price, shares, '2026-10-15', '2026-10-16')
                self.assertLessEqual(trade['buy_cash'], model.BUDGET)

    def test_missing_exit_retains_known_entry_cash_and_shares(self):
        entry = bars(1)[0]
        result = model.outcome('2026-10-13', 'fixture', '2026-10-14',
                               '2026-10-15', entry, None)
        self.assertEqual(result['status'], 'unresolved')
        self.assertEqual(result['reference_buy_price'], entry['open'])
        self.assertGreater(result['shares'], 0)
        self.assertGreater(result['buy_cash'], 0)
        self.assertFalse(result['cash_reservation_assumption'])
        self.assertIsNone(result['net_pnl'])

    def test_shares_respect_remaining_cash_including_minimum_fee(self):
        shares = model.shares_within_budget(6., CostConfig(), budget=1000.)
        trade = calculate_trade(6., 6., shares, '2026-10-15', '2026-10-16')
        self.assertEqual(shares, 100)
        self.assertLessEqual(trade['buy_cash'], 1000.)
        self.assertEqual(model.shares_within_budget(6., CostConfig(), budget=600.), 0)

    def test_definition_identity_stable_but_sensitive_to_execution_costs(self):
        first = model.strategy_definition_sha256()
        self.assertEqual(first, model.strategy_definition_sha256(CostConfig()))
        self.assertNotEqual(first, model.strategy_definition_sha256(CostConfig(slippage_bps=20)))
        self.assertEqual(model.PROTOCOL['estimator_parameters'], model.MODEL_PARAMETERS)

    def test_protocol_identity_matches_exact_written_bytes(self):
        protocol = {**model.PROTOCOL, 'costs': CostConfig().to_dict(),
                    'historical_development_cutoff': '2026-10-16',
                    'frozen_before_new_forward_observations': '2026-10-17T21:00:00+08:00'}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'protocol.json'
            model._write(path, protocol)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                             hashlib.sha256(model._json_bytes(protocol)).hexdigest())

    def test_prediction_timing_uses_next_exchange_session_and_strict_open(self):
        early = model.prediction_timing('2026-10-16', '2026-10-19T09:29:59+08:00')
        self.assertEqual(early['target_trade_date'], '2026-10-19')
        self.assertTrue(early['generated_before_target_open'])
        self.assertFalse(model.prediction_timing('2026-10-16',
                         '2026-10-19T09:30:00+08:00')['generated_before_target_open'])


class SplitAndPortfolioTests(unittest.TestCase):
    def test_four_segments_purge_trade_exit_boundaries(self):
        start = dt.date(2020, 1, 1)
        frame = pd.DataFrame([portfolio_row(start + dt.timedelta(days=i), str(s)) for i in range(300) for s in range(5)])
        parts = model.split(frame)
        self.assertEqual(list(parts), ['train', 'calibration', 'selection', 'diagnostic'])
        for left, right in zip(list(parts)[:-1], list(parts)[1:]):
            self.assertLess(parts[left].outcome_date.max(), parts[right].date.min())
        self.assertLess(sum(len(f) for f in parts.values()), len(frame))

    def test_exit_at_close_occupies_capital_at_same_day_open(self):
        start = dt.date(2020, 1, 1)
        rows = [portfolio_row(start + dt.timedelta(days=i), f'{i}-{s}') for i in range(3) for s in range(4)]
        records = model.simulate(pd.DataFrame(rows), .8)
        selected = [r for r in records if r['status'] != 'abstain']
        self.assertEqual(sum(r['signal_date'] == start.isoformat() for r in selected), 3)
        self.assertEqual(sum(r['signal_date'] == (start + dt.timedelta(days=1)).isoformat() for r in selected), 0)
        self.assertEqual(sum(r['signal_date'] == (start + dt.timedelta(days=2)).isoformat() for r in selected), 3)

    def test_unknown_exit_reserves_capacity_and_stays_win_denominator(self):
        start = dt.date(2020, 1, 1)
        rows = [portfolio_row(start, f'unknown-{s}', status='unresolved') for s in range(3)]
        rows += [portfolio_row(start + dt.timedelta(days=5), 'later')]
        records = model.simulate(pd.DataFrame(rows), .8)
        report = validate_strategy(records, bootstrap_samples=1000)
        self.assertEqual(report['n_attempted_trades'], 3)
        self.assertEqual(report['n_unresolved'], 3)
        self.assertEqual(report['win_rate'], 0)
        self.assertFalse(report['claim_80_allowed'])

    def test_losses_reduce_cash_and_cannot_be_replaced_by_new_capital(self):
        start = dt.date(2020, 1, 1)
        first = [portfolio_row(start, f'loss-{s}') for s in range(3)]
        for row in first:
            row['reference_sell_price'] = .01
        later = [portfolio_row(start + dt.timedelta(days=2), f'later-{s}') for s in range(3)]
        records = model.simulate(pd.DataFrame(first + later), .8)
        trades = [r for r in records if r['status'] == 'closed']
        original = [r for r in trades if r['symbol'].startswith('loss-')]
        following = [r for r in trades if r['symbol'].startswith('later-')]
        self.assertEqual(len(original), 3)
        available = model.INITIAL_CAPITAL + sum(r['net_pnl'] for r in original)
        self.assertLess(available, 2000.)
        self.assertLessEqual(sum(r['buy_cash'] for r in following), available + 1e-7)
        self.assertTrue(all(r['cash_after_entry'] >= -1e-7 for r in trades))
        self.assertTrue(all(r['shares'] < original[0]['shares'] for r in following))

    def test_future_profit_cannot_finance_same_exit_day_open(self):
        start = dt.date(2020, 1, 1)
        first = [portfolio_row(start, f'profit-{s}') for s in range(3)]
        for row in first:
            row['reference_sell_price'] = 100.
        same_exit_open = [portfolio_row(start + dt.timedelta(days=1), 'too-early')]
        records = model.simulate(pd.DataFrame(first + same_exit_open), .8)
        self.assertFalse(any(r.get('symbol') == 'too-early' for r in records))

    def test_unresolved_exit_keeps_actual_reference_entry_cost_reserved(self):
        start = dt.date(2020, 1, 1)
        row = portfolio_row(start, status='unresolved')
        record = model.simulate(pd.DataFrame([row]), .8)[0]
        trade = calculate_trade(6., 6., record['shares'], row['entry_date'], row['outcome_date'])
        self.assertAlmostEqual(record['reserved_cash'], trade['buy_cash'])
        self.assertAlmostEqual(record['cash_after_entry'], model.INITIAL_CAPITAL - trade['buy_cash'])
        self.assertFalse(record['cash_reservation_assumption'])
        self.assertIsNone(record['net_pnl'])

    def test_unknown_entry_cash_reserve_is_explicitly_an_assumption(self):
        row = portfolio_row(dt.date(2020, 1, 1), status='unresolved')
        row['reference_buy_price'] = None
        record = model.simulate(pd.DataFrame([row]), .8)[0]
        self.assertTrue(record['cash_reservation_assumption'])
        self.assertIsNone(record['buy_cash'])
        self.assertEqual(record['reserved_cash'], model.BUDGET)
        self.assertEqual(record['cash_after_entry'], model.INITIAL_CAPITAL - model.BUDGET)

    def test_no_high_probability_signal_is_cash_not_forced_top_three(self):
        row = portfolio_row(dt.date(2020, 1, 1), probability=.6)
        records = model.simulate(pd.DataFrame([row]), .8)
        self.assertEqual([r['status'] for r in records], ['abstain'])

    def test_more_slippage_never_improves_same_fixed_trade(self):
        row = portfolio_row(dt.date(2020, 1, 1))
        frame = pd.DataFrame([row])
        normal = model.simulate(frame, .8)[0]
        stress = model.simulate(frame, .8, costs=CostConfig(slippage_bps=20))[0]
        self.assertLess(stress['net_return'], normal['net_return'])

    def test_small_lucky_sample_cannot_activate_policy(self):
        rows = [portfolio_row(dt.date(2020, 1, 1) + dt.timedelta(days=i * 3), str(i)) for i in range(20)]
        report = validate_strategy(model.simulate(pd.DataFrame(rows), .8), bootstrap_samples=1000)
        self.assertEqual(report['win_rate'], 1)
        self.assertFalse(model.policy_eligible(report))


if __name__ == '__main__':
    unittest.main()
