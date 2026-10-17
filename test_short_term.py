"""Isolated synthetic unit fixtures ONLY; never production samples or win rates."""
import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import collector
import short_term as st


def bars(n=270):
    out = []
    for i in range(n):
        c = 6 + i*.001
        out.append({"symbol": "600000.SH", "trade_date": (dt.date(2020, 1, 1)+dt.timedelta(days=i)).isoformat(),
                    "open": c, "high": c+.1, "low": c-.1, "close": c,
                    "volume": 100000+i, "source": "SYNTHETIC_UNIT_TEST_ONLY"})
    return out


def plan():
    return {"symbol": "600000.SH", "entry_date": "2020-09-14", "earliest_exit_date": "2020-09-15",
            "time_exit_date": "2020-09-16", "entry_window": ["09:35:00", "10:30:00"], "buy_limit_cap": 7.2}


def fill(side, when, price=7., quantity=100, fees=5., tag="1"):
    return {"fill_id": side+tag, "symbol": "600000.SH", "side": side, "executed_at": when,
            "price": price, "quantity": quantity, "fees_paid": fees,
            "evidence_kind": "broker_confirmed_fill", "source_ref": "synthetic-unit-test://not-a-real-trade"}


def event(when=None, kind="limit_touch", verified=True):
    return {"symbol": "600000.SH", "kind": kind, "occurred_at": when,
            "official_limit_verified": verified, "source_ref": "synthetic-unit-test://event"}


