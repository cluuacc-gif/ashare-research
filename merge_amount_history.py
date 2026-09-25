#!/usr/bin/env python3
"""Replay amount evidence and merge amount fields into an existing sealed market DB.

Reads only amount-verified bars from the real-amount-evidence archive layout.
Does not invent amounts, does not touch prediction DB, does not claim official limits.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import collector as c  # noqa: E402
from sealed_database import inspect_sealed, sha256  # noqa: E402
from short_term import write_json  # noqa: E402

FIELDS = ["trade_date"] + list(c.FIELDS)  # 15 columns in payload rows


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_symbol_rows(path: Path):
    payload = json.loads(gzip.decompress(path.read_bytes()))
    if payload.get("adjustment") != "raw":
        raise ValueError("non-raw history refused")
    fields = payload.get("fields")
    if fields != FIELDS:
        raise ValueError(f"unexpected fields in {path.name}")
    rows = payload["rows"]
    out = []
    for row in rows:
        rec = dict(zip(FIELDS, row))
        amount = rec.get("amount")
        if amount is None:
            continue
        if not isinstance(amount, (int, float)) or amount < 0:
            raise ValueError("invalid amount")
        out.append((rec["trade_date"], float(amount), rec.get("volume")))
    return payload["symbol"], out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--expected-db-sha256", required=True)
    p.add_argument("--amount-root", required=True, help="extracted real-amount-evidence root")
    p.add_argument("--output", required=True, help="new directory for working copy + report")
    args = p.parse_args()

    src = Path(args.db).resolve()
    if sha256(src) != args.expected_db_sha256:
        raise SystemExit("database SHA mismatch; refuse")
    before = inspect_sealed(src)
    if before["application_id"] != c.APP_ID:
        raise SystemExit("identity mismatch")

    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    working = out / src.name
    shutil.copy2(src, working)
    after = inspect_sealed(working)
    if after != before:
        raise SystemExit("copy differs from source")

    files = sorted(Path(args.amount_root).rglob("normalized/*.json.gz"))
    if not files:
        raise SystemExit("no amount evidence files")
    print(f"evidence files: {len(files)}", flush=True)

    db = sqlite3.connect(working)
    db.execute("PRAGMA journal_mode=DELETE")
    updated = inserted = skipped = errors = 0
    symbols_ok = 0
    try:
        for i, path in enumerate(files, 1):
            try:
                symbol, rows = load_symbol_rows(path)
            except Exception as e:
                errors += 1
                print(f"skip {path.name}: {type(e).__name__}: {e}", flush=True)
                continue
            if not rows:
                skipped += 1
                continue
            with db:
                for trade_date, amount, volume in rows:
                    # Prefer evidence amount only when missing or conflicting-but-null in facts.
                    cur = db.execute(
                        "SELECT amount FROM history_quote_facts WHERE symbol=? AND trade_date=?",
                        (symbol, trade_date),
                    ).fetchone()
                    if cur is not None:
                        if cur[0] is None:
                            db.execute(
                                "UPDATE history_quote_facts SET amount=? WHERE symbol=? AND trade_date=?",
                                (amount, symbol, trade_date),
                            )
                            updated += 1
                        else:
                            skipped += 1
                    else:
                        # quote_overrides row may hold newer evening quotes; fill amount only.
                        cur2 = db.execute(
                            "SELECT amount FROM quote_overrides WHERE symbol=? AND trade_date=?",
                            (symbol, trade_date),
                        ).fetchone()
                        if cur2 is not None and cur2[0] is None:
                            db.execute(
                                "UPDATE quote_overrides SET amount=? WHERE symbol=? AND trade_date=?",
                                (amount, symbol, trade_date),
                            )
                            updated += 1
                        else:
                            skipped += 1
            symbols_ok += 1
            if i % 200 == 0:
                print(f"processed {i}/{len(files)} updated={updated} skipped={skipped} errors={errors}", flush=True)
        db.execute(
            "INSERT OR REPLACE INTO source_log(run_id,source,operation,fetched_at,status,url,error,snapshot_ref,sha256,bytes) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "amount-merge-34741917528",
                "sina_compressed_raw_history",
                "verified_amount_evidence_import",
                c.stamp(),
                "success",
                None,
                None,
                str(Path(args.amount_root).resolve()),
                None,
                None,
            ),
        )
        db.commit()
        coverage = db.execute(
            "SELECT COUNT(*) FROM daily_quotes WHERE amount IS NOT NULL"
        ).fetchone()[0]
        total = db.execute("SELECT COUNT(*) FROM daily_quotes").fetchone()[0]
        hist_amount = db.execute(
            "SELECT COUNT(*) FROM history_quote_facts WHERE amount IS NOT NULL"
        ).fetchone()[0]
        symbols_with_amount = db.execute(
            "SELECT COUNT(*) FROM (SELECT symbol FROM history_quote_facts WHERE amount IS NOT NULL GROUP BY symbol)"
        ).fetchone()[0]
    finally:
        db.close()

    sealed = inspect_sealed(working)
    report = {
        "schema_version": "1.0",
        "kind": "amount_evidence_merge",
        "generated_at": c.stamp(),
        "source_archive": "real-amount-evidence.tar.gz",
        "source_run_id": "34741917528",
        "evidence_files": len(files),
        "symbols_processed": symbols_ok,
        "amount_rows_updated": updated,
        "rows_skipped_existing_or_missing": skipped,
        "file_errors": errors,
        "input_database": before,
        "working_database": {**sealed, "path": str(working)},
        "amount_coverage_after": {
            "daily_quotes_amount_not_null": coverage,
            "daily_quotes_total": total,
            "history_amount_not_null": hist_amount,
            "symbols_with_history_amount": symbols_with_amount,
        },
        "official_status_verified": False,
        "official_limits_verified": False,
        "data_status": "DATA NOT READY",
        "model_ready": False,
        "prediction_database_touched": False,
        "persistence_pending": True,
    }
    write_json(out / "amount_merge_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
