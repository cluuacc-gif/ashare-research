"""Frozen, research-only A-share price-action planning rules.

All numeric thresholds are this project's hypotheses, not creator quotations.
Inputs must be known at signal-day close. Plans are for T+1 entry; shares bought
that day cannot be sold before T+2. Structural stops are not guaranteed fills.
"""

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import math


SETUPS = (
    "breakout_followthrough",
    "trend_pullback_rejection",
    "range_failed_breakout",
)
ACCOUNT_REFERENCE = 30000.0
POSITION_CASH_LIMIT = 10000.0
RISK_FRACTION = 0.005
GAP_STRESS = 0.02
EPSILON = 1e-9
RULE_VERSION = "pa-t1-frozen-20261018-v1"
RULE_DEFINITIONS = {
    "attribution": "project hypotheses; not verified creator thresholds",
    "bull_context": "ma20_gap>=0; ma20_slope5>=.003; efficiency20>=.2; ret20>0",
    "range_context": "prior_efficiency20<=.3; prior_overlap5>=.45",
    "global_filters": "amount20>=20m; breadth>=.35; mean_return>=-.015; ATR/close .008..05; ret1 -.04...06; close 5..100",
    "breakout_followthrough": "bull_context; prevclose>breakout_level; prevclose>prevopen; prevbody>=.55; prevclose_location>=.75; prev_range_atr .8..1.8; close>open; close>=prevclose; low>=breakout_level-.2ATR; close_location>=.6; stop=floor_cent(min(low,prevlow,breakout_level)-.15ATR)",
    "trend_pullback_rejection": "bull_context; low<=MA20+.35ATR; close>MA20; close>open; close_location>=.65; lower_wick>=.25; prevclose<prevopen; stop=floor_cent(min(low,prior5low)-.15ATR)",
    "range_failed_breakout": "prior_range_context; low<prior20low-.1ATR; close>prior20low+.1ATR; close>open; close_location>=.75; (close-prior20low)/(prior20high-prior20low)<=.35; stop=floor_cent(low-.15ATR)",
    "entry_cap": "for target,RR in [(3%,1.5),(6%,1.8)]: risk_max=min(.03,target/RR); cap=floor_cent(min(Tclose*1.025,100,stop/(1-risk_max),resistance/(1+target))); require cap>=Tclose and .008<=risk<=risk_max and room>=target; 6% requires bull context",
    "entry_floor": "ceil_cent(max(5,Tclose*.99,stop+.01))",
    "risk": "structural stop frozen on T; risk .008...03; reference budget 30000*.005; extra 2% gap stress",
    "targets": "jointly solve admissible cap/target; first valid 3% with RR>=1.5, otherwise 6% only with bull context and RR>=1.8",
    "quantity": "100-share lots; min(10000/cap,150/(cap-stop+cap*.02)); fixed before entry",
    "execution": "daily D1-open proxy; no D1 sale; minute data required for intraday conditional fills",
}
CAUSAL_PLAN_KEYS = (
    "symbol", "code", "date", "signal_date", "trade_date", "close", "open", "high", "low",
    "atr14", "ret20", "ma20_gap", "ma20_slope5", "efficiency20", "close_location",
    "body_fraction", "lower_wick_fraction", "overlap5", "prior20_high", "prior20_low",
    "prior60_high", "prior5_low", "previous_open", "previous_high", "previous_low",
    "previous_close", "previous_body_fraction", "previous_close_location",
    "previous_range_atr", "breakout_level", "prior_range_height", "prior_efficiency20",
    "prior_overlap5", "market_breadth", "market_mean_return", "relative_return5", "median_amount20",
)


