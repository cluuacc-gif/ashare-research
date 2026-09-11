#!/usr/bin/env python3
"""Real-data cloud acceptance probe. Never opens either production database."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("ashare_collector", ROOT / "collector.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

MASTER_SOURCES = (
    ("SH", "stock_info_sh_name_code", {"symbol": "主板A股"}, "证券代码", "证券简称", "上市日期", "https://www.sse.com.cn/assortment/stock/list/share/"),
    ("SH", "stock_info_sh_name_code", {"symbol": "科创板"}, "证券代码", "证券简称", "上市日期", "https://www.sse.com.cn/assortment/stock/list/share/"),
    ("SZ", "stock_info_sz_name_code", {"symbol": "A股列表"}, "A股代码", "A股简称", "A股上市日期", "https://www.szse.cn/market/product/stock/list/index.html"),
    ("BJ", "stock_info_bj_name_code", {}, "证券代码", "证券简称", "上市日期", "https://www.bse.cn/nq/listedcompany.html"),
)

# Broad sanity floors, not expected-universe counts.  Exact coverage is assessed
# later by the production quality gate; these only reject empty/truncated probes.
MARKET_COUNT_FLOORS = {"SH": 1000, "SZ": 1500, "BJ": 200}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def file_record(path, output):
    return {"path": str(path.relative_to(output)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}


def fetch_vendor_census(fetcher, output, through):
    """Fetch a paginated real current-market census; never label it official."""
    rows, metas, pagination = collector.em_spot(fetcher, through.isoformat())
    candidates = {ex: {} for ex in ("SH", "SZ", "BJ")}
    rejected = []
    for row in rows:
        try:
            sym = collector.symbol(row.get("f12"))
            candidates[sym[-2:]][sym] = {
                "name": str(row.get("f14", "")), "list_date": None,
                "discovery_basis": "vendor_current_membership_only",
            }
        except (ValueError, TypeError) as exc:
            rejected.append(str(exc))
    counts = {ex: len(v) for ex, v in candidates.items()}
    nonempty_name_rates = {
        ex: (sum(bool(v.get("name", "").strip()) for v in values.values()) / len(values)
             if values else 0.0)
        for ex, values in candidates.items()
    }
    census_pass = (
        pagination.get("pagination_complete") is True
        and all(counts[ex] >= MARKET_COUNT_FLOORS[ex] for ex in counts)
        and all(nonempty_name_rates[ex] >= 0.99 for ex in counts)
    )
    path = output / "vendor_market_census.json"
    # Current membership is not a historical universe or an official status list.
    write_json(path, {"candidates": candidates, "pagination": pagination,
                      "rejected": rejected, "official_verified": False,
                      "used_for_price_pool": False, "counts": counts,
                      "minimum_sanity_counts": MARKET_COUNT_FLOORS,
                      "nonempty_name_rates": nonempty_name_rates,
                      "census_probe_pass": census_pass})
    return candidates, {"source": "eastmoney_current_market_census",
                        "source_kind": "public_vendor_current_membership",
                        "pagination": pagination, "counts": counts,
                        "minimum_sanity_counts": MARKET_COUNT_FLOORS,
                        "nonempty_name_rates": nonempty_name_rates,
                        "census_probe_pass": census_pass,
                        "source_urls": [m["url"] for m in metas],
                        "official_verified": False, "snapshot": file_record(path, output)}


def candidate_symbols(values, limit=4):
    """Deterministic bounded probe set; selection is never an investment list."""
    symbols = sorted(values)
    if len(symbols) <= limit:
        return symbols
    indexes = sorted({0, len(symbols) // 3, (2 * len(symbols)) // 3, len(symbols) - 1})
    return [symbols[index] for index in indexes[:limit]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--through", help="Inclusive calendar upper bound, not an assertion that the date is a trading day")
    parser.add_argument("--output", default="cloud-output")
    parser.add_argument("--plan", action="store_true", help="Print execution plan without network or file writes")
    args = parser.parse_args()
    started = collector.now()
    latest_closed_bound = started.date() if started.time() >= dt.time(15, 10) else started.date() - dt.timedelta(days=1)
    through = dt.date.fromisoformat(args.through) if args.through else latest_closed_bound
    if through > latest_closed_bound:
        parser.error("拒绝未来或尚未收盘的日期")
    if args.plan:
        print(json.dumps({"mode": "cloud_source_acceptance_only", "calendar_upper_bound": through.isoformat(), "official_master_endpoints": len(MASTER_SOURCES), "history_probe_candidates_per_exchange": 4, "real_vendor_market_census_fallback": True, "minimum_history_bars": 250, "writes_production_databases": False, "formal_prediction": False}, ensure_ascii=False))
        return 0
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error("输出目录必须为空，避免覆盖旧证据")
    report = {"schema_version": "1.1", "mode": "diagnostic/source_probe", "status": "RUNNING", "started_at": started.isoformat(), "calendar_upper_bound": through.isoformat(), "calendar_verified": False, "actual_latest_quote_dates": {}, "data_status": "DATA NOT READY", "formal_probability_model_ready": False, "writes_production_databases": False, "master_sources": [], "market_census_sources": [], "history_sources": [], "files": [], "github_run_id": os.environ.get("GITHUB_RUN_ID"), "github_commit": os.environ.get("GITHUB_SHA"), "collector_sha256": hashlib.sha256((ROOT / "collector.py").read_bytes()).hexdigest()}
    eligible = {ex: {} for ex in ("SH", "SZ", "BJ")}
    all_members = {ex: set() for ex in eligible}
    failed_exchanges = set()
    old_enough = through - dt.timedelta(days=550)
    lower = through - dt.timedelta(days=850)

    def checkpoint():
        write_json(output / "source_acceptance.json", report)

    checkpoint()
    try:
        for i, (exchange, operation, kwargs, code_key, name_key, date_key, url) in enumerate(MASTER_SOURCES):
            item = {"exchange": exchange, "operation": operation, "source_url": url, "started_at": collector.stamp(), "source_updated_at": None}
            try:
                rows = collector.ak_call(operation, kwargs, timeout=90)
                if not rows:
                    raise ValueError("官方主表返回空数据")
                path = output / "normalized_master" / f"{i}_{exchange}.json"
                write_json(path, rows)
                report["files"].append(file_record(path, output))
                # These are AKShare-normalized response records, not original HTTP bytes.
                item.update(snapshot=str(path.relative_to(output)), snapshot_kind="akshare_normalized_response", row_count=len(rows))
                for row in rows:
                    sym = collector.symbol(row[code_key], exchange)
                    all_members[exchange].add(sym)
                    raw_date = str(row.get(date_key) or "")[:10]
                    try:
                        listing_date = dt.date.fromisoformat(raw_date)
                    except ValueError:
                        continue
                    if listing_date <= old_enough:
                        eligible[exchange][sym] = {"name": str(row.get(name_key, "")), "list_date": listing_date.isoformat()}
                item["status"] = "retrieved"
            except Exception as exc:
                failed_exchanges.add(exchange)
                item.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:1500])
            item["finished_at"] = collector.stamp()
            report["master_sources"].append(item)
            checkpoint()

        fetcher = collector.Fetcher(output / "raw_history", retries=2, timeout=20, interval=1)
        # Always test an independent, fully paginated real census.  It is a
        # fallback for source availability, never an official status substitute.
        census = {ex: {} for ex in eligible}
        try:
            census, census_meta = fetch_vendor_census(fetcher, output, through)
            report["market_census_sources"].append(census_meta)
            report["files"].append(census_meta["snapshot"])
        except Exception as exc:
            report["market_census_sources"].append({
                "source": "eastmoney_current_market_census", "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"[:1500],
                "official_verified": False, "census_probe_pass": False,
            })
        # Official, sufficiently old symbols are preferred for history probes.
        # If an official endpoint is unavailable, vendor membership may only
        # select a mechanical test symbol and is explicitly labelled as such.
        for exchange in eligible:
            if not eligible[exchange]:
                eligible[exchange].update(census[exchange])
        checkpoint()
        for exchange in eligible:
            if not eligible[exchange]:
                report["history_sources"].append({"exchange": exchange, "status": "not_attempted", "reason": "官方名单与备用供应商均未提供可验证代码"})
                checkpoint()
                continue
            succeeded = False
            for sym in candidate_symbols(eligible[exchange]):
                for provider_name, provider in (("eastmoney_history", collector.em_history), ("tencent_history", collector.tx_history)):
                    item = {"exchange": exchange, "symbol": sym, "source": provider_name, "purpose": "technical_acceptance_only", "started_at": collector.stamp(), "source_updated_at": None}
                    item["symbol_discovery_basis"] = eligible[exchange][sym].get("discovery_basis", "official_list")
                    try:
                        rows, meta = provider(fetcher, sym, lower.isoformat(), through.isoformat())
                        dates = [row["trade_date"] for row in rows]
                        if not rows or len(dates) != len(set(dates)):
                            raise ValueError("空日线或重复日期")
                        for row in rows:
                            collector.validate_quote(row, through.isoformat())
                            snapshot = row.get("snapshot")
                            if snapshot:
                                row["snapshot"] = {**snapshot, "path": str(Path(snapshot["path"]).relative_to(output))}
                        rows.sort(key=lambda r: r["trade_date"])
                        path = output / "normalized_history" / f"{sym}_{provider_name}.json"
                        write_json(path, rows)
                        report["files"].append(file_record(path, output))
                        ohlcv = sum(all(row.get(field) is not None for field in ("open", "high", "low", "close", "volume")) for row in rows)
                        amount = sum(row.get("amount") is not None for row in rows)
                        item.update(status="history_250_retrieved" if ohlcv >= 250 else "insufficient", bars=len(rows), ohlcv_bars=ohlcv, amount_bars=amount, earliest_date=rows[0]["trade_date"], latest_date=rows[-1]["trade_date"], normalized_ref=str(path.relative_to(output)), source_url=meta["url"], fetched_at=meta["fetched_at"])
                        report["actual_latest_quote_dates"][sym] = rows[-1]["trade_date"]
                        succeeded = ohlcv >= 250
                    except Exception as exc:
                        item.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:1500])
                    item["finished_at"] = collector.stamp()
                    report["history_sources"].append(item)
                    checkpoint()
                    if succeeded:
                        break
                if succeeded:
                    break
        for path in sorted((output / "raw_history").glob("*.gz")):
            report["files"].append(file_record(path, output))
        covered = {r["exchange"] for r in report["history_sources"] if r["status"] == "history_250_retrieved"}
        report["history_endpoint_probe_pass"] = covered == {"SH", "SZ", "BJ"}
        report["official_master_counts"] = {ex: len(members) for ex, members in all_members.items()}
        report["official_master_full_coverage_verified"] = not failed_exchanges and all(all_members.values())
        census_pass = any(x.get("census_probe_pass") for x in report["market_census_sources"])
        report["market_census_probe_pass"] = census_pass
        report["observed_market_census_counts"] = next(
            (x.get("counts", {}) for x in report["market_census_sources"] if x.get("census_probe_pass")), {})
        report["status"] = "CLOUD_SOURCE_PROBE_PASS" if report["history_endpoint_probe_pass"] and census_pass else "CLOUD_SOURCE_PROBE_INCOMPLETE"
        report["full_universe_history_downloaded"] = False
        report["note"] = "探针通过仅证明真实全市场当前名单入口可分页读取，且三个市场各一只证券取得至少250根未复权OHLCV；不证明官方状态、全市场历史回填、当日完整性或概率模型就绪。"
    finally:
        report["finished_at"] = collector.stamp()
        checkpoint()
        lines = ["# 云端行情入口验收", "", f"状态：{report['status']}", f"开始：{report['started_at']}", f"结束：{report['finished_at']}", "", "正式数据状态：DATA NOT READY；没有写入行情生产库或预测库。", "", "## 当前市场名单入口", "", "| 来源 | 沪市 | 深市 | 北交所 | 分页完整 | 官方状态 |", "|---|---:|---:|---:|---|---|"]
        for row in report.get("market_census_sources", []):
            counts = row.get("counts", {})
            lines.append(f"| {row.get('source', '未知')} | {counts.get('SH', '无法验证')} | {counts.get('SZ', '无法验证')} | {counts.get('BJ', '无法验证')} | {row.get('pagination', {}).get('pagination_complete', False)} | {row.get('official_verified', False)} |")
        lines += ["", "## 历史行情入口", "", "| 市场 | 来源 | 结果 | 日线根数 | 最新日期 |", "|---|---|---|---:|---|"]
        for row in report["history_sources"]:
            lines.append(f"| {row['exchange']} | {row.get('source', '未请求')} | {row['status']} | {row.get('bars', '无法验证')} | {row.get('latest_date', '无法验证')} |")
        lines += ["", report.get("note", "运行未正常完成，详见JSON记录。")]
        lines += ["", "## 官方名单错误（不由备用行情覆盖）"]
        for row in report["master_sources"]:
            if row["status"] == "failed":
                lines += ["", f"### {row['exchange']} / {row['operation']}", "```text", row["error"], "```"]
        (output / "云端行情入口验收.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(output / "source_acceptance.json")}, ensure_ascii=False))
    return 0 if report["status"] == "CLOUD_SOURCE_PROBE_PASS" else 3


if __name__ == "__main__":
    sys.exit(main())
