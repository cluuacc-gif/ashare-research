import copy
import datetime as dt
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import gross_swing_strategy as g


def bar(day, open_=7, high=7.3, low=6.9, close=7.2):
    return {'trade_date': day, 'open': open_, 'high': high, 'low': low, 'close': close,
            'volume': 1000000, 'amount': 30000000, 'conflict': 0}


def row(signal='2026-01-01', entry='2026-01-02', exit_='2026-01-05', symbol='000001.SZ',
        buy=7, day1_close=7.1, day2_open=7.1, day2_high=7.5, day2_low=7, day2_close=7.3):
    result = {'date': signal, 'symbol': symbol, 'close': 7, 'ret1': .01, 'ret20': .03,
              'ma20_gap': .01, 'candle_body': .01, 'range': .05, 'close_location': .7,
              'median_amount20': 30000000, 'volatility20': .02,
              'market_breadth': .6, 'market_mean_return': .01,
              'p_profit': .7, 'p_ge3': .3, 'p_ge6': .1, 'p_day1_green': .6}
    result.update(g.label_outcome(signal, symbol, entry, exit_,
        bar(entry, buy, max(buy, day1_close) + .1, min(buy, day1_close) - .1, day1_close),
        bar(exit_, day2_open, day2_high, day2_low, day2_close), 7))
    return result


