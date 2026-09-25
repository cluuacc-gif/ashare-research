#!/usr/bin/env python3
"""Simulate limit-order entries on real daily bars and score E+1/E+2 outcomes.

Research-only. Uses open/high/low/close as a conservative fill proxy:
- buy fills if day's low <= planned limit price (limit order can rest)
- skip if open already above buy cap (chase / too strong open)
- skip if high==low==limit_up-like (one-word board) before entry window — approximate
- E+1/E+2 exits use next sessions' close / planned stop / time exit

Never claims broker fills. Never writes prediction DB.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from short_term import write_json  # noqa: E402


def board_limit_ratio(symbol: str) -> float:
    if symbol.endswith(".BJ"):
        return 0.30
    code = symbol.split(".")[0]
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def fetch_panel(db, end_date: str, min_bars: int = 60):
    rows = db.execute(
        """
        SELECT symbol, trade_date, open, high, low, close, volume, amount, prev_close
        FROM daily_quotes
        WHERE close BETWEEN 4.5 AND 10.5 AND open>0 AND high>0 AND low>0 AND close>0
        ORDER BY symbol, trade_date
        """
    ).fetchall()
    by_sym = defaultdict(list)
    for r in rows:
        by_sym[r[0]].append(r)
    sessions = sorted({r[1] for r in rows if r[1] <= end_date})
    next_session = {}
    for a, b in zip(sessions, sessions[1:]):
        next_session[a] = b
    # second next
    next2 = {}
    for i in range(len(sessions) - 2):
        next2[sessions[i]] = sessions[i + 2]
    return by_sym, sessions, next_session, next2


def simulate_symbol(series, next_session, next2):
    """series is list of sqlite rows for one symbol, ordered by date."""
    out = []
    for i in range(25, len(series) - 2):
        row = series[i]
        date = row[1]
        o, h, l, c = row[2], row[3], row[4], row[5]
        prev = series[i - 1][5] or row[8]
        if not prev or prev <= 0:
            continue
        if not (5.0 <= c <= 10.0):
            continue
        # research plan approx: trigger near max(close, prior20 high)+tick, cap ~ close*1.025
        prior_high20 = max(series[i - 20 : i][j][3] for j in range(20)) if i >= 20 else h
        prior_low20 = min(series[i - 20 : i][j][4] for j in range(20)) if i >= 20 else l
        tick = 0.01
        trigger = math.ceil(max(c, prior_high20) / tick) * tick
        cap = math.floor(min(trigger + 0.15 * (h - l + 1e-6) * 2, c * 1.025) / tick) * tick
        stop = math.floor(min(prior_low20 - tick, trigger - 0.8 * (h - l + 1e-6) * 2) / tick) * tick
        if cap < trigger or stop <= 0:
            continue
        open_gap = o / prev - 1.0
        # skip chase / one-word open at limit
        ratio = board_limit_ratio(row[0])
        limit_up = round(prev * (1 + ratio), 2)
        if open_gap > 0.04:
            out.append({"date": date, "symbol": row[0], "filled": False, "reason": "open_gap_gt_4pct", "open_gap": open_gap})
            continue
        if o >= limit_up - 1e-6 and h == l:
            out.append({"date": date, "symbol": row[0], "filled": False, "reason": "one_word_limit_open", "open_gap": open_gap})
            continue
        if h < trigger:
            out.append({"date": date, "symbol": row[0], "filled": False, "reason": "trigger_not_touched", "open_gap": open_gap})
            continue
        # fill at min(trigger, cap) if low <= that price after open consideration
        fill_px = trigger if l <= trigger else cap
        if l > cap:
            out.append({"date": date, "symbol": row[0], "filled": False, "reason": "never_below_cap", "open_gap": open_gap})
            continue
        if o > cap:
            out.append({"date": date, "symbol": row[0], "filled": False, "reason": "open_above_cap", "open_gap": open_gap})
            continue
        # filled this day (research proxy)
        d1 = next_session.get(date)
        d2 = next2.get(date)
        # find future bars
        future = series[i + 1 :]
        bar1 = next((x for x in future if x[1] == d1), None)
        bar2 = next((x for x in future if x[1] == d2), None)
        exit_px = None
        exit_day = None
        reason_exit = "hold"
        # stop check on day of entry after fill (same-day low)
        if l < stop:
            exit_px = stop
            exit_day = date
            reason_exit = "same_day_stop"
        elif bar1 is not None:
            if bar1[4] <= stop:  # low <= stop
                exit_px = stop
                exit_day = bar1[1]
                reason_exit = "e1_stop"
            else:
                exit_px = bar1[5]
                exit_day = bar1[1]
                reason_exit = "e1_close"
        elif bar2 is not None:
            # E+2 time exit approx at close
            if bar2[4] <= stop:
                exit_px = stop
                exit_day = bar2[1]
                reason_exit = "e2_stop"
            else:
                exit_px = bar2[5]
                exit_day = bar2[1]
                reason_exit = "e2_time_exit"
        else:
            out.append({"date": date, "symbol": row[0], "filled": True, "reason": "insufficient_exit_calendar", "fill": fill_px, "open_gap": open_gap})
            continue
        ret = (exit_px / fill_px - 1.0) if exit_px and fill_px else None
        # simple cost: commission+tax ~ 0.001 round trip research constant
        net = (ret - 0.001) if ret is not None else None
        out.append(
            {
                "date": date,
                "symbol": row[0],
                "filled": True,
                "reason": "filled",
                "fill": fill_px,
                "exit": exit_px,
                "exit_day": exit_day,
                "exit_reason": reason_exit,
                "return": ret,
                "net_return": net,
                "win": (net is not None and net > 0),
                "open_gap": open_gap,
                "close": c,
            }
        )
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--end-date", default="2026-09-11")
    p.add_argument("--output", required=True)
    args = p.parse_args()
    db = sqlite3.connect(Path(args.db).as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        by_sym, sessions, n1, n2 = fetch_panel(db, args.end_date)
    finally:
        db.close()
    all_rows = []
    for sym, series in by_sym.items():
        all_rows.extend(simulate_symbol(series, n1, n2))
    filled = [r for r in all_rows if r.get("filled")]
    skipped = [r for r in all_rows if not r.get("filled")]
    wins = [r for r in filled if r.get("win")]
    nets = [r["net_return"] for r in filled if r.get("net_return") is not None]
    by_date = defaultdict(list)
    for r in filled:
        if r.get("net_return") is not None:
            by_date[r["date"]].append(r["net_return"])
    # open_gap buckets
    buckets = {}
    for lo, hi, name in ((-1, 0.00, "flat_down"), (0.0, 0.02, "gap_0_2"), (0.02, 0.04, "gap_2_4")):
        sub = [r for r in filled if r.get("net_return") is not None and lo <= (r.get("open_gap") or 0) < hi]
        if sub:
            buckets[name] = {
                "n": len(sub),
                "win_rate": sum(1 for r in sub if r.get("win")) / len(sub),
                "mean_net": statistics.mean(r["net_return"] for r in sub),
            }
    result = {
        "kind": "simulated_limit_entry_backtest",
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "end_date": args.end_date,
        "fill_proxy": "daily low<=trigger, skip open_gap>4% or one-word limit open",
        "costs_model": "flat 10bp round-trip research constant",
        "signals_considered": len(all_rows),
        "fills": len(filled),
        "skipped": len(skipped),
        "skip_reasons": {
            k: sum(1 for r in skipped if r.get("reason") == k)
            for k in sorted({r.get("reason") for r in skipped})
        },
        "wins": len(wins),
        "win_rate_gross": (len(wins) / len(filled)) if filled else None,
        "mean_net_return": statistics.mean(nets) if nets else None,
        "median_net_return": statistics.median(nets) if nets else None,
        "independent_dates": len(by_date),
        "open_gap_buckets": buckets,
        "execution_win_rate_80_validated": False,
        "note": "Research simulation only. Not broker fills. 80% target not met/claimed.",
        "sample_filled": filled[:20],
    }
    write_json(args.output, result)
    print(json.dumps({k: result[k] for k in result if k != "sample_filled"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
