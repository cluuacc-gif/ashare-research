#!/usr/bin/env python3
"""Bounded three-market real field probe. No simulated data and no production writes."""
import argparse,datetime as dt,hashlib,json,re,os
from pathlib import Path
import collector as c
from short_term import write_json,digest

def decode_history(text):
    from akshare.stock.cons import hk_js_decode
    import py_mini_racer
    match=re.search(r'=\s*("(?:\\.|[^"\\])*")\s*;',text)
    if not match: raise ValueError("expected Sina compressed history quoted payload")
    payload=json.loads(match.group(1))
    ctx=py_mini_racer.MiniRacer()
    ctx.eval(hk_js_decode)
    data=ctx.call("d",payload)
    if not isinstance(data,list): raise ValueError("decoded history is not a list")
    return data

def rows_from_raw(text,sym,meta,start,end):
    result=[]
    for item in decode_history(text):
        day=c.date_value(str(item.get("date",""))[:10])
        if not start<=day<=end:continue
        q={key:c.number(item.get(key)) for key in ("open","high","low","close","volume","amount")}
        q.update(symbol=sym,trade_date=day,source="sina_compressed_raw_history",fetched_at=meta["fetched_at"],snapshot=meta,finality="historical_provider")
        c.validate_quote(q,end)
        if q["amount"] is not None and q["volume"] and q["amount"]>0:
            mean=q["amount"]/q["volume"]
            if not q["low"]-.011<=mean<=q["high"]+.011:
                raise ValueError("amount/volume price outside daily range; units not verified")
        result.append(q)
    if not result:raise ValueError("no target history")
    if len({r["trade_date"] for r in result})!=len(result):raise ValueError("duplicate trade dates")
    return sorted(result,key=lambda r:r["trade_date"])

def main(output):
    from akshare.stock.cons import zh_sina_a_stock_hist_url
    root=Path(output);root.mkdir(parents=True,exist_ok=False)
    fetcher=c.Fetcher(root/"raw",retries=1,timeout=15,interval=1)
    end="2026-09-11";start="2024-06-01"
    report={"kind":"diagnostic/real_history_amount_source_probe","generated_at":c.stamp(),"target_date":end,"github_run_id":os.environ.get("GITHUB_RUN_ID"),"securities":[],"production_database_written":False,"data_status":"DATA NOT READY","scope":"three interface test securities, not a watchlist"}
    for sym in ("600000.SH","000001.SZ","920000.BJ"):
        item={"symbol":sym,"started_at":c.stamp()}
        try:
            url=zh_sina_a_stock_hist_url.format(sym[-2:].lower()+sym[:6])
            text,meta=fetcher.request("sina_compressed_raw_history",url)
            rows=rows_from_raw(text,sym,meta,start,end)
            replay=rows_from_raw(text,sym,meta,start,end)
            if rows!=replay:raise ValueError("source replay mismatch")
            target=root/(sym+".json")
            write_json(target,rows)
            item.update(status="retrieved",bars=len(rows),amount_bars=sum(r["amount"] is not None for r in rows),first_date=rows[0]["trade_date"],last_date=rows[-1]["trade_date"],source_url=url,raw_sha256=meta["sha256"],normalized_sha256=digest(target),fetched_at=meta["fetched_at"])
        except Exception as exc:item.update(status="failed",error=f"{type(exc).__name__}: {exc}"[:1800])
        item["finished_at"]=c.stamp();report["securities"].append(item)
        write_json(root/"probe.json",report)
        print(json.dumps(item,ensure_ascii=False),flush=True)
    report["finished_at"]=c.stamp()
    report["all_three_250_with_amount"]=all(x.get("amount_bars",0)>=250 and x.get("last_date")==end for x in report["securities"])
    write_json(root/"probe.json",report)

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--output",required=True)
    main(p.parse_args().output)
