#!/usr/bin/env python3
"""Enhanced signal features: auction volume ratio, theme heat, market breadth."""
from __future__ import annotations

import json
from collections import defaultdict


def market_breadth(db, as_of: str) -> dict:
    """Limit-like counts + up ratio -> regime gate refinement."""
    rows = db.execute(
        """
        SELECT close, prev_close, high, low
        FROM daily_quotes
        WHERE trade_date=? AND close>0 AND prev_close>0
        """,
        (as_of,),
    ).fetchall()
    if not rows:
        return {"ok": False, "up_ratio": None, "strong_ratio": None, "regime": "unknown"}
    ups = 0
    strong = 0
    for c, prev, h, l in rows:
        ret = c / prev - 1
        if ret > 0:
            ups += 1
        if abs(ret) >= 0.095:
            strong += 1
    n = len(rows)
    up_ratio = ups / n
    strong_ratio = strong / n
    # crude: weak if fewer than 40% up
    regime = "up" if up_ratio >= 0.55 else ("down" if up_ratio < 0.40 else "flat")
    return {
        "ok": up_ratio >= 0.40,
        "up_ratio": round(up_ratio, 4),
        "strong_ratio": round(strong_ratio, 4),
        "n": n,
        "regime": regime,
        "rule": "skip if up_ratio<0.40 or combined with 5d down",
    }


def auction_volume_ratio(auction_row: dict | None, db, symbol: str, as_of: str, lookback: int = 5) -> dict:
    """auction volume vs recent avg volume (proxy: lots field from qt)."""
    if not auction_row:
        return {"ratio": None, "note": "no_auction_row"}
    vol = auction_row.get("volume_lots")
    if vol is None:
        return {"ratio": None, "note": "no_auction_volume"}
    avg = db.execute(
        """
        SELECT AVG(volume) FROM (
          SELECT volume FROM daily_quotes
          WHERE symbol=? AND trade_date<? AND volume IS NOT NULL
          ORDER BY trade_date DESC LIMIT ?
        )
        """,
        (symbol, as_of, lookback),
    ).fetchone()[0]
    if not avg or avg <= 0:
        return {"ratio": None, "note": "no_avg_volume", "auction_volume": vol}
    # qt volume_lots * 100 shares approx vs volume in shares
    auction_shares = float(vol) * 100.0
    return {
        "ratio": round(auction_shares / float(avg), 3),
        "auction_volume": vol,
        "avg_volume": avg,
        "note": "auction_lots*100 / 5d avg volume",
    }


def theme_heat(db, symbol: str, window_days: int = 30) -> dict:
    """News keyword co-occurrence heat for symbol's industry peers."""
    ind = db.execute(
        "SELECT industry FROM industry_members WHERE symbol=? ORDER BY observed_at DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    industry = ind[0] if ind else None
    # count recent news for same industry
    if not industry:
        return {"industry": None, "news_count": 0, "hot": False}
    peers = [
        r[0]
        for r in db.execute(
            "SELECT symbol FROM industry_members WHERE industry=? LIMIT 200", (industry,)
        )
    ]
    if not peers:
        return {"industry": industry, "news_count": 0, "hot": False}
    marks = ",".join("?" * min(len(peers), 50))
    n = db.execute(
        f"SELECT COUNT(*) FROM news_events WHERE symbols_json LIKE ?",
        (f"%{peers[0]}%",),
    ).fetchone()[0]
    # cheaper: count news whose title mentions industry or any peer code
    total = 0
    for p in peers[:50]:
        total += db.execute(
            "SELECT COUNT(*) FROM news_events WHERE symbols_json LIKE ?",
            (f"%{p}%",),
        ).fetchone()[0]
    return {
        "industry": industry,
        "peer_news_count": total,
        "hot": total >= 5,
        "note": "peer announcements last window (no date filter on payload)",
    }


def composite_quality(
    db,
    symbol: str,
    as_of: str,
    open_gap: float | None,
    o2c: float | None,
    auction_row: dict | None = None,
) -> dict:
    from signal_quality import score_symbol

    base = score_symbol(db, symbol, as_of, open_gap, o2c)
    breadth = market_breadth(db, as_of)
    volr = auction_volume_ratio(auction_row, db, symbol, as_of)
    theme = theme_heat(db, symbol)
    veto = list(base.get("veto") or [])
    if not breadth.get("ok"):
        veto.append(f"breadth_{breadth.get('regime')}")
    if volr.get("ratio") is not None and volr["ratio"] < 0.3:
        veto.append("auction_vol_thin")
    if volr.get("ratio") is not None and volr["ratio"] > 8:
        veto.append("auction_vol_explode")
    score = float(base.get("quality_score") or 0)
    if theme.get("hot"):
        score = min(1.0, score + 0.08)
    if volr.get("ratio") and 0.8 <= volr["ratio"] <= 4.0:
        score = min(1.0, score + 0.10)
    return {
        **base,
        "breadth": breadth,
        "auction_volume": volr,
        "theme": theme,
        "quality_score": round(score, 4),
        "veto": veto,
    }
