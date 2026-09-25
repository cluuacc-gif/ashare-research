#!/usr/bin/env python3
"""Backtest the capital-limited Top1-3 research rule vs baselines.

Rule under test (same as auction shortlist):
  - universe: close in [5,10], non-ST, bars>=250 preferred
  - veto: news high risk / medium>=3, open_gap>4% or one-word limit open
  - score: gene + liquidity + price band + open_gap(2-4%) + mild o2c
  - pick TopK (K=1,2,3) per date
  - fill proxy: open<=cap and low<=trigger (see simulate_entry_backtest)
  - exit: E+1 stop/close else E+2 time exit; cost 10bp

Reports win rate / mean net vs (a) all no-veto signals, (b) gap2-4 only.
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


def board_limit_ratio(symbol: str) -> float:
    if symbol.endswith(".BJ"):
        return 0.30
    code = symbol.split(".")[0]
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def load_news_risks(db):
    out = {}
    for symbols_json, title in db.execute("SELECT symbols_json, title FROM news_events"):
        level = risk_level(title)
        if level == "low":
            continue
        try:
            symbols = json.loads(symbols_json or "[]")
        except Exception:
            continue
        for sym in symbols:
            item = out.setdefault(sym, {"high": 0, "medium": 0})
            item[level] += 1
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--end-date", default="2026-09-11")
    p.add_argument("--top-k", type=int, default=3)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    end = args.end_date

    db = sqlite3.connect(Path(args.db).as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        risks = load_news_risks(db)
        # st names
        st = {r[0] for r in db.execute("SELECT symbol FROM security_master WHERE UPPER(COALESCE(name,'')) LIKE '%ST%'")}
        # limit-like count approx via prior window at load time — compute rolling in pass
        rows = db.execute(
            """
            SELECT symbol, trade_date, open, high, low, close, volume, amount, prev_close
            FROM daily_quotes
            WHERE trade_date<=? AND close>0 AND open>0 AND high>0 AND low>0
            ORDER BY trade_date, symbol
            """,
            (end,),
        ).fetchall()
    finally:
        db.close()

    by_sym = defaultdict(list)
    by_date = defaultdict(list)
    for r in rows:
        by_sym[r[0]].append(r)
        by_date[r[1]].append(r)

    sessions = sorted(by_date)
    n1 = {a: b for a, b in zip(sessions, sessions[1:])}
    n2 = {sessions[i]: sessions[i + 2] for i in range(len(sessions) - 2)}

    def gene_count(sym, idx):
        # count prior 250 bars with |close/prev-1|>=0.095 and high==low (one-word) OR big range
        series = by_sym[sym]
        c = 0
        for j in range(max(0, idx - 250), idx):
            r = series[j]
            if j == 0:
                continue
            prev = series[j - 1][5]
            if not prev:
                continue
            ret = r[5] / prev - 1
            if abs(ret) >= 0.095:
                c += 1
        return c

    def plan_and_fill(sym, i):
        series = by_sym[sym]
        row = series[i]
        date = row[1]
        o, h, l, c = row[2], row[3], row[4], row[5]
        prev = series[i - 1][5] if i else row[8]
        if not prev or prev <= 0 or not (5.0 <= c <= 10.0):
            return None
        open_gap = o / prev - 1.0
        ratio = board_limit_ratio(sym)
        limit_up = round(prev * (1 + ratio), 2)
        if open_gap > 0.04:
            return {"kind": "veto", "reason": "gap", "date": date, "symbol": sym}
        if o >= limit_up - 1e-6 and h == l:
            return {"kind": "veto", "reason": "oneword", "date": date, "symbol": sym}
        risk = risks.get(sym)
        if risk and (risk.get("high") or risk.get("medium", 0) >= 3):
            return {"kind": "veto", "reason": "news", "date": date, "symbol": sym}
        if sym in st:
            return {"kind": "veto", "reason": "st", "date": date, "symbol": sym}
        prior_high20 = max(series[max(0, i - 20):i][j][3] for j in range(min(20, i))) if i else h
        prior_low20 = min(series[max(0, i - 20):i][j][4] for j in range(min(20, i))) if i else l
        tick = 0.01
        trigger = math.ceil(max(c, prior_high20) / tick) * tick
        cap = math.floor(min(trigger + 0.3 * (h - l + 1e-6), c * 1.025) / tick) * tick
        stop = math.floor(min(prior_low20 - tick, trigger - 1.6 * (h - l + 1e-6)) / tick) * tick
        if cap < trigger or stop <= 0:
            return None
        gene = gene_count(sym, i)
        amount = float(row[7] or row[6] or 0)
        price_score = 1.0 if 5.0 <= c <= 7.5 else 0.6
        gene_score = min(gene / 5.0, 1.0)
        liq_score = min(amount / 1_000_000_000.0, 1.0)
        if 0.02 <= open_gap <= 0.04:
            gap_score = 1.0
        elif 0.0 <= open_gap < 0.02:
            gap_score = 0.55
        elif -0.02 <= open_gap < 0.0:
            gap_score = 0.35
        else:
            gap_score = 0.25
        o2c = c / o - 1 if o else 0
        mom_score = 0.5 + min(max(o2c, -0.05), 0.08) * 4
        score = 0.28 * gene_score + 0.22 * liq_score + 0.15 * price_score + 0.25 * gap_score + 0.10 * mom_score
        filled = False
        if o <= cap and l <= trigger:
            fill = trigger if l <= trigger else min(cap, o)
            filled = True
        else:
            fill = None
        return {
            "kind": "signal",
            "date": date,
            "symbol": sym,
            "score": score,
            "filled": filled,
            "fill": fill,
            "stop": stop,
            "open_gap": open_gap,
            "close": c,
            "gene": gene,
            "amount": amount,
        }

    # index map for exit lookup
    index_of = {sym: {series[i][1]: i for i in range(len(series))} for sym, series in by_sym.items()}

    def settle(sig):
        if not sig or not sig.get("filled"):
            return None
        sym, date = sig["symbol"], sig["date"]
        series = by_sym[sym]
        i = index_of[sym][date]
        fill, stop = sig["fill"], sig["stop"]
        row = series[i]
        if row[4] < stop:
            ret = stop / fill - 1
            return ret - 0.001
        d1 = n1.get(date)
        d2 = n2.get(date)
        if d1 and d1 in index_of[sym]:
            r1 = series[index_of[sym][d1]]
            if r1[4] <= stop:
                ret = stop / fill - 1
            else:
                ret = r1[5] / fill - 1
            return ret - 0.001
        if d2 and d2 in index_of[sym]:
            r2 = series[index_of[sym][d2]]
            if r2[4] <= stop:
                ret = stop / fill - 1
            else:
                ret = r2[5] / fill - 1
            return ret - 0.001
        return None

    # Build daily candidate sets
    by_date_signals = defaultdict(list)
    by_date_vetoes = defaultdict(int)
    for sym, series in by_sym.items():
        for i in range(25, len(series)):
            sig = plan_and_fill(sym, i)
            if not sig:
                continue
            if sig["kind"] == "veto":
                by_date_vetoes[sig["date"]] += 1
            else:
                by_date_signals[sig["date"]].append(sig)

    def eval_take(chooser):
        """chooser(list_of_signals)->list_of_selected_signals for one date."""
        picked = []
        for date in sorted(by_date_signals):
            cands = by_date_signals[date]
            picked.extend(chooser(cands) or [])
        nets = []
        filled = 0
        for sig in picked:
            if sig.get("filled"):
                n = settle(sig)
                if n is not None:
                    nets.append(n)
                    filled += 1
        return {
            "picked_signals": len(picked),
            "fills": filled,
            "win_rate": (sum(1 for x in nets if x > 0) / len(nets)) if nets else None,
            "mean_net": statistics.mean(nets) if nets else None,
            "median_net": statistics.median(nets) if nets else None,
            "sum_net": sum(nets) if nets else None,
        }

    def topk(k):
        return lambda cands: sorted(cands, key=lambda z: (-z["score"], -(z.get("amount") or 0)))[:k]

    def all_no_veto(cands):
        return cands

    def gap2_4(cands):
        return [z for z in cands if 0.02 <= z.get("open_gap", -1) <= 0.04]

    def topk_gap24(k):
        def f(cands):
            sub = [z for z in cands if 0.02 <= z.get("open_gap", -1) <= 0.04]
            return sorted(sub, key=lambda z: (-z["score"], -(z.get("amount") or 0)))[:k]
        return f

    result = {
        "kind": "capital_limited_rule_backtest",
        "end_date": end,
        "universe_days": len(by_date_signals),
        "veto_count": sum(by_date_vetoes.values()),
        "baselines": {
            "all_no_veto": eval_take(all_no_veto),
            "gap_2_4_only": eval_take(gap2_4),
        },
        "top1": eval_take(topk(1)),
        "top2": eval_take(topk(2)),
        "top3": eval_take(topk(3)),
        "top1_gap2_4": eval_take(topk_gap24(1)),
        "top3_gap2_4": eval_take(topk_gap24(3)),
        "execution_win_rate_80_validated": False,
        "note": "Simulated fills only; research hypothesis check for capital-limited rule.",
    }
    write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
