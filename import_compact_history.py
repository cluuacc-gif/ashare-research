#!/usr/bin/env python3
"""Append replay-verified historical facts to the ORIGINAL market DB with shared provenance.

Raw/normalized source files remain immutable. Each compact observation resolves by
evidence_id and trade_date; no source, timestamp or former observation is deleted.
"""
import argparse
import hashlib
import json
from pathlib import Path

import collector as c
from import_full_history import existing_database
from short_term import digest, write_json
from verify_full_history import verified_part, replay

ARCHIVE_SHA = 'b89e332051ed5c43e4d93e2a4d0ebcff480fa24a2c24350b9136c99df2cbfbc0'
SOURCE_REF = 'https://github.com/cluuacc-gif/ashare-research/releases/tag/bootstrap-evidence-34677204472-1'
TRANSPORT_REF = 'https://github.com/cluuacc-gif/ashare-research/blob/data/history-34677204472-1/index.json'

def prepare(parts, symbols):
    all_parts=[]; seen=set(); indices=set(); total=0
    for file in sorted(parts.rglob('bootstrap_part.json')):
        summary,listed=verified_part(file.parent)
        if summary['collector_sha256']!=digest(Path(c.__file__)):
            raise ValueError('collector revision differs from the actual capture')
        if summary['part'] in indices: raise ValueError('duplicate partition')
        indices.add(summary['part'])
        for sym,item in summary['results'].items():
            if sym in seen or sym not in symbols: raise ValueError('duplicate or unknown security')
            seen.add(sym)
            if item.get('normalized_ref'):
                rows=replay(file.parent,item,summary['target_date'],listed)
                total+=len(rows)
        all_parts.append((file.parent,summary,listed))
    if indices!=set(range(8)) or seen!=symbols or total!=3070573:
        raise ValueError('original complete archive coverage contract mismatch')
    return all_parts,total

def evidence(db, root, archive_root, item, row, archive_sha=ARCHIVE_SHA):
    value={'schema':'historical_source_evidence_v1','symbol':item['symbol'],
           'source':row['source'],'fetched_at':row['fetched_at'],
           'source_updated_at':row.get('source_updated_at'),
           'source_url':row['snapshot']['url'],'raw_body_sha256':row['snapshot']['sha256'],
           'raw_file_sha256':item['raw_file_sha256'],
           'normalized_file_sha256':item['normalized_sha256'],
           'raw_ref':str((root/item['raw_ref']).relative_to(archive_root)),
           'normalized_ref':str((root/item['normalized_ref']).relative_to(archive_root)),
           'archive_sha256':archive_sha,'archive_ref':SOURCE_REF,
           'transport_index_ref':TRANSPORT_REF,'adjustment':'raw'}
    key=c.digest(value)
    db.db.execute('INSERT OR IGNORE INTO history_source_evidence(evidence_sha256,symbol,source,fetched_at,payload) VALUES(?,?,?,?,?)',
                  (key,item['symbol'],row['source'],row['fetched_at'],c.canonical(value)))
    identifier=db.db.execute('SELECT evidence_id FROM history_source_evidence WHERE evidence_sha256=?',(key,)).fetchone()[0]
    return identifier,key

def put_symbol(db,root,archive_root,summary,listed,sym,item):
    rows=replay(root,item,summary['target_date'],listed)
    old={r['trade_date']:r for r in db.db.execute('SELECT * FROM daily_quotes WHERE symbol=?',(sym,))}
    with db.db:
        eid,ekey=evidence(db,root,archive_root,item,rows[0])
        done=db.db.execute('SELECT row_count FROM history_evidence_imports WHERE evidence_id=?',(eid,)).fetchone()
        if done:
            if done[0]!=len(rows):raise ValueError('checkpoint row count conflict')
            return 0,0
        observations=[];quotes=[];overlap=0
        for row in rows:
            day=row['trade_date'];source=row['source']
            if day in old:
                # Keep the established merge/revision behavior for existing keys.
                row['snapshot']={**row['snapshot'],'path':str(root/item['raw_ref']),
                                 'archive_ref':SOURCE_REF,'archive_sha256':ARCHIVE_SHA}
                db.put_quote(row,commit=False);overlap+=1
                continue
            observation_sha=hashlib.sha256((ekey+'|'+day).encode()).hexdigest()
            pointer={'schema':'history_evidence_v1','evidence_id':eid,'trade_date':day}
            observations.append((sym,day,source,row['fetched_at'],row.get('source_updated_at'),observation_sha,c.canonical(pointer)))
            provenance={key:{'source':source,'history_evidence_id':eid} for key in c.FIELDS if row.get(key) is not None}
            quotes.append((sym,day,*[row.get(key) for key in c.FIELDS],row['fetched_at'],row.get('source_updated_at'),source,'historical_provider',0,c.canonical(provenance)))
        db.db.executemany('INSERT INTO quote_observations(symbol,trade_date,source,fetched_at,source_updated_at,sha256,payload) VALUES(?,?,?,?,?,?,?)',observations)
        db.db.executemany('INSERT INTO daily_quotes VALUES('+','.join('?' for _ in range(22))+')',quotes)
        db.db.execute('INSERT INTO history_evidence_imports VALUES(?,?,?)',(eid,len(rows),c.stamp()))
        db.db.execute('INSERT OR REPLACE INTO backfill_progress VALUES(?,?,?,?,?,?,?)',
                      (sym,summary['target_date'],250,'completed' if item['ohlcv_bars']>=250 else 'insufficient_history',item['ohlcv_bars'],item['fetched_at'],None if item['ohlcv_bars']>=250 else 'real history is shorter than 250; no padding'))
    return len(quotes),overlap

