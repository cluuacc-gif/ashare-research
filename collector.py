#!/usr/bin/env python3
"""A股行情记录层 v0.2.2（Python 3.10+）。不预测、不交易、不连接预测库。

核心抓取仅用标准库；官方证券主表适配需要可选 akshare。公告为证据导入接口。
所有价格为未复权价格，金额为元，成交量为股，百分比为百分数。
公开接口可能变化。字段校验成功不等于来源真实性或时点状态已获官方确认。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as dt
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo

VERSION = "0.2.2"
TZ = ZoneInfo("Asia/Shanghai")
APP_ID = 0x41534851
OFFICIAL_DOMAINS = ("sse.com.cn", "szse.cn", "bse.cn", "cninfo.com.cn")
EM_SPOT = "https://push2.eastmoney.com/api/qt/clist/get"
EM_HIST = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_SPOT_URLS = (
    EM_SPOT,
    "https://79.push2.eastmoney.com/api/qt/clist/get",
    "https://17.push2.eastmoney.com/api/qt/clist/get",
    "https://5.push2.eastmoney.com/api/qt/clist/get",
)
EM_HIST_URLS = (
    EM_HIST,
    "https://91.push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://7.push2his.eastmoney.com/api/qt/stock/kline/get",
)
TX_SPOT = "https://qt.gtimg.cn/q="
TX_HIST = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TX_CENSUS = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
SINA_KLINE = "https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData"
FIELDS = ("prev_close", "open", "high", "low", "close", "pct_change",
          "volume", "amount", "turnover", "amplitude", "total_market_cap",
          "float_market_cap", "limit_up", "limit_down")
SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS collection_runs(
 run_id TEXT PRIMARY KEY,kind TEXT NOT NULL,target_date TEXT,started_at TEXT NOT NULL,
 finished_at TEXT,status TEXT NOT NULL,details TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_log(
 id INTEGER PRIMARY KEY,run_id TEXT,source TEXT NOT NULL,operation TEXT NOT NULL,
 fetched_at TEXT NOT NULL,status TEXT NOT NULL,url TEXT,error TEXT,
 snapshot_ref TEXT,sha256 TEXT,bytes INTEGER);
CREATE TABLE IF NOT EXISTS security_master(
 symbol TEXT PRIMARY KEY,code TEXT NOT NULL,exchange TEXT NOT NULL,name TEXT,
 list_date TEXT,board TEXT,first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,
 source TEXT NOT NULL,official INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS master_snapshots(
 id INTEGER PRIMARY KEY,exchange TEXT NOT NULL,asof_date TEXT NOT NULL,
 observed_at TEXT NOT NULL,source TEXT NOT NULL,official INTEGER NOT NULL,
 symbols_json TEXT NOT NULL,source_ref TEXT);
CREATE TABLE IF NOT EXISTS security_status_daily(
 symbol TEXT NOT NULL,trade_date TEXT NOT NULL,observed_at TEXT NOT NULL,
 is_st INTEGER,delisting INTEGER,trading_status TEXT NOT NULL,
 verified INTEGER NOT NULL,source_url TEXT,published_at TEXT,payload TEXT NOT NULL,
 PRIMARY KEY(symbol,trade_date,observed_at));
CREATE TABLE IF NOT EXISTS quote_observations(
 id INTEGER PRIMARY KEY,symbol TEXT NOT NULL,trade_date TEXT NOT NULL,source TEXT NOT NULL,
 fetched_at TEXT NOT NULL,source_updated_at TEXT,sha256 TEXT NOT NULL UNIQUE,
 payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS quote_obs_date ON quote_observations(trade_date,symbol);
CREATE TABLE IF NOT EXISTS daily_quotes(
 symbol TEXT NOT NULL,trade_date TEXT NOT NULL,prev_close REAL,open REAL,high REAL,
 low REAL,close REAL,pct_change REAL,volume REAL,amount REAL,turnover REAL,
 amplitude REAL,total_market_cap REAL,float_market_cap REAL,limit_up REAL,limit_down REAL,
 fetched_at TEXT NOT NULL,source_updated_at TEXT,source TEXT NOT NULL,
 finality TEXT NOT NULL,conflict INTEGER NOT NULL DEFAULT 0,provenance TEXT NOT NULL,
 PRIMARY KEY(symbol,trade_date));
CREATE TABLE IF NOT EXISTS limit_records(
 symbol TEXT NOT NULL,trade_date TEXT NOT NULL,observed_at TEXT NOT NULL,
 limit_up REAL,limit_down REAL,applicable INTEGER,official_verified INTEGER NOT NULL,
 touched_up INTEGER,closed_up INTEGER,failed_close INTEGER,touched_down INTEGER,
 closed_down INTEGER,source TEXT NOT NULL,payload TEXT NOT NULL,
 PRIMARY KEY(symbol,trade_date,observed_at));
CREATE TABLE IF NOT EXISTS industry_members(
 symbol TEXT NOT NULL,classification TEXT NOT NULL,industry TEXT NOT NULL,
 effective_date TEXT NOT NULL,observed_at TEXT NOT NULL,source_url TEXT NOT NULL,
 PRIMARY KEY(symbol,classification,industry,effective_date,observed_at));
CREATE TABLE IF NOT EXISTS news_events(
 event_id TEXT PRIMARY KEY,symbols_json TEXT NOT NULL,title TEXT NOT NULL,
 event_type TEXT NOT NULL,fact_status TEXT NOT NULL,published_at TEXT,
 event_at TEXT,retrieved_at TEXT NOT NULL,source_url TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trading_calendar(
 exchange TEXT NOT NULL,trade_date TEXT NOT NULL,is_open INTEGER NOT NULL,
 source_url TEXT NOT NULL,verified_at TEXT NOT NULL,PRIMARY KEY(exchange,trade_date));
CREATE TABLE IF NOT EXISTS backfill_progress(
 symbol TEXT NOT NULL,end_date TEXT NOT NULL,required_bars INTEGER NOT NULL,
 status TEXT NOT NULL,bars INTEGER NOT NULL,last_attempt_at TEXT NOT NULL,error TEXT,
 PRIMARY KEY(symbol,end_date,required_bars));
CREATE TABLE IF NOT EXISTS quality_reports(
 report_id TEXT PRIMARY KEY,target_date TEXT NOT NULL,generated_at TEXT NOT NULL,
 core_data_ready INTEGER NOT NULL,payload TEXT NOT NULL,report_ref TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_imports(
 import_id TEXT PRIMARY KEY,kind TEXT NOT NULL,observed_at TEXT NOT NULL,
 payload TEXT NOT NULL);
"""


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def stamp() -> str:
    return now().isoformat(timespec="microseconds")


def canonical(x) -> str:
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(x) -> str:
    return hashlib.sha256(canonical(x).encode()).hexdigest()


def file_ref(path, base):
    """同项目文件保存相对路径，迁移整个数据目录后引用仍可读。"""
    path=Path(path).resolve()
    try:
        return str(path.relative_to(Path(base).resolve()))
    except ValueError:
        return str(path)


def date_value(x: str) -> str:
    return dt.date.fromisoformat(str(x)[:10]).isoformat()


