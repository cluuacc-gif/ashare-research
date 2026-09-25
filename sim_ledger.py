#!/usr/bin/env python3
"""Simulated portfolio ledger: virtual fills under research rule, settle E+1/E+2.

Stores JSONL under sim-ledger/ so forward samples accumulate without broker API.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent
LEDGER = ROOT / "sim-ledger" / "positions.jsonl"


def append_line(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--action", required=True, choices=("open", "settle", "status"))
    p.add_argument("--symbol")
    p.add_argument("--entry-price", type=float)
    p.add_argument("--qty", type=float, default=100)
    p.add_argument("--entry-date")
    p.add_argument("--note", default="")
    p.add_argument("--exit-price", type=float)
    p.add_argument("--exit-date")
    args = p.parse_args()
    now = datetime.now(TZ).isoformat()
    if args.action == "open":
        if not (args.symbol and args.entry_price and args.entry_date):
            raise SystemExit("open requires --symbol --entry-price --entry-date")
        rec = {
            "kind": "sim_open",
            "symbol": args.symbol,
            "entry_date": args.entry_date,
            "entry_price": args.entry_price,
            "qty": args.qty,
            "opened_at": now,
            "rule": "gap2-4 o2c>=5 close<=8 not-Fri non-ST no-high-risk",
            "note": args.note,
            "status": "open",
        }
        append_line(LEDGER, rec)
        print(json.dumps(rec, ensure_ascii=False))
        return 0
    rows = load(LEDGER)
    if args.action == "settle":
        if not (args.symbol and args.exit_price and args.exit_date):
            raise SystemExit("settle requires --symbol --exit-price --exit-date")
        # settle latest open for symbol
        for rec in reversed(rows):
            if rec.get("symbol") == args.symbol and rec.get("status") == "open":
                entry = rec["entry_price"]
                ret = args.exit_price / entry - 1 - 0.001
                rec.update(
                    status="closed",
                    exit_date=args.exit_date,
                    exit_price=args.exit_price,
                    net_return=ret,
                    win=ret > 0,
                    closed_at=now,
                )
                append_line(LEDGER, {"kind": "sim_settle", **rec})
                print(json.dumps(rec, ensure_ascii=False))
                return 0
        raise SystemExit("no open position")
    opens = [r for r in rows if r.get("status") == "open" and r.get("kind") == "sim_open"]
    closed = [r for r in rows if r.get("status") == "closed"]
    wins = [r for r in closed if r.get("win")]
    summary = {
        "open": len(opens),
        "closed": len(closed),
        "wins": len(wins),
        "win_rate": (len(wins) / len(closed)) if closed else None,
        "mean_net": (sum(r["net_return"] for r in closed) / len(closed)) if closed else None,
        "ledger": str(LEDGER),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
