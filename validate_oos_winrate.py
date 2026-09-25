#!/usr/bin/env python3
"""Out-of-sample check + pessimistic take-profit model for the high-win-rate rule.

Rule (from search_high_winrate_deep):
  entry: open gap 2-4%, o2c>=+5%, close<=8, skip Friday, non-ST, no high/med news
  fill:  open<=trigger<=high and close>=trigger
  exits: (a) TP-touch: if high>=entry*1.02 take +2%
         (b) TP-close: if close>=entry*1.02 take +2%  [pessimistic]
         (c) E+1 close only
Splits: in-sample <= split_date, out-of-sample after.
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
    p.add_argument("--split-date", default="2026-04-15")
    p.add_argument("--end-date", default="2026-09-11")
    p.add_argument("--output", required=True)
    args = p.parse_args()
    split, end = args.split_date, args.end_date

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
    for r in rows:
        by_sym[r[0]].append(r)
    sessions = sorted({r[1] for r in rows})
    n1 = {a: b for a, b in zip(sessions, sessions[1:])}

    recs = []
    for sym, series in by_sym.items():
        idx = {series[i][1]: i for i in range(len(series))}
        for i in range(30, len(series) - 1):
            row = series[i]
            date = row[1]
            if date < "2024-01-01":
                continue
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
            stop = math.floor(min(prior_low20 - tick, trigger - 1.6 * (h - l + 1e-6)) / tick) * tick
            if stop <= 0:
                continue
            if not ((l <= trigger <= h) and (o <= trigger) and (c >= trigger)):
                continue
            fill = trigger
            d1 = n1.get(date)
            r1 = series[idx[d1]] if d1 and d1 in idx else None

            def net(px):
                return px / fill - 1 - 0.001

            # (a) TP touch
            if l < stop and h < fill * 1.02:
                a = net(stop)
            elif r1 is None:
                a = None
            elif r1[4] <= stop and r1[3] < fill * 1.02:
                a = net(stop)
            elif r1[3] >= fill * 1.02:
                a = 0.02 - 0.001
            else:
                a = net(r1[5])
            # (b) pessimistic TP close
            if l < stop and c < fill * 1.02:
                b = net(stop)
            elif r1 is None:
                b = None
            elif r1[4] <= stop and r1[5] < fill * 1.02:
                b = net(stop)
            elif r1[5] >= fill * 1.02:
                b = 0.02 - 0.001
            else:
                b = net(r1[5])
            # (c) close only
            if r1 is None:
                e = None
            else:
                e = net(stop if r1[4] <= stop else r1[5])

            recs.append({"date": date, "symbol": sym, "a": a, "b": b, "e": e})

    def block(sub, key):
        nets = [r[key] for r in sub if r.get(key) is not None]
        if not nets:
            return {"n": 0, "win_rate": None, "mean_net": None, "sum_net": None}
        return {
            "n": len(nets),
            "win_rate": sum(1 for x in nets if x > 0) / len(nets),
            "mean_net": statistics.mean(nets),
            "median_net": statistics.median(nets),
            "sum_net": sum(nets),
        }

    def split_stats(sub):
        return {
            "tp_touch": block(sub, "a"),
            "tp_close_pessimistic": block(sub, "b"),
            "e1_close_only": block(sub, "e"),
        }

    ins = [r for r in recs if r["date"] <= split]
    oos = [r for r in recs if r["date"] > split]
    result = {
        "kind": "oos_and_pessimistic_tp_validation",
        "split_date": split,
        "end_date": end,
        "rule": "gap2-4%, o2c>=5%, close<=8, skip Fri, non-ST, no high/med news, conservative fill",
        "in_sample": split_stats(ins),
        "out_of_sample": split_stats(oos),
        "all": split_stats(recs),
        "claim_80_oos_tp_touch": (oos and block(oos, "a")["win_rate"] or 0) >= 0.80,
        "claim_80_oos_tp_close": (oos and block(oos, "b")["win_rate"] or 0) >= 0.80,
        "note": "80% may only be claimed if out-of-sample also reaches it under stated exit model.",
    }
    write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
