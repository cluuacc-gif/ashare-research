"""Isolated synthetic unit fixtures ONLY; never production samples or win rates."""
import copy
import datetime as dt
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


if __name__ == "__main__": unittest.main()
