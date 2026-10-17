#!/usr/bin/env python3
"""Archive vendor supplements and append them to a later current snapshot.

Preserves observed timestamps and raw HTTP bytes. Checksums are transport
verification, not independent content review or official data acceptance.
"""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import zipfile

import collector as c
from pipeline_state import MARKET_ID, write

TABLES = {'security_status_daily': 'observed_at', 'limit_records': 'observed_at',
          'industry_members': 'observed_at', 'news_events': 'retrieved_at'}
MAX_BYTES = 100_000_000


def digest(body):
    return hashlib.sha256(body).hexdigest()


def child(root, name):
    relative = PurePosixPath(name)
    root = Path(root).resolve()
    path = root.joinpath(*relative.parts)
    if relative.is_absolute() or '..' in relative.parts or '\\' in name or not path.resolve().is_relative_to(root) or path.is_symlink():
        raise ValueError('unsafe supplement path')
    return path


def export_rows(database, started, destination, source_database, raw_responses):
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = {table: [dict(r) for r in db.execute(
            f'SELECT * FROM {table} WHERE {clock}>=?', (started,))]
                for table, clock in TABLES.items()}
        base = db.execute('SELECT MAX(trade_date) FROM daily_quotes').fetchone()[0]
    packet = {'schema_version': 'vendor-admission-1', 'market_file_id': MARKET_ID,
              'base_trade_date': base, 'generated_at': c.stamp(), 'started_at': started,
              'input_database': source_database, 'tables': rows,
              'raw_responses': raw_responses, 'official_verified': False,
              'raw_replay_verified': False, 'formal_data_ready': False}
    validate(packet)
    write(destination, packet)
    return packet


def validate(packet):
    if packet.get('schema_version') != 'vendor-admission-1' or packet.get('market_file_id') != MARKET_ID:
        raise ValueError('supplement schema or lineage mismatch')
    if packet.get('official_verified') is not False or packet.get('formal_data_ready') is not False or packet.get('raw_replay_verified') is not False:
        raise ValueError('vendor supplement cannot grant official acceptance')
    generated = c.timestamp(packet['generated_at'])
    started = c.timestamp(packet['started_at'])
    if generated > c.now() or started > generated:
        raise ValueError('future or reversed collection time')
    c.date_value(packet['base_trade_date'])
    if packet['base_trade_date'] > generated.date().isoformat():
        raise ValueError('future supplement base date')
    if set(packet['tables']) != set(TABLES):
        raise ValueError('unexpected supplement tables')
    if not any(packet['tables'].values()):
        raise ValueError('no vendor supplement rows captured')
    for table, clock in TABLES.items():
        for row in packet['tables'][table]:
            observed = c.timestamp(row[clock])
            if not started <= observed <= generated:
                raise ValueError('record observation outside actual collection')
            if table == 'news_events':
                for symbol in json.loads(row['symbols_json']):
                    c.symbol(symbol)
                if row['published_at'] and c.timestamp(row['published_at']) > observed:
                    raise ValueError('announcement published after retrieval')
            else:
                c.symbol(row['symbol'])
                day = c.date_value(row['effective_date'] if table == 'industry_members' else row['trade_date'])
                if day > observed.date().isoformat():
                    raise ValueError('future vendor record date')
            if table == 'security_status_daily' and row['verified'] != 0:
                raise ValueError('vendor status cannot become official')
            if table == 'limit_records' and row['official_verified'] != 0:
                raise ValueError('vendor limits cannot become official')
    return packet


def verify_archive(body):
    if len(body) > MAX_BYTES:
        raise ValueError('unbounded supplement archive')
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or sum(i.file_size for i in archive.infolist()) > 5 * MAX_BYTES:
            raise ValueError('duplicate or unbounded supplement content')
        for name in names:
            child(Path.cwd(), name)
        packet = validate(json.loads(archive.read('admission_rows.json')))
        for source in packet['raw_responses']:
            raw = archive.read(source['path'])
            if len(raw) != source['bytes'] or digest(raw) != source['sha256']:
                raise ValueError('raw HTTP bytes/hash mismatch')
    return packet


