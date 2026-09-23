#!/usr/bin/env python3
"""Engineering acceptance with real archived quotes, never synthetic prices.

The generated SQLite file is an isolated validation fixture, not the user's
canonical database. No prediction, probability, or DATA READY may result.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

import collector as c
from compact_quote_provenance import migrate
from recover_evening_pipeline import entries, decode, prepare_current, write_json
from sealed_database import inspect_sealed, sha256

ROOT = Path(__file__).resolve().parent


def run(data_root, output):
    rows = entries(data_root)[-2:]
    if len(rows) < 2 or rows[0]['base_trade_date'] == rows[1]['base_trade_date']:
        raise ValueError('two distinct real session archives required')
    with tempfile.TemporaryDirectory(prefix='ashare_real_validation_') as folder:
        root = Path(folder)
        seed = root / 'validation_input.sqlite3'
        # Empty schema is only a test container; all records come from raw replay.
        db = c.Database(seed); db.close()
        with sqlite3.connect(seed) as db: db.execute('PRAGMA journal_mode=DELETE')
        first = decode(data_root, rows[0], root / 'seed.zip')
        command = [sys.executable, str(ROOT / 'import_probe_artifact.py'), first['archive'],
                   '--db', str(seed), '--evidence-root', str(root / 'seed_evidence'),
                   '--source-ref', 'https://github.com/cluuacc-gif/ashare-research/blob/data/evening/' + rows[0]['index_path'],
                   '--expected-db-sha256', sha256(seed)]
        subprocess.run(command, check=True, capture_output=True, text=True)
        before = inspect_sealed(seed)
        selected = root / 'transport'; selected.mkdir()
        write_json(selected / 'catalog.json', {'schema_version': '1.0', 'datasets': rows})
        # Only immutable archived files are exposed to the offline importer.
        for row in rows:
            index_path = Path(data_root) / row['index_path']
            index = json.loads(index_path.read_text())
            for relative in [row['index_path']] + [v['path'] for v in index['files']]:
                destination = selected / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((Path(data_root) / relative).read_bytes())
        result = prepare_current(seed, before['sha256'], selected, root / 'prepared')
        candidate = Path(result['working_database']['path'])
        with sqlite3.connect(candidate.as_uri() + '?mode=ro', uri=True) as db:
            actual = dict(db.execute('SELECT trade_date,COUNT(*) FROM daily_quotes GROUP BY trade_date'))
            for row in rows:
                if actual[row['base_trade_date']] != row['quotes']:
                    raise AssertionError('real daily row count mismatch')
        if sha256(seed) != before['sha256']:
            raise AssertionError('original validation input changed')
        if result['data_status'] != 'DATA NOT READY' or result['canonical_database_imported']:
            raise AssertionError('engineering test incorrectly granted production readiness')
        again = migrate(candidate, sha256(candidate))
        if again['changed']:
            raise AssertionError('migration not idempotent')
        repeat = prepare_current(candidate, sha256(candidate), selected, root / 'repeat')
        if not all(r.get('already_imported') for r in repeat['imports']):
            raise AssertionError('repeated import did not resume from real source log')
        if repeat['daily_rows'] != result['daily_rows']:
            raise AssertionError('repeated import changed observation count')
        broken = root / 'truncated.sqlite3'
        broken.write_bytes(candidate.read_bytes()[:-4096])
        try:
            inspect_sealed(broken)
        except ValueError:
            truncated_rejected = True
        else:
            raise AssertionError('truncated database accepted')
        report = {'kind': 'real_archived_data_engineering_acceptance', 'generated_at': c.stamp(),
                  'source_runs': [r['github_run_id'] for r in rows], 'dates_and_counts': actual,
                  'real_daily_rows': result['daily_rows'], 'raw_replay_verified': True,
                  'isolated_input_unchanged': True, 'idempotent_resume': True,
                  'truncated_file_rejected': truncated_rejected,
                  'compaction': json.loads((root / 'prepared' / 'compaction.json').read_text()),
                  'frozen_preparation': result['frozen_quality'],
                  'result': 'PASS', 'canonical_database_imported': False,
                  'full_250_history_tested': False, 'data_status': 'DATA NOT READY',
                  'model_ready': False, 'prediction_database_touched': False}
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, report)
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-root', required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    print(json.dumps(run(a.data_root, a.output), ensure_ascii=False))