def resolve_compact_observation(db, observation, archive_root):
    """Resolve an immutable history pointer to its original normalized source row."""
    import gzip
    from verify_full_history import safe_path
    pointer=json.loads(observation["payload"])
    if pointer.get("schema")!="history_evidence_v1":return pointer
    evidence_row=db.db.execute("SELECT * FROM history_source_evidence WHERE evidence_id=?",(pointer["evidence_id"],)).fetchone()
    if not evidence_row:raise ValueError("missing historical evidence")
    meta=json.loads(evidence_row["payload"])
    path=safe_path(archive_root,meta["normalized_ref"])
    if digest(path)!=meta["normalized_file_sha256"]:raise ValueError("source digest changed")
    payload=json.loads(gzip.decompress(path.read_bytes()))
    matches=[dict(zip(payload["fields"],x,strict=True)) for x in payload["rows"] if x[0]==pointer["trade_date"]]
    if len(matches)!=1 or pointer["trade_date"]!=observation["trade_date"] or payload["symbol"]!=observation["symbol"]:
        raise ValueError("source observation identity mismatch")
    result=matches[0]
    result.update(symbol=meta["symbol"],source=meta["source"],fetched_at=meta["fetched_at"],snapshot=payload["snapshot"],finality="historical_provider")
    return result

def run(parts,path,expected,output,verify_only=False):
    symbols=existing_database(path,expected)
    archive_root=parts.parent
    archive=archive_root.parent/'real-history-evidence.tar.gz'
    if digest(archive)!=ARCHIVE_SHA:raise ValueError('entire restored archive SHA256 mismatch')
    partitions,total=prepare(parts,symbols)
    result={'kind':'verified_compact_history_import','schema_version':'1.0','started_at':c.stamp(),
            'original_database_sha256':expected,'source_archive_sha256':ARCHIVE_SHA,
            'source_ref':SOURCE_REF,'transport_index_ref':TRANSPORT_REF,'verified_real_bars':total,
            'verified_symbols':len(symbols),'data_status':'DATA NOT READY','model_ready':False,
            'prediction_database_touched':False,'database_written':False}
    if verify_only:
        result['finished_at']=c.stamp();write_json(output,result);print(json.dumps(result,ensure_ascii=False));return result
    existing_database(path,expected)
    db=c.Database(path)
    try:
        original_metadata=[tuple(x) for x in db.db.execute('SELECT * FROM metadata ORDER BY key')]
        before=db.counts();result['counts_before']=before
        db.db.executescript('''CREATE TABLE IF NOT EXISTS history_source_evidence(
 evidence_id INTEGER PRIMARY KEY,evidence_sha256 TEXT NOT NULL UNIQUE,symbol TEXT NOT NULL,
 source TEXT NOT NULL,fetched_at TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS history_evidence_imports(
 evidence_id INTEGER PRIMARY KEY,row_count INTEGER NOT NULL,imported_at TEXT NOT NULL);''')
        run_id=db.start('verified_compact_history_import','2026-09-11',result)
        result['run_id']=run_id;inserted=0;overlap=0;done=0
        for root,summary,listed in partitions:
            for sym,item in summary['results'].items():
                if not item.get('normalized_ref'):continue
                added,revised=put_symbol(db,root,archive_root,summary,listed,sym,item)
                inserted+=added;overlap+=revised;done+=1
                if done%250==0:
                    progress={'imported_symbols':done,'new_security_dates':inserted,'existing_keys_observed':overlap,'at':c.stamp()}
                    write_json(output.with_suffix('.progress.json'),progress)
                    print(json.dumps(progress),flush=True)
        if [tuple(x) for x in db.db.execute('SELECT * FROM metadata ORDER BY key')]!=original_metadata:
            raise ValueError('original metadata changed')
        result.update(database_written=True,new_security_dates=inserted,existing_keys_observed=overlap,counts_after=db.counts())
        result['history_250_symbols']=db.db.execute('SELECT COUNT(*) FROM (SELECT symbol FROM daily_quotes WHERE open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL AND close IS NOT NULL AND volume IS NOT NULL GROUP BY symbol HAVING COUNT(*)>=250)').fetchone()[0]
        result['current_amount_rows']=db.db.execute('SELECT COUNT(*) FROM daily_quotes WHERE amount IS NOT NULL').fetchone()[0]
        result['quote_conflicts']=db.db.execute('SELECT COUNT(*) FROM daily_quotes WHERE conflict!=0').fetchone()[0]
        db.log(run_id,'github_real_history_artifact','verified_compact_history_import','partial',SOURCE_REF,raw={'sha256':ARCHIVE_SHA,'path':str(archive),'bytes':archive.stat().st_size})
        db.finish(run_id,'partial',result);db.db.commit()
        db.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        if db.db.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower()!='delete':raise ValueError('WAL sealing failed')
        result['sqlite_integrity_check']=db.db.execute('PRAGMA integrity_check').fetchone()[0]
        if result['sqlite_integrity_check']!='ok':raise ValueError('integrity failure')
    finally:db.db.close()
    result['database_sha256']=digest(path);result['database_bytes']=path.stat().st_size
    result['finished_at']=c.stamp();result['same_original_identity_required']=True
    result['persistence_pending']='replace original Library ID with expected-current-version guard before updating the handoff'
    write_json(output,result);print(json.dumps(result,ensure_ascii=False),flush=True)
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--parts',type=Path,required=True);p.add_argument('--db',type=Path,required=True)
    p.add_argument('--expected-db-sha256',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--verify-only',action='store_true')
    a=p.parse_args();run(a.parts,a.db,a.expected_db_sha256,a.output,a.verify_only)