class GrossSwingTests(unittest.TestCase):
    def test_red_first_day_stays_in_primary_denominator(self):
        rows = [row(day1_close=6.9, day2_open=6.9, day2_high=7, day2_low=6.7, day2_close=6.8)]
        records = g.simulate(pd.DataFrame(rows))
        self.assertEqual(records[0]['status'], 'closed')
        report = g.metrics(records, bootstrap_samples=100)
        self.assertEqual(report['n_attempts'], 1)
        self.assertEqual(report['n_losses'], 1)
        self.assertEqual(report['first_day_not_green_conditional']['n'], 1)
        self.assertEqual(report['first_day_green_conditional']['n'], 0)

    def test_same_bar_target_stop_conservative_and_upper_bound(self):
        result = g.exit_outcome(7, 7, 7.5, 6.7, 7.2, .03, .02)
        self.assertTrue(result['ambiguous_same_bar'])
        self.assertAlmostEqual(result['reference_sell_price'], 6.86)
        self.assertAlmostEqual(result['optimistic_sell_price'], 7.21)
        self.assertLess(result['gross_return'], 0)
        self.assertGreaterEqual(result['optimistic_gross_return'], .03 - 1e-12)

    def test_gap_stop_uses_open_not_stop(self):
        result = g.exit_outcome(7, 6.5, 7.4, 6.4, 7.2, .03, .02)
        self.assertEqual(result['reference_sell_price'], 6.5)
        self.assertFalse(result['ambiguous_same_bar'])

    def test_gap_profit_uses_open(self):
        result = g.exit_outcome(7, 7.5, 7.7, 7, 7.3, .03, .02)
        self.assertEqual(result['reference_sell_price'], 7.5)

    def test_targets_round_up_so_three_percent_is_really_three(self):
        result = g.exit_outcome(7.01, 7.02, 7.5, 7, 7.1, .03, .02)
        self.assertEqual(result['target_price'], 7.23)
        self.assertGreaterEqual(result['gross_return'], .03)

    def test_one_price_is_unresolved_not_retroactive_no_fill(self):
        candidate = row()
        candidate.update(outcome_status='unresolved', reason='one_price_entry_execution_unresolved',
                         entry_one_price=True)
        records = g.simulate(pd.DataFrame([candidate]))
        self.assertEqual(records[0]['status'], 'unresolved')
        self.assertAlmostEqual(records[0]['reserved_cash'], 1300 * 7.21)
        self.assertTrue(records[0]['cash_reservation_assumption'])
        report = g.metrics(records)
        self.assertEqual(report['win_rate'], 0)
        self.assertEqual(report['n_unresolved'], 1)
        self.assertIsNone(report['mean_gross_return'])

    def test_day1_label_does_not_require_day2_evidence(self):
        candidate = g.label_outcome('2026-01-01', '000001.SZ', '2026-01-02',
                                   '2026-01-05', bar('2026-01-02'), None, 7)
        self.assertEqual(candidate['label_day1_green'], 1)
        self.assertIsNone(candidate['label_profit'])
        self.assertEqual(candidate['outcome_status'], 'unresolved')

    def test_known_open_gap_cancellation_retained(self):
        candidate = row(buy=7.3)
        records = g.simulate(pd.DataFrame([candidate]))
        self.assertEqual(records[0]['status'], 'no_fill')
        report = g.metrics(records)
        self.assertEqual(report['n_signals'], 1)
        self.assertEqual(report['n_cancelled'], 1)
        self.assertEqual(report['n_attempts'], 0)
        self.assertIsNone(report['win_rate'])

    def test_unknown_positions_block_future_plans(self):
        candidates = []
        for k in range(3):
            candidate = row(symbol=f'{k:06}.SZ')
            candidate.update(outcome_status='unresolved', reason='missing_exit')
            candidates.append(candidate)
        candidates.append(row(signal='2026-01-05', entry='2026-01-06', exit_='2026-01-07', symbol='999999.SZ'))
        records = g.simulate(pd.DataFrame(candidates))
        self.assertEqual(sum(x['status'] == 'unresolved' for x in records), 3)
        self.assertEqual(records[-1]['status'], 'abstain')
        self.assertGreaterEqual(min(x['cash_after_entry'] for x in records if 'cash_after_entry' in x), 0)

    def test_same_session_exit_cash_cannot_fund_open(self):
        candidates = [row(symbol=f'{k:06}.SZ') for k in range(3)]
        candidates.append(row(signal='2026-01-02', entry='2026-01-05', exit_='2026-01-06', symbol='999999.SZ'))
        records = g.simulate(pd.DataFrame(candidates))
        self.assertEqual(records[-1]['status'], 'abstain')

    def test_loss_changes_next_position_lots_without_cash_topup(self):
        candidates = [row(symbol=f'{k:06}.SZ', day2_open=5.5, day2_high=7, day2_low=5.4, day2_close=6.8) for k in range(3)]
        candidates += [row(signal='2026-01-05', entry='2026-01-06', exit_='2026-01-07', symbol=f'{k+10:06}.SZ') for k in range(3)]
        records = [x for x in g.simulate(pd.DataFrame(candidates)) if x['status'] == 'closed']
        self.assertEqual(len(records), 6)
        self.assertEqual(records[0]['shares'], 1300)
        self.assertEqual(records[-1]['shares'], 700)
        self.assertGreaterEqual(records[-1]['cash_after_entry'], 0)

    def test_touch_is_separate_from_actual_policy_exit(self):
        records = g.simulate(pd.DataFrame([row(day2_high=7.5, day2_low=6.7)]))
        report = g.metrics(records)
        self.assertEqual(report['day2_touch6_rate'], 1)
        self.assertEqual(report['ge3_rate'], 0)
        self.assertEqual(report['win_rate'], 0)
        self.assertEqual(report['optimistic_win_rate_upper_proxy'], 1)
        self.assertIsNone(report['actual_win_rate'])

    def test_lot_quantity_predeclared_at_T_cap_not_lower_future_open(self):
        first = row(buy=6.8, day1_close=6.9, day2_open=7, day2_high=7.5, day2_low=6.9, day2_close=7.3)
        result = g.simulate(pd.DataFrame([first]))[0]
        self.assertEqual(result['frozen_buy_limit_cap'], 7.21)
        self.assertEqual(result['shares'], 1300)
        self.assertLess(result['shares'], int(10000 / 6.8 // 100) * 100)

    def test_earlier_low_open_cannot_expand_later_frozen_order(self):
        normal = [row(symbol=f'{k:06}.SZ') for k in range(3)]
        low_open = copy.deepcopy(normal)
        low_open[0] = row(symbol='000000.SZ', buy=5.5, day1_close=6)
        with patch.object(g, 'INITIAL_CAPITAL', 26000):
            a = g.simulate(pd.DataFrame(normal))
            b = g.simulate(pd.DataFrame(low_open))
        self.assertEqual([x['frozen_order_quantity'] for x in a], [1300, 1300, 1000])
        self.assertEqual([x['frozen_order_quantity'] for x in a],
                         [x['frozen_order_quantity'] for x in b])
        self.assertGreater(b[-1]['cash_after_entry'], a[-1]['cash_after_entry'])
        self.assertAlmostEqual(a[-1]['frozen_budget'], 26000 - 2 * 1300 * 7.21)

    def test_cancelled_order_budget_cannot_expand_later_frozen_order(self):
        candidates = [row(symbol=f'{k:06}.SZ') for k in range(3)]
        candidates[0] = row(symbol='000000.SZ', buy=7.3)
        with patch.object(g, 'INITIAL_CAPITAL', 26000):
            records = g.simulate(pd.DataFrame(candidates))
        self.assertEqual(records[0]['status'], 'no_fill')
        self.assertEqual([x['frozen_order_quantity'] for x in records], [1300, 1300, 1000])

    def test_unknown_entries_reserve_frozen_capital_with_limited_cash(self):
        candidates = [row(symbol=f'{k:06}.SZ') for k in range(3)]
        for candidate in candidates:
            candidate.update(outcome_status='unresolved', entry_one_price=True, reason='one_price_entry')
        with patch.object(g, 'INITIAL_CAPITAL', 26000):
            records = g.simulate(pd.DataFrame(candidates))
        self.assertEqual([x['frozen_order_quantity'] for x in records], [1300, 1300, 1000])
        self.assertTrue(all(x['reserved_cash'] == x['frozen_capital'] for x in records))
        self.assertAlmostEqual(records[-1]['cash_after_entry'], 26000 - 3600 * 7.21)

    def test_features_do_not_use_future_prices(self):
        days = [(dt.date(2024, 1, 1) + dt.timedelta(days=i)).isoformat() for i in range(270)]
        history = [bar(day, 7 + i*.0001, 7.2 + i*.0001, 6.9 + i*.0001, 7.1 + i*.0001)
                   for i, day in enumerate(days)]
        first = g.symbol_features(history, '000001.SZ', days)
        changed = copy.deepcopy(history)
        changed[-1] = bar(days[-1], 9, 9.9, 8, 8.5)
        second = g.symbol_features(changed, '000001.SZ', days)
        keys = [x for x in g.FEATURES if not x.startswith('market_')]
        a = first[first.date == days[-3]][keys].to_numpy(dtype=float)
        b = second[second.date == days[-3]][keys].to_numpy(dtype=float)
        np.testing.assert_array_equal(a, b)
        self.assertNotEqual(first.loc[first.date == days[-3], 'label_ge6'].iloc[0],
                            second.loc[second.date == days[-3], 'label_ge6'].iloc[0])

    def test_split_purges_two_session_outcomes(self):
        dates = pd.bdate_range('2025-01-01', periods=260).strftime('%Y-%m-%d').tolist()
        data = pd.DataFrame({'date': dates[:-2], 'outcome_date': dates[2:],
                             'outcome_status': 'closed'})
        parts = g.split(data)
        for a, b in zip(list(parts)[:-1], list(parts)[1:]):
            self.assertLess(parts[a].outcome_date.max(), parts[b].date.min())

    def test_definition_is_gross_and_historical_only(self):
        self.assertFalse(g.PROTOCOL['costs_included'])
        self.assertFalse(g.PROTOCOL['first_day_green_required'])
        self.assertFalse(g.PROTOCOL['daily_touch_is_actual_execution'])
        self.assertEqual(g.PROTOCOL['selection_grid_size'], 72)
        self.assertEqual(len(g.strategy_definition_sha256()), 64)


if __name__ == '__main__':
    unittest.main()
