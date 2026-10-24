import copy
import datetime as dt
import json
import sqlite3
import unittest
from unittest import mock

import opening_strength as opening


BASE = '2026-10-16'
TARGET = '2026-10-19'
CUTOFF = '2026-10-18T14:00:00+08:00'
OBSERVED = '2026-10-17T12:00:00+08:00'


def market():
    db = sqlite3.connect(':memory:')
    db.execute('PRAGMA application_id=1095977041')
    db.executescript('''
        CREATE TABLE security_master(symbol TEXT PRIMARY KEY,name TEXT,last_seen_at TEXT);
        CREATE TABLE daily_quotes(symbol TEXT,trade_date TEXT,open REAL,high REAL,
            low REAL,close REAL,volume REAL,amount REAL,conflict INTEGER,
            fetched_at TEXT,source_updated_at TEXT,finality TEXT,PRIMARY KEY(symbol,trade_date));
        CREATE TABLE industry_members(symbol TEXT,classification TEXT,industry TEXT,
            effective_date TEXT,observed_at TEXT,source_url TEXT);
    ''')
    start = dt.date.fromisoformat(BASE) - dt.timedelta(days=269)
    for number in range(8):
        symbol = f'{number + 1:06}.SZ'
        db.execute('INSERT INTO security_master VALUES(?,?,?)', (symbol, f'研究样本{number + 1}', OBSERVED))
        for index in range(270):
            close = 6.5 + number * .2 + index * .001
            day = (start + dt.timedelta(days=index)).isoformat()
            db.execute('INSERT INTO daily_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (symbol, day, close - .02,
                        close + .05, close - .15, close, 5e6, 30e6 + number * 1e6,
                        0, OBSERVED, day + 'T15:00:00+08:00', 'historical_provider'))
        db.execute('INSERT INTO industry_members VALUES(?,?,?,?,?,?)',
                   (symbol, 'sina_industry', '板块甲', BASE, OBSERVED, 'https://example.invalid/source'))
    return db


def snapshot(plan, timestamp='2026-10-19T09:30:10+08:00'):
    quotes = []
    for candidate in plan['watchlist']:
        close = candidate['close']
        quotes.append({'symbol': candidate['symbol'], 'exchange_timestamp': timestamp,
            'received_at': '2026-10-19T09:30:11+08:00',
            'open': close * 1.005, 'last': close * 1.01,
            'prev_close': close, 'ask1': close * 1.011, 'ask1_volume': 1000,
            'status': {'verified': True, 'is_st': False, 'delisting': False, 'trading_status': 'normal'},
            'limits': {'official_verified': True, 'limit_up': close * 1.1, 'limit_down': close * .9}})
    return {'kind': 'opening_quote_snapshot_v1', 'observed_at': '2026-10-19T09:30:12+08:00',
            'quotes': quotes}


