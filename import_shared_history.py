#!/usr/bin/env python3
"""Append actual replayed history to the existing market identity using shared evidence.

No prediction database is opened. Existing observations and source facts are
retained. daily_quotes remains the same named 22-column read/write interface.
Only repeated provenance is represented once per original source response.
"""
import argparse,hashlib,json,sqlite3,tempfile,shutil
from pathlib import Path
import collector as c
import import_compact_history as old
from short_term import digest,write_json
from import_full_history import existing_database
from verify_full_history import replay,verified_part

COLUMNS=['symbol','trade_date',*c.FIELDS,'fetched_at','source_updated_at','source','finality','conflict','provenance']

def initialize(db):
    db.db.executescript('''CREATE TABLE IF NOT EXISTS history_source_evidence(
evidence_id INTEGER PRIMARY KEY,evidence_sha256 TEXT NOT NULL UNIQUE,symbol TEXT NOT NULL,
source TEXT NOT NULL,fetched_at TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS history_evidence_imports(
evidence_id INTEGER PRIMARY KEY,row_count INTEGER NOT NULL,imported_at TEXT NOT NULL);''')
    entry=db.db.execute("SELECT value FROM metadata WHERE key='history_storage_layout'").fetchone()
    if entry:
        if entry[0]!='shared_source_v1':raise ValueError('unrecognized existing history storage layout')
        return
    if [r[1] for r in db.db.execute('PRAGMA table_info(daily_quotes)')]!=COLUMNS:raise ValueError('unexpected original quote interface')
    # Explicit BEGIN includes all DDL in one atomic migration.
    db.db.execute('BEGIN IMMEDIATE')
    try:
        db.db.execute('ALTER TABLE daily_quotes RENAME TO quote_overrides')
        db.db.execute('CREATE TABLE history_quote_facts(symbol TEXT NOT NULL,trade_date TEXT NOT NULL,'+','.join(k+' REAL' for k in c.FIELDS)+',evidence_id INTEGER NOT NULL,conflict INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(symbol,trade_date)) WITHOUT ROWID')
        pairs=','.join("'"+k+"',b."+k for k in c.FIELDS)
        provenance="(SELECT json_group_object(j.key,json_object('source',e.source,'history_evidence_id',b.evidence_id)) FROM json_each(json_object("+pairs+")) j WHERE j.value IS NOT NULL)"
        values=['b.symbol','b.trade_date',*['b.'+k for k in c.FIELDS],'e.fetched_at',"json_extract(e.payload,'$.source_updated_at')",'e.source',"'historical_provider'",'b.conflict',provenance]
        db.db.execute('CREATE VIEW daily_quotes('+','.join(COLUMNS)+') AS SELECT '+','.join(COLUMNS)+' FROM quote_overrides UNION ALL SELECT '+','.join(values)+' FROM history_quote_facts b JOIN history_source_evidence e ON e.evidence_id=b.evidence_id WHERE NOT EXISTS(SELECT 1 FROM quote_overrides o WHERE o.symbol=b.symbol AND o.trade_date=b.trade_date)')
        insert='INSERT OR REPLACE INTO quote_overrides('+','.join(COLUMNS)+') VALUES('+','.join('NEW.'+k for k in COLUMNS)+');'
        db.db.execute('CREATE TRIGGER daily_quotes_insert INSTEAD OF INSERT ON daily_quotes BEGIN '+insert+' END')
        db.db.execute("CREATE TRIGGER daily_quotes_update INSTEAD OF UPDATE ON daily_quotes BEGIN SELECT CASE WHEN NEW.symbol!=OLD.symbol OR NEW.trade_date!=OLD.trade_date THEN RAISE(ABORT,'quote identity change refused') END; "+insert+' END')
        db.db.execute("INSERT INTO metadata VALUES('history_storage_layout','shared_source_v1')")
        if [r[1] for r in db.db.execute('PRAGMA table_info(daily_quotes)')]!=COLUMNS:raise ValueError('read/write schema mismatch')
        db.db.commit()
    except BaseException:
        db.db.rollback();raise