def timestamp(x: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(x.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("时间必须含时区")
    return value.astimezone(TZ)


def number(x):
    if x in (None, "", "-", "--", "None", "null"):
        return None
    try:
        value = float(str(x).replace(",", ""))
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def symbol(code, exchange=None) -> str:
    value = str(code).strip()
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value):
        value, explicit = value.split(".")
        if exchange is not None and exchange != explicit:
            raise ValueError("证券代码与交易所冲突")
        exchange = explicit
    elif re.fullmatch(r"(sh|sz|bj)\d{6}", value.lower()):
        if exchange is not None and exchange != value[:2].upper():
            raise ValueError("证券代码与交易所冲突")
        exchange, value = value[:2].upper(), value[2:]
    value = value.zfill(6)
    if not re.fullmatch(r"\d{6}", value):
        raise ValueError("无效证券代码")
    inferred = ("SH" if value.startswith(("60", "68")) else
                            "SZ" if value.startswith(("00", "30")) else
                            "BJ" if value.startswith(("4", "8", "92")) else None)
    exchange = exchange or inferred
    if exchange not in ("SH", "SZ", "BJ") or exchange != inferred:
        raise ValueError("不在已支持A股代码范围")
    return value + "." + exchange


def official_url(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in OFFICIAL_DOMAINS)


def board_of(sym: str) -> str:
    code, ex = sym.split(".")
    return "北交所" if ex == "BJ" else "科创板" if code.startswith("68") else "创业板" if code.startswith("30") else "主板"


def validate_quote(q: dict, end_date: str | None = None) -> None:
    if symbol(q["symbol"]) != q["symbol"]:
        raise ValueError("入库代码必须规范化为六位数字.交易所")
    day = date_value(q["trade_date"])
    if day > (end_date or now().date().isoformat()) or day > now().date().isoformat():
        raise ValueError("拒绝未来日期行情")
    prices = [q.get(k) for k in ("open", "high", "low", "close")]
    if any(type(v) not in (int,float) or not math.isfinite(v) or v <= 0 for v in prices):
        raise ValueError("OHLC缺失或非正数，不能写成有效日线")
    o, h, l, c = prices
    if h + 0.000001 < max(o, l, c) or l - 0.000001 > min(o, h, c):
        raise ValueError("OHLC顺序错误")
    for k in FIELDS:
        if q.get(k) is not None and (type(q[k]) not in (int,float) or not math.isfinite(q[k])):
            raise ValueError(k + "必须为有限数值或null")
    for k in ("volume", "amount", "total_market_cap", "float_market_cap", "turnover", "amplitude"):
        if q.get(k) is not None and q[k] < 0:
            raise ValueError(k + "不能为负")
    for k in ("prev_close", "limit_up", "limit_down"):
        if q.get(k) is not None and q[k] <= 0:
            raise ValueError(k + "必须为正数或null")
    if q.get("limit_up") is not None and q.get("limit_down") is not None and q["limit_up"]<=q["limit_down"]:
        raise ValueError("涨跌停价顺序错误")
    if q.get("fetched_at") and timestamp(q["fetched_at"])>now()+dt.timedelta(seconds=1):
        raise ValueError("抓取时间不能在未来")
    if q.get("source_updated_at"):
        updated=timestamp(q["source_updated_at"])
        if updated.date().isoformat() != day:
            raise ValueError("行情时间与目标交易日不符，拒绝沿用旧值")
        if updated>timestamp(q.get("fetched_at") or stamp())+dt.timedelta(seconds=1):
            raise ValueError("供应商更新时间晚于实际抓取时间")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        if "预测数据库" in self.path.name:
            raise ValueError("安全拒绝：此采集器不能打开预测数据库")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        existing = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        app_id = self.db.execute("PRAGMA application_id").fetchone()[0]
        if existing and app_id != APP_ID:
            self.db.close()
            raise ValueError("已有文件不是本采集器行情库；拒绝修改")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute(f"PRAGMA application_id={APP_ID}")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES('schema_version','1')")
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES('prev_close_semantics','provider_reference_not_raw_previous_close')")
        self.db.commit()

    def close(self):
        self.db.commit()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.close()

    def start(self, kind, day, details=None):
        run_id = f"{kind}-{day}-{uuid.uuid4().hex[:10]}"
        with self.db:
            self.db.execute("INSERT INTO collection_runs VALUES(?,?,?,?,?,?,?)",
                            (run_id, kind, day, stamp(), None, "running", canonical(details or {})))
        return run_id

    def finish(self, run_id, status, details):
        with self.db:
            self.db.execute("UPDATE collection_runs SET finished_at=?,status=?,details=? WHERE run_id=?",
                            (stamp(), status, canonical(details), run_id))

    def log(self, run_id, source, operation, status, url=None, error=None, raw=None):
        raw = raw or {}
        with self.db:
            self.db.execute("INSERT INTO source_log(run_id,source,operation,fetched_at,status,url,error,snapshot_ref,sha256,bytes) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (run_id, source, operation, raw.get("fetched_at") or stamp(), status, url, error,
                             file_ref(raw["path"],self.path.parent) if raw.get("path") else None, raw.get("sha256"), raw.get("bytes")))

    def upsert_security(self, sym, name, source, list_date=None, official=False):
        sym = symbol(sym)
        code, ex = sym.split(".")
        with self.db:
            self.db.execute("""INSERT INTO security_master VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol) DO UPDATE SET
            name=CASE WHEN excluded.official>=security_master.official THEN excluded.name ELSE security_master.name END,
            list_date=COALESCE(excluded.list_date,security_master.list_date),
            last_seen_at=excluded.last_seen_at,
            source=CASE WHEN excluded.official>=security_master.official THEN excluded.source ELSE security_master.source END,
            official=MAX(security_master.official,excluded.official)""",
            (sym, code, ex, name, list_date, board_of(sym), stamp(), stamp(), source, int(official)))

    def put_quote(self, q, commit=True):
        validate_quote(q)
        q = dict(q)
        if q.get("snapshot",{}).get("path"):
            q["snapshot"]={**q["snapshot"],"path":file_ref(q["snapshot"]["path"],self.path.parent)}
        q.setdefault("fetched_at", stamp())
        q.setdefault("finality", "historical_provider")
        key = digest(q)
        old = self.db.execute("SELECT * FROM daily_quotes WHERE symbol=? AND trade_date=?",
                              (q["symbol"], q["trade_date"])).fetchone()
        merged = dict(old) if old else {k: None for k in FIELDS}
        provenance = json.loads(old["provenance"]) if old else {}
        conflict = old["conflict"] if old else 0
        source = q["source"]
        for k in FIELDS:
            value = q.get(k)
            if value is None:
                continue
            previous_source = provenance.get(k, {}).get("source")
            if k in ("open", "high", "low", "close") and merged.get(k) is not None and previous_source != source:
                if abs(merged[k] - value) > 0.011:
                    conflict = 1
            # 同源修订可更新；东方财富为主源；备用源只补空值，不用null破坏历史。
            if merged.get(k) is None or previous_source == source or source.startswith("eastmoney"):
                merged[k] = value
                provenance[k] = {"source": source, "fetched_at": q["fetched_at"], "observation_sha256": key,"snapshot":q.get("snapshot")}
        payload = [q["symbol"], q["trade_date"]] + [merged.get(k) for k in FIELDS] + [
            q["fetched_at"], q.get("source_updated_at"), source, q["finality"], conflict, canonical(provenance)]
        with self.db if commit else contextlib.nullcontext():
            self.db.execute("INSERT OR IGNORE INTO quote_observations(symbol,trade_date,source,fetched_at,source_updated_at,sha256,payload) VALUES(?,?,?,?,?,?,?)",
                            (q["symbol"], q["trade_date"], source, q["fetched_at"], q.get("source_updated_at"), key, canonical(q)))
            self.db.execute("INSERT OR REPLACE INTO daily_quotes VALUES(" + ",".join("?" for _ in payload) + ")", payload)
        if merged.get("limit_up") is not None and merged.get("limit_down") is not None:
            limit_source=provenance["limit_up"]["source"]
            self.put_limits(q["symbol"], q["trade_date"], merged["limit_up"], merged["limit_down"], True, False, limit_source,commit=commit)
        official_limits=self.db.execute("SELECT * FROM limit_records WHERE symbol=? AND trade_date=? AND official_verified=1 ORDER BY observed_at DESC LIMIT 1",(q["symbol"],q["trade_date"])).fetchone()
        if official_limits:
            # 限价先入库、行情后到达或修订时，追加标签重算，不修改旧观察。
            self.put_limits(q["symbol"],q["trade_date"],official_limits["limit_up"],official_limits["limit_down"],official_limits["applicable"],True,official_limits["source"],json.loads(official_limits["payload"]),commit=commit)

    def put_limits(self, sym, day, up, down, applicable, official, source, extra=None, commit=True):
        quote = self.db.execute("SELECT high,low,close FROM daily_quotes WHERE symbol=? AND trade_date=?", (sym, day)).fetchone()
        values = [None] * 5
        extra=dict(extra or {})
        if quote and applicable and up is not None and down is not None:
            high, low, close = quote
            extra["quote_limit_conflict"]=high>up+0.011 or low<down-0.011
            if not extra["quote_limit_conflict"]:
                touched, closed = high >= up - 0.005, abs(close-up) < 0.005
                values = [int(touched), int(closed), int(touched and not closed), int(low <= down+0.005), int(abs(close-down)<0.005)]
        with self.db if commit else contextlib.nullcontext():
            self.db.execute("INSERT INTO limit_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (sym, day, stamp(), up, down, None if applicable is None else int(applicable), int(official), *values, source, canonical(extra)))

    def counts(self):
        return {t: self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
                ("security_master", "daily_quotes", "quote_observations", "limit_records", "industry_members", "news_events", "collection_runs")}


class Fetcher:
    def __init__(self, root, retries=2, timeout=15, interval=0.5):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.retries, self.timeout, self.interval = retries, timeout, interval
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.logs = []
        self.failures = {}

    def request(self, source, url, encoding="utf-8"):
        with self.lock:
            if self.failures.get(source,0) >= 5:
                raise RuntimeError(source+"本轮已熔断，保留已有数据；下一次独立运行再试")
        error = None
        for attempt in range(self.retries + 1):
            with self.lock:
                wait = max(0, self.next_request - time.monotonic())
                if wait:
                    time.sleep(wait)
                self.next_request = time.monotonic() + self.interval
            try:
                req = urllib.request.Request(url, headers={"User-Agent": f"AshareResearchCollector/{VERSION}", "Accept": "application/json,text/plain,*/*"})
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    body = response.read(20_000_001)
                if len(body) > 20_000_000:
                    raise ValueError("单次响应超20MB，拒绝异常载荷")
                sha = hashlib.sha256(body).hexdigest()
                path = self.root / (source + "_" + sha + ".raw.gz")
                if not path.exists():
                    with gzip.open(path, "wb") as stream:
                        stream.write(body)
                meta = {"source": source, "url": url, "fetched_at": stamp(), "path": str(path.resolve()), "sha256": sha, "bytes": len(body)}
                with self.lock:
                    self.logs.append(meta)
                    self.failures[source] = 0
                return body.decode(encoding), meta
            except urllib.error.HTTPError as exc:
                error = f"HTTP {exc.code}"
                if exc.code in (401, 403):
                    with self.lock:
                        self.failures[source] = 5
                    break  # 不绕过登录/权限/封禁。
            except (OSError, ValueError) as exc:
                error = f"{type(exc).__name__}: {exc}"
            if attempt < self.retries:
                time.sleep(min(2 ** attempt, 8))
        with self.lock:
            self.failures[source] = self.failures.get(source,0)+1
        raise RuntimeError(f"{source}请求失败：{error}")

    def json(self, source, url):
        body, meta = self.request(source, url)
        return json.loads(body), meta


def url_with(base, params):
    return base + "?" + urllib.parse.urlencode(params)


def normalize_em_spot(row, target_date):
    sym = symbol(row.get("f12"))
    ts = number(row.get("f124"))
    if not ts:
        raise ValueError("快照缺少供应商更新时间f124，不推定为今天")
    updated = dt.datetime.fromtimestamp(ts, TZ)
    if updated.date().isoformat() != target_date or updated.time() < dt.time(15):
        raise ValueError("快照不是目标日收盘后数据")
    mapping = {"prev_close":"f18","open":"f17","high":"f15","low":"f16","close":"f2",
               "pct_change":"f3","volume":"f5","amount":"f6","turnover":"f8","amplitude":"f7",
               "total_market_cap":"f20","float_market_cap":"f21"}
    q = {k: number(row.get(v)) for k, v in mapping.items()}
    if q["volume"] is not None:
        q["volume"] *= 100  # 东方财富手 -> 股。
    q.update(symbol=sym, trade_date=target_date, source="eastmoney_spot", source_updated_at=updated.isoformat(),
             fetched_at=stamp(), finality="post_close_provider", name=str(row.get("f14", "")))
    if row.get("_snapshot"):
        q.update(snapshot=row["_snapshot"],fetched_at=row["_snapshot"]["fetched_at"])
    validate_quote(q, target_date)
    return q


def em_spot(fetcher, target_date):
    attempts=[]
    last=([],[],{"provider_total":None,"pagination_complete":False,"issues":["尚未请求"]})
    for base_index, base in enumerate(EM_SPOT_URLS):
        rows, metas, expected, seen, issues = [], [], None, set(), []
        source = f"eastmoney_spot_host_{base_index}"
        for page in range(1, 201):
            params = {"pn":page,"pz":200,"po":1,"np":1,"fltt":2,"invt":2,"fid":"f12",
                      "fs":"m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                      "fields":"f2,f3,f5,f6,f7,f8,f12,f14,f15,f16,f17,f18,f20,f21,f124"}
            try:
                data, meta = fetcher.json(source, url_with(base, params))
            except Exception as exc:
                issues.append(str(exc)); break
            metas.append(meta)
            block = data.get("data") or {}
            if "total" not in block or "diff" not in block:
                issues.append("东方财富快照结构变化：缺total/diff"); break
            if expected is None:
                expected = int(block["total"])
            elif expected != int(block["total"]):
                issues.append("分页期间证券数量变化"); break
            batch = block["diff"]
            batch = list(batch.values()) if isinstance(batch, dict) else batch
            if not batch:
                break
            for raw in batch:
                code = str(raw.get("f12"))
                if code in seen:
                    issues.append("快照分页重复"); continue
                seen.add(code)
                rows.append({**raw,"_snapshot":meta})
            if len(rows) >= expected:
                break
        if expected is None or len(rows) != expected:
            issues.append(f"快照分页不完整：{len(rows)}/{expected}")
        pagination={"provider_total":expected,"pagination_complete":not issues,
                    "issues":issues,"endpoint":base,"endpoint_attempts":attempts+[base]}
        last=(rows,metas,pagination)
        if pagination["pagination_complete"]:
            return last
        attempts.append(base)
    return last


def normalize_em_history(sym, data, start, end):
    block = data.get("data")
    if not block or not isinstance(block.get("klines"), list):
        raise ValueError("东方财富历史结构变化或没有数据")
    result = []
    for line in block["klines"]:
        values = line.split(",")
        if len(values) < 11:
            raise ValueError("历史日线字段不足")
        day = date_value(values[0])
        if not start <= day <= end:
            continue
        q = dict(zip(("open","close","high","low","volume","amount","amplitude","pct_change","change","turnover"), map(number, values[1:11])))
        if q["volume"] is not None:
            q["volume"] *= 100
        # close-change 是供应商涨跌幅参考价；不冒充上一交易日原始收盘。
        q["prev_close"] = round(q["close"]-q["change"], 4) if q["close"] is not None and q["change"] is not None else None
        q.update(symbol=sym, trade_date=day, source="eastmoney_history", fetched_at=stamp(),
                 finality="historical_provider", prev_close_semantics="provider_reference")
        validate_quote(q, end)
        result.append(q)
    return result


def em_history(fetcher, sym, start, end):
    code, ex = sym.split(".")
    params = {"secid":("1" if ex=="SH" else "0")+"."+code,"klt":101,"fqt":0,
              "beg":start.replace("-", ""),"end":end.replace("-", ""),"lmt":10000,
              "fields1":"f1,f2,f3,f4,f5,f6","fields2":"f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"}
    errors=[]
    for base_index, base in enumerate(EM_HIST_URLS):
        try:
            data, meta = fetcher.json(f"eastmoney_history_host_{base_index}", url_with(base, params))
            rows=normalize_em_history(sym, data, start, end)
            for row in rows: row.update(snapshot=meta,fetched_at=meta["fetched_at"])
            return rows, meta
        except Exception as exc:
            errors.append(f"{base}: {type(exc).__name__}: {exc}")
    raise RuntimeError("东方财富历史全部公开入口失败："+" | ".join(errors))


def normalize_tx_spot(text, target_date, errors=None):
    out = []
    for key, body in re.findall(r'v_((?:sh|sz|bj)\d{6})="([^"]*)"', text):
        cells = body.split("~")
        if len(cells) < 49 or not re.fullmatch(r"\d{14}", cells[30]):
            continue
        updated = dt.datetime.strptime(cells[30], "%Y%m%d%H%M%S").replace(tzinfo=TZ)
        if updated.date().isoformat() != target_date or updated.time() < dt.time(15):
            continue
        indexes = {"close":3,"prev_close":4,"open":5,"volume":36,"amount":37,"pct_change":32,
                   "high":33,"low":34,"turnover":38,"amplitude":43,"float_market_cap":44,
                   "total_market_cap":45,"limit_up":47,"limit_down":48}
        q = {k:number(cells[i]) for k,i in indexes.items()}
        for k, factor in (("volume",100),("amount",10000),("float_market_cap",100000000),("total_market_cap",100000000)):
            if q[k] is not None:
                q[k] *= factor
        for k in ("limit_up", "limit_down"):
            if q[k] is not None and q[k] <= 0:
                q[k] = None
        q.update(symbol=symbol(key), name=cells[1], trade_date=target_date, source="tencent_spot",
                 source_updated_at=updated.isoformat(), fetched_at=stamp(), finality="post_close_provider")
        try:
            validate_quote(q, target_date)
            out.append(q)
        except ValueError as exc:
            if errors is not None:
                errors.append({"symbol":q["symbol"],"error":str(exc)})
    return out


def tx_spot(fetcher, symbols, target_date):
    result, metas = [], []
    for offset in range(0, len(symbols), 50):
        keys = [s[-2:].lower()+s[:6] for s in symbols[offset:offset+50]]
        text, meta = fetcher.request("tencent_spot", TX_SPOT+",".join(keys), encoding="gb18030")
        errors=[]
        rows=normalize_tx_spot(text, target_date,errors)
        for row in rows: row.update(snapshot=meta,fetched_at=meta["fetched_at"])
        result.extend(rows)
        if errors: meta["rejected_rows"]=errors
        metas.append(meta)
    return result, metas


def tx_market_census(fetcher):
    """Tencent's paginated current A-share board, retained as raw evidence."""
    rows, metas, expected, seen, issues = [], [], None, set(), []
    page_size = 200
    for offset in range(0, 20000, page_size):
        params = {"_appver":"11.17.0","board_code":"aStock","sort_type":"price",
                  "direct":"down","offset":str(offset),"count":str(page_size)}
        try:
            data, meta = fetcher.json("tencent_market_census", url_with(TX_CENSUS, params))
        except Exception as exc:
            issues.append(str(exc)); break
        metas.append(meta)
        block = data.get("data") or {}
        batch = block.get("rank_list")
        if "total" not in block or not isinstance(batch, list):
            issues.append("腾讯市场清单结构变化：缺total/rank_list"); break
        total = int(block["total"])
        if expected is None:
            expected = total
        elif total != expected:
            issues.append("分页期间证券数量变化"); break
        if not batch:
            break
        for raw in batch:
            raw_code = raw.get("code") or raw.get("stock_code") or raw.get("symbol")
            try:
                sym = symbol(raw_code)
            except (ValueError, TypeError):
                continue
            if sym in seen:
                continue
            seen.add(sym)
            rows.append({"symbol":sym,
                         "name":str(raw.get("name") or raw.get("stock_name") or raw.get("sname") or ""),
                         "raw":raw,"_snapshot":meta})
        if len(rows) >= expected:
            break
    if expected is None or len(rows) != expected:
        issues.append(f"腾讯市场清单分页不完整：{len(rows)}/{expected}")
    return rows, metas, {"provider_total":expected,"pagination_complete":not issues,
                         "issues":issues,"endpoint":TX_CENSUS}


def tx_history(fetcher, sym, start, end):
    key = sym[-2:].lower()+sym[:6]
    params = {"param":f"{key},day,{start},{end},640,"}
    data, meta = fetcher.json("tencent_history", url_with(TX_HIST, params))
    block = (data.get("data") or {}).get(key) or {}
    # 只接受未复权day，不用qfqday/hfqday补缺。
    if not isinstance(block.get("day"), list):
        raise ValueError("腾讯无未复权day字段；拒绝用复权价替代")
    result = []
    for values in block["day"]:
        if len(values) < 6:
            raise ValueError("腾讯历史字段不足")
        day = date_value(values[0])
        if not start <= day <= end:
            continue
        q = dict(zip(("open","close","high","low","volume"), map(number, values[1:6])))
        if q["volume"] is not None:
            q["volume"] *= 100
        q.update(symbol=sym,trade_date=day,source="tencent_history",fetched_at=meta["fetched_at"],snapshot=meta,finality="historical_provider")
        # 本原始接口备用适配只取稳定OHLCV；不猜成交额/市值/历史ST。
        validate_quote(q,end)
        result.append(q)
    return result, meta


def sina_history(fetcher, sym, start, end):
    """Unadjusted 240-minute bars used only as a third independent history route."""
    key = sym[-2:].lower()+sym[:6]
    params = {"symbol":key,"scale":"240","ma":"no","datalen":"1970"}
    text, meta = fetcher.request("sina_history", url_with(SINA_KLINE, params))
    match = re.search(r"=\((.*)\);?\s*$", text, re.S)
    if not match:
        raise ValueError("新浪K线JSONP结构变化")
    data = json.loads(match.group(1))
    if not isinstance(data, list):
        raise ValueError("新浪K线不是列表")
    result=[]
    for values in data:
        day = date_value(str(values.get("day") or values.get("date") or "")[:10])
        if not start <= day <= end:
            continue
        q={k:number(values.get(k)) for k in ("open","high","low","close","volume","amount")}
        q.update(symbol=sym,trade_date=day,source="sina_history",fetched_at=meta["fetched_at"],
                 snapshot=meta,finality="historical_provider")
        validate_quote(q,end)
        result.append(q)
    if not result:
        raise ValueError("新浪没有返回目标区间未复权日线")
    return result, meta


def provider_child(operation, kwargs):
    """可选AKShare调用在子进程执行，父进程硬超时；避免请求永久挂起。"""
    import akshare as ak
    function = getattr(ak, operation)
    frame = function(**kwargs)
    print("ASHARE_JSON=" + frame.to_json(orient="records", force_ascii=False, date_format="iso"))


def ak_call(operation, kwargs, timeout=90):
    if operation not in {"stock_info_sh_name_code","stock_info_sz_name_code","stock_info_bj_name_code"}:
        raise ValueError("仅允许已实现的只读AKShare适配")
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "_ak", operation, canonical(kwargs)],
                          text=True,capture_output=True,timeout=timeout,check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1200:] or proc.stdout[-1200:])
    lines = [x for x in proc.stdout.splitlines() if x.startswith("ASHARE_JSON=")]
    if not lines:
        raise ValueError("AKShare未返回预期JSON")
    return json.loads(lines[-1][12:])


