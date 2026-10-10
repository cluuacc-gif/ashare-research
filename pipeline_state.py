#!/usr/bin/env python3
"""Verified GitHub snapshot store. Immutable assets first; one CAS pointer last."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile

import collector as c
from sealed_database import inspect_sealed, sha256

POINTER = 'state/current.json'
BRANCH = 'data/evening'
MARKET_ID = 'libfile_d756624099288191b2687f1948b17c94'
HANDOFF_ID = 'libfile_d5f3ecbb7d448191adaa8f1cbf66e977'
SEED = {
    'release_tag': 'canonical-store-v5',
    'database': {'name': 'market-database-v9-auto-intel.sqlite3', 'bytes': 891887616,
                 'sha256': '05aaf6d04245259ff35e4511179b17a6195ca2a72ca2d9ff0f0ec83aec0561a5'},
    'handoff': {'name': 'handoff-v8-admission.json', 'bytes': 54234,
                'sha256': 'd2750ebffaf69b004cd37ae2fe048cdb82460045abff9fd5e7adf40fd053c4f4'},
    'seed_only': True, 'data_status': 'DATA NOT READY', 'model_ready': False,
}


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def ref(path):
    path = Path(path)
    return {'name': path.name, 'bytes': path.stat().st_size, 'sha256': sha256(path)}


def verify_file(path, item):
    if Path(path).stat().st_size != item['bytes'] or sha256(path) != item['sha256']:
        raise ValueError('saved asset bytes/hash mismatch: ' + item['name'])


def pointer(repo):
    # Missing pointer may bootstrap ONLY from the explicitly hashed migration seed.
    # Authorization/network errors must never silently become a seed fallback.
    try:
        result = run('gh', 'api', f'repos/{repo}/contents/{POINTER}?ref={BRANCH}')
    except subprocess.CalledProcessError as exc:
        if 'HTTP 404' in (exc.stderr or ''):
            return dict(SEED), None
        raise
    payload = json.loads(result)
    value = json.loads(base64.b64decode(payload['content']))
    if value.get('market_file_id') != MARKET_ID or value.get('handoff_file_id') != HANDOFF_ID:
        raise ValueError('snapshot lineage differs from original project')
    return value, payload['sha']


def restore(repo, destination, release_tag=None):
    out = Path(destination).resolve()
    out.mkdir(parents=True, exist_ok=False)
    if release_tag:
        run('gh', 'release', 'download', release_tag, '--repo', repo,
            '--pattern', 'manifest.json', '--dir', str(out))
        current, blob = json.loads((out/'manifest.json').read_text()), None
        if current.get('release_tag') != release_tag or current.get('market_file_id') != MARKET_ID or current.get('handoff_file_id') != HANDOFF_ID:
            raise ValueError('pinned release lineage mismatch')
    else:
        current, blob = pointer(repo)
    required = ['database', 'handoff']
    if not current.get('seed_only'):
        required += ['frozen', 'quality']
    for key in required:
        item = current[key]
        if Path(item['name']).name != item['name']:
            raise ValueError('invalid asset filename')
        run('gh', 'release', 'download', current['release_tag'], '--repo', repo,
            '--pattern', item['name'], '--dir', str(out))
        verify_file(out / item['name'], item)
    sealed = inspect_sealed(out / current['database']['name'])
    if sealed['application_id'] != c.APP_ID:
        raise ValueError('market database identity mismatch')
    if not current.get('seed_only'):
        with sqlite3.connect((out/current['database']['name']).as_uri()+'?mode=ro', uri=True) as db:
            latest = db.execute('SELECT MAX(trade_date) FROM daily_quotes').fetchone()[0]
        handoff = json.loads((out/current['handoff']['name']).read_text())
        if latest != current['base_trade_date'] or handoff.get('latest_imported_trade_date') != latest:
            raise ValueError('manifest/database/handoff date mismatch')
    write(out / 'restore.json', {'pointer_blob_sha': blob, 'manifest': current,
                                'restored_at': c.stamp(), 'verified': True})
    return out


def quality_report(prep):
    q = prep['complete_quality']
    lines = [f"# A股行情数据质量报告 {prep['base_trade_date']}", '',
             '**DATA NOT READY**；已更新真实行情及描述性冻结数据，正式准入与模型仍独立审查。', '',
             f"- 生成时间：{prep['generated_at']}",
             f"- 下一交易日：{prep['next_trade_date']}",
             f"- 供应商证券数：{q.get('vendor_master_securities')}",
             f"- 当日报价：{prep['base_quotes']}；官方应有分母：未知",
             f"- 历史行数：{prep['daily_rows']}；至少250根：{q.get('history_250_symbols')}",
             f"- 描述性因子：{q.get('descriptive_features_count')}",
             f"- 完整20日成交额因子：{q.get('full_amount20_features')}",
             '- 价格观察范围不是正式候选池；官方状态、限价、时点行业/复权、事件完整性及模型门未通过。',
             '- 原预测数据库未修改；历史回测不计为事前预测；未验证80%净盈利胜率。']
    return '\n'.join(lines) + '\n'


def publish(repo, restored, output, run_id):
    restored, out = Path(restored), Path(output)
    previous = json.loads((restored / 'restore.json').read_text())
    prep = json.loads((out / 'preparation.json').read_text())
    prep['complete_quality'] = json.loads((out / 'frozen' / 'quality.json').read_text())
    day = prep['base_trade_date']
    database = Path(prep['working_database']['path'])
    sealed = inspect_sealed(database)
    if sealed['sha256'] != prep['working_database']['sha256']:
        raise ValueError('working database changed after preparation')
    if not prep['engineering_quality']['engineering_ready']:
        raise ValueError('engineering quality must pass before publication')
    tag = f"prepared-{day}-{run_id}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    # Copy to fixed names in this new release; original versions are never deleted.
    import shutil
    asset_dir = out / 'published'
    asset_dir.mkdir(exist_ok=False)
    shutil.copy2(database, asset_dir / 'market.sqlite3')
    shutil.copy2(out / 'frozen_inputs.zip', asset_dir / 'frozen.zip')
    write(asset_dir / 'quality.json', prep['complete_quality'])
    write(asset_dir / 'engineering_quality.json', prep['engineering_quality'])
    (asset_dir / 'quality.md').write_text(quality_report(prep), encoding='utf-8')
    handoff = json.loads((restored / previous['manifest']['handoff']['name']).read_text(encoding='utf-8-sig'))
    # The migration export keeps original IDs and existing history/success pointer.
    # It does not pretend to replace a ChatGPT Library version.
    attempt = {'target_trade_date': day, 'next_trade_date': prep['next_trade_date'],
               'generated_at': prep['generated_at'], 'persisted_at': c.stamp(),
               'data_status': 'DATA NOT READY', 'model_ready': False,
               'canonical_database_imported': True, 'daily_rows': prep['daily_rows'],
               'base_quotes': prep['base_quotes'], 'github_run_id': str(run_id),
               'storage_backend': 'github_immutable_release', 'release_tag': tag,
               'database': {**ref(asset_dir / 'market.sqlite3'), 'file_id': MARKET_ID},
               'frozen': ref(asset_dir / 'frozen.zip'), 'quality': ref(asset_dir / 'quality.json')}
    handoff.setdefault('attempt_history', []).append(attempt)
    handoff.update(latest_attempt=attempt, status='DATA NOT READY', bootstrap_complete=False,
                   model_ready=False, latest_imported_trade_date=day)
    handoff['latest_prepared_dataset'] = attempt
    handoff.setdefault('latest_successful_dataset', None)
    handoff['storage_migration'] = {'market_file_id': MARKET_ID, 'handoff_file_id': HANDOFF_ID,
                                    'backend': 'github_immutable_release',
                                    'library_version_replaced': False}
    write(asset_dir / 'handoff.json', handoff)
    manifest = {'schema_version': '2.0', 'market_file_id': MARKET_ID,
                'handoff_file_id': HANDOFF_ID, 'release_tag': tag, 'base_trade_date': day,
                'next_trade_date': prep['next_trade_date'], 'generated_at': prep['generated_at'],
                'published_at': c.stamp(), 'github_run_id': str(run_id),
                'github_commit': os.environ.get('GITHUB_SHA'), 'data_status': 'DATA NOT READY',
                'model_ready': False, 'preparation_ready': True,
                'formal_data_ready': False, 'engineering_quality': ref(asset_dir / 'engineering_quality.json'),
                'daily_rows': prep['daily_rows'], 'base_quotes': prep['base_quotes'],
                'database': ref(asset_dir / 'market.sqlite3'), 'handoff': ref(asset_dir / 'handoff.json'),
                'frozen': ref(asset_dir / 'frozen.zip'), 'quality': ref(asset_dir / 'quality.json'),
                'quality_markdown': ref(asset_dir / 'quality.md'),
                'parent_pointer_blob_sha': previous['pointer_blob_sha']}
    write(asset_dir / 'manifest.json', manifest)
    run('gh', 'release', 'create', tag, '--repo', repo, '--target', os.environ.get('GITHUB_SHA', 'main'),
        '--title', f'Prepared real quotes {day}', '--notes', 'Immutable research preparation. Formal data/model gates remain false.')
    run('gh', 'release', 'upload', tag, *map(str, asset_dir.iterdir()), '--repo', repo)
    # Independent readback of all assets BEFORE moving the sole current pointer.
    with tempfile.TemporaryDirectory(prefix='ashare-readback-') as temporary:
        run('gh', 'release', 'download', tag, '--repo', repo, '--dir', temporary)
        for item in [ref(p) for p in asset_dir.iterdir()]:
            verify_file(Path(temporary) / item['name'], item)
        inspect_sealed(Path(temporary) / 'market.sqlite3')
    current, blob = pointer(repo)
    if blob != previous['pointer_blob_sha']:
        raise ValueError('current snapshot changed concurrently; immutable assets kept, pointer not replaced')
    payload = {'message': f'Publish verified preparation {day} run {run_id}', 'branch': BRANCH,
               'content': base64.b64encode((asset_dir / 'manifest.json').read_bytes()).decode()}
    if blob:
        payload['sha'] = blob
    request = asset_dir / 'pointer-request.json'
    write(request, payload)
    run('gh', 'api', '--method', 'PUT', f'repos/{repo}/contents/{POINTER}', '--input', str(request))
    verified, _ = pointer(repo)
    if verified != manifest:
        raise ValueError('published pointer readback differs')
    write(out / 'published_manifest.json', manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['restore', 'publish'])
    parser.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY', 'cluuacc-gif/ashare-research'))
    parser.add_argument('--destination')
    parser.add_argument('--restored')
    parser.add_argument('--output')
    parser.add_argument('--run-id', default=os.environ.get('GITHUB_RUN_ID'))
    parser.add_argument('--release-tag')
    args = parser.parse_args()
    if args.operation == 'restore':
        path = restore(args.repo, args.destination, args.release_tag)
        if os.environ.get('GITHUB_ENV'):
            saved = json.loads((path / 'restore.json').read_text())['manifest']
            with open(os.environ['GITHUB_ENV'], 'a') as stream:
                stream.write(f"DB={path / saved['database']['name']}\nHO={path / saved['handoff']['name']}\n")
    else:
        result = publish(args.repo, args.restored, args.output, args.run_id)
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
                stream.write(f"release_tag={result['release_tag']}\ntarget_day={result['base_trade_date']}\n")
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