def put_symbol(db,root,archive_root,summary,listed,sym,item):
    rows=replay(root,item,summary['target_date'],listed)
    if not rows:raise ValueError('no verified source rows')
    prior={r['trade_date']:r for r in db.db.execute('SELECT * FROM daily_quotes WHERE symbol=?',(sym,))}
    with db.db:
        eid,key=old.evidence(db,root,archive_root,item,rows[0])
        done=db.db.execute('SELECT row_count FROM history_evidence_imports WHERE evidence_id=?',(eid,)).fetchone()
        if done:
            if done[0]!=len(rows):raise ValueError('checkpoint conflict')
            return 0,0
        values=[];overlap=0
        for row in rows:
            if row['trade_date'] in prior:
                row['snapshot']={**row['snapshot'],'path':str(root/item['raw_ref']),'archive_ref':old.SOURCE_REF,'archive_sha256':old.ARCHIVE_SHA}
                db.put_quote(row,commit=False);overlap+=1
            else:values.append((sym,row['trade_date'],*[row.get(k) for k in c.FIELDS],eid,0))
        db.db.executemany('INSERT INTO history_quote_facts VALUES('+','.join('?' for _ in range(18))+')',values)
        db.db.execute('INSERT INTO history_evidence_imports VALUES(?,?,?)',(eid,len(rows),c.stamp()))
        db.db.execute('INSERT OR REPLACE INTO backfill_progress VALUES(?,?,?,?,?,?,?)',(sym,summary['target_date'],250,'completed' if item['ohlcv_bars']>=250 else 'insufficient_history',item['ohlcv_bars'],item['fetched_at'],None if item['ohlcv_bars']>=250 else 'actual history shorter than 250; not padded'))
    return len(values),overlap

def check_real_compatibility(parts):
    targets={'600000.SH','000001.SZ','920000.BJ'};checks=[]
    for file in parts.rglob('bootstrap_part.json'):
        summary,listed=verified_part(file.parent)
        for sym in targets.intersection(summary['results']):
            item=summary['results'][sym];rows=replay(file.parent,item,summary['target_date'],listed)
            with tempfile.TemporaryDirectory(prefix='shared-real-compat-') as temp:
                a=c.Database(Path(temp)/'shared.sqlite3');b=c.Database(Path(temp)/'legacy.sqlite3')
                a.put_quote(rows[0]);b.put_quote(rows[0]);initialize(a)
                inserted,overlap=put_symbol(a,file.parent,parts.parent,summary,listed,sym,item)
                with b.db:
                    for row in rows:
                        row['snapshot']={**row['snapshot'],'path':str(file.parent/item['raw_ref']),'archive_ref':old.SOURCE_REF,'archive_sha256':old.ARCHIVE_SHA};b.put_quote(row,commit=False)
                fields='symbol,trade_date,'+','.join(c.FIELDS)+',fetched_at,source_updated_at,source,finality,conflict'
                assert [tuple(r) for r in a.db.execute('SELECT '+fields+' FROM daily_quotes ORDER BY trade_date')]==[tuple(r) for r in b.db.execute('SELECT '+fields+' FROM daily_quotes ORDER BY trade_date')]
                before=a.counts();assert put_symbol(a,file.parent,parts.parent,summary,listed,sym,item)==(0,0);assert a.counts()==before
                # A later actual observation must still work through the view.
                a.put_quote(rows[-1]);b.put_quote(rows[-1])
                assert [tuple(r) for r in a.db.execute('SELECT '+fields+' FROM daily_quotes ORDER BY trade_date')]==[tuple(r) for r in b.db.execute('SELECT '+fields+' FROM daily_quotes ORDER BY trade_date')]
                assert a.db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                checks.append({'symbol':sym,'real_rows':len(rows),'numeric_and_capture_metadata_equal':True,'repeat_unchanged':True,'future_collector_write_compatible':True,'new_keys':inserted,'existing_keys':overlap})
                a.db.close();b.db.close()
    if {x['symbol'] for x in checks}!=targets:raise ValueError('three market source checks incomplete')
    return checks

