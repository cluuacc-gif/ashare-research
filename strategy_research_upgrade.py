#!/usr/bin/env python3
"""Strategy research upgrade: walk-forward, slippage, regime, exits, sizing.

Rule family (from earlier search):
  entry filters: gap 2-4%, o2c>=+5%, close<=8, skip Friday, non-ST, no high/med news
  fill: conservative daily proxy + optional slippage ticks
Exits compared: TP-touch, TP-close, E+1 close, trail stop, time exit E+2.
Regimes: 20-day index (equal-weight proxy) return.
Does not claim 80% unless walk-forward + pessimistic fill supports it.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from short_term import write_json  # noqa: E402
from news_risk import risk_level  # noqa: E402


def ratio_of(sym: str) -> float:
    if sym.endswith(".BJ"):
        return 0.30
    code = sym.split(".")[0]
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    p.add_argument("--end-date", default="2026-09-11")
    p.add_argument("--slippage-ticks", type=float, default=1.0)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    end = args.end_date
    slip = 0.01 * max(0.0, args.slippage_ticks)

    db = sqlite3.connect(Path(args.db).as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        risks = {}
        for sj, title in db.execute("SELECT symbols_json, title FROM news_events"):
            lv = risk_level(title)
            if lv == "low":
                continue
            try:
                syms = json.loads(sj or "[]")
            except Exception:
                continue
            for s in syms:
                it = risks.setdefault(s, {"high": 0, "medium": 0})
                it[lv] += 1
        st = {
            r[0]
            for r in db.execute(
                "SELECT symbol FROM security_master WHERE UPPER(COALESCE(name,'')) LIKE '%ST%'"
            )
        }
        rows = db.execute(
            """
            SELECT symbol, trade_date, open, high, low, close, volume, amount, prev_close
            FROM daily_quotes WHERE trade_date<=? AND close>0 AND open>0 AND high>0 AND low>0
            ORDER BY symbol, trade_date
            """,
            (end,),
        ).fetchall()
    finally:
        db.close()

    by_sym = defaultdict(list)
    by_date_close = defaultdict(list)
    for r in rows:
        by_sym[r[0]].append(r)
        by_date_close[r[1]].append(r[5])
    sessions = sorted(by_date_close)
    n1 = {a: b for a, b in zip(sessions, sessions[1:])}
    n2 = {sessions[i]: sessions[i + 2] for i in range(len(sessions) - 2)}
    # market regime: mean close ret over session
    mkt = {}
    prev_m = None
    for d in sessions:
        avg = sum(by_date_close[d]) / len(by_date_close[d])
        if prev_m:
            mkt[d] = avg / prev_m - 1
        prev_m = avg
    # rolling 20d market return
    mkt_hist = []
    regime = {}
    for d in sessions:
        if d in mkt:
            mkt_hist.append(mkt[d])
        if len(mkt_hist) >= 20:
            r20 = 1.0
            for x in mkt_hist[-20:]:
                r20 *= 1 + x
            r20 -= 1
            regime[d] = "up" if r20 > 0.02 else ("down" if r20 < -0.02 else "flat")
        else:
            regime[d] = "unknown"

    recs = []
    for sym, series in by_sym.items():
        idx = {series[i][1]: i for i in range(len(series))}
        for i in range(30, len(series) - 1):
            row = series[i]
            date = row[1]
            o, h, l, c, prev = row[2], row[3], row[4], row[5], (row[8] or series[i - 1][5])
            if not prev or prev <= 0 or not (5.0 <= c <= 10.0):
                continue
            gap = o / prev - 1.0
            if not (0.02 <= gap <= 0.04):
                continue
            o2c = c / o - 1 if o else 0
            if o2c < 0.05 or c > 8.0:
                continue
            if sym in st:
                continue
            risk = risks.get(sym)
            if risk and (risk.get("high") or risk.get("medium", 0) >= 3):
                continue
            import datetime as dt

            if dt.date.fromisoformat(date).weekday() == 4:
                continue
            prior_high20 = max(series[i - 20 : i][k][3] for k in range(20)) if i >= 20 else h
            prior_low20 = min(series[i - 20 : i][k][4] for k in range(20)) if i >= 20 else l
            tick = 0.01
            trigger = math.ceil(max(c, prior_high20) / tick) * tick
            # slippage: buy at trigger+slip if still <= high
            fill = trigger + slip
            if fill > h:
                continue
            if not (o <= fill <= h and c >= trigger):
                continue
            stop = math.floor(min(prior_low20 - tick, trigger - 1.6 * (h - l + 1e-6)) / tick) * tick
            if stop <= 0:
                continue
            d1, d2 = n1.get(date), n2.get(date)
            r1 = series[idx[d1]] if d1 and d1 in idx else None
            r2 = series[idx[d2]] if d2 and d2 in idx else None

            def net(px):
                return px / fill - 1 - 0.001

            tp = fill * 1.02
            # exits
            if l < stop and h < tp:
                e_touch = e_close = e_e1 = net(stop)
            elif r1 is None:
                e_touch = e_close = e_e1 = None
            else:
                if r1[3] >= tp:
                    e_touch = 0.02 - slip / fill - 0.001
                elif r1[4] <= stop:
                    e_touch = net(stop)
                else:
                    e_touch = net(r1[5])
                if r1[5] >= tp:
                    e_close = 0.02 - slip / fill - 0.001
                elif r1[4] <= stop:
                    e_close = net(stop)
                else:
                    e_close = net(r1[5])
                e_e1 = net(stop if r1[4] <= stop else r1[5])
            # trail: exit at max(stop, entry*0.99) on e1 close if e1 low broke entry
            if r1 is not None:
                if r1[4] < fill * 0.99:
                    e_trail = net(max(stop, fill * 0.99))
                elif r1[3] >= tp:
                    e_trail = 0.02 - slip / fill - 0.001
                else:
                    e_trail = net(r1[5])
            else:
                e_trail = None
            # e2 time
            if r1 is not None and r1[4] <= stop:
                e_e2 = net(stop)
            elif r2 is not None:
                e_e2 = net(stop if r2[4] <= stop else r2[5])
            else:
                e_e2 = e_e1

            recs.append(
                {
                    "date": date,
                    "symbol": sym,
                    "regime": regime.get(date, "unknown"),
                    "gap": gap,
                    "e_touch": e_touch,
                    "e_close": e_close,
                    "e_e1": e_e1,
                    "e_trail": e_trail,
                    "e_e2": e_e2,
                }
            )

    def stats(sub, key):
        nets = [r[key] for r in sub if r.get(key) is not None]
        if len(nets) < 20:
            return {"n": len(nets), "win_rate": None, "mean_net": None}
        return {
            "n": len(nets),
            "win_rate": sum(1 for x in nets if x > 0) / len(nets),
            "mean_net": statistics.mean(nets),
            "median_net": statistics.median(nets),
            "sum_net": sum(nets),
            "best": max(nets),
            "worst": min(nets),
        }

    # walk-forward: 4 blocks by date quartile
    dates = sorted({r["date"] for r in recs})
    if not dates:
        raise SystemExit("no samples")
    cuts = [dates[0], dates[len(dates) // 4], dates[len(dates) // 2], dates[3 * len(dates) // 4], dates[-1]]
    folds = []
    for a, b in zip(cuts, cuts[1:]):
        sub = [r for r in recs if a <= r["date"] <= b]
        folds.append({"from": a, "to": b, **stats(sub, "e_close")})

    by_regime = {}
    for reg in ("up", "flat", "down", "unknown"):
        sub = [r for r in recs if r["regime"] == reg]
        by_regime[reg] = {k: stats(sub, k) for k in ("e_touch", "e_close", "e_trail", "e_e2")}

    result = {
        "kind": "strategy_research_upgrade",
        "slippage_ticks": args.slippage_ticks,
        "n_all": len(recs),
        "exits_overall": {
            k: stats(recs, k) for k in ("e_touch", "e_close", "e_e1", "e_trail", "e_e2")
        },
        "walk_forward_e_close": folds,
        "by_regime": by_regime,
        "position_sizing_suggestion": {
            "rule": "fixed fraction 1-2% risk per trade; max 1-3 names; skip if no signal",
            "kelly_note": "do not use full Kelly until forward fills exist; half-Kelly max if ever",
        },
        "claim_80_allowed": False,
        "note": "Upgrade for research robustness. 80% not claimed under slippage+pessimistic exits.",
    }
    write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
