#!/usr/bin/env python3
"""Local orchestration checks; market DB always opened read-only in this file."""
import argparse
import datetime as dt
import json
from pathlib import Path
import sqlite3

import collector as c
from short_term import digest, write_json
from trade_calendar import context, adjacent


def inventory(db_path):
    path = Path(db_path).resolve()
    if not path.is_file():
        raise ValueError("existing market database required")
    db = sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        if db.execute("PRAGMA application_id").fetchone()[0] != c.APP_ID:
            raise ValueError("wrong database identity")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("database integrity check failed")
        counts = db.execute("SELECT symbol,COUNT(*) AS n,MAX(trade_date) AS latest FROM daily_quotes WHERE open>0 AND high>0 AND low>0 AND close>0 AND volume IS NOT NULL GROUP BY symbol").fetchall()
        by_symbol = {r["symbol"]: dict(r) for r in counts}
        symbols = [r[0] for r in db.execute("SELECT symbol FROM security_master ORDER BY symbol")]
        return {"sha256": digest(path), "securities": len(symbols),
                "daily_rows": db.execute("SELECT COUNT(*) FROM daily_quotes").fetchone()[0],
                "latest_date": db.execute("SELECT MAX(trade_date) FROM daily_quotes").fetchone()[0],
                "history_250_symbols": sum(v["n"] >= 250 for v in by_symbol.values()),
                "missing_250_symbols": [s for s in symbols if by_symbol.get(s, {}).get("n", 0) < 250]}
    finally:
        db.close()


def evening_request(db_path, at, limit=1000):
    at = c.timestamp(at)
    cal = context(at.date())
    if not cal["is_session"] or at.time() < dt.time(18):
        raise ValueError("not an after-18:00 exchange session")
    if not 0 <= limit <= 1000:
        raise ValueError("bounded bootstrap batch required")
    inv = inventory(db_path)
    # Exchange round-robin, never a stock recommendation or price-biased list.
    groups = {ex: iter([s for s in inv["missing_250_symbols"] if s.endswith(ex)]) for ex in ("SH", "SZ", "BJ")}
    selected = []
    while groups and len(selected) < limit:
        for ex in list(groups):
            try:
                selected.append(next(groups[ex]))
            except StopIteration:
                del groups[ex]
            if len(selected) >= limit:
                break
    return {"schema_version": "1.0", "operation": "evening_prepare",
            "request_id": "evening-"+at.strftime("%Y%m%dT%H%M%S"),
            "target_date": at.date().isoformat(), "requested_at": at.isoformat(),
            "market_database_sha256_before": inv["sha256"],
            "bootstrap_symbols": selected, "bootstrap_policy": "only symbols with fewer than 250 observed OHLCV bars; raw history, not model training",
            "formal_prediction": False}


def morning_check(handoff_path, db_path, day, at):
    handoff = json.loads(Path(handoff_path).read_text(encoding="utf-8"))
    cal = context(day)
    result = {"mode": "diagnostic/data_unverified", "prediction_date": day,
              "generated_at": c.timestamp(at).isoformat(), "information_cutoff": day+"T09:00:00+08:00",
              "calendar": cal, "data_ready": False, "model_ready": False,
              "formal_prediction_allowed": False, "market_db_written": False, "blockers": []}
    if not cal["is_session"]:
        result["mode"] = "skip/market_closed"
        return result
    expected = adjacent(day, -1)
    result["base_date_required"] = expected
    inv = inventory(db_path)
    result["inventory"] = {k: v for k, v in inv.items() if k != "missing_250_symbols"}
    current_ref = handoff.get("latest_attempt", {}).get("market_db") or handoff.get("resources", {}).get("latest_market_database", {})
    if current_ref.get("sha256") != inv["sha256"]:
        result["blockers"].append("current_database_hash_does_not_match_handoff")
    good = handoff.get("latest_successful_dataset") or {}
    if good.get("base_trade_date") != expected:
        result["blockers"].append("no_successfully_saved_dataset_for_previous_session")
    if good.get("data_status") != "DATA READY" or good.get("bootstrap_complete") is not True:
        result["blockers"].append("full_data_gate_or_bootstrap_not_passed")
    for field in ("market_db", "stock_pool", "features", "sector_snapshot", "quality_json", "bundle"):
        ref = good.get(field)
        if not isinstance(ref, dict) or not ref.get("sha256") or not (ref.get("library_file_id") or ref.get("bundle_library_file_id")):
            result["blockers"].append("missing_frozen_reference:"+field)
    saved = good.get("saved_at") or good.get("dependencies_saved_before_pointer_at")
    if not saved or c.timestamp(saved) > c.timestamp(result["information_cutoff"]):
        result["blockers"].append("dataset_not_saved_before_information_cutoff")
    if good.get("market_db", {}).get("sha256") != inv["sha256"]:
        result["blockers"].append("successful_dataset_database_hash_mismatch")
    if inv["latest_date"] != expected:
        result["blockers"].append("database_latest_date_is_not_previous_session")
    result["data_ready"] = not result["blockers"]
    # A flag alone never authorizes probabilities. The existing model/plan gate
    # must also inspect model files, dated OOS labels, calibration and costs.
    result["model_ready"] = handoff.get("model_ready") is True
    if not result["model_ready"]:
        result["blockers"].append("model_not_validated")
    elif result["data_ready"]:
        result["mode"] = "pending/independent_model_and_news_validation"
    result["late_after_open"] = c.timestamp(at) >= c.timestamp(day+"T09:30:00+08:00")
    if result["late_after_open"]:
        result["blockers"].append("late_after_market_open_excluded_from_forward_results")
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("evening-request")
    q.add_argument("--db", required=True); q.add_argument("--output", required=True)
    q.add_argument("--limit", type=int, default=1000)
    q = sub.add_parser("morning-check")
    q.add_argument("--db", required=True); q.add_argument("--handoff", required=True)
    q.add_argument("--output", required=True)
    q = sub.add_parser("calendar"); q.add_argument("--date", default=c.now().date().isoformat())
    args = p.parse_args()
    if args.command == "evening-request":
        out = evening_request(args.db, c.stamp(), args.limit)
    elif args.command == "morning-check":
        out = morning_check(args.handoff, args.db, c.now().date().isoformat(), c.stamp())
    else:
        out = context(args.date)
    if getattr(args, "output", None):
        write_json(args.output, out)
    print(json.dumps(out, ensure_ascii=False))
