import copy
import json
import unittest

import nextday_forward as forward
import nextday_report as report


def study():
    raw = forward.initialize({'schema': 'nextday-frozen-cohort-v1',
        'base_trade_date': '2026-10-16', 'target_trade_date': '2026-10-19',
        'D2_trade_date': '2026-10-20', 'generated_at': '2026-10-18T14:00:00+08:00',
        'cohort_id': 'c'*64, 'forward_eligible': True,
        'coverage': dict(base_cohort_count=1, market_gate_passed_count=1,
                         sector_gate_passed_count=1, leadership_gate_passed_count=1, confluence_count=1),
        'market_gate': dict(known=True, eligible=True, base_breadth=.6,
                            base_return_1d=.01, base_return_5d=.02),
        'base_candidates': [dict(symbol='000001.SZ', name='示例公司', base_close=10,
            frozen_entry_floor=9.9, frozen_entry_cap=10.25, frozen_quantity_shares=900,
            sector_identity=dict(classification='sina_industry', sector_code=None, sector='板块甲'),
            relative_return_5d=.02, leadership_rank=1, leadership_member_count=10,
            leadership_percentile=.1, confluence_eligible=True, reasons=[], flow_known=False)]},
        '2026-10-16', '2026-10-19', 'd'*64, '2026-10-18T14:00:00+08:00')
    raw['tracked_dates'] = 0
    return raw


