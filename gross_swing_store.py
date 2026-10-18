#!/usr/bin/env python3
"""Publish isolated gross-swing research bundles; never publish trading approval."""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

import collector as c
from pipeline_state import (BRANCH, HANDOFF_ID, MARKET_ID, pointer as data_pointer,
                            ref, run, verify_file, write)

POINTER = 'state/gross-swing-current.json'
SCHEMA = 'gross-swing-research-store-v2'
FILES = ['summary.json', 'protocol.json', 'predictions.json', 'model.joblib',
         'diagnostic_trades.csv.gz', 'prediction_inputs.csv.gz', 'report.html',
         'source_manifest.json']
MAX_SUMMARY_BYTES = 100_000
OBJECTIVE = 'gross_profit_3_to_6_percent'


def pointer(repo, timeout=15):
    """A missing optional research pointer is distinct from an access failure."""
    try:
        result = subprocess.run(
            ['gh', 'api', f'repos/{repo}/contents/{POINTER}?ref={BRANCH}'],
            check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        if 'HTTP 404' in (exc.stderr or ''):
            return None, None
        raise
    payload = json.loads(result.stdout)
    return json.loads(base64.b64decode(payload['content'])), payload['sha']


def _research_only(payload):
    for key in ('formal_trade_ready', 'formal_prediction_allowed',
                'execution_win_rate_80_validated'):
        if payload.get(key) is not False:
            raise ValueError('research cannot grant formal admission: ' + key)
    if (payload.get('real_execution_win_rate') is not None or
            payload.get('actual_win_rate') is not None):
        raise ValueError('daily-bar research cannot establish real execution win rate')
    if payload.get('objective') != OBJECTIVE or payload.get('costs_included') is not False:
        raise ValueError('gross research objective/cost convention mismatch')
    if not isinstance(payload.get('gross_target'), dict) or not payload['gross_target']:
        raise ValueError('missing explicit gross research target')


def _definition_hash(protocol):
    definition = protocol.get('strategy_definition')
    if not isinstance(definition, dict) or not definition:
        raise ValueError('missing frozen static strategy definition')
    if definition.get('objective') != OBJECTIVE or definition.get('costs_included') is not False:
        raise ValueError('static strategy definition is not the gross objective')
    encoded = json.dumps(definition, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _timestamp(value):
    timestamp = dt.datetime.fromisoformat(value)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError('research timestamps must include a timezone')
    return timestamp


def _forward_timing(manifest, summary, *, published=False):
    base = dt.date.fromisoformat(manifest['base_trade_date'])
    target = dt.date.fromisoformat(manifest['target_trade_date'])
    if target <= base or summary.get('target_trade_date') != target.isoformat():
        raise ValueError('research prediction target date mismatch')
    deadline = dt.datetime.combine(target, dt.time(9, 30), c.TZ)
    generated = _timestamp(summary['generated_at'])
    if summary.get('generated_before_target_open') is not (generated < deadline):
        raise ValueError('research generation forward qualification mismatch')
    if published or 'forward_eligible' in manifest:
        readback = _timestamp(manifest['assets_readback_verified_at'])
        if readback < generated:
            raise ValueError('asset readback precedes research generation')
        expected = generated < deadline and readback < deadline
        if manifest.get('forward_eligible') is not expected:
            raise ValueError('research publication forward qualification mismatch')


def verify_bundle(root, manifest):
    root = Path(root).resolve()
    if manifest.get('schema_version') != SCHEMA or sorted(manifest['files']) != sorted(FILES):
        raise ValueError('research artifact set/schema mismatch')
    for name, item in manifest['files'].items():
        if item['name'] != name or Path(name).name != name:
            raise ValueError('research asset filename mismatch')
        verify_file(root / name, item)
    if (root / 'summary.json').stat().st_size >= MAX_SUMMARY_BYTES:
        raise ValueError('research summary must remain below 100 KB')
    summary = json.loads((root / 'summary.json').read_text(encoding='utf-8'))
    predictions = json.loads((root / 'predictions.json').read_text(encoding='utf-8'))
    protocol = json.loads((root / 'protocol.json').read_text(encoding='utf-8'))
    source = json.loads((root / 'source_manifest.json').read_text(encoding='utf-8'))
    for payload in (summary, predictions):
        _research_only(payload)
        if payload.get('base_trade_date') != manifest['base_trade_date']:
            raise ValueError('research base date mismatch')
        if payload.get('input_database_sha256') != manifest['input_database_sha256']:
            raise ValueError('research source hash mismatch')
        if payload.get('target_trade_date') != manifest['target_trade_date']:
            raise ValueError('research prediction target date mismatch')
    if summary != manifest['summary']:
        raise ValueError('research pointer summary differs from saved artifact')
    if any(summary.get(key) != predictions.get(key) for key in
           ('generated_at', 'generated_before_target_open', 'strategy_definition_sha256')):
        raise ValueError('research frozen prediction identity mismatch')
    if (source['release_tag'] != manifest['snapshot_release'] or
            source['database']['sha256'] != manifest['input_database_sha256'] or
            source['base_trade_date'] != manifest['base_trade_date'] or
            source.get('preparation_ready') is not True or
            source.get('market_file_id') != MARKET_ID or source.get('handoff_file_id') != HANDOFF_ID or
            source.get('next_trade_date') != manifest['target_trade_date']):
        raise ValueError('research snapshot lineage mismatch')
    if manifest.get('protocol_sha256') != manifest['files']['protocol.json']['sha256']:
        raise ValueError('research protocol hash mismatch')
    if (summary.get('protocol_sha256') != manifest['protocol_sha256'] or
            predictions.get('protocol_sha256') != manifest['protocol_sha256'] or
            summary.get('protocol') != protocol):
        raise ValueError('research protocol differs across artifacts')
    if not isinstance(protocol, dict) or not protocol:
        raise ValueError('missing frozen research protocol')
    definition_sha = _definition_hash(protocol)
    if summary['gross_target'] != protocol['strategy_definition'].get('gross_target'):
        raise ValueError('research gross target differs from static definition')
    for payload in (manifest, summary, predictions):
        if payload.get('strategy_definition_sha256') != definition_sha:
            raise ValueError('research static definition hash mismatch')
        if payload.get('gross_target') != summary['gross_target']:
            raise ValueError('research gross target differs across artifacts')
    if predictions.get('formal_candidates') != []:
        raise ValueError('research must not output formally approved candidates')
    rows = predictions.get('rows')
    if not isinstance(rows, list) or len({r['symbol'] for r in rows}) != len(rows):
        raise ValueError('invalid research prediction rows/duplicates')
    for row in rows:
        for key in ('p_profit', 'p_ge3', 'p_ge6', 'p_day1_green'):
            probability = row.get(key)
            if type(probability) not in (int, float) or not 0 <= probability <= 1:
                raise ValueError('invalid research probability/signal')
        if (not row['p_ge6'] <= row['p_ge3'] <= row['p_profit'] or
                any(type(row.get(key)) is not bool for key in
                    ('research_signal', 'shadow_signal', 'policy_signal')) or
                row.get('formal_trade_ready') is not False):
            raise ValueError('invalid research probability/signal')
    _research_only(manifest)
    _forward_timing(manifest, summary)
    return summary, predictions


def restore(repo, destination, release_tag=None):
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=False)
    if release_tag:
        run('gh', 'release', 'download', release_tag, '--repo', repo,
            '--pattern', 'gross_swing_manifest.json', '--dir', str(root))
        manifest = json.loads((root / 'gross_swing_manifest.json').read_text(encoding='utf-8'))
    else:
        manifest, _ = pointer(repo)
        if not manifest:
            raise ValueError('no published gross-swing research')
    if release_tag and manifest['release_tag'] != release_tag:
        raise ValueError('research release identity mismatch')
    for name in FILES:
        run('gh', 'release', 'download', manifest['release_tag'], '--repo', repo,
            '--pattern', name, '--dir', str(root))
    verify_bundle(root, manifest)
    _forward_timing(manifest, manifest['summary'], published=True)
    write(root / 'gross_swing_restore.json', {
        'manifest': manifest, 'verified': True, 'restored_at': c.stamp()})
    return manifest


def publish(repo, output, store, run_id):
    out, store = Path(output).resolve(), Path(store).resolve()
    restored = json.loads((store / 'restore.json').read_text(encoding='utf-8'))
    source = restored['manifest']
    if restored.get('verified') is not True or source.get('preparation_ready') is not True:
        raise ValueError('research requires a verified prepared snapshot')
    verify_file(store / source['database']['name'], source['database'])
    write(out / 'source_manifest.json', source)
    summary = json.loads((out / 'summary.json').read_text(encoding='utf-8'))
    old, blob = pointer(repo)
    current, source_blob = data_pointer(repo)
    if (current['release_tag'] != source['release_tag'] or
            current['database']['sha256'] != source['database']['sha256']):
        raise ValueError('data snapshot advanced; stale research pointer refused')
    tag = f"gross-swing-research-{source['base_trade_date']}-{run_id}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    manifest = {
        'schema_version': SCHEMA, 'release_tag': tag,
        'base_trade_date': source['base_trade_date'],
        'target_trade_date': source['next_trade_date'],
        'snapshot_release': source['release_tag'],
        'input_database_sha256': source['database']['sha256'],
        'github_run_id': str(run_id), 'github_commit': os.environ.get('GITHUB_SHA'),
        'github_run_url': f'https://github.com/{repo}/actions/runs/{run_id}',
        'generated_at': c.stamp(), 'formal_trade_ready': False,
        'formal_prediction_allowed': False, 'execution_win_rate_80_validated': False,
        'real_execution_win_rate': None, 'actual_win_rate': None, 'summary': summary,
        'objective': OBJECTIVE, 'gross_target': summary.get('gross_target'), 'costs_included': False,
        'files': {name: ref(out / name) for name in FILES},
        'parent_pointer_blob_sha': blob, 'data_pointer_blob_sha': source_blob,
    }
    if summary.get('target_trade_date') != source['next_trade_date']:
        raise ValueError('research prediction target differs from source next session')
    deadline = dt.datetime.combine(dt.date.fromisoformat(source['next_trade_date']), dt.time(9, 30), c.TZ)
    manifest.update(strategy_definition_sha256=summary.get('strategy_definition_sha256'),
                    forward_evidence_scope='frozen_research_predictions_only_not_broker_execution')
    manifest['protocol_sha256'] = manifest['files']['protocol.json']['sha256']
    verify_bundle(out, manifest)
    run('gh', 'release', 'create', tag, '--repo', repo,
        '--target', os.environ.get('GITHUB_SHA', 'main'), '--latest=false',
        '--title', f"Gross swing strategy research {source['base_trade_date']}",
        '--notes', 'Daily-bar gross-swing research. No broker execution, formal 80% proof, orders or email.')
    run('gh', 'release', 'upload', tag, *[str(out / name) for name in FILES], '--repo', repo)
    with tempfile.TemporaryDirectory(prefix='gross-swing-assets-') as temporary:
        run('gh', 'release', 'download', tag, '--repo', repo, '--dir', temporary)
        verify_bundle(temporary, manifest)
    manifest['assets_readback_verified_at'] = c.stamp()
    manifest['forward_eligible'] = bool(summary.get('generated_before_target_open') and
        dt.datetime.fromisoformat(manifest['assets_readback_verified_at']) < deadline)
    _forward_timing(manifest, summary, published=True)
    write(out / 'gross_swing_manifest.json', manifest)
    run('gh', 'release', 'upload', tag, str(out / 'gross_swing_manifest.json'), '--repo', repo)
    with tempfile.TemporaryDirectory(prefix='gross-swing-manifest-') as temporary:
        run('gh', 'release', 'download', tag, '--repo', repo,
            '--pattern', 'gross_swing_manifest.json', '--dir', temporary)
        if json.loads((Path(temporary) / 'gross_swing_manifest.json').read_text(encoding='utf-8')) != manifest:
            raise ValueError('research manifest readback differs')
    _, current_blob = pointer(repo)
    latest, latest_source_blob = data_pointer(repo)
    if current_blob != blob or latest_source_blob != source_blob or latest != current:
        raise ValueError('data/research pointer advanced; immutable release retained without pointer replacement')
    request = {
        'message': f'Publish verified gross-swing research {tag}', 'branch': BRANCH,
        'content': base64.b64encode((out / 'gross_swing_manifest.json').read_bytes()).decode()}
    if blob:
        request['sha'] = blob
    write(out / 'gross-swing-pointer-request.json', request)
    run('gh', 'api', '--method', 'PUT', f'repos/{repo}/contents/{POINTER}',
        '--input', str(out / 'gross-swing-pointer-request.json'))
    actual, _ = pointer(repo)
    if actual != manifest:
        raise ValueError('research pointer readback differs')
    write(out / 'published_gross_swing_manifest.json', manifest)
    return manifest


