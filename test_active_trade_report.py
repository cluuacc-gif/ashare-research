import copy
import json
import math
import unittest
from unittest.mock import patch

import active_trade_report as report


def study():
    return {
        "schema": report.VERSION,
        "system_enabled": True,
        "signal_generation_enabled": True,
        "mode": "conditional_manual_execution",
        "order_execution_enabled": False,
        "empirical_validation_passed": False,
        "formal_trade_ready": False,
        "actual_win_rate": None,
        "execution_win_rate_80_validated": False,
        "generated_at": "2026-10-18T20:10:00+08:00",
        "base_trade_date": "2026-10-16",
        "target_trade_date": "2026-10-19",
        "D2_trade_date": "2026-10-20",
        "input_database_sha256": "a" * 64,
        "policy_id": "b" * 64,
        "protocol_sha256": "b" * 64,
        "status": "conditional_plans_ready",
        "coverage": {"candidate_pool_count": 300, "active_evaluated_count": 300,
                     "active_qualifying_count": 3, "conditional_plan_count": 1},
        "plans": [{"active_plan_id": "active-plan-001", "symbol": "000001", "name": "示例公司",
                   "sector_identity": {"classification": "vendor-industry", "sector_code": "BK001", "sector": "示例板块"},
                   "base_close": 10.0, "frozen_entry_floor": 9.90, "frozen_entry_cap": 10.0,
                   "structural_stop": 9.8, "target_pct": .03, "frozen_quantity_shares": 300,
                   "frozen_reserved_cash_cny": 3000.0, "frozen_reserved_risk_cny": 120.0,
                   "atr14": .15, "rank_score": .8}],
        "reference_account": {"initial_capital_cny": 30000, "available_cash_cny": 27000,
                              "reserved_cash_cny": 3000, "realized_reference_pnl_cny": 0,
                              "open_positions": 0, "reference_closed": 0,
                              "reference_profit_rate": 0.0, "unknown_positions": 0},
    }


