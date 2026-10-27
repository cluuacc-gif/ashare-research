"""The independent rule is enabled; only verified setups receive frozen lots."""
import copy
import datetime as dt
from decimal import Decimal, ROUND_FLOOR
import json
import sqlite3
import unittest
from unittest import mock

import active_trade_system as active
import nextday_cohort as cohort

BASE = '2026-10-16'
TARGET = '2026-10-19'
CUTOFF = '2026-10-18T14:00:00+08:00'
OBSERVED = '2026-10-18T12:00:00+08:00'


def fixtures():
    db = sqlite3.connect(':memory:')
    db.execute('PRAGMA application_id=1095977041')
    db.execute('CREATE TABLE security_master(symbol TEXT PRIMARY KEY,name TEXT,last_seen_at TEXT)')
    db.execute('''CREATE TABLE daily_quotes(symbol TEXT,trade_date TEXT,open REAL,
        high REAL,low REAL,close REAL,volume REAL,amount REAL,conflict INTEGER,
        fetched_at TEXT,source_updated_at TEXT,finality TEXT)''')
    frozen, pool, flows = {}, [], []
    for board in range(3):
        name = '板块' + str(board)
        frozen[name] = []
        for member in range(10):
            index = board * 10 + member
            symbol = f'{index + 1:06}.SZ'
            db.execute('INSERT INTO security_master VALUES(?,?,?)', (symbol, '股票'+symbol, OBSERVED))
            close = 10 + board
            rate = .0014 + (9-member) * .00008
            for offset in range(61):
                day = (dt.date.fromisoformat(BASE) - dt.timedelta(days=60-offset)).isoformat()
                value = close / (1 + rate) ** (60-offset)
                db.execute('INSERT INTO daily_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (symbol, day, value-.02, value+(.03 if offset == 60 else .005), value-.09, value, 5e6,
                     60e6, 0, OBSERVED, day+'T15:00:00+08:00', 'historical_provider'))
            frozen[name].append({'symbol': symbol, 'base_close': close,
                'membership_effective_date': BASE, 'membership_observed_at': OBSERVED})
            cap = float((Decimal(str(close))*Decimal('1.025')).quantize(Decimal('.01'), rounding=ROUND_FLOOR))
            pool.append({'symbol': symbol, 'name': '股票'+symbol, 'close': close,
                'feature_last_session': BASE, 'research_score': 100-index,
                'frozen_buy_cap': cap, 'selection_passed': False,
                'price_action_score': 0, 'eligible_patterns': []})
        flows.append({'code': f'BK00{board}', 'name': name, 'trade_date': BASE,
            'observed_at': OBSERVED, 'source_updated_at': BASE+'T15:00:00+08:00',
            'main_net_inflow_yuan': 1e8, 'main_net_ratio_pct': 5,
            'membership_status': 'complete', 'vendor_member_total': 10,
            'members': [{'symbol': row['symbol'], 'effective_date': BASE, 'observed_at': OBSERVED}
                        for row in frozen[name]]})
    outlook = {'schema': 'sector-outlook-v1', 'status': 'ready', 'classification': 'sina_industry',
        'base_date': BASE, 'target_date': TARGET, 'information_cutoff': CUTOFF,
        'frozen_members': frozen, 'constituents_sha256': cohort._digest(frozen),
        'benchmark': {'base_return_1d': .001, 'base_return_5d': .001, 'base_breadth': .5},
        'rankings': [{'sector': name, 'score': 0, 'coverage': 1, 'member_count': 10, 'components': {}}
                     for name in frozen]}
    opening = {'base_date': BASE, 'target_date': TARGET, 'information_cutoff': CUTOFF,
               'candidate_pool': pool, 'selection_passed': False, 'execution_win_rate_80_validated': False}
    flow = {'schema': 'sector-fund-flow-v1', 'classification': 'eastmoney_industry',
        'base_date': BASE, 'status': 'ready', 'information_cutoff': CUTOFF, 'sectors': flows}
    return db, outlook, opening, flow


