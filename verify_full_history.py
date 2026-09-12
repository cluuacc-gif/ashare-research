#!/usr/bin/env python3
"""Replay all real bootstrap responses, summarize coverage, and preserve immutable evidence."""
import argparse
import collections
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys

import collector as c
from full_history_bootstrap import FIELDS, verified_seed
from short_term import digest, write_json

def safe_path(root, ref):
    p=(root/ref).resolve()
    if not p.is_relative_to(root.resolve()) or not p.is_file():
        raise ValueError("missing or unsafe evidence path: "+ref)
    return p

def verified_part(root):
    manifest=json.loads((root/"manifest.json").read_text())
    listed={}
    for f in manifest["files"]:
        p=safe_path(root,f["path"])
        if f["path"] in listed or digest(p)!=f["sha256"] or p.stat().st_size!=f["bytes"]:
            raise ValueError("part manifest hash/length/duplicate mismatch")
        listed[f["path"]]=f
    if "bootstrap_part.json" not in listed:
        raise ValueError("unmanifested partition summary")
    return json.loads((root/"bootstrap_part.json").read_text()),listed

def replay(root,item,end,listed):
    ref=item["normalized_ref"]
    if ref not in listed or item["raw_ref"] not in listed:
        raise ValueError("unmanifested normalized/raw data")
    path=safe_path(root,ref)
    if digest(path)!=item["normalized_sha256"]:
        raise ValueError("normalized digest mismatch")
    payload=json.loads(gzip.decompress(path.read_bytes()))
    if payload["fields"]!=FIELDS or payload["adjustment"]!="raw" or payload["symbol"]!=item["symbol"]:
        raise ValueError("unexpected fields, adjustment or security")
    snap=payload["snapshot"]; rawpath=safe_path(root,item["raw_ref"])
    raw=gzip.decompress(rawpath.read_bytes())
    if digest(rawpath)!=item["raw_file_sha256"] or hashlib.sha256(raw).hexdigest()!=snap["sha256"] or len(raw)!=snap["bytes"]:
        raise ValueError("raw response digest/length mismatch")
    if snap["path"]!=item["raw_ref"] or snap["fetched_at"]!=item["fetched_at"] or c.timestamp(snap["fetched_at"])>c.now():
        raise ValueError("raw provenance/time mismatch")
    rows=[dict(zip(FIELDS,values,strict=True)) for values in payload["rows"]]
    if len(rows)!=item["bars"] or not rows:
        raise ValueError("bar count mismatch")
    dates=[r["trade_date"] for r in rows]
    if dates!=sorted(set(dates)) or dates[0]!=item["earliest_date"] or dates[-1]!=item["latest_date"]:
        raise ValueError("duplicate/out-of-order dates")
    symbol=item["symbol"]; provider=item["provider"]
    if payload["source"]!=provider:
        raise ValueError("provider mismatch")
    low=dates[0]
    if provider=="eastmoney_history":
        obj=json.loads(raw)
        if str(obj["data"]["code"])!=symbol.split(".")[0]:
            raise ValueError("raw security mismatch")
        expected=c.normalize_em_history(symbol,obj,low,end)
    elif provider=="tencent_history":
        class Frozen:
            def json(self,source,url):
                if source!=provider: raise ValueError("unexpected replay source")
                return json.loads(raw),snap
        expected,_=c.tx_history(Frozen(),symbol,low,end)
    elif provider=="sina_history":
        class Frozen:
            def request(self,source,url):
                if source!=provider or url!=snap["url"]: raise ValueError("unexpected replay URL")
                return raw.decode("utf-8"),snap
        expected,_=c.sina_history(Frozen(),symbol,low,end)
    else:
        raise ValueError("unimplemented provider")
    expected={r["trade_date"]:r for r in expected}
    if set(expected)!=set(dates):
        raise ValueError("replayed dates differ")
    complete=0
    for r in rows:
        if any(r.get(f)!=expected[r["trade_date"]].get(f) for f in c.FIELDS):
            raise ValueError("raw response replay mismatch")
        r.update(symbol=symbol,source=provider,fetched_at=snap["fetched_at"],snapshot=snap)
        c.validate_quote(r,end)
        complete+=all(r.get(f) is not None for f in ("open","high","low","close","volume"))
    if complete!=item["ohlcv_bars"]:
        raise ValueError("OHLCV count mismatch")
    return rows

