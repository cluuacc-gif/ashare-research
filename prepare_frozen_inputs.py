#!/usr/bin/env python3
"""Freeze descriptive inputs from an existing market DB; never grants readiness.

No network, no prediction, no DB writes, and no historical refill. A weekend
supplement retains the last actual session as its base. Unknown official state
and missing inputs remain unknown, even when vendor coverage is high.
"""
import argparse
import collections
import datetime as dt
import hashlib
import itertools
import json
import math
from pathlib import Path
import sqlite3
import statistics

import collector as c
import short_term as st
from trade_calendar import context, adjacent

VERSION = "frozen-descriptive-inputs-1.0.0"


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def write_jsonl(path, rows):
    with path.open("x", encoding="utf-8") as stream:
        for row in rows:
            stream.write(canonical(row) + "\n")


def valid_number(value):
    return type(value) in (float, int) and math.isfinite(value)


def prepare(db_path, base, next_day, expected_sha, output):
    started = c.stamp()
    path, out = Path(db_path).resolve(), Path(output).resolve()
    if not path.is_file() or st.digest(path) != expected_sha:
        raise ValueError("existing current database hash mismatch")
    if not context(base)["is_session"] or adjacent(base, 1) != next_day:
        raise ValueError("base/next session mismatch")
    if base > c.now().date().isoformat():
        raise ValueError("future base date forbidden")
    if base == c.now().date().isoformat() and c.now().time() < dt.time(18):
        raise ValueError("same-day evening inputs require an after-18:00 run")
    out.mkdir(parents=True, exist_ok=False)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        if db.execute("PRAGMA application_id").fetchone()[0] != c.APP_ID:
            raise ValueError("not the original market database")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("database integrity failure")
        master = {r["symbol"]: dict(r) for r in db.execute(
            "SELECT * FROM security_master ORDER BY symbol")}
        write_jsonl(out / "security_master_observed.jsonl", master.values())
        current = {r["symbol"]: dict(r) for r in db.execute(
            "SELECT symbol,trade_date," + ",".join(c.FIELDS) +
            ",fetched_at,source_updated_at,source,finality,conflict FROM daily_quotes WHERE trade_date=?",
            (base,))}
        write_jsonl(out / "base_quotes_observed.jsonl", [current[s] for s in sorted(current)])
        features, errors, counts, history_latest, validations = [], [], {}, {}, []
        selected = "symbol,trade_date," + ",".join(c.FIELDS) + ",fetched_at,source,conflict"
        cursor = db.execute("SELECT " + selected +
                            " FROM daily_quotes WHERE trade_date<=? ORDER BY symbol,trade_date", (base,))
        for symbol, group in itertools.groupby(cursor, key=lambda r: r["symbol"]):
            rows = [dict(r) for r in group]
            counts[symbol] = sum(all(valid_number(r[k]) and r[k] > 0 for k in
                                    ("open", "high", "low", "close")) and
                                 valid_number(r["volume"]) and r["volume"] >= 0 for r in rows)
            history_latest[symbol] = rows[-1]["trade_date"]
            if rows[-1]["trade_date"] != base:
                errors.append({"symbol": symbol, "reason": "no_base_day_quote",
                               "latest_date": rows[-1]["trade_date"]})
                continue
            try:
                if any(r["conflict"] for r in rows):
                    raise ValueError("unresolved quote conflict")
                # Limits in this vendor DB are not official: never pass them as
                # officially verified inputs to the limit-event calculation.
                value = st.daily_features(rows, base)
                amounts = [r.get("amount") for r in rows[-20:]]
                full_amount = len(amounts) == 20 and all(valid_number(v) and v >= 0 for v in amounts)
                value.update({
                    "wrapper_version": VERSION,
                    "evidence_label": "derived_calculation",
                    "input_database_sha256": expected_sha,
                    "input_rows_sha256": hashlib.sha256(canonical(rows).encode()).hexdigest(),
                    "input_first_date": rows[0]["trade_date"],
                    "input_last_date": rows[-1]["trade_date"],
                    "input_source_names": sorted({r["source"] for r in rows}),
                    "input_fetched_at_min": min(r["fetched_at"] for r in rows),
                    "input_fetched_at_max": max(r["fetched_at"] for r in rows),
                    "amount_today": rows[-1]["amount"],
                    "turnover_today": rows[-1]["turnover"],
                    "amplitude_today_provider_percent": rows[-1]["amplitude"],
                    "median_amount20": statistics.median(amounts) if full_amount else None,
                    "amount_observations_last20": sum(valid_number(v) for v in amounts),
                    "prior_high60": max(r["high"] for r in rows[-61:-1]) if len(rows) >= 61 else None,
                    "mean_volume20": statistics.mean(r["volume"] for r in rows[-20:])
                        if len(rows) >= 20 and all(valid_number(r["volume"]) for r in rows[-20:]) else None,
                    "formal_use_allowed": False,
                    "limitations": ["vendor_observation_windows_not_verified_exchange_sessions",
                                    "unadjusted_prices_corporate_action_breaks_unverified",
                                    "official_status_limits_and_sector_groups_unverified",
                                    "activity_score_and_all_probabilities_withheld"],
                })
                # Recalculate three independent formulas against the real rows.
                checks = {"ma20": len(rows) < 20 or math.isclose(value["ma"].get("20"), sum(r["close"] for r in rows[-20:])/20, rel_tol=1e-12),
                          "return5": len(rows) <= 5 or math.isclose(value["returns"]["5"], (rows[-1]["close"]-rows[-6]["close"])/rows[-6]["close"], abs_tol=1e-12),
                          "no_fake_probabilities": value["probabilities"] is None and value["activity_score"] is None,
                          "no_fake_limits": all(a["touch_count"] is None and a["close_limit_count"] is None for a in value["activity"].values())}
                if not all(checks.values()):
                    raise ValueError("independent formula/label QA failed")
                validations.append({"symbol": symbol, "checks": checks})
                features.append(value)
            except (ValueError, TypeError, ZeroDivisionError) as exc:
                errors.append({"symbol": symbol, "reason": "invalid_feature_input", "detail": str(exc)})
            if len(counts) % 1000 == 0:
                print(canonical({"processed": len(counts), "features": len(features)}), flush=True)
        write_jsonl(out / "features_diagnostic.jsonl", features)
        write_jsonl(out / "feature_validation_real.jsonl", validations)
        fby = {f["symbol"]: f for f in features}
        paths, observed_pool = [], []
        for symbol, security in master.items():
            quote = current.get(symbol)
            feature = fby.get(symbol)
            close = quote.get("close") if quote else None
            inside = 5 <= close <= 10 if valid_number(close) else None
            reasons = []
            if inside is False:
                reasons.append("outside_raw_close_5_to_10")
            if inside is None:
                reasons.append("base_day_close_unavailable")
            if counts.get(symbol, 0) < 250:
                reasons.append("fewer_than_250_observed_bars")
            if not feature:
                reasons.append("base_features_unavailable")
            med = feature.get("median_amount20") if feature else None
            if med is None:
                reasons.append("liquidity_20day_median_amount_unverified")
            elif med < 30_000_000:
                reasons.append("median_amount20_below_30million")
            amount = quote.get("amount") if quote else None
            if amount is None:
                reasons.append("base_amount_unavailable")
            elif amount < 50_000_000:
                reasons.append("base_amount_below_50million")
            reasons.extend(["official_master_completeness_unverified", "official_ST_halt_delisting_status_unverified",
                            "official_limit_prices_unverified", "major_risk_news_overlay_incomplete"])
            row = {"symbol": symbol, "name": security["name"], "exchange": security["exchange"],
                   "base_trade_date": base, "raw_close": close,
                   "observed_price_in_range": inside, "history_bars": counts.get(symbol, 0),
                   "history_latest_date": history_latest.get(symbol),
                   "vendor_name_contains_ST": "ST" in (security["name"] or "").upper(),
                   "official_status_verified": False, "formal_eligible": False,
                   "disposition": "unverified_price_interval_observation" if inside else "not_in_observed_price_interval",
                   "reasons": reasons}
            paths.append(row)
            if inside:
                observed_pool.append(row)
        write_jsonl(out / "screening_path_all_observed.jsonl", paths)
        write_jsonl(out / "price_interval_observed_NOT_CANDIDATES.jsonl", observed_pool)
        write_jsonl(out / "feature_errors.jsonl", errors)
        n = len(master)
        missing_base = sorted(set(master)-set(current))
        field_coverage = {field: {"n": sum(valid_number(r.get(field)) for r in current.values()),
                                   "denominator_vendor_master": n}
                          for field in c.FIELDS}
        observed_returns = [f["returns"]["1"] for f in features if f["returns"]["1"] is not None]
        q = {"schema_version": VERSION, "run_id": out.name, "started_at": started,
             "generated_at": c.stamp(), "preparation_date": c.now().date().isoformat(),
             "base_trade_date": base, "next_trade_date": next_day,
             "data_status": "DATA NOT READY", "formal_prediction_allowed": False,
             "bootstrap_complete": False, "model_ready": False,
             "preparation_kind": "readonly_existing_data_supplement",
             "database_sha256_before": expected_sha,
             "database_sha256_after": st.digest(path), "market_db_written": False,
             "official_expected_securities": None, "vendor_master_securities": n,
             "base_quotes": len(current), "base_missing_symbols": missing_base,
             "database_latest_date": db.execute("SELECT MAX(trade_date) FROM daily_quotes").fetchone()[0],
             "base_vendor_coverage": len(current)/n if n else None,
             "official_universe_coverage": None,
             "historical_rows": sum(1 for _ in db.execute("SELECT symbol FROM daily_quotes")),
             "history_250_symbols": sum(v >= 250 for v in counts.values()),
             "history_250_latest_base": sum(v >= 250 and history_latest[s] == base for s,v in counts.items()),
             "history_short_symbols": sorted(s for s in master if counts.get(s,0) < 250),
             "observed_price_interval_count": len(observed_pool),
             "price_interval_history_250": sum(r["history_bars"] >= 250 for r in observed_pool),
             "descriptive_features_count": len(features), "feature_errors": errors,
             "full_amount20_features": sum(f["median_amount20"] is not None for f in features),
             "official_master_rows": sum(bool(r["official"]) for r in master.values()),
             "official_status_rows": db.execute("SELECT COUNT(DISTINCT symbol) FROM security_status_daily WHERE verified=1 AND trade_date=?", (base,)).fetchone()[0],
             "official_limit_rows": db.execute("SELECT COUNT(DISTINCT symbol) FROM limit_records WHERE official_verified=1 AND trade_date=?", (base,)).fetchone()[0],
             "sector_membership_rows": db.execute("SELECT COUNT(*) FROM industry_members").fetchone()[0],
             "field_coverage": field_coverage,
             "feature_coverage": {k: sum(f.get(k) is not None for f in features) for k in
                                  ("atr14", "rsi14", "macd", "kdj", "boll20", "volume_vs_prior5", "mean_amount20")},
             "real_formula_check_symbols": len(validations),
             "observed_panel_breadth": {"scope": "vendor_observation_panel_including_unverified_states_not_official_market_breadth",
                 "return_basis": "raw_close_over_previous_observed_close_minus_one",
                 "denominator": len(observed_returns),
                 "up": sum(r > 1e-10 for r in observed_returns),
                 "down": sum(r < -1e-10 for r in observed_returns),
                 "flat": sum(abs(r) <= 1e-10 for r in observed_returns),
                 "up_ge5": sum(r >= .05-1e-10 for r in observed_returns),
                 "down_le_minus5": sum(r <= -.05+1e-10 for r in observed_returns),
                 "median_return": statistics.median(observed_returns) if observed_returns else None,
                 "mean_return": statistics.mean(observed_returns) if observed_returns else None,
                 "emotion_score": None},
             "formal_pool": None, "watchlist": [], "rankings": None, "probabilities": None,
             "blockers": ["official_master_and_state_incomplete", str(len(missing_base))+"_vendor_base_quotes_missing" if missing_base else "official_finality_unverified",
                          "historical_amount_batch_pending_replay_and_import", "official_limits_and_actions_unverified",
                          "sector_fundflow_chip_news_risk_coverage_incomplete", "formal_calibrated_model_not_passed"],
             "sources": {"market_db_sha256": expected_sha,
                         "existing_feature_module_sha256": st.digest(Path(st.__file__)),
                         "wrapper_sha256": st.digest(Path(__file__)),
                         "calendar_file_sha256": st.digest(Path(__file__).with_name("exchange_calendar_2026.json"))},
             "counterfactual_note": "No candidates, orders or tomorrow labels generated; computation date is real, not backfilled to the base session."}
        if q["database_sha256_after"] != expected_sha:
            raise ValueError("market DB changed during read-only preparation")
        st.write_json(out / "quality.json", q)
        manifest = {"run_id": out.name, "base_trade_date": base, "generated_at": c.stamp(),
                    "data_status": "DATA NOT READY", "files": [
                        {"path": p.name, "sha256": st.digest(p), "bytes": p.stat().st_size}
                        for p in sorted(out.iterdir()) if p.is_file()]}
        st.write_json(out / "manifest.json", manifest)
        return {k:q[k] for k in ("data_status", "base_trade_date", "next_trade_date", "base_quotes",
                                "historical_rows", "history_250_symbols", "descriptive_features_count",
                                "observed_price_interval_count", "full_amount20_features", "real_formula_check_symbols")}
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--next", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(canonical(prepare(args.db, args.base, args.next, args.expected_sha256, args.output)))
