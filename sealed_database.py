#!/usr/bin/env python3
"""Restore a verified sealed Library snapshot into a unique SQLite workspace.

This is a filesystem recovery utility. It neither changes data-admission rules
nor authorizes any forecast. Never use a bare main file from a live WAL writer.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def inspect_sealed(path):
    path = Path(path).resolve()
    with path.open('rb') as stream:
        header = stream.read(100)
    if header[:16] != b'SQLite format 3\x00' or header[18:20] != bytes((1, 1)):
        raise ValueError('checkpointed DELETE-mode SQLite snapshot required')
    for suffix in ('-wal', '-journal'):
        sibling = Path(str(path) + suffix)
        if sibling.exists() and sibling.stat().st_size:
            raise ValueError('nonempty SQLite sidecar; acquire a fresh sealed snapshot in a new directory')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        check = db.execute('PRAGMA integrity_check').fetchone()[0]
        if check != 'ok':
            raise ValueError('SQLite integrity check failed: ' + str(check))
        return {'application_id': db.execute('PRAGMA application_id').fetchone()[0],
                'integrity_check': check, 'bytes': path.stat().st_size, 'sha256': sha256(path)}
    finally:
        db.close()


def restore(source, expected_sha256, expected_application_id):
    source = Path(source).resolve()
    if sha256(source) != expected_sha256:
        raise ValueError('source differs from current saved version')
    before = inspect_sealed(source)
    if before['application_id'] != expected_application_id:
        raise ValueError('database identity mismatch')
    work = Path(tempfile.mkdtemp(prefix='ashare_sealed_'))
    destination = work / source.name
    try:
        shutil.copy2(source, destination)
        after = inspect_sealed(destination)
        if after != before or sha256(source) != expected_sha256:
            raise ValueError('source changed while restoring or copy differs')
        return {'working_database': str(destination), 'source': str(source),
                'restoration_verified': True, **after}
    except Exception:
        shutil.rmtree(work)
        raise


def export(source, destination, expected_application_id):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError('use a new destination; do not overwrite an active database')
    before = inspect_sealed(source)
    if before['application_id'] != expected_application_id:
        raise ValueError('database identity mismatch')
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    after = inspect_sealed(destination)
    if before != after:
        raise ValueError('export differs from sealed source')
    return {'exported_database': str(destination), 'reopened_verified': True, **after}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('restore', 'inspect', 'export'))
    parser.add_argument('--source', required=True)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--application-id', type=int, default=1095977041)
    parser.add_argument('--destination')
    args = parser.parse_args()
    if args.operation == 'restore':
        if not args.expected_sha256:
            parser.error('restore requires the current saved SHA256')
        result = restore(args.source, args.expected_sha256, args.application_id)
    elif args.operation == 'export':
        if not args.destination:
            parser.error('export requires a new destination')
        result = export(args.source, args.destination, args.application_id)
    else:
        result = inspect_sealed(args.source)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