class FeatureTests(unittest.TestCase):
    def test_history_computation_not_a_prediction(self):
        rows = bars(); result = st.daily_features(rows, rows[-1]["trade_date"])
        self.assertTrue(result["historical_250_observations"])
        self.assertIsNone(result["activity_score"])
        self.assertIsNone(result["probabilities"])
        self.assertAlmostEqual(result["ma"]["5"], 6.267)
        self.assertGreater(result["atr14"], 0)

    def test_missing_amount_and_limits_not_invented(self):
        rows = bars(); r = st.daily_features(rows, rows[-1]["trade_date"])
        self.assertIsNone(r["mean_amount20"])
        self.assertIsNone(r["activity"]["250"]["close_limit_count"])
        self.assertIsNone(r["activity"]["250"]["post_limit_next_return_mean"])

    def test_future_or_stale_endpoint_rejected(self):
        rows = bars()
        with self.assertRaises(ValueError): st.daily_features(rows, rows[-2]["trade_date"])
        with self.assertRaises(ValueError): st.daily_features(rows, "2026-09-11")

    def test_duplicate_unsorted_mixed_adjusted_invalid_rejected(self):
        rows = bars()
        for changed in (rows+rows[-1:], rows[::-1]):
            with self.assertRaises(ValueError): st.daily_features(changed, rows[-1]["trade_date"])
        for key, value in (("symbol", "000001.SZ"), ("adjustment", "qfq"), ("high", 1), ("volume", -1), ("amount", float("nan"))):
            changed = copy.deepcopy(rows); changed[-1][key] = value
            with self.assertRaises(ValueError): st.daily_features(changed, rows[-1]["trade_date"])

    def test_last_limit_not_used_for_unrevealed_next_return(self):
        rows = bars()
        for r in rows:
            r.update(official_limits_verified=True, limit_up=100., no_price_limit=False)
        rows[-1]["close"] = rows[-1]["high"] = rows[-1]["limit_up"] = 7.
        r = st.daily_features(rows, rows[-1]["trade_date"])["activity"]["20"]
        self.assertEqual(r["close_limit_count"], 1)
        self.assertEqual(r["post_limit_next_return_n"], 0)
        self.assertIsNone(r["post_limit_next_return_mean"])

    def test_source_probe_audit_has_real_inputs(self):
        # Schema test only. The separate audit-source command processes the real
        # GitHub artifact and reports those counts without treating these as data.
        self.assertEqual(st.MODEL_STATUS, "not_trained")


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.p = plan()
        self.b = fill("buy", "2020-09-14T09:40:00+08:00")
        self.s = fill("sell", "2020-09-15T10:00:00+08:00", price=7.4)

    def test_no_fill_not_loss_or_win(self):
        r = st.account_trade(self.p, [])
        self.assertEqual(r["status"], "no_fill"); self.assertIsNone(r["net_win"])

    def test_net_profit_includes_fees(self):
        r = st.account_trade(self.p, [self.b, self.s])
        self.assertAlmostEqual(r["net_profit"], 30)
        self.assertTrue(r["eligible_for_formal_score"])

    def test_price_up_can_still_lose_after_fees(self):
        self.s["price"] = 7.01
        r = st.account_trade(self.p, [self.b, self.s])
        self.assertFalse(r["net_win"])

    def test_zero_net_not_up(self):
        self.s["price"] = 7.1
        r = st.account_trade(self.p, [self.b, self.s])
        self.assertEqual(r["net_profit"], 0); self.assertFalse(r["net_win"])

    def test_Tplus1_same_day_exit_not_formal(self):
        self.s["executed_at"] = "2020-09-14T14:00:00+08:00"
        r = st.account_trade(self.p, [self.b, self.s])
        self.assertIn("Tplus1_violation", r["protocol_breaches"])
        self.assertFalse(r["eligible_for_formal_score"])
        self.assertIsNotNone(r["net_profit"])  # Breach retained, not hidden.

    def test_pending_sell_not_filled_at_stop(self):
        r = st.account_trade(self.p, [self.b])
        self.assertEqual(r["status"], "open_or_partial_exit")
        self.assertIsNone(r["net_profit"])

    def test_gap_down_uses_actual_price_not_stop(self):
        self.s["price"] = 6.
        r = st.account_trade(self.p, [self.b, self.s])
        self.assertEqual(r["net_profit"], -110)

    def test_partial_fill_and_partial_sale(self):
        self.s["quantity"] = 50
        r = st.account_trade(self.p, [self.b, self.s])
        self.assertEqual(r["remaining_quantity"], 50)
        self.assertIsNone(r["net_win"])

    def test_cost_missing_kept_unresolved(self):
        self.s["fees_paid"] = None
        r = st.account_trade(self.p, [self.b, self.s])
        self.assertIsNone(r["net_profit"]); self.assertFalse(r["eligible_for_formal_score"])

    def test_duplicate_fill_oversell_quote_as_fill_rejected(self):
        with self.assertRaises(ValueError): st.account_trade(self.p, [self.b, self.b])
        self.s["quantity"] = 200
        with self.assertRaises(ValueError): st.account_trade(self.p, [self.b, self.s])
        self.b["evidence_kind"] = "daily_high_touch"
        with self.assertRaises(ValueError): st.account_trade(self.p, [self.b])

    def test_limit_before_entry_not_a_post_entry_win(self):
        r = st.account_trade(self.p, [self.b, self.s], [event("2020-09-14T09:32:00+08:00")])
        self.assertIsNone(r["post_entry_limit_touch"])

    def test_daily_high_cannot_prove_sequence(self):
        r = st.account_trade(self.p, [self.b, self.s], [event()])
        self.assertIsNone(r["post_entry_limit_touch"])

    def test_true_post_entry_limit_and_close_are_separate(self):
        r = st.account_trade(self.p, [self.b, self.s], [event("2020-09-14T11:00:00+08:00")])
        self.assertTrue(r["post_entry_limit_touch"])
        self.assertIsNone(r["post_entry_close_limit"])

    def test_limit_after_exit_not_credited(self):
        r = st.account_trade(self.p, [self.b, self.s], [event("2020-09-15T14:00:00+08:00")])
        self.assertIsNone(r["post_entry_limit_touch"])

    def test_vendor_limit_not_official(self):
        r = st.account_trade(self.p, [self.b, self.s], [event("2020-09-14T11:00:00+08:00", verified=False)])
        self.assertIsNone(r["post_entry_limit_touch"])

    def test_no_hit_false_requires_full_coverage(self):
        cov = {"complete": True, "official_limits_verified": True, "all_days_limit_applicable": True,
               "symbol": self.p["symbol"], "start": self.b["executed_at"], "end": self.s["executed_at"], "source_ref": "unit-test://coverage"}
        r = st.account_trade(self.p, [self.b, self.s], observation_end=self.s["executed_at"], tape_coverage=cov)
        self.assertFalse(r["post_entry_limit_touch"])
        cov["all_days_limit_applicable"] = False
        r = st.account_trade(self.p, [self.b, self.s], observation_end=self.s["executed_at"], tape_coverage=cov)
        self.assertIsNone(r["post_entry_limit_touch"])

    def test_empty_history_never_80(self):
        r = st.summarize_trades([])
        self.assertIsNone(r["closed_trade_historical_frequency"])
        self.assertFalse(r["80pct_winrate_validated"])

    def test_same_frozen_plan_not_counted_twice(self):
        sample = st.account_trade(self.p, [self.b, self.s])
        with self.assertRaises(ValueError): st.summarize_trades([sample, sample])

    def test_same_day_trades_not_independent_days(self):
        sample = st.account_trade(self.p, [self.b, self.s])
        r = st.summarize_trades([dict(sample, trade_id=str(i)) for i in range(100)])
        self.assertEqual(r["independent_entry_dates"], 1)
        self.assertIsNone(r["entry_date_cluster_descriptive_95"])
        self.assertFalse(r["80pct_winrate_validated"])

    def test_all_success_not_certainty(self):
        sample = st.account_trade(self.p, [self.b, self.s]); rows = []
        for i in range(40):
            r = dict(sample, trade_id=str(i)); r["entry_date"] = (dt.date(2020, 1, 1)+dt.timedelta(days=i)).isoformat(); rows.append(r)
        result = st.summarize_trades(rows)
        self.assertLess(result["entry_date_cluster_descriptive_95"][0], 1.)
        self.assertFalse(result["80pct_winrate_validated"])


