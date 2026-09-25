#!/usr/bin/env python3
"""Backfill descriptive (non-official) limit prices and ST flags into market DB copy."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import collector as c  # noqa: E402
from enrich_data_admission import board_ratio, round_tick  # noqa: E402
from sealed_database import inspect_sealed, sha256  # noqa: E402
from short_term import write_json  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--expected-db-sha256", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--limit-record-days", type=int, default=40)
    args = p.parse_args()
    src = Path(args.db).resolve()
    if sha256(src) != args.expected_db_sha256:
        raise SystemExit("db sha mismatch")
    before = inspect_sealed(src)
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    working = out / src.name
    shutil.copy2(src, working)
    if inspect_sealed(working) != before:
        raise SystemExit("copy differs")

    db = sqlite3.connect(working)
    db.execute("PRAGMA journal_mode=DELETE")
    updated_fields = limit_rows = status_rows = 0
    try:
        with db:
            # 1) Fill limit_up/limit_down on history facts where null.
            rows = db.execute(
                "SELECT symbol, trade_date, prev_close, close, limit_up, limit_down "
                "FROM history_quote_facts WHERE limit_up IS NULL OR limit_down IS NULL"
            ).fetchall()
            for symbol, trade_date, prev_close, close, lu, ld in rows:
                ref = prev_close if prev_close and prev_close > 0 else close
                if not ref or ref <= 0:
                    continue
                ratio = board_ratio(symbol)
                up, down = round_tick(ref * (1 + ratio)), round_tick(ref * (1 - ratio))
                db.execute(
                    "UPDATE history_quote_facts SET limit_up=?, limit_down=? WHERE symbol=? AND trade_date=?",
                    (up, down, symbol, trade_date),
                )
                updated_fields += 1
            # 2) Same for quote_overrides (evening/newer quotes).
            rows = db.execute(
                "SELECT symbol, trade_date, prev_close, close FROM quote_overrides "
                "WHERE limit_up IS NULL OR limit_down IS NULL"
            ).fetchall()
            for symbol, trade_date, prev_close, close in rows:
                ref = prev_close if prev_close and prev_close > 0 else close
                if not ref or ref <= 0:
                    continue
                ratio = board_ratio(symbol)
                up, down = round_tick(ref * (1 + ratio)), round_tick(ref * (1 - ratio))
                db.execute(
                    "UPDATE quote_overrides SET limit_up=?, limit_down=? WHERE symbol=? AND trade_date=?",
                    (up, down, symbol, trade_date),
                )
                updated_fields += 1

            # 3) limit_records only for recent window (descriptive, official=0).
            max_day = db.execute("SELECT MAX(trade_date) FROM daily_quotes").fetchone()[0]
            days = [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT trade_date FROM daily_quotes WHERE trade_date<=? ORDER BY trade_date DESC LIMIT ?",
                    (max_day, args.limit_record_days),
                )
            ]
            dayset = set(days)
            recent = db.execute(
                "SELECT symbol, trade_date, prev_close, close, high, low FROM daily_quotes "
                "WHERE trade_date IN (%s) AND close>0" % ",".join("?" * len(days)),
                days,
            ).fetchall()
            for symbol, trade_date, prev_close, close, high, low in recent:
                ref = prev_close if prev_close and prev_close > 0 else close
                if not ref or ref <= 0:
                    continue
                ratio = board_ratio(symbol)
                up, down = round_tick(ref * (1 + ratio)), round_tick(ref * (1 - ratio))
                touched_up = 1 if high is not None and high >= up - 1e-6 else 0
                closed_up = 1 if close is not None and close >= up - 1e-6 else 0
                touched_down = 1 if low is not None and low <= down + 1e-6 else 0
                closed_down = 1 if close is not None and close <= down + 1e-6 else 0
                db.execute(
                    "INSERT OR REPLACE INTO limit_records("
                    "symbol,trade_date,observed_at,limit_up,limit_down,applicable,"
                    "official_verified,touched_up,closed_up,failed_close,touched_down,closed_down,source,payload"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        symbol,
                        trade_date,
                        c.stamp(),
                        up,
                        down,
                        1,
                        0,
                        touched_up,
                        closed_up,
                        None,
                        touched_down,
                        closed_down,
                        "descriptive_board_ratio",
                        json.dumps({"ratio": ratio, "ref_close": ref}, ensure_ascii=False),
                    ),
                )
                limit_rows += 1

            # 4) ST name flags (descriptive standing row).
            for row in db.execute("SELECT symbol, name FROM security_master").fetchall():
                name = row[1] or ""
                is_st = 1 if "ST" in name.upper() else 0
                db.execute(
                    "INSERT OR REPLACE INTO security_status_daily("
                    "symbol,trade_date,observed_at,is_st,delisting,trading_status,verified,source_url,published_at,payload"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        row[0],
                        "1970-01-01",
                        c.stamp(),
                        is_st,
                        0,
                        "unknown",
                        0,
                        None,
                        None,
                        json.dumps({"descriptive_name_flag": True, "name": name}, ensure_ascii=False),
                    ),
                )
                status_rows += 1

        db.execute(
            "INSERT INTO source_log(run_id,source,operation,fetched_at,status,url,error,snapshot_ref,sha256,bytes) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "descriptive-admission-fill",
                "derived_from_quotes_and_names",
                "descriptive_limit_status_fill",
                c.stamp(),
                "success",
                None,
                None,
                None,
                None,
                None,
            ),
        )
        db.commit()
    finally:
        db.close()
    sealed = inspect_sealed(working)
    report = {
        "kind": "descriptive_admission_fill",
        "history_or_override_rows_filled": updated_fields,
        "limit_record_rows": limit_rows,
        "status_rows": status_rows,
        "limit_record_days": args.limit_record_days,
        "official_verified": False,
        "working_database": sealed,
        "data_status": "DATA NOT READY",
    }
    write_json(out / "descriptive_fill.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