class NextdayReportTests(unittest.TestCase):
    def test_compact_idempotent_and_full_render_parity(self):
        raw = study()
        compact = report.compact(raw)
        self.assertEqual(report.compact(compact), compact)
        self.assertEqual(report.render(raw), report.render(compact))

    def test_compaction_drops_large_ledger_protocol_and_unknown_keys_under25kb(self):
        raw = study()
        raw['records'] = [{'secret': 'x'*100000}]*100
        raw['tracked_cohorts'] = [{'secret': 'x'*100000}]*100
        raw['protocol']['secret'] = 'x'*100000
        raw['current_cohort']['protocol'] = {'secret': 'x'*100000}
        row = raw['current_cohort']['base_candidates'][0]
        row.update(name='中'*100000, reasons=['中'*100000]*100,
            membership_snapshot_sha256='h'*100000, extra='x'*100000)
        row['sector_identity'] = {key: '中'*100000 for key in ('classification', 'sector_code', 'sector')}
        raw['current_cohort']['base_candidates'] = [copy.deepcopy(row) for _ in range(100)]
        for summary in raw['summary'].values():
            summary.update(extra='x'*100000, bootstrap_interval=dict(
                gross_mean=[-.01, .02], reference_profit_rate=[.3, .7], secret='x'*100000))
        compact = report.compact(raw)
        self.assertLess(len(json.dumps(compact, ensure_ascii=False).encode()), 25000)
        self.assertEqual(len(compact['current_cohort']['base_candidates']), 12)
        self.assertNotIn('records', compact)
        self.assertNotIn('tracked_cohorts', compact)
        self.assertNotIn('protocol', compact)
        self.assertNotIn('protocol', compact['current_cohort'])
        self.assertTrue(compact['full_evidence_archived_separately'])
        self.assertEqual(report.compact(compact), compact)
        self.assertEqual(report.render(raw), report.render(compact))

    def test_nondefault_flags_never_upgrade_trade_fill_or_probability_claims(self):
        raw = study()
        raw.update(formal_trade_ready=True, actual_win_rate=.99,
            execution_win_rate_80_validated=True, orders_submitted=True, automatic_policy_activation=True)
        raw['current_cohort'].update(formal_trade_ready=True, actual_win_rate=.99)
        raw['current_cohort']['base_candidates'][0].update(formal_trade_ready=True,
            actual_win_rate=.99, execution_win_rate_80_validated=True, predicted_probability=.99)
        raw['summary']['base_0940'].update(formal_trade_ready=True, actual_win_rate=.99,
                                         execution_win_rate_80_validated=True)
        compact = report.compact(raw)
        for part in (compact, compact['current_cohort'], compact['current_cohort']['base_candidates'][0],
                     compact['summary']['base_0940']):
            self.assertFalse(part['formal_trade_ready'])
            self.assertIsNone(part['actual_win_rate'])
            self.assertFalse(part['execution_win_rate_80_validated'])
        self.assertFalse(compact['orders_submitted'])
        self.assertFalse(compact['automatic_policy_activation'])
        self.assertNotIn('predicted_probability', compact['current_cohort']['base_candidates'][0])
        self.assertIn('主动规则系统已独立启用', report.render(raw))
        self.assertNotIn('主策略继续停用', report.render(raw))
        self.assertIn('80%以上实际胜率尚未验证', report.render(raw))

    def test_compact_bound_includes_multibyte_metadata_and_all_intervals(self):
        raw = study()
        for key in ('generated_at', 'updated_at', 'base_trade_date', 'target_trade_date',
                    'protocol_sha256', 'input_database_sha256'):
            raw[key] = '\U0001f600'*1000
        current = raw['current_cohort']
        for key in ('schema', 'base_trade_date', 'target_trade_date', 'D2_trade_date',
                    'generated_at', 'cohort_id', 'cohort_sha256', 'protocol_sha256', 'status', 'reason'):
            current[key] = '\U0001f600'*1000
        row = current['base_candidates'][0]
        row.update(name='\U0001f600'*1000, symbol='\U0001f600'*1000,
            membership_snapshot_sha256='\U0001f600'*1000, reasons=['\U0001f600'*1000]*3)
        row['sector_identity'] = {key: '\U0001f600'*1000 for key in ('classification', 'sector_code', 'sector')}
        current['base_candidates'] = [copy.deepcopy(row) for _ in range(12)]
        for metric in raw['summary'].values():
            metric.update({key: 1e12 for key in report.METRICS})
            metric['bootstrap_interval'] = {key: [-1e12, 1e12] for key in
                ('gross_mean', 'reference_profit_rate', 'moving5_gross_mean', 'moving5_reference_profit_rate')}
        compact = report.compact(raw)
        self.assertLess(len(json.dumps(compact, ensure_ascii=False).encode()), 25000)
        self.assertEqual(report.compact(compact), compact)

    def test_intervals_require_minimum_closed_observations_and_dates(self):
        raw = study()
        raw['summary']['base_0940']['bootstrap_interval'] = {'reference_profit_rate': [.8, .99]}
        self.assertEqual(report.compact(raw)['summary']['base_0940']['bootstrap_interval'], {})
        raw['summary']['base_0940'].update(reference_closed=40, distinct_entry_dates=20)
        self.assertEqual(report.compact(raw)['summary']['base_0940']['bootstrap_interval']['reference_profit_rate'], [.8, .99])

    def test_html_strings_and_attributes_are_escaped(self):
        raw = study()
        row = raw['current_cohort']['base_candidates'][0]
        row['name'] = '<script>alert("x")</script>'
        row['symbol'] = '<img src=x>'
        row['sector_identity']['sector'] = '<iframe src="x">'
        raw['current_cohort']['generated_at'] = '<a href="javascript:x">'
        rendered = report.render(raw)
        self.assertNotIn('<script>', rendered)
        self.assertNotIn('<iframe', rendered)
        self.assertNotIn('<img src=x>', rendered)
        self.assertIn('&lt;script&gt;', rendered)
        self.assertIn('&quot;', rendered)

    def test_six_arms_matched_controls_timing_and_decomposition_are_visible(self):
        rendered = report.render(study())
        for arm in report.ARMS:
            self.assertIn(report.NAMES[arm], rendered)
        for text in ('D2固定14:50', '09:40完成观察，下一分钟开盘参考',
            '14:40完成观察，下一分钟开盘参考', '同股、同日配对', 'D1买入至收盘均值',
            'D1收盘至D2开盘均值', 'D2开盘至退出均值', '不是账户组合', '不是12个持仓',
            '参考持仓及待入场合计最多两只', '不会自动启用规则', '费用为零'):
            self.assertIn(text, rendered)

    def test_zero_future_ledger_and_empty_cohort_are_pending_not_zero_win_probability(self):
        raw = study()
        raw['current_cohort']['base_candidates'] = []
        rendered = report.render(raw)
        self.assertIn('当前影子样本为空', rendered)
        self.assertIn('新增未来已闭合样本为0', rendered)
        self.assertIn('盈利比例待积累', rendered)
        self.assertNotIn('<td>0.00%</td>', rendered)

    def test_known_forward_summary_unknown_coverage_and_pairs_survive(self):
        raw = study()
        raw['summary']['base_0940'].update(scheduled_observations=10, reference_closed=4,
            distinct_entry_dates=2, unknown=3, pending=3, reference_gross_profit_rate=.5,
            reference_ge3_rate=.25, reference_ge6_rate=0, mean_gross_return=.006,
            worst_gross_return=-.03, mean_D1_after_buy_return=.01, mean_overnight_return=-.004,
            mean_D2_after_open_return=0, coverage_fraction_closed=.4,
            average_winning_return=.02, average_nonwinning_return=-.008)
        raw['paired_timing']['base'].update(both_closed=3, unmatched_or_pending=7,
            mean_return_1440_minus_0940=.002, profit_fraction_1440_minus_0940=.1)
        compact = report.compact(raw)
        self.assertEqual(compact['summary']['base_0940']['unknown'], 3)
        self.assertEqual(compact['summary']['base_0940']['coverage_fraction_closed'], .4)
        self.assertEqual(compact['paired_timing']['base']['both_closed'], 3)
        self.assertEqual(report.render(raw), report.render(compact))
        rendered = report.render(raw)
        for text in ('50.00%', '40.00%', '-3.00%', '-0.40%', '0.20%', '10.00%'):
            self.assertIn(text, rendered)

    def test_reason_codes_are_explained_without_showing_internal_names(self):
        raw = study()
        row = raw['current_cohort']['base_candidates'][0]
        row.update(confluence_eligible=False, reasons=['stock_not_top_30_percent_of_full_board_history'])
        rendered = report.render(raw)
        self.assertIn('仅原池对照', rendered)
        self.assertIn('未进入整个板块近5日涨幅前30%', rendered)
        self.assertNotIn('stock_not_top_30_percent', rendered)

    def test_primary_source_links_only_support_research_not_our_win_claim(self):
        rendered = report.render(study())
        for domain in ('brookstradingcourse.com', 'investors.com', 'lindaraschke.net',
                       'research.rug.nl', 'davidhbailey.com'):
            self.assertIn(domain, rendered)
        self.assertIn('未验证本项目的数值阈值', rendered)

    def test_schema_mismatch_nonfinite_and_input_immutability(self):
        self.assertEqual(report.compact({}), {})
        self.assertEqual(report.render({'schema': 'old'}), '')
        raw = study()
        raw['summary']['base_0940']['reference_gross_profit_rate'] = float('nan')
        raw['summary']['base_0940']['reference_closed'] = 10**1000
        compact = report.compact(raw)
        self.assertIsNone(compact['summary']['base_0940']['reference_gross_profit_rate'])
        self.assertEqual(compact['summary']['base_0940']['reference_closed'], 0)
        json.dumps(compact, allow_nan=False)
        raw = study(); before = copy.deepcopy(raw)
        report.compact(raw); report.render(raw)
        self.assertEqual(raw, before)


if __name__ == '__main__':
    unittest.main()
