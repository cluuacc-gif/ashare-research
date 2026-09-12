#!/usr/bin/env python3
"""Real full-census historical bootstrap into evidence artifacts, never production DBs."""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import time
import zipfile
import tempfile

import collector as c
import import_probe_artifact as verifier
from short_term import digest, write_json
import trade_calendar

FIELDS = ["trade_date"] + list(c.FIELDS)

def verified_seed(root):
    root = Path(root).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp)/"original_seed.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for p in root.rglob("*"):
                if p.is_file():
                    z.write(p, str(p.relative_to(root)))
        report, files, securities, quotes = verifier.verify(archive)
    day = report["calendar_upper_bound"]
    if not trade_calendar.is_session(day):
        raise ValueError("seed date is not a verified exchange session")
    original_histories = {}
    for item in report["history_sources"]:
        if item.get("status") == "history_250_retrieved":
            original_histories[item["symbol"]] = json.loads(files[item["normalized_ref"]])
    return report, securities, original_histories

def complete_bars(rows):
    return sum(all(r.get(k) is not None for k in ("open","high","low","close","volume")) for r in rows)

def compact_rows(rows):
    return [[r.get(k) for k in FIELDS] for r in rows]

def run(seed, output, part, parts, minutes):
    root = Path(output).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("immutable output directory must be empty")
    root.mkdir(parents=True, exist_ok=True)
    report, securities, initial = verified_seed(seed)
    end = report["calendar_upper_bound"]
    symbols = sorted(securities)[part::parts]
    low = (dt.date.fromisoformat(end)-dt.timedelta(days=850)).isoformat()
    fetcher = c.Fetcher(root/"raw", retries=1, timeout=15, interval=.75)
    deadline = time.monotonic()+minutes*60
    results = {}
    summary = {"schema_version": "1.0", "kind": "real_full_census_history_bootstrap",
               "target_date": end, "part": part, "parts": parts,
               "seed_source_run": report.get("github_run_id"),
               "vendor_universe_count": len(securities),
               "official_full_universe_certified": False,
               "requested_symbols": len(symbols), "started_at": c.stamp(),
               "github_run_id": os.environ.get("GITHUB_RUN_ID"),
               "commit_sha": os.environ.get("GITHUB_SHA"),
               "collector_sha256": digest(Path(c.__file__)),
               "production_database_written": False, "formal_prediction": False,
               "minimum_ohlcv_bars": 250, "results": results}
    def checkpoint():
        summary["finished_at"] = c.stamp()
        summary["attempted_symbols"] = len(results)
        summary["history_250_symbols"] = sum(r.get("ohlcv_bars",0)>=250 for r in results.values())
        summary["latest_date_250_symbols"] = sum(r.get("ohlcv_bars",0)>=250 and r.get("latest_date")==end for r in results.values())
        summary["actual_total_bars"] = sum(r.get("bars",0) for r in results.values())
        summary["pending_symbols"] = [s for s in symbols if s not in results]
        write_json(root/"bootstrap_part.json", summary)
    checkpoint()
    for sym in symbols:
        if time.monotonic() >= deadline:
            break
        item = {"symbol": sym, "name": securities[sym]["name"], "list_date": securities[sym].get("list_date"),
                "official_status_verified": False, "attempts": []}
        chosen = None
        if sym in initial:
            chosen = initial[sym]
            snapshot = chosen[0]["snapshot"]
            old = (Path(seed)/snapshot["path"]).resolve()
            body = gzip.decompress(old.read_bytes())
            if hashlib.sha256(body).hexdigest() != snapshot["sha256"]:
                raise ValueError("seed history raw response mismatch")
            target = root/"raw"/old.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(old.read_bytes())
            for row in chosen:
                row["snapshot"] = {**snapshot,"path":str(target.resolve())}
            item["reused_original_seed_history"] = True
        else:
            # Tencent's raw day field is the verified stable route. Fallbacks
            # remain independent real providers; no qfq/hfq or synthetic rows.
            for name, fn in (("tencent_history",c.tx_history),("eastmoney_history",c.em_history),("sina_history",c.sina_history)):
                attempt={"provider":name,"started_at":c.stamp()}
                try:
                    rows, meta = fn(fetcher,sym,low,end)
                    dates=[r["trade_date"] for r in rows]
                    if not rows or len(set(dates)) != len(dates):
                        raise ValueError("empty or duplicate dates")
                    rows.sort(key=lambda r:r["trade_date"])
                    for r in rows: c.validate_quote(r,end)
                    attempt.update(status="retrieved",bars=len(rows),latest_date=rows[-1]["trade_date"])
                    if chosen is None or complete_bars(rows)>complete_bars(chosen):
                        chosen=rows
                    if complete_bars(rows)>=250:
                        item["attempts"].append(attempt)
                        break
                except Exception as exc:
                    attempt.update(status="failed",error=(type(exc).__name__+": "+str(exc))[:1600])
                item["attempts"].append(attempt)
        if chosen:
            for row in chosen: c.validate_quote(row,end)
            snap=chosen[0]["snapshot"]
            rawpath=Path(snap["path"])
            raw=gzip.decompress(rawpath.read_bytes())
            if hashlib.sha256(raw).hexdigest()!=snap["sha256"] or len(raw)!=snap["bytes"]:
                raise ValueError("raw response hash/length mismatch")
            snapshot={**snap,"path":str(rawpath.relative_to(root))}
            payload={"symbol":sym,"adjustment":"raw","source":chosen[0]["source"],
                     "snapshot":snapshot,"fields":FIELDS,"rows":compact_rows(chosen)}
            target=root/"normalized"/(sym+".json.gz")
            target.parent.mkdir(parents=True,exist_ok=True)
            with gzip.open(target,"wt",encoding="utf-8") as f:
                json.dump(payload,f,ensure_ascii=False,allow_nan=False,separators=(",",":"))
            item.update(status="history_250_retrieved" if complete_bars(chosen)>=250 else "insufficient_history",
                        bars=len(chosen),ohlcv_bars=sum(all(r.get(k) is not None for k in ("open","high","low","close","volume")) for r in chosen),
                        amount_bars=sum(r.get("amount") is not None for r in chosen),
                        earliest_date=chosen[0]["trade_date"],latest_date=chosen[-1]["trade_date"],
                        provider=chosen[0]["source"],fetched_at=chosen[0]["fetched_at"],
                        normalized_ref=str(target.relative_to(root)),normalized_sha256=digest(target),
                        raw_ref=snapshot["path"],raw_file_sha256=digest(rawpath))
        else:
            item.update(status="no_verified_history",bars=0,ohlcv_bars=0,amount_bars=0)
        results[sym]=item
        checkpoint()
        if len(results)%50==0:
            print(json.dumps({k:summary[k] for k in ("part","requested_symbols","attempted_symbols","history_250_symbols","actual_total_bars")}),flush=True)
    checkpoint()
    files=[{"path":str(p.relative_to(root)),"sha256":digest(p),"bytes":p.stat().st_size}
           for p in sorted(root.rglob("*")) if p.is_file()]
    write_json(root/"manifest.json",{"files":files,"generated_at":c.stamp(),
                                    "data_status":"DATA NOT READY","scope":"raw OHLCV coverage only"})
    print(json.dumps({k:v for k,v in summary.items() if k not in ("results","pending_symbols")}),flush=True)

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed",required=True)
    p.add_argument("--output",required=True)
    p.add_argument("--part",type=int,required=True)
    p.add_argument("--parts",type=int,default=8)
    p.add_argument("--minutes",type=int,default=50)
    a=p.parse_args()
    if not 0<=a.part<a.parts<=16 or not 1<=a.minutes<=100:
        p.error("invalid bounded partition or time budget")
    run(a.seed,a.output,a.part,a.parts,a.minutes)
