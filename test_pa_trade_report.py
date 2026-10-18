import copy
import json
import unittest

import pa_trade_report as report


def study(active=True):
    metrics = dict(n_attempts=42, n_closed=40, n_unresolved=2, n_cancelled=3,
                   independent_entry_dates=18, win_rate=0.55, ge3_rate=0.3, ge6_rate=0.1,
                   mean_closed_gross_return=0.004, profit_factor=1.2,
                   validation=dict(selection_gate=True, selection_blockers=[],
                                   familywise_lower_mean_return=0.001,
                                   moving_block_win_ci95=[0.3, 0.7]))
    return dict(schema=report.VERSION, generated_at='2026-10-18', base_trade_date='2026-10-16',
                target_trade_date='2026-10-19', input_database_sha256='a'*64,
                policy=dict(active=active, selected_setup='breakout_followthrough', scope='research_only'),
                formal_trade_ready=False, actual_win_rate=None,
                coverage=dict(scanned=50000, history_candidates=2000, live_candidates=100,
                              eligible_plans=12, current_selected=1),
                diagnostic={key: copy.deepcopy(metrics) for key in report.DIAGNOSTICS},
                selection=[dict(setup=key, eligible_research=True, metrics=copy.deepcopy(metrics)) for key in report.SETUPS],
                current_watchlist=[dict(symbol='000001', name='示例公司', context='bull', entry_cap=20.14,
                                        frozen_entry_floor=19.8, structural_stop=19.74, target_pct=0.03,
                                        reward_risk=1.51, breakeven_probability=0.398,
                                        stress_breakeven_probability=0.57, quantity_ref=100)])


class ReportTests(unittest.TestCase):
    def test_compact_is_idempotent_and_render_matches_full_evidence(self):
        raw = study(); slim = report.compact(raw)
        self.assertEqual(report.compact(slim), slim)
        self.assertEqual(report.render(raw), report.render(slim))

    def test_compact_keeps_bounded_fields_below_fifteen_kb(self):
        raw = study(); raw['raw_ticks'] = ['x'*100000]*100
        raw['current_watchlist'] *= 100
        for row in raw['current_watchlist']:
            row['name'] = '庞大内容'*10000
            row['unknown'] = 'x'*100000
        raw['selection'] *= 100
        for metric in raw['diagnostic'].values():
            metric['records'] = [{'data': 'x'*100000}]
            metric['validation']['selection_blockers'] = ['large'*10000]*100
        compact = report.compact(raw)
        self.assertLess(len(json.dumps(compact, ensure_ascii=False).encode('utf-8')), 15000)
        self.assertEqual(len(compact['current_watchlist']), 5)
        self.assertEqual(len(compact['selection']), 3)

    def test_diagnostics_include_all_three_rules_and_control_rows(self):
        rendered = report.render(study())
        for key in report.DIAGNOSTICS:
            self.assertIn(report.NAMES[key], rendered)
        self.assertIn('40', rendered)
        self.assertIn('55.00%', rendered)

    def test_inactive_study_shows_no_executable_watchlist(self):
        raw = study(False)
        self.assertEqual(report.compact(raw)['current_watchlist'], [])
        rendered = report.render(raw)
        self.assertIn('保留空名单', rendered)
        self.assertNotIn('示例公司', rendered)
        for key in report.DIAGNOSTICS[:4]:
            self.assertIn(report.NAMES[key], rendered)

    def test_html_user_values_are_escaped(self):
        raw = study(); raw['current_watchlist'][0]['name'] = '<script>alert("x")</script>'
        rendered = report.render(raw)
        self.assertNotIn('<script>', rendered)
        self.assertIn('&lt;script&gt;', rendered)

    def test_never_upgrades_formal_or_actual_probability_claims(self):
        raw = study(); raw.update(formal_trade_ready=True, actual_win_rate=0.99)
        slim = report.compact(raw)
        self.assertFalse(slim['formal_trade_ready'])
        self.assertIsNone(slim['actual_win_rate'])
        self.assertIn('盈亏平衡门槛不是预测胜率', report.render(raw))
        self.assertIn('80%以上实际胜率尚未验证', report.render(raw))

    def test_execution_limits_and_attribution_are_visible(self):
        rendered = report.render(study())
        for text in ('买入当天不卖', '历史分钟证据与实时接入尚未完成', '本次未取得字幕',
                     '具体数值阈值由本项目设定', '实际损失超过预算', 'T+2开盘处理'):
            self.assertIn(text, rendered)
        self.assertIn('https://www.youtube.com/watch?v=152osf_ULas', rendered)

    def test_schema_mismatch_and_empty_metrics_are_safe(self):
        self.assertEqual(report.compact({}), {})
        self.assertEqual(report.render({'schema': 'old-version'}), '')
        raw = study(); raw['diagnostic'] = {'selected_policy': {}}
        self.assertIn('—', report.render(raw))

    def test_compaction_does_not_mutate_full_evidence(self):
        raw = study(); old = copy.deepcopy(raw)
        report.compact(raw); report.render(raw)
        self.assertEqual(raw, old)


if __name__ == '__main__':
    unittest.main()
