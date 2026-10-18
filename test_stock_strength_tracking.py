import copy
import sqlite3
import unittest

import stock_strength_tracking as tracking


def forecast():
    return {'base_trade_date': '2026-10-09', 'target_trade_date': '2026-10-12',
            'generated_at': '2026-10-09T18:00:00+08:00',
            'sector_stock_selection': {'primary': [{'symbol': '000001.SZ', 'name': 'Fixture only', 'close': 7}]}}


class StockStrengthTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('''CREATE TABLE daily_quotes(symbol TEXT,trade_date TEXT,open REAL,high REAL,low REAL,
            close REAL,volume REAL,conflict INTEGER,fetched_at TEXT,source_updated_at TEXT,source TEXT,finality TEXT)''')

    def tearDown(self):
        self.db.close()

    def add(self, day, open_=7, close=7.1, symbol='000001.SZ', **kwargs):
        row = {'symbol': symbol, 'trade_date': day, 'open': open_, 'high': max(open_, close)+.1,
               'low': min(open_, close)-.1, 'close': close, 'volume': 1000, 'conflict': 0,
               'fetched_at': day+'T16:00:00+08:00', 'source_updated_at': day+'T15:10:00+08:00',
               'source': 'fixture', 'finality': 'post_close_provider'}
        row.update(kwargs)
        self.db.execute('INSERT INTO daily_quotes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', tuple(row.values()))

    def test_future_rows_never_become_early_wins(self):
        self.add('2026-10-12', close=7.2)
        self.add('2026-10-13', close=7.4)
        self.add('2026-10-14', close=7.6)
        result = tracking.evaluate(self.db, forecast(), '2026-10-12T14:59:59+08:00')
        self.assertEqual(result['n_D2_resolved'], 0)
        self.assertEqual(result['n_strength3_resolved'], 0)
        self.assertIsNone(result['rows'][0]['D1_close_above_open'])
        self.assertIsNone(result['rows'][0]['D2_open_reference_profitable'])
        self.assertEqual(result['status'], 'pending')

    def test_missing_D3_retains_known_D2_price_event(self):
        self.add('2026-10-12', close=7.2)
        self.add('2026-10-13', close=7.4)
        result = tracking.evaluate(self.db, forecast(), '2026-10-13T18:00:00+08:00')
        self.assertEqual(result['n_D2_resolved'], 1)
        self.assertTrue(result['rows'][0]['D2_open_reference_ge3'])
        self.assertIsNone(result['rows'][0]['strength3_label'])
        self.assertEqual(result['n_strength3_pending'], 1)
        self.assertIsNone(result['actual_win_rate'])

    def test_completed_continuous_strength_is_price_event_only(self):
        self.add('2026-10-12', close=7.2)
        self.add('2026-10-13', close=7.4)
        self.add('2026-10-14', close=7.6)
        result = tracking.evaluate(self.db, forecast(), '2026-10-14T18:00:00+08:00')
        self.assertEqual(result['n_strength3'], 1)
        self.assertEqual(result['strength3_rate'], 1)
        self.assertEqual(result['measurement'], 'OPEN_REFERENCE_PRICE_EVENT')
        self.assertIsNone(result['actual_win_rate'])
        self.assertFalse(result['rows'][0]['execution_verified'])

    def test_missing_and_conflicting_bars_are_unresolved_not_wins(self):
        self.add('2026-10-12', close=7.2)
        self.add('2026-10-13', close=7.4, conflict=1)
        result = tracking.evaluate(self.db, forecast(), '2026-10-14T18:00:00+08:00')
        self.assertEqual(result['n_D2_unresolved'], 1)
        self.assertEqual(result['n_strength3_unresolved'], 1)
        self.assertEqual(result['n_D2_open_reference_profitable'], 0)
        self.assertIsNone(result['D2_open_reference_profitable_rate'])
        self.assertIsNone(result['rows'][0]['strength3_label'])

    def test_future_retrieval_and_update_timestamps_are_excluded(self):
        for field in ('fetched_at', 'source_updated_at'):
            with self.subTest(field=field):
                self.db.execute('DELETE FROM daily_quotes')
                self.add('2026-10-12', close=7.2)
                self.add('2026-10-13', close=7.4, **{field: '2026-10-15T18:00:00+08:00'})
                result = tracking.evaluate(self.db, forecast(), '2026-10-14T18:00:00+08:00')
                self.assertEqual(result['n_D2_resolved'], 0)
                self.assertEqual(result['n_invalid_or_future_observations'], 1)

    def test_false_finality_and_nonpositive_prices_are_excluded(self):
        for change in ({'finality': 'intraday'}, {'volume': 0}, {'close': 0}):
            with self.subTest(change=change):
                self.db.execute('DELETE FROM daily_quotes')
                self.add('2026-10-12', close=7.2)
                self.add('2026-10-13', **change)
                result = tracking.evaluate(self.db, forecast(), '2026-10-14T18:00:00+08:00')
                self.assertEqual(result['n_D2_resolved'], 0)

    def test_close_cannot_be_retrieved_before_session_close(self):
        self.add('2026-10-12', close=7.2)
        self.add('2026-10-13', close=7.4, fetched_at='2026-10-13T14:00:00+08:00')
        result = tracking.evaluate(self.db, forecast(), '2026-10-14T18:00:00+08:00')
        self.assertEqual(result['n_D2_unresolved'], 1)

    def test_after_open_prediction_is_not_forward_eligible(self):
        value = forecast()
        value['generated_at'] = '2026-10-12T09:30:00+08:00'
        result = tracking.evaluate(self.db, value, '2026-10-14T18:00:00+08:00')
        self.assertFalse(result['forward_eligible'])
        self.assertEqual(result['status'], 'ineligible_forecast')
        self.assertEqual(result['rows'], [])

    def test_future_generated_forecast_is_not_known_at_cutoff(self):
        result = tracking.evaluate(self.db, forecast(), '2026-10-09T17:59:00+08:00')
        self.assertFalse(result['forward_eligible'])

    def test_only_first_three_frozen_stocks_never_reranked(self):
        value = forecast()
        value['sector_stock_selection']['primary'] = [{'symbol': f'{index:06}.SZ', 'close': 7}
                                                     for index in range(5)]
        result = tracking.evaluate(self.db, value, '2026-10-14T18:00:00+08:00')
        self.assertEqual([row['symbol'] for row in result['rows']], ['000000.SZ', '000001.SZ', '000002.SZ'])
        self.assertEqual(result['n_selected_beyond_three_ignored'], 2)

    def test_D2_profit_is_separate_from_three_day_strength(self):
        self.add('2026-10-12', close=6.9)
        self.add('2026-10-13', close=7.3)
        self.add('2026-10-14', close=7.5)
        result = tracking.evaluate(self.db, forecast(), '2026-10-14T18:00:00+08:00')
        self.assertTrue(result['rows'][0]['D2_open_reference_profitable'])
        self.assertFalse(result['rows'][0]['strength3_label'])
        self.assertFalse(result['rows'][0]['strength2_label'])


if __name__ == '__main__':
    unittest.main()
