#!/usr/bin/env python3
"""Replay verified raw vendor artifacts into an EXISTING market DB.

No network, predictions, synthetic inputs, status inference, or admission changes.
File checksums establish consistency, not independent authentication of the upload.
"""
import argparse
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import sqlite3
import zipfile

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("collector", ROOT / "collector.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def verify(artifact):
    with zipfile.ZipFile(artifact) as archive:
        info = archive.infolist()
        names = [i.filename for i in info]
        if len(names) != len(set(names)) or sum(i.file_size for i in info) > 100_000_000:
            raise ValueError("重复条目或文件超100MB")
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ValueError("不安全的归档路径")
        data = {i.filename: archive.read(i) for i in info if not i.is_dir()}
    report = json.loads(data["source_acceptance.json"])
    if report.get("mode") != "diagnostic/source_probe":
        raise ValueError("非预期真实源探针产物")
    if report.get("collector_sha256") != sha((ROOT / "collector.py").read_bytes()):
        raise ValueError("采集器版本不一致，须审查后使用对应版本重放")
    end = c.date_value(report["calendar_upper_bound"])
    if c.timestamp(report["finished_at"]) > c.now():
        raise ValueError("产物时间位于未来")
    listed = set()
    for f in report["files"]:
        body = data[f["path"]]
        if sha(body) != f["sha256"] or len(body) != f["bytes"]:
            raise ValueError("产物校验失败: " + f["path"])
        listed.add(f["path"])
    securities, quotes = {}, []
    for source in report["master_sources"]:
        if source.get("status") != "retrieved":
            continue
        path = source["snapshot"]
        if path not in listed or not c.official_url(source["source_url"]):
            raise ValueError("名单没有已校验文件或官方来源声明")
        rows = json.loads(data[path])
        if len(rows) != source["row_count"]:
            raise ValueError("主表记录数不一致")
        for row in rows:
            sym = c.symbol(row.get("证券代码", row.get("A股代码")), source["exchange"])
            listing = row.get("上市日期", row.get("A股上市日期"))
            name = row.get("证券简称", row.get("A股简称"))
            if not name:
                raise ValueError("主表简称缺失")
            securities[sym] = {"name": name, "list_date": c.date_value(listing) if listing else None,
                               "source": "artifact_official_" + source["exchange"]}
    for source in report.get("market_census_sources", []):
        if source.get("census_probe_pass") is not True:
            continue
        ref = source["snapshot"]; body = data[ref["path"]]
        if sha(body) != ref["sha256"] or len(body) != ref["bytes"]:
            raise ValueError("供应商主表哈希不一致")
        census = json.loads(body)
        observed = sum(len(v) for v in census["candidates"].values())
        if not census["pagination"]["pagination_complete"] or observed != census["pagination"]["provider_total"]:
            raise ValueError("供应商主表分页不完整")
        for exchange, values in census["candidates"].items():
            for sym, info in values.items():
                if c.symbol(sym, exchange) != sym or not info["name"]:
                    raise ValueError("供应商证券名称/市场不一致")
                securities.setdefault(sym, {"name": info["name"], "list_date": None,
                                            "source": "artifact_"+source["source"]})
    for source in report["history_sources"]:
        if source.get("status") not in ("history_250_retrieved", "insufficient"):
            continue
        if source.get("source") not in ("eastmoney_history", "tencent_history", "sina_history"):
            raise ValueError("非已实现重放来源")
        path = source["normalized_ref"]
        if path not in listed:
            raise ValueError("日线文件缺少校验")
        rows = json.loads(data[path])
        if not rows or len(rows) != source["bars"]:
            raise ValueError("日线根数不一致")
        if len({r["trade_date"] for r in rows}) != len(rows):
            raise ValueError("日线日期重复")
        snapshot = rows[0]["snapshot"]
        raw_path = snapshot["path"]
        if raw_path not in listed:
            raise ValueError("缺少原始行情响应")
        raw = gzip.decompress(data[raw_path])
        if len(raw) != snapshot["bytes"] or sha(raw) != snapshot["sha256"]:
            raise ValueError("原始响应解压校验失败")
        payload = json.loads(raw) if source["source"] != "sina_history" else None
        if source["source"] == "eastmoney_history":
            if str(payload["data"]["code"]) != source["symbol"].split(".")[0]:
                raise ValueError("原始响应证券代码不符")
            replay = c.normalize_em_history(source["symbol"], payload, min(r["trade_date"] for r in rows), end)
        elif source["source"] == "tencent_history":
            class ReplayFetcher:
                def json(self, requested_source, url):
                    if requested_source != "tencent_history":
                        raise ValueError("非预期重放来源")
                    return payload, snapshot
            replay, _ = c.tx_history(ReplayFetcher(), source["symbol"], min(r["trade_date"] for r in rows), end)
        else:
            class SinaReplayFetcher:
                def request(self, requested_source, url):
                    if requested_source != "sina_history" or url != snapshot["url"]:
                        raise ValueError("新浪原始请求证券/周期不符")
                    return raw.decode("utf-8"), snapshot
            replay, _ = c.sina_history(SinaReplayFetcher(), source["symbol"], min(r["trade_date"] for r in rows), end)
        expected = {r["trade_date"]: r for r in replay}
        if set(expected) != {r["trade_date"] for r in rows}:
            raise ValueError("原始日线与整理后的日期不一致")
        for row in rows:
            c.validate_quote(row, end)
            if row["symbol"] != source["symbol"] or row["source"] != source["source"]:
                raise ValueError("来源或证券不一致")
            if row.get("snapshot") != snapshot or row["fetched_at"] != snapshot["fetched_at"]:
                raise ValueError("日线快照/实际抓取时间不一致")
            if c.timestamp(row["fetched_at"]) > c.timestamp(report["finished_at"]):
                raise ValueError("记录时间晚于产物完成时间")
            for field in c.FIELDS:
                if row.get(field) != expected[row["trade_date"]].get(field):
                    raise ValueError("原始行情重放不一致: " + field)
            if row["symbol"] not in securities:
                raise ValueError("主表缺少对应证券，不能推造证券状态")
        quotes.extend(rows)
    # Daily snapshot has its own manifest because it is captured after the probe.
    if "daily_snapshot/manifest.json" in data:
        daily_manifest = json.loads(data["daily_snapshot/manifest.json"])
        daily_listed = set()
        for ref in daily_manifest["files"]:
            key = "daily_snapshot/"+ref["path"]
            if key in daily_listed or key not in data or sha(data[key]) != ref["sha256"] or len(data[key]) != ref["bytes"]:
                raise ValueError("当日快照校验失败")
            daily_listed.add(key)
        if "daily_snapshot/quotes.json" not in daily_listed or "daily_snapshot/data_quality.json" not in daily_listed:
            raise ValueError("缺少已校验当日行情/质量报告")
        quality = json.loads(data["daily_snapshot/data_quality.json"])
        daily_rows = json.loads(data["daily_snapshot/quotes.json"])
        if quality["target_date"] != end or len(daily_rows) != quality["observed_quotes"]:
            raise ValueError("当日行情日期/数量不一致")
        if c.timestamp(quality["finished_at"]) > c.now():
            raise ValueError("快照位于未来")
        replay_cache = {}; seen_daily = set()
        for row in daily_rows:
            sym = c.symbol(row["symbol"])
            if sym in seen_daily or sym not in securities or row["trade_date"] != end or row["source"] != "tencent_spot":
                raise ValueError("当日证券/日期/来源冲突")
            seen_daily.add(sym)
            snapshot = row["snapshot"]; raw_path = snapshot["path"]
            if raw_path not in daily_listed:
                raise ValueError("缺少当日报价原始响应")
            if raw_path not in replay_cache:
                raw = gzip.decompress(data[raw_path])
                if sha(raw) != snapshot["sha256"] or len(raw) != snapshot["bytes"]:
                    raise ValueError("当日原始响应解压校验失败")
                replay_cache[raw_path] = {q["symbol"]: q for q in c.normalize_tx_spot(raw.decode("gb18030"), end)}
            expected = replay_cache[raw_path].get(sym)
            if not expected or any(row.get(k) != expected.get(k) for k in c.FIELDS):
                raise ValueError("当日报价与原始响应重放不一致")
            if row["source_updated_at"] != expected["source_updated_at"] or row["fetched_at"] != snapshot["fetched_at"]:
                raise ValueError("当日报价时间戳冲突")
            if c.timestamp(row["fetched_at"]) > c.timestamp(quality["finished_at"]):
                raise ValueError("报价抓取晚于报告完成")
            if row.get("official_status_verified") is not False or row.get("official_limits_verified") is not False:
                raise ValueError("供应商报价不能冒充官方状态/限价")
            c.validate_quote(row, end)
            quotes.append(row)
    if not quotes:
        raise ValueError("没有通过原始响应重放的真实行情")
    return report, data, securities, quotes


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("artifact", type=Path)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--evidence-root", type=Path, required=True)
    p.add_argument("--source-ref", required=True, help="Persistent source artifact ID or URL")
    p.add_argument("--verify-only", action="store_true")
    p.add_argument("--expected-db-sha256", help="Required before any write to preserve the restored current version")
    args = p.parse_args()
    if not args.db.is_file():
        p.error("必须提供现有行情库，禁止意外新建数据库")
    if not args.verify_only and (not args.expected_db_sha256 or sha(args.db.read_bytes()) != args.expected_db_sha256):
        p.error("行情库版本哈希缺失或已变化，拒绝写入")
    # Check identity read-only before invoking a constructor that can create tables.
    dbcheck = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
    app_id = dbcheck.execute("PRAGMA application_id").fetchone()[0]
    dbcheck.close()
    if app_id != c.APP_ID:
        p.error("不是原行情数据库，拒绝修改")
    report, files, securities, quotes = verify(args.artifact)
    digest = sha(args.artifact.read_bytes())
    summary = {"archive_sha256": digest, "source_ref": args.source_ref,
               "github_run_id_declared": report.get("github_run_id"),
               "verified_securities": len(securities), "verified_daily_quotes": len(quotes),
               "latest_quote_date": max(r["trade_date"] for r in quotes),
               "original_finished_at": report["finished_at"], "verified_at": c.stamp(),
               "data_status": "DATA NOT READY", "bootstrap_complete": False,
               "model_ready": False, "raw_replay_verified": True,
               "origin": "github_artifact; checksums_and_raw_response_replay_verified",
               "official_master_coverage_certified": False,
               "historical_status_verified": False, "prediction_db_touched": False}
    if args.verify_only:
        print(json.dumps(summary, ensure_ascii=False)); return
    existing = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        already = existing.execute("SELECT 1 FROM source_log WHERE operation='verified_artifact_import' AND sha256=?", (digest,)).fetchone()
    finally:
        existing.close()
    if already:
        summary.update(already_imported=True, db_sha256=sha(args.db.read_bytes()))
        print(json.dumps(summary, ensure_ascii=False)); return
    root = args.evidence_root.resolve() / digest
    for name, body in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != body:
            raise ValueError("同一产物目录已有冲突文件")
        target.write_bytes(body)
    db = c.Database(args.db)
    try:
        if db.db.execute("SELECT 1 FROM source_log WHERE operation='verified_artifact_import' AND sha256=?", (digest,)).fetchone():
            summary["already_imported"] = True
        else:
            run_id = db.start("verified_artifact_import", report["calendar_upper_bound"], summary)
            for sym, item in securities.items():
                # The uploaded AKShare list is provisional until original exchange
                # provenance and census are independently verified in the research layer.
                db.upsert_security(sym, item["name"], item["source"], item["list_date"], False)
            with db.db:
                for row in quotes:
                    row = {**row, "snapshot": {**row["snapshot"],
                           "path": str(root / row["snapshot"]["path"]),
                           "artifact_ref": args.source_ref, "archive_sha256": digest}}
                    db.put_quote(row, commit=False)
            db.log(run_id, "github_artifact", "verified_artifact_import", "partial",
                   raw={"sha256": digest, "bytes": args.artifact.stat().st_size,
                        "path": str(args.artifact.resolve())})
            summary["run_id"] = run_id
            summary["counts"] = db.counts()
            db.finish(run_id, "partial", summary)
        if db.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("数据库完整性异常")
    finally:
        db.close()
    summary["db_sha256"] = sha(args.db.read_bytes())
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