def run(seed,parts,output):
    root=Path(output)
    root.mkdir(parents=True,exist_ok=True)
    seed_report,securities,_=verified_seed(seed)
    end=seed_report["calendar_upper_bound"]
    summaries=[]; checked=[]; failures=[]; seen=set(); all_parts=set()
    provider_counts=collections.Counter(); market=collections.defaultdict(collections.Counter)
    for sf in sorted(Path(parts).rglob("bootstrap_part.json")):
        partroot=sf.parent
        try:
            summary,listed=verified_part(partroot)
            if summary["target_date"]!=end or summary["vendor_universe_count"]!=len(securities) or summary["collector_sha256"]!=digest(Path(c.__file__)):
                raise ValueError("partition date, universe or code version mismatch")
            if summary["part"] in all_parts:
                raise ValueError("duplicate partition")
            all_parts.add(summary["part"]); summaries.append(summary)
        except Exception as exc:
            failures.append({"partition":str(sf),"error":str(exc)}); continue
        for sym,item in summary["results"].items():
            if sym not in securities or sym in seen or sym!=item["symbol"]:
                failures.append({"symbol":sym,"error":"unexpected or duplicate security"}); continue
            seen.add(sym)
            if not item.get("normalized_ref"):
                failures.append({"symbol":sym,"error":"no real historical response","attempts":item["attempts"]}); continue
            try:
                rows=replay(partroot,item,end,listed)
                normalized=safe_path(partroot,item["normalized_ref"])
                entry={**item,"partition":summary["part"],"normalized_local_path":str(normalized.resolve()),
                       "raw_replay_verified":True}
                checked.append(entry)
                ex=sym.split(".")[1]; market[ex]["securities"]+=1
                market[ex]["bars"]+=len(rows)
                market[ex]["history_250"]+=item["ohlcv_bars"]>=250
                market[ex]["latest_250"]+=item["ohlcv_bars"]>=250 and item["latest_date"]==end
                provider_counts[item["provider"]]+=1
            except Exception as exc:
                failures.append({"symbol":sym,"error":type(exc).__name__+": "+str(exc)})
    missing=sorted(set(securities)-seen)
    n250=sum(x["ohlcv_bars"]>=250 for x in checked)
    dated=sum(x["ohlcv_bars"]>=250 and x["latest_date"]==end for x in checked)
    report={"schema_version":"1.0","kind":"full_history_replay_acceptance","target_date":end,
            "generated_at":c.stamp(),"github_run_id":os.environ.get("GITHUB_RUN_ID"),
            "commit_sha":os.environ.get("GITHUB_SHA"),"seed_run_id":seed_report.get("github_run_id"),
            "vendor_universe":len(securities),"official_universe":None,
            "attempted_symbols":len(seen),"verified_history_symbols":len(checked),
            "history_250_symbols":n250,"history_250_vendor_coverage":n250/len(securities),
            "history_250_latest_at_target":dated,
            "total_real_bars":sum(x["bars"] for x in checked),
            "amount_available_bars":sum(x["amount_bars"] for x in checked),
            "markets":dict(market),"actual_providers":dict(provider_counts),
            "partitions_received":sorted(all_parts),"pending_symbols":missing,"failures":failures,
            "raw_replay_verified":True,"all_250_vendor_bars_complete":n250==len(securities) and not missing and not failures,
            "bootstrap_complete":False,"data_status":"DATA NOT READY","model_ready":False,
            "production_database_written":False,"unique_handoff_updated":False,
            "limitations":["Vendor census is not a certified official point-in-time universe.",
                "Newly listed, suspended and stale histories are retained without invented bars.",
                "Historical exchange session calendars, corporate actions, statuses and official limit prices are not complete.",
                "Industry, event and intraday execution evidence is not present in daily OHLCV.",
                "Raw-history acceptance does not authorize any formal probability or 80% win-rate claim.",
                "Original market/prediction database identities and original handoff are unchanged by this artifact job."]}
    write_json(root/"history_quality.json",report)
    write_json(root/"verified_history_index.json",{"target_date":end,"items":checked})
    title="# A股全池历史数据验收_"+end
    lines=[title,"","**DATA NOT READY**","",
           "本次仅对真实历史行情执行原始响应重放、哈希和逐字段校验。正式研究数据与模型仍独立准入。","",
           "| 项目 | 实际结果 |","|---|---:|",
           "| 供应商证券主表 | "+str(len(securities))+" |",
           "| 官方完整分母 | 该项数据目前无法验证 |",
           "| 已尝试证券 | "+str(len(seen))+" |",
           "| 原始响应重放通过证券 | "+str(len(checked))+" |",
           "| 至少250根完整真实OHLCV | "+str(n250)+" |",
           "| 至少250根且最新日期为"+end+" | "+str(dated)+" |",
           "| 真实日线总数 | "+str(report["total_real_bars"])+" |",
           "| 尚未尝试 | "+str(len(missing))+" |",
           "| 未通过/无数据 | "+str(len(failures))+" |","",
           "市场明细："+json.dumps(report["markets"],ensure_ascii=False),"",
           "实际来源："+json.dumps(dict(provider_counts),ensure_ascii=False),"",
           "原行情库、预测库与唯一跨日交接文件未由此云端作业写入。",
           "完整历史证券状态、官方限价、板块事件和可成交验证仍缺失，不能据此宣称模型或80%胜率通过。",
           "模型的真实时间顺序探索性结果另见 model_validation.json；若不存在则未完成该计算。",""]
    (root/("A股全池历史验收_"+end+".md")).write_text("\n".join(lines),encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in ("failures","limitations","pending_symbols")},ensure_ascii=False),flush=True)
    return report

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed",required=True);p.add_argument("--parts",required=True);p.add_argument("--output",required=True)
    a=p.parse_args();run(a.seed,a.parts,a.output)
