#!/usr/bin/env python3
"""Meaningful codec compatibility checks using ONLY restored real source records."""
import argparse,json,tempfile
from pathlib import Path
import collector as c
import import_compact_history as imp
from verify_full_history import verified_part,replay

def main(parts):
    targets={'600000.SH','000001.SZ','920000.BJ'};checked=[]
    for file in parts.rglob('bootstrap_part.json'):
        summary,listed=verified_part(file.parent)
        for sym in targets.intersection(summary['results']):
            item=summary['results'][sym];rows=replay(file.parent,item,summary['target_date'],listed)
            with tempfile.TemporaryDirectory(prefix='real-history-codec-') as temp:
                a=c.Database(Path(temp)/'compact-check.sqlite3');b=c.Database(Path(temp)/'legacy-check.sqlite3')
                a.db.executescript('''CREATE TABLE history_source_evidence(evidence_id INTEGER PRIMARY KEY,evidence_sha256 TEXT NOT NULL UNIQUE,symbol TEXT NOT NULL,source TEXT NOT NULL,fetched_at TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE history_evidence_imports(evidence_id INTEGER PRIMARY KEY,row_count INTEGER NOT NULL,imported_at TEXT NOT NULL);''')
                # Existing real key exercises the established merge branch.
                a.put_quote(rows[0]);b.put_quote(rows[0])
                added,overlap=imp.put_symbol(a,file.parent,parts.parent,summary,listed,sym,item)
                with b.db:
                    for row in rows:
                        row['snapshot']={**row['snapshot'],'path':str(file.parent/item['raw_ref']),'archive_ref':imp.SOURCE_REF,'archive_sha256':imp.ARCHIVE_SHA}
                        b.put_quote(row,commit=False)
                fields='symbol,trade_date,'+','.join(c.FIELDS)+',conflict'
                left=[tuple(r) for r in a.db.execute('SELECT '+fields+' FROM daily_quotes ORDER BY trade_date')]
                right=[tuple(r) for r in b.db.execute('SELECT '+fields+' FROM daily_quotes ORDER BY trade_date')]
                assert left==right,'fact or conflict difference'
                for observation in a.db.execute("SELECT * FROM quote_observations WHERE payload LIKE '%history_evidence_v1%'"):
                    original=imp.resolve_compact_observation(a,observation,parts.parent)
                    expected=next(x for x in rows if x['trade_date']==original['trade_date'])
                    assert all(original.get(k)==expected.get(k) for k in c.FIELDS)
                    assert original['fetched_at']==expected['fetched_at']
                before=a.counts();assert imp.put_symbol(a,file.parent,parts.parent,summary,listed,sym,item)==(0,0)
                assert before==a.counts(),'idempotency failure'
                assert a.db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                checked.append({'symbol':sym,'real_rows':len(rows),'new_keys':added,'existing_keys':overlap,'same_facts':True,'lineage_resolved':True,'repeat_unchanged':True})
                a.db.close();b.db.close()
    assert {x['symbol'] for x in checked}==targets
    print(json.dumps({'real_source_compatibility_checks':checked,'production_database_touched':False},ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--parts',type=Path,required=True)
    main(p.parse_args().parts)
