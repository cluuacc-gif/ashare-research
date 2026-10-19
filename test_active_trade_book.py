"""Synthetic manual-evidence and reference-account invariants, not returns."""
import copy
import datetime as dt
import unittest

import active_trade_book as book
from nextday_forward import expected_ends, instant
from test_nextday_forward import db_fixture, evidence

BASE = '2026-10-16'
D1 = '2026-10-19'
D2 = '2026-10-20'


def plan():
    return {'active_plan_id': 'active-frozen-test', 'symbol': '000001.SZ', 'name': '合成样本',
        'base_trade_date': BASE, 'target_trade_date': D1, 'D2_trade_date': D2,
        'generated_at': '2026-10-18T16:00:00+08:00', 'base_close': 10.,
        'frozen_entry_floor': 9.95, 'frozen_entry_cap': 10.05, 'structural_stop': 9.85,
        'target_pct': .03, 'frozen_quantity_shares': 300, 'frozen_reserved_cash_cny': 3015.,
        'sector_identity': {'classification': 'eastmoney_industry', 'sector_code': 'BK001', 'sector': '合成板块'}}


def entry_evidence():
    payload = evidence(D1)
    row = next(row for row in payload['000001.SZ']['bars'] if row['end_at'] == D1+'T14:40:00+08:00')
    row.update(close=10.03)
    return payload


def live_inputs():
    decision = instant(D1+'T14:40:00+08:00')
    rows = [copy.deepcopy(row) for row in entry_evidence()['000001.SZ']['bars']
            if instant(row['end_at']) <= decision]
    for row in rows:
        row.update(symbol='000001.SZ', complete=True,
                   observed_at=(instant(row['end_at'])+dt.timedelta(seconds=1)).isoformat())
    quote = {'symbol': '000001.SZ', 'exchange_timestamp': D1+'T14:40:20+08:00',
        'received_at': D1+'T14:40:21+08:00', 'last': 10.01, 'ask1': 10.02,
        'ask1_volume': 500, 'prev_close': 10.,
        'status': {'verified': True, 'is_st': False, 'suspended': False,
                   'delisting': False, 'trading_status': 'normal'},
        'limits': {'official_verified': True, 'limit_up': 11., 'limit_down': 9.}}
    return rows, quote


def minute(payload, clock):
    return next(row for row in payload['000001.SZ']['bars'] if row['end_at'].endswith(clock+':00+08:00'))