def refresh_official_master(db, run_id, root):
    specs = [
      ("SH","stock_info_sh_name_code",{"symbol":"主板A股"},"证券代码","证券简称","上市日期","https://www.sse.com.cn/assortment/stock/list/share/"),
      ("SH","stock_info_sh_name_code",{"symbol":"科创板"},"证券代码","证券简称","上市日期","https://www.sse.com.cn/assortment/stock/list/share/"),
      ("SZ","stock_info_sz_name_code",{"symbol":"A股列表"},"A股代码","A股简称","A股上市日期","https://www.szse.cn/market/product/stock/list/index.html"),
      ("BJ","stock_info_bj_name_code",{},"证券代码","证券简称","上市日期","https://www.bse.cn/nq/listedcompany.html")]
    by_exchange, failures, refs = {}, set(), {}
    for ex, op, kwargs, code_key, name_key, date_key, url in specs:
        try:
            rows = ak_call(op, kwargs)
            if not rows:
                raise ValueError("官方主表为空")
            target = Path(root)/f"official_{ex}_{op}_{digest(rows)}.json.gz"
            target.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(target,"wt",encoding="utf-8") as stream:
                stream.write(canonical(rows))
            refs.setdefault(ex,[]).append(file_ref(target,db.path.parent))
            for row in rows:
                sym = symbol(row[code_key],ex)
                name = str(row[name_key])
                listed = date_value(row[date_key]) if row.get(date_key) else None
                db.upsert_security(sym,name,"official_"+ex,listed,True)
                by_exchange.setdefault(ex,[]).append(sym)
                # 官方名单名称仅用于ST名称标记；不冒充完整风险/停牌核验。
                payload={"name":name,"basis":"official_list_name_only","full_risk_checked":False}
                with db.db:
                    db.db.execute("INSERT OR REPLACE INTO security_status_daily VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (sym,now().date().isoformat(),stamp(),int("ST" in name.upper()),int("退" in name),"unknown",0,url,None,canonical(payload)))
                    if row.get("所属行业"):
                        db.db.execute("INSERT OR IGNORE INTO industry_members VALUES(?,?,?,?,?,?)",
                           (sym,"exchange_industry",str(row["所属行业"]),now().date().isoformat(),stamp(),url))
            db.log(run_id,"official_"+ex,op,"success",url,raw={"path":str(target.resolve()),"sha256":digest(rows),"bytes":len(canonical(rows).encode())})
        except Exception as exc:
            failures.add(ex)
            db.log(run_id,"official_"+ex,op,"failed",url,error=str(exc)[:1500])
    for ex, members in by_exchange.items():
        if ex in failures:
            continue  # 上海主板/科创板任一失败，不宣称上海主表完整。
        with db.db:
            db.db.execute("INSERT INTO master_snapshots(exchange,asof_date,observed_at,source,official,symbols_json,source_ref) VALUES(?,?,?,?,?,?,?)",
                (ex,now().date().isoformat(),stamp(),"official_"+ex,1,canonical(sorted(set(members))),canonical(refs[ex])))
    return {"status":"partial" if failures else "completed","exchanges_failed":sorted(failures),"observed_securities_by_exchange":{ex:len(set(members)) for ex,members in by_exchange.items()},"note":"当前证券主表不是历史时点主表；官方名单名称不是完整停牌和风险验证"}


def log_metas(db, run_id, metas, operation):
    for meta in metas:
        db.log(run_id,meta["source"],operation,"success",meta["url"],raw=meta)
        if meta.get("rejected_rows"):
            db.log(run_id,meta["source"],operation,"partial",meta["url"],error=canonical(meta["rejected_rows"]),raw=meta)


def collect(db, day, fetcher, official=True):
    if day != now().date().isoformat() or now().time() < dt.time(15,10):
        raise ValueError("collect仅接受北京时间当天15:10以后；历史日期请用backfill")
    run_id = db.start("collect",day)
    calendar={r["exchange"]:r["is_open"] for r in db.db.execute("SELECT * FROM trading_calendar WHERE trade_date=?",(day,))}
    if set(calendar)!={"SH","SZ","BJ"}:
        details={"status":"blocked","reason":"沪深京目标日官方日历证据不完整；未请求行情，不按工作日推定开市"}
        db.finish(run_id,"blocked",details)
        return details
    if not all(calendar.values()):
        status="holiday" if not any(calendar.values()) else "blocked"
        details={"status":status,"reason":"官方日历休市，跳过" if status=="holiday" else "沪深京开休市状态不一致，须核对"}
        db.finish(run_id,status,details)
        return details
    if official:
        refresh_official_master(db,run_id,fetcher.root)
    accepted, rejected, problems = 0,0,[]
    try:
        raw_rows, metas, pagination = em_spot(fetcher,day)
        log_metas(db,run_id,metas,"spot")
        if not pagination["pagination_complete"]:
            db.log(run_id,"eastmoney_spot","pagination","partial",EM_SPOT,error=canonical(pagination))
        for raw in raw_rows:
            try:
                sym = symbol(raw.get("f12"))
                db.upsert_security(sym,str(raw.get("f14","")),"eastmoney_provisional")
                q = normalize_em_spot(raw,day)
                db.put_quote(q)
                accepted += 1
            except (ValueError,KeyError,TypeError) as exc:
                rejected += 1
                problems.append({"code":str(raw.get("f12")),"reason":str(exc)})
    except Exception as exc:
        db.log(run_id,"eastmoney_spot","spot","failed",EM_SPOT,error=str(exc))
    # 备用源对既有全证券清单抓取，不依赖主源当天成功；同时提供供应商限价交叉证据。
    all_symbols = [r[0] for r in db.db.execute("SELECT symbol FROM security_master ORDER BY symbol")]
    tx_count = 0
    for offset in range(0,len(all_symbols),50):
        try:
            rows,metas=tx_spot(fetcher,all_symbols[offset:offset+50],day)
            log_metas(db,run_id,metas,"spot_backup")
            for q in rows:
                db.put_quote(q)
                tx_count += 1
        except Exception as exc:
            db.log(run_id,"tencent_spot","spot_backup","failed",TX_SPOT,error=str(exc))
    details={"status":"observations_saved" if accepted or tx_count else "blocked","eastmoney_accepted":accepted,"rejected":rejected,"tencent_accepted":tx_count,"rejections":problems,
             "master_symbols_seen":len(all_symbols),"note":"快照为供应商盘后数据，不冒充交易所最终数据"}
    db.finish(run_id,"observations_saved" if accepted or tx_count else "blocked",details)
    return details


def backfill(db, end, fetcher, bars=250, workers=2, max_symbols=None):
    if end > now().date().isoformat() or (end==now().date().isoformat() and now().time()<dt.time(15,10)):
        raise ValueError("禁止回填未来或尚未收盘的交易日")
    members=[r[0] for r in db.db.execute("SELECT symbol FROM security_master ORDER BY symbol")]
    run_id=db.start("backfill",end,{"bars":bars,"current_master_only":True,"survivorship_bias_unresolved":True})
    if not members:
        db.log(run_id,"security_master","backfill_admission","blocked",error="证券主表为空，未发送历史请求")
        db.finish(run_id,"blocked",{"reason":"证券主表为空，无法启动全池历史回填；没有写入模拟证券"})
        return {"status":"blocked","reason":"证券主表为空","completed":0,"failed":0}
    if max_symbols:
        members=members[:max_symbols]
    jobs=[]
    broad_start=(dt.date.fromisoformat(end)-dt.timedelta(days=max(800,int(bars*2.8)))).isoformat()
    for sym in members:
        prior=db.db.execute("SELECT status FROM backfill_progress WHERE symbol=? AND end_date=? AND required_bars=?",(sym,end,bars)).fetchone()
        existing=db.db.execute("SELECT COUNT(*) n,MAX(trade_date) last_day,SUM(amount IS NOT NULL AND volume IS NOT NULL AND conflict=0) complete_rows FROM daily_quotes WHERE symbol=? AND trade_date BETWEEN ? AND ?",(sym,broad_start,end)).fetchone()
        if prior and prior[0]=="complete" and (existing["complete_rows"] or 0)>=bars and existing["last_day"]==end:
            continue
        # 已有完整底座时只重查近期，覆盖供应方修订；不足则扩大回填窗口。
        start=max(broad_start,(dt.date.fromisoformat(existing["last_day"])-dt.timedelta(days=20)).isoformat()) if (existing["complete_rows"] or 0)>=bars else broad_start
        jobs.append((sym,start))
    def work(job):
        sym,start=job
        errors=[]
        collected=[]; metas=[]
        for provider_name,provider in (("eastmoney_history",em_history),("tencent_history",tx_history)):
            try:
                rows,meta=provider(fetcher,sym,start,end)
                if not rows:
                    raise ValueError("来源没有返回目标区间日线")
                collected.extend(rows); metas.append(meta)
                # 主源不足时仍尝试备用，不因返回1行就当成成功回填。
                needed=bars if start==broad_start else 1
                if len({r["trade_date"] for r in rows})>=needed and max(r["trade_date"] for r in rows)==end:
                    break
            except Exception as exc:
                errors.append({"source":provider_name,"error":str(exc)})
        return sym,collected,metas,errors
    completed=failed=0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        # 有界队列：不同时保存5000只的全部历史响应。
        pending={}; remaining=iter(jobs)
        def fill():
            while len(pending)<workers*2:
                job=next(remaining,None)
                if job is None: break
                pending[pool.submit(work,job)]=job
        fill()
        while pending:
            done,_=concurrent.futures.wait(pending,return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                pending.pop(future)
                sym,rows,metas,errors=future.result()
                for err in errors:
                    db.log(run_id,err["source"],"history","failed",error=err["error"])
                log_metas(db,run_id,metas,"history")
                with db.db:
                    for q in rows:
                        db.put_quote(q,commit=False)
                result=db.db.execute("SELECT COUNT(*) n,MAX(trade_date) last_day,SUM(amount IS NOT NULL AND volume IS NOT NULL AND conflict=0) complete_rows FROM daily_quotes WHERE symbol=? AND trade_date BETWEEN ? AND ?",(sym,broad_start,end)).fetchone()
                count=result["n"]; full=result["complete_rows"] or 0
                status="complete" if full>=bars and result["last_day"]==end else "insufficient" if rows else "failed"
                with db.db:
                    db.db.execute("INSERT OR REPLACE INTO backfill_progress VALUES(?,?,?,?,?,?,?)",(sym,end,bars,status,count,stamp(),canonical(errors)))
                completed += int(status=="complete")
                failed += int(status!="complete")
                print(canonical({"symbol":sym,"bars":count,"bars_with_volume_amount":full,"status":status}),flush=True)
            fill()
    result={"status":"completed" if not failed else "partial","requested":len(members),"jobs":len(jobs),
            "completed":completed,"skipped_previously_complete":len(members)-len(jobs),"failed_or_insufficient":failed,"current_master_only":True,"limited_run":max_symbols is not None,
            "warning":"当前存续证券回填不能消除幸存者偏差；历史ST/退市状态仍需时点证据"}
    db.finish(run_id,result["status"],result)
    return result


def import_evidence(db, packet, evidence_base=None):
    """真实证据导入接口；只校验结构/域名/时点，不认证网页内容真实性。"""
    kind=packet["kind"]
    records=packet["records"]
    if kind not in ("official_status","calendar","news_events","industry_members"):
        raise ValueError("不支持的证据类型")
    if not isinstance(records,list):
        raise ValueError("records须为数组")
    evidence_base=Path(evidence_base or ".").resolve()
    verified_files={}
    # 先完整预检，避免后半批失败导致前半批悄悄写入。
    for r in records:
        url=r.get("source_url","")
        if not url.startswith("https://"):
            raise ValueError("缺少HTTPS来源")
        if kind in ("official_status","calendar") and not official_url(url):
            raise ValueError("官方状态/日历只接受交易所或巨潮域名")
        if kind in ("official_status","calendar"):
            if not r.get("published_at") or r.get("content_reviewed") is not True:
                raise ValueError("官方证据须记录发布时间并经内容核验，不能只填一个官方URL")
            evidence=r.get("evidence",{})
            evidence_path=(evidence_base/str(evidence.get("path",""))).resolve()
            if not evidence_path.is_file() or not re.fullmatch(r"[0-9a-f]{64}",str(evidence.get("sha256",""))):
                raise ValueError("官方证据须有原始文件和SHA256")
            h=hashlib.sha256()
            with evidence_path.open("rb") as stream:
                for block in iter(lambda:stream.read(1024*1024),b""): h.update(block)
            if h.hexdigest()!=evidence["sha256"]:
                raise ValueError("证据文件SHA256不符")
            verified_files[evidence["sha256"]]=evidence_path
        if r.get("published_at") and timestamp(r["published_at"])>now():
            raise ValueError("未来发布时间")
        if kind != "calendar":
            for s in r.get("symbols",[r.get("symbol")]):
                symbol(s)
        if kind=="calendar":
            if r["exchange"] not in ("SH","SZ","BJ") or type(r["is_open"]) is not bool:
                raise ValueError("交易日历字段无效")
            date_value(r["trade_date"])
        elif kind=="official_status":
            if date_value(r["trade_date"])>now().date().isoformat():
                raise ValueError("不能导入未来实际交易状态")
            if r.get("trading_status") not in ("traded","suspended","delisting","unknown"):
                raise ValueError("证券交易状态无效")
            if any(type(r.get(k)) is not bool for k in ("is_st","delisting")) or not r.get("published_at"):
                raise ValueError("官方状态缺少布尔状态或披露时间")
            if "limit_applicable" in r:
                app=r["limit_applicable"]; up=number(r.get("limit_up")); down=number(r.get("limit_down"))
                if type(app) is not bool or (app and (up is None or down is None or not up>down>0)):
                    raise ValueError("限价或适用标记错误")
        elif kind=="news_events":
            if not r.get("title") or r.get("fact_status") not in ("company_announcement","official_policy","authoritative_media","unconfirmed"):
                raise ValueError("消息标题或事实等级无效")
        elif kind=="industry_members":
            if not r.get("classification") or not r.get("industry"):
                raise ValueError("行业分类缺失")
            date_value(r["effective_date"])
    archived={}
    for sha,original in verified_files.items():
        target=db.path.parent/"已核实来源快照"/(sha+original.suffix)
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists(): shutil.copyfile(original,target)
        archived[sha]=str(target.relative_to(db.path.parent))
    with db.db:
        db.db.execute("INSERT INTO evidence_imports VALUES(?,?,?,?)",(uuid.uuid4().hex,kind,stamp(),canonical({"packet":packet,"archived_evidence":archived})))
        for r in records:
            url=r["source_url"]
            if kind=="calendar":
                if r["exchange"] not in ("SH","SZ","BJ") or type(r["is_open"]) is not bool:
                    raise ValueError("交易日历字段无效")
                db.db.execute("INSERT OR REPLACE INTO trading_calendar VALUES(?,?,?,?,?)",(r["exchange"],date_value(r["trade_date"]),int(r["is_open"]),url,stamp()))
            elif kind=="official_status":
                sym=symbol(r["symbol"]); day=date_value(r["trade_date"])
                if r.get("trading_status") not in ("traded","suspended","delisting","unknown"):
                    raise ValueError("证券交易状态无效")
                for key in ("is_st","delisting"):
                    if type(r.get(key)) is not bool:
                        raise ValueError("官方状态不得缺失或用字符串代替布尔值")
                if not r.get("published_at"):
                    raise ValueError("官方状态须记录披露时间")
                db.db.execute("INSERT INTO security_status_daily VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sym,day,stamp(),int(r["is_st"]),int(r["delisting"]),r["trading_status"],1,url,r["published_at"],canonical(r)))
                if "limit_applicable" in r:
                    app=r["limit_applicable"]
                    up,down=number(r.get("limit_up")),number(r.get("limit_down"))
                    if type(app) is not bool or (app and (up is None or down is None or not up>down>0)):
                        raise ValueError("限价或适用标记错误")
                    db.put_limits(sym,day,up,down,app,True,url,r,commit=False)
            elif kind=="news_events":
                if r.get("fact_status") not in ("company_announcement","official_policy","authoritative_media","unconfirmed"):
                    raise ValueError("消息事实等级无效")
                event_id=r.get("event_id") or digest(r)
                db.db.execute("INSERT OR IGNORE INTO news_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (event_id,canonical([symbol(s) for s in r["symbols"]]),r["title"],r.get("event_type","unclassified"),r["fact_status"],r.get("published_at"),r.get("event_at"),stamp(),url,canonical(r)))
            elif kind=="industry_members":
                db.db.execute("INSERT OR IGNORE INTO industry_members VALUES(?,?,?,?,?,?)",
                    (symbol(r["symbol"]),r["classification"],r["industry"],date_value(r["effective_date"]),stamp(),url))
            else:
                raise ValueError("不支持的证据类型")
    return {"kind":kind,"imported":len(records),"content_truth_requires_external_review":True}


def quality(db, day, report_dir, bars=250):
    if bars<250:
        raise ValueError("质量准入的历史目标不得低于250交易日")
    master=[]; missing_master=[]
    for ex in ("SH","SZ","BJ"):
        row=db.db.execute("SELECT * FROM master_snapshots WHERE exchange=? AND asof_date=? AND official=1 ORDER BY id DESC LIMIT 1",(ex,day)).fetchone()
        if row:
            master.extend(json.loads(row["symbols_json"]))
        else:
            missing_master.append(ex)
    known=set(master)
    expected=len(known) if not missing_master else None
    quotes={r["symbol"]:dict(r) for r in db.db.execute("SELECT * FROM daily_quotes WHERE trade_date=?",(day,))}
    statuses={}
    for r in db.db.execute("SELECT * FROM security_status_daily WHERE trade_date=? ORDER BY observed_at",(day,)):
        if r["verified"]:
            statuses[r["symbol"]]=dict(r)
    population=known if expected is not None else known|set(quotes)
    traded=[s for s in population if statuses.get(s,{}).get("trading_status")!="suspended"]
    suspended={s for s in population if statuses.get(s,{}).get("trading_status")=="suspended"}
    def ratio(n,d):
        return n/d if d else None
    actual=len(set(quotes)&population) if expected is not None else len(quotes)
    def complete(keys):
        return sum(all(quotes.get(s,{}).get(k) is not None for k in keys) for s in traded)
    eligible_history=[]; immature=[]; listing_unknown=[]; history_eligibility_unknown=[]
    calendars={ex:[r[0] for r in db.db.execute("SELECT trade_date FROM trading_calendar WHERE exchange=? AND is_open=1 AND trade_date<=? ORDER BY trade_date",(ex,day))] for ex in ("SH","SZ","BJ")}
    calendar_complete={}
    for ex,days in calendars.items():
        start=days[-bars] if len(days)>=bars else None
        span=(dt.date.fromisoformat(day)-dt.date.fromisoformat(start)).days+1 if start else 0
        recorded=db.db.execute("SELECT COUNT(*) FROM trading_calendar WHERE exchange=? AND trade_date BETWEEN ? AND ?",(ex,start,day)).fetchone()[0] if start else 0
        calendar_complete[ex]=bool(start and recorded==span and days[-1]==day)
    for s in population:
        row=db.db.execute("SELECT list_date FROM security_master WHERE symbol=?",(s,)).fetchone()
        if not row or not row[0]:
            listing_unknown.append(s)
        else:
            calendar=calendars[s[-2:]]
            listed=row[0]
            if not calendar_complete[s[-2:]]:
                history_eligibility_unknown.append(s)
            elif listed<=calendar[-bars]:
                eligible_history.append(s)
            elif listed>=calendar[0]:
                immature.append(s)
            else:
                history_eligibility_unknown.append(s)
    broad_start=(dt.date.fromisoformat(day)-dt.timedelta(days=max(800,int(bars*2.8)))).isoformat()
    hist_counts={}
    history_gaps={}
    for s in population:
        dates={r[0] for r in db.db.execute("SELECT trade_date FROM daily_quotes WHERE symbol=? AND trade_date BETWEEN ? AND ? AND volume IS NOT NULL AND amount IS NOT NULL AND conflict=0",(s,broad_start,day))}
        hist_counts[s]=len(dates)
        if s in eligible_history:
            suspended_dates=set()
            hist_states={}
            for r in db.db.execute("SELECT trade_date,trading_status FROM security_status_daily WHERE symbol=? AND verified=1 AND trade_date<=? ORDER BY observed_at",(s,day)):
                hist_states[r[0]]=r[1]
            suspended_dates={d for d,state in hist_states.items() if state=="suspended"}
            required=set(calendars[s[-2:]][-bars:])
            history_gaps[s]=sorted(required-dates-suspended_dates)
    history_ok=sum(hist_counts[s]>=bars and not history_gaps[s] for s in eligible_history)
    limits={}
    for r in db.db.execute("SELECT * FROM limit_records WHERE trade_date=? AND official_verified=1 ORDER BY observed_at",(day,)):
        limits[r["symbol"]]=dict(r)
    limits_ok=sum(s in limits and (limits[s]["applicable"]==0 or (limits[s]["applicable"]==1 and limits[s]["limit_up"] is not None and limits[s]["limit_down"] is not None)) for s in traded)
    limit_conflicts=sum(bool(json.loads(r["payload"]).get("quote_limit_conflict")) for r in limits.values())
    metrics={
      "expected_securities":expected,"actual_quotes":actual,"official_suspended":len(suspended),
      "quote_coverage":ratio(len((set(quotes)&population)|suspended),expected) if expected is not None else None,
      "ohlc_complete_rate":ratio(complete(("open","high","low","close")),len(traded)) if expected is not None else None,
      "volume_complete_rate":ratio(complete(("volume",)),len(traded)) if expected is not None else None,
      "amount_complete_rate":ratio(complete(("amount",)),len(traded)) if expected is not None else None,
      "reference_return_complete_rate":ratio(complete(("prev_close","pct_change","turnover","amplitude")),len(traded)) if expected is not None else None,
      "market_cap_complete_rate":ratio(complete(("total_market_cap","float_market_cap")),len(traded)) if expected is not None else None,
      "st_status_verified_rate":ratio(sum(s in statuses and statuses[s]["is_st"] is not None for s in population),len(population)) if expected is not None else None,
      "trading_status_verified_rate":ratio(sum(statuses.get(s,{}).get("trading_status") in ("traded","suspended","delisting") for s in population),len(population)) if expected is not None else None,
      "official_limit_verified_rate":ratio(limits_ok,len(traded)) if expected is not None else None,
      "official_limit_price_conflicts":limit_conflicts,
      "history_250_rate":ratio(history_ok,len(eligible_history)) if expected is not None and not history_eligibility_unknown else None,"history_eligible_denominator":len(eligible_history),
      "history_sufficient_count":history_ok,"history_gap_securities":sum(bool(gaps) for gaps in history_gaps.values()),"listing_date_missing":len(listing_unknown),
      "listed_under_250_trade_days":len(immature),"history_eligibility_unknown":len(history_eligibility_unknown),
      "observed_ge250_bar_count":sum(v>=bars for v in hist_counts.values()),"quote_conflicts":sum(q["conflict"] for q in quotes.values()),
      "dated_quotes":len(quotes),"price_5_to_10_count_observed":sum(5<=q["close"]<=10 for q in quotes.values())}
    checks={k:metrics[k]==1.0 for k in ("quote_coverage","ohlc_complete_rate","volume_complete_rate","amount_complete_rate","reference_return_complete_rate","market_cap_complete_rate","st_status_verified_rate","trading_status_verified_rate","official_limit_verified_rate","history_250_rate")}
    checks["official_master_complete"]=not missing_master and bool(expected)
    checks["listing_dates_complete"]=not listing_unknown and bool(population)
    checks["history_eligibility_verified"]=not history_eligibility_unknown and bool(population)
    checks["no_cross_source_conflicts"]=metrics["quote_conflicts"]==0
    checks["no_official_limit_price_conflicts"]=limit_conflicts==0
    cal=list(db.db.execute("SELECT is_open FROM trading_calendar WHERE trade_date=?",(day,)))
    checks["exchange_calendar_verified"]=len(cal)==3 and all(r[0]==1 for r in cal)
    checks["historical_calendar_complete"]=all(calendar_complete.values())
    core_ready=all(checks.values())
    sources=[dict(r) for r in db.db.execute("SELECT source,MAX(fetched_at) last_recorded_at,MAX(CASE WHEN status='success' THEN fetched_at END) last_success_at,COUNT(*) records,SUM(status='success') successful_records FROM source_log GROUP BY source")]
    failures=[dict(r) for r in db.db.execute("SELECT source,operation,fetched_at,error FROM source_log WHERE status!='success' ORDER BY id DESC LIMIT 20")]
    result={"target_date":day,"generated_at":stamp(),"collector_version":VERSION,"metrics":metrics,"checks":checks,
      "core_data_ready":core_ready,"formal_probability_model_ready":False,"mode":"data_ready/model_pending" if core_ready else "diagnostic/watchlist",
      "missing_official_master_exchanges":missing_master,"sources":sources,"recent_failures":failures,
      "history_required_bars":bars,"history_universe_warning":"历史达标须窗口内至少250根含成交量额、无冲突日线，并覆盖最近250个官方交易日；已核实停牌可解释日期缺口，但不虚造K线。日历含完整自然日开休市记录；日历不足时分母未知。上市不足单列。当前存续名单回填仍有幸存者偏差，不能冒充历史全池回测。",
      "historical_calendar_complete":calendar_complete,"history_gaps":history_gaps,
      "formal_probability_blockers":["概率模型与时间外验证尚未实现","供应商行情不能冒充官方最终OHLC；公司行动与完整风险排查须另行通过","指标、行业、新闻等特征仍须覆盖与时点核验"],
      "missing_symbols":sorted(known-set(quotes)-suspended) if expected is not None else None}
    directory=Path(report_dir); directory.mkdir(parents=True,exist_ok=True)
    report_id=uuid.uuid4().hex
    path=directory/f"A股行情数据质量报告_{day}.md"
    archive=directory/"历次检查"/f"A股行情数据质量报告_{day}_{report_id}.md"
    archive.parent.mkdir(parents=True,exist_ok=True)
    result["report_id"]=report_id
    result["immutable_report_ref"]=file_ref(archive,db.path.parent)
    labels={"expected_securities":"应有证券数量（官方主表）","actual_quotes":"已取得目标日日线数量","official_suspended":"官方核实停牌数","quote_coverage":"行情/已核实停牌覆盖率","ohlc_complete_rate":"OHLC完整率","amount_complete_rate":"成交额完整率","market_cap_complete_rate":"总/流通市值完整率","st_status_verified_rate":"ST及交易状态核实覆盖率","history_250_rate":f"至少{bars}日历史覆盖率","history_eligible_denominator":"已确定历史覆盖率分母","history_sufficient_count":"历史达标证券数","listing_date_missing":"上市日期缺失数","listed_under_250_trade_days":"上市不足250交易日单列数","history_eligibility_unknown":"历史准入分母未验证证券数","observed_ge250_bar_count":"已观察至少250根有效日线证券数","quote_conflicts":"跨源OHLC冲突数","dated_quotes":"目标日有日期证据行情数","price_5_to_10_count_observed":"已观察5～10元数量（非完整池）"}
    labels.update(volume_complete_rate="成交量完整率",reference_return_complete_rate="参考前收/涨跌幅/换手/振幅完整率",trading_status_verified_rate="交易状态核实覆盖率",official_limit_verified_rate="官方限价/无限幅状态覆盖率",official_limit_price_conflicts="官方限价与OHLC冲突数",history_gap_securities="最近250交易日缺口证券数",st_status_verified_rate="ST状态核实覆盖率")
    lines=[f"# A股行情数据质量报告｜{day}","",f"生成时间：{result['generated_at']}（北京时间）",f"采集器版本：{VERSION}","",f"数据准入：{'通过' if core_ready else '未通过'}；运行模式：{result['mode']}","",f"目标日沪深京开市状态：{'已核实开市' if checks['exchange_calendar_verified'] else '该项数据目前无法验证；本报告日期仅为检查目标，不认定已经核实为交易日'}", "", "| 检查项 | 实际结果 |","|---|---:|"]
    for k,v in metrics.items():
        display="该项数据目前无法验证" if v is None else f"{v:.2%}" if k.endswith(("rate","coverage")) else str(v)
        lines.append(f"| {labels[k]} | {display} |")
    lines += ["","## 未通过检查","",*(f"- {k}" for k,v in checks.items() if not v),"","## 数据源与实际抓取时间","",canonical(sources),"","## 最近失败记录","",*(f"- {r['fetched_at']}｜{r['source']}｜{r['operation']}｜{r['error']}" for r in failures),"","## 边界","",result["history_universe_warning"],"","所有缺失保留null；没有生成随机行情、模拟证券或预测。主源失败不删除已有交易日，备用源仅补可验证字段。供应商限价与官方限价分开保存。","","即使数据准入通过，也不代表模型已训练。正式概率仍需时间外验证；本程序不会修改预测数据库。"]
    archive.write_text("\n".join(lines)+"\n",encoding="utf-8")
    path.write_text(archive.read_text(encoding="utf-8"),encoding="utf-8")
    json_path=path.with_suffix(".json")
    json_path.write_text(canonical(result)+"\n",encoding="utf-8")
    archive.with_suffix(".json").write_text(canonical(result)+"\n",encoding="utf-8")
    with db.db:
        db.db.execute("INSERT INTO quality_reports VALUES(?,?,?,?,?,?)",(report_id,day,result["generated_at"],int(core_ready),canonical(result),file_ref(archive,db.path.parent)))
    return result


def validate_watchlist(db, packet):
    """仅验证真实研究输入，不主动编造名字；输出无排名、无概率、不写预测库。"""
    forbidden={"rank","ranking","score","probability","probabilities","p_up","p_ge5","p_touch","p_limit","p1","p2","p3a","p3b","top3","top5","top10","排名","评分","概率"}
    def check_keys(obj):
        if isinstance(obj,dict):
            if forbidden & {str(k).lower() for k in obj}:
                raise ValueError("降级观察禁止排名、总分或概率字段")
            for v in obj.values(): check_keys(v)
        elif isinstance(obj,list):
            for v in obj: check_keys(v)
    check_keys(packet)
    cutoff=timestamp(packet["knowledge_cutoff"])
    base=date_value(packet["base_date"])
    if cutoff>now() or cutoff.time()>dt.time(9) or base>=cutoff.date().isoformat():
        raise ValueError("观察清单截止时间无效")
    items=packet["observations"]
    if not 1<=len(items)<=10:
        raise ValueError("观察数量须1～10，不凑数")
    seen=set()
    for item in items:
        sym=symbol(item["symbol"])
        if sym in seen: raise ValueError("重复证券")
        seen.add(sym)
        q=db.db.execute("SELECT * FROM daily_quotes WHERE symbol=? AND trade_date=?",(sym,base)).fetchone()
        if not q or not 5<=q["close"]<=10 or q["conflict"]:
            raise ValueError("观察证券缺少目标基础日5～10元可核实行情，或存在数据冲突")
        if timestamp(q["fetched_at"])>cutoff:
            raise ValueError("本地行情在截止后才取得，不冒充已存的事前数据")
        states=[dict(r) for r in db.db.execute("SELECT * FROM security_status_daily WHERE symbol=? AND trade_date IN (?,?) AND verified=1 ORDER BY trade_date,observed_at",(sym,base,cutoff.date().isoformat()))]
        states=[r for r in states if r["published_at"] and timestamp(r["published_at"])<=cutoff and timestamp(r["observed_at"])<=cutoff]
        if not states or states[-1]["is_st"] or states[-1]["delisting"] or states[-1]["trading_status"]!="traded":
            raise ValueError("观察证券未通过ST/退市/交易状态核验")
        if len(item.get("reasons",[]))<3 or len(item.get("risks",[]))<3:
            raise ValueError("每只须至少3条理由与3条风险")
        if not item.get("event_ids"):
            raise ValueError("观察证券必须有真实公告/新闻证据")
        for eid in item["event_ids"]:
            e=db.db.execute("SELECT * FROM news_events WHERE event_id=?",(eid,)).fetchone()
            if not e or not e["published_at"] or timestamp(e["published_at"])>cutoff or timestamp(e["retrieved_at"])>cutoff or e["fact_status"]=="unconfirmed" or sym not in json.loads(e["symbols_json"]):
                raise ValueError("事件不满足截止或已核实条件")
    return {"kind":"diagnostic","status":"watchlist","title":"非全市场候选观察（无排名）","base_date":base,
            "knowledge_cutoff":packet["knowledge_cutoff"],"generated_at":stamp(),"formal_score_eligible":False,
            "research_timing":"late_or_retrospective" if now()>=cutoff.replace(hour=9,minute=30,second=0) else "pre_open_diagnostic",
            "observations":sorted(items,key=lambda x:x["symbol"]),"warning":"不声称完整5～10元股票池筛选，不给概率，不计入正式模型成绩"}


def main():
    if len(sys.argv)>1 and sys.argv[1]=="_ak":
        provider_child(sys.argv[2],json.loads(sys.argv[3])); return
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db",default="A股行情数据库.sqlite3")
    p.add_argument("--snapshots",default="行情原始快照")
    p.add_argument("--reports",default="质量报告")
    p.add_argument("--timeout",type=float,default=15)
    p.add_argument("--retries",type=int,default=2)
    p.add_argument("--interval",type=float,default=0.5,help="所有请求最小间隔秒；尊重来源限流")
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("init"); sub.add_parser("status"); sub.add_parser("refresh-master")
    a=sub.add_parser("collect"); a.add_argument("--date",default=now().date().isoformat()); a.add_argument("--skip-official-master",action="store_true")
    a=sub.add_parser("daily"); a.add_argument("--date",default=now().date().isoformat()); a.add_argument("--bars",type=int,default=250); a.add_argument("--workers",type=int,default=2)
    a=sub.add_parser("backfill"); a.add_argument("--end",required=True); a.add_argument("--bars",type=int,default=250); a.add_argument("--workers",type=int,default=2); a.add_argument("--max-symbols",type=int)
    a=sub.add_parser("quality"); a.add_argument("--date",required=True); a.add_argument("--bars",type=int,default=250)
    a=sub.add_parser("import-evidence"); a.add_argument("input_json")
    a=sub.add_parser("watchlist"); a.add_argument("input_json"); a.add_argument("--output",required=True)
    a=sub.add_parser("record-blocker"); a.add_argument("--date",required=True); a.add_argument("--reason",required=True)
    args=p.parse_args()
    if args.timeout<=0 or args.retries<0 or args.retries>5 or args.interval<0.2:
        p.error("timeout须为正，retries为0～5，interval不得小于0.2秒")
    db=None
    exit_code=0
    try:
        if args.command!="init" and not Path(args.db).is_file():
            raise ValueError("行情库不存在；先显式init，避免误建新库")
        db=Database(args.db)
        if args.command in ("init","status"):
            result={"collector_version":VERSION,"database":str(db.path),"counts":db.counts(),"integrity":db.db.execute("PRAGMA integrity_check").fetchone()[0],"writes_prediction_database":False}
        elif args.command=="record-blocker":
            run=db.start("environment_check",date_value(args.date))
            db.log(run,"execution_environment","access_blocker_record","blocked",error=args.reason)
            db.finish(run,"blocked",{"reason":args.reason})
            result=quality(db,date_value(args.date),args.reports)
        elif args.command=="quality":
            result=quality(db,date_value(args.date),args.reports,args.bars)
        elif args.command=="import-evidence":
            result=import_evidence(db,json.loads(Path(args.input_json).read_text(encoding="utf-8")),Path(args.input_json).resolve().parent)
        elif args.command=="watchlist":
            result=validate_watchlist(db,json.loads(Path(args.input_json).read_text(encoding="utf-8")))
            Path(args.output).write_text(canonical(result)+"\n",encoding="utf-8")
        else:
            fetcher=Fetcher(args.snapshots,args.retries,args.timeout,args.interval)
            if args.command=="refresh-master":
                run=db.start("refresh_master",now().date().isoformat())
                result=refresh_official_master(db,run,fetcher.root)
                db.finish(run,result["status"],result)
            elif args.command=="collect":
                result=collect(db,date_value(args.date),fetcher,not args.skip_official_master)
                if result["status"]!="holiday":
                    result["quality"]=quality(db,date_value(args.date),args.reports)
            elif args.command=="daily":
                if not 1<=args.workers<=4 or args.bars<250:
                    raise ValueError("workers须1～4；历史目标不得低于250根")
                day=date_value(args.date)
                snapshot=collect(db,day,fetcher)
                result={"status":snapshot["status"],"snapshot":snapshot}
                if snapshot["status"]!="holiday":
                    # 日历失败停止；盘口缺字段仍回填日线，不让quality退出码阻断修复。
                    if not snapshot.get("reason"):
                        result["backfill"]=backfill(db,day,fetcher,args.bars,args.workers)
                        result["status"]=result["backfill"]["status"]
                    result["quality"]=quality(db,day,args.reports,args.bars)
            else:
                if not 1<=args.workers<=4 or args.bars<250 or (args.max_symbols is not None and args.max_symbols<1):
                    raise ValueError("workers须1～4；本项目历史目标不得低于250根")
                result=backfill(db,date_value(args.end),fetcher,args.bars,args.workers,args.max_symbols)
                result["quality"]=quality(db,date_value(args.end),args.reports,args.bars)
        print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
        if result.get("status") in ("blocked","partial") or result.get("core_data_ready") is False or result.get("quality",{}).get("core_data_ready") is False:
            exit_code=3
    except (ValueError,KeyError,RuntimeError,OSError,sqlite3.Error) as exc:
        p.exit(2,"未完成："+str(exc)+"\n")
    finally:
        if db: db.close()
    return exit_code


if __name__=="__main__":
    sys.exit(main())