class ActiveSystemTests(unittest.TestCase):
    def setUp(self):
        clock = mock.patch('nextday_cohort._now', return_value=dt.datetime.fromisoformat('2026-10-20T15:00:00+08:00'))
        clock.start()
        self.addCleanup(clock.stop)
        self.db, self.outlook, self.opening, self.flow = fixtures()
        self.addCleanup(self.db.close)

    def build(self, **kwargs):
        return active.build(self.db, BASE, TARGET, CUTOFF, self.outlook, self.opening,
                            kwargs.pop('fund_flow', self.flow), **kwargs)

    def test_active_independent_of_legacy_admission_and_no_fake_proof(self):
        result = self.build()
        self.assertTrue(result['system_enabled'])
        self.assertTrue(result['signal_generation_enabled'])
        self.assertEqual(result['mode'], 'conditional_manual_execution')
        self.assertEqual(result['status'], 'conditional_plans_ready')
        self.assertTrue(result['plans'])
        for key in ('order_execution_enabled', 'empirical_validation_passed', 'formal_trade_ready',
                    'execution_win_rate_80_validated', 'actual_orders_submitted',
                    'reference_fills_are_actual_fills', 'broker_connected'):
            self.assertFalse(result[key], key)
        self.assertIsNone(result['actual_win_rate'])
        for plan in result['plans']:
            self.assertFalse(plan['entry_confirmed'])
            self.assertIsNone(plan['actual_fill'])
            self.assertIsNone(plan['actual_win_rate'])
            self.assertFalse(plan['execution_win_rate_80_validated'])
        json.dumps(result, allow_nan=False)

    def test_entire_pool_not_legacy_top12_is_evaluated(self):
        result = self.build()
        self.assertEqual(result['coverage']['active_evaluated_count'], 30)
        beyond12 = next(row for row in result['candidate_evaluations'] if row['symbol'] == '000021.SZ')
        self.assertTrue(beyond12['eligible'])
        self.assertEqual(beyond12['leadership_rank'], 1)
        self.assertIn('top300', cohort.build(self.db, BASE, TARGET, CUTOFF, self.outlook,
                      self.opening, self.flow, max_candidates=300)['protocol']['universe'])

    def test_empty_or_absent_pool_keeps_rule_enabled(self):
        for opening in ({'base_date': BASE, 'target_date': TARGET, 'information_cutoff': CUTOFF,
                         'candidate_pool': []}, None):
            with self.subTest(opening=opening):
                result = active.build(self.db, BASE, TARGET, CUTOFF, self.outlook, opening, self.flow)
                self.assertTrue(result['system_enabled'])
                self.assertTrue(result['signal_generation_enabled'])
                self.assertEqual(result['plans'], [])

    def test_missing_actual_flow_is_observation_only_with_zero_lots(self):
        result = self.build(fund_flow=None)
        self.assertTrue(result['system_enabled'])
        self.assertEqual(result['status'], 'cash_no_qualifying_setup')
        self.assertFalse(result['plans'])
        self.assertTrue(all(row['frozen_quantity_shares'] == 0 for row in result['candidate_evaluations']))
        self.assertTrue(all('actual_same_vendor_flow_unknown_observation_only' in row['reasons']
                            for row in result['candidate_evaluations']))

    def test_same_display_name_cannot_donate_flow_across_taxonomy(self):
        self.flow['sectors'] = self.flow['sectors'][:1]
        # The money-flow board called 板块0 actually owns board1's constituents.
        self.flow['sectors'][0]['members'] = copy.deepcopy(self.flow['sectors'][0]['members'])
        for row in self.flow['sectors'][0]['members']:
            row['symbol'] = f'{int(row["symbol"][:6])+10:06}.SZ'
        result = self.build()
        first = next(row for row in result['candidate_evaluations'] if row['symbol'] == '000001.SZ')
        actual = next(row for row in result['candidate_evaluations'] if row['symbol'] == '000011.SZ')
        self.assertFalse(first['flow_known'])
        self.assertEqual(first['frozen_quantity_shares'], 0)
        self.assertTrue(actual['flow_known'])
        self.assertEqual(actual['sector_identity']['classification'], 'eastmoney_industry')
        self.assertEqual(actual['sector_identity']['sector_code'], 'BK000')

    def test_both_actual_net_and_ratio_are_strictly_positive(self):
        for key, value in (('main_net_inflow_yuan', 0), ('main_net_ratio_pct', 0),
                           ('main_net_inflow_yuan', -1), ('main_net_ratio_pct', -1)):
            with self.subTest(key=key, value=value):
                flow = copy.deepcopy(self.flow)
                for row in flow['sectors']:
                    row[key] = value
                result = self.build(fund_flow=flow)
                self.assertFalse(result['plans'])
                self.assertTrue(all(row['flow_known'] for row in result['candidate_evaluations']))

    def test_future_source_or_constituent_cannot_supply_positive_flow(self):
        for mutation in ('future_observed', 'future_member', 'truncated', 'intraday', 'wrong_taxonomy'):
            with self.subTest(mutation=mutation):
                flow = copy.deepcopy(self.flow)
                if mutation == 'wrong_taxonomy':
                    flow['classification'] = 'sina_industry'
                for row in flow['sectors']:
                    if mutation == 'future_observed': row['observed_at'] = TARGET+'T15:00:00+08:00'
                    elif mutation == 'future_member': row['members'][0]['observed_at'] = TARGET+'T15:00:00+08:00'
                    elif mutation == 'truncated': row['vendor_member_total'] = 99
                    elif mutation == 'intraday': row['source_updated_at'] = BASE+'T14:59:00+08:00'
                self.assertFalse(self.build(fund_flow=flow)['plans'])

    def test_market_gates_fail_without_deactivating_rule(self):
        for key, value in (('base_breadth', .1), ('base_breadth', None),
                           ('base_return_1d', -.05), ('base_return_5d', -.11)):
            old = self.outlook['benchmark'][key]
            self.outlook['benchmark'][key] = value
            result = self.build()
            self.assertTrue(result['system_enabled'])
            self.assertFalse(result['plans'])
            self.outlook['benchmark'][key] = old

    def test_price_action_gates_are_own_fixed_quality_rules(self):
        cases = (
            ('median_amount20', 49_999_999), ('atr14_pct', .0079), ('atr14_pct', .0401),
            ('ret20', 0), ('ma20_gap', -.001), ('ma20_gap', .081),
            ('ret1', -.0201), ('ret1', .0401), ('close_location', .5999),
        )
        real_snapshot = active.price_action_snapshot
        for key, value in cases:
            def altered(*args, **kwargs):
                result = real_snapshot(*args, **kwargs)
                result[key] = value
                return result
            with self.subTest(key=key, value=value), mock.patch('active_trade_system.price_action_snapshot', side_effect=altered):
                self.assertFalse(self.build()['plans'])
        # Deliberately poison legacy score/pattern output: our own gates still pass.
        def no_legacy_pass(*args, **kwargs):
            result = real_snapshot(*args, **kwargs)
            result.update(price_action_score=0, eligible_patterns=[], research_signal=False)
            return result
        with mock.patch('active_trade_system.price_action_snapshot', side_effect=no_legacy_pass):
            self.assertTrue(self.build()['plans'])

    def test_sixty_one_known_bars_required_and_future_daily_source_rejected(self):
        self.db.execute('DELETE FROM daily_quotes WHERE trade_date=(SELECT MIN(trade_date) FROM daily_quotes)')
        result = self.build()
        self.assertFalse(result['plans'])
        self.assertTrue(all('requires_61_valid_trailing_bars' in row['reasons'] for row in result['candidate_evaluations']))
        self.db.execute('UPDATE daily_quotes SET fetched_at=? WHERE trade_date=?',
                        (TARGET+'T15:00:00+08:00', BASE))
        self.assertFalse(self.build()['plans'])

    def test_full_board_leadership_not_rank_among_prefiltered_candidates(self):
        self.opening['candidate_pool'] = [self.opening['candidate_pool'][9]]
        result = self.build()
        row = result['candidate_evaluations'][0]
        self.assertEqual(row['leadership_rank'], 10)
        self.assertEqual(row['leadership_member_count'], 10)
        self.assertEqual(row['leadership_top_count'], 3)
        self.assertFalse(result['plans'])

    def test_duplicate_daily_history_is_observation_instead_of_crashing_or_buying(self):
        self.db.execute('INSERT INTO daily_quotes SELECT * FROM daily_quotes WHERE trade_date=?', (BASE,))
        result = self.build()
        self.assertTrue(result['system_enabled'])
        self.assertFalse(result['plans'])
        self.assertTrue(all('invalid_or_duplicate_price_action_history' in row['reasons']
                            for row in result['candidate_evaluations']))

    def test_joint_stop_cap_floor_quantity_and_risk_are_frozen(self):
        result = self.build()
        self.assertEqual(len(result['plans']), 2)
        for plan in result['plans']:
            cap, floor, stop = [Decimal(str(plan[key])) for key in
                                ('frozen_entry_cap', 'frozen_entry_floor', 'structural_stop')]
            quantity = plan['frozen_quantity_shares']
            self.assertEqual(quantity % 100, 0)
            self.assertGreaterEqual(quantity, 100)
            self.assertTrue(Decimal('.008') <= (floor-stop)/floor <= (cap-stop)/cap <= Decimal('.02'))
            self.assertGreaterEqual(Decimal('.03') / ((cap-stop)/cap), Decimal('1.5'))
            self.assertLessEqual(cap*quantity, 10000)
            self.assertEqual(cap*quantity, Decimal(str(plan['frozen_reserved_cash_cny'])))
            self.assertLessEqual((cap-stop+cap*Decimal('.02'))*quantity, 150)
            raw_lows = [row[0] for row in self.db.execute('SELECT low FROM daily_quotes WHERE symbol=? ORDER BY trade_date DESC LIMIT 5', (plan['symbol'],))]
            expected = (Decimal(str(min(raw_lows))) - Decimal('.15')*Decimal(str(plan['atr14']))).quantize(Decimal('.01'), rounding=ROUND_FLOOR)
            self.assertEqual(stop, expected)
            self.assertEqual(plan['target_pct'], .03)

    def test_max_two_total_one_exact_sector_with_existing_positions_and_cash(self):
        first = self.build()
        sectors = [json.dumps(plan['sector_identity'], sort_keys=True) for plan in first['plans']]
        self.assertEqual(len(sectors), len(set(sectors)))
        chosen = first['plans'][0]
        occupied = self.build(occupied_symbols=[chosen['symbol']], occupied_sectors=[chosen['sector_identity']])
        self.assertEqual(len(occupied['plans']), 1)
        self.assertNotEqual(occupied['plans'][0]['sector_identity'], chosen['sector_identity'])
        self.assertFalse(self.build(occupied_symbols=['held1', 'held2'])['plans'])
        low_cash = self.build(available_cash=1800)
        self.assertLessEqual(sum(plan['frozen_reserved_cash_cny'] for plan in low_cash['plans']), 1800)
        self.assertFalse(self.build(available_cash=100)['plans'])
        self.assertTrue(self.build(available_cash=0)['system_enabled'])

    def test_STAR_100_share_allocation_is_rejected_without_raising_risk(self):
        symbol = '688001.SH'
        self.db.execute('UPDATE daily_quotes SET symbol=? WHERE symbol=?', (symbol, '000001.SZ'))
        self.db.execute('UPDATE security_master SET symbol=? WHERE symbol=?', (symbol, '000001.SZ'))
        self.opening['candidate_pool'][0]['symbol'] = symbol
        self.opening['candidate_pool'] = self.opening['candidate_pool'][:1]
        self.outlook['frozen_members']['板块0'][0]['symbol'] = symbol
        self.outlook['constituents_sha256'] = cohort._digest(self.outlook['frozen_members'])
        self.flow['sectors'][0]['members'][0]['symbol'] = symbol
        low_cash = self.build(available_cash=1800)
        star = next(row for row in low_cash['candidate_evaluations'] if row['symbol'] == symbol)
        self.assertEqual(star['frozen_quantity_shares'], 0)
        self.assertIn('STAR_minimum_200_shares_exceeds_frozen_cash_or_risk_budget', star['reasons'])
        self.assertTrue(all(row['symbol'] != symbol for row in low_cash['plans']))
        regular = self.build()
        star_plan = next(row for row in regular['plans'] if row['symbol'] == symbol)
        self.assertGreaterEqual(star_plan['frozen_quantity_shares'], 200)
        self.assertLessEqual(star_plan['frozen_reserved_risk_cny'], 150)
        self.assertEqual(regular['protocol_sha256'], active.PROTOCOL_SHA256)

    def test_late_or_nonadjacent_target_waits_enabled_not_retroactive(self):
        for target, cutoff in ((TARGET, TARGET+'T09:15:00+08:00'), ('2026-10-20', CUTOFF)):
            result = active.build(self.db, BASE, target, cutoff, self.outlook, self.opening, self.flow)
            self.assertTrue(result['system_enabled'])
            self.assertEqual(result['status'], 'waiting_for_valid_source')
            self.assertFalse(result['plans'])

    def test_T_only_plan_identity_and_input_order_independence(self):
        before = self.build(input_sha256='before_bytes')
        original = copy.deepcopy((self.outlook, self.opening, self.flow))
        self.build()
        self.assertEqual(original, (self.outlook, self.opening, self.flow))
        self.db.execute('INSERT INTO daily_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            ('000001.SZ', TARGET, 10, 100, 1, 99, 1e10, 1e12, 0,
             TARGET+'T15:01:00+08:00', TARGET+'T15:00:00+08:00', 'historical_provider'))
        self.opening['future_win_rate'] = .99
        for row in self.opening['candidate_pool']:
            row.update(D2_close=999, future_return=999, actual_win=True, unknown_extra={'Pwin': .99})
        for rows in self.outlook['frozen_members'].values():
            for row in rows:
                row['future_label'] = 999
        self.outlook['rankings'][0]['future_return'] = -.99
        self.opening['candidate_pool'].reverse()
        after = self.build(input_sha256='different_database_bytes')
        self.assertEqual(before['plans'], after['plans'])
        self.assertEqual(before['policy_id'], after['policy_id'])
        self.assertNotEqual(before['input_database_sha256'], after['input_database_sha256'])

    def test_explicit_entry_and_D2_next_bar_exit_never_exact_target_fill(self):
        protocol = self.build()['protocol']
        self.assertEqual(protocol['entry']['decision_time'], '14:40')
        self.assertIn('confirmation_close*1.003', protocol['entry']['maximum_entry_price'])
        self.assertIn('minute_STARTING_14:40_ending_14:41', protocol['entry']['reference_entry'])
        self.assertIn('not_the_manual_quote_or_an_actual_fill', protocol['entry']['reference_entry'])
        self.assertFalse(protocol['entry']['broker_connected'])
        self.assertFalse(protocol['exit']['same_day_sale'])
        self.assertIn('stop_wins_if_both', protocol['exit']['intraday_trigger'])
        self.assertIn('next_available_consecutive', protocol['exit']['intraday_reference'])
        self.assertIn('minute_STARTING_14:50_OPEN_ending_14:51', protocol['exit']['no_trigger'])
        self.assertFalse(protocol['exit']['references_are_actual_fills'])


if __name__ == '__main__':
    unittest.main()