def attach(payload, snapshot, *, repo=None):
    """Bounded optional read for reports: never delays them for model downloads."""
    research = payload.setdefault('research', {})
    safe = {'formal_trade_ready': False, 'formal_prediction_allowed': False,
            'execution_win_rate_80_validated': False, 'real_execution_win_rate': None,
            'actual_win_rate': None, 'objective': OBJECTIVE, 'costs_included': False}
    try:
        manifest, _ = pointer(repo or os.environ.get('GITHUB_REPOSITORY', 'cluuacc-gif/ashare-research'), timeout=12)
        if not manifest:
            research['gross_swing_strategy'] = {**safe, 'status': 'unavailable'}
            return payload
        if (manifest.get('snapshot_release') != snapshot['release_tag'] or
                manifest.get('input_database_sha256') != snapshot['database']['sha256'] or
                manifest.get('base_trade_date') != snapshot['base_trade_date']):
            research['gross_swing_strategy'] = {**safe, 'status': 'snapshot_mismatch'}
            return payload
        summary = manifest['summary']
        if any(summary.get(key) != manifest.get(key) for key in
               ('base_trade_date', 'target_trade_date', 'input_database_sha256',
                'protocol_sha256', 'strategy_definition_sha256', 'gross_target')):
            raise ValueError('research summary identity mismatch')
        if len(json.dumps(summary, ensure_ascii=False).encode('utf-8')) >= MAX_SUMMARY_BYTES:
            raise ValueError('oversized research summary')
        _research_only(manifest)
        _research_only(summary)
        if _definition_hash(summary['protocol']) != manifest['strategy_definition_sha256']:
            raise ValueError('research summary static definition mismatch')
        if summary['gross_target'] != summary['protocol']['strategy_definition'].get('gross_target'):
            raise ValueError('research summary gross target differs from static definition')
        _forward_timing(manifest, summary, published=True)
        research['gross_swing_strategy'] = {
            **summary, **safe, 'status': 'research_only',
            'release_tag': manifest['release_tag'], 'github_run_url': manifest.get('github_run_url'),
            'snapshot_release': manifest['snapshot_release'],
            'forward_eligible': manifest['forward_eligible']}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        research['gross_swing_strategy'] = {
            **safe, 'status': 'unavailable', 'reason': type(exc).__name__}
    return payload


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['publish', 'restore'])
    parser.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY', 'cluuacc-gif/ashare-research'))
    parser.add_argument('--output'); parser.add_argument('--store')
    parser.add_argument('--destination'); parser.add_argument('--release-tag')
    args = parser.parse_args()
    if args.operation == 'publish':
        result = publish(args.repo, args.output, args.store, os.environ.get('GITHUB_RUN_ID', 'local'))
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
                stream.write('research_tag=' + result['release_tag'] + '\n')
    else:
        result = restore(args.repo, args.destination, args.release_tag)
    print(json.dumps({k: result[k] for k in ('release_tag', 'base_trade_date', 'snapshot_release',
                                           'execution_win_rate_80_validated')}, ensure_ascii=False))