class OpeningPlanTests(unittest.TestCase):
    def setUp(self):
        clock = mock.patch('opening_strength._now', return_value=dt.datetime.fromisoformat('2026-10-18T15:00:00+08:00'))
        clock.start()
        self.addCleanup(clock.stop)
        self.db = market()
        self.addCleanup(self.db.close)

    def plan(self, **kwargs):
        return opening.build_plan(self.db, BASE, TARGET, CUTOFF, **kwargs)

    def test_frozen_plan_has_no_trading_or_probability_claim(self):
        plan = self.plan()
        self.assertEqual(plan['base_trade_date'], BASE)
        self.assertEqual(plan['target_trade_date'], TARGET)
        self.assertEqual(len(plan['watchlist']), 5)
        self.assertFalse(plan['live_signal'])
        self.assertFalse(plan['formal_trade_eligible'])
        self.assertFalse(plan['win_rate_80_verified'])
        self.assertIsNone(plan['historical_win_rate'])
        self.assertEqual(plan['opening_confirmation']['status'], 'not_observed')
        json.dumps(plan, allow_nan=False)

    def test_expanded_price_range_keeps_budget_lots_and_does_not_mutate_default_protocol(self):
        original_protocol = copy.deepcopy(opening.PROTOCOL)
        self.db.execute('UPDATE daily_quotes SET open=open*4,high=high*4,low=low*4,close=close*4')
        self.assertEqual(self.plan()['watchlist'], [])
        wide = self.plan(price_min=5, price_max=100, include_pool=True)
        self.assertEqual(len(wide['candidate_pool']), 8)
        self.assertEqual(wide['protocol']['entry_reference_price_range'], [5, 100])
        self.assertEqual(opening.PROTOCOL, original_protocol)
        for row in wide['candidate_pool']:
            self.assertGreater(row['close'], 10)
            self.assertEqual(row['frozen_buy_cap'], opening._cent_floor(min(row['close'] * 1.025, 100)))
            self.assertGreaterEqual(row['frozen_quantity_shares'], 100)
            self.assertEqual(row['frozen_quantity_shares'] % 100, 0)
            self.assertLessEqual(row['frozen_reserved_cash_cny'], 10000)

    def test_unaffordable_one_lot_is_not_a_candidate(self):
        self.db.execute('UPDATE daily_quotes SET open=open*20,high=high*20,low=low*20,close=close*20')
        wide = self.plan(price_max=200)
        self.assertEqual(wide['watchlist'], [])
        self.assertEqual(wide['coverage']['rejections']['one_lot_exceeds_frozen_position_budget'], 8)

    def test_invalid_price_ranges_are_rejected(self):
        for low, high in ((0, 100), (10, 5), (5, float('nan')), (True, 100)):
            with self.subTest(range=(low, high)), self.assertRaises(ValueError):
                self.plan(price_min=low, price_max=high)

    def test_future_session_prices_are_never_features(self):
        before = self.plan()
        self.db.execute('INSERT INTO daily_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            ('000001.SZ', TARGET, 100, 200, 1, 150, 1e9, 1e11, 0,
             '2026-10-19T15:01:00+08:00', '2026-10-19T15:00:00+08:00', 'historical_provider'))
        self.assertEqual(before, self.plan())

    def test_unknown_timestamp_excludes_quote_even_when_price_is_plausible(self):
        self.db.execute('UPDATE daily_quotes SET fetched_at=NULL WHERE symbol=? AND trade_date=?', ('000001.SZ', BASE))
        self.assertNotIn('000001.SZ', [row['symbol'] for row in self.plan()['watchlist']])

    def test_after_cutoff_history_is_not_imported_into_plan(self):
        self.db.execute('UPDATE daily_quotes SET fetched_at=? WHERE symbol=?',
                        ('2026-10-19T15:01:00+08:00', '000001.SZ'))
        self.assertNotIn('000001.SZ', [row['symbol'] for row in self.plan()['watchlist']])

    def test_old_missing_amounts_do_not_discard_complete_recent_liquidity(self):
        before = self.plan()
        recent_start = (dt.date.fromisoformat(BASE) - dt.timedelta(days=20)).isoformat()
        self.db.execute('UPDATE daily_quotes SET amount=NULL WHERE trade_date<?', (recent_start,))
        self.assertEqual(before, self.plan())

    def test_amounts_in_every_one_of_latest_21_bars_are_required(self):
        for days_ago in (0, 1, 20):
            with self.subTest(days_ago=days_ago):
                day = (dt.date.fromisoformat(BASE) - dt.timedelta(days=days_ago)).isoformat()
                self.db.execute('UPDATE daily_quotes SET amount=NULL WHERE trade_date=?', (day,))
                plan = self.plan()
                self.assertEqual(plan['watchlist'], [])
                self.assertEqual(plan['coverage']['rejections']['insufficient_or_invalid_prior_bars'], 8)
                self.db.execute('UPDATE daily_quotes SET amount=30000000 WHERE trade_date=?', (day,))

    def test_missing_unused_older_ohlc_does_not_raise_or_enter_features(self):
        before = self.plan()
        unused_end = (dt.date.fromisoformat(BASE) - dt.timedelta(days=250)).isoformat()
        self.db.execute('UPDATE daily_quotes SET close=NULL,amount=NULL WHERE trade_date<=?', (unused_end,))
        self.assertEqual(before, self.plan())

    def test_unfinalized_or_unknown_source_time_quotes_are_rejected(self):
        self.db.execute('UPDATE daily_quotes SET finality=? WHERE trade_date=?', ('intraday', BASE))
        self.assertEqual(self.plan()['watchlist'], [])
        self.db.execute('UPDATE daily_quotes SET finality=? WHERE trade_date=?', ('post_close_provider', BASE))
        self.db.execute('UPDATE daily_quotes SET source_updated_at=? WHERE trade_date=?',
                        ('2026-10-19T15:00:00+08:00', BASE))
        self.assertEqual(self.plan()['watchlist'], [])

    def test_minimum_history_counts_valid_bars_and_locked_days_excluded(self):
        self.db.execute('DELETE FROM daily_quotes WHERE symbol=? AND trade_date<?', ('000001.SZ', '2026-03-01'))
        self.db.execute('UPDATE daily_quotes SET high=close,low=close,open=close WHERE symbol=? AND trade_date=?',
                        ('000002.SZ', BASE))
        rejected = self.plan()['coverage']['rejections']
        self.assertEqual(rejected['insufficient_or_invalid_prior_bars'], 2)

    def test_name_screen_is_only_preliminary_but_st_is_excluded(self):
        self.db.execute("UPDATE security_master SET name='*ST研究' WHERE symbol='000001.SZ'")
        self.assertNotIn('000001.SZ', [row['symbol'] for row in self.plan()['watchlist']])

    def test_only_three_budgets_no_reserve_reallocation(self):
        rows = self.plan()['watchlist']
        self.assertEqual([row['budget_slot'] for row in rows], [1, 2, None, None, None])
        self.assertLessEqual(sum(row['frozen_reserved_cash_cny'] for row in rows), 30000)
        for row in rows:
            self.assertEqual(row['frozen_quantity_shares'] % 100, 0)
            self.assertLessEqual(row['frozen_reserved_cash_cny'], 10000)
            self.assertLessEqual(row['frozen_buy_cap'], row['close'] * 1.025)
        self.assertEqual([row['frozen_quantity_shares'] for row in rows[2:]], [0, 0, 0])

    def test_membership_after_cutoff_is_not_used(self):
        self.db.execute('UPDATE industry_members SET observed_at=?', ('2026-10-19T09:00:00+08:00',))
        self.assertTrue(all(row['sector'] is None for row in self.plan()['watchlist']))

    def test_sector_dates_and_cutoff_must_match(self):
        forecast = {'base_date': BASE, 'target_date': TARGET, 'information_cutoff': CUTOFF,
                    'classification': 'sina_industry', 'status': 'ready',
                    'rankings': [{'sector': '板块甲', 'score': 85}]}
        good = self.plan(sector_forecast=forecast)
        self.assertTrue(good['sector_overlay']['available'])
        self.assertTrue(all(row['sector_score'] == 85 for row in good['watchlist']))
        forecast['target_date'] = '2026-10-20'
        wrong = self.plan(sector_forecast=forecast)
        self.assertFalse(wrong['sector_overlay']['available'])
        forecast['target_date'] = TARGET
        forecast['information_cutoff'] = '2026-10-19T09:30:00+08:00'
        self.assertFalse(self.plan(sector_forecast=forecast)['sector_overlay']['available'])

    def test_frozen_sector_membership_overrides_incompatible_taxonomy(self):
        forecast = {'base_date': BASE, 'target_date': TARGET, 'information_cutoff': CUTOFF,
            'classification': 'exchange_industry', 'status': 'ready',
            'rankings': [{'sector': '板块乙', 'score': 100}],
            'frozen_members': {'板块乙': [{'symbol': '000001.SZ', 'base_close': 6.769}]}}
        plan = self.plan(sector_forecast=forecast)
        row = next(row for row in plan['watchlist'] if row['symbol'] == '000001.SZ')
        self.assertEqual(row['sector'], '板块乙')
        self.assertEqual(row['sector_score'], 100)

    def test_same_session_morning_cannot_use_close(self):
        with self.assertRaises(ValueError):
            opening.build_plan(self.db, BASE, TARGET, BASE + 'T09:00:00+08:00')

    def test_timezone_and_target_contract_required(self):
        with self.assertRaises(ValueError):
            opening.build_plan(self.db, BASE, TARGET, '2026-10-18T14:00:00')
        with self.assertRaises(ValueError):
            opening.build_plan(self.db, BASE, BASE, CUTOFF)

    def test_future_cutoff_or_plan_first_created_after_open_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'future'):
            opening.build_plan(self.db, BASE, TARGET, '2099-10-18T14:00:00+08:00')
        # This timestamp will remain invalid after that session has occurred too.
        with self.assertRaises(ValueError):
            opening.build_plan(self.db, BASE, TARGET, TARGET + 'T09:31:00+08:00')


