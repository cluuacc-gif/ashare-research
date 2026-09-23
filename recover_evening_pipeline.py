#!/usr/bin/env python3
"""Replay pending immutable evening artifacts into an isolated current DB copy.

No network or prediction. The caller must restore the current canonical file,
persist all outputs with version checks, then update the original handoff.
An unavailable current DB is a hard stop, never an implicit new database.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import sys
import zipfile

import collector as c
from compact_quote_provenance import migrate
from import_probe_artifact import verify
from prepare_frozen_inputs import prepare
from sealed_database import inspect_sealed, sha256
from trade_calendar import context, adjacent

ROOT = Path(__file__).resolve().parent


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def safe_child(root, value):
    relative = PurePosixPath(value)
    if relative.is_absolute() or '..' in relative.parts or '\\' in value:
        raise ValueError('unsafe evidence path')
    path = Path(root).resolve().joinpath(*relative.parts)
    if not path.resolve().is_relative_to(Path(root).resolve()) or path.is_symlink():
        raise ValueError('evidence path escapes transport root')
    return path


def decode(root, entry, destination):
    """Verify every block and the original archive before exposing its bytes."""
    index = json.loads(safe_child(root, entry['index_path']).read_text())
    for key in ('base_trade_date', 'archive_sha256', 'archive_bytes', 'github_run_id', 'collector_sha256'):
        if index[key] != entry[key]:
            raise ValueError('catalog/index mismatch: ' + key)
    if index['archive_bytes'] > 100_000_000:
        raise ValueError('bounded evidence archive required')
    if not context(index['base_trade_date'])['is_session']:
        raise ValueError('quote date is not an exchange session')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix('.partial')
    if destination.exists() or partial.exists():
        raise ValueError('new archive path required')
    seen, h, size = set(), hashlib.sha256(), 0
    try:
        with partial.open('xb') as stream:
            for block in index['files']:
                if block['path'] in seen:
                    raise ValueError('duplicate transport block')
                seen.add(block['path'])
                raw = base64.b64decode(safe_child(root, block['path']).read_bytes().strip(), validate=True)
                if len(raw) != block['bytes'] or hashlib.sha256(raw).hexdigest() != block['sha256']:
                    raise ValueError('transport block verification failed')
                size += len(raw)
                if size > index['archive_bytes']:
                    raise ValueError('archive exceeds declared byte count')
                stream.write(raw); h.update(raw)
        if size != index['archive_bytes'] or h.hexdigest() != index['archive_sha256']:
            raise ValueError('original archive byte count or SHA256 differs')
        report, _, securities, quotes = verify(partial)
        if str(report.get('github_run_id')) != str(entry['github_run_id']):
            raise ValueError('source run mismatch')
        if report['calendar_upper_bound'] != entry['base_trade_date']:
            raise ValueError('source base date mismatch')
        if len(securities) != entry['securities'] or len(quotes) != entry['quotes']:
            raise ValueError('raw replay counts differ from catalog')
        partial.rename(destination)
        return {'base_trade_date': entry['base_trade_date'], 'source_run_id': entry['github_run_id'],
                'archive': str(destination), 'sha256': h.hexdigest(), 'bytes': size,
                'securities': len(securities), 'quotes': len(quotes), 'raw_replay_verified': True}
    finally:
        if partial.exists(): partial.unlink()


def entries(root):
    rows = json.loads((Path(root) / 'catalog.json').read_text())['datasets']
    if not rows or len(rows) > 10000:
        raise ValueError('empty or unbounded transport catalog')
    digests = [r['archive_sha256'] for r in rows]
    if len(digests) != len(set(digests)):
        raise ValueError('duplicate archive in catalog')
    return sorted(rows, key=lambda r: (r['base_trade_date'], r['original_finished_at'], r['index_path']))


def prepare_current(source, expected_sha, root, output):
    source, out = Path(source).resolve(), Path(output).resolve()
    if not source.is_file() or sha256(source) != expected_sha:
        raise ValueError('current canonical database missing or hash mismatch; no replacement created')
    original = inspect_sealed(source)
    if original['application_id'] != c.APP_ID:
        raise ValueError('canonical market database identity mismatch')
    out.mkdir(parents=True, exist_ok=False)
    working = out / source.name
    shutil.copy2(source, working)
    compaction = migrate(working, expected_sha)
    write_json(out / 'compaction.json', compaction)
    with sqlite3.connect(working.as_uri() + '?mode=ro', uri=True) as db:
        imported = {r[0] for r in db.execute(
            "SELECT sha256 FROM source_log WHERE operation='verified_artifact_import'")}
    records = []
    for entry in entries(root):
        if entry['archive_sha256'] in imported:
            records.append({'base_trade_date': entry['base_trade_date'], 'sha256': entry['archive_sha256'], 'already_imported': True})
            continue
        recovered = decode(root, entry, out / 'archives' / (str(entry['github_run_id']) + '.zip'))
        command = [sys.executable, str(ROOT / 'import_probe_artifact.py'), recovered['archive'],
                   '--db', str(working), '--evidence-root', str(out / 'evidence'), '--source-ref',
                   'https://github.com/cluuacc-gif/ashare-research/blob/data/evening/' + entry['index_path']]
        subprocess.run(command + ['--verify-only'], check=True, capture_output=True, text=True)
        result = subprocess.run(command + ['--expected-db-sha256', sha256(working)],
                                check=True, capture_output=True, text=True)
        records.append(json.loads(result.stdout))
        write_json(out / 'import_progress.json', records)
    sealed = inspect_sealed(working)
    with sqlite3.connect(working.as_uri() + '?mode=ro', uri=True) as db:
        base = db.execute('SELECT MAX(trade_date) FROM daily_quotes').fetchone()[0]
        inventory = dict(db.execute('SELECT trade_date,COUNT(*) FROM daily_quotes GROUP BY trade_date'))
        total = db.execute('SELECT COUNT(*) FROM daily_quotes').fetchone()[0]
    latest = entries(root)[-1]['base_trade_date']
    if base != latest:
        raise ValueError('working database latest date differs from latest transport')
    frozen = prepare(working, base, adjacent(base, 1), sealed['sha256'], out / 'frozen')
    with zipfile.ZipFile(out / 'frozen_inputs.zip', 'x', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((out / 'frozen').rglob('*')):
            if path.is_file(): archive.write(path, path.relative_to(out / 'frozen'))
    if sha256(source) != expected_sha:
        raise ValueError('canonical input changed during isolated preparation')
    result = {'schema_version': '1.0', 'kind': 'evening_import_preparation',
              'generated_at': c.stamp(), 'base_trade_date': base, 'next_trade_date': adjacent(base, 1),
              'input_database': original, 'working_database': {**sealed, 'path': str(working)},
              'source_database_unchanged': True, 'daily_rows': total, 'base_quotes': inventory[base],
              'imports': records, 'frozen_quality': frozen,
              'bundle': {'path': str(out / 'frozen_inputs.zip'), 'sha256': sha256(out / 'frozen_inputs.zip')},
              'canonical_database_imported': False, 'persistence_pending': True,
              'data_status': 'DATA NOT READY', 'model_ready': False, 'prediction_database_touched': False}
    write_json(out / 'preparation.json', result)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', required=True)
    p.add_argument('--expected-db-sha256', required=True)
    p.add_argument('--data-root', required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    print(json.dumps(prepare_current(a.db, a.expected_db_sha256, a.data_root, a.output), ensure_ascii=False))
