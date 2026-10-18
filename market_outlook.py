#!/usr/bin/env python3
"""Freeze sector/opening research from a verified snapshot; never send or trade."""
from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from contextlib import closing

import collector as c
import pipeline_state as state
from trade_calendar import context

POINTER = 'state/market-outlook-current.json'
ASSETS = ('outlook.json', 'report.html', 'source_manifest.json')


def read_pointer(repo):
    try:
        raw = json.loads(state.run('gh', 'api', f'repos/{repo}/contents/{POINTER}?ref={state.BRANCH}'))
        return json.loads(base64.b64decode(raw['content'])), raw['sha']
    except subprocess.CalledProcessError as exc:
        if 'HTTP 404' in (exc.stderr or ''):
            return None, None
        raise


def compact_pointer(manifest):
    """Archive large frozen constituents as hashed assets, outside Contents API."""
    value = copy.deepcopy(manifest)
    value['schema'] = 'market-outlook-pointer-v1'
    value['outlook']['sector_outlook'].pop('frozen_members', None)
    value['outlook']['sector_outlook']['frozen_members_archived'] = True
    raw = json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8')
    if len(raw) > 800_000:
        raise ValueError('compact outlook pointer exceeds readback size limit')
    return value


def validate(value, source):
    if value.get('schema') != 'market-outlook-v1':
        raise ValueError('unknown outlook schema')
    if (value.get('snapshot_release') != source['release_tag'] or
            value.get('input_database_sha256') != source['database']['sha256'] or
            value.get('base_trade_date') != source['base_trade_date'] or
            value.get('target_trade_date') != source['next_trade_date']):
        raise ValueError('outlook belongs to a different snapshot')
    if context(value['base_trade_date'])['next_session'] != value['target_trade_date']:
        raise ValueError('target is not the next verified trading session')
    cutoff = dt.datetime.fromisoformat(value['information_cutoff'])
    if cutoff.tzinfo is None:
        raise ValueError('outlook cutoff must include timezone')
    close = dt.datetime.combine(dt.date.fromisoformat(value['base_trade_date']), dt.time(15), c.TZ)
    if cutoff < close:
        raise ValueError('closing-price inputs were not yet known')
    generated = dt.datetime.fromisoformat(value['generated_at'])
    if cutoff > generated:
        raise ValueError('information cutoff is in the future')
    if value.get('formal_trade_ready') is not False or value.get('actual_win_rate') is not None:
        raise ValueError('research output cannot claim actual trade qualification')
    if value.get('execution_win_rate_80_validated') is not False:
        raise ValueError('unvalidated execution win rate')
    for key in ('sector_outlook', 'opening_strength_plan'):
        part = value.get(key)
        if not isinstance(part, dict):
            raise ValueError('missing research section: '+key)
        for field, expected in (('base_trade_date', value['base_trade_date']),
                                ('target_trade_date', value['target_trade_date']),
                                ('information_cutoff', value['information_cutoff'])):
            if part.get(field) != expected:
                raise ValueError('research section identity differs: '+key+'/'+field)
    return value


def generate(store, cutoff=None):
    root = Path(store).resolve()
    restored = json.loads((root/'restore.json').read_text(encoding='utf-8'))
    source = restored['manifest']
    if not restored.get('verified') or not source.get('preparation_ready'):
        raise ValueError('verified prepared snapshot required')
    database = root/source['database']['name']
    if Path(source['database']['name']).name != source['database']['name']:
        raise ValueError('invalid database asset name')
    state.verify_file(database, source['database'])
    generated = c.stamp()
    if cutoff:
        explicit = dt.datetime.fromisoformat(cutoff.replace('Z','+00:00'))
        if explicit.tzinfo is None:
            raise ValueError('explicit cutoff must include timezone')
        cutoff = explicit.astimezone(c.TZ).isoformat()
    else:
        cutoff = generated
    # A standalone weekend preview uses its real preparation time, never a
    # fabricated Friday 18:00 cutoff. Mail retains its own scheduled cutoff.
    from sector_forecast import generate as sectors
    from opening_strength import build_plan
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA application_id').fetchone()[0] != c.APP_ID:
            raise ValueError('database identity mismatch')
        outlook = sectors(db, source['base_trade_date'], source['next_trade_date'], cutoff)
    plan = build_plan(database, source['base_trade_date'], source['next_trade_date'], cutoff,
                      sector_forecast=outlook, input_sha256=source['database']['sha256'])
    value = dict(schema='market-outlook-v1', generated_at=generated, information_cutoff=cutoff,
                 snapshot_release=source['release_tag'], input_database_sha256=source['database']['sha256'],
                 base_trade_date=source['base_trade_date'], target_trade_date=source['next_trade_date'],
                 sector_outlook=outlook, opening_strength_plan=plan, formal_trade_ready=False,
                 actual_win_rate=None, execution_win_rate_80_validated=False, costs_included=False,
                 smtp_sent=False, orders_submitted=False)
    validate(value, source)
    return value, source


