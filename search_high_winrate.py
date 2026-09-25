#!/usr/bin/env python3
"""Search honest entry filters for higher simulated win rate.

Must not claim 80% unless a pre-declared rule actually achieves it on
real bars with conservative fill/exit and cost. Reports best achieved rates.
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
    p.add_argument("--output", required=True)
    args = p.parse_args()
    end = args.end_date
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
            FROM daily_quotes
            WHERE trade_date<=? AND close>0 AND open>0 AND high>0 AND low>0
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
    n2 = {sessions[i]: sessions[i + 2] for i in range(len(sessions) - 2)}

    samples = []  # one dict per potential entry
    for sym, series in by_sym.items():
        idx = {series[i][1]: i for i in range(len(series))}
        for i in range(25, len(series) - 2):
            row = series[i]
            date = row[1]
            o, h, l, c = row[2], row[3], row[4], row[5]
            prev = series[i - 1][5] or row[8]
            if not prev or prev <= 0 or not (5.0 <= c <= 10.0):
                continue
            gap = o / prev - 1.0
            if not (0.02 <= gap <= 0.04):
                continue
            if sym in st:
                continue
            risk = risks.get(sym)
            if risk and (risk.get("high") or risk.get("medium", 0) >= 3):
                continue
            ratio = ratio_of(sym)
            limit_up = round(prev * (1 + ratio), 2)
            if o >= limit_up - 1e-6 and h == l:
                continue
            # gene in last 250
            gene = 0
            for j in range(max(1, i - 250), i):
                pr = series[j - 1][5]
                if pr and abs(series[j][5] / pr - 1) >= 0.095:
                    gene += 1
            amount = float(row[7] or row[6] or 0)
            o2c = c / o - 1 if o else 0.0
            prior_high20 = max(series[i - 20 : i][k][3] for k in range(20)) if i >= 20 else h
            prior_low20 = min(series[i - 20 : i][k][4] for k in range(20)) if i >= 20 else l
            tick = 0.01
            trigger = math.ceil(max(c, prior_high20) / tick) * tick
            # conservative: fill only if low <= trigger AND close >= trigger (closed at/above trigger)
            # also open <= trigger (not opened above)
            cons_fill = (l <= trigger <= h) and (o <= trigger) and (c >= trigger)
            loose_fill = (o <= min(trigger, c * 1.025)) and (l <= trigger)
            stop = math.floor(min(prior_low20 - tick, trigger - 1.6 * (h - l + 1e-6)) / tick) * tick
            if stop <= 0 or trigger <= 0:
                continue

            def settle(fill_px, mode):
                if fill_px is None:
                    return None
                if mode == "e1_close_only":
                    d1 = n1.get(date)
                    if not d1 or d1 not in idx:
                        return None
                    r1 = series[idx[d1]]
                    return r1[5] / fill_px - 1 - 0.001
                if l < stop and mode != "e1_close_only":
                    return stop / fill_px - 1 - 0.001
                d1 = n1.get(date)
                d2 = n2.get(date)
                if d1 and d1 in idx:
                    r1 = series[idx[d1]]
                    px = stop if r1[4] <= stop else r1[5]
                    return px / fill_px - 1 - 0.001
                if d2 and d2 in idx:
                    r2 = series[idx[d2]]
                    px = stop if r2[4] <= stop else r2[5]
                    return px / fill_px - 1 - 0.001
                return None

            samples.append(
                {
                    "date": date,
                    "symbol": sym,
                    "gap": gap,
                    "gene": gene,
                    "amount": amount,
                    "o2c": o2c,
                    "close": c,
                    "weekday": __import__("datetime").date.fromisoformat(date).weekday(),
                    "cons_fill": cons_fill,
                    "loose_fill": loose_fill,
                    "trigger": trigger,
                    "ret_cons": settle(trigger if cons_fill else None, "e1"),
                    "ret_cons_e1": settle(trigger if cons_fill else None, "e1_close_only"),
                    "ret_loose": settle(trigger if loose_fill else None, "e1"),
                }
            )

    def stats(subset, key="ret_cons"):
        nets = [s[key] for s in subset if s.get(key) is not None]
        if not nets:
            return {"n": 0, "win_rate": None, "mean_net": None}
        return {
            "n": len(nets),
            "win_rate": sum(1 for x in nets if x > 0) / len(nets),
            "mean_net": statistics.mean(nets),
            "median_net": statistics.median(nets),
            "sum_net": sum(nets),
        }

    grid = []
    for gene_min in (0, 2, 5, 8):
        for amt_min in (0, 5e8, 1e9, 2e9):
            for o2c_min in (-0.05, 0.0, 0.02):
                for close_max in (7.5, 9.0, 10.0):
                    for wd in ("all", "not_fri"):
                        sub = []
                        for s in samples:
                            if s["gene"] < gene_min:
                                continue
                            if s["amount"] < amt_min:
                                continue
                            if s["o2c"] < o2c_min:
                                continue
                            if s["close"] > close_max:
                                continue
                            if wd == "not_fri" and s["weekday"] == 4:
                                continue
                            sub.append(s)
                        stt = stats(sub, "ret_cons")
                        if stt["n"] < 30:
                            continue
                        grid.append(
                            {
                                "gene_min": gene_min,
                                "amt_min": amt_min,
                                "o2c_min": o2c_min,
                                "close_max": close_max,
                                "weekday": wd,
                                **stt,
                            }
                        )
    grid.sort(key=lambda z: (-(z["win_rate"] or 0), -(z["mean_net"] or -9)))
    best = grid[:15]
    base = stats(samples, "ret_cons")
    base_loose = stats(samples, "ret_loose")
    base_e1 = stats(samples, "ret_cons_e1")
    above80 = [g for g in grid if (g["win_rate"] or 0) >= 0.80 and g["n"] >= 100]
    result = {
        "kind": "winrate_filter_search",
        "end_date": end,
        "gap_universe_n": len(samples),
        "fill_modes": {
            "conservative_trigger_touch_and_close_above": base,
            "conservative_exit_e1_close_only": base_e1,
            "loose_fill": base_loose,
        },
        "best_filters": best,
        "filters_with_winrate_ge_80_n_ge_100": above80,
        "max_win_rate_observed": best[0]["win_rate"] if best else None,
        "max_win_rate_rule": best[0] if best else None,
        "execution_win_rate_80_validated": bool(above80),
        "claim_80_allowed": bool(above80),
        "note": (
            "If no grid cell reaches 80% with n>=100, 80% must not be claimed. "
            "Fill uses trigger touch; exit E+1 stop/close else E+2; cost 10bp."
        ),
    }
    write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
