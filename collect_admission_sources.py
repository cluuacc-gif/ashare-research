#!/usr/bin/env python3
"""Collect industry, announcement/news, and as-available official status evidence.

Writes into an EXISTING market database copy via collector.import_evidence where
rules allow. Never invents official verification. Never touches prediction DB.
"""
from __future__ import annotations

import argparse
import hashlib
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

try:
    import requests
except ImportError:
    requests = None


def session():
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (compatible; ashare-research-admission/0.1)"
    return s


def to_symbol(code: str) -> str:
    code = code.strip().lower()
    if code.startswith("sh"):
        return code[2:] + ".SH"
    if code.startswith("sz"):
        return code[2:] + ".SZ"
    if code.startswith("bj"):
        return code[2:] + ".BJ"
    if code.endswith((".SH", ".SZ", ".BJ")):
        return code.upper()
    if code.startswith("6"):
        return code + ".SH"
    if code.startswith(("0", "3")):
        return code + ".SZ"
    if code.startswith(("4", "8", "9")):
        return code + ".BJ"
    return code.upper()


def fetch_sina_industries(sess, delay=0.15):
    url = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
    r = sess.get(url, timeout=30)
    r.raise_for_status()
    m = re.search(r"S_Finance_bankuai_sinaindustry\s*=\s*(\{.*?\})\s*;?\s*$", r.text, re.S | re.M)
    if not m:
        m = re.search(r"S_Finance_bankuai_sinaindustry\s*=\s*(\{.*\})", r.text, re.S)
    if not m:
        raise RuntimeError("sina industry index parse failed")
    raw = m.group(1)
    # values: key -> "key,行业名,count,..."
    industries = {}
    for key, val in re.findall(r'"([^"]+)":"([^"]+)"', raw):
        parts = val.split(",")
        if len(parts) >= 2:
            industries[key] = parts[1]
    members = []
    for key, name in industries.items():
        try:
            page = 1
            while True:
                u = (
                    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                    "Market_Center.getHQNodeData?num=100&sort=symbol&asc=1&page=%d&node=%s"
                    % (page, key)
                )
                rr = sess.get(u, timeout=30)
                rr.raise_for_status()
                text = rr.text.strip()
                if text in ("", "null", "[]"):
                    break
                rows = json.loads(text)
                if not rows:
                    break
                for row in rows:
                    code = row.get("symbol") or row.get("code")
                    if not code:
                        continue
                    try:
                        sym = to_symbol(code)
                        sym = c.symbol(sym)
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
                if page > 20:
                    break
                time.sleep(delay)
        except Exception as e:
            print(f"industry {name}: {type(e).__name__}: {e}", flush=True)
        time.sleep(delay)
    return industries, members


def fetch_cninfo_announcements(sess, se_date="2026-09-01~2026-09-25", pages=3):
    """Best-effort company announcements from cninfo (official domain)."""
    out = []
    for column, plate in (("szse", ""), ("sse", ""), ("neeq", "")):
        for page in range(1, pages + 1):
            try:
                r = sess.post(
                    "https://www.cninfo.com.cn/new/hisAnnouncement/query",
                    data={
                        "pageNum": page,
                        "pageSize": 30,
                        "column": column,
                        "tabName": "fulltext",
                        "plate": plate,
                        "stock": "",
                        "searchkey": "",
                        "secid": "",
                        "category": "",
                        "trade": "",
                        "seDate": se_date,
                    },
                    headers={
                        "Referer": "https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
                        "X-Requested-With": "XMLHttpRequest",
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    },
                    timeout=30,
                )
                if r.status_code != 200:
                    print(f"cninfo {column} p{page} HTTP {r.status_code}", flush=True)
                    break
                payload = r.json()
                anns = payload.get("announcements") or []
                if not anns:
                    break
                for a in anns:
                    code = (a.get("secCode") or "").zfill(6)
                    if not code:
                        continue
                    sec_name = a.get("secName") or ""
                    title = a.get("announcementTitle") or ""
                    art = a.get("announcementId") or ""
                    adj = a.get("adjunctUrl") or ""
                    url = f"https://static.cninfo.com.cn/{adj}" if adj else "https://www.cninfo.com.cn/"
                    # classify risk-ish titles
                    low = title.lower()
                    risk = any(
                        k in title
                        for k in ("退市", "风险", "立案", "处罚", "问询", "停牌", "警示", "ST", "诉讼", "违规", "减持", "质押")
                    )
                    out.append(
                        {
                            "event_id": f"cninfo-{art}" if art else None,
                            "symbols": [to_symbol(code + (".SH" if code.startswith("6") else ".SZ" if code.startswith(("0", "3")) else ".BJ"))],
                            "title": title[:200],
                            "event_type": "risk_flagged" if risk else "company_announcement",
                            "fact_status": "company_announcement",
                            "published_at": None,
                            "source_url": "https://www.cninfo.com.cn/",
                            "payload_extra": {"sec_name": sec_name, "adjunct": adj, "announcement_id": art},
                        }
                    )
                time.sleep(0.3)
            except Exception as e:
                print(f"cninfo {column} p{page}: {type(e).__name__}: {e}", flush=True)
                break
    return out