def forward_tracking(value, source, store, repo):
    """Score the earliest frozen forecast per target date after its actual close."""
    prior, _ = read_pointer(repo)
    previous = ((prior or {}).get('outlook') or {}).get('sector_forward_tracking') or {}
    resolved = list(previous.get('resolved', []))
    pending = list(previous.get('pending', []))
    known_targets = {row['target_date'] for row in resolved + pending}
    old_forecast = ((prior or {}).get('outlook') or {}).get('sector_outlook') or {}
    if (prior and prior.get('forward_eligible') is True and old_forecast.get('status') == 'ready'
            and old_forecast.get('target_date') not in known_targets):
        pending.append({'target_date':old_forecast['target_date'], 'forecast_id':old_forecast['forecast_id'],
                        'release_tag':prior['release_tag'], 'asset':prior['files']['outlook.json']})
    remaining = []
    cutoff = dt.datetime.fromisoformat(value['information_cutoff'])
    database = Path(store).resolve()/source['database']['name']
    from sector_forecast import evaluate
    for item in pending:
        due = dt.datetime.combine(dt.date.fromisoformat(item['target_date']),dt.time(15),c.TZ)
        if cutoff < due or value['base_trade_date'] < item['target_date']:
            remaining.append(item)
            continue
        with tempfile.TemporaryDirectory(prefix='sector-forward-') as temporary:
            state.run('gh','release','download',item['release_tag'],'--repo',repo,
                      '--pattern','outlook.json','--dir',temporary)
            path = Path(temporary)/'outlook.json'
            state.verify_file(path,item['asset'])
            frozen = json.loads(path.read_text(encoding='utf-8'))['sector_outlook']
            if frozen['forecast_id'] != item['forecast_id'] or frozen['target_date'] != item['target_date']:
                raise ValueError('archived sector forecast differs from its original identity')
            result = evaluate(database,frozen,value['information_cutoff'])
        if result['status'] != 'resolved' or result.get('forward_eligible') is not True:
            remaining.append({**item,'last_status':result['status'],
                              'last_missing_stocks':result.get('missing_stocks')})
            continue
        keys = ('forecast_id','target_date','status','forward_eligible','top3_evaluated_sectors',
                'top3_relative_strength_hits','top3_absolute_up_hits','top3_top_quartile_hits')
        resolved.append({key:result.get(key) for key in keys})
    denominator = sum(row['top3_evaluated_sectors'] for row in resolved)
    value['sector_forward_tracking'] = {
        'schema':'sector-forward-ledger-v1', 'earliest_forecast_per_target_session':True,
        'resolved':resolved, 'pending':remaining, 'resolved_dates':len(resolved),
        'resolved_sector_predictions':denominator,
        'relative_strength_hit_rate':sum(row['top3_relative_strength_hits'] for row in resolved)/denominator if denominator else None,
        'absolute_up_rate':sum(row['top3_absolute_up_hits'] for row in resolved)/denominator if denominator else None,
        'top_quartile_hit_rate':sum(row['top3_top_quartile_hits'] for row in resolved)/denominator if denominator else None,
        'gross_trade_win_rate':None,
    }
    value['tracking_parent_release'] = prior['release_tag'] if prior else None
    return value


def verify_bundle(out, manifest):
    out = Path(out)
    if sorted(manifest['files']) != sorted(ASSETS):
        raise ValueError('outlook asset set mismatch')
    for name in ASSETS:
        if manifest['files'][name]['name'] != name:
            raise ValueError('outlook asset name mismatch')
        state.verify_file(out/name, manifest['files'][name])
    value = json.loads((out/'outlook.json').read_text(encoding='utf-8'))
    source = json.loads((out/'source_manifest.json').read_text(encoding='utf-8'))
    validate(value, source)
    if manifest['outlook'] != value:
        raise ValueError('published outlook differs from frozen assets')
    from market_outlook_report import render
    if (out/'report.html').read_text(encoding='utf-8') != render(value):
        raise ValueError('HTML report differs from frozen outlook')
    return value


