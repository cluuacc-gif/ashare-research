#!/usr/bin/env python3
"""Signal-quality features for research shortlists.

Adds: market regime gate, industry heat, limit-height risk, open-momentum.
Pure computation from existing DB fields.
"""
from __future__ import annotations

import json
from collections import defaultdict


def market_regime(db, as_of: str, lookback: int = 5) -> dict:
    """Equal-weight market 5-day return proxy -> gate for trading."""
    dates = [
        r[0]
        for r in db.execute(
            "SELECT DISTINCT trade_date FROM daily_quotes WHERE trade_date<=? ORDER BY trade_date DESC LIMIT ?",
            (as_of, lookback + 1),
        )
    ]
    dates = list(reversed(dates))
    if len(dates) < lookback + 1:
        return {"ok": False, "reason": "insufficient_sessions", "ret5": None, "regime": "unknown"}
    closes = {}
    for d in dates:
        row = db.execute(
            "SELECT AVG(close) FROM daily_quotes WHERE trade_date=? AND close>0", (d,)
        ).fetchone()
        closes[d] = row[0] if row else None
    first, last = closes[dates[0]], closes[dates[-1]]
    if not first or not last:
        return {"ok": False, "reason": "thin_bars", "ret5": None, "regime": "unknown"}
    ret5 = last / first - 1.0
    if ret5 > 0.005:
        regime = "up"
    elif ret5 < -0.01:
        regime = "down"
    else:
        regime = "flat"
    # research gate: avoid new entries when market clearly weak
    ok = regime != "down"
    return {
        "ok": ok,
        "ret5": round(ret5, 5),
        "regime": regime,
        "rule": "skip new entries if market 5d ret < -1%",
    }


def industry_heat(db, as_of: str, top_n: int = 8) -> dict:
    """Average same-day o2c by industry membership (Sina)."""
    rows = db.execute(
        """
        SELECT q.symbol, q.open, q.close, m.industry
        FROM daily_quotes q
        JOIN industry_members m ON m.symbol = q.symbol
        WHERE q.trade_date=? AND q.open>0 AND q.close>0
        """,
        (as_of,),
    ).fetchall()
    acc = defaultdict(list)
    for sym, o, c, ind in rows:
        if not ind:
            continue
        acc[ind].append(c / o - 1.0)
    heats = []
    for ind, vals in acc.items():
        if len(vals) < 3:
            continue
        heats.append({"industry": ind, "n": len(vals), "mean_o2c": sum(vals) / len(vals)})
    heats.sort(key=lambda z: -z["mean_o2c"])
    return {"top": heats[:top_n], "bottom": heats[-top_n:] if len(heats) > top_n else []}


def limit_height_risk(db, symbol: str, as_of: str, window: int = 5) -> dict:
    """Count recent near-limit days. Too high = chase/height risk."""
    rows = db.execute(
        """
        SELECT trade_date, close, prev_close, high, low
        FROM daily_quotes
        WHERE symbol=? AND trade_date<=? AND prev_close>0
        ORDER BY trade_date DESC LIMIT ?
        """,
        (symbol, as_of, window),
    ).fetchall()
    strong = 0
    for r in rows:
        ret = r[1] / r[2] - 1.0 if r[2] else 0
        if abs(ret) >= 0.095:
            strong += 1
    return {
        "recent_strong_days": strong,
        "height_risk": strong >= 3,
        "note": "3+ near-limit days in 5 sessions -> veto chase",
    }


def open_momentum(open_gap: float | None, o2c: float | None) -> dict:
    """Entry-window quality from day open proxy (no 9:25 feed yet)."""
    gap = open_gap
    if gap is None:
        return {"score": 0.0, "reason": "no_gap"}
    if gap > 0.04:
        return {"score": 0.0, "reason": "gap_too_high"}
    if 0.02 <= gap <= 0.04 and (o2c or 0) >= 0.05:
        return {"score": 1.0, "reason": "gap_2_4_and_o2c_ge_5"}
    if 0.02 <= gap <= 0.04:
        return {"score": 0.6, "reason": "gap_2_4"}
    return {"score": 0.2, "reason": "gap_outside_2_4"}


def score_symbol(db, symbol: str, as_of: str, open_gap: float | None, o2c: float | None) -> dict:
    mkt = market_regime(db, as_of)
    heat = industry_heat(db, as_of)
    ind_row = db.execute(
        "SELECT industry FROM industry_members WHERE symbol=? ORDER BY observed_at DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    industry = ind_row[0] if ind_row else None
    top_inds = {x["industry"] for x in heat.get("top") or []}
    height = limit_height_risk(db, symbol, as_of)
    mom = open_momentum(open_gap, o2c)
    veto = []
    if not mkt.get("ok"):
        veto.append(f"market_{mkt.get('regime')}")
    if height.get("height_risk"):
        veto.append(f"height_risk_{height.get('recent_strong_days')}")
    if mom.get("score", 0) < 0.6:
        veto.append(f"mom_{mom.get('reason')}")
    industry_bonus = 0.15 if industry in top_inds else 0.0
    quality = round(
        0.45 * float(mom.get("score") or 0)
        + 0.25 * (1.0 if mkt.get("ok") else 0.0)
        + 0.15 * (0.0 if height.get("height_risk") else 1.0)
        + industry_bonus
        + 0.05,
        4,
    )
    return {
        "market": mkt,
        "industry": industry,
        "industry_hot": industry in top_inds,
        "height": height,
        "open_momentum": mom,
        "quality_score": quality,
        "veto": veto,
    }
