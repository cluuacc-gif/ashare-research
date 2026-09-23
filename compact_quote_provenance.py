#!/usr/bin/env python3
"""Losslessly deduplicate quote provenance in an existing market database.

No network, predictions, model training or data-admission changes. The public
22-column daily_quotes interface and exact provenance JSON remain unchanged.
Run on an isolated restored copy, then save back with the original file ID/CAS.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
from zoneinfo import ZoneInfo

APP_ID = 1095977041
LAYOUT = "normalized_field_sources_v1"
FIELDS = ("prev_close", "open", "high", "low", "close", "pct_change", "volume",
          "amount", "turnover", "amplitude", "total_market_cap", "float_market_cap",
          "limit_up", "limit_down")
COLUMNS = ("symbol", "trade_date", *FIELDS, "fetched_at", "source_updated_at",
           "source", "finality", "conflict", "provenance")
SCALARS = COLUMNS[:-1]


def stamp():
    return dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_sealed(path, expected_sha256):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError("existing database required; no implicit creation")
    for suffix in ("-wal", "-journal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("active or stale SQLite sidecar; restore into a fresh directory")
    with path.open("rb") as stream:
        header = stream.read(100)
    if len(header) != 100 or not header.startswith(b"SQLite format 3\0"):
        raise ValueError("not a SQLite snapshot")
    page_size = struct.unpack(">H", header[16:18])[0]
    page_size = 65536 if page_size == 1 else page_size
    page_count = struct.unpack(">I", header[28:32])[0]
    if page_size * page_count != path.stat().st_size:
        raise ValueError("incomplete SQLite snapshot: header size differs from file bytes")
    if header[18:20] != b"\x01\x01":
        raise ValueError("snapshot must be checkpointed and use DELETE journal mode")
    if digest(path) != expected_sha256:
        raise ValueError("restored snapshot SHA256 differs from pinned source")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA application_id").fetchone()[0] != APP_ID:
            raise ValueError("wrong market database identity")
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("database integrity check failed")
    return path


def rows_digest(db, table):
    # Only trusted fixed table names selected below reach this function.
    value = hashlib.sha256()
    count = 0
    for row in db.execute(f"SELECT * FROM {table} ORDER BY symbol,trade_date"):
        value.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":"),
                                allow_nan=False).encode("utf-8") + b"\n")
        count += 1
    return {"rows": count, "sha256": value.hexdigest()}


def make_interface(db, target):
    scalar_names = ",".join(SCALARS)
    provenance = """(SELECT json_group_object(field,json(value)) FROM (
      SELECT p.field,v.value FROM quote_provenance_fields p
      JOIN quote_provenance_values v ON v.id=p.value_id
      WHERE p.symbol=f.symbol AND p.trade_date=f.trade_date ORDER BY p.field))"""
    db.execute(f"CREATE VIEW {target}({','.join(COLUMNS)}) AS SELECT " +
               ",".join("f." + name for name in SCALARS) + "," + provenance +
               " FROM quote_override_facts f")
    insert = f"""
      SELECT CASE WHEN NEW.provenance IS NULL OR NOT json_valid(NEW.provenance)
        THEN RAISE(ABORT,'invalid provenance JSON') END;
      SELECT CASE WHEN json_type(NEW.provenance)!='object' OR EXISTS(
        SELECT 1 FROM json_each(NEW.provenance) WHERE type!='object')
        THEN RAISE(ABORT,'provenance must be a map of source objects') END;
      INSERT OR REPLACE INTO quote_override_facts({scalar_names})
        VALUES({','.join('NEW.'+name for name in SCALARS)});
      INSERT INTO quote_provenance_values(value)
        SELECT DISTINCT j.value FROM json_each(NEW.provenance) j
        WHERE NOT EXISTS(SELECT 1 FROM quote_provenance_values v WHERE v.value=j.value);
      DELETE FROM quote_provenance_fields
        WHERE symbol=NEW.symbol AND trade_date=NEW.trade_date;
      INSERT INTO quote_provenance_fields(symbol,trade_date,field,value_id)
        SELECT NEW.symbol,NEW.trade_date,j.key,v.id FROM json_each(NEW.provenance) j
        JOIN quote_provenance_values v ON v.value=j.value;
    """
    db.execute(f"CREATE TRIGGER {target}_sources_insert INSTEAD OF INSERT ON {target} BEGIN " + insert + " END")
    db.execute(f"CREATE TRIGGER {target}_sources_update INSTEAD OF UPDATE ON {target} BEGIN " +
               "SELECT CASE WHEN NEW.symbol!=OLD.symbol OR NEW.trade_date!=OLD.trade_date " +
               "THEN RAISE(ABORT,'quote identity change refused') END; " + insert + " END")


def migrate(path, expected_sha256):
    path = verify_sealed(path, expected_sha256)
    started = stamp()
    before_bytes = path.stat().st_size
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA busy_timeout=30000")
        saved = db.execute("SELECT value FROM metadata WHERE key='quote_provenance_layout'").fetchone()
        if saved:
            if saved[0] != LAYOUT:
                raise ValueError("unrecognized provenance layout")
            return {"kind": "lossless_provenance_compaction", "changed": False,
                    "sha256": expected_sha256, "size_bytes": before_bytes,
                    "layout": LAYOUT, "checked_at": stamp()}
        objects = {name: (kind, sql) for name, kind, sql in db.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL")}
        target = "quote_overrides" if objects.get("daily_quotes", (None,))[0] == "view" else "daily_quotes"
        if objects.get(target, (None,))[0] != "table":
            raise ValueError("unexpected quote storage layout")
        if tuple(r[1] for r in db.execute(f"PRAGMA table_info({target})")) != COLUMNS:
            raise ValueError("22-column quote interface differs from supported schema")
        if any(name in objects for name in ("quote_override_facts", "quote_provenance_values", "quote_provenance_fields")):
            raise ValueError("partial migration or conflicting table names")
        original_rows = rows_digest(db, target)
        original_meta = db.execute("SELECT * FROM metadata ORDER BY key").fetchall()
        other_counts = {name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                        for name, (kind, _) in objects.items() if kind == "table" and name != target}
        dependent = []
        if target == "quote_overrides":
            for name in ("daily_quotes", "daily_quotes_insert", "daily_quotes_update"):
                if name not in objects:
                    raise ValueError("shared-history compatibility interface missing")
                dependent.append((name, *objects[name]))
        if any(kind == "trigger" and name not in {x[0] for x in dependent}
               and target in (sql or "") for name, (kind, sql) in objects.items()):
            raise ValueError("unrecognized dependent trigger; manual review required")
        if db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (target,)).fetchone()[0]:
            raise ValueError("custom quote index requires explicit migration review")
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("CREATE TABLE quote_override_facts(symbol TEXT NOT NULL,trade_date TEXT NOT NULL," +
                       ",".join(name + " REAL" for name in FIELDS) +
                       ",fetched_at TEXT NOT NULL,source_updated_at TEXT,source TEXT NOT NULL," +
                       "finality TEXT NOT NULL,conflict INTEGER NOT NULL DEFAULT 0," +
                       "PRIMARY KEY(symbol,trade_date)) WITHOUT ROWID")
            db.execute("CREATE TABLE quote_provenance_values(id INTEGER PRIMARY KEY,value TEXT NOT NULL UNIQUE)")
            db.execute("CREATE TABLE quote_provenance_fields(symbol TEXT NOT NULL,trade_date TEXT NOT NULL," +
                       "field TEXT NOT NULL,value_id INTEGER NOT NULL REFERENCES quote_provenance_values(id),PRIMARY KEY(symbol,trade_date,field)) WITHOUT ROWID")
            db.execute(f"INSERT INTO quote_override_facts SELECT {','.join(SCALARS)} FROM {target}")
            if db.execute(f"SELECT COUNT(*) FROM {target} WHERE NOT json_valid(provenance)").fetchone()[0]:
                raise ValueError("invalid existing provenance JSON")
            if db.execute(f"SELECT COUNT(*) FROM {target} WHERE json_type(provenance)!='object'").fetchone()[0]:
                raise ValueError("unexpected provenance shape")
            if db.execute(f"SELECT COUNT(*) FROM {target},json_each(provenance) j WHERE j.type!='object'").fetchone()[0]:
                raise ValueError("unexpected source value shape")
            db.execute(f"INSERT OR IGNORE INTO quote_provenance_values(value) SELECT j.value FROM {target},json_each(provenance) j")
            db.execute(f"INSERT INTO quote_provenance_fields SELECT q.symbol,q.trade_date,j.key,v.id " +
                       f"FROM {target} q,json_each(q.provenance) j JOIN quote_provenance_values v ON v.value=j.value")
            for name, kind, _ in reversed(dependent):
                db.execute(f"DROP {kind.upper()} {name}")
            db.execute(f"DROP TABLE {target}")
            make_interface(db, target)
            for _, _, sql in dependent:
                db.execute(sql)
            after_rows = rows_digest(db, target)
            if after_rows != original_rows:
                raise ValueError("lossless comparison failed, including exact provenance JSON")
            if tuple(r[1] for r in db.execute("PRAGMA table_info(daily_quotes)")) != COLUMNS:
                raise ValueError("public quote interface changed")
            if db.execute("SELECT * FROM metadata ORDER BY key").fetchall() != original_meta:
                raise ValueError("original metadata changed")
            for name, count in other_counts.items():
                if db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] != count:
                    raise ValueError("unrelated table changed: " + name)
            db.execute("INSERT INTO metadata VALUES('quote_provenance_layout',?)", (LAYOUT,))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        db.execute("VACUUM")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("integrity check failed after compaction; do not publish")
        if rows_digest(db, target) != original_rows:
            raise ValueError("post-compaction content changed; do not publish")
        result = {"kind": "lossless_provenance_compaction", "changed": True, "layout": LAYOUT,
                  "started_at": started, "finished_at": stamp(), "input_sha256": expected_sha256,
                  "before_bytes": before_bytes, "after_bytes": path.stat().st_size,
                  "exact_22_column_fingerprint": original_rows,
                  "unique_source_objects": db.execute("SELECT COUNT(*) FROM quote_provenance_values").fetchone()[0],
                  "field_source_links": db.execute("SELECT COUNT(*) FROM quote_provenance_fields").fetchone()[0],
                  "unrelated_table_counts_preserved": True, "integrity_check": "ok",
                  "data_readiness_changed": False, "model_readiness_changed": False,
                  "prediction_database_touched": False}
    finally:
        db.close()
    result["sha256"] = digest(path)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--audit", required=True, type=Path)
    args = parser.parse_args()
    audit = migrate(args.db, args.expected_sha256)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    args.audit.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False))
