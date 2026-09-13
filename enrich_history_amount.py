#!/usr/bin/env python3
"""Fetch genuinely missing historical amount fields using an independently verified public source."""
import argparse,datetime as dt,gzip,hashlib,json,os,time
from pathlib import Path
import collector as c
from full_history_bootstrap import verified_seed,FIELDS
from probe_history_amount import rows_from_raw
from short_term import write_json,digest

def run(seed,output,part,parts,minutes):
    import akshare
    from akshare.stock.cons import zh_sina_a_stock_hist_url,hk_js_decode
    root=Path(output).resolve()
    if root.exists() and any(root.iterdir()):raise ValueError("immutable output must be empty")
    root.mkdir(parents=True,exist_ok=True)
    report,securities,_=verified_seed(seed);end=report["calendar_upper_bound"]
    start=(dt.date.fromisoformat(end)-dt.timedelta(days=850)).isoformat()
    selected=sorted(securities)[part::parts]
    fetcher=c.Fetcher(root/"raw",retries=1,timeout=15,interval=1.25)
    deadline=time.monotonic()+minutes*60
    summary={"schema_version":"1.0","kind":"real_missing_amount_enrichment","target_date":end,"part":part,"parts":parts,"vendor_universe":len(securities),"started_at":c.stamp(),"akshare_version":akshare.__version__,"decoder_sha256":hashlib.sha256(hk_js_decode.encode()).hexdigest(),"probe_code_sha256":digest(Path(__file__).with_name("probe_history_amount.py")),"github_run_id":os.environ.get("GITHUB_RUN_ID"),"data_status":"DATA NOT READY","production_database_written":False,"results":{}}
    def checkpoint():
        summary["finished_at"]=c.stamp()
        summary["attempted_symbols"]=len(summary["results"])
        summary["history_250_amount_symbols"]=sum(r.get("amount_bars",0)>=250 for r in summary["results"].values())
        summary["total_amount_bars"]=sum(r.get("amount_bars",0) for r in summary["results"].values())
        summary["pending_symbols"]=[s for s in selected if s not in summary["results"]]
        write_json(root/"amount_part.json",summary)
    checkpoint()
    for sym in selected:
        if time.monotonic()>=deadline:break
        if fetcher.failures.get("sina_compressed_raw_history",0)>=5:break
        item={"symbol":sym,"started_at":c.stamp()}
        try:
            url=zh_sina_a_stock_hist_url.format(sym[-2:].lower()+sym[:6])
            text,meta=fetcher.request("sina_compressed_raw_history",url)
            rows=rows_from_raw(text,sym,meta,start,end)
            snap={**meta,"path":str(Path(meta["path"]).relative_to(root))}
            payload={"symbol":sym,"source":"sina_compressed_raw_history","adjustment":"raw","snapshot":snap,"fields":FIELDS,"rows":[[r.get(f) for f in FIELDS] for r in rows]}
            out=root/"normalized"/(sym+".json.gz");out.parent.mkdir(exist_ok=True)
            with gzip.open(out,"wt",encoding="utf-8") as f:json.dump(payload,f,ensure_ascii=False,allow_nan=False,separators=(",",":"))
            item.update(status="retrieved",bars=len(rows),amount_bars=sum(r.get("amount") is not None for r in rows),earliest_date=rows[0]["trade_date"],latest_date=rows[-1]["trade_date"],normalized_ref=str(out.relative_to(root)),normalized_sha256=digest(out),raw_ref=snap["path"],raw_file_sha256=digest(Path(meta["path"])),fetched_at=meta["fetched_at"])
        except Exception as exc:item.update(status="failed",error=f"{type(exc).__name__}: {exc}"[:1500])
        item["finished_at"]=c.stamp();summary["results"][sym]=item
        checkpoint()
        if len(summary["results"])%50==0:print(json.dumps({k:summary[k] for k in ("part","attempted_symbols","history_250_amount_symbols","total_amount_bars")}),flush=True)
    checkpoint()
    files=[{"path":str(p.relative_to(root)),"bytes":p.stat().st_size,"sha256":digest(p)} for p in sorted(root.rglob("*")) if p.is_file()]
    write_json(root/"manifest.json",{"files":files,"generated_at":c.stamp(),"data_status":"DATA NOT READY"})
    print(json.dumps({k:v for k,v in summary.items() if k not in ("results","pending_symbols")}),flush=True)

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed",required=True);p.add_argument("--output",required=True)
    p.add_argument("--part",type=int,required=True);p.add_argument("--parts",type=int,default=8);p.add_argument("--minutes",type=int,default=40)
    a=p.parse_args()
    if not 0<=a.part<a.parts<=8 or not 1<=a.minutes<=45:p.error("invalid bounded partition")
    run(a.seed,a.output,a.part,a.parts,a.minutes)