class ManualEntryReviewTests(unittest.TestCase):
    def review(self, rows=None, quote=None, now=D1+'T14:40:30+08:00', current_plan=None):
        default_rows, default_quote = live_inputs()
        return book.review_entry(plan() if current_plan is None else current_plan,
            default_rows if rows is None else rows, default_quote if quote is None else quote, now)

    def test_full_live_evidence_is_conditional_manual_confirmation_never_a_fill(self):
        result = self.review()
        self.assertTrue(result['conditions_met'])
        self.assertEqual(result['status'], 'conditions_met_manual_execution')
        for key in ('order_execution_enabled', 'actual_fill_verified', 'orders_submitted'):
            self.assertFalse(result[key])
        self.assertEqual(result['reference_quantity'], plan()['frozen_quantity_shares'])
        self.assertLessEqual(result['reference_ask'], result['maximum_buy_price'])

    def test_only_frozen_target_and_late_window_can_be_reviewed(self):
        for now in (D1+'T14:39:59+08:00', D1+'T14:45:01+08:00', D2+'T14:40:30+08:00'):
            with self.subTest(now=now):
                self.assertFalse(self.review(now=now)['conditions_met'])
        late = plan(); late['generated_at'] = D1+'T09:15:00+08:00'
        self.assertFalse(self.review(current_plan=late)['conditions_met'])

    def test_complete_prefix_preserves_lunch_and_cannot_use_future_or_unseen_bars(self):
        rows, quote = live_inputs()
        expected = expected_ends(D1, instant(D1+'T14:40:00+08:00'))
        self.assertEqual(len(rows), len(expected))
        cases = []
        missing = copy.deepcopy(rows); missing.pop(3); cases.append(missing)
        future_known = copy.deepcopy(rows); future_known[0]['observed_at'] = D1+'T14:41:00+08:00'; cases.append(future_known)
        unfinished = copy.deepcopy(rows); unfinished[-1]['complete'] = False; cases.append(unfinished)
        wrong_symbol = copy.deepcopy(rows); wrong_symbol[-1]['symbol'] = '000002.SZ'; cases.append(wrong_symbol)
        bad_prices = copy.deepcopy(rows); bad_prices[-1]['close'] = 99; cases.append(bad_prices)
        duplicate = copy.deepcopy(rows); duplicate.append(copy.deepcopy(rows[-1])); cases.append(duplicate)
        for bad_rows in cases:
            with self.subTest(last=bad_rows[-1]):
                self.assertFalse(self.review(rows=bad_rows, quote=quote)['conditions_met'])

    def test_quote_time_and_official_status_are_required(self):
        for mutation in ('future', 'stale', 'wrong_symbol', 'ST', 'halted', 'delisting',
                         'unknown_status', 'unknown_limits', 'zero_ask_volume'):
            rows, quote = live_inputs()
            if mutation == 'future': quote['received_at'] = D1+'T14:40:31+08:00'
            elif mutation == 'stale': quote['exchange_timestamp'] = D1+'T14:39:00+08:00'
            elif mutation == 'wrong_symbol': quote['symbol'] = '000002.SZ'
            elif mutation == 'ST': quote['status']['is_st'] = True
            elif mutation == 'halted': quote['status']['suspended'] = True
            elif mutation == 'delisting': quote['status']['delisting'] = True
            elif mutation == 'unknown_status': quote['status']['verified'] = False
            elif mutation == 'unknown_limits': quote['limits']['official_verified'] = False
            else: quote['ask1_volume'] = 0
            with self.subTest(mutation=mutation):
                self.assertFalse(self.review(rows=rows, quote=quote)['conditions_met'])

    def test_fresh_ask_still_must_respect_frozen_bounds_stop_and_official_limits(self):
        for last, ask in ((10.06, 10.07), (9.90, 9.91), (9.84, 9.85), (10.99, 11.00)):
            rows, quote = live_inputs(); quote.update(last=last, ask1=ask)
            with self.subTest(last=last, ask=ask):
                self.assertFalse(self.review(rows=rows, quote=quote)['conditions_met'])
        rows, quote = live_inputs(); quote['prev_close'] = 9.9
        self.assertFalse(self.review(rows=rows, quote=quote)['conditions_met'])

    def test_STAR_requires_at_least_200_frozen_shares(self):
        rows, quote = live_inputs()
        star = plan(); star.update(symbol='688001.SH', frozen_quantity_shares=100)
        quote['symbol'] = star['symbol']
        for row in rows: row['symbol'] = star['symbol']
        self.assertFalse(self.review(rows=rows, quote=quote, current_plan=star)['conditions_met'])
        star['frozen_quantity_shares'] = 200
        self.assertTrue(self.review(rows=rows, quote=quote, current_plan=star)['conditions_met'])


