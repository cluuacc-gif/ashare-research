#!/usr/bin/env python3
"""Publish/read back immutable model releases, then CAS the separate model pointer."""
import argparse
import base64
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile

import collector as c
from pipeline_state import BRANCH, run, write, ref, verify_file, pointer as data_pointer

POINTER = 'state/model-current.json'
FILES = ['model.joblib','validation.json','predictions.json','predictions.sqlite3',
         'input_manifest.json','model_handoff.json','historical_holdout.csv.gz','prediction_inputs.csv.gz']


def pointer(repo):
    try:
        payload = json.loads(run('gh','api',f'repos/{repo}/contents/{POINTER}?ref={BRANCH}'))
        return json.loads(base64.b64decode(payload['content'])), payload['sha']
    except subprocess.CalledProcessError as exc:
        if 'HTTP 404' in (exc.stderr or ''): return None, None
        raise


def verify_bundle(root, manifest):
    root = Path(root)
    if sorted(manifest['files']) != sorted(FILES):
        raise ValueError('model artifact set mismatch')
    for name,item in manifest['files'].items():
        if item['name'] != name: raise ValueError('model asset filename mismatch')
        verify_file(root/name,item)
    predictions = json.loads((root/'predictions.json').read_text())
    validation = json.loads((root/'validation.json').read_text())
    source = json.loads((root/'input_manifest.json').read_text())
    handoff = json.loads((root/'model_handoff.json').read_text())
    for payload in (predictions, validation, handoff['latest_model']):
        for key in ('model_id','base_trade_date','target_trade_date','snapshot_release','input_database_sha256','prediction_count'):
            if payload[key] != manifest[key]: raise ValueError('model metadata mismatch: '+key)
    if source['release_tag'] != manifest['snapshot_release'] or source['database']['sha256'] != manifest['input_database_sha256']:
        raise ValueError('model source identity mismatch')
    if not validation['purged_time_split_verified'] or validation['status'] != 'TRAIN_SAVE_RELOAD_PREDICT_VERIFIED':
        raise ValueError('model runtime validation not passed')
    train, cal, test = [validation['split'][key] for key in ('train','calibration','test')]
    if not (train['last_outcome_date'] < cal['first_input_date'] and cal['last_outcome_date'] < test['first_input_date']
            and test['last_outcome_date'] <= manifest['base_trade_date'] < manifest['target_trade_date']):
        raise ValueError('model split overlaps or uses future labels')
    rows = predictions['rows']
    if len(rows) != manifest['prediction_count'] or not rows or len({r['symbol'] for r in rows}) != len(rows):
        raise ValueError('invalid prediction count/duplicates')
    if any(not (0 <= r['P2'] <= r['P1'] <= 1) for r in rows):
        raise ValueError('invalid probability ranges')
    with sqlite3.connect((root/'predictions.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        if db.execute('PRAGMA application_id').fetchone()[0] != 0x41534D50 or db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('prediction database identity/integrity failed')
        saved = db.execute('SELECT symbol,p_up,p_ge5,rank,model_id,base_trade_date,target_trade_date,formal_trade_ready FROM model_predictions ORDER BY rank').fetchall()
        expected = [(r['symbol'],r['P1'],r['P2'],r['rank'],manifest['model_id'],manifest['base_trade_date'],manifest['target_trade_date'],0) for r in rows]
        if saved != expected: raise ValueError('prediction database differs from saved predictions')
    return predictions, validation


def restore(repo, destination, release_tag=None, snapshot=None):
    root = Path(destination).resolve(); root.mkdir(parents=True, exist_ok=False)
    if release_tag:
        run('gh','release','download',release_tag,'--repo',repo,'--pattern','model_manifest.json','--dir',str(root))
        manifest = json.loads((root/'model_manifest.json').read_text())
    else:
        manifest, _ = pointer(repo)
        if not manifest: raise ValueError('no published model')
    if release_tag and manifest['release_tag'] != release_tag: raise ValueError('model release identity mismatch')
    if snapshot and (manifest['snapshot_release'] != snapshot['release_tag'] or
                     manifest['input_database_sha256'] != snapshot['database']['sha256']):
        raise ValueError('model belongs to a different data snapshot; stale model refused')
    for name in FILES:
        run('gh','release','download',manifest['release_tag'],'--repo',repo,'--pattern',name,'--dir',str(root))
    verify_bundle(root,manifest)
    write(root/'model_restore.json',{'manifest':manifest,'verified':True,'restored_at':c.stamp()})
    return root


def publish(repo, output, run_id):
    out = Path(output)
    predictions = json.loads((out/'predictions.json').read_text())
    old, blob = pointer(repo)
    source, source_blob = data_pointer(repo)
    if source['release_tag'] != predictions['snapshot_release'] or source['database']['sha256'] != predictions['input_database_sha256']:
        raise ValueError('data snapshot advanced during training; stale model publication refused')
    tag = f"model-{predictions['base_trade_date']}-{run_id}-{os.environ.get('GITHUB_RUN_ATTEMPT','1')}"
    manifest = {k:predictions[k] for k in ('model_id','model_version','base_trade_date','target_trade_date','snapshot_release',
        'input_database_sha256','prediction_count','generated_at','model_ready','formal_trade_ready','win_rate_gate')}
    manifest.update(schema_version='1.0',release_tag=tag,github_run_id=str(run_id),github_commit=os.environ.get('GITHUB_SHA'),
                    files={name:ref(out/name) for name in FILES},parent_pointer_blob_sha=blob,
                    data_pointer_blob_sha=source_blob,publication_started_at=c.stamp())
    verify_bundle(out,manifest)
    run('gh','release','create',tag,'--repo',repo,'--target',os.environ.get('GITHUB_SHA','main'),'--latest=false',
        '--title',f"Operational price/volume model {predictions['base_trade_date']}",
        '--notes','Production training/inference bundle. Measured performance recorded; trading admission remains separate.')
    run('gh','release','upload',tag,*[str(out/name) for name in FILES],'--repo',repo)
    with tempfile.TemporaryDirectory(prefix='model-readback-') as temporary:
        run('gh','release','download',tag,'--repo',repo,'--dir',temporary)
        verify_bundle(temporary,manifest)
    manifest['assets_readback_verified_at'] = c.stamp()
    deadline = dt.datetime.combine(dt.date.fromisoformat(manifest['target_trade_date']),dt.time(9,30),c.TZ)
    manifest['forward_eligible'] = (dt.datetime.fromisoformat(manifest['generated_at']) < deadline and c.now() < deadline)
    write(out/'model_manifest.json',manifest)
    run('gh','release','upload',tag,str(out/'model_manifest.json'),'--repo',repo)
    with tempfile.TemporaryDirectory(prefix='model-manifest-') as temporary:
        run('gh','release','download',tag,'--repo',repo,'--pattern','model_manifest.json','--dir',temporary)
        if json.loads((Path(temporary)/'model_manifest.json').read_text()) != manifest:
            raise ValueError('model manifest readback differs')
    _, current_blob = pointer(repo)
    current_source, current_source_blob = data_pointer(repo)
    if current_blob != blob or current_source_blob != source_blob or current_source != source:
        raise ValueError('data/model pointer advanced; saved release kept and current pointer not replaced')
    if manifest['forward_eligible'] and c.now() >= deadline:
        raise ValueError('publication crossed market open; retry as retrospective')
    request = {'message':f'Publish verified model {manifest["model_id"]}','branch':BRANCH,
               'content':base64.b64encode((out/'model_manifest.json').read_bytes()).decode()}
    if blob: request['sha'] = blob
    write(out/'model-pointer-request.json',request)
    run('gh','api','--method','PUT',f'repos/{repo}/contents/{POINTER}','--input',str(out/'model-pointer-request.json'))
    actual, _ = pointer(repo)
    if actual != manifest: raise ValueError('model pointer readback differs')
    write(out/'published_model_manifest.json',manifest)
    return manifest


def attach(payload, store, snapshot):
    root = Path(store)
    saved = json.loads((root/'model_restore.json').read_text())
    if not saved['verified']: raise ValueError('unverified model restore')
    manifest = saved['manifest']
    if manifest['snapshot_release'] != snapshot['release_tag'] or manifest['input_database_sha256'] != snapshot['database']['sha256']:
        raise ValueError('report model and data snapshots differ')
    predictions, validation = verify_bundle(root,manifest)
    expected = snapshot['next_trade_date']
    if manifest['target_trade_date'] != expected or (payload['stage'] == 'morning' and payload['target_date'] != expected):
        raise ValueError('report model target differs')
    payload['research'].update(model_ready=True, formal_trade_ready=False,
        model_release=manifest['release_tag'],model_predictions=predictions,model_validation=validation,
        model_forward_record_eligible=manifest['forward_eligible'])
    return payload


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation',choices=['publish','restore'])
    p.add_argument('--repo',default=os.environ.get('GITHUB_REPOSITORY','cluuacc-gif/ashare-research'))
    p.add_argument('--output'); p.add_argument('--destination'); p.add_argument('--store'); p.add_argument('--release-tag')
    a = p.parse_args()
    if a.operation == 'publish':
        result = publish(a.repo,a.output,os.environ.get('GITHUB_RUN_ID','local'))
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'],'a') as stream: stream.write('model_tag='+result['release_tag']+'\n')
        print(json.dumps(result,ensure_ascii=False))
    else:
        snapshot = json.loads((Path(a.store)/'restore.json').read_text())['manifest'] if a.store else None
        restore(a.repo,a.destination,a.release_tag,snapshot)
