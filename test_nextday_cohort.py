"""Causal seals, matched cohorts, actual board leadership, and no fill claims."""
import copy
import datetime as dt
from decimal import Decimal, ROUND_FLOOR
import json
import sqlite3
import unittest
from unittest import mock

import nextday_cohort as cohort

BASE = '2026-10-16'
TARGET = '2026-10-19'
CUTOFF = '2026-10-18T14:00:00+08:00'
OBSERVED = '2026-10-18T12:00:00+08:00'


def fixtures():
    db = sqlite3.connect(':memory:')
    db.execute('PRAGMA application_id=1095977041')
    db.execute('''CREATE TABLE daily_quotes(symbol TEXT,trade_date TEXT,open REAL,
        high REAL,low REAL,close REAL,volume REAL,amount REAL,conflict INTEGER,
        fetched_at TEXT,source_updated_at TEXT,finality TEXT)''')
    frozen, candidates = {'板块甲': [], '板块乙': []}, []
    for index in range(24):
        symbol = f'{index + 1:06}.SZ'
        close = 8 + index * .1
        # All are positive; the first few are leaders of each entire board.
        daily_rate = .001 + (11 - index % 12) * .0003
        for offset in range(30):
            day = (dt.date.fromisoformat(BASE) - dt.timedelta(days=29-offset)).isoformat()
            value = close / (1 + daily_rate) ** (29-offset)
            db.execute('INSERT INTO daily_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (symbol, day, value - .01, value + .05, value - .08, value,
                 5e6, 40e6, 0, OBSERVED, day + 'T15:00:00+08:00', 'historical_provider'))
        name = '板块甲' if index < 12 else '板块乙'
        frozen[name].append({'symbol': symbol, 'base_close': close,
            'membership_effective_date': BASE, 'membership_observed_at': OBSERVED})
        cap = float((Decimal(str(close)) * Decimal('1.025')).quantize(Decimal('.01'), rounding=ROUND_FLOOR))
        candidates.append({'symbol': symbol, 'name': '样本' + symbol, 'close': close,
            'feature_last_session': BASE, 'research_score': 100-index,
            'frozen_buy_cap': cap, 'features': {'ret5': 999}, 'sector': '无效旧映射'})
    outlook = {'schema': 'sector-outlook-v1', 'status': 'ready', 'classification': 'sina_industry',
        'base_date': BASE, 'target_date': TARGET, 'information_cutoff': CUTOFF,
        'frozen_members': frozen, 'constituents_sha256': cohort._digest(frozen),
        'benchmark': {'base_return_1d': .001, 'base_return_5d': .001, 'base_breadth': .5},
        'rankings': [{'sector': name, 'score': 1, 'coverage': 1,
            'components': {'return_1d': .01, 'return_5d': .02, 'relative_return_5d': .019,
                'breadth': 1, 'breadth_excess': .5, 'above_ma5': 1,
                'participation': 1, 'close_location': .7}}
            for name in frozen]}
    plan = {'base_date': BASE, 'target_date': TARGET, 'information_cutoff': CUTOFF,
            'candidate_pool': candidates, 'watchlist': candidates[-5:]}
    flow = {'schema': 'sector-fund-flow-v1', 'classification': 'eastmoney_industry',
        'base_date': BASE, 'status': 'ready', 'information_cutoff': CUTOFF,
        'sectors': [{'code': 'BK001', 'name': '板块甲', 'trade_date': BASE,
            'observed_at': OBSERVED, 'source_updated_at': BASE+'T15:00:00+08:00',
            'main_net_inflow_yuan': 1e8, 'main_net_ratio_pct': 5,
            'membership_status': 'complete', 'vendor_member_total': 12,
            # Same display name but a completely different, verified membership.
            'members': [{'symbol': row['symbol'], 'effective_date': BASE, 'observed_at': OBSERVED}
                        for row in frozen['板块乙']]}]}
    return db, outlook, plan, flow