class ActiveTradeReportTests(unittest.TestCase):
    def test_compaction_is_idempotent_and_render_has_full_input_parity(self):
        raw = study()
        slim = report.compact(raw)
        self.assertEqual(slim, report.compact(slim))
        self.assertEqual(report.render(raw), report.render(slim))
        self.assertEqual(raw, study())

    def test_enabled_state_is_independent_of_empirical_validation(self):
        raw = study()
        raw["legacy_policy"] = {"active": False, "selection_gate": False}
        raw["diagnostic"] = {"win_rate": .2}
        result = report.compact(raw)
        self.assertTrue(result["system_enabled"])
        self.assertTrue(result["signal_generation_enabled"])
        self.assertFalse(result["empirical_validation_passed"])
        rendered = report.render(raw)
        self.assertIn("规则系统已启用", rendered)
        self.assertIn("条件计划已生成", rendered)
        self.assertIn("系统启用独立于旧研究是否通过", rendered)
        self.assertNotIn("关闭", rendered)
        self.assertNotIn("停用", rendered)

    def test_empty_and_source_wait_states_keep_system_running(self):
        for status, title in (("cash_no_qualifying_setup", "当天空仓"),
                              ("waiting_for_valid_source", "等待有效新数据")):
            raw = study()
            raw.update(status=status, plans=[])
            self.assertTrue(report.compact(raw)["system_enabled"])
            rendered = report.render(raw)
            self.assertIn(title, rendered)
            self.assertIn("系统持续运行", rendered)
            self.assertNotIn("示例公司", rendered)

    def test_never_promotes_supplied_broker_or_win_rate_claims(self):
        raw = study()
        raw.update(system_enabled=False, signal_generation_enabled=False,
                   empirical_validation_passed=True, formal_trade_ready=True,
                   actual_win_rate=.99, execution_win_rate_80_validated=True,
                   order_execution_enabled=True, actual_orders_submitted=True,
                   broker_connected=True, live_feed_connected=True,
                   reference_fills_are_actual_fills=True, score_is_probability=True)
        raw["plans"][0].update(actual_fill=9.95, entry_confirmed=True, target_pct=.99)
        slim = report.compact(raw)
        self.assertIsNone(slim["actual_win_rate"])
        self.assertTrue(slim["system_enabled"])
        self.assertEqual(slim["mode"], "conditional_manual_execution")
        for key in ("empirical_validation_passed", "formal_trade_ready", "execution_win_rate_80_validated",
                    "order_execution_enabled", "actual_orders_submitted", "broker_connected",
                    "live_feed_connected", "reference_fills_are_actual_fills", "score_is_probability"):
            self.assertFalse(slim[key], key)
        self.assertIsNone(slim["plans"][0]["actual_fill"])
        self.assertFalse(slim["plans"][0]["entry_confirmed"])
        self.assertEqual(slim["plans"][0]["target_pct"], .03)
        rendered = report.render(raw)
        self.assertNotIn("99.00%", rendered)
        self.assertIn("真实胜率未知", rendered)
        self.assertIn("80%以上实际胜率尚未验证", rendered)

    def test_reports_actual_buy_target_formula_instead_of_close_based_fill(self):
        rendered = report.render(study())
        self.assertIn("实际买价 × 1.03", rendered)
        self.assertIn("尚无实际买价", rendered)
        self.assertIn("T日收盘参考 10.00", rendered)
        self.assertNotIn("10.30", rendered)
        self.assertIn("300股", rendered)
        self.assertIn("9.90～10.00", rendered)

    def test_compact_keeps_only_two_plans_and_no_raw_records_below_twenty_kb(self):
        raw = study()
        raw["plans"] *= 100
        raw["raw"] = "x" * 500000
        raw["protocol"] = {"huge": "x" * 500000}
        raw["records"] = [{"raw_minutes": "x" * 500000}]
        for key in report._TOP_TEXT:
            raw[key] = "庞大内容" * 10000
        for row in raw["plans"]:
            row.update(name="庞大内容" * 10000, evidence={"raw": "x" * 100000})
            row["sector_identity"] = {key: "庞大内容" * 10000
                                      for key in ("sector", "classification", "sector_code", "unknown")}
        raw["reference_account"].update(records=["x" * 500000],
                                         open_positions=[{"raw": "x" * 500000}] * 3)
        slim = report.compact(raw)
        self.assertEqual(len(slim["plans"]), 2)
        self.assertEqual(slim["reference_account"]["open_positions"], 3)
        self.assertNotIn("raw", slim)
        self.assertNotIn("protocol", slim)
        self.assertNotIn("records", slim)
        self.assertNotIn("evidence", slim["plans"][0])
        self.assertNotIn("records", slim["reference_account"])
        self.assertLess(len(json.dumps(slim, ensure_ascii=False).encode("utf-8")), 20000)
        self.assertEqual(slim, report.compact(slim))

    def test_html_escapes_every_text_source(self):
        raw = study()
        attack = '<script>alert("x")</script>'
        raw["plans"][0].update(name=attack, symbol=attack, sector_identity={"sector": attack})
        raw.update(base_trade_date=attack, policy_id=attack, input_database_sha256=attack)
        rendered = report.render(raw)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("&quot;x&quot;", rendered)

    def test_unknown_probability_is_never_zero(self):
        raw = study()
        raw.pop("reference_account")
        rendered = report.render(raw)
        self.assertIn("真实成交胜率</small><b>未知", rendered)
        self.assertIn("已平仓参考胜率</small><b>未知", rendered)
        self.assertNotIn("0.00%", rendered)
        self.assertNotIn("<b>0%</b>", rendered)

    def test_reference_account_is_not_actual_broker_profit(self):
        raw = study()
        raw["reference_account"].update(realized_reference_pnl_cny=300, reference_profit_rate=.01,
                                         open_positions=1, reference_closed=2, unknown_positions=1)
        rendered = report.render(raw)
        self.assertIn("1.00%", rendered)
        self.assertIn("分钟参考账户", rendered)
        self.assertIn("不代表实际券商持仓或实际盈利", rendered)
        self.assertIn("真实盈利待成交回执核对", rendered)
        self.assertIn("真实买卖价格、费用及盈亏须由成交回执确认", rendered)

    def test_risk_and_causal_tplus1_limits_are_visible(self):
        rendered = report.render(study())
        for text in ("最多300只", "全板块有效成员前30%", "14:40", "D1买入当天不卖", "不主动延至D3",
                     "下一连续可交易分钟开盘", "同分钟两者都触发则止损优先", "D2 14:50开盘参考退出",
                     "150元", "2%跳空压力", "不是最大损失保证", "不追价", "未知持仓仍保留"):
            self.assertIn(text, rendered)

    def test_invalid_inputs_are_safe_and_not_rendered_as_live_plans(self):
        for value in (None, [], {}, {"schema": "old-version"}):
            self.assertEqual(report.compact(value), {})
            self.assertEqual(report.render(value), "")
        for field, value in (("plans", {}), ("coverage", []), ("reference_account", []), ("status", {})):
            raw = study()
            raw[field] = value
            self.assertEqual(report.render(raw), report.render(report.compact(raw)))
        for key, value in (("frozen_entry_floor", math.nan), ("frozen_entry_cap", math.inf),
                           ("structural_stop", 10.01), ("frozen_quantity_shares", 150),
                           ("frozen_reserved_cash_cny", True)):
            raw = study()
            raw["plans"][0][key] = value
            self.assertEqual(report.compact(raw)["plans"], [])
            self.assertIn("当天空仓", report.render(raw))

    def test_invalid_source_never_displays_stale_plans(self):
        raw = study()
        raw["status"] = "waiting_for_valid_source"
        self.assertEqual(report.compact(raw)["plans"], [])
        self.assertNotIn("示例公司", report.render(raw))

    def test_render_and_compact_leave_input_unchanged(self):
        raw = study()
        before = copy.deepcopy(raw)
        report.compact(raw)
        report.render(raw)
        self.assertEqual(raw, before)


