"""Synthetic boundary tests, never strategy acceptance evidence."""
import copy
from datetime import date, timedelta
import json
import unittest

from strategy_validation import CostConfig, calculate_trade, validate_strategy


def evidence():
    return {"source_type": "broker_fill", "verified": True, "actual_fill_prices": True,
            "fees_verified": True, "buy_fill_id": "fixture-buy", "sell_fill_id": "fixture-sell",
            "fee_statement_id": "fixture-statement", "actual_fees": {
                "buy_commission": 5, "sell_commission": 5, "buy_transfer": .1,
                "sell_transfer": .11, "sell_tax": 5.5}}


def fixture(count=300, days=100, verified=False):
    result = []
    for i in range(count):
        day = date(2025, 1, 1) + timedelta(days=i % days)
        row = calculate_trade(10, 10.5, 100, day.isoformat(), (day + timedelta(days=1)).isoformat(),
                              evidence=evidence() if verified else None)
        row["symbol"] = f"fixture-{i}"
        result.append(row)
    return result


VERIFIED = {"execution_evidence_verified": True, "pit_verified": True, "universe_verified": True,
            "status_verified": True, "action_verified": True, "out_of_sample_verified": True}


class CostTests(unittest.TestCase):
    def test_minimum_fees_can_turn_gross_win_into_net_loss(self):
        row = calculate_trade(10, 10.01, 100, "2026-10-15", "2026-10-16", config=CostConfig(slippage_bps=0))
        self.assertGreater(row["gross_pnl"], 0)
        self.assertLess(row["net_pnl"], 0)
        self.assertFalse(row["win"])
        self.assertEqual(row["fees"]["buy_commission"], 5)
        self.assertEqual(row["fees"]["sell_commission"], 5)
        self.assertFalse(row["execution_verified"])
        self.assertEqual(row["cost_basis"], "configurable_research_assumptions")

    def test_whole_lots_and_t_plus_one(self):
        for qty in (0, 1, 99, 101, 100.0, True):
            with self.assertRaises(ValueError):
                calculate_trade(10, 11, qty, "2026-10-15", "2026-10-16")
        for exit_ in ("2026-10-15", "2026-10-14"):
            with self.assertRaisesRegex(ValueError, "T\\+1"):
                calculate_trade(10, 11, 100, "2026-10-15", exit_)
        with self.assertRaisesRegex(ValueError, "belong"):
            calculate_trade(10, 11, 100, "2026-10-16", "2026-10-17", trading_dates=["2026-10-16", "2026-10-19"])
        row = calculate_trade(10, 11, 100, "2026-10-16", "2026-10-19", trading_dates=["2026-10-16", "2026-10-19"])
        self.assertTrue(row["calendar_verified"])

    def test_slippage_each_side_and_cash_reconciliation(self):
        row = calculate_trade(10, 11, 100, "2026-10-15", "2026-10-16", config=CostConfig(slippage_bps=20))
        self.assertAlmostEqual(row["slippage_cost"], 4.2)
        self.assertAlmostEqual(row["gross_pnl"] - row["total_cost"], row["net_pnl"])
        self.assertAlmostEqual(row["sell_cash"] - row["buy_cash"], row["net_pnl"])
        self.assertAlmostEqual(row["net_return"], row["net_pnl"] / row["buy_cash"])

    def test_actual_fills_use_itemized_actual_costs_without_second_slippage(self):
        row = calculate_trade(10, 11, 100, "2026-10-15", "2026-10-16", config=CostConfig(slippage_bps=100), evidence=evidence())
        self.assertEqual(row["slippage_cost"], 0)
        self.assertAlmostEqual(row["net_pnl"], 100 - 15.71)
        self.assertTrue(row["execution_verified"])
        self.assertEqual(row["cost_basis"], "reviewed_actual_fees")

    def test_bare_or_market_print_flags_cannot_verify_fill(self):
        for ev in ({"verified": True}, {**evidence(), "source_type": "daily_ohlc"},
                   {**evidence(), "actual_fees": {}}, {**evidence(), "source_type": "tick_execution_audit"}):
            row = calculate_trade(10, 11, 100, "2026-10-15", "2026-10-16", evidence=ev)
            self.assertFalse(row["execution_verified"])

    def test_nonfinite_or_negative_costs_rejected(self):
        for value in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                CostConfig(slippage_bps=value)
        with self.assertRaises(ValueError):
            calculate_trade(float("nan"), 11, 100, "2026-10-15", "2026-10-16")


