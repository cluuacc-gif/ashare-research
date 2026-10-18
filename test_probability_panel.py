import copy
from contextlib import closing
import datetime as dt
from pathlib import Path
import sqlite3
import tempfile
import unittest

import numpy as np
import pandas as pd

import formal_price_model as old_model
import probability_panel as p
import price_action_research as action
from test_gross_swing_strategy import bar


def history_rows(count=285, price=25):
    result = []
    for index in range(count):
        day = (dt.date(2025, 1, 1)+dt.timedelta(days=index)).isoformat()
        value = price+index*.004+.03*np.sin(index/3)
        result.append(bar(day, value, value+.13, value-.11, value+.04))
    return result


def labels(*, first=None, second=None, close=25):
    return p.policy_labels('2026-01-01', '000001.SZ', '2026-01-02', '2026-01-05',
        first or bar('2026-01-02', 25, 25.2, 24.9, 25.1),
        second or bar('2026-01-05', 25.1, 26.6, 25, 25.8), close)


class ProbabilityPanelTests(unittest.TestCase):
    def test_wide_universe_does_not_change_existing_model(self):
        rows = history_rows()
        sessions = [row['trade_date'] for row in rows]
        self.assertFalse(p.symbol_features(rows, '000001.SZ', sessions).empty)
        self.assertTrue(old_model.features(rows, '000001.SZ', dict(zip(sessions, range(len(sessions))))).empty)
        self.assertEqual(p.PRICE_MAX, 100)

    def test_labels_follow_policy_instead_of_raw_close(self):
        result = labels(second=bar('2026-01-05', 25, 26, 24, 25.8))
        self.assertEqual(result['raw_label_profit'], 1)
        self.assertEqual(result['raw_label_ge3'], 1)
        self.assertEqual(result['label_profit3'], 0)
        self.assertEqual(result['label_ge3'], 0)
        self.assertTrue(result['policy_ambiguous3'])
        self.assertGreater(result['policy_optimistic_return3'], 0)

    def test_three_and_six_percent_are_distinct_policies(self):
        result = labels(second=bar('2026-01-05', 25, 26.1, 24.8, 25.2))
        self.assertEqual(result['label_profit3'], 1)
        self.assertEqual(result['label_ge3'], 1)
        self.assertEqual(result['label_ge6'], 0)
        self.assertAlmostEqual(result['policy_gross_return3'], .03)

    def test_cancelled_open_is_not_negative_training_label(self):
        result = labels(first=bar('2026-01-02', 26, 26.2, 25.9, 26.1))
        self.assertTrue(result['entry_cancelled'])
        self.assertEqual(result['policy_label_status'], 'cancelled')
        self.assertIsNone(result['label_profit3'])
        self.assertIsNone(result['label_ge3'])
        self.assertIsNone(result['label_ge6'])

    def test_sub_minimum_open_is_cancelled_even_if_later_profitable(self):
        result = labels(first=bar('2026-01-02', 4.9, 5, 4.8, 4.95), close=5)
        self.assertEqual(result['policy_label_status'], 'cancelled')

    def test_cap_is_frozen_at_T_and_rounded_down_to_cent(self):
        self.assertEqual(p.frozen_cap(25), 25.62)
        self.assertEqual(p.frozen_cap(99), 100)
        self.assertEqual(labels()['frozen_buy_limit_cap'], 25.62)

    def test_missing_or_one_price_execution_remains_unknown(self):
        one = labels(first=bar('2026-01-02', 25, 25, 25, 25))
        self.assertEqual(one['policy_label_status'], 'unresolved')
        self.assertIsNone(one['label_profit3'])
        missing = p.policy_labels('2026-01-01', '000001.SZ', '2026-01-02', None,
                                  bar('2026-01-02', 25, 25.1, 24.9, 25), None, 25)
        self.assertIsNone(missing['label_profit3'])

    def test_future_prices_cannot_change_past_features(self):
        rows = history_rows()
        sessions = [row['trade_date'] for row in rows]
        before = p.symbol_features(rows, '000001.SZ', sessions)
        changed = copy.deepcopy(rows)
        for source in changed[265:]:
            source.update(open=26, high=26.4, low=25.8, close=26.3, volume=500000)
        after = p.symbol_features(changed, '000001.SZ', sessions)
        causal = [column for column in p.FEATURES if column not in
                  ('market_mean_return', 'market_breadth', 'relative_return5', 'price_action_score')]
        pd.testing.assert_frame_equal(before.loc[before.index<265, causal],
                                      after.loc[after.index<265, causal])

    def test_invalid_trailing_bar_and_amount_fail_closed(self):
        for key, value in (('conflict', 1), ('amount', None), ('volume', 0)):
            rows = history_rows()
            rows[-5][key] = value
            frame = p.symbol_features(rows, '000001.SZ', [row['trade_date'] for row in rows])
            self.assertNotIn(len(rows)-1, frame.index)

    def test_missing_global_market_session_rejects_nonconsecutive_window(self):
        rows = history_rows()
        sessions = [row['trade_date'] for row in rows]
        rows.pop(-10)
        frame = p.symbol_features(rows, '000001.SZ', sessions)
        self.assertNotIn(sessions[-1], list(frame.date))

    def test_one_price_signal_and_raw_corporate_action_jump_fail_closed(self):
        rows = history_rows()
        rows[-1].update(high=rows[-1]['close'], low=rows[-1]['close'], open=rows[-1]['close'])
        frame = p.symbol_features(rows, '000001.SZ', [row['trade_date'] for row in rows])
        self.assertNotIn(rows[-1]['trade_date'], list(frame.date))
        rows = history_rows()
        for row in rows[-10:]:
            for key in ('open', 'high', 'low', 'close'):
                row[key] *= .5
        frame = p.symbol_features(rows, '000001.SZ', [row['trade_date'] for row in rows])
        self.assertNotIn(rows[-1]['trade_date'], list(frame.date))

    def test_history_minimum_counts_valid_bars(self):
        rows = history_rows(250)
        rows[0]['conflict'] = 1
        self.assertTrue(p.symbol_features(rows, '000001.SZ', [row['trade_date'] for row in rows]).empty)

    def test_missing_D3_does_not_delete_mature_D2_policy(self):
        rows = history_rows()
        frame = p.symbol_features(rows, '000001.SZ', [row['trade_date'] for row in rows])
        penultimate_signal = frame.iloc[-3]
        self.assertEqual(penultimate_signal.policy_label_status, 'closed')
        self.assertTrue(pd.isna(penultimate_signal.strength3_label))
        self.assertTrue(pd.isna(frame.iloc[-1].label_profit3))

    def test_duplicate_input_dates_rejected(self):
        rows = history_rows()
        rows.append(rows[-1].copy())
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            p.symbol_features(rows, '000001.SZ', [row['trade_date'] for row in rows])

    def test_readonly_panel_market_context_and_latest_unknown_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'market.db'
            with closing(sqlite3.connect(path)) as db:
                db.execute(f'PRAGMA application_id={p.c.APP_ID}')
                db.executescript('CREATE TABLE daily_quotes(symbol TEXT,trade_date TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,amount REAL,conflict INTEGER); CREATE TABLE security_master(symbol TEXT,name TEXT);')
                for index, price in enumerate((7, 25, 80)):
                    symbol = f'{index:06}.SZ'
                    rows = history_rows(price=price)
                    db.execute('INSERT INTO security_master VALUES (?,?)', (symbol, 'fixture'))
                    db.executemany('INSERT INTO daily_quotes VALUES (?,?,?,?,?,?,?,?,?)',
                        [(symbol, r['trade_date'], r['open'], r['high'], r['low'], r['close'],
                          r['volume'], r['amount'], r['conflict']) for r in rows])
                db.commit()
            before = path.read_bytes()
            history, live, names, scanned = p.panel(path, rows[-1]['trade_date'])
            self.assertEqual(scanned, 855)
            self.assertEqual(len(live), 3)
            self.assertEqual(len(names), 3)
            self.assertTrue(live.label_profit3.isna().all())
            self.assertTrue(history.market_breadth.between(0, 1).all())
            self.assertTrue(live.price_action_score.between(0, 100).all())
            pd.testing.assert_series_equal(live.price_action_score,
                action.add_score(live).price_action_score)
            self.assertEqual(before, path.read_bytes())


if __name__ == '__main__':
    unittest.main()
