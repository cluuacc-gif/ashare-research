import copy
import datetime as dt
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import price_action_research as p
from test_gross_swing_strategy import row, bar


def candidate(**kwargs):
    result = row(**kwargs)
    result.update(price_action_score=75, atr14_pct=.02, range_atr=1,
        body_atr=.3, upper_wick_atr=.1, efficiency20=.4, compression5_20=.8,
        breakout_atr=.1, higher_high_low3=1, pullback_depth_atr=.5,
        relative_return5=.03, volume_ratio20=1.2, ma5_gap=.01,
        ret5=.04, day3_date='2026-01-06', strength3_label=1)
    return result


def history_rows(count=280):
    rows = []
    for index in range(count):
        day = (dt.date(2025, 1, 1)+dt.timedelta(days=index)).isoformat()
        price = 6+index*.003+.03*np.sin(index/3)
        rows.append(bar(day, price, price+.08, price-.06, price+.04))
    return rows


class PriceActionTests(unittest.TestCase):
    def test_predeclared_small_rule_count_and_no_formal_activation(self):
        self.assertEqual(len(p.PATTERNS), 3)
        self.assertFalse(p.PROTOCOL['formal_activation'])
        self.assertFalse(p.PROTOCOL['historical_fundflow_used'])
        self.assertFalse(p.PROTOCOL['same_day_stop_or_sale_allowed'])

    def test_future_bars_cannot_change_past_features_or_score(self):
        rows = history_rows()
        before = p.price_action_features(rows)
        changed = copy.deepcopy(rows)
        for source in changed[261:]:
            source.update(open=9, high=9.3, low=8.8, close=9.2, volume=100)
        after = p.price_action_features(changed)
        pd.testing.assert_frame_equal(before.iloc[:261], after.iloc[:261])

    def test_compression_does_not_include_signal_day_range(self):
        rows = history_rows()
        first = p.price_action_features(rows).iloc[-1]
        rows[-1]['high'] += 3
        second = p.price_action_features(rows).iloc[-1]
        self.assertAlmostEqual(first.compression5_20, second.compression5_20)
        self.assertNotEqual(first.range_atr, second.range_atr)

    def test_snapshot_missing_market_context_cannot_signal(self):
        result = p.price_action_snapshot(history_rows())
        self.assertTrue(result['available'])
        self.assertFalse(result['market_context_known'])
        self.assertFalse(result['research_signal'])
        self.assertFalse(result['score_is_probability'])
        self.assertGreaterEqual(result['price_action_score'], 0)
        self.assertLessEqual(result['price_action_score'], 100)

    def test_snapshot_invalid_trailing_bar_fails_closed(self):
        rows = history_rows()
        rows[-10]['conflict'] = 1
        result = p.price_action_snapshot(rows)
        self.assertFalse(result['available'])
        self.assertFalse(result['research_signal'])

    def test_auxiliary_three_day_label_is_independent_of_trade_exit(self):
        days = ['2026-01-02', '2026-01-05', '2026-01-06']
        bars = {days[0]: bar(days[0], 7, 7.2, 6.9, 7.1),
                days[1]: bar(days[1], 7.1, 7.5, 7, 7.3),
                days[2]: bar(days[2], 7.3, 7.6, 7.2, 7.4)}
        result = p.auxiliary_labels(*days, bars)
        self.assertEqual(result['strength3_label'], 1)
        del bars[days[2]]
        missing = p.auxiliary_labels(*days, bars)
        self.assertIsNone(missing['strength3_label'])
        self.assertEqual(missing['strength3_status'], 'unresolved')

    def test_three_day_down_label_not_retroactive_entry_filter(self):
        source = candidate(day1_close=6.9, day2_open=6.9, day2_high=7, day2_low=6.7, day2_close=6.8)
        source['strength3_label'] = 0
        records = p.simulate(pd.DataFrame([source]), 'higher_lows')
        report = p.metrics(records, bootstrap_samples=100)
        self.assertEqual(report['n_attempts'], 1)
        self.assertEqual(report['n_losses'], 1)
        self.assertEqual(report['strength3_rate'], 0)

    def test_buy_cap_is_2p5_percent_not_prior_3_percent(self):
        source = candidate(buy=7.19)
        records = p.simulate(pd.DataFrame([source]))
        self.assertEqual(records[0]['status'], 'no_fill')
        self.assertAlmostEqual(records[0]['frozen_buy_limit_cap'], 7.175)
        self.assertEqual(p.metrics(records)['n_cancelled'], 1)

    def test_open_savings_and_cancelled_order_not_reallocated(self):
        sources = [candidate(symbol='000001.SZ', buy=7.3),
                   candidate(symbol='000002.SZ', buy=5.1),
                   candidate(symbol='000003.SZ', buy=5.2)]
        records = p.simulate(pd.DataFrame(sources))
        self.assertEqual(len(records), 3)
        self.assertEqual([source['frozen_order_quantity'] for source in records], [1300]*3)
        self.assertEqual(records[0]['status'], 'no_fill')

    def test_unknown_entry_reserves_frozen_cash_and_blocks_capacity(self):
        sources = []
        for index in range(3):
            source = candidate(symbol=f'{index:06}.SZ')
            source.update(outcome_status='unresolved', entry_one_price=True,
                          reason='one_price_entry_execution_unresolved')
            sources.append(source)
        sources.append(candidate(signal='2026-01-05', entry='2026-01-06',
                                 exit_='2026-01-07', symbol='999999.SZ'))
        records = p.simulate(pd.DataFrame(sources))
        attempts = [source for source in records if source['status']=='unresolved']
        self.assertEqual(len(attempts), 3)
        self.assertAlmostEqual(attempts[0]['reserved_cash'], 1300*7.175)
        self.assertEqual(records[-1]['status'], 'abstain')

    def test_D2_proceeds_not_available_for_D2_open(self):
        sources = [candidate(symbol=f'{index:06}.SZ') for index in range(3)]
        sources.append(candidate(signal='2026-01-02', entry='2026-01-05',
                                 exit_='2026-01-06', symbol='999999.SZ'))
        records = p.simulate(pd.DataFrame(sources))
        self.assertEqual(records[-1]['status'], 'abstain')

    def test_target_stop_ambiguity_remains_conservative(self):
        source = candidate(day2_open=7, day2_high=7.5, day2_low=6.7, day2_close=7.2)
        records = p.simulate(pd.DataFrame([source]))
        self.assertTrue(records[0]['ambiguous_same_bar'])
        self.assertLess(records[0]['gross_return'], 0)
        report = p.metrics(records, bootstrap_samples=100)
        self.assertEqual(report['win_rate'], 0)
        self.assertEqual(report['optimistic_win_rate_upper_proxy'], 1)

    def test_missing_D3_does_not_remove_known_D2_trade(self):
        source = candidate()
        source['strength3_label'] = None
        records = p.simulate(pd.DataFrame([source]))
        report = p.metrics(records, bootstrap_samples=100)
        self.assertEqual(report['n_closed'], 1)
        self.assertEqual(report['n_strength3_unknown'], 1)
        self.assertEqual(report['strength3_rate'], 0)
        self.assertIsNone(report['strength3_known_rate'])

    def test_purging_uses_D3_not_only_D2(self):
        dates = [(dt.date(2025, 1, 1)+dt.timedelta(days=index)).isoformat() for index in range(205)]
        frame = pd.DataFrame({'date': dates[:200], 'outcome_date': dates[2:202], 'day3_date': dates[3:203]})
        chunks = p.chronological_split(frame)
        self.assertLess(chunks['development'].day3_date.max(), chunks['selection'].date.min())
        self.assertLess(chunks['selection'].day3_date.max(), chunks['diagnostic'].date.min())
        self.assertNotIn(dates[117], list(chunks['development'].date))

    def test_score_is_bounded_and_not_fitted_probability(self):
        frame = pd.DataFrame([candidate(), dict(candidate(), ret20=5, relative_return5=3,
            efficiency20=1, close_location=1, range_atr=.01, compression5_20=.01)])
        scores = p.add_score(frame).price_action_score
        self.assertTrue(scores.between(0, 100).all())

    def test_complete_readonly_research_writes_auditable_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'market.db'
            base = '2026-10-16'
            final = dt.date.fromisoformat(base)
            with sqlite3.connect(source) as db:
                db.execute(f'PRAGMA application_id={p.c.APP_ID}')
                db.executescript('CREATE TABLE daily_quotes(symbol TEXT,trade_date TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,amount REAL,conflict INTEGER); CREATE TABLE security_master(symbol TEXT,name TEXT);')
                for security in range(3):
                    symbol = f'{security:06}.SZ'
                    db.execute('INSERT INTO security_master VALUES (?,?)', (symbol, 'Fixture only'))
                    rows = []
                    for index in range(500):
                        day = (final-dt.timedelta(days=499-index)).isoformat()
                        price = 6+security*.1+index*.001+.02*np.sin(index/4)
                        rows.append((symbol, day, price, price+.08, price-.06,
                                     price+.03, 1000000, 30000000, 0))
                    db.executemany('INSERT INTO daily_quotes VALUES (?,?,?,?,?,?,?,?,?)', rows)
            db.close()
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            output = Path(directory)/'research'
            with patch('builtins.print'):
                result = p.run(source, base, output, digest)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
            self.assertFalse(result['formal_trade_ready'])
            self.assertEqual(result['prediction_count'], 3)
            self.assertEqual(len(result['selection_rules']), 3)
            self.assertTrue((output/'summary.json').is_file())
            self.assertTrue((output/'predictions.json').is_file())
            self.assertTrue((output/'diagnostic_trades.csv.gz').is_file())


if __name__ == '__main__':
    unittest.main()
