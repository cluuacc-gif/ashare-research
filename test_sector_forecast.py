"""Synthetic causal/coverage contracts, never evidence of market accuracy."""
import copy
from datetime import date, timedelta
import json
import sqlite3
import unittest

import sector_forecast as sf


class SectorForecastTests(unittest.TestCase):
    BASE = '2025-02-03'
    TARGET = '2025-02-04'
    CUTOFF = '2025-02-03T18:00:00+08:00'

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE industry_members(symbol TEXT,classification TEXT,industry TEXT,
                effective_date TEXT,observed_at TEXT,source_url TEXT);
            CREATE TABLE daily_quotes(symbol TEXT,trade_date TEXT,open REAL,high REAL,low REAL,
                close REAL,amount REAL,conflict INTEGER,fetched_at TEXT,
                source_updated_at TEXT,source TEXT,finality TEXT);
        ''')
        self.days = []
        day = date(2025, 1, 6)
        while len(self.days) < 21:
            if day.weekday() < 5:
                self.days.append(day.isoformat())
            day += timedelta(days=1)
        self.assertEqual(self.days[-1], self.BASE)
        for group, growth in [('甲', .003), ('乙', .001), ('丙', -.002)]:
            for index in range(5):
                symbol = f'SYNTHETIC-{group}-{index}'
                self.db.execute('INSERT INTO industry_members VALUES(?,?,?,?,?,?)',
                    (symbol, 'sina_industry', group, '2025-01-01',
                     '2025-02-03T16:00:00+08:00', 'https://vendor.example/industry'))
                for position, trade_day in enumerate(self.days):
                    close = (10 + index) * (1 + growth) ** position
                    self.insert_bar(symbol, trade_day, close, 1000000 + position * 10000)
        self.db.commit()

    def insert_bar(self, symbol, trade_day, close, amount=1000000, fetched=None):
        self.db.execute('INSERT INTO daily_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (symbol, trade_day, close * .998, close * 1.01, close * .99,
             close, amount, 0, fetched or '2025-02-03T16:00:00+08:00',
             None, 'synthetic_fixture', 'historical_provider'))

    def generate(self, cutoff=None, target=None):
        return sf.generate(self.db, self.BASE, target or self.TARGET, cutoff or self.CUTOFF)

    def test_ready_rankings_are_scores_not_probabilities(self):
        result = self.generate()
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['rankings'][0]['sector'], '甲')
        self.assertEqual(result['coverage']['covered_stocks'], 15)
        self.assertTrue(result['no_probability_claim'])
        self.assertFalse(result['formal_trade_ready'])
        self.assertIsNone(result['validation']['historical_win_rate'])
        self.assertFalse(result['forward_eligible'])  # Historical fixture is not a live forecast.
        self.assertEqual(result['base_trade_date'], self.BASE)
        self.assertEqual(result['target_trade_date'], self.TARGET)
        json.dumps(result, allow_nan=False)

    def test_target_prices_cannot_change_forecast(self):
        before = self.generate()
        for symbol in [row[0] for row in self.db.execute('SELECT DISTINCT symbol FROM daily_quotes')]:
            self.insert_bar(symbol, self.TARGET, 1000, fetched='2025-02-04T16:00:00+08:00')
        after = self.generate()
        self.assertEqual(before['rankings'], after['rankings'])
        self.assertEqual(before['forecast_id'], after['forecast_id'])

    def test_membership_learned_after_cutoff_is_excluded(self):
        self.db.execute("UPDATE industry_members SET observed_at='2025-02-03T18:00:01+08:00'")
        result = self.generate()
        self.assertEqual(result['status'], 'insufficient_data')
        self.assertEqual(result['coverage']['excluded_unknown_or_future_rows'], 15)

    def test_membership_known_after_price_base_can_support_later_forward_issue(self):
        self.db.execute("UPDATE industry_members SET effective_date='2025-02-04', observed_at='2025-02-04T07:00:00+08:00'")
        self.assertEqual(self.generate()['status'], 'insufficient_data')
        result = self.generate('2025-02-04T08:00:00+08:00', '2025-02-05')
        self.assertEqual(result['status'], 'ready')
        self.assertTrue(result['sources']['membership_effective_after_price_base'])

    def test_timezone_comparison_uses_instants(self):
        self.db.execute("UPDATE industry_members SET observed_at='2025-02-03T09:59:00Z'")
        self.assertEqual(self.generate()['status'], 'ready')
        self.db.execute("UPDATE industry_members SET observed_at='2025-02-03T10:01:00Z'")
        self.assertEqual(self.generate()['status'], 'insufficient_data')

    def test_future_effective_membership_is_excluded(self):
        self.db.execute("UPDATE industry_members SET effective_date='2025-02-04'")
        self.assertEqual(self.generate()['status'], 'insufficient_data')

    def test_ambiguous_latest_membership_is_not_double_counted(self):
        self.db.execute('INSERT INTO industry_members SELECT symbol,classification,\'冲突\',effective_date,observed_at,source_url FROM industry_members')
        result = self.generate()
        self.assertEqual(result['status'], 'insufficient_data')
        self.assertEqual(result['coverage']['ambiguous_symbol_memberships'], 15)

    def test_later_quote_fetch_or_conflict_invalidates_member_history(self):
        self.db.execute("UPDATE daily_quotes SET fetched_at='2025-02-03T19:00:00+08:00' WHERE symbol='SYNTHETIC-甲-0'")
        self.assertEqual(self.generate()['status'], 'insufficient_data')
        self.db.execute("UPDATE daily_quotes SET fetched_at='2025-02-03T16:00:00+08:00',conflict=1 WHERE symbol='SYNTHETIC-甲-0'")
        self.assertEqual(self.generate()['status'], 'insufficient_data')

    def test_gap_or_missing_history_cannot_be_forward_filled(self):
        self.db.execute("DELETE FROM daily_quotes WHERE symbol='SYNTHETIC-甲-0' AND trade_date=?", (self.days[5],))
        self.assertEqual(self.generate()['status'], 'insufficient_data')

    def test_unknown_source_update_time_is_not_treated_as_known(self):
        self.db.execute("UPDATE daily_quotes SET source_updated_at='not-a-timestamp'")
        self.assertEqual(self.generate()['status'], 'insufficient_data')

    def test_invalid_cutoffs_are_rejected(self):
        for cutoff in ('2025-02-03T14:59:59+08:00', '2025-02-04T09:30:00+08:00', '2025-02-03T18:00:00'):
            with self.subTest(cutoff=cutoff), self.assertRaises(ValueError):
                self.generate(cutoff)

    def test_auction_cutoff_can_use_prior_close_features(self):
        self.assertEqual(self.generate('2025-02-04T09:25:00+08:00')['status'], 'ready')

    def test_database_is_not_mutated(self):
        before = self.db.total_changes
        self.generate()
        self.assertEqual(self.db.total_changes, before)

    def test_evaluation_uses_frozen_members_and_separates_relative_and_absolute(self):
        forecast = self.generate()
        self.db.execute("UPDATE industry_members SET industry='事后全部换组'")
        for sector, rows in forecast['frozen_members'].items():
            gross = {'甲': -.01, '乙': -.02, '丙': -.03}[sector]
            for row in rows:
                self.insert_bar(row['symbol'], self.TARGET, row['base_close'] * (1 + gross),
                                fetched='2025-02-04T16:00:00+08:00')
        result = sf.evaluate(self.db, forecast, '2025-02-04T18:00:00+08:00')
        self.assertEqual(result['status'], 'resolved')
        winner = next(row for row in result['sectors'] if row['sector'] == '甲')
        self.assertTrue(winner['relative_strength'])
        self.assertFalse(winner['absolute_up'])
        self.assertTrue(winner['top_quartile'])
        self.assertIsNone(result['gross_trade_win_rate'])

    def test_missing_target_quote_remains_unresolved(self):
        forecast = self.generate()
        result = sf.evaluate(self.db, forecast, '2025-02-04T18:00:00+08:00')
        self.assertEqual(result['status'], 'unresolved')
        self.assertEqual(result['missing_stocks'], 15)
        self.assertIsNone(result['benchmark_return'])
        self.assertIsNone(result['top3_relative_strength_hits'])
        self.assertTrue(all(row['absolute_up'] is None for row in result['sectors']))

    def test_partial_missing_quotes_do_not_rebase_benchmark(self):
        forecast = self.generate()
        for rows in forecast['frozen_members'].values():
            for row in rows:
                if row['symbol'] != 'SYNTHETIC-甲-0':
                    self.insert_bar(row['symbol'], self.TARGET, row['base_close'] * 1.01,
                                    fetched='2025-02-04T16:00:00+08:00')
        result = sf.evaluate(self.db, forecast, '2025-02-04T18:00:00+08:00')
        self.assertEqual(result['status'], 'unresolved')
        self.assertEqual(result['missing_stocks'], 1)
        self.assertIsNone(result['top3_relative_strength_hits'])
        self.assertTrue(all(row['relative_strength'] is None for row in result['sectors']))

    def test_constituent_tampering_is_rejected(self):
        forecast = self.generate()
        changed = copy.deepcopy(forecast)
        changed['frozen_members']['甲'][0]['base_close'] = 1
        with self.assertRaises(ValueError):
            sf.evaluate(self.db, changed, '2025-02-04T18:00:00+08:00')

    def test_evaluation_before_target_close_is_rejected(self):
        with self.assertRaises(ValueError):
            sf.evaluate(self.db, self.generate(), '2025-02-04T14:59:59+08:00')


if __name__ == '__main__':
    unittest.main()