class AdmissionTests(unittest.TestCase):
    def candidate_packet(self):
        return {"base_date": "2020-09-11", "entry_date": "2020-09-14",
                "verified_open_sessions": ["2020-09-11", "2020-09-14", "2020-09-15", "2020-09-16"],
                "candidate": {"symbol": "600000.SH", "in_frozen_pool": True, "real_event_verified": True,
                              "base_day_touch_limit": False, "consecutive_limits": 0, "tick_size": .01,
                              "entry_limits_date": "2020-09-14", "entry_no_price_limit": False, "entry_limit_up": 7.15,
                              "features": {"as_of": "2020-09-11", "close": 6.5, "atr14": .15,
                                           "prior_high20": 6.51, "support20": 6.35, "bars": 270,
                                           "returns": {"5": .01, "20": .03}}},
                "execution_model": {"p_net_profit": .85, "entry_date_cluster_95_lower": .8}}

    def test_plan_formula_and_weekend_calendar_isolated_from_model_gate(self):
        # Bypass gate ONLY in this isolated formula unit test, never in CLI.
        with tempfile.TemporaryDirectory() as tmp, patch.object(st, "plan_gate", return_value=[]):
            r = st.build_conditional_plan(self.candidate_packet(), tmp)
        p = r["plan"]
        self.assertEqual(p["entry_trigger"], 6.52)
        self.assertEqual(p["buy_limit_cap"], 6.54)
        self.assertEqual(p["earliest_exit_date"], "2020-09-15")
        self.assertEqual(p["time_exit_date"], "2020-09-16")
        self.assertTrue(p["no_same_day_exit"])

    def test_formula_rejects_already_limit_and_extended_gap(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(st, "plan_gate", return_value=[]):
            packet = self.candidate_packet(); packet["candidate"]["base_day_touch_limit"] = True
            self.assertIsNone(st.build_conditional_plan(packet, tmp)["plan"])
            packet = self.candidate_packet(); packet["candidate"]["features"]["prior_high20"] = 7.
            self.assertIsNone(st.build_conditional_plan(packet, tmp)["plan"])

    def test_empty_and_data_ready_only_cannot_generate_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            for p in ({}, {"data_status": "DATA READY"}):
                r = st.build_conditional_plan(p, tmp)
                self.assertIsNone(r["plan"])
                self.assertIn("execution_model_not_validated", r["reasons"])
                self.assertIn("80pct_target_not_established", r["reasons"])

    def test_hash_mismatch_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"model.json"; path.write_text("{}")
            r = st.plan_gate({"evidence_files": {"model": {"path": "model.json", "sha256": "wrong"}}}, tmp)
            self.assertIn("missing_or_changed_evidence:model", r)

    def test_sandbox_path_escape_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = st.plan_gate({"evidence_files": {"model": {"path": "../../etc/passwd"}}}, tmp)
            self.assertIn("missing_or_changed_evidence:model", r)

    def test_rounding_to_verified_tick(self):
        self.assertEqual(st.tick_price(6.001, .01, True), 6.01)
        self.assertEqual(st.tick_price(6.009, .01), 6.00)

    def test_unzoned_timestamp_rejected(self):
        with self.assertRaises(ValueError): st.instant("2020-09-14T09:40:00")

    def test_snapshot_fetcher_constructor(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = collector.Fetcher(Path(tmp)/"raw", retries=2, timeout=15, interval=.4)
            self.assertEqual(f.interval, .4)


class ExecutionEvidenceRegressionTests(unittest.TestCase):
    """Synthetic contracts, not live executions or performance evidence."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-execution-evidence-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        raw_source = self.root / "synthetic-fill-proof.txt"
        raw_source.write_text("SYNTHETIC UNIT TEST ONLY; no actual account trades.\n", encoding="utf-8")
        self.source_ref = {"path": raw_source.name, "sha256": st.digest(raw_source)}
        self.model = {
            "status": "time_out_of_sample_validated", "label": st.LABEL,
            "strategy_version": st.VERSION, "model_version": "synthetic-unit-test-only-v1",
            "rule_frozen_at": "2020-09-11T08:00:00+08:00",
            "train_label_end": "2020-06-29", "calibration_start": "2020-06-30",
            "calibration_end": "2020-09-10", "test_start": "2020-09-14", "test_end": "2020-09-16",
            "purged_time_split_verified": True, "same_day_pool_baseline_verified": True,
            "costs_slippage_and_unfilled_included": True,
            # Deliberately untrusted summary claims: the raw fixture has one trade.
            "validation_n": 200, "independent_entry_dates": 60, "unresolved_trade_count": 0,
            "p_net_profit": .85, "entry_date_cluster_95_lower": .81, "expected_net_return": .01,
        }
        self.packet = {
            key: True for key in ("full_universe_verified", "frozen_pool_verified", "factors_verified",
                                  "official_status_verified", "official_limits_verified", "calendar_verified",
                                  "corporate_actions_verified", "news_risk_verified", "bootstrap_complete")
        }
        self.packet.update({
            "data_status": "DATA READY", "execution_model": self.model,
            "base_date": "2020-09-17", "entry_date": "2020-09-18",
            "information_cutoff": "2020-09-17T18:00:00+08:00",
            "generated_at": "2020-09-17T18:01:00+08:00", "saved_at": "2020-09-17T18:02:00+08:00",
        })
        self.proof = {
            "source_type": "broker_fill", "verified": True, "actual_fill_prices": True,
            "fees_verified": True, "buy_fill_id": "buy1", "sell_fill_id": "sell1",
            "fee_statement_id": "SYNTHETIC-UNIT-TEST-ONLY", "source_ref": self.source_ref,
            "actual_fees": {"buy_commission": 5., "buy_transfer": 0.,
                            "sell_commission": 5., "sell_transfer": 0., "sell_tax": 0.},
        }
        self.trade = {
            "plan": plan(), "plan_frozen_at": "2020-09-14T09:00:00+08:00", "signal_date": "2020-09-11",
            "fills": [fill("buy", "2020-09-14T09:40:00+08:00"),
                      fill("sell", "2020-09-15T10:00:00+08:00", price=7.4)],
            "execution_evidence": self.proof,
        }
        self.evidence = {
            "fixture_kind": "synthetic_unit_test_only_not_real_evidence",
            "real_data": True, "strategy_version": st.VERSION, "label": st.LABEL,
            "model_version": self.model["model_version"], "rule_frozen_at": self.model["rule_frozen_at"],
            "dataset_kind": "live", "trades": [self.trade],
            "verification": {key: True for key in ("frozen_forward_verified", "execution_evidence_verified",
                                                     "pit_verified", "universe_verified", "status_verified",
                                                     "action_verified", "out_of_sample_verified",
                                                     "cohort_complete_verified")},
            "summary": {"net_win_rate": .85, "n_trades": 200, "independent_entry_dates": 60},
        }

    def validation(self):
        return st._execution_validation(self.evidence, self.model, self.packet, self.root)

    def write_gate_artifacts(self):
        refs = {}
        for name in ("universe", "pool", "factors", "calendar", "status", "risk", "model", "oos_trades"):
            payload = self.model if name == "model" else self.evidence if name == "oos_trades" else {
                "fixture_kind": "synthetic_unit_test_only"}
            path = self.root / (name + ".json")
            path.write_text(json.dumps(payload), encoding="utf-8")
            refs[name] = {"path": path.name, "sha256": st.digest(path)}
        self.packet["evidence_files"] = refs

    def test_summary_claim_of_85_percent_without_raw_fills_never_unlocks_plan(self):
        self.evidence["trades"] = [{"status": "closed", "net_profit": 100., "net_win": True,
                                    "entry_date": "2020-09-14", "source": "untrusted-summary-only"}]
        report = self.validation()
        self.assertEqual(report["n_trades"], 0)
        self.assertEqual(report["n_unresolved"], 1)
        self.assertFalse(report["execution_verified"])
        self.assertFalse(report["formal_gate_passed"])
        self.write_gate_artifacts()
        reasons = st.plan_gate(self.packet, self.root)
        self.assertIn("recomputed_execution_validation_failed", reasons)
        self.assertIn("execution_sample_summary_mismatch", reasons)
        self.assertIn("execution:unresolved_trade_or_exit_evidence", reasons)
        result = st.build_conditional_plan(self.packet, self.root)
        self.assertIsNone(result["plan"])
        self.assertFalse(result["automatic_trading"])

    def test_rule_frozen_during_test_period_is_rejected(self):
        self.model["rule_frozen_at"] = self.evidence["rule_frozen_at"] = "2020-09-14T08:00:00+08:00"
        with self.assertRaisesRegex(ValueError, "not frozen before the forward holdout"):
            self.validation()
        self.write_gate_artifacts()
        self.assertIn("model_or_execution_evidence_unreadable", st.plan_gate(self.packet, self.root))

    def test_raw_fills_and_actual_fees_are_recomputed_instead_of_trusting_claims(self):
        self.trade.update(net_pnl=99999., net_profit=99999., net_win=True)
        self.trade["fills"][1]["price"] = 6.
        report = self.validation()
        self.assertEqual(report["n_trades"], 1)
        self.assertEqual(report["n_unresolved"], 0)
        self.assertEqual(report["wins"], 0)
        self.assertEqual(report["sum_net_pnl"], -110.)
        self.assertTrue(report["each_trade_execution_evidence_verified"])
        self.assertFalse(report["formal_gate_passed"])

    def test_complete_raw_fixture_still_requires_enough_independent_dates(self):
        report = self.validation()
        self.assertEqual(report["n_trades"], 1)
        self.assertEqual(report["n_unresolved"], 0)
        self.assertEqual(report["sum_net_pnl"], 30.)
        self.assertEqual(report["independent_entry_dates"], 1)
        self.assertTrue(report["each_trade_execution_evidence_verified"])
        self.assertIn("insufficient_completed_trades", report["blockers"])
        self.assertIn("insufficient_independent_entry_dates", report["blockers"])
        self.assertFalse(report["formal_gate_passed"])

    def test_itemized_fee_tampering_becomes_unresolved_and_stays_in_denominator(self):
        self.trade["execution_evidence"]["actual_fees"]["sell_commission"] = 0.
        report = self.validation()
        self.assertEqual(report["n_trades"], 0)
        self.assertEqual(report["n_unresolved"], 1)
        self.assertEqual(report["n_attempted_trades"], 1)
        self.assertEqual(report["win_rate"], 0.)
        self.assertIn("fees disagree", report["unresolved_records"][0]["reason"])
        self.assertFalse(report["formal_gate_passed"])

    def test_source_proof_missing_changed_or_fill_ids_mismatched_is_unresolved(self):
        cases = ("missing", "changed_hash", "mismatched_ids", "outside_root")
        original = copy.deepcopy(self.proof)
        for case in cases:
            with self.subTest(case=case):
                self.trade["execution_evidence"] = copy.deepcopy(original)
                proof = self.trade["execution_evidence"]
                if case == "missing":
                    proof["source_ref"]["path"] = "nonexistent-proof.txt"
                elif case == "changed_hash":
                    proof["source_ref"]["sha256"] = "0" * 64
                elif case == "mismatched_ids":
                    proof["sell_fill_id"] = "different-sell-id"
                else:
                    proof["source_ref"]["path"] = "../outside-proof.txt"
                report = self.validation()
                self.assertEqual(report["n_trades"], 0)
                self.assertEqual(report["n_unresolved"], 1)
                self.assertFalse(report["each_trade_execution_evidence_verified"])
                self.assertFalse(report["formal_gate_passed"])

    def test_fill_ids_cannot_be_reused_by_a_different_frozen_plan(self):
        second = copy.deepcopy(self.trade)
        second["plan"]["buy_limit_cap"] = 7.19
        self.evidence["trades"].append(second)
        report = self.validation()
        self.assertEqual(report["n_trades"], 1)
        self.assertEqual(report["n_unresolved"], 1)
        self.assertEqual(report["n_attempted_trades"], 2)
        self.assertEqual(report["win_rate"], .5)
        self.assertIn("fill reused by another trade", report["unresolved_records"][0]["reason"])
        self.assertFalse(report["formal_gate_passed"])

    def test_incomplete_forward_cohort_blocks_otherwise_recomputed_trade(self):
        self.evidence["verification"]["cohort_complete_verified"] = False
        report = self.validation()
        self.assertEqual(report["n_trades"], 1)
        self.assertEqual(report["n_unresolved"], 0)
        self.assertIn("forward_cohort_or_holdout_unverified", report["blockers"])
        self.assertFalse(report["formal_gate_passed"])


if __name__ == "__main__": unittest.main()
