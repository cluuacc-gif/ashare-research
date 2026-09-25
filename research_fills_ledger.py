#!/usr/bin/env python3
"""Create / inspect a research fills ledger. Never fabricates broker fills.

Without real broker receipts, E+1/E+2 net-profit labels stay unfilled.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from short_term import write_json  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_fills(
  fill_id TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('buy','sell')),
  filled_at TEXT NOT NULL,
  price REAL NOT NULL,
  quantity REAL NOT NULL,
  fee REAL,
  tax REAL,
  net_amount REAL,
  broker TEXT,
  source_ref TEXT,
  evidence_sha256 TEXT,
  imported_at TEXT NOT NULL,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS research_positions(
  position_id TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  entry_fill_id TEXT NOT NULL,
  exit_fill_id TEXT,
  plan_id TEXT,
  opened_at TEXT NOT NULL,
  closed_at TEXT,
  status TEXT NOT NULL,
  e1_exit_at TEXT,
  e2_exit_at TEXT,
  realized_net_profit REAL,
  labels_complete INTEGER NOT NULL DEFAULT 0
);
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--output")
    args = p.parse_args()
    path = Path(args.db)
    db = sqlite3.connect(path)
    try:
        db.executescript(SCHEMA)
        db.commit()
        fills = db.execute("SELECT COUNT(*) FROM research_fills").fetchone()[0]
        positions = db.execute("SELECT COUNT(*) FROM research_positions").fetchone()[0]
        labeled = db.execute("SELECT COUNT(*) FROM research_positions WHERE labels_complete=1").fetchone()[0]
    finally:
        db.close()
    report = {
        "kind": "research_fills_ledger",
        "database": str(path.resolve()),
        "fills": fills,
        "positions": positions,
        "labeled_positions": labeled,
        "min_labels_for_80_claim": 100,
        "min_independent_entry_dates": 40,
        "execution_win_rate_80_validated": False,
        "blocker": "no_real_broker_fill_receipts" if fills == 0 else "labels_incomplete",
        "note": (
            "Research-only mode: no broker connection. Provide real fill receipts "
            "(symbol, side, time, price, qty, fees, evidence) to complete E+1/E+2 net-profit labels."
        ),
        "data_status": "DATA NOT READY",
        "model_ready": False,
    }
    if args.output:
        write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
