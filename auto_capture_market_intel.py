#!/usr/bin/env python3
"""Auto-capture market intelligence: status/limits, announcements, industry.

Sources (public, automated):
  - Tencent quotes: name/ST, high/low, limit_up/limit_down, volume/amount
  - Eastmoney notice API: company announcements + risk keywords
  - Sina industry boards: sector membership

Writes into an existing sealed market DB copy. Never claims official exchange
verification (verified=0 / official_verified=0) unless source is sse/szse/bse/cninfo.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import collector as c  # noqa: E402
from sealed_database import inspect_sealed, sha256  # noqa: E402
from short_term import write_json  # noqa: E402

import requests  # noqa: E402

RISK_KEYS = (
    "退市", "风险", "立案", "处罚", "问询", "停牌", "警示", "ST",
    "诉讼", "违规", "减持", "质押", "终止", "亏损", "关注函",
)


def clear_proxy():
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(k, None)


def to_tx(sym: str) -> str:
    code, ex = sym.split(".")
    return ("sh" if ex == "SH" else "sz" if ex == "SZ" else "bj") + code


def parse_tx_line(line: str):
    if "=" not in line or "~" not in line:
        return None
    body = line.split("=", 1)[1].strip().strip('"')
    parts = body.split("~")
    if len(parts) < 50:
        return None
    try:
        name = parts[1]
        code = parts[2]
        price = float(parts[3]) if parts[3] else None
        prev = float(parts[4]) if parts[4] else None
        # index heuristics from known Tencent layout
        high = float(parts[33]) if parts[33] else None
        low = float(parts[34]) if parts[34] else None
        # later fields: limit up / limit down around 47/48
        up = down = None
        for idx in (47, 46, 48, 45, 44):
            if idx < len(parts) and parts[idx]:
                try:
                    v = float(parts[idx])
                except ValueError:
                    continue
                if prev and 0.5 * prev < v < 1.6 * prev:
                    if up is None:
                        up = v
                    elif down is None and v < prev:
                        down = v
        # fallback compute from prev
        if prev and up is None:
            ratio = 0.20 if code.startswith(("300", "301", "688", "689")) else 0.30 if code.startswith(("4", "8", "92")) else 0.10
            up = round(prev * (1 + ratio), 2)
            down = round(prev * (1 - ratio), 2)
        is_st = 1 if ("ST" in name.upper()) else 0
        traded = price is not None and price > 0
        status = "traded" if traded else "unknown"
        return {
            "code": code,
            "name": name,
            "price": price,
            "prev_close": prev,
            "high": high,
            "low": low,
            "limit_up": up,
            "limit_down": down,
            "is_st": is_st,
            "trading_status": status,
        }
    except Exception:
        return None


def fetch_tx_quotes(sess, symbols, batch=60):
    out = {}
    for i in range(0, len(symbols), batch):
        chunk = symbols[i : i + batch]
        codes = ",".join(to_tx(s) for s in chunk)
        try:
            r = sess.get("https://qt.gtimg.cn/q=" + codes, timeout=30)
            for line in r.text.splitlines():
                row = parse_tx_line(line)
                if not row:
                    continue
                # map back to symbol
                code = row["code"]
                for s in chunk:
                    if s.startswith(code + "."):
                        out[s] = row
                        break
        except Exception as e:
            print(f"tx batch {i}: {type(e).__name__}: {e}", flush=True)
        time.sleep(0.15)
    return out


def fetch_em_notices(sess, symbols, max_per_symbol=15):
    events = []
    for i, sym in enumerate(symbols):
        code = sym.split(".")[0]
        try:
            r = sess.get(
                "https://np-anotice-stock.eastmoney.com/api/security/ann",
                params={
                    "sr": -1,
                    "page_size": max_per_symbol,
                    "page_index": 1,
                    "ann_type": "A",
                    "client_source": "web",
                    "stock_list": code,
                },
                timeout=25,
            )
            data = (r.json() or {}).get("data") or {}
            for a in data.get("list") or []:
                title = (a.get("title") or a.get("title_ch") or "")[:200]
                if not title:
                    continue
                risk = any(k in title for k in RISK_KEYS)
                art = a.get("art_code") or ""
                display = a.get("display_time") or a.get("notice_date") or a.get("sort_date")
                published = None
                if display:
                    published = str(display)[:19].replace(" ", "T") + "+08:00"
                events.append(
                    {
                        "event_id": f"em-{art}" if art else None,
                        "symbols": [sym],
                        "title": title,
                        "event_type": "risk_flagged" if risk else "company_announcement",
                        "fact_status": "company_announcement",
                        "published_at": published,
                        "source_url": "https://data.eastmoney.com/notices/",
                    }
                )
        except Exception as e:
            if i < 3:
                print(f"em {sym}: {type(e).__name__}: {e}", flush=True)
        if i % 50 == 0:
            print(f"notices {i}/{len(symbols)} events={len(events)}", flush=True)
        time.sleep(0.12)
    return events


def fetch_sina_industry(sess):
    url = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
    r = sess.get(url, timeout=30)
    r.raise_for_status()
    m = re.search(r"S_Finance_bankuai_sinaindustry\s*=\s*(\{.*\})", r.text, re.S)
    industries = {}
    for key, val in re.findall(r'"([^"]+)":"([^"]+)"', m.group(1)):
        parts = val.split(",")
        if len(parts) >= 2:
            industries[key] = parts[1]
    members = []
    for key, name in industries.items():
        page = 1
        while page <= 15:
            u = (
                "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                "Market_Center.getHQNodeData?num=100&sort=symbol&asc=1&page=%d&node=%s"
                % (page, key)
            )
            try:
                rr = sess.get(u, timeout=30)
                rows = json.loads(rr.text or "[]")
            except Exception:
                break
            if not rows:
                break
            for row in rows:
                code = row.get("symbol") or ""
                try:
                    # sina codes look like sh600176 / sz000001 / bj920001
                    sym = c.symbol(code)
                except Exception:
                    continue
                members.append(
                    {
                        "symbol": sym,
                        "classification": "sina_industry",
                        "industry": name,
                        "effective_date": "2026-09-25",
                        "source_url": u,
                    }
                )
            if len(rows) < 100:
                break
            page += 1
            time.sleep(0.1)
    return industries, members


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--expected-db-sha256", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--max-symbols", type=int, default=0, help="0=all")
    p.add_argument("--skip-notices", action="store_true")
    p.add_argument("--skip-industry", action="store_true")
    args = p.parse_args()
    clear_proxy()

    src = Path(args.db).resolve()
    if sha256(src) != args.expected_db_sha256:
        raise SystemExit("db sha mismatch")
    before = inspect_sealed(src)
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    working = out / src.name
    shutil.copy2(src, working)

    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0 (compatible; ashare-intel/0.2)"

    db = sqlite3.connect(working.as_uri() + "?mode=ro", uri=True)
    symbols = [r[0] for r in db.execute("SELECT symbol FROM security_master ORDER BY symbol")]
    db.close()
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]
    print(f"symbols={len(symbols)}", flush=True)

    print("tencent quotes...", flush=True)
    quotes = fetch_tx_quotes(sess, symbols)
    print(f"quotes={len(quotes)}", flush=True)

    print("sina industry...", flush=True)
    industries, members = ([], [])
    if not args.skip_industry:
        try:
            industries, members = fetch_sina_industry(sess)
        except Exception as e:
            print(f"industry fail {e}", flush=True)
    print(f"industries={len(industries)} members={len(members)}", flush=True)

    events = []
    if not args.skip_notices:
        print("eastmoney notices...", flush=True)
        # prioritize price-band 5-10 and ST names for risk, then rest up to cap
        band, others = [], []
        db = sqlite3.connect(working.as_uri() + "?mode=ro", uri=True)
        band_rows = {
            r[0]
            for r in db.execute(
                "SELECT symbol FROM daily_quotes WHERE trade_date=(SELECT MAX(trade_date) FROM daily_quotes) AND close BETWEEN 5 AND 10"
            )
        }
        db.close()
        for s in symbols:
            (band if s in band_rows else others).append(s)
        ordered = band + others
        # notice cap keeps runtime bounded
        events = fetch_em_notices(sess, ordered[:800], max_per_symbol=10)
        print(f"events={len(events)}", flush=True)

    wdb = c.Database(working)
    imported = {"status": 0, "limits": 0, "industry": 0, "news": 0, "errors": []}

    with wdb.db:
        now = c.stamp()
        for sym, q in quotes.items():
            day = c.now().date().isoformat()
            wdb.db.execute(
                "INSERT OR REPLACE INTO security_status_daily VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    sym,
                    day,
                    now,
                    int(q["is_st"]),
                    0,
                    q["trading_status"],
                    0,
                    "https://qt.gtimg.cn/",
                    now,
                    json.dumps({"source": "tencent_qt", **{k: q[k] for k in ("name", "price", "prev_close", "limit_up", "limit_down", "trading_status", "is_st")}}, ensure_ascii=False),
                ),
            )
            imported["status"] += 1
            if q.get("limit_up") and q.get("limit_down") and q["limit_up"] > q["limit_down"] > 0:
                wdb.put_limits(
                    sym,
                    day,
                    q["limit_up"],
                    q["limit_down"],
                    True,
                    False,
                    "https://qt.gtimg.cn/",
                    {"source": "tencent_qt_quote_limits", **q},
                    commit=False,
                )
                imported["limits"] += 1

    if members:
        try:
            res = c.import_evidence(wdb, {"kind": "industry_members", "records": members})
            imported["industry"] = res["imported"]
        except Exception as e:
            imported["errors"].append(f"industry:{type(e).__name__}:{e}")

    news_records = []
    for e in events:
        rec = {
            "symbols": e["symbols"],
            "title": e["title"],
            "event_type": e["event_type"],
            "fact_status": e["fact_status"],
            "published_at": e.get("published_at"),
            "source_url": e["source_url"],
        }
        if e.get("event_id"):
            rec["event_id"] = e["event_id"]
        news_records.append(rec)
    if news_records:
        try:
            res = c.import_evidence(wdb, {"kind": "news_events", "records": news_records})
            imported["news"] = res["imported"]
        except Exception as e:
            imported["errors"].append(f"news:{type(e).__name__}:{e}")

    with wdb.db:
        wdb.db.execute(
            "INSERT INTO source_log(run_id,source,operation,fetched_at,status,url,error,snapshot_ref,sha256,bytes) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "auto-intel-20260925",
                "tencent+eastmoney+sina",
                "auto_market_intel_collect",
                c.stamp(),
                "success" if not imported["errors"] else "partial",
                None,
                json.dumps(imported["errors"], ensure_ascii=False)[:400],
                None,
                None,
                None,
            ),
        )
    counts = {
        "status_rows": wdb.db.execute("SELECT COUNT(*) FROM security_status_daily").fetchone()[0],
        "status_today": wdb.db.execute(
            "SELECT COUNT(*) FROM security_status_daily WHERE trade_date=?",
            (c.now().date().isoformat(),),
        ).fetchone()[0],
        "limit_rows": wdb.db.execute("SELECT COUNT(*) FROM limit_records").fetchone()[0],
        "limit_today": wdb.db.execute(
            "SELECT COUNT(*) FROM limit_records WHERE trade_date=?",
            (c.now().date().isoformat(),),
        ).fetchone()[0],
        "industry_members": wdb.db.execute("SELECT COUNT(*) FROM industry_members").fetchone()[0],
        "news_events": wdb.db.execute("SELECT COUNT(*) FROM news_events").fetchone()[0],
        "official_status_verified": wdb.db.execute("SELECT COUNT(*) FROM security_status_daily WHERE verified=1").fetchone()[0],
        "official_limit_verified": wdb.db.execute("SELECT COUNT(*) FROM limit_records WHERE official_verified=1").fetchone()[0],
    }
    wdb.finish("auto-intel-20260925", "success" if not imported["errors"] else "partial", imported)
    try:
        wdb.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        wdb.db.execute("PRAGMA journal_mode=DELETE")
        wdb.db.commit()
    except Exception:
        pass
    wdb.db.close()
    sealed = inspect_sealed(working)
    report = {
        "kind": "auto_market_intel_collect",
        "generated_at": c.stamp(),
        "symbols": len(symbols),
        "quotes_captured": len(quotes),
        "industries": len(industries),
        "industry_members": len(members),
        "notice_events": len(events),
        "imported": imported,
        "counts_after": counts,
        "fills_receipts_required": False,
        "note": "Vendor/auto capture for research screening. Official exchange verification still requires sse/szse/bse/cninfo evidence packets.",
        "data_status": "DATA NOT READY",
        "working_database": sealed,
    }
    write_json(out / "auto_intel_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
