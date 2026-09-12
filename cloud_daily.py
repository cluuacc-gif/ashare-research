#!/usr/bin/env python3
"""After-close cloud transport: real quotes, bounded history repair, no forecasts.

The persistent production DB is updated by the Library writer after this artifact
has been verified. GitHub runner has no prediction DB and never submits orders.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import time

import collector as c
import cloud_probe as probe
from close_snapshot import capture
from short_term import daily_features, write_json, digest
import trade_calendar


def request_for_today(request, now):
    if request is None:
        return {"target_date": now.date().isoformat(), "bootstrap_symbols": [], "request_id": "scheduled-"+now.date().isoformat()}
    if request.get("schema_version") != "1.0" or request.get("operation") != "evening_prepare":
        raise ValueError("unsupported request")
    if request.get("target_date") != now.date().isoformat():
        raise ValueError("stale/future request cannot fetch a different day's current quotes")
    requested = c.timestamp(request["requested_at"])
    if requested > now or requested.date() != now.date():
        raise ValueError("request timestamp is stale or future")
    symbols = request.get("bootstrap_symbols", [])
    if not isinstance(symbols, list) or len(symbols) > 1000 or len(symbols) != len(set(symbols)):
        raise ValueError("invalid bounded bootstrap request")
    if any(c.symbol(s) != s for s in symbols):
        raise ValueError("invalid security symbol")
    return request


def collect(output, request=None, scheduled=False):
    root = Path(output).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("output must be new; old evidence cannot be overwritten")
    root.mkdir(parents=True, exist_ok=True)
    started = c.now()
    cal = trade_calendar.context(started.date())
    write_json(root/"calendar.json", cal)
    # Evaluate the real clock first. On a closed day, zero market requests.
    if not cal["is_session"]:
        result = {"status": "SKIPPED_MARKET_CLOSED", "date": cal["date"],
                  "started_at": started.isoformat(), "finished_at": c.stamp(),
                  "calendar": cal, "actual_new_quotes": 0, "formal_prediction": False}
        write_json(root/"run_status.json", result)
        return result
    if started.time() < dt.time(18):
        raise ValueError("evening preparation must start at/after 18:00 Asia/Shanghai")
    req = request_for_today(None if scheduled else request, started)
    day = req["target_date"]
    report = {"schema_version": "1.1", "mode": "diagnostic/evening_collection",
              "status": "RUNNING", "started_at": started.isoformat(),
              "calendar_upper_bound": day, "calendar_verified": True,
              "data_status": "DATA NOT READY", "formal_probability_model_ready": False,
              "writes_production_databases": False, "master_sources": [],
              "market_census_sources": [], "history_sources": [], "files": [],
              "github_run_id": os.environ.get("GITHUB_RUN_ID"),
              "github_commit": os.environ.get("GITHUB_SHA"), "request": req,
              "collector_sha256": digest(Path(__file__).with_name("collector.py")),
              "bootstrap_requested": len(req.get("bootstrap_symbols", [])),
              "bootstrap_complete": False, "model_ready": False}

    def checkpoint():
        # All already obtained files survive later endpoint errors/time limits.
        report["finished_at"] = c.stamp()
        report["files"] = [probe.file_record(p, root) for folder in ("normalized_master", "normalized_history", "raw_history")
                           for p in sorted((root/folder).glob("*")) if p.is_file()]
        report["files"] += [probe.file_record(root/"calendar.json", root)]
        for source in report["market_census_sources"]:
            if source.get("snapshot"):
                report["files"].append(source["snapshot"])
        write_json(root/"source_acceptance.json", report)

    checkpoint()
    census = {}
    try:
        for i, (ex, operation, kwargs, _, _, _, url) in enumerate(probe.MASTER_SOURCES):
            item = {"exchange": ex, "operation": operation, "source_url": url,
                    "started_at": c.stamp(), "source_updated_at": None}
            try:
                rows = c.ak_call(operation, kwargs, timeout=60)
                if not rows:
                    raise ValueError("empty official response")
                path = root/"normalized_master"/f"{i}_{ex}.json"
                write_json(path, rows)
                item.update(status="retrieved", snapshot=str(path.relative_to(root)),
                            snapshot_kind="akshare_normalized_response", row_count=len(rows))
            except Exception as exc:
                item.update(status="failed", error=str(exc)[:1500])
            item["finished_at"] = c.stamp()
            report["master_sources"].append(item)
            checkpoint()
        fetcher = c.Fetcher(root/"raw_history", retries=2, timeout=15, interval=.5)
        # Independent public sources. A vendor census never certifies ST/halts.
        for name, fn in (("tencent_current_market_census", probe.fetch_tencent_census),
                         ("eastmoney_current_market_census", probe.fetch_vendor_census)):
            try:
                members, meta = fn(fetcher, root, dt.date.fromisoformat(day))
                report["market_census_sources"].append(meta)
                if meta["census_probe_pass"]:
                    census = {s: v for market in members.values() for s, v in market.items()}
                    break
            except Exception as exc:
                report["market_census_sources"].append({"source": name, "status": "failed", "error": str(exc)[:1500]})
        checkpoint()
        if not census:
            raise ValueError("no complete vendor census; cannot enumerate current quotes")
        daily = capture(root)
        report["daily_quote_count"] = daily.get("observed_quotes", 0)
        checkpoint()
        # Only symbols absent from the writer's 250-bar inventory are requested.
        # Normal incremental collection has an empty list: no daily full reload.
        requested = req.get("bootstrap_symbols", [])
        if any(s not in census for s in requested):
            report["bootstrap_unlisted_symbols"] = [s for s in requested if s not in census]
        deadline = time.monotonic() + 1800
        lower = (dt.date.fromisoformat(day)-dt.timedelta(days=850)).isoformat()
        for sym in requested:
            # Keep the artifact below the importer's existing 100 MB safety cap.
            # Unprocessed securities remain pending; do not lower the data gate.
            if time.monotonic() >= deadline or sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) > 65_000_000:
                break
            if sym not in census:
                continue
            for name, provider in (("eastmoney_history", c.em_history),
                                   ("tencent_history", c.tx_history), ("sina_history", c.sina_history)):
                item = {"exchange": sym[-2:], "symbol": sym, "source": name,
                        "purpose": "initial_history_backfill", "started_at": c.stamp()}
                succeeded = False
                try:
                    rows, meta = provider(fetcher, sym, lower, day)
                    dates = [r["trade_date"] for r in rows]
                    if not rows or len(dates) != len(set(dates)):
                        raise ValueError("empty/duplicate historical dates")
                    for row in rows:
                        c.validate_quote(row, day)
                        row["snapshot"] = {**row["snapshot"], "path": str(Path(row["snapshot"]["path"]).relative_to(root))}
                    rows.sort(key=lambda r: r["trade_date"])
                    path = root/"normalized_history"/f"{sym}_{name}.json"
                    write_json(path, rows)
                    n = sum(all(r.get(k) is not None for k in ("open", "high", "low", "close", "volume")) for r in rows)
                    item.update(status="history_250_retrieved" if n >= 250 else "insufficient",
                                bars=len(rows), ohlcv_bars=n, amount_bars=sum(r.get("amount") is not None for r in rows),
                                earliest_date=rows[0]["trade_date"], latest_date=rows[-1]["trade_date"],
                                normalized_ref=str(path.relative_to(root)), source_url=meta["url"], fetched_at=meta["fetched_at"])
                    succeeded = n >= 250
                    if rows[-1]["trade_date"] == day:
                        write_json(root/"partial_features"/f"{sym}.json", daily_features(rows, day))
                except Exception as exc:
                    item.update(status="failed", error=str(exc)[:1500])
                item["finished_at"] = c.stamp()
                report["history_sources"].append(item)
                checkpoint()
                if succeeded:
                    break
        done = {x["symbol"] for x in report["history_sources"] if x.get("status") == "history_250_retrieved"}
        report["bootstrap_batch_succeeded"] = len(done)
        report["bootstrap_pending"] = [s for s in requested if s not in done]
        report["status"] = "REAL_DATA_PARTIAL" if report["daily_quote_count"] else "DATA_NOT_RETRIEVED"
    except Exception as exc:
        report["status"] = "DATA_NOT_READY"
        report["error"] = f"{type(exc).__name__}: {exc}"[:2000]
    finally:
        checkpoint()
        status = {"status": report["status"], "data_status": "DATA NOT READY", "date": day,
                  "actual_new_quotes": report.get("daily_quote_count", 0),
                  "history_250_symbols_in_this_batch": report.get("bootstrap_batch_succeeded", 0),
                  "bootstrap_complete": False, "model_ready": False,
                  "started_at": report["started_at"], "finished_at": report["finished_at"]}
        write_json(root/"run_status.json", status)
        files = [probe.file_record(p, root) for p in sorted(root.rglob("*")) if p.is_file()]
        write_json(root/"transport_manifest.json", {"files": files, "generated_at": c.stamp(), "scope": "real partial transport, not a successful research dataset"})
    return status


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True)
    p.add_argument("--request", type=Path)
    p.add_argument("--scheduled", action="store_true", help="direct cloud timer: current quotes only; no implicit bootstrap")
    args = p.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8")) if args.request and args.request.exists() else None
    print(json.dumps(collect(args.output, request, args.scheduled), ensure_ascii=False))