def publish(repo, output, run_id):
    out = Path(output).resolve()
    value = json.loads((out/'outlook.json').read_text(encoding='utf-8'))
    source = json.loads((out/'source_manifest.json').read_text(encoding='utf-8'))
    validate(value, source)
    current, _ = state.pointer(repo)
    if current['release_tag'] != source['release_tag'] or current['database']['sha256'] != value['input_database_sha256']:
        raise ValueError('market snapshot advanced; stale outlook refused')
    old, blob = read_pointer(repo)
    if value.get('tracking_parent_release') != ((old or {}).get('release_tag')):
        raise ValueError('outlook advanced after its forward ledger was prepared')
    tag = f"market-outlook-{value['base_trade_date']}-{run_id}-{os.environ.get('GITHUB_RUN_ATTEMPT','1')}"
    deadline = dt.datetime.combine(dt.date.fromisoformat(value['target_trade_date']), dt.time(9,15), c.TZ)
    source_commit = os.environ.get('OUTLOOK_SOURCE_COMMIT') or os.environ.get('GITHUB_SHA','main')
    manifest = dict(schema='market-outlook-publication-v1', release_tag=tag,
                    github_commit=source_commit, github_run_id=str(run_id),
                    github_run_url=f'https://github.com/{repo}/actions/runs/{run_id}',
                    snapshot_release=value['snapshot_release'], base_trade_date=value['base_trade_date'],
                    target_trade_date=value['target_trade_date'], input_database_sha256=value['input_database_sha256'],
                    forward_eligible=dt.datetime.fromisoformat(value['generated_at']) < deadline and c.now() < deadline,
                    formal_trade_ready=False, actual_win_rate=None, outlook=value,
                    files={name: state.ref(out/name) for name in ASSETS}, parent_pointer_blob_sha=blob)
    verify_bundle(out, manifest)
    # Fail before creating a release if its current pointer cannot be read back.
    compact_pointer(manifest)
    state.run('gh','release','create',tag,'--repo',repo,'--target',source_commit,
              '--latest=false','--title',f"Next-session sector outlook {value['target_trade_date']}",
              '--notes','Frozen research ranking and conditional opening watchlist. No orders or mail; scores are not probabilities.')
    state.run('gh','release','upload',tag,*[str(out/name) for name in ASSETS],'--repo',repo)
    with tempfile.TemporaryDirectory(prefix='outlook-readback-') as temporary:
        state.run('gh','release','download',tag,'--repo',repo,'--dir',temporary)
        verify_bundle(temporary,manifest)
    manifest['assets_readback_verified_at'] = c.stamp()
    state.write(out/'outlook_manifest.json',manifest)
    state.run('gh','release','upload',tag,str(out/'outlook_manifest.json'),'--repo',repo)
    published_pointer = compact_pointer(manifest)
    state.write(out/'outlook_pointer.json',published_pointer)
    # Refuse races with either market-data publication or another outlook writer.
    current, _ = state.pointer(repo)
    if current['release_tag'] != source['release_tag'] or current['database']['sha256'] != value['input_database_sha256']:
        raise ValueError('market data changed during publication')
    _, current_blob = read_pointer(repo)
    if blob != current_blob:
        raise ValueError('outlook pointer changed concurrently')
    request = dict(message=f'Freeze sector and opening research {value["target_trade_date"]}',
                   branch=state.BRANCH, content=base64.b64encode((out/'outlook_pointer.json').read_bytes()).decode())
    if blob:
        request['sha'] = blob
    state.write(out/'pointer-request.json', request)
    state.run('gh','api','--method','PUT',f'repos/{repo}/contents/{POINTER}',
              '--input',str(out/'pointer-request.json'))
    if read_pointer(repo)[0] != published_pointer:
        raise ValueError('outlook pointer readback differs')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--cutoff')
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY','cluuacc-gif/ashare-research'))
    args = parser.parse_args()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    value, source = generate(args.store, args.cutoff)
    if args.publish:
        forward_tracking(value,source,args.store,args.repo)
    from market_outlook_report import render
    state.write(out/'outlook.json', value)
    state.write(out/'source_manifest.json', source)
    (out/'report.html').write_text(render(value),encoding='utf-8')
    manifest = publish(args.repo,out,os.environ.get('GITHUB_RUN_ID','local')) if args.publish else None
    print(json.dumps(dict(base=value['base_trade_date'],target=value['target_trade_date'],
                         publication=manifest['release_tag'] if manifest else None,
                         sector_status=value['sector_outlook'].get('status'),
                         smtp_sent=False,orders_submitted=False),ensure_ascii=False))


if __name__ == '__main__':
    main()
