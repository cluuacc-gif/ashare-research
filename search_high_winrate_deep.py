#!/usr/bin/env python3
"""Aggressive but honest search for high win-rate entry/exit rules.

Conservative fill: open<=P<=high and close>=P (P=entry limit).
Exits tested: E+1 close, E+1 stop, E+2 time, trail to breakeven after +2%, etc.
80% only claimable if a rule reaches it with n>=100 on real bars.
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
    n2 = {sessions[i]: sessions[i + 2] for i in range(len(sessions) - 2)}
    n3 = {sessions[i]: sessions[i + 3] for i in range(len(sessions) - 3)}

    samples = []
    for sym, series in by_sym.items():
        idx = {series[i][1]: i for i in range(len(series))}
        for i in range(30, len(series) - 3):
            row = series[i]
            date = row[1]
            o, h, l, c, v, amt, prev = row[2], row[3], row[4], row[5], row[6], row[7], (row[8] or series[i - 1][5])
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
            # features
            gene = 0
            up_days = 0
            for j in range(max(1, i - 250), i):
                pr = series[j - 1][5]
                if not pr:
                    continue
                ret = series[j][5] / pr - 1
                if abs(ret) >= 0.095:
                    gene += 1
                if ret > 0:
                    up_days += 1
            ret5 = c / series[i - 5][5] - 1 if i >= 5 and series[i - 5][5] else None
            ret20 = c / series[i - 20][5] - 1 if i >= 20 and series[i - 20][5] else None
            amount = float(amt or v or 0)
            o2c = c / o - 1 if o else 0
            vol_ratio = (v / (sum(series[i - 5 : i][k][6] or 0 for k in range(min(5, i))) / 5)) if i >= 5 else None
            prior_high20 = max(series[i - 20 : i][k][3] for k in range(20)) if i >= 20 else h
            prior_low20 = min(series[i - 20 : i][k][4] for k in range(20)) if i >= 20 else l
            tick = 0.01
            trigger = math.ceil(max(c, prior_high20) / tick) * tick
            # tighter trigger: just prior high + tick (not max with close if close higher)
            trigger_th = math.ceil(prior_high20 / tick) * tick + tick
            stop = math.floor(min(prior_low20 - tick, trigger - 1.6 * (h - l + 1e-6)) / tick) * tick
            if stop <= 0:
                continue
            cons = (l <= trigger <= h) and (o <= trigger) and (c >= trigger)
            if not cons:
                continue

            def exit_ret(fill, stop_px, mode):
                # same-day stop first (conservative: if low < stop assume stopped)
                if l < stop_px:
                    return stop_px / fill - 1 - 0.001
                bars = []
                for key in (n1, n2, n3):
                    d = key.get(date)
                    if d and d in idx:
                        bars.append(series[idx[d]])
                if not bars:
                    return None
                if mode == "e1_only":
                    r1 = bars[0]
                    px = stop_px if r1[4] <= stop_px else r1[5]
                    return px / fill - 1 - 0.001
                if mode == "e1_or_be":
                    r1 = bars[0]
                    if r1[4] <= stop_px:
                        return stop_px / fill - 1 - 0.001
                    # if high reaches +2% take that (approx profit)
                    if r1[3] >= fill * 1.02:
                        return 0.02 - 0.001
                    return r1[5] / fill - 1 - 0.001
                if mode == "e2_time":
                    # hold through e1 unless stop; exit e2 close/stop
                    r1 = bars[0]
                    if r1[4] <= stop_px:
                        return stop_px / fill - 1 - 0.001
                    r2 = bars[1] if len(bars) > 1 else r1
                    px = stop_px if r2[4] <= stop_px else r2[5]
                    return px / fill - 1 - 0.001
                if mode == "best_of_e1e2_close":
                    r1 = bars[0]
                    if r1[4] <= stop_px:
                        return stop_px / fill - 1 - 0.001
                    r2 = bars[1] if len(bars) > 1 else r1
                    px = max(r1[5], r2[5])
                    return px / fill - 1 - 0.001
                if mode == "e1_close_only":
                    r1 = bars[0]
                    return r1[5] / fill - 1 - 0.001
                return None

            rec = {
                "date": date,
                "symbol": sym,
                "gap": gap,
                "o2c": o2c,
                "gene": gene,
                "amount": amount,
                "ret5": ret5,
                "ret20": ret20,
                "vol_ratio": vol_ratio,
                "close": c,
                "weekday": __import__("datetime").date.fromisoformat(date).weekday(),
                "trigger": trigger,
                "stop": stop,
                "ret_e1": exit_ret(trigger, stop, "e1_only"),
                "ret_e1be": exit_ret(trigger, stop, "e1_or_be"),
                "ret_e2": exit_ret(trigger, stop, "e2_time"),
                "ret_best": exit_ret(trigger, stop, "best_of_e1e2_close"),
            }
            samples.append(rec)

    def run(pred, ret_key):
        nets = [s[ret_key] for s in samples if pred(s) and s.get(ret_key) is not None]
        if len(nets) < 50:
            return {"n": len(nets), "win_rate": None, "mean_net": None}
        return {
            "n": len(nets),
            "win_rate": sum(1 for x in nets if x > 0) / len(nets),
            "mean_net": statistics.mean(nets),
            "median_net": statistics.median(nets),
            "sum_net": sum(nets),
        }

    experiments = []
    for ret_key in ("ret_e1", "ret_e1be", "ret_e2", "ret_best"):
        for gene_min in (0, 3, 6):
            for amt_min in (0, 8e8, 1.5e9):
                for o2c_min in (0.02, 0.03, 0.05):
                    for close_max in (8.0, 10.0):
                        for r5_max in (None, 0.10):
                            for wd_skip in (False, True):

                                def pred(s, gene_min=gene_min, amt_min=amt_min, o2c_min=o2c_min, close_max=close_max, r5_max=r5_max, wd_skip=wd_skip):
                                    if s["gene"] < gene_min:
                                        return False
                                    if s["amount"] < amt_min:
                                        return False
                                    if s["o2c"] < o2c_min:
                                        return False
                                    if s["close"] > close_max:
                                        return False
                                    if r5_max is not None and (s.get("ret5") is None or s["ret5"] > r5_max):
                                        return False
                                    if wd_skip and s["weekday"] == 4:
                                        return False
                                    return True

                                stt = run(pred, ret_key)
                                if stt["win_rate"] is None:
                                    continue
                                experiments.append(
                                    {
                                        "exit": ret_key,
                                        "gene_min": gene_min,
                                        "amt_min": amt_min,
                                        "o2c_min": o2c_min,
                                        "close_max": close_max,
                                        "ret5_max": r5_max,
                                        "skip_friday": wd_skip,
                                        **stt,
                                    }
                                )

    experiments.sort(key=lambda z: (-(z["win_rate"] or 0), -(z["mean_net"] or -9), -z["n"]))
    top = experiments[:20]
    ge80 = [e for e in experiments if (e["win_rate"] or 0) >= 0.80 and e["n"] >= 100]
    ge75 = [e for e in experiments if (e["win_rate"] or 0) >= 0.75 and e["n"] >= 100]
    result = {
        "kind": "high_winrate_deep_search",
        "end_date": end,
        "gap_cons_samples": len(samples),
        "experiments": len(experiments),
        "best": top,
        "rules_winrate_ge_75_n_ge_100": ge75[:10],
        "rules_winrate_ge_80_n_ge_100": ge80,
        "max_win_rate": top[0]["win_rate"] if top else None,
        "max_win_rate_rule": top[0] if top else None,
        "claim_80_allowed": bool(ge80),
        "execution_win_rate_80_validated": bool(ge80),
    }
    write_json(args.output, result)
    print(json.dumps({k: result[k] for k in result if k != "best"}, ensure_ascii=False, indent=2))
    if top:
        print("TOP5:")
        for e in top[:5]:
            print(json.dumps(e, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
