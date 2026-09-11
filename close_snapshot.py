#!/usr/bin/env python3
"""Capture timestamped post-close vendor quotes for an entire verified census.

Preserves partial batches and raw responses. Does not open a production DB,
assert official states, or promote an observed price slice to a formal pool.
"""
import argparse
import datetime as dt
import json
from pathlib import Path

import collector
from short_term import digest, write_json


def capture(source_dir):
    root = Path(source_dir).resolve()
    manifest_path = root/"source_acceptance.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    day = manifest["calendar_upper_bound"]
    started = collector.stamp()
    if day != collector.now().date().isoformat() or collector.now().time() < dt.time(15, 10):
        raise ValueError("current quote interface only usable for today's closed session")
    target = root/"daily_snapshot"
    if target.exists() and any(target.iterdir()):
        raise ValueError("refusing to overwrite a prior collection")
    target.mkdir(parents=True, exist_ok=True)
    sources = [x for x in manifest.get("market_census_sources", []) if x.get("census_probe_pass")]
    if not sources: raise ValueError("no complete paginated census")
    selected = sources[-1]
    census_path = (root/selected["snapshot"]["path"]).resolve()
    if not census_path.is_relative_to(root) or digest(census_path) != selected["snapshot"]["sha256"]:
        raise ValueError("census hash/path mismatch")
    census = json.loads(census_path.read_text(encoding="utf-8"))
    members = {sym: info for values in census["candidates"].values() for sym, info in values.items()}
    symbols = sorted(members)
    fetcher = collector.Fetcher(target/"raw", retries=2, timeout=15, interval=.4)
    quotes = {}; errors = []; batches = []
    for offset in range(0, len(symbols), 50):
        batch = symbols[offset:offset+50]
        try:
            rows, metas = collector.tx_spot(fetcher, batch, day)
            for meta in metas:
                meta["path"] = str(Path(meta["path"]).relative_to(root))
            observed = set()
            for row in rows:
                if row["symbol"] not in batch or row["symbol"] in quotes:
                    errors.append({"symbol": row["symbol"], "error": "unexpected_or_duplicate_symbol"}); continue
                row["official_status_verified"] = False
                row["official_limits_verified"] = False
                row["limit_price_origin"] = "vendor_not_official"
                row["list_date"] = None; row["is_st"] = None; row["trading_status"] = None
                row["name_indicates_st"] = "ST" in row.get("name", "").upper()
                row["price_change"] = row["close"]-row["prev_close"] if row.get("prev_close") else None
                quotes[row["symbol"]] = row; observed.add(row["symbol"])
            batch_record = {"offset": offset, "expected": len(batch), "retrieved": len(observed),
                            "missing": sorted(set(batch)-observed), "responses": metas}
            batches.append(batch_record)
            write_json(target/f"batch_{offset:05d}.json", {"meta": batch_record, "quotes": rows})
        except Exception as exc:
            errors.append({"offset": offset, "symbols": batch, "error": f"{type(exc).__name__}: {exc}"[:1800]})
            # Bounded source circuit breaker. Explicit refusals never cause
            # switching identities/hosts or continued requests to the source.
            if "403" in str(exc) or "401" in str(exc) or fetcher.failures.get("tencent_spot", 0) >= 5: break
    values = [quotes[s] for s in sorted(quotes)]
    observed_pool = [r for r in values if 5 <= r["close"] <= 10]
    expected = len(symbols)
    stats = {field: sum(r.get(field) is not None for r in values) for field in
             ("open", "high", "low", "close", "volume", "amount", "total_market_cap", "float_market_cap", "turnover")}
    result = {"schema_version": "1.0", "mode": "diagnostic/current_close_snapshot",
              "data_status": "DATA NOT READY", "target_date": day, "started_at": started, "finished_at": collector.stamp(),
              "source_census_sha256": digest(census_path), "universe_denominator_kind": "vendor_current_census",
              "expected_vendor_securities": expected, "official_expected_securities": None,
              "observed_quotes": len(values), "provider_scope_quote_coverage": len(values)/expected if expected else None,
              "official_all_A_coverage": None, "field_nonmissing_counts": stats,
              "observed_price_range_count": len(observed_pool), "formal_pool_complete": False,
              "status_official_coverage": 0, "historical_250_all_market_coverage": None,
              "full_bootstrap_complete": False, "model_ready": False, "production_database_written": False,
              "missing_symbols": sorted(set(symbols)-set(quotes)), "errors": errors,
              "source_updated_min": min((r["source_updated_at"] for r in values), default=None),
              "source_updated_max": max((r["source_updated_at"] for r in values), default=None),
              "observed_exchange_counts": {ex: sum(r["symbol"].endswith(ex) for r in values) for ex in ("SH", "SZ", "BJ")},
              "note": "真实供应商盘后报价；非交易所最终证明。部分缺失或状态未知不能标正式股票池/模型READY。"}
    write_json(target/"quotes.json", values)
    write_json(target/"observed_5_to_10_not_formal_pool.json", observed_pool)
    write_json(target/"data_quality.json", result)
    lines = [f"# A股行情数据质量报告｜{day}", "", "DATA NOT READY", "",
             f"实际抓取：{started} 至 {result['finished_at']}", "",
             f"供应商当前市场名单：{expected}只；取得目标日带时间戳盘后报价：{len(values)}只。",
             f"已观察到收盘5～10元：{len(observed_pool)}只（尚未完成官方状态/风险/历史排除，不是正式候选池）。", "",
             "| 字段 | 非缺失条数 |", "|---|---:|"]
    lines += [f"| {k} | {v} |" for k, v in stats.items()]
    lines += ["", "官方全A分母、ST/停复牌/异常状态、官方限价、全池250日历史、事件/行业及模型验证尚未齐全。",
              "本产物仅为原始数据快照；未写入或替换行情生产库/预测库。", "",
              "下一次研究：不具备正式概率条件。没有生成TOP3、条件单买入价或80%胜率。"]
    (target/f"A股行情数据质量报告_{day}.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    files = [{"path": str(p.relative_to(target)), "sha256": digest(p), "bytes": p.stat().st_size}
             for p in sorted(target.rglob("*")) if p.is_file()]
    write_json(target/"manifest.json", {"files": files, "generated_at": collector.stamp(), "data_status": "DATA NOT READY"})
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__); p.add_argument("--source-dir", required=True)
    args = p.parse_args(); report = capture(args.source_dir)
    print(json.dumps({k: v for k, v in report.items() if k not in ("missing_symbols", "errors")}, ensure_ascii=False))