def run(parts,path,expected,output):
    symbols=existing_database(path,expected);archive_root=parts.parent
    archive=archive_root.parent/'real-history-evidence.tar.gz'
    if digest(archive)!=old.ARCHIVE_SHA:raise ValueError('full raw archive hash mismatch')
    checks=check_real_compatibility(parts)
    partitions,total=old.prepare(parts,symbols)
    result={'kind':'verified_shared_history_import','started_at':c.stamp(),'input_database_sha256':expected,'source_archive_sha256':old.ARCHIVE_SHA,'source_ref':old.SOURCE_REF,'transport_index_ref':old.TRANSPORT_REF,'raw_replayed_bars':total,'real_compatibility_checks':checks,'data_status':'DATA NOT READY','model_ready':False,'prediction_database_touched':False,'persistence_pending':True}
    existing_database(path,expected);db=c.Database(path)
    before=db.counts();metadata=[tuple(r) for r in db.db.execute('SELECT * FROM metadata ORDER BY key')]
    try:
        initialize(db);run_id=db.start('verified_shared_history_import','2026-09-11',result)
        added=overlap=done=0
        for root,summary,listed in partitions:
            for sym,item in summary['results'].items():
                if not item.get('normalized_ref'):continue
                n,m=put_symbol(db,root,archive_root,summary,listed,sym,item);added+=n;overlap+=m;done+=1
                if done%500==0:print(json.dumps({'done':done,'new_keys':added,'at':c.stamp()}),flush=True)
        if [tuple(r) for r in db.db.execute("SELECT * FROM metadata WHERE key!='history_storage_layout' ORDER BY key")]!=metadata:raise ValueError('original metadata changed')
        result.update(run_id=run_id,new_security_dates=added,existing_keys_observed=overlap,counts_before=before,counts_after=db.counts(),verified_symbols=done)
        result['history_250_symbols']=db.db.execute('SELECT COUNT(*) FROM(SELECT symbol FROM daily_quotes WHERE open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL AND close IS NOT NULL AND volume IS NOT NULL GROUP BY symbol HAVING COUNT(*)>=250)').fetchone()[0]
        result['amount_rows']=db.db.execute('SELECT COUNT(*) FROM daily_quotes WHERE amount IS NOT NULL').fetchone()[0]
        result['quote_conflicts']=db.db.execute('SELECT COUNT(*) FROM daily_quotes WHERE conflict!=0').fetchone()[0]
        result['source_evidence_rows']=db.db.execute('SELECT COUNT(*) FROM history_source_evidence').fetchone()[0]
        result['historical_fact_observations']=db.db.execute('SELECT COUNT(*) FROM history_quote_facts').fetchone()[0]
        result['total_logical_observations']=result['historical_fact_observations']+db.db.execute('SELECT COUNT(*) FROM quote_observations').fetchone()[0]
        db.log(run_id,'github_real_history_artifact','verified_shared_history_import','partial',old.SOURCE_REF,raw={'sha256':old.ARCHIVE_SHA,'path':str(archive),'bytes':archive.stat().st_size})
        db.finish(run_id,'partial',result);db.db.commit();db.db.execute('PRAGMA wal_checkpoint(TRUNCATE)');db.db.execute('PRAGMA journal_mode=DELETE')
        result['sqlite_integrity_check']=db.db.execute('PRAGMA integrity_check').fetchone()[0]
        if result['sqlite_integrity_check']!='ok' or db.db.execute('PRAGMA application_id').fetchone()[0]!=c.APP_ID:raise ValueError('integrity or application identity failure')
    finally:db.db.close()
    result.update(finished_at=c.stamp(),database_sha256=digest(path),database_bytes=path.stat().st_size)
    write_json(output,result);print(json.dumps(result,ensure_ascii=False),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--parts',type=Path,required=True);p.add_argument('--db',type=Path,required=True);p.add_argument('--expected-db-sha256',required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.parts,a.db,a.expected_db_sha256,a.output)