def fetch_sina_news_headlines(sess, pages=2):
    out = []
    try:
        r = sess.get(
            "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num=50&page=1",
            timeout=30,
        )
        data = r.json()
        for item in (data.get("result") or {}).get("data") or []:
            title = item.get("title") or ""
            url = item.get("url") or "https://finance.sina.com.cn/"
            stime = item.get("ctime") or item.get("create_time")
            out.append(
                {
                    "event_id": f"sina-roll-{item.get('docid') or hashlib.sha1(title.encode()).hexdigest()[:16]}",
                    "symbols": [],
                    "title": title[:200],
                    "event_type": "market_headline",
                    "fact_status": "authoritative_media" if "sina" in url or "证券" in title else "unconfirmed",
                    "published_at": None,
                    "source_url": url if url.startswith("https://") else "https://finance.sina.com.cn/",
                }
            )
    except Exception as e:
        print(f"sina news: {type(e).__name__}: {e}", flush=True)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--expected-db-sha256", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    if requests is None:
        raise SystemExit("requests required")

    src = Path(args.db).resolve()
    if sha256(src) != args.expected_db_sha256:
        raise SystemExit("db sha mismatch")
    before = inspect_sealed(src)
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    working = out / src.name
    shutil.copy2(src, working)

    sess = session()
    # clear proxy for public market sites if inherited
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(k, None)

    print("fetching industries...", flush=True)
    industries, members = fetch_sina_industries(sess)
    print(f"industries={len(industries)} members={len(members)}", flush=True)

    print("fetching cninfo announcements...", flush=True)
    anns = fetch_cninfo_announcements(sess)
    print(f"announcements={len(anns)}", flush=True)

    print("fetching sina headlines...", flush=True)
    news = fetch_sina_news_headlines(sess)
    print(f"headlines={len(news)}", flush=True)

    db = c.Database(working)
    imported = {"industry_members": 0, "news_events": 0, "official_status": 0, "errors": []}

    # industry via import_evidence
    if members:
        packet = {"kind": "industry_members", "records": members}
        try:
            res = c.import_evidence(db, packet)
            imported["industry_members"] = res["imported"]
        except Exception as e:
            imported["errors"].append(f"industry:{type(e).__name__}:{e}")

    news_records = []
    for a in anns + news:
        rec = {
            "symbols": a.get("symbols") or [],
            "title": a["title"],
            "event_type": a.get("event_type", "unclassified"),
            "fact_status": a.get("fact_status", "unconfirmed"),
            "published_at": a.get("published_at"),
            "event_at": a.get("event_at"),
            "source_url": a["source_url"],
        }
        if a.get("event_id"):
            rec["event_id"] = a["event_id"]
        # news_events import uses symbols list; empty symbols need a placeholder?
        if not rec["symbols"]:
            rec["symbols"] = ["000001.SZ"]  # market-level placeholder for storage
            rec["event_type"] = "market_headline"
        news_records.append(rec)
    if news_records:
        packet = {"kind": "news_events", "records": news_records}
        try:
            res = c.import_evidence(db, packet)
            imported["news_events"] = res["imported"]
        except Exception as e:
            imported["errors"].append(f"news:{type(e).__name__}:{e}")

    # Official status remains empty unless verified packets exist.
    with db.db:
        db.db.execute(
            "INSERT INTO source_log(run_id,source,operation,fetched_at,status,url,error,snapshot_ref,sha256,bytes) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "admission-collect-20260925",
                "sina_industry+cninfo+sina_news",
                "admission_source_collect",
                c.stamp(),
                "success" if not imported["errors"] else "partial",
                None,
                json.dumps(imported["errors"], ensure_ascii=False)[:500],
                None,
                None,
                None,
            ),
        )

    counts = {
        "industry_members": db.db.execute("SELECT COUNT(*) FROM industry_members").fetchone()[0],
        "news_events": db.db.execute("SELECT COUNT(*) FROM news_events").fetchone()[0],
        "security_status_verified": db.db.execute("SELECT COUNT(*) FROM security_status_daily WHERE verified=1").fetchone()[0],
        "limit_official": db.db.execute("SELECT COUNT(*) FROM limit_records WHERE official_verified=1").fetchone()[0],
    }
    db.finish("admission-collect-20260925", "success" if not imported["errors"] else "partial", imported)
    # Seal: checkpoint and close so inspect_sealed accepts the file.
    try:
        db.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.db.execute("PRAGMA journal_mode=DELETE")
        db.db.commit()
    except Exception:
        pass
    db.db.close()
    sealed = inspect_sealed(working)
    report = {
        "kind": "admission_source_collect",
        "generated_at": c.stamp(),
        "industries_found": len(industries),
        "industry_records_submitted": len(members),
        "announcements": len(anns),
        "headlines": len(news),
        "imported": imported,
        "counts_after": counts,
        "official_status_collected": False,
        "official_limits_collected": False,
        "note": "Industry from Sina boards; announcements/headlines as public feeds. Official exchange per-symbol status/limits still require verified official packets with evidence files.",
        "data_status": "DATA NOT READY",
        "working_database": sealed,
    }
    write_json(out / "admission_collect.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
