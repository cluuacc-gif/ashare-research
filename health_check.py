#!/usr/bin/env python3
"""End-to-end health check for the A-share research pipeline.

Read-only. Verifies calendar, market DB, recent quote coverage, sector/news/status
coverage, and optionally SMTP configuration (without sending).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import collector as c  # noqa: E402
from short_term import write_json  # noqa: E402
from trade_calendar import context  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--output")
    args = p.parse_args()
    day = datetime.now(TZ).date().isoformat()
    cal = context(day)
    checks = {}
    blockers = []

    path = Path(args.db)
    checks["db_exists"] = path.is_file()
    if not path.is_file():
        blockers.append("market_database_missing")
        report = {"kind": "health_check", "generated_at": datetime.now(TZ).isoformat(), "checks": checks, "blockers": blockers, "ok": False}
        if args.output:
            write_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        app = db.execute("PRAGMA application_id").fetchone()[0]
        checks["app_id_ok"] = app == c.APP_ID
        checks["integrity"] = db.execute("PRAGMA quick_check").fetchone()[0]
        if not checks["app_id_ok"]:
            blockers.append("wrong_app_id")
        if checks["integrity"] != "ok":
            blockers.append("integrity_check_failed")
        latest = db.execute("SELECT MAX(trade_date) FROM daily_quotes").fetchone()[0]
        rows = db.execute("SELECT COUNT(*) FROM daily_quotes").fetchone()[0]
        quotes_latest = db.execute(
            "SELECT COUNT(*) FROM daily_quotes WHERE trade_date=?", (latest,)
        ).fetchone()[0]
        amount_ratio = db.execute(
            "SELECT AVG(amount IS NOT NULL) FROM daily_quotes WHERE trade_date=?", (latest,)
        ).fetchone()[0] or 0
        industry = db.execute("SELECT COUNT(*) FROM industry_members").fetchone()[0]
        news = db.execute("SELECT COUNT(*) FROM news_events").fetchone()[0]
        status = db.execute(
            "SELECT COUNT(DISTINCT symbol) FROM security_status_daily"
        ).fetchone()[0]
        limits = db.execute(
            "SELECT COUNT(*) FROM limit_records WHERE trade_date>=date(?, '-7 day')",
            (latest,),
        ).fetchone()[0]
        band = db.execute(
            "SELECT COUNT(*) FROM daily_quotes WHERE trade_date=? AND close BETWEEN 5 AND 10",
            (latest,),
        ).fetchone()[0]
    finally:
        db.close()

    checks.update(
        {
            "latest_trade_date": latest,
            "daily_rows": rows,
            "quotes_latest": quotes_latest,
            "amount_ratio_latest": round(float(amount_ratio), 4),
            "industry_members": industry,
            "news_events": news,
            "status_symbols": status,
            "limit_rows_recent": limits,
            "price_band_5_10": band,
            "calendar_is_session_today": cal["is_session"],
            "next_session": cal.get("next_session"),
            "previous_session": cal.get("previous_session"),
        }
    )
    if not latest:
        blockers.append("no_quotes")
    if quotes_latest < 5000:
        blockers.append("latest_day_thin_coverage")
    if industry < 1000:
        blockers.append("industry_sparse")
    if news < 10:
        blockers.append("news_sparse")
    if status < 5000:
        blockers.append("status_sparse")

    smtp_keys = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "MAIL_TO"]
    smtp = {k: bool(os.environ.get(k)) for k in smtp_keys}
    checks["smtp_env"] = smtp
    if not all(smtp.values()):
        blockers.append("smtp_secrets_incomplete_for_local_send")

    # Research gates stay honest.
    checks["data_status"] = "DATA NOT READY"
    checks["model_ready"] = False
    checks["ok"] = not any(
        b
        for b in blockers
        if b
        not in (
            "smtp_secrets_incomplete_for_local_send",
        )
    )
    report = {
        "kind": "health_check",
        "generated_at": datetime.now(TZ).isoformat(),
        "day": day,
        "checks": checks,
        "blockers": blockers,
        "ok": checks["ok"],
    }
    if args.output:
        write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if checks["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
