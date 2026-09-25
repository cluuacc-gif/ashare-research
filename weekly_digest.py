#!/usr/bin/env python3
"""Weekly research digest: signals, vetoes, sim ledger, coverage."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--end-date", default=datetime.now(TZ).date().isoformat())
    p.add_argument("--output")
    args = p.parse_args()
    end = datetime.fromisoformat(args.end_date)
    start = (end - timedelta(days=6)).date().isoformat()
    root = ROOT / "mail-runtime"
    days = []
    if root.is_dir():
        for d in sorted([x.name for x in root.iterdir() if x.is_dir()]):
            if start <= d <= args.end_date:
                days.append(d)
    tops = []
    vetoes = 0
    empty_days = 0
    for d in days:
        for name in (f"auction-{d}.json", "auction.json"):
            pth = root / d / name
            if pth.is_file():
                data = json.loads(pth.read_text(encoding="utf-8"))
                tops.extend(data.get("capital_limited_top") or [])
                vetoes += len(data.get("capital_limited_vetoes") or [])
                if data.get("empty_signal"):
                    empty_days += 1
                break
    ledger = ROOT / "ashare-research" / "sim-ledger" / "positions.jsonl"
    if not ledger.is_file():
        ledger = ROOT / "sim-ledger" / "positions.jsonl"
    fills = []
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if line.strip():
                fills.append(json.loads(line))
    closed = [f for f in fills if f.get("status") == "closed"]
    wins = [f for f in closed if f.get("win")]
    report = {
        "kind": "weekly_digest",
        "start": start,
        "end": args.end_date,
        "days_with_reports": len(days),
        "shortlist_mentions": len(tops),
        "vetoes": vetoes,
        "empty_signal_days": empty_days,
        "sim_closed": len(closed),
        "sim_wins": len(wins),
        "sim_win_rate": (len(wins) / len(closed)) if closed else None,
        "top_symbols": [
            {"symbol": t.get("symbol"), "name": t.get("name"), "score": t.get("research_score")}
            for t in tops[:15]
        ],
        "note": "Weekly research digest. Not investment advice.",
    }
    text = (
        f"# A股研究周报 {start} ~ {args.end_date}\n\n"
        f"- 覆盖天数：{report['days_with_reports']}\n"
        f"- 优选提及：{report['shortlist_mentions']}｜否决：{vetoes}｜空仓日：{empty_days}\n"
        f"- 模拟成交：{report['sim_closed']}｜胜率：{report['sim_win_rate']}\n\n"
        "## 本周提及标的\n\n"
        + "\n".join(f"- `{t.get('symbol')}` {t.get('name') or ''}" for t in tops[:15])
        + "\n\n本邮件为研究摘要，不构成投资建议。\n"
    )
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        Path(str(args.output) + ".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