class FrozenCohortTests(unittest.TestCase):
    def setUp(self):
        clock = mock.patch('nextday_cohort._now', return_value=dt.datetime.fromisoformat('2026-10-20T15:00:00+08:00'))
        clock.start()
        self.addCleanup(clock.stop)
        self.db, self.outlook, self.plan, self.flow = fixtures()
        self.addCleanup(self.db.close)

    def build(self, **kwargs):
        return cohort.build(self.db, BASE, TARGET, CUTOFF, self.outlook, self.plan, **kwargs)

    def test_base_top12_and_same_cohort_mask_not_another_watchlist(self):
        result = self.build()
        rows = result['base_candidates']
        self.assertEqual([row['symbol'] for row in rows], [f'{index:06}.SZ' for index in range(1, 13)])
        self.assertEqual([row['symbol'] for row in rows if row['confluence_eligible']],
                         [f'{index:06}.SZ' for index in range(1, 5)])
        self.assertEqual(result['coverage']['base_cohort_count'], 12)
        self.assertEqual(result['coverage']['market_gate_passed_count'], 12)
        self.assertEqual(result['coverage']['confluence_count'], 4)
        self.assertEqual(result['D2_trade_date'], '2026-10-20')
        self.assertTrue(result['forward_eligible'])
        # Existing sector score=1 and an absurd supplied ret5 do not define the new gate.
        self.assertTrue(rows[0]['sector_gate_eligible'])
        self.assertLess(rows[0]['stock_features']['return_5d'], .1)

    def test_optional_candidate_limit_preserves_default_protocol_and_separate_hash(self):
        default = self.build()
        explicit = self.build(max_candidates=12)
        self.assertEqual(default, explicit)
        self.assertEqual(default['protocol'], cohort.PROTOCOL)
        expanded = self.build(max_candidates=300)
        self.assertEqual(len(expanded['base_candidates']), 24)
        self.assertEqual(expanded['protocol']['universe'],
                         'top300_valid_opening_pool_by_research_score_desc_then_symbol')
        self.assertNotEqual(default['protocol_sha256'], expanded['protocol_sha256'])
        self.assertEqual(cohort.PROTOCOL['universe'],
                         'top12_valid_opening_pool_by_research_score_desc_then_symbol')
        for invalid in (True, 0, -1, 301, 12.0, '12', None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.build(max_candidates=invalid)

    def test_honest_research_protocol_and_no_probability_or_actual_fill(self):
        result = self.build()
        self.assertFalse(result['formal_trade_ready'])
        self.assertIsNone(result['actual_win_rate'])
        self.assertFalse(result['execution_win_rate_80_validated'])
        self.assertFalse(result['reference_fills_are_actual_fills'])
        self.assertFalse(result['actual_orders_submitted'])
        self.assertEqual(result['hypothesis_status'], 'fixed_prospective_hypotheses_not_validated')
        self.assertEqual(result['generated_at'], CUTOFF)
        self.assertEqual(len(result['protocol']['arms']), 6)
        self.assertEqual(result['protocol']['fees_cny'], 0)
        self.assertFalse(result['protocol']['intraday_profit_target_exits'])
        self.assertEqual(result['protocol']['primary_outcome'], 'D2_exit_reference_price>D1_entry_reference_price')
        self.assertEqual(result['protocol']['exit_decision_time'], '14:50')
        self.assertFalse(any('probability' in key and key != 'score_is_probability'
                             for row in result['base_candidates'] for key in row))
        json.dumps(result, allow_nan=False)

    def test_future_prices_and_outcome_labels_cannot_change_frozen_identity(self):
        before = self.build(input_sha256='before-database-bytes')
        self.db.execute('INSERT INTO daily_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            ('000001.SZ', TARGET, 50, 100, 1, 80, 1e10, 1e12, 0,
             TARGET+'T15:01:00+08:00', TARGET+'T15:00:00+08:00', 'historical_provider'))
        self.plan['future_win_rate'] = .99
        self.plan['candidate_pool'][0].update(D2_close=999, prediction_probability=.99,
                                             actual_win=True, unknown_extra={'future_label': True})
        self.outlook['rankings'][0]['future_return'] = -.9
        self.outlook['frozen_members']['板块甲'][0]['future_label'] = 999
        after = self.build(input_sha256='after-database-bytes')
        for key in ('cohort_id', 'cohort_sha256', 'protocol_sha256', 'base_candidates', 'sector_boards'):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(before['input_database_sha256'], 'before-database-bytes')
        self.assertEqual(after['input_database_sha256'], 'after-database-bytes')

    def test_input_order_independence_and_no_mutation(self):
        original = copy.deepcopy((self.outlook, self.plan, self.flow))
        first = self.build(fund_flow=self.flow)
        self.assertEqual((self.outlook, self.plan, self.flow), original)
        self.plan['candidate_pool'].reverse()
        self.assertEqual(first['cohort_id'], self.build(fund_flow=self.flow)['cohort_id'])
        self.plan['candidate_pool'][0]['research_score'] = 100
        # Deterministic tie break uses symbol, not original pool order.
        self.assertEqual(self.build()['base_candidates'][0]['symbol'], '000001.SZ')

    def test_whitelisted_used_feature_changes_do_change_seal(self):
        before = self.build()['cohort_id']
        self.outlook['benchmark']['base_breadth'] = .49
        after = self.build()
        self.assertNotEqual(before, after['cohort_id'])
        self.assertEqual(after['coverage']['confluence_count'], 0)
        self.assertEqual(after['coverage']['base_cohort_count'], 12)
        self.assertEqual(after['coverage']['market_gate_passed_count'], 0)

    def test_market_unknown_and_each_fixed_market_gate_fail_closed(self):
        for field, value in (('base_breadth', None), ('base_return_1d', -.001),
                             ('base_return_5d', -.001), ('base_breadth', .49)):
            with self.subTest(field=field, value=value):
                old = self.outlook['benchmark'][field]
                self.outlook['benchmark'][field] = value
                result = self.build()
                self.assertEqual(len(result['base_candidates']), 12)
                self.assertFalse(result['market_gate']['eligible'])
                self.assertEqual(result['coverage']['confluence_count'], 0)
                self.outlook['benchmark'][field] = old

    def test_wrong_sector_dates_or_unsealed_membership_cannot_donate_benchmark(self):
        for mutation in ('wrong_target', 'future_cutoff', 'bad_hash', 'missing_taxonomy'):
            with self.subTest(mutation=mutation):
                old = copy.deepcopy(self.outlook)
                if mutation == 'wrong_target': self.outlook['target_date'] = '2026-10-20'
                elif mutation == 'future_cutoff': self.outlook['information_cutoff'] = TARGET+'T09:00:00+08:00'
                elif mutation == 'bad_hash': self.outlook['constituents_sha256'] = 'bad'
                else: self.outlook['classification'] = None
                result = self.build()
                self.assertFalse(result['market_gate']['known'])
                self.assertEqual(result['coverage']['confluence_count'], 0)
                self.assertEqual(len(result['base_candidates']), 12)
                self.outlook = old

    def test_flow_taxonomy_is_matched_by_actual_membership_not_chinese_name(self):
        first = self.build(fund_flow=self.flow)
        self.assertTrue(all(not row['flow_known'] for row in first['base_candidates']))
        self.assertTrue(all(row['evidence_tier'] == 'technical_only_flow_unknown'
                            for row in first['base_candidates']))
        self.plan['candidate_pool'] = self.plan['candidate_pool'][12:]
        second = self.build(fund_flow=self.flow)
        self.assertTrue(all(row['flow_known'] for row in second['base_candidates']))
        self.assertTrue(all(row['sector_classification'] == 'eastmoney_industry'
                            for row in second['base_candidates']))
        self.assertTrue(all(row['sector_identity']['sector_code'] == 'BK001'
                            for row in second['base_candidates']))

    def test_stale_future_intraday_or_wrong_taxonomy_flow_gets_no_credit(self):
        self.plan['candidate_pool'] = self.plan['candidate_pool'][12:]
        for mutation in ('stale', 'future', 'intraday', 'wrong_taxonomy', 'truncated', 'future_members'):
            with self.subTest(mutation=mutation):
                flow = copy.deepcopy(self.flow)
                row = flow['sectors'][0]
                if mutation == 'stale': flow['base_date'] = '2026-10-15'
                elif mutation == 'future': row['observed_at'] = TARGET+'T15:00:00+08:00'
                elif mutation == 'intraday': row['source_updated_at'] = BASE+'T14:59:00+08:00'
                elif mutation == 'wrong_taxonomy': flow['classification'] = 'sina_industry'
                elif mutation == 'truncated': row['vendor_member_total'] = 100
                else: row['members'][0]['observed_at'] = TARGET+'T15:00:00+08:00'
                result = self.build(fund_flow=flow)
                self.assertTrue(all(not item['flow_credit'] for item in result['base_candidates']))
                self.assertTrue(all(not item['flow_known'] for item in result['base_candidates']))

    def test_negative_real_flow_fails_vendor_gate_without_hiding_in_fallback(self):
        self.plan['candidate_pool'] = self.plan['candidate_pool'][12:]
        self.flow['sectors'][0].update(main_net_inflow_yuan=-1e8, main_net_ratio_pct=-5)
        result = self.build(fund_flow=self.flow)
        self.assertTrue(all(row['flow_known'] for row in result['base_candidates']))
        self.assertEqual(result['coverage']['confluence_count'], 0)
        self.assertTrue(all('same_vendor_actual_flow_not_positive' in row['reasons']
                            for row in result['base_candidates']))

    def test_leadership_is_rank_of_entire_board_not_prefiltered_pool(self):
        # This is the only candidate in its board, but it ranks 12/12 there.
        self.plan['candidate_pool'] = [self.plan['candidate_pool'][11]]
        result = self.build()
        row = result['base_candidates'][0]
        self.assertEqual(row['leadership_rank'], 12)
        self.assertEqual(row['leadership_member_count'], 12)
        self.assertFalse(row['leadership_gate_eligible'])
        self.assertFalse(row['confluence_eligible'])

    def test_future_history_or_membership_is_never_point_in_time_backfilled(self):
        self.db.execute('UPDATE daily_quotes SET fetched_at=? WHERE symbol=?',
                        (TARGET+'T15:00:00+08:00', '000001.SZ'))
        result = self.build()
        self.assertNotIn('000001.SZ', [row['symbol'] for row in result['base_candidates']])
        self.assertEqual(result['base_candidates'][0]['symbol'], '000002.SZ')
        for member in self.outlook['frozen_members']['板块甲']:
            member['membership_observed_at'] = TARGET+'T15:00:00+08:00'
        self.outlook['constituents_sha256'] = cohort._digest(self.outlook['frozen_members'])
        result = self.build()
        self.assertTrue(all(row['sector'] is None for row in result['base_candidates'] if row['symbol'] < '000013.SZ'))

    def test_invalid_history_reduces_actual_board_coverage(self):
        for number in range(5, 13):
            self.db.execute('UPDATE daily_quotes SET fetched_at=? WHERE symbol=?',
                            (TARGET+'T15:00:00+08:00', f'{number:06}.SZ'))
        result = self.build()
        first = result['base_candidates'][0]
        self.assertFalse(first['sector_gate_eligible'])
        self.assertIn('sector_valid_members_below_5', first['reasons'])
        self.assertIn('sector_history_coverage_below_0.65_or_unknown', first['reasons'])

    def test_price_boundaries_and_lot_budget_use_exact_cents(self):
        for close, floor, cap in ((5, 5, 5.12), (8.01, 7.93, 8.21), (100, 99, 100)):
            with self.subTest(close=close):
                actual_floor, actual_cap, quantity = cohort._bounds(close)
                self.assertEqual((actual_floor, actual_cap), (floor, cap))
                self.assertGreaterEqual(quantity, 100)
                self.assertEqual(quantity % 100, 0)
                self.assertLessEqual(Decimal(str(actual_cap)) * quantity, 10000)
                self.assertGreater(Decimal(str(actual_cap)) * (quantity+100), 10000)
        self.plan['candidate_pool'][0]['frozen_buy_cap'] = 999
        self.plan['candidate_pool'][1]['close'] = 4.99
        result = self.build()
        self.assertEqual(result['coverage']['rejections']['candidate_frozen_cap_differs_from_protocol'], 1)
        self.assertEqual(result['coverage']['rejections']['invalid_price_or_research_score'], 1)

    def test_wrong_opening_plan_and_missing_pool_keep_empty_reason_and_counts(self):
        self.plan['target_date'] = '2026-10-20'
        result = self.build()
        self.assertEqual(result['base_candidates'], [])
        self.assertEqual(result['reason'], 'opening_plan_missing_or_date_cutoff_mismatch')
        self.assertEqual(result['coverage']['market_gate_passed_count'], 0)
        self.plan['target_date'] = TARGET
        self.plan.pop('candidate_pool')
        result = self.build()
        self.assertEqual(result['base_candidates'], [])
        self.assertIn('no_watchlist_substitution', result['reason'])

    def test_forward_eligibility_requires_adjacent_session_and_0915_freeze(self):
        late = cohort.build(self.db, BASE, TARGET, TARGET+'T09:15:00+08:00', self.outlook, self.plan)
        self.assertFalse(late['forward_eligible'])
        self.assertIn('freeze_not_before_target_09_15', late['forward_ineligible_reasons'])
        wrong = cohort.build(self.db, BASE, '2026-10-20', CUTOFF, self.outlook, self.plan)
        self.assertFalse(wrong['forward_eligible'])
        self.assertIn('target_not_adjacent_verified_session', wrong['forward_ineligible_reasons'])
        with self.assertRaises(ValueError):
            cohort.build(self.db, BASE, TARGET, '2026-10-18T14:00:00', self.outlook, self.plan)
        with self.assertRaises(ValueError):
            cohort.build(self.db, BASE, TARGET, BASE+'T14:00:00+08:00', self.outlook, self.plan)
        with self.assertRaises(ValueError):
            cohort.build(self.db, BASE, TARGET, '2099-01-01T14:00:00+08:00', self.outlook, self.plan)


if __name__ == '__main__':
    unittest.main()
