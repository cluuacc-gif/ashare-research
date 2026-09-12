#!/usr/bin/env python3
"""Merge replay-verified bootstrap bars into the existing market DB; never recreate an identity."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

import collector as c
from short_term import digest, write_json
from verify_full_history import verified_part, replay

def existing_database(path,expected):
    if not path.is_file() or "预测" in path.name:
        raise ValueError("an existing market database is required; prediction databases are forbidden")
    if digest(path)!=expected:
        raise ValueError("restored database hash changed; refresh the current original version")
    con=sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True)
    try:
        if con.execute("PRAGMA application_id").fetchone()[0]!=c.APP_ID:
            raise ValueError("not the original market database application identity")
        if con.execute("PRAGMA quick_check").fetchone()[0]!="ok":
            raise ValueError("existing database integrity failure")
        symbols={r[0] for r in con.execute("SELECT symbol FROM security_master")}
    finally:con.close()
    return symbols

def run(parts,db_path,expected,archive_sha,source_ref,output,verify_only):
    if len(archive_sha)!=64 or any(x not in "0123456789abcdef" for x in archive_sha):
        raise ValueError("expected evidence archive SHA256 required")
    symbols=existing_database(db_path,expected)
    partitions=[];seen=set();rows=0;hist250=0
    for file in sorted(parts.rglob("bootstrap_part.json")):
        summary,listed=verified_part(file.parent)
        if summary["collector_sha256"]!=digest(Path(c.__file__)):
            raise ValueError("collector revision mismatch; cannot replay with unreviewed code")
        for sym,item in summary["results"].items():
            if sym in seen or sym not in symbols:raise ValueError("duplicate or unknown security in restored original master")
            seen.add(sym)
            if not item.get("normalized_ref"):continue
            verified=replay(file.parent,item,summary["target_date"],listed)
            rows+=len(verified);hist250+=item["ohlcv_bars"]>=250
        partitions.append((file.parent,summary,listed))
    if not rows:raise ValueError("no replay-verified real bars")
    result={"kind":"verified_full_history_import","source_ref":source_ref,"archive_sha256":archive_sha,
            "started_at":c.stamp(),"original_database_sha256":expected,"verified_real_bars":rows,
            "history_250_symbols_in_archive":hist250,"data_status":"DATA NOT READY","model_ready":False,
            "prediction_database_touched":False,"original_handoff_updated":False,
            "same_original_identity_required":True}
    if verify_only:
        result["database_written"]=False;write_json(output,result);print(json.dumps(result,ensure_ascii=False));return
    existing_database(db_path,expected)
    db=c.Database(db_path)
    try:
        exists=db.db.execute("SELECT 1 FROM source_log WHERE operation='verified_full_history_import' AND sha256=?",(archive_sha,)).fetchone()
        if exists:
            result.update(already_imported=True,database_written=False)
        else:
            run_id=db.start("verified_full_history_import",max(q[1]["target_date"] for q in partitions),result)
            for root,summary,listed in partitions:
                for sym,item in summary["results"].items():
                    if not item.get("normalized_ref"):continue
                    verified=replay(root,item,summary["target_date"],listed)
                    with db.db:
                        for q in verified:
                            q["snapshot"]={**q["snapshot"],"path":str((root/q["snapshot"]["path"]).resolve()),
                                  "artifact_ref":source_ref,"archive_sha256":archive_sha}
                            db.put_quote(q,commit=False)
            db.log(run_id,"github_real_history_artifact","verified_full_history_import","partial",
                   raw={"sha256":archive_sha,"path":str(parts.resolve()),"bytes":0})
            result.update(run_id=run_id,database_written=True,counts=db.counts())
            db.finish(run_id,"partial",result)
        db.db.commit()
        db.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if db.db.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower()!="delete":
            raise ValueError("database could not be sealed without WAL dependency")
        result["sqlite_integrity_check"]=db.db.execute("PRAGMA integrity_check").fetchone()[0]
        if result["sqlite_integrity_check"]!="ok":raise ValueError("post-import integrity failure")
    finally:db.db.close()
    result["database_sha256"]=digest(db_path)
    result["finished_at"]=c.stamp()
    result["persistence_pending"]="replace the same original Library ID with expected-current-version check; update handoff only after persistence succeeds"
    write_json(output,result);print(json.dumps(result,ensure_ascii=False))

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--parts",type=Path,required=True);p.add_argument("--db",type=Path,required=True)
    p.add_argument("--expected-db-sha256",required=True);p.add_argument("--archive-sha256",required=True)
    p.add_argument("--source-ref",required=True);p.add_argument("--output",type=Path,required=True)
    p.add_argument("--verify-only",action="store_true")
    a=p.parse_args();run(a.parts,a.db,a.expected_db_sha256,a.archive_sha256,a.source_ref,a.output,a.verify_only)
