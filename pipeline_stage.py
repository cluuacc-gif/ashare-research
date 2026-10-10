#!/usr/bin/env python3
"""Engineering gates and target routing. Never grants model/data admission."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3

import collector as c
from trade_calendar import context, adjacent


def check_database(path, target, previous, previous_rows):
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        latest = db.execute('SELECT MAX(trade_date) FROM daily_quotes').fetchone()[0]
        rows = db.execute('SELECT COUNT(*) FROM daily_quotes').fetchone()[0]
        master = db.execute('SELECT COUNT(*) FROM security_master').fetchone()[0]
        counts = dict(db.execute('SELECT trade_date, COUNT(*) FROM daily_quotes WHERE trade_date>=? GROUP BY trade_date', (previous,)))
        invalid = db.execute('''SELECT COUNT(*) FROM daily_quotes WHERE trade_date=? AND
            (open IS NULL OR close IS NULL OR high IS NULL OR low IS NULL OR volume IS NULL
            OR open<=0 OR close<=0 OR low<=0 OR high<low OR volume<0
            OR high+0.000001<MAX(open,close) OR low-0.000001>MIN(open,close))''', (target,)).fetchone()[0]
    missing = []
    day = previous
    while day < target:
        day = adjacent(day, 1)
        if day <= target and counts.get(day, 0) == 0:
            missing.append(day)
    quotes = counts.get(target, 0)
    blockers = []
    if latest != target: blockers.append('latest_date_mismatch')
    if rows < previous_rows: blockers.append('historical_row_loss')
    if missing: blockers.append('missing_incremental_sessions')
    if master < 5000 or quotes < 5000 or quotes / max(master, 1) < .95:
        blockers.append('vendor_quote_coverage_below_engineering_threshold')
    if invalid: blockers.append('invalid_base_ohlcv')
    result = {'kind': 'engineering_quality_gate', 'generated_at': c.stamp(),
              'target_trade_date': target, 'latest_trade_date': latest,
              'daily_rows': rows, 'previous_daily_rows': previous_rows,
              'vendor_master_securities': master, 'base_quotes': quotes,
              'vendor_coverage': quotes/max(master, 1), 'invalid_ohlcv': invalid,
              'missing_incremental_sessions': missing, 'blockers': blockers,
              'engineering_ready': not blockers, 'formal_data_ready': False,
              'model_ready': False, 'threshold_note': '5000 quotes and 95% vendor coverage; not official universe acceptance'}
    if blockers:
        raise ValueError('engineering quality failed: ' + json.dumps(result, ensure_ascii=False))
    return result


def route(root, catchup=False):
    root = Path(root)
    if catchup:
        values = json.loads((root/'catalog.json').read_text())['datasets']
        latest = max(values, key=lambda x: (x['base_trade_date'], x['original_finished_at']))
        day = latest['base_trade_date']
        if day > c.now().date().isoformat() or not context(day)['is_session']:
            raise ValueError('invalid catch-up target')
        return {'has_quotes': True, 'target_day': day, 'mode': 'archived_catchup'}
    status = json.loads((root/'run_status.json').read_text())
    if status.get('status') == 'SKIPPED_MARKET_CLOSED':
        return {'has_quotes': False, 'target_day': status['date'], 'mode': 'market_closed'}
    acceptance = json.loads((root/'source_acceptance.json').read_text())
    if acceptance.get('daily_quote_count', 0) <= 0 or status.get('actual_new_quotes', 0) <= 0:
        raise ValueError('no verified close quotes; do not start preparation')
    day = acceptance['calendar_upper_bound']
    if day != status['date'] or not context(day)['is_session']:
        raise ValueError('collection target mismatch')
    return {'has_quotes': True, 'target_day': day, 'mode': 'new_collection'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True)
    p.add_argument('--catchup', action='store_true')
    a = p.parse_args()
    value = route(a.root, a.catchup)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            for key, val in value.items():
                stream.write(f'{key}={str(val).lower() if isinstance(val, bool) else val}\n')
    print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    main()
