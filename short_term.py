#!/usr/bin/env python3
"""Point-in-time features and conditional-plan accounting; never sends orders.

This is an execution/research contract, NOT a trained prediction model.
Raw daily bars cannot prove a fill or the ordering of an intraday limit hit.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import statistics
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = "short-term-contract-0.1.0"
TZ = ZoneInfo("Asia/Shanghai")
MODEL_STATUS = "not_trained"
WINDOWS = (20, 60, 120, 250)
LABEL = "net_profit_after_costs_conditional_fill_Eplus1_Eplus2"


def stamp():
    return dt.datetime.now(TZ).isoformat()


def instant(value):
    result = dt.datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timestamp must carry a timezone")
    return result.astimezone(TZ)


def number(value, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("finite numeric input required")
    if positive and value <= 0:
        raise ValueError("positive numeric input required")
    return float(value)


def tick_price(value, tick, up=False):
    value, tick = Decimal(str(value)), Decimal(str(tick))
    if value <= 0 or tick <= 0:
        raise ValueError("positive price/tick required")
    rounded = (value / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return float(rounded * tick)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def ema(values, span):
    result = [float(values[0])]
    alpha = 2 / (span + 1)
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def wilder(values, period=14):
    if len(values) < period:
        return None
    result = statistics.mean(values[:period])
    for value in values[period:]:
        result = ((period - 1) * result + value) / period
    return result


def daily_features(rows, as_of):
    """Descriptive, raw-price features. Never silently use future/adjusted bars.

    Windows are observations, not verified exchange sessions. Corporate-action
    adjusted technical signals require point-in-time factors not supplied here.
    Limit-event statistics are null unless official prices exist for every day.
    """
    dt.date.fromisoformat(as_of)
    if not rows:
        raise ValueError("no real observations")
    dates = [r["trade_date"] for r in rows]
    if dates != sorted(set(dates)) or dates[-1] != as_of:
        raise ValueError("history must be unique, ordered and end exactly at as_of")
    symbols = {r["symbol"] for r in rows}
    if len(symbols) != 1:
        raise ValueError("mixed securities")
    for row in rows:
        dt.date.fromisoformat(row["trade_date"])
        if row.get("adjustment", "raw") != "raw":
            raise ValueError("adjusted prices are not raw-price observations")
        o, h, l, c = (number(row[k], True) for k in ("open", "high", "low", "close"))
        if l > min(o, c) or h < max(o, c) or l > h:
            raise ValueError("invalid OHLC")
        for key in ("volume", "amount", "turnover"):
            if row.get(key) is not None and number(row[key]) < 0:
                raise ValueError("negative observation")
    close = [r["close"] for r in rows]
    high = [r["high"] for r in rows]
    low = [r["low"] for r in rows]
    raw_returns = [None] + [close[i] / close[i-1] - 1 for i in range(1, len(rows))]
    out = {"symbol": next(iter(symbols)), "as_of": as_of, "bars": len(rows),
           "feature_version": VERSION, "adjustment": "raw",
           "is_investment_candidate": False, "official_calendar_coverage_verified": False,
           "corporate_action_adjustment_verified": False,
           "historical_250_observations": len(rows) >= 250,
           "close": close[-1], "returns": {}, "ma": {}, "activity": {},
           "activity_score": None, "probabilities": None}
    for n in (1, 3, 5, 10, 20, 60):
        out["returns"][str(n)] = close[-1] / close[-1-n] - 1 if len(close) > n else None
    for n in (5, 10, 20, 60):
        out["ma"][str(n)] = statistics.mean(close[-n:]) if len(close) >= n else None
    tr = [max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1])) for i in range(1, len(rows))]
    out["atr14"] = wilder(tr)
    changes = [close[i]-close[i-1] for i in range(1, len(rows))]
    gain = wilder([max(x, 0) for x in changes]); loss = wilder([max(-x, 0) for x in changes])
    out["rsi14"] = (None if gain is None or loss is None else 50.0 if gain == loss == 0
                    else 100.0 if loss == 0 else 100 - 100 / (1 + gain/loss))
    if len(rows) >= 60:
        dif = [a-b for a, b in zip(ema(close, 12), ema(close, 26))]
        dea = ema(dif, 9)
        out["macd"] = {"dif": dif[-1], "dea": dea[-1], "histogram_twice": 2*(dif[-1]-dea[-1])}
    else:
        out["macd"] = None
    k = d = 50.0
    for i in range(8, len(rows)):
        hi = max(high[i-8:i+1]); lo = min(low[i-8:i+1])
        rsv = 50.0 if hi == lo else (close[i]-lo)/(hi-lo)*100
        k = k*2/3 + rsv/3; d = d*2/3 + k/3
    out["kdj"] = {"k": k, "d": d, "j": 3*k-2*d} if len(rows) >= 9 else None
    out["boll20"] = ({"mid": statistics.mean(close[-20:]),
                      "upper": statistics.mean(close[-20:])+2*statistics.pstdev(close[-20:]),
                      "lower": statistics.mean(close[-20:])-2*statistics.pstdev(close[-20:])}
                     if len(rows) >= 20 else None)
    out["prior_high20"] = max(high[-21:-1]) if len(rows) >= 21 else None
    out["support20"] = min(low[-20:]) if len(rows) >= 20 else None
    for n in (5, 20):
        amounts = [r.get("amount") for r in rows[-n:]]
        out[f"mean_amount{n}"] = (statistics.mean(amounts) if len(amounts) == n and
                                  all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in amounts) else None)
    prior_vol = [r.get("volume") for r in rows[-6:-1]]
    out["volume_vs_prior5"] = (rows[-1]["volume"]/statistics.mean(prior_vol)
                              if len(prior_vol) == 5 and rows[-1].get("volume") is not None and
                              all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in prior_vol) else None)
    for n in WINDOWS:
        sub = rows[-n:]; start = len(rows)-len(sub)
        rs = raw_returns[start:]
        official = (len(sub) == n and all(r.get("official_limits_verified") is True and
                    type(r.get("limit_up")) in (int, float) and r["limit_up"] > 0 and
                    r.get("no_price_limit") is False for r in sub))
        turnover = [r.get("turnover") for r in sub]
        valid_rs = [x for x in rs if x is not None]
        touch = [r["high"] >= r["limit_up"]-1e-8 for r in sub] if official else None
        seal = [abs(r["close"]-r["limit_up"]) < 1e-8 for r in sub] if official else None
        next_returns = ([raw_returns[start+i+1] for i in range(len(sub))
                         if seal[i] and start+i+1 < len(rows)] if official else None)
        amps = [(high[i]-low[i])/close[i-1] for i in range(max(start, 1), len(rows))]
        after_big = [raw_returns[i+1] for i in range(max(start, 1), len(rows)-1)
                     if raw_returns[i] > .05+1e-10]
        out["activity"][str(n)] = {
            "observations": len(sub), "return_observations": len(valid_rs),
            "up_gt5": sum(x > .05+1e-10 for x in valid_rs) if len(valid_rs) == n else None,
            "up_gt7": sum(x > .07+1e-10 for x in valid_rs) if len(valid_rs) == n else None,
            "down_le_minus5": sum(x <= -.05+1e-10 for x in valid_rs) if len(valid_rs) == n else None,
            "mean_raw_amplitude": statistics.mean(amps) if len(amps) == n else None,
            "post_big_return_n": len(after_big),
            "post_big_return_mean": statistics.mean(after_big) if after_big else None,
            "post_big_positive_frequency": sum(x > 0 for x in after_big)/len(after_big) if after_big else None,
            "mean_turnover": statistics.mean(turnover) if len(sub) == n and all(type(x) in (int, float) and math.isfinite(x) for x in turnover) else None,
            "touch_count": sum(touch) if official else None,
            "close_limit_count": sum(seal) if official else None,
            "failed_close_count": sum(t and not s for t, s in zip(touch, seal)) if official else None,
            "post_limit_next_return_n": len(next_returns) if official else None,
            "post_limit_next_return_mean": statistics.mean(next_returns) if next_returns else None,
            "official_limit_coverage": "verified" if official else "unverified",
        }
    return out


def plan_gate(packet, artifact_root):
    """Validate an upstream research packet, not an alternative model trainer.

    Upstream must supply frozen audited data + time-out-of-sample calibrated
    execution-label evidence. Setting data_ready alone cannot unlock prices.
    """
    root = Path(artifact_root).resolve()
    reasons = []
    for key in ("full_universe_verified", "frozen_pool_verified", "factors_verified",
                "official_status_verified", "official_limits_verified", "calendar_verified",
                "corporate_actions_verified", "news_risk_verified", "bootstrap_complete"):
        if packet.get(key) is not True:
            reasons.append(key)
    if packet.get("data_status") != "DATA READY":
        reasons.append("data_not_ready")
    refs = packet.get("evidence_files", {})
    for name in ("universe", "pool", "factors", "calendar", "status", "risk", "model", "oos_trades"):
        ref = refs.get(name, {})
        path = (root / str(ref.get("path", ""))).resolve()
        if not path.is_relative_to(root) or not path.is_file() or digest(path) != ref.get("sha256"):
            reasons.append("missing_or_changed_evidence:"+name)
    model = packet.get("execution_model", {})
    if (model.get("status") != "time_out_of_sample_validated" or model.get("label") != LABEL or
            model.get("strategy_version") != VERSION or not model.get("model_version")):
        reasons.append("execution_model_not_validated")
    try:
        model_path = (root / refs["model"]["path"]).resolve()
        if not model_path.is_relative_to(root) or json.loads(model_path.read_text(encoding="utf-8")) != model:
            reasons.append("model_artifact_mismatch")
        trades_path = (root / refs["oos_trades"]["path"]).resolve()
        if not trades_path.is_relative_to(root): raise ValueError("outside evidence root")
        evidence = json.loads(trades_path.read_text(encoding="utf-8"))
        if (evidence.get("real_data") is not True or evidence.get("strategy_version") != VERSION or
                evidence.get("label") != LABEL or not evidence.get("trades") or
                len(evidence["trades"]) != model.get("validation_n")):
            reasons.append("missing_real_execution_samples")
        if evidence.get("model_version") != model.get("model_version"):
            reasons.append("sample_model_version_mismatch")
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        reasons.append("model_or_execution_evidence_unreadable")
    if model.get("costs_slippage_and_unfilled_included") is not True:
        reasons.append("execution_costs_unverified")
    if model.get("purged_time_split_verified") is not True or model.get("same_day_pool_baseline_verified") is not True:
        reasons.append("time_split_or_baseline_unverified")
    if model.get("validation_n", 0) < 100 or model.get("independent_entry_dates", 0) < 40:
        reasons.append("insufficient_samples")
    if model.get("unresolved_trade_count", 1) != 0:
        reasons.append("unresolved_test_trades")
    probability = model.get("p_net_profit")
    lower = model.get("entry_date_cluster_95_lower")
    if (type(probability) not in (float, int) or not .8 <= probability <= 1 or
            type(lower) not in (float, int) or not .8 <= lower <= probability):
        reasons.append("80pct_target_not_established")
    if type(model.get("expected_net_return")) not in (float, int) or not math.isfinite(model["expected_net_return"]) or not model["expected_net_return"] > 0:
        reasons.append("nonpositive_or_unknown_expectancy")
    try:
        base = dt.date.fromisoformat(packet["base_date"])
        entry = dt.date.fromisoformat(packet["entry_date"])
        known = instant(packet["information_cutoff"])
        generated = instant(packet["generated_at"])
        saved = instant(packet["saved_at"])
        if not (base < entry and known <= generated <= saved <= dt.datetime.now(TZ)):
            reasons.append("invalid_information_timeline")
        if known > dt.datetime.combine(entry, dt.time(9), TZ) or saved >= dt.datetime.combine(entry, dt.time(9, 30), TZ):
            reasons.append("late_or_future_information")
        if dt.date.fromisoformat(model["test_end"]) >= entry:
            reasons.append("model_uses_future_outcomes")
        if not (model["train_label_end"] < model["calibration_start"] <= model["calibration_end"] < model["test_start"] <= model["test_end"]):
            reasons.append("overlapping_training_labels")
    except (KeyError, TypeError, ValueError):
        reasons.append("missing_timeline")
    return sorted(set(reasons))


def build_conditional_plan(packet, artifact_root):
    """Broker-neutral stop-limit research plan; not a submitted brokerage order.

    The price rule is a versioned research hypothesis, not an optimized strategy.
    Invalid/incomplete input returns no ticker list and no executable price.
    """
    reasons = plan_gate(packet, artifact_root)
    result = {"version": VERSION, "generated_at": stamp(), "mode": "diagnostic/data_unverified",
              "reasons": reasons, "plan": None, "automatic_trading": False}
    if reasons:
        return result
    candidate = packet["candidate"]
    f = candidate["features"]
    close = number(f["close"], True); atr = number(f["atr14"], True)
    if not 5 <= close <= 10 or f["as_of"] != packet["base_date"] or f["bars"] < 250:
        result["reasons"] = ["candidate_price_date_or_history_invalid"]; return result
    if candidate.get("in_frozen_pool") is not True or candidate.get("real_event_verified") is not True:
        result["reasons"] = ["candidate_not_approved_by_full_pool_and_event_research"]; return result
    if candidate.get("base_day_touch_limit") is not False or candidate.get("consecutive_limits", 1) > 0:
        result["reasons"] = ["already_limit_up_or_unverified_do_not_chase"]; return result
    if f["returns"].get("5") is None or f["returns"].get("20") is None or f["returns"]["5"] > .15 or f["returns"]["20"] > .30:
        result["reasons"] = ["already_extended_or_unverified"]; return result
    tick = number(candidate["tick_size"], True)
    if candidate.get("entry_limits_date") != packet["entry_date"] or candidate.get("entry_no_price_limit") is not False:
        result["reasons"] = ["entry_session_official_limit_unverified"]; return result
    trigger = tick_price(max(close, number(f["prior_high20"], True))+tick, tick, True)
    cap = tick_price(min(trigger + .15*atr, close*1.025, number(candidate["entry_limit_up"], True)-tick), tick)
    stop = tick_price(min(number(f["support20"], True)-tick, trigger-.8*atr), tick)
    if cap < trigger or stop <= 0 or (cap-stop)/cap > .05:
        result["reasons"] = ["entry_too_far_or_planned_risk_too_large"]; return result
    sessions = packet["verified_open_sessions"]
    try:
        i = sessions.index(packet["entry_date"])
        if sessions != sorted(set(sessions)) or i == 0 or sessions[i-1] != packet["base_date"] or len(sessions) <= i+2:
            raise ValueError("incomplete calendar")
    except (ValueError, IndexError):
        result["reasons"] = ["insufficient_verified_exit_calendar"]; return result
    target = tick_price(cap+2*(cap-stop), tick, True)
    result.update(mode="research/conditional_plan", reasons=[], plan={
        "symbol": candidate["symbol"], "base_date": packet["base_date"],
        "entry_date": sessions[i], "earliest_exit_date": sessions[i+1],
        "time_exit_date": sessions[i+2], "entry_trigger": trigger,
        "buy_limit_cap": cap, "invalidation_price": stop, "profit_trigger": target,
        "entry_window": ["09:35:00", "10:30:00"], "time_exit_trigger": "14:50:00",
        "skip_if_open_above": cap, "cancel_if_trades_below": stop,
        "skip_if_already_touched_limit_before_entry": True,
        "cancel_remainder_at": "10:30:00", "no_same_day_exit": True,
        "stop_is_not_guaranteed_execution_price": True,
        "exit_delay_if_suspended_or_no_executable_bid": True,
        "broker_capability_verified": False,
        "parameter_status": "fixed_research_hypothesis_not_backtest_optimized",
        "p_net_profit": packet["execution_model"]["p_net_profit"],
        "confidence_lower": packet["execution_model"]["entry_date_cluster_95_lower"],
        "fills_required_for_results": True,
    })
    return result


def account_trade(plan, fills, market_events=(), observation_end=None, tape_coverage=None):
    """Account confirmed fills. Never infers fills from daily highs/lows.

    fees_paid includes all commission/taxes; actual fill prices already include
    execution slippage. Missing costs -> null net win. Breaches are retained and
    disqualify formal scoring, not silently dropped as unprofitable observations.
    """
    ordered = sorted(fills, key=lambda f: instant(f["executed_at"]))
    if ordered != list(fills):
        raise ValueError("fills must be chronological")
    buy_q = sell_q = 0; buy_cash = sell_cash = 0.; fees = 0.; costs_known = True
    breaches = []; buy_times = []; sale_times = []
    refs = set()
    for fill in ordered:
        if not fill.get("fill_id") or fill["fill_id"] in refs or not fill.get("source_ref"):
            raise ValueError("unique confirmed fill and source required")
        refs.add(fill["fill_id"])
        if fill.get("evidence_kind") not in ("broker_confirmed_fill", "audited_execution_replay"):
            raise ValueError("a quote touch is not a fill")
        if fill.get("symbol") != plan["symbol"]:
            raise ValueError("fill belongs to another security")
        price = number(fill["price"], True); quantity = fill["quantity"]
        if type(quantity) is not int or quantity <= 0:
            raise ValueError("positive integer fill quantity required")
        when = instant(fill["executed_at"])
        if when > dt.datetime.now(TZ):
            raise ValueError("future fill")
        day = when.date().isoformat()
        if fill["side"] == "buy":
            buy_q += quantity; buy_cash += quantity*price; buy_times.append(when)
            if day != plan["entry_date"]: breaches.append("entry_date_mismatch")
            if price > plan["buy_limit_cap"]: breaches.append("entry_exceeded_limit_cap")
            if when.time().isoformat() < plan["entry_window"][0] or when.time().isoformat() > plan["entry_window"][1]:
                breaches.append("entry_outside_window")
        elif fill["side"] == "sell":
            sell_q += quantity; sell_cash += quantity*price; sale_times.append(when)
            if day < plan["earliest_exit_date"] or any(when.date() <= b.date() for b in buy_times):
                breaches.append("Tplus1_violation")
            if sell_q > buy_q: raise ValueError("sale exceeds owned shares")
        else:
            raise ValueError("unknown fill side")
        fee = fill.get("fees_paid")
        if fee is None:
            costs_known = False
        elif number(fee) < 0:
            raise ValueError("negative fees")
        else:
            fees += fee
    closed = buy_q > 0 and buy_q == sell_q
    net = round(sell_cash-buy_cash-fees, 2) if closed and costs_known else None
    end = instant(observation_end) if observation_end else None
    start = min(buy_times) if buy_times else None
    holding_end = max(sale_times) if closed else end
    if end and start and end < start:
        raise ValueError("observation end precedes entry")
    if end and sale_times and end < max(sale_times):
        raise ValueError("observation ends before a recorded sale")
    if end and end > dt.datetime.now(TZ):
        raise ValueError("future observation end")
    touch = seal = None
    coverage = tape_coverage or {}
    complete = bool(start and holding_end and end and end >= holding_end and
                    coverage.get("complete") is True and coverage.get("official_limits_verified") is True and
                    coverage.get("all_days_limit_applicable") is True and coverage.get("source_ref") and
                    coverage.get("symbol") == plan["symbol"] and
                    instant(coverage["start"]) <= start and instant(coverage["end"]) >= holding_end)
    if complete:
        touch = seal = False
    for event in market_events:
        if event.get("symbol") != plan["symbol"]:
            raise ValueError("event belongs to another security")
        if event.get("official_limit_verified") is not True:
            continue
        if not event.get("occurred_at") or event.get("kind") not in ("limit_touch", "close_limit"):
            continue  # A daily high without event time is NOT post-entry evidence.
        when = instant(event["occurred_at"])
        if when > dt.datetime.now(TZ):
            raise ValueError("future market event")
        if start and holding_end and start < when <= holding_end and (end is None or when <= end):
            touch = True
            if event["kind"] == "close_limit": seal = True
    trade_id = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return {"trade_id": trade_id, "status": "no_fill" if not buy_q else "closed" if closed else "open_or_partial_exit",
            "bought_quantity": buy_q, "sold_quantity": sell_q, "remaining_quantity": buy_q-sell_q,
            "net_profit": net, "net_return_on_buy_notional": net/buy_cash if net is not None else None,
            "net_win": net > 0 if net is not None else None,
            "post_entry_limit_touch": touch, "post_entry_close_limit": seal,
            "costs_verified": costs_known, "protocol_breaches": sorted(set(breaches)),
            "eligible_for_formal_score": closed and costs_known and not breaches,
            "entry_date": plan["entry_date"], "symbol": plan["symbol"],
            "unfilled_is_not_a_win_or_loss": True,
            "pending_positions_must_not_be_discarded": not closed and buy_q > 0}


def summarize_trades(results):
    ids = [r.get("trade_id") for r in results]
    if any(not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("one current result version per unique frozen trade plan required")
    eligible = [r for r in results if r["eligible_for_formal_score"]]
    pending = sum(r["status"] == "open_or_partial_exit" for r in results)
    excluded = sum(r["status"] == "closed" and not r["eligible_for_formal_score"] for r in results)
    n = len(eligible); k = sum(r["net_win"] for r in eligible)
    returns = [r["net_return_on_buy_notional"] for r in eligible]
    groups = {}
    for r in eligible: groups.setdefault(r["entry_date"], []).append(r)
    lower = upper = None
    # Delete-one-entry-date jackknife: large-cluster-count descriptive interval,
    # not a binomial interval pretending same-day stocks are independent.
    if n and len(groups) >= 40 and all(len(v) < n for v in groups.values()):
        leave = [(k-sum(r["net_win"] for r in v))/(n-len(v)) for v in groups.values()]
        center = statistics.mean(leave); g = len(groups)
        se = math.sqrt((g-1)/g*sum((p-center)**2 for p in leave))
        lower, upper = max(0, k/n-2.024*se), min(1, k/n+2.024*se)
        # Degenerate all-success/all-failure clusters do NOT establish certainty.
        # Take the envelope with descriptive Wilson rather than claiming 100%.
        z = 1.96; p = k/n; den = 1+z*z/n
        center_w = (p+z*z/(2*n))/den
        margin_w = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
        lower = min(lower, center_w-margin_w); upper = max(upper, center_w+margin_w)
    return {"closed_valid_n": n, "net_profitable_n": k,
            "closed_trade_historical_frequency": k/n if n else None,
            "independent_entry_dates": len(groups), "pending_trade_n": pending,
            "closed_invalid_n": excluded, "no_fill_n": sum(r["status"] == "no_fill" for r in results),
            "mean_net_return": statistics.mean(returns) if returns else None,
            "median_net_return": statistics.median(returns) if returns else None,
            "maximum_net_return": max(returns) if returns else None,
            "minimum_net_return": min(returns) if returns else None,
            "entry_date_cluster_descriptive_95": [lower, upper] if lower is not None else None,
            "interval_method": "envelope_of_entry_date_jackknife_large_G_and_descriptive_Wilson; serial dependence remains",
            "cohort_complete": pending == 0 and excluded == 0,
            "calibrated_predictive_probability": None,
            "80pct_winrate_validated": False,
            "note": "Historical execution accounting is not independent model validation. 样本不足时不输出预测概率。"}


def audit_source(source_dir, output):
    root = Path(source_dir).resolve(); output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("audit output must be new/empty; preserve previous versions")
    probe = json.loads((root/"source_acceptance.json").read_text(encoding="utf-8"))
    computed = []; errors = []
    for item in probe.get("history_sources", []):
        if item.get("status") != "history_250_retrieved": continue
        path = (root/item["normalized_ref"]).resolve()
        if not path.is_relative_to(root): raise ValueError("invalid source path")
        rows = json.loads(path.read_text(encoding="utf-8"))
        try:
            result = daily_features(rows, item["latest_date"])
            result.update(source_url=item["source_url"], fetched_at=item["fetched_at"],
                          input_sha256=digest(path), purpose="engineering_validation_only_not_stock_selection")
            computed.append(result)
        except (KeyError, ValueError, TypeError) as exc:
            errors.append({"path": str(path), "error": str(exc)})
    result = {"version": VERSION, "generated_at": stamp(), "data_status": "DATA NOT READY",
              "model_status": MODEL_STATUS, "mode": "diagnostic/short_term_upgrade",
              "observed_base_dates": sorted({x["as_of"] for x in computed}),
              "source_probe_sha256": digest(root/"source_acceptance.json"),
              "real_history_test_symbols": len(computed), "real_history_test_rows": sum(x["bars"] for x in computed),
              "full_universe_history_complete": False, "official_limit_history_verified": False,
              "intraday_execution_dataset_available": False, "parameters_validated": False,
              "p_net_profit": None, "p_limit_after_entry": None, "winrate_80_validated": False,
              "conditional_plans": [], "features": computed, "errors": errors}
    write_json(output/"short_term_audit.json", result)
    lines = ["# 超短线条件单模块验收", "", f"生成时间：{result['generated_at']}",
             "", "DATA NOT READY；执行口径已实现，不代表选股或80%胜率已验证。", "",
             f"真实历史计算：{len(computed)}只接口测试证券，{result['real_history_test_rows']}根原始日线。",
             "测试证券按接口验收机械抽取，不是5～10元候选，不输出买入价或排名。", "",
             "## 本次实现", "", "- E日条件买入；最早E+1卖出，E+2尾盘触发时间退出；均为交易日。",
             "- 区分触发价、最高委托价、实际成交价；超价或先触板则放弃。",
             "- 只用确认成交记录计算扣费后净收益，未成交/未卖出/费用缺失单列。",
             "- 买入后触板需要时序证据；仅日线最高价不能证明买入后涨停。",
             "- 80%仅为验证目标，数据READY本身不能解锁概率或具体条件单。", "",
             "## 尚缺", "", "全池250日历史、官方状态/限价、时点复权、事件/板块、分时成交与严格时间外校准均未完成。",
             "因此本次没有合格买入名单、条件单价位、正式预测或80%胜率结论。"]
    (output/"超短线条件单模块验收.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("audit-source"); p.add_argument("--source-dir", required=True); p.add_argument("--output", required=True)
    p = commands.add_parser("plan"); p.add_argument("--packet", required=True); p.add_argument("--artifact-root", required=True); p.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "audit-source":
        result = audit_source(args.source_dir, args.output)
    else:
        result = build_conditional_plan(json.loads(Path(args.packet).read_text(encoding="utf-8")), args.artifact_root)
        if Path(args.output).exists(): raise ValueError("refusing to overwrite a frozen plan")
        write_json(args.output, result)
    print(json.dumps({k: v for k, v in result.items() if k not in ("features",)}, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