class ActiveMailIntegrationTests(unittest.TestCase):
    def payload(self):
        return {"stage": "evening", "target_date": "2026-10-16", "base_trade_date": "2026-10-16",
                "information_cutoff": "2026-10-18T21:00:00+08:00",
                "calendar": {"next_session": "2026-10-19"},
                "research": {"active_trade_system": study()}}

    def test_both_email_formats_lead_with_enabled_plan_and_reference_account(self):
        import mail_layout
        payload = self.payload()
        content = mail_layout.content(payload)
        self.assertEqual(content["sections"][0]["title"], "我的交易系统｜" + report.POLICY_NAME)
        self.assertEqual(sum(section["title"].startswith("我的交易系统")
                             for section in content["sections"]), 1)
        for body in (mail_layout.render_text(payload), mail_layout.render_html(payload)):
            for phrase in ("已启用；条件式人工执行", "示例公司", "9.90～10.00", "实际买价×1.03",
                           "300股", "主动系统分钟参考账户", "待个人券商成交回执核对，当前未知",
                           "盘中确认待人工核对", "当前没有实时行情或券商连接"):
                self.assertIn(phrase, body)
            self.assertLess(body.index("我的交易系统"), body.index("隔日毛收益策略（旧研究）"))

    def test_missing_stale_and_future_mail_input_keeps_system_enabled_without_plan(self):
        import mail_layout
        for change in ({}, {"base_trade_date": "2026-10-15"},
                       {"target_trade_date": "2026-10-20"},
                       {"generated_at": "2026-10-18T21:00:01+08:00"}):
            payload = self.payload()
            if change:
                payload["research"]["active_trade_system"].update(change)
            else:
                payload["research"].pop("active_trade_system")
            rendered = mail_layout.render_text(payload)
            self.assertIn("已启用；条件式人工执行", rendered)
            self.assertIn("等待有效新数据", rendered)
            self.assertNotIn("示例公司", rendered)
            self.assertIn("实际成交胜率：未知", rendered)

    def test_snapshot_only_attaches_matching_frozen_active_context(self):
        import snapshot_mail
        for field, value, valid in (("generated_at", "2026-10-18T20:10:00+08:00", True),
                                    ("generated_at", "2026-10-18T21:00:01+08:00", False),
                                    ("input_database_sha256", "wrong", False),
                                    ("target_trade_date", "2026-10-20", False)):
            payload = self.payload()
            active = study()
            active[field] = value
            with patch("snapshot_mail.sqlite3.connect", side_effect=OSError("fixture unavailable")):
                snapshot_mail.attach_market_outlook(payload, "unused-fixture.sqlite3",
                    "2026-10-16", "2026-10-19", "a" * 64,
                    {"active_trade_system": active})
            attached = payload["research"]["active_trade_system"]
            self.assertTrue(attached["system_enabled"])
            self.assertEqual(bool(attached["plans"]), valid)
            self.assertFalse(attached["order_execution_enabled"])
            self.assertIsNone(attached["actual_win_rate"])


if __name__ == "__main__":
    unittest.main()
