"""Decision-boundary tests for frozen T+1 plans (no external market claims)."""
import math
import unittest

from pa_trade_engine import classify_context, evaluate_setup, make_plan, ceil_cent


def breakout(**updates):
    f = dict(close=20.0, open=19.94, high=20.05, low=19.94, atr14=0.4,
             ret20=0.08, ma20_gap=0.01, ma20_slope5=0.004, efficiency20=0.4,
             close_location=0.85, body_fraction=0.55, lower_wick_fraction=0.0,
             overlap5=0.3, prior20_high=20.7, prior20_low=18.5, prior60_high=20.7,
             prior5_low=19.8, previous_open=19.7, previous_high=20.0,
             previous_low=19.8, previous_close=19.98, previous_body_fraction=0.7,
             previous_close_location=0.95, previous_range_atr=1.0,
             breakout_level=19.9, prior_range_height=1.4, prior_efficiency20=0.4,
             prior_overlap5=0.3, market_breadth=0.6, market_mean_return=0.002,
             relative_return5=0.01, median_amount20=100000000,
             code="000001", signal_date="2026-10-16")
    f.update(updates)
    return f


class FrozenPlanTests(unittest.TestCase):
    def test_breakout_followthrough_produces_defined_three_percent_plan(self):
        p = make_plan(breakout(), "breakout_followthrough")
        self.assertTrue(p["eligible"], p["reasons"])
        self.assertEqual(p["target_pct"], 0.03)
        self.assertGreaterEqual(p["reward_risk"], 1.5)
        self.assertLessEqual(p["entry_cap"], 20 * 1.025)

    def test_breakout_requires_the_next_bar_followthrough(self):
        p = make_plan(breakout(close=19.97), "breakout_followthrough")
        self.assertFalse(p["eligible"])
        self.assertIn("followthrough_not_confirmed", p["reasons"])

    def test_weak_initial_breakout_is_rejected_even_with_green_followthrough(self):
        p = make_plan(breakout(previous_body_fraction=0.54), "breakout_followthrough")
        self.assertIn("previous_breakout_bar_weak", p["reasons"])
        self.assertFalse(p["eligible"])

    def test_context_is_required_for_pullback(self):
        f = breakout(open=19.85, low=19.7, previous_open=20.0, previous_close=19.95,
                     lower_wick_fraction=0.43, prior5_low=19.7, prior60_high=22.0,
                     ma20_gap=0.008)
        self.assertTrue(make_plan(f, "trend_pullback_rejection")["eligible"])
        f["ma20_slope5"] = -0.004
        self.assertIn("bull_context_required", make_plan(f, "trend_pullback_rejection")["reasons"])

    def test_range_reclaim_at_lower_edge_and_its_context(self):
        f = breakout(close=19.8, open=19.65, high=19.82, low=19.5,
                     previous_close=19.9, previous_open=20.0, prior20_low=19.6,
                     prior20_high=20.4, prior_efficiency20=0.2, prior_overlap5=0.6,
                     ma20_gap=-0.01, ma20_slope5=0.0, close_location=0.94)
        p = make_plan(f, "range_failed_breakout")
        self.assertTrue(p["eligible"], p["reasons"])
        f["prior_overlap5"] = 0.44
        self.assertFalse(make_plan(f, "range_failed_breakout")["eligible"])

    def test_global_market_liquidity_gate_prevents_valid_pattern(self):
        for change, reason in ((dict(median_amount20=19999999), "insufficient_liquidity"),
                               (dict(market_breadth=0.34), "market_breadth_too_weak"),
                               (dict(market_mean_return=-0.016), "market_return_too_weak")):
            with self.subTest(change=change):
                p = make_plan(breakout(**change), "breakout_followthrough")
                self.assertFalse(p["eligible"])
                self.assertIn(reason, p["reasons"])

    def test_large_stop_is_not_fixed_by_moving_or_chasing_entry(self):
        p = make_plan(breakout(previous_low=18.0), "breakout_followthrough")
        self.assertFalse(p["eligible"])
        self.assertIn("entry_cap_below_signal_close", p["reasons"])
        self.assertEqual(p["structural_stop"], 17.94)

    def test_six_percent_requires_trend_and_sufficient_resistance_room(self):
        p = make_plan(breakout(prior60_high=22.0, previous_low=19.5), "breakout_followthrough")
        self.assertTrue(p["eligible"], p["reasons"])
        self.assertEqual(p["target_pct"], 0.06)
        q = make_plan(breakout(prior60_high=22.0, previous_low=19.5, ma20_slope5=0.0), "breakout_followthrough")
        self.assertFalse(q["eligible"])

    def test_three_percent_target_jointly_reduces_cap_instead_of_forcing_six(self):
        p = make_plan(breakout(prior60_high=22.0), "breakout_followthrough")
        self.assertTrue(p["eligible"], p["reasons"])
        self.assertEqual(p["target_pct"], 0.03)
        self.assertEqual(p["entry_cap"], 20.14)
        self.assertGreaterEqual(p["reward_risk"], 1.5)

    def test_future_labels_and_future_prices_cannot_change_plan_identity(self):
        a = make_plan(breakout(), "breakout_followthrough")
        b = make_plan(breakout(future_label=1, day2_close=200, first_day_green=False), "breakout_followthrough")
        self.assertEqual(a, b)

    def test_breakout_without_bull_context_is_not_a_trend_entry(self):
        p = make_plan(breakout(ma20_slope5=0.0029), "breakout_followthrough")
        self.assertFalse(p["eligible"])
        self.assertIn("bull_context_required", p["reasons"])

    def test_reference_lot_obeys_both_cash_and_stressed_risk_limits(self):
        p = make_plan(breakout(), "breakout_followthrough")
        self.assertGreater(p["quantity_ref"], 0)
        self.assertEqual(p["quantity_ref"] % 100, 0)
        self.assertLessEqual(p["reference_cash"], 10000)
        self.assertLessEqual(p["stressed_reference_loss"], p["risk_budget"])
        self.assertGreater(p["stress_breakeven_probability"], p["breakeven_probability"])

    def test_one_lot_cannot_be_funded_by_reallocating_risk(self):
        f = breakout(close=100, open=99.7, high=100.1, low=99.5, atr14=1.5,
                     previous_open=97.9, previous_close=99.8, previous_high=99.9,
                     previous_low=97.65, previous_range_atr=1.5,
                     breakout_level=99.5, prior60_high=110)
        p = make_plan(f, "breakout_followthrough")
        self.assertFalse(p["eligible"])
        self.assertEqual(p["quantity_ref"], 0)
        self.assertIn("risk_budget_below_one_board_lot", p["reasons"])

    def test_unknown_and_missing_or_nonfinite_inputs_abstain(self):
        self.assertFalse(make_plan(breakout(), "unknown")["eligible"])
        for value in (None, math.nan, math.inf):
            with self.subTest(value=value):
                p = make_plan(breakout(atr14=value), "breakout_followthrough")
                self.assertFalse(p["eligible"])
                self.assertTrue(p["reasons"])

    def test_frozen_identity_and_tplusone_metadata(self):
        a = make_plan(breakout(), "breakout_followthrough")
        b = make_plan(dict(reversed(list(breakout().items()))), "breakout_followthrough")
        self.assertEqual(a["pa_plan_id"], b["pa_plan_id"])
        self.assertNotEqual(a["pa_plan_id"], make_plan(breakout(signal_date="2026-10-15"), "breakout_followthrough")["pa_plan_id"])
        self.assertEqual(a["frozen_entry_floor"], ceil_cent(max(5, 20 * 0.99, a["structural_stop"] + 0.01)))
        self.assertEqual((a["entry_earliest"], a["sale_earliest"]), ("T+1", "T+2"))
        self.assertEqual(a["quantity_frozen_at"], "T_close")

    def test_breakout_projection_used_only_when_no_higher_historical_high(self):
        p = make_plan(breakout(prior60_high=19.9), "breakout_followthrough")
        self.assertAlmostEqual(p["resistance"], 21.3)
        q = evaluate_setup(breakout(), "breakout_followthrough")
        self.assertTrue(q["eligible"])

    def test_context_and_tick_rounding_boundaries(self):
        self.assertEqual(classify_context(breakout()), "bull")
        self.assertEqual(classify_context(breakout(ma20_gap=-0.01, ma20_slope5=-0.004)), "bear")
        self.assertEqual(classify_context(breakout(ma20_gap=-0.01, ma20_slope5=0,
                                                  prior_efficiency20=0.3, prior_overlap5=0.45)), "range")
        self.assertEqual(ceil_cent(10.001), 10.01)


if __name__ == "__main__":
    unittest.main()
