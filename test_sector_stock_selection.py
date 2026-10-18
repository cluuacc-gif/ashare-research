"""Causal joins, genuine-flow evidence and frozen capital for the sector screen."""
import copy
import datetime as dt
import json
import sqlite3
import unittest
from unittest import mock

import sector_stock_selection as screen

BASE = '2026-10-16'
TARGET = '2026-10-19'
CUTOFF = '2026-10-18T15:00:00+08:00'
OBSERVED = '2026-10-18T12:00:00+08:00'


def fixtures():
    db = sqlite3.connect(':memory:')
    db.execute('PRAGMA application_id=1095977041')
    db.execute('''CREATE TABLE daily_quotes(symbol TEXT,trade_date TEXT,open REAL,
        high REAL,low REAL,close REAL,volume REAL,amount REAL,conflict INTEGER,
        fetched_at TEXT,source_updated_at TEXT,finality TEXT)''')
    symbols = [f'{index + 1:06}.SZ' for index in range(16)]
    for index, symbol in enumerate(symbols):
        for offset in range(90):
            day = (dt.date.fromisoformat(BASE) - dt.timedelta(days=89 - offset)).isoformat()
            close = 6.3 + index * .1 + offset * .006
            db.execute('INSERT INTO daily_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (symbol, day, close - .02, close + .05, close - .10, close,
                 5e6, 35e6, 0, OBSERVED, day + 'T15:00:00+08:00', 'historical_provider'))
    frozen = {}
    rankings = []
    for number, name in enumerate(('强板块甲', '强板块乙', '弱板块丙')):
        frozen[name] = [{'symbol': symbol, 'base_close': 6.834 + index * .1,
            'membership_effective_date': BASE, 'membership_observed_at': OBSERVED}
            for index, symbol in enumerate(symbols) if number * 5 <= index < (number + 1) * 5]
        rankings.append({'sector': name, 'score': 85 - number * 15, 'coverage': 1,
            'components': {'return_1d': .01, 'return_5d': .03,
                'relative_return_5d': .02 if number < 2 else -.03,
                'breadth': .7 if number < 2 else .3,
                'breadth_excess': .2 if number < 2 else -.2,
                'above_ma5': .8, 'participation': .5, 'close_location': .7}})
    outlook = {'schema': 'sector-outlook-v1', 'status': 'ready',
        'classification': 'sina_industry', 'base_date': BASE, 'target_date': TARGET,
        'information_cutoff': CUTOFF, 'frozen_members': frozen,
        'constituents_sha256': screen._digest(frozen), 'rankings': rankings,
        'benchmark': {'base_return_1d': 0, 'base_return_5d': .005, 'base_breadth': .5}}
    candidates = []
    for index, symbol in enumerate(symbols):
        close = 6.834 + index * .1
        candidates.append({'symbol': symbol, 'name': '样本' + symbol, 'close': close,
            'feature_last_session': BASE, 'sector': '错误旧映射', 'research_score': 99 - index,
            'frozen_buy_cap': round(close * 1.025, 2),
            'features': {'close_location': .7, 'ma20_gap': .02, 'ret5': .01,
                'amount_ratio20': 1.1, 'median_amount20': 35e6}, 'pattern': '温和转强'})
    plan = {'base_date': BASE, 'target_date': TARGET, 'information_cutoff': CUTOFF,
            'candidate_pool': candidates, 'watchlist': [candidates[-1]]}
    flow = {'schema': 'sector-fund-flow-v1', 'classification': 'eastmoney_industry',
        'base_date': BASE, 'status': 'ready', 'information_cutoff': CUTOFF,
        'sectors': [{'code': 'BK0001', 'name': '强板块甲', 'trade_date': BASE,
            'source_updated_at': BASE + 'T15:00:00+08:00', 'observed_at': OBSERVED,
            'main_net_inflow_yuan': 1e8, 'main_net_ratio_pct': 5,
            'main_net_3d_yuan': 2e8, 'main_net_5d_yuan': 2e8,
            'main_net_10d_yuan': 3e8, 'membership_status': 'complete',
            'vendor_member_total': 5,
            'members': [{'symbol': symbol, 'effective_date': BASE,
                'observed_at': OBSERVED, 'source_url': 'https://example.invalid/vendor',
                'flow_status': 'verified_current_date', 'flow_trade_date': BASE,
                'flow_observed_at': OBSERVED, 'source_updated_at': BASE + 'T15:00:00+08:00',
                'main_net_inflow_yuan': 10e6}
                for symbol in symbols[5:10]]}]}
    return db, outlook, plan, flow