class ValidationTests(unittest.TestCase):
    def test_zero_trades_and_cash_never_count_as_wins(self):
        result = validate_strategy([{"status": "abstain"}, {"status": "cash"}])
        self.assertEqual(result["n_trades"], 0)
        self.assertEqual(result["n_abstentions"], 2)
        self.assertIsNone(result["win_rate"])
        self.assertIsNone(result["profit_factor"])
        self.assertFalse(result["formal_gate_passed"])
        json.dumps(result, allow_nan=False)

    def test_perfect_proxy_never_formal(self):
        result = validate_strategy(fixture(300), verification=VERIFIED)
        self.assertEqual(result["win_rate"], 1)
        self.assertFalse(result["execution_verified"])
        self.assertFalse(result["claim_80_allowed"])
        self.assertFalse(result["formal_gate_passed"])
        self.assertEqual(result["date_block_bootstrap"]["samples"], 2000)
        json.dumps(result, allow_nan=False)

    def test_under_eighty_not_rewritten_and_costs_are_net(self):
        rows = fixture(300)
        for row in rows[:75]:
            row.update(net_pnl=-50, net_return=-.05)
        result = validate_strategy(rows)
        self.assertEqual(result["win_rate"], .75)
        self.assertIn("net_win_rate_not_above_target", result["blockers"])

    def test_unknown_exits_count_in_denominator_and_block_formal(self):
        rows = fixture(300, verified=True)
        rows += [{"status": "unresolved", "entry_date": "2025-01-01", "reason": "missing exit fill"}] * 100
        result = validate_strategy(rows, dataset_kind="live", verification=VERIFIED)
        self.assertEqual(result["n_trades"], 300)
        self.assertEqual(result["n_attempted_trades"], 400)
        self.assertEqual(result["win_rate"], .75)
        self.assertEqual(result["closed_trade_win_rate"], 1)
        self.assertIn("unresolved_trade_or_exit_evidence", result["blockers"])
        self.assertFalse(result["formal_gate_passed"])

    def test_same_day_exit_and_nonfinite_closed_row_remain_unresolved(self):
        rows = fixture(2)
        rows[0]["exit_date"] = rows[0]["entry_date"]
        rows[1]["net_pnl"] = float("nan")
        result = validate_strategy(rows)
        self.assertEqual(result["n_trades"], 0)
        self.assertEqual(result["n_unresolved"], 2)
        self.assertEqual(result["win_rate"], 0)
        json.dumps(result, allow_nan=False)

    def test_independent_dates_and_sample_minimum(self):
        small = validate_strategy(fixture(199, 100))
        clustered = validate_strategy(fixture(300, 2))
        self.assertIn("insufficient_completed_trades", small["blockers"])
        self.assertIn("insufficient_independent_entry_dates", clustered["blockers"])
        self.assertEqual(clustered["independent_entry_dates"], 2)

    def test_large_loss_tail_blocks_despite_high_win_rate(self):
        rows = fixture(300)
        for row in rows:
            row.update(net_pnl=1, net_return=.001)
        for row in rows[:15]:
            row.update(net_pnl=-100, net_return=-.1)
        result = validate_strategy(rows)
        self.assertEqual(result["win_rate"], .95)
        self.assertLess(result["profit_factor"], 1)
        self.assertLess(result["mean_net_return"], 0)
        self.assertIn("nonpositive_or_unknown_net_expectancy", result["blockers"])

    def test_date_bootstrap_deterministic_and_respects_date_clustering(self):
        rows = fixture(300, 100)
        for row in rows:
            if row["entry_date"] < "2025-01-20":
                row.update(net_pnl=-10, net_return=-.01)
        first = validate_strategy(rows, seed=42, bootstrap_samples=1000)
        second = validate_strategy(copy.deepcopy(rows), seed=42, bootstrap_samples=1000)
        self.assertEqual(first["date_block_bootstrap"], second["date_block_bootstrap"])
        self.assertGreater(first["win_rate"], .8)
        self.assertLess(first["date_block_bootstrap"]["win_rate_ci95"][0], .8)
        self.assertIn("date_block_95pct_lower_not_above_target", first["blockers"])

    def test_actual_evidence_and_all_gates_needed(self):
        rows = fixture(300, verified=True)
        result = validate_strategy(rows, dataset_kind="live", verification=VERIFIED)
        self.assertTrue(result["formal_gate_passed"])
        self.assertTrue(result["realized_live_80pct_verified"])
        self.assertFalse(result["out_of_sample_80pct_verified"])
        for key in ("execution_evidence_verified", "pit_verified", "universe_verified", "status_verified", "action_verified"):
            flags = {**VERIFIED, key: False}
            self.assertFalse(validate_strategy(rows, dataset_kind="live", verification=flags)["formal_gate_passed"])
        broken = copy.deepcopy(rows)
        broken[0]["execution_evidence"]["fee_statement_id"] = ""
        self.assertFalse(validate_strategy(broken, dataset_kind="live", verification=VERIFIED)["formal_gate_passed"])

    def test_actual_net_values_must_reconcile_to_fill_and_fee_evidence(self):
        rows = fixture(300, verified=True)
        rows[0]["net_pnl"] *= 2
        rows[0]["net_return"] *= 2
        result = validate_strategy(rows, dataset_kind="live", verification=VERIFIED)
        self.assertFalse(result["execution_verified"])
        self.assertFalse(result["formal_gate_passed"])

    def test_out_of_sample_is_distinct_from_realized_live(self):
        rows = fixture(300, verified=True)
        result = validate_strategy(rows, dataset_kind="out_of_sample", verification=VERIFIED)
        self.assertTrue(result["out_of_sample_80pct_verified"])
        self.assertFalse(result["realized_live_80pct_verified"])
        flags = {**VERIFIED, "out_of_sample_verified": False}
        self.assertFalse(validate_strategy(rows, dataset_kind="out_of_sample", verification=flags)["formal_gate_passed"])

    def test_acceptance_threshold_cannot_be_relaxed(self):
        for kwargs in ({"target_win_rate": .79}, {"min_trades": 10}, {"min_entry_dates": 5}, {"bootstrap_samples": 999}):
            with self.assertRaises(ValueError):
                validate_strategy([], **kwargs)


if __name__ == "__main__":
    unittest.main()