def publish(source, root, run_id):
    if not re.fullmatch(r'\d+', str(run_id)):
        raise ValueError('source run identity required')
    source, root = Path(source), Path(root)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in ['admission_rows.json', 'auto_intel_report.json']:
            archive.write(source / name, name)
        for path in sorted((source / 'raw').glob('*.body')):
            if path.is_symlink():
                raise ValueError('raw source symlink forbidden')
            archive.write(path, 'raw/' + path.name)
    body = buffer.getvalue()
    packet = verify_archive(body)
    sha = digest(body)
    target = root / 'admission' / (str(run_id) + '-' + sha[:16])
    target.mkdir(parents=True, exist_ok=True)
    blocks = []
    for i, start in enumerate(range(0, len(body), 512 * 1024)):
        block = body[start:start+512*1024]
        path = target / f'{i:04d}.b64'
        encoded = base64.b64encode(block) + b'\n'
        if path.exists() and path.read_bytes() != encoded:
            raise ValueError('immutable supplement conflict')
        path.write_bytes(encoded)
        blocks.append({'path': path.relative_to(root).as_posix(), 'bytes': len(block), 'sha256': digest(block)})
    item = {'base_trade_date': packet['base_trade_date'], 'generated_at': packet['generated_at'],
            'github_run_id': str(run_id), 'archive_sha256': sha, 'archive_bytes': len(body),
            'index_path': (target / 'index.json').relative_to(root).as_posix(),
            'raw_replay_verified': False, 'formal_data_ready': False}
    write(target / 'index.json', {**item, 'files': blocks})
    catalog_path = root / 'admission/catalog.json'
    catalog = json.loads(catalog_path.read_text(encoding='utf-8')) if catalog_path.exists() else {'datasets': []}
    if not any(r['archive_sha256'] == sha for r in catalog['datasets']):
        catalog['datasets'].append(item)
    catalog['datasets'].sort(key=lambda r: (r['generated_at'], r['index_path']))
    write(catalog_path, catalog)
    return item


def import_pending(database, root, target_day):
    root = Path(root)
    catalog_path = root / 'admission/catalog.json'
    if not catalog_path.is_file():
        return []
    datasets = json.loads(catalog_path.read_text(encoding='utf-8'))['datasets']
    if len(datasets) > 10000:
        raise ValueError('unbounded supplement catalog')
    results = []
    with sqlite3.connect(database) as db:
        if db.execute('PRAGMA application_id').fetchone()[0] != c.APP_ID:
            raise ValueError('canonical market identity required')
        imported = {r[0] for r in db.execute("SELECT sha256 FROM source_log WHERE operation='admission_transport_import'")}
        columns = {table: [r[1] for r in db.execute(f'PRAGMA table_info({table})')] for table in TABLES}
        for item in sorted(datasets, key=lambda r: (r['generated_at'], r['index_path'])):
            sha = item['archive_sha256']
            if sha in imported or item['base_trade_date'] > target_day:
                continue
            index = json.loads(child(root, item['index_path']).read_text(encoding='utf-8'))
            if any(index.get(k) != v for k, v in item.items()) or not 0 < item['archive_bytes'] <= MAX_BYTES:
                raise ValueError('supplement catalog/index mismatch')
            parts, size = [], 0
            seen = set()
            for block in index['files']:
                if block['path'] in seen:
                    raise ValueError('duplicate supplement block')
                seen.add(block['path'])
                raw = base64.b64decode(child(root, block['path']).read_bytes().strip(), validate=True)
                size += len(raw)
                if len(raw) != block['bytes'] or digest(raw) != block['sha256'] or size > item['archive_bytes']:
                    raise ValueError('supplement block verification failed')
                parts.append(raw)
            body = b''.join(parts)
            if size != item['archive_bytes'] or digest(body) != sha:
                raise ValueError('supplement archive verification failed')
            packet = verify_archive(body)
            if packet['base_trade_date'] != item['base_trade_date'] or packet['generated_at'] != item['generated_at']:
                raise ValueError('supplement packet/index mismatch')
            # Validate every row before opening a transaction that changes the DB.
            for table in TABLES:
                if any(set(row) != set(columns[table]) for row in packet['tables'][table]):
                    raise ValueError('supplement row schema mismatch')
            counts = {}
            with db:
                for table in TABLES:
                    before = db.total_changes
                    names = columns[table]
                    db.executemany(f"INSERT OR IGNORE INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                                   [[row[name] for name in names] for row in packet['tables'][table]])
                    counts[table] = db.total_changes - before
                db.execute('INSERT INTO source_log(run_id,source,operation,fetched_at,status,snapshot_ref,sha256,bytes) VALUES(?,?,?,?,?,?,?,?)',
                           (item['github_run_id'], 'vendor_admission_archive', 'admission_transport_import', c.stamp(),
                            'success', item['index_path'], sha, len(body)))
            imported.add(sha)
            results.append({'archive_sha256': sha, 'counts': counts, 'raw_http_responses': len(packet['raw_responses']),
                            'raw_replay_verified': False, 'formal_data_ready': False})
        db.execute('PRAGMA journal_mode=DELETE')
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', required=True)
    parser.add_argument('--data-root', required=True)
    args = parser.parse_args()
    print(json.dumps(publish(args.source_dir, args.data_root, os.environ['GITHUB_RUN_ID'])))