class ReferenceBookTests(unittest.TestCase):
    def setUp(self):
        self.db = db_fixture()
        self.addCleanup(self.db.close)

    def first(self, data=None, current_plan=None, cutoff=D1+'T17:00:00+08:00'):
        return book.evaluate(plan() if current_plan is None else current_plan,
            entry_evidence() if data is None else data, self.db, cutoff)

    def finish(self, previous, data=None, cutoff=D2+'T17:00:00+08:00'):
        return book.evaluate(plan(), evidence(D2, 10.10) if data is None else data,
                             self.db, cutoff, previous)

    def test_future_payload_does_not_buy_early_and_D1_never_sells(self):
        early = self.first(cutoff=D1+'T14:40:00+08:00')
        self.assertEqual(early['status'], 'pending_entry')
        self.assertNotIn('reference_buy_price', early)
        first = self.first()
        self.assertEqual(first['status'], 'pending_exit')
        self.assertEqual(first['reference_buy_price'], 10.)
        self.assertNotIn('reference_sell_price', first)
        self.assertFalse(first['actual_fill_verified'])
        self.assertEqual(self.finish(first, cutoff=D2+'T09:30:00+08:00')['status'], 'pending_exit')

    def test_fixed_D2_exit_is_open_of_minute_starting_1450(self):
        data = evidence(D2, 10.10)
        minute(data, '14:51').update(open=10.17, low=10.15, high=10.19, close=10.18)
        result = self.finish(self.first(), data)
        self.assertEqual(result['status'], 'reference_closed')
        self.assertEqual(result['reference_sell_price'], 10.17)
        self.assertEqual(result['reference_exit_at'], D2+'T14:50:00+08:00')
        self.assertEqual(result['reason'], 'D2_fixed_1450_reference_exit')
        self.assertAlmostEqual(result['gross_return'], .017)
        self.assertFalse(result['actual_fill_verified'])
        self.assertIsNone(result['actual_win_rate'])
        self.assertFalse(result['costs_included'])

    def test_completed_target_minute_exits_next_open_not_exact_target(self):
        data = evidence(D2, 10.10)
        minute(data, '10:01').update(high=10.31, low=10.08, close=10.12)
        minute(data, '10:02').update(open=10.26, high=10.28, low=10.24, close=10.27)
        result = self.finish(self.first(), data)
        self.assertEqual(result['reason'], 'completed_minute_target_next_open')
        self.assertEqual(result['reference_exit_at'], D2+'T10:01:00+08:00')
        self.assertEqual(result['reference_sell_price'], 10.26)
        self.assertLess(result['gross_return'], .03)
        self.assertFalse(result['exit_ge3'])

    def test_same_bar_stop_and_target_uses_stop_reason_and_next_open(self):
        data = evidence(D2, 10.10)
        minute(data, '10:01').update(high=10.31, low=9.84, close=10.12)
        minute(data, '10:02').update(open=9.80, high=9.82, low=9.78, close=9.81)
        result = self.finish(self.first(), data)
        self.assertTrue(result['same_bar_stop_and_target'])
        self.assertEqual(result['reason'], 'completed_minute_stop_next_open')
        self.assertEqual(result['reference_sell_price'], 9.80)
        self.assertLess(result['gross_return'], 0)

    def test_D1_failure_and_D2_gap_exit_first_tradable_open(self):
        for mutation in ('D1_failure', 'D2_gap_down', 'D2_gap_up'):
            with self.subTest(mutation=mutation):
                if mutation == 'D1_failure':
                    self.db.execute('UPDATE daily_quotes SET close=9.8 WHERE trade_date=?', (D1,))
                    data = evidence(D2, 10.10)
                elif mutation == 'D2_gap_down': data = evidence(D2, 9.80)
                else: data = evidence(D2, 10.35)
                first = self.first()
                result = self.finish(first, data)
                self.assertEqual(result['status'], 'reference_closed')
                self.assertEqual(result['reference_exit_at'], D2+'T09:30:00+08:00')
                self.assertEqual(result['reference_sell_price'], data['000001.SZ']['bars'][0]['open'])
                self.db.execute('UPDATE daily_quotes SET close=10.1 WHERE trade_date=?', (D1,))

    def test_D1_failure_waits_for_first_positive_volume_D2_open(self):
        self.db.execute('UPDATE daily_quotes SET close=9.8 WHERE trade_date=?', (D1,))
        data = evidence(D2, 10.10)
        minute(data, '09:31').update(volume=0., amount=0.)
        minute(data, '09:32').update(open=10.16, high=10.18, low=10.14, close=10.17)
        result = self.finish(self.first(), data)
        self.assertEqual(result['status'], 'reference_closed')
        self.assertEqual(result['reference_exit_at'], D2+'T09:31:00+08:00')
        self.assertEqual(result['reference_sell_price'], 10.16)

    def test_1130_trigger_uses_1300_open_across_scheduled_lunch_gap(self):
        data = evidence(D2, 10.10)
        minute(data, '11:30').update(high=10.31, low=10.08, close=10.12)
        minute(data, '13:01').update(open=10.26, high=10.28, low=10.24, close=10.27)
        result = self.finish(self.first(), data)
        self.assertEqual(result['reason'], 'completed_minute_target_next_open')
        self.assertEqual(result['reference_exit_at'], D2+'T13:00:00+08:00')
        self.assertEqual(result['reference_sell_price'], 10.26)

    def test_missing_or_zero_volume_exit_is_retained_unknown_and_reserves_cash(self):
        first = self.first()
        for mutation in ('missing', 'zero_volume'):
            data = evidence(D2, 10.10)
            if mutation == 'missing': data['000001.SZ']['bars'].pop(2)
            else: minute(data, '14:51').update(volume=0., amount=0.)
            result = self.finish(first, data)
            self.assertEqual(result['status'], 'unknown_exit')
            self.assertNotIn('reference_sell_price', result)
            stats = book.account([result])
            self.assertEqual(stats['unknown_positions'], 1)
            self.assertEqual(stats['reserved_cash_cny'], plan()['frozen_reserved_cash_cny'])
            self.assertIsNone(stats['reference_profit_rate'])

    def test_D1_evidence_cannot_be_backfilled_after_D2_deadline(self):
        first = self.first({})
        self.assertEqual(first['status'], 'unknown_entry')
        late = evidence(D2, 10.10)
        late['000001.SZ']['bars'] = entry_evidence()['000001.SZ']['bars']+late['000001.SZ']['bars']
        result = self.finish(first, late)
        self.assertEqual(result['status'], 'unknown_entry')
        self.assertNotIn('reference_buy_price', result)
        stats = book.account([result])
        self.assertEqual(stats['reserved_cash_cny'], plan()['frozen_reserved_cash_cny'])
        self.assertIsNone(stats['reference_profit_rate'])

    def test_no_signal_and_outside_cap_release_cash_without_substitution(self):
        data = entry_evidence()
        minute(data, '14:40').update(open=10.03, close=10.02)
        no_signal = self.first(data)
        self.assertEqual(no_signal['status'], 'no_signal')
        self.assertEqual(book.account([no_signal])['reserved_cash_cny'], 0)
        outside = self.first(evidence(D1, 10.10))
        self.assertEqual(outside['status'], 'price_outside_bounds')
        self.assertNotIn('reference_buy_price', outside)
        self.assertEqual(book.account([outside])['available_cash_cny'], 30000)

    def test_frozen_admission_and_budget_survive_reruns_and_cannot_be_rewritten(self):
        study = {'protocol_sha256': 'policy', 'target_trade_date': D1,
            'generated_at': plan()['generated_at'], 'plans': [plan()]}
        self.assertEqual(book.roll(study, False, {}, self.db, D1+'T08:00:00+08:00', 'policy')['records'], [])
        frozen = book.roll(study, True, {}, self.db, D1+'T08:00:00+08:00', 'policy')
        original = copy.deepcopy(frozen['frozen_plan_dates'])
        study['reference_book'] = frozen
        study['plans'] = []
        rerun = book.roll(study, True, {}, self.db, D1+'T08:01:00+08:00', 'policy')
        self.assertEqual(rerun['frozen_plan_dates'], original)
        self.assertEqual(len(rerun['records']), 1)
        self.assertEqual(rerun['account']['reserved_cash_cny'], 3015.)
        for key, value in (('frozen_entry_cap', 99), ('structural_stop', 1), ('frozen_quantity_shares', 900)):
            changed = plan(); changed[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                book.evaluate(changed, {}, self.db, D1+'T08:02:00+08:00', frozen['records'][0])

    def test_cash_account_keeps_unknowns_out_of_closed_win_rate_and_blocks_overspend(self):
        unknown = self.first({})
        closed = self.finish(self.first())
        stats = book.account([unknown, closed])
        self.assertEqual(stats['reference_closed'], 1)
        self.assertEqual(stats['reference_profit_rate'], 1.)
        self.assertEqual(stats['reference_profit_fraction_unknown_as_not_won'], .5)
        self.assertLess(stats['reference_profit_rate_wilson95'][0], .8)
        self.assertAlmostEqual(stats['reference_profit_rate_wilson95'][1],1.)
        self.assertEqual(stats['unknown_positions'], 1)
        self.assertIsNone(stats['actual_win_rate'])
        self.assertFalse(stats['actual_user_positions_known'])
        self.assertFalse(stats['is_actual_broker_account'])
        self.assertAlmostEqual(stats['available_cash_cny'], 30000+closed['gross_pnl_reference']-3015)
        overspent = copy.deepcopy(unknown); overspent['reserved_cash_cny'] = 30001
        with self.assertRaises(ValueError): book.account([overspent])

    def test_policy_change_cannot_reset_existing_positions_or_cash(self):
        study = {'protocol_sha256': 'old-policy', 'target_trade_date': D1,
            'generated_at': plan()['generated_at'], 'plans': [plan()]}
        old_book = book.roll(study, True, {}, self.db, D1+'T08:00:00+08:00', 'old-policy')
        study['reference_book'] = old_book
        with self.assertRaises(ValueError):
            book.roll(study, False, {}, self.db, D1+'T08:01:00+08:00', 'new-policy')


if __name__ == '__main__':
    unittest.main()