class OpeningConfirmationTests(unittest.TestCase):
    def setUp(self):
        clock = mock.patch('opening_strength._now', return_value=dt.datetime.fromisoformat('2026-10-20T10:00:00+08:00'))
        clock.start()
        self.addCleanup(clock.stop)
        self.db = market()
        self.addCleanup(self.db.close)
        self.plan = opening.build_plan(self.db, BASE, TARGET, CUTOFF)
        self.snapshot = snapshot(self.plan)
        self.cutoff = '2026-10-19T09:30:15+08:00'

    def confirm(self):
        return opening.confirm_plan(self.plan, self.snapshot, self.cutoff)

    def test_fresh_verified_evidence_remains_research_not_fill(self):
        result = self.confirm()
        self.assertEqual(result['status'], 'evidence_verified_research_only')
        self.assertEqual(len(result['confirmed']), 2)
        self.assertEqual(len(result['rejected']), 3)
        self.assertFalse(result['actual_fill_verified'])
        self.assertFalse(result['formal_trade_eligible'])
        self.assertFalse(result['live_signal'])

    def test_expanded_range_confirmation_obeys_its_frozen_protocol(self):
        self.db.execute('UPDATE daily_quotes SET open=open*4,high=high*4,low=low*4,close=close*4')
        self.plan = opening.build_plan(self.db, BASE, TARGET, CUTOFF, price_max=100)
        self.snapshot = snapshot(self.plan)
        self.assertEqual(len(self.confirm()['confirmed']), 2)
        self.plan['protocol']['entry_reference_price_range'] = [5, 10]
        self.assertEqual(self.confirm()['confirmed'], [])

    def test_0925_is_only_conditional_watch_even_with_good_prices(self):
        self.cutoff = '2026-10-19T09:25:00+08:00'
        result = self.confirm()
        self.assertEqual(result['status'], 'preopen_watch_only')
        self.assertFalse(result['confirmed'])

    def test_old_collector_outer_timestamp_does_not_verify_symbol(self):
        self.snapshot = {'kind': 'auction_snapshot_0925', 'generated_at': self.cutoff,
                         'tradeable_top': self.snapshot['quotes']}
        self.assertEqual(self.confirm()['reason'], 'timestamped_opening_snapshot_missing')

    def test_no_symbol_timestamp_or_future_timestamp_rejects(self):
        for timestamp in (None, '2026-10-19T15:00:00+08:00', '2026-10-19T09:30:14+08:00'):
            with self.subTest(timestamp=timestamp):
                self.snapshot['quotes'][0]['exchange_timestamp'] = timestamp
                self.assertNotIn(self.plan['watchlist'][0]['symbol'], [r['symbol'] for r in self.confirm()['confirmed']])

    def test_stale_exchange_time_rejects_even_fresh_outer_clock(self):
        self.cutoff = '2026-10-19T09:32:00+08:00'
        self.snapshot['observed_at'] = self.cutoff
        result = self.confirm()
        self.assertFalse(result['confirmed'])

    def test_unverified_status_or_limits_cannot_confirm(self):
        for field in ('status', 'limits'):
            with self.subTest(field=field):
                payload = copy.deepcopy(self.snapshot)
                payload['quotes'][0][field] = {}
                result = opening.confirm_plan(self.plan, payload, self.cutoff)
                self.assertEqual(result['rejected'][0]['reason'], 'official_status_or_limits_not_verified')

    def test_no_ask_or_locked_limit_is_not_an_executable_opportunity(self):
        payload = copy.deepcopy(self.snapshot)
        payload['quotes'][0]['ask1_volume'] = 0
        result = opening.confirm_plan(self.plan, payload, self.cutoff)
        self.assertEqual(result['rejected'][0]['reason'], 'invalid_price_limit_or_visible_ask')
        payload = copy.deepcopy(self.snapshot)
        payload['quotes'][0]['limits']['limit_up'] = payload['quotes'][0]['ask1'] + .01
        result = opening.confirm_plan(self.plan, payload, self.cutoff)
        self.assertEqual(result['rejected'][0]['reason'], 'near_limit_or_inconsistent_visible_ask')

    def test_future_day_high_or_close_cannot_change_confirmation(self):
        before = self.confirm()
        for quote in self.snapshot['quotes']:
            quote.update(high=10000, low=.01, close=10000, volume=1e12)
        self.assertEqual(before, self.confirm())

    def test_gap_and_cap_are_frozen_before_actual_open(self):
        first = self.snapshot['quotes'][0]
        first['open'] = first['prev_close'] * 1.03
        self.assertEqual(self.confirm()['rejected'][0]['reason'], 'opening_gap_outside_frozen_range')
        first['open'] = first['prev_close'] * 1.005
        first['ask1'] = self.plan['watchlist'][0]['frozen_buy_cap'] + .01
        self.assertEqual(self.confirm()['rejected'][0]['reason'], 'price_outside_frozen_cap')

    def test_after_window_or_wrong_day_is_not_late_buy_signal(self):
        self.cutoff = '2026-10-19T09:35:01+08:00'
        self.assertEqual(self.confirm()['reason'], 'outside_frozen_opening_observation_window')
        self.cutoff = '2026-10-20T09:30:15+08:00'
        self.assertEqual(self.confirm()['reason'], 'not_target_session')

    def test_new_plan_after_quote_cannot_retroactively_confirm(self):
        self.plan['information_cutoff'] = '2026-10-19T09:30:16+08:00'
        self.assertEqual(self.confirm()['reason'], 'plan_not_frozen_before_observation')

    def test_future_cutoff_cannot_claim_verified_opening_evidence(self):
        with mock.patch('opening_strength._now', return_value=dt.datetime.fromisoformat(CUTOFF)):
            self.assertEqual(self.confirm()['reason'], 'opening_confirmation_cutoff_is_future')

    def test_plan_first_created_after_open_is_not_an_exante_plan(self):
        self.plan['information_cutoff'] = '2026-10-19T09:30:01+08:00'
        self.assertEqual(self.confirm()['reason'], 'plan_not_frozen_before_session_open')


if __name__ == '__main__':
    unittest.main()