class SectorStockSelectionTests(unittest.TestCase):
    def setUp(self):
        self.db, self.outlook, self.plan, self.flow = fixtures()
        self.addCleanup(self.db.close)
        # The separate mathematical engine owns its formula tests. This fixture
        # isolates joins/budget policy without making synthetic returns a model fit.
        patcher = mock.patch('sector_stock_selection._stock_action', return_value={
            'available': True, 'price_action_score': 80, 'eligible_patterns': ['higher_lows'],
            'atr14_pct': .025, 'score_is_probability': False})
        patcher.start()
        self.addCleanup(patcher.stop)

    def generate(self, flow=None):
        return screen.generate(self.db, BASE, TARGET, CUTOFF, self.outlook, self.plan, flow)

    def test_primary_stocks_are_members_of_strong_sector_not_old_top_five(self):
        result = self.generate()
        self.assertEqual(result['status'], 'conditional_research_only')
        self.assertEqual(len(result['primary']), 3)
        self.assertNotIn('000016.SZ', [row['symbol'] for row in result['watchlist']])
        self.assertTrue(all(row['sector'] in ('强板块甲', '强板块乙') for row in result['watchlist']))
        self.assertFalse(result['flow_integrated'])
        self.assertFalse(result['win_rate_80_verified'])
        self.assertIsNone(result['gross_trade_win_rate'])
        self.assertFalse(result['formal_trade_eligible'])
        json.dumps(result, ensure_ascii=False, allow_nan=False)

    def test_same_name_different_taxonomy_does_not_transfer_fund_flow(self):
        result = self.generate(self.flow)
        flow_primary = [row for row in result['primary'] if row['flow_integrated']]
        self.assertTrue(flow_primary)
        self.assertTrue(all(row['symbol'] in [f'{number:06}.SZ' for number in range(6, 11)]
                            for row in flow_primary))
        self.assertTrue(all(row['sector_classification'] == 'eastmoney_industry' for row in flow_primary))
        for row in result['watchlist']:
            if row['symbol'] in [f'{number:06}.SZ' for number in range(1, 6)]:
                self.assertFalse(row['flow_integrated'])
                self.assertIsNone(row['sector_fund_flow'])

    def test_flow_without_constituents_remains_separate_table(self):
        self.flow['sectors'][0]['members'] = []
        self.flow['sectors'][0]['membership_status'] = 'unavailable'
        result = self.generate(self.flow)
        self.assertEqual(result['flow_status'], 'verified_vendor_flow_separate_taxonomy')
        self.assertEqual(result['actual_fund_flow'][0]['main_net_inflow_yuan'], 1e8)
        self.assertFalse(result['flow_integrated'])
        self.assertTrue(all(not row['flow_integrated'] for row in result['watchlist']))

    def test_future_or_stale_money_flow_is_never_joined(self):
        for mutation in ('wrong_base', 'future_payload', 'future_row', 'wrong_row_date', 'intraday'):
            with self.subTest(mutation=mutation):
                flow = copy.deepcopy(self.flow)
                if mutation == 'wrong_base':
                    flow['base_date'] = '2026-10-15'
                elif mutation == 'future_payload':
                    flow['information_cutoff'] = '2026-10-19T09:31:00+08:00'
                elif mutation == 'future_row':
                    flow['sectors'][0]['observed_at'] = '2026-10-19T09:31:00+08:00'
                elif mutation == 'wrong_row_date':
                    flow['sectors'][0]['trade_date'] = '2026-10-15'
                else:
                    flow['sectors'][0]['source_updated_at'] = BASE + 'T14:00:00+08:00'
                result = self.generate(flow)
                self.assertFalse(result['flow_integrated'])
                self.assertEqual(result['actual_fund_flow'], [])

    def test_partial_future_or_truncated_membership_cannot_create_flow_board(self):
        for mutation in ('future_member', 'partial', 'truncated'):
            with self.subTest(mutation=mutation):
                flow = copy.deepcopy(self.flow)
                row = flow['sectors'][0]
                if mutation == 'future_member':
                    row['members'][0]['observed_at'] = '2026-10-19T09:31:00+08:00'
                elif mutation == 'partial':
                    row['membership_status'] = 'partial'
                else:
                    row['vendor_member_total'] = 500
                result = self.generate(flow)
                self.assertFalse(result['flow_integrated'])
                self.assertEqual(len(result['actual_fund_flow']), 1)

    def test_negative_actual_flow_does_not_receive_positive_flow_ranking(self):
        self.flow['sectors'][0]['main_net_inflow_yuan'] = -1e8
        self.flow['sectors'][0]['main_net_ratio_pct'] = -5
        result = self.generate(self.flow)
        self.assertTrue(result['flow_integrated'])  # Valid evidence is retained.
        self.assertTrue(all(not row['flow_integrated'] for row in result['primary']))
        board = next(row for row in result['sector_board'] if row['classification'] == 'eastmoney_industry')
        self.assertFalse(board['eligible_for_stock_selection'])

    def test_individual_flow_unknown_or_outflow_is_never_primary(self):
        members = self.flow['sectors'][0]['members']
        members[0]['main_net_inflow_yuan'] = -10e6
        members[1]['flow_observed_at'] = '2026-10-19T09:31:00+08:00'
        members[2]['flow_trade_date'] = '2026-10-15'
        result = self.generate(self.flow)
        primary = {row['symbol'] for row in result['primary']}
        self.assertTrue(primary.isdisjoint({members[index]['symbol'] for index in range(3)}))
        for row in result['primary']:
            if row['flow_integrated']:
                self.assertEqual(row['individual_fund_flow']['status'], 'positive')

    def test_b_share_exclusions_are_reconciled_not_subtracted_from_flow_values(self):
        row = self.flow['sectors'][0]
        row.update(vendor_member_total=7, excluded_non_ashare_count=2,
                   membership_scope='A_share_only', vendor_membership_scope='all_share_classes')
        result = self.generate(self.flow)
        self.assertTrue(result['flow_integrated'])
        board = next(row for row in result['sector_board'] if row['classification'] == 'eastmoney_industry')
        self.assertEqual(board['member_count'], 5)
        self.assertEqual(board['flow']['main_net_inflow_yuan'], 1e8)
        self.assertEqual(board['flow_scope'], 'vendor_all_share_classes')

    def test_acceleration_compares_daily_means_not_unequal_window_totals(self):
        self.flow['sectors'][0]['main_net_3d_yuan'] = 60e6
        self.flow['sectors'][0]['main_net_5d_yuan'] = 90e6
        result = self.generate(self.flow)
        observed = result['actual_fund_flow'][0]
        self.assertEqual(observed['per_session_acceleration_3d_vs_5d_yuan'], 2e6)
        self.assertTrue(observed['flow_acceleration_verified'])
        self.flow['sectors'][0]['main_net_3d_yuan'] = None
        result = self.generate(self.flow)
        self.assertIsNone(result['actual_fund_flow'][0]['per_session_acceleration_3d_vs_5d_yuan'])
        self.assertFalse(result['actual_fund_flow'][0]['flow_acceleration_verified'])

    def test_frozen_cap_and_three_budgets_do_not_depend_on_old_reserve_quantities(self):
        for row in self.plan['candidate_pool']:
            row.update(frozen_quantity_shares=0, frozen_reserved_cash_cny=0)
        original = copy.deepcopy(self.plan)
        result = self.generate()
        self.assertLessEqual(sum(row['frozen_reserved_cash_cny'] for row in result['watchlist']), 30000)
        sectors = [row['sector'] for row in result['primary']]
        self.assertLessEqual(max(sectors.count(sector) for sector in sectors), 2)
        for row in result['primary']:
            self.assertGreater(row['frozen_quantity_shares'], 0)
            self.assertEqual(row['frozen_quantity_shares'] % 100, 0)
            self.assertLessEqual(row['frozen_buy_cap'], min(row['close'] * 1.025, 10))
            self.assertLessEqual(row['frozen_reserved_cash_cny'], 10000)
            self.assertFalse(row['risk_exit_reference']['d1_exit_allowed'])
            self.assertEqual(row['risk_exit_reference']['available_session'], 'D2')
            self.assertEqual(row['risk_exit_reference']['profit_reference_fractions'], [.03, .06])
            self.assertEqual(row['risk_exit_reference']['stop_fraction'], .02)
            self.assertEqual(row['risk_exit_reference']['default_profit_target_fraction'], .03)
            self.assertTrue(row['risk_exit_reference']['stretch_target_requires_separate_predeclared_policy'])
        self.assertTrue(all(row['frozen_quantity_shares'] == 0 for row in result['reserves']))
        self.assertEqual(self.plan['watchlist'], original['watchlist'])

    def test_very_weak_market_keeps_observation_without_primary_budgets(self):
        self.outlook['benchmark'].update(base_breadth=.2, base_return_1d=-.02)
        result = self.generate()
        self.assertTrue(result['market_risk_off'])
        self.assertEqual(result['primary'], [])
        self.assertTrue(all(row['frozen_reserved_cash_cny'] == 0 for row in result['watchlist']))

    def test_tampered_frozen_members_or_missing_sector_evidence_does_not_backfill_stocks(self):
        self.outlook['frozen_members']['强板块甲'][0]['symbol'] = '000016.SZ'
        result = self.generate()
        self.assertEqual(result['watchlist'], [])
        self.assertEqual(result['technical_sector_reason'], 'sector_constituents_hash_unverified')

    def test_target_day_prices_do_not_enter_the_ranking(self):
        before = self.generate()
        self.db.execute('INSERT INTO daily_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            ('000001.SZ', TARGET, 6, 100, 1, 99, 1e8, 1e9, 0,
             TARGET + 'T15:01:00+08:00', TARGET + 'T15:00:00+08:00', 'post_close_provider'))
        self.assertEqual(before, self.generate())

    def test_future_or_conflicting_base_history_excludes_stock(self):
        self.db.execute('UPDATE daily_quotes SET fetched_at=? WHERE symbol=? AND trade_date=?',
            (TARGET + 'T15:01:00+08:00', '000001.SZ', BASE))
        self.db.execute('UPDATE daily_quotes SET conflict=1 WHERE symbol=? AND trade_date=?',
            ('000002.SZ', BASE))
        result = self.generate()
        self.assertTrue(all(row['symbol'] not in ('000001.SZ', '000002.SZ') for row in result['watchlist']))

    def test_future_effective_membership_cannot_be_earlier_frozen_context(self):
        for rows in self.outlook['frozen_members'].values():
            for row in rows:
                row['membership_effective_date'] = TARGET
        self.outlook['constituents_sha256'] = screen._digest(self.outlook['frozen_members'])
        self.assertEqual(self.generate()['watchlist'], [])

    def test_ten_yuan_cap_must_allow_the_required_opening_strength(self):
        symbol = '000001.SZ'
        self.plan['candidate_pool'][0]['close'] = 9.99
        multiplier = 9.99 / 6.834
        self.db.execute('UPDATE daily_quotes SET open=open*?,high=high*?,low=low*?,close=close*? WHERE symbol=?',
                        (multiplier, multiplier, multiplier, multiplier, symbol))
        result = self.generate()
        self.assertNotIn(symbol, [row['symbol'] for row in result['watchlist']])
        self.assertEqual(result['coverage']['rejections']['frozen_cap_cannot_allow_required_opening_strength'], 1)

    def test_no_predefined_price_action_pattern_cannot_be_promoted_by_sector_flow(self):
        with mock.patch('sector_stock_selection._stock_action', return_value={
                'available': True, 'price_action_score': 99, 'eligible_patterns': [], 'atr14_pct': .025}):
            result = self.generate(self.flow)
        self.assertEqual(result['primary'], [])
        self.assertEqual(result['coverage']['rejections']['no_predefined_price_action_structure'], 10)

    def test_early_turn_requires_participation_not_merely_lagging_sector(self):
        row = self.outlook['rankings'][2]
        row['score'] = 70
        row['components'].update(relative_return_5d=-.01, breadth=.65, breadth_excess=.15,
                                 participation=.5, close_location=.75)
        result = self.generate()
        early = next(board for board in result['sector_board'] if board['sector'] == '弱板块丙')
        self.assertEqual(early['phase'], 'emerging')
        self.assertTrue(early['eligible_for_stock_selection'])
        row['components']['participation'] = .1
        result = self.generate()
        early = next(board for board in result['sector_board'] if board['sector'] == '弱板块丙')
        self.assertFalse(early['eligible_for_stock_selection'])


if __name__ == '__main__':
    unittest.main()
