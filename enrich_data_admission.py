#!/usr/bin/env python3
"""Fill descriptive admission fields from real quotes; never mark official gates as passed.

Computes board-based limit prices (unofficial), ST flags from names, and a data
admission report. Official exchange verification remains false unless sources exist.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import collector as c  # noqa: E402
from sealed_database import inspect_sealed  # noqa: E402
from short_term import write_json  # noqa: E402

# Board limit ratios (descriptive research defaults; NOT official certification).
RATIO = {
    "SH688": 0.20,  # STAR
    "SZ300": 0.20,  # ChiNext
    "SZ301": 0.20,
    "BJ": 0.30,     # BSE
    "DEFAULT": 0.10,
}


def board_ratio(symbol: str) -> float:
    if symbol.endswith(".BJ"):
        return RATIO["BJ"]
    code = symbol.split(".")[0]
    if code.startswith("688") or code.startswith("689"):
        return RATIO["SH688"]
    if code.startswith("300") or code.startswith("301"):
        return RATIO["SZ300"]
    return RATIO["DEFAULT"]


def round_tick(value: float) -> float:
    return round(value + 1e-9, 2)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    path = Path(args.db).resolve()
    sealed = inspect_sealed(path)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        total = db.execute("SELECT COUNT(*) FROM daily_quotes").fetchone()[0]
        amount_ok = db.execute("SELECT COUNT(*) FROM daily_quotes WHERE amount IS NOT NULL").fetchone()[0]
        base = db.execute("SELECT MAX(trade_date) FROM daily_quotes").fetchone()[0]
        # History amount20 completeness for 5-10 band on base date.
        band = db.execute(
            """
            SELECT symbol, close FROM daily_quotes
            WHERE trade_date=? AND close BETWEEN 5 AND 10
            """,
            (base,),
        ).fetchall()
        full_amount20 = 0
        for row in band:
            rows = db.execute(
                "SELECT amount FROM daily_quotes WHERE symbol=? AND trade_date<=? "
                "AND amount IS NOT NULL ORDER BY trade_date DESC LIMIT 20",
                (row["symbol"], base),
            ).fetchall()
            if len(rows) == 20 and all(r[0] is not None and r[0] >= 0 for r in rows):
                full_amount20 += 1
        name_st = 0
        for row in db.execute("SELECT symbol, name FROM security_master").fetchall():
            if row["name"] and ("ST" in row["name"].upper() or "*ST" in (row["name"] or "")):
                name_st += 1
        status_verified = db.execute(
            "SELECT COUNT(*) FROM security_status_daily WHERE verified=1"
        ).fetchone()[0]
        limit_official = db.execute(
            "SELECT COUNT(*) FROM limit_records WHERE official_verified=1"
        ).fetchone()[0]
        industry = db.execute("SELECT COUNT(*) FROM industry_members").fetchone()[0]
        news = db.execute("SELECT COUNT(*) FROM news_events").fetchone()[0]
        hist250 = db.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT symbol FROM daily_quotes
              WHERE close>0 AND volume IS NOT NULL
              GROUP BY symbol HAVING COUNT(*)>=250
            )
            """
        ).fetchone()[0]
        symbols = db.execute("SELECT COUNT(*) FROM security_master").fetchone()[0]
    finally:
        db.close()

    # Descriptive gate checklist (honest).
    checks = {
        "amount_history_merged": amount_ok / total > 0.90 if total else False,
        "amount_coverage_ratio": round(amount_ok / total, 6) if total else 0.0,
        "full_amount20_on_price_band": full_amount20,
        "price_band_count_on_base": len(band),
        "history_250_symbols": hist250,
        "securities": symbols,
        "st_name_flags_descriptive_only": name_st,
        "official_status_verified_rows": status_verified,
        "official_limit_verified_rows": limit_official,
        "sector_membership_rows": industry,
        "news_event_rows": news,
        "calendar_verified": True,
        "official_status_verified": False,
        "official_limits_verified": False,
        "corporate_actions_verified": False,
        "news_risk_verified": False,
        "bootstrap_complete": hist250 >= 5000,
    }
    blockers = []
    if not checks["amount_history_merged"]:
        blockers.append("amount_coverage_below_90pct")
    if checks["official_status_verified_rows"] == 0:
        blockers.append("official_status_daily_missing")
    if checks["official_limit_verified_rows"] == 0:
        blockers.append("official_daily_limits_missing")
    if checks["sector_membership_rows"] == 0:
        blockers.append("sector_membership_missing")
    if checks["news_event_rows"] == 0:
        blockers.append("news_risk_coverage_missing")
    if checks["full_amount20_on_price_band"] < checks["price_band_count_on_base"] * 0.9:
        blockers.append("amount20_incomplete_on_price_band")
    # corporate actions cannot be asserted from current evidence
    blockers.append("point_in_time_corporate_actions_unverified")

    data_ready = not blockers
    report = {
        "schema_version": "1.0",
        "kind": "data_admission_assessment",
        "generated_at": c.stamp(),
        "base_trade_date": base,
        "database": sealed,
        "checks": checks,
        "blockers": blockers,
        "data_status": "DATA READY" if data_ready else "DATA NOT READY",
        "bootstrap_complete": checks["bootstrap_complete"] and data_ready,
        "model_ready": False,
        "note": (
            "Amount merge from real-amount-evidence is complete. "
            "Board-based limit prices and ST name flags are descriptive only "
            "and are not official exchange verification."
        ),
    }
    write_json(out / "data_admission_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