def _plan_id(features, setup):
    causal = {key: features[key] for key in CAUSAL_PLAN_KEYS if key in features}
    payload = json.dumps({"rules": RULE_VERSION, "definitions": RULE_DEFINITIONS,
                          "setup": setup, "features": causal},
                         sort_keys=True, default=str, ensure_ascii=True)
    return "pa-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def floor_cent(value):
    """Round toward a less aggressive buy/stop price at the one-cent tick."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_FLOOR))


def ceil_cent(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def _number(features, key):
    value = features.get(key)
    if isinstance(value, bool):
        raise ValueError("invalid_feature:" + key)
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise ValueError("missing_or_invalid_feature:" + key) from None
    if not math.isfinite(value):
        raise ValueError("nonfinite_feature:" + key)
    return value


def _bull_context(features):
    return (
        _number(features, "ma20_gap") >= 0
        and _number(features, "ma20_slope5") >= 0.003
        and _number(features, "efficiency20") >= 0.2
        and _number(features, "ret20") > 0
    )


def _range_context(features):
    return (
        _number(features, "prior_efficiency20") <= 0.3
        and _number(features, "prior_overlap5") >= 0.45
    )


def classify_context(features):
    """Return bull, bear, range or unclear; malformed inputs return unclear."""
    try:
        if _bull_context(features):
            return "bull"
        if _number(features, "ma20_gap") < 0 and _number(features, "ma20_slope5") < -0.003:
            return "bear"
        if _range_context(features):
            return "range"
    except ValueError:
        pass
    return "unclear"


def evaluate_setup(features, setup):
    """Check the named pattern, independently of market and funding gates."""
    result = {"eligible": False, "reasons": [], "context": classify_context(features),
              "structural_stop": None, "setup": setup}
    if setup not in SETUPS:
        result["reasons"].append("unknown_setup")
        return result
    try:
        c, o, h, l, atr = (_number(features, key) for key in ("close", "open", "high", "low", "atr14"))
        if min(c, o, h, l, atr) <= 0 or h < max(c, o, l) or l > min(c, o, h):
            raise ValueError("invalid_ohlc_or_atr")
        pc, po = (_number(features, key) for key in ("previous_close", "previous_open"))
        cl = _number(features, "close_location")
        reasons = result["reasons"]
        if setup == "breakout_followthrough":
            if not _bull_context(features):
                reasons.append("bull_context_required")
            level = _number(features, "breakout_level")
            pl = _number(features, "previous_low")
            if not (pc > level and pc > po):
                reasons.append("previous_bar_not_bull_breakout")
            if _number(features, "previous_body_fraction") < 0.55 or _number(features, "previous_close_location") < 0.75:
                reasons.append("previous_breakout_bar_weak")
            if not 0.8 <= _number(features, "previous_range_atr") <= 1.8:
                reasons.append("previous_breakout_range_outside_bounds")
            if not (c > o and c >= pc and l >= level - 0.2 * atr and cl >= 0.6):
                reasons.append("followthrough_not_confirmed")
            stop = min(l, pl, level) - 0.15 * atr
        elif setup == "trend_pullback_rejection":
            if not _bull_context(features):
                reasons.append("bull_context_required")
            ma_gap = _number(features, "ma20_gap")
            if ma_gap <= -1:
                raise ValueError("invalid_ma20_gap")
            ma20 = c / (1 + ma_gap)
            if not (l <= ma20 + 0.35 * atr and c > ma20):
                reasons.append("pullback_not_at_ma20")
            if not (c > o and cl >= 0.65 and _number(features, "lower_wick_fraction") >= 0.25 and pc < po):
                reasons.append("pullback_rejection_not_confirmed")
            stop = min(l, _number(features, "prior5_low")) - 0.15 * atr
        else:
            if not _range_context(features):
                reasons.append("prior_range_context_required")
            floor = _number(features, "prior20_low")
            ceiling = _number(features, "prior20_high")
            if ceiling <= floor:
                raise ValueError("invalid_prior_range")
            position = (c - floor) / (ceiling - floor)
            if not (l < floor - 0.1 * atr and c > floor + 0.1 * atr and c > o and cl >= 0.75 and position <= 0.35):
                reasons.append("failed_breakout_reclaim_not_confirmed")
            stop = l - 0.15 * atr
        result["structural_stop"] = floor_cent(stop)
        result["eligible"] = not reasons
    except ValueError as exc:
        result["reasons"].append(str(exc))
    return result


def make_plan(features, setup):
    """Build a capped-entry research plan, or abstain with explicit reasons.

    breakeven_probability values are mathematical gross break-even thresholds,
    not estimated hit rates. quantity_ref includes an extra 2% loss stress and
    only illustrates a 30,000 yuan account with a 10,000 yuan position limit.
    """
    plan = {"eligible": False, "reasons": [], "setup": setup,
            "pa_plan_id": _plan_id(features, setup), "rule_version": RULE_VERSION,
            "context": classify_context(features), "entry_cap": None,
            "frozen_entry_floor": None,
            "structural_stop": None, "target_pct": None, "reward_risk": None,
            "breakeven_probability": None, "stress_breakeven_probability": None,
            "quantity_ref": 0, "risk_budget": ACCOUNT_REFERENCE * RISK_FRACTION,
            "account_reference": ACCOUNT_REFERENCE, "gap_stress_pct": GAP_STRESS,
            "entry_earliest": "T+1", "sale_earliest": "T+2",
            "execution_model": "daily_D1_open_proxy",
            "quantity_frozen_at": "T_close",
            "stop_execution_note": "No same-day sale of newly purchased shares; gap/limit losses may exceed budget."}
    try:
        c = _number(features, "close")
        atr = _number(features, "atr14")
        pc = _number(features, "previous_close")
        if c <= 0 or atr <= 0 or pc <= 0:
            raise ValueError("nonpositive_price_or_atr")
        checks = (
            (_number(features, "median_amount20") >= 20000000, "insufficient_liquidity"),
            (_number(features, "market_breadth") >= 0.35, "market_breadth_too_weak"),
            (_number(features, "market_mean_return") >= -0.015, "market_return_too_weak"),
            (0.008 - EPSILON <= atr / c <= 0.05 + EPSILON, "atr_pct_outside_bounds"),
            (-0.04 - EPSILON <= c / pc - 1 <= 0.06 + EPSILON, "daily_return_outside_bounds"),
            (5 <= c <= 100, "price_outside_bounds"),
        )
        plan["reasons"].extend(reason for valid, reason in checks if not valid)
        pattern = evaluate_setup(features, setup)
        plan["structural_stop"] = pattern["structural_stop"]
        plan["reasons"].extend(pattern["reasons"])
        if plan["reasons"]:
            return plan
        stop = pattern["structural_stop"]
        if not (0 < stop < c):
            plan["reasons"].append("invalid_structural_stop")
            return plan
        if setup == "range_failed_breakout":
            resistance = _number(features, "prior20_high")
        else:
            previous_high = _number(features, "prior60_high")
            if previous_high > c:
                resistance = previous_high
            elif setup == "breakout_followthrough":
                height = _number(features, "prior_range_height")
                if height <= 0:
                    raise ValueError("invalid_prior_range_height")
                resistance = _number(features, "breakout_level") + height
            else:
                plan["reasons"].append("no_defined_overhead_target")
                return plan
        plan["resistance"] = resistance
        plan["frozen_entry_floor"] = ceil_cent(max(5.0, c * 0.99, stop + 0.01))
        alternatives = [(0.03, 1.5)]
        if _bull_context(features):
            alternatives.append((0.06, 1.8))
        selected = None
        attempts = []
        for target, minimum_rr in alternatives:
            risk_max = min(0.03, target / minimum_rr)
            cap = floor_cent(min(c * 1.025, 100.0, stop / (1 - risk_max), resistance / (1 + target)))
            risk = (cap - stop) / cap if cap > 0 else -1
            room = resistance / cap - 1 if cap > 0 else -1
            attempts.append((cap, risk, room))
            if cap >= c - EPSILON and 0.008 - EPSILON <= risk <= risk_max + EPSILON and room >= target - EPSILON:
                selected = (target, cap, risk, room)
                break
        if selected is None:
            plan["entry_cap"] = attempts[-1][0]
            reason = "entry_cap_below_signal_close" if all(item[0] < c - EPSILON for item in attempts) else "insufficient_target_room_or_reward_risk"
            plan["reasons"].append(reason)
            return plan
        target, cap, risk, room = selected
        plan.update(entry_cap=cap, structural_risk_pct=risk, upside_room_pct=room)
        stress_loss = risk + GAP_STRESS
        raw_quantity = min(POSITION_CASH_LIMIT / cap, plan["risk_budget"] / (cap - stop + cap * GAP_STRESS))
        quantity = int(math.floor((raw_quantity + EPSILON) / 100)) * 100
        plan.update(target_pct=target, reward_risk=target / risk,
                    breakeven_probability=risk / (target + risk),
                    stress_breakeven_probability=stress_loss / (target + stress_loss),
                    quantity_ref=quantity, target_price_ref=ceil_cent(cap * (1 + target)),
                    reference_cash=quantity * cap,
                    stressed_reference_loss=quantity * (cap - stop + cap * GAP_STRESS))
        if quantity <= 0:
            plan["reasons"].append("risk_budget_below_one_board_lot")
            return plan
        plan["eligible"] = True
    except ValueError as exc:
        plan["reasons"].append(str(exc))
    return plan
