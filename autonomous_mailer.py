#!/usr/bin/env python3
"""Prepare sealed small reports early; the delivery path never restores databases."""
import argparse
import base64
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

import collector as c
import mail_gate
import mail_timing
import pipeline_state as state
import model_store
import snapshot_mail

ROOT = Path(__file__).resolve().parent
NO_RESEND = {'in_flight','delivery_unknown','partial_acceptance'}


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    os.replace(temporary, path)


def slot(root, stage, day):
    mail_timing.scheduled_at(stage, day)
    return Path(root).resolve() / 'outbox' / (day+'-'+stage)


def local_file(root, name):
    path = (root/name).resolve()
    if Path(name).is_absolute() or not path.is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError('unsafe mail outbox path')
    return path


def prune_cache(cache, protected):
    """Keep each kind's newest three verified flat restores and protected paths.

    Unknown or incomplete directories are left alone. Cleanup never recurses or
    follows links, and cannot remove outbox reports or original source files.
    """
    cache = Path(cache)
    if cache.is_symlink() or not cache.is_dir():
        return []
    cache = cache.resolve()
    protected = {(Path(value) if Path(value).is_absolute() else cache/Path(value)).resolve()
                 for value in protected}
    pattern = re.compile(r'(prepared|model)-(\d{4}-\d{2}-\d{2})-(\d+)-(\d+)')

    def eligible(path):
        match = pattern.fullmatch(path.name)
        try:
            if not match or path.is_symlink() or not path.is_dir() or path.resolve() != cache/path.name:
                return None
            date = dt.date.fromisoformat(match[2])
            files = list(path.iterdir())
            if any(item.is_symlink() or not item.is_file() or item.resolve().parent != path for item in files):
                return None
            kind = match[1]
            marker_name = 'restore.json' if kind == 'prepared' else 'model_restore.json'
            manifest_name = 'manifest.json' if kind == 'prepared' else 'model_manifest.json'
            expected = ({'market.sqlite3','handoff.json','frozen.zip','quality.json'} if kind == 'prepared'
                        else set(model_store.FILES)) | {marker_name,manifest_name}
            if {item.name for item in files} != expected:
                return None
            marker_path, manifest_path = path/marker_name, path/manifest_name
            if marker_path.stat().st_size > 1024*1024 or manifest_path.stat().st_size > 1024*1024:
                return None
            marker = json.loads(marker_path.read_text(encoding='utf-8'))
            manifest = marker.get('manifest')
            if (marker.get('verified') is not True or not isinstance(manifest,dict) or
                manifest.get('release_tag') != path.name or
                json.loads(manifest_path.read_text(encoding='utf-8')) != manifest):
                return None
            if kind == 'prepared':
                if {manifest[key]['name'] for key in ('database','handoff','frozen','quality')} != expected-{marker_name,manifest_name}:
                    return None
            elif (set(manifest['files']) != set(model_store.FILES) or
                  any(item['name'] != name for name,item in manifest['files'].items())):
                return None
            return kind, (date,int(match[3]),int(match[4])), files
        except (OSError,ValueError,KeyError,TypeError,AttributeError,RuntimeError):
            return None

    groups = {'prepared':[], 'model':[]}
    try:
        entries = list(cache.iterdir())
    except OSError:
        return []
    for path in entries:
        candidate = eligible(path)
        if candidate:
            kind, order, _ = candidate
            groups[kind].append((order,path))
    removed = []
    for candidates in groups.values():
        for _,path in sorted(candidates,reverse=True)[3:]:
            if path in protected:
                continue
            candidate = eligible(path)  # Recheck the complete flat structure before deletion.
            if not candidate:
                continue
            try:
                for item in sorted(candidate[2],key=lambda value:value.name.endswith('_restore.json') or value.name=='restore.json'):
                    if (path.is_symlink() or path.resolve() != cache/path.name or item.is_symlink() or
                        not item.is_file() or item.resolve().parent != path):
                        break
                    item.unlink()
                else:
                    path.rmdir()
                    removed.append(path.name)
            except OSError:
                continue  # A busy/changed cache must not prevent preparation.
    return removed


def cached_inputs(root, repo):
    snapshot, _ = state.pointer(repo)
    tag = snapshot.get('release_tag', '')
    if not snapshot.get('preparation_ready') or not re.fullmatch(r'[A-Za-z0-9._-]+', tag):
        raise ValueError('current research snapshot not prepared')
    cache = Path(root).resolve()/'cache'
    cache.mkdir(parents=True, exist_ok=True)
    store = cache/tag
    if not store.exists():
        staging = cache/(tag+'.partial-'+uuid.uuid4().hex)
        state.restore(repo, staging, tag)
        staging.rename(store)
    restored = json.loads((store/'restore.json').read_text(encoding='utf-8'))
    if restored['manifest'] != snapshot or not restored['verified']:
        raise ValueError('cached snapshot identity differs')
    for key in ('database', 'handoff', 'frozen', 'quality'):
        state.verify_file(store/snapshot[key]['name'], snapshot[key])
    model, _ = model_store.pointer(repo)
    if not model or model.get('snapshot_release') != tag or model.get('input_database_sha256') != snapshot['database']['sha256']:
        raise ValueError('matching model has not been published')
    model_tag = model['release_tag']
    if not re.fullmatch(r'[A-Za-z0-9._-]+', model_tag):
        raise ValueError('invalid model release')
    model_path = cache/model_tag
    if not model_path.exists():
        staging = cache/(model_tag+'.partial-'+uuid.uuid4().hex)
        model_store.restore(repo, staging, model_tag, snapshot)
        staging.rename(model_path)
    saved_model = json.loads((model_path/'model_restore.json').read_text(encoding='utf-8'))
    if saved_model['manifest'] != model or not saved_model['verified']:
        raise ValueError('cached model identity differs')
    model_store.verify_bundle(model_path, model)
    prune_cache(cache, {store,model_path})
    return store, model_path


def seal(root, stage, day, subject, body, html_body, metadata):
    out = slot(root, stage, day)
    bundle = out/('bundle-'+uuid.uuid4().hex)
    bundle.mkdir(parents=True, exist_ok=False)
    parts = []
    for name, text in [('report.txt', body), ('report.html', html_body)]:
        raw = text.encode('utf-8')
        (bundle/name).write_bytes(raw)
        parts.append({'path':name,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
    value = {**metadata,'stage':stage,'target_date':day,'subject':subject,
             'prepared_at':c.stamp(),'scheduled_at':mail_timing.scheduled_at(stage,day).isoformat(),
             'files':parts}
    atomic(bundle/'manifest.json',value)
    raw = (bundle/'manifest.json').read_bytes()
    if c.now() >= mail_timing.scheduled_at(stage,day):
        raise ValueError('prepared report crossed its publication deadline')
    atomic(out/'active.json',{'bundle':bundle.name,'manifest_sha256':hashlib.sha256(raw).hexdigest()})
    return value


def missing_content(stage, day, diagnostic='preparation_not_complete'):
    clock = mail_timing.SCHEDULES[stage].strftime('%H:%M')
    subject = f'A股研究缺口简报｜{day}｜{clock}'
    body = (f'{day} 北京时间 {clock} 研究准备状态\n\n'
            '本次没有在准备截止前取得日期一致、独立核验的数据和模型报告。\n'
            '本期不提供旧报告、候选或概率，后续不补发迟到回顾。\n'
            f'诊断：{diagnostic}。详细运行状态保留在系统中。\n')
    return subject, body, '<html><body><pre>'+html.escape(body)+'</pre></body></html>'


def prepare(stage, day, root, repo, missing_policy='brief', store=None, model=None):
    out = slot(root, stage, day)
    gate = mail_gate.check(day)
    due = mail_timing.scheduled_at(stage,day)
    if not gate['allowed'] or c.now().date().isoformat() != day:
        result = gate if not gate['allowed'] else {'state':'wrong_research_day','smtp_accepted':False}
        atomic(out/'preparation.json',result)
        return result
    if c.now() >= due:
        result = {'state':'preparation_deadline_passed','smtp_accepted':False}
        atomic(out/'preparation.json',result)
        return result
    receipt_path, previous, receipt_blob = f'state/delivery/{stage}-{day}.json', None, None
    metadata = {'repository':repo,'receipt_path':receipt_path,'receipt_blob':None,
                'content_kind':'research','formal_trade_ready':False}
    if missing_policy == 'brief':
        # Remains available to the independent sender if a restore hangs.
        seal(root,stage,day,*missing_content(stage,day),
             {**metadata,'content_kind':'missing_data_brief'})
    # Check prior acceptance while preparing, never at the send deadline.
    receipt_error = None
    if os.environ.get('MAIL_DELIVERY_BACKEND') == 'dedicated':
        local_receipt = out/'delivery.json'
        if local_receipt.is_file():
            previous = json.loads(local_receipt.read_text(encoding='utf-8'))
    else:
        try:
            receipt_path, previous, receipt_blob = snapshot_mail.delivery_record(repo,stage,day)
        except subprocess.CalledProcessError as exc:
            receipt_error = type(exc).__name__
    if previous and previous.get('smtp_accepted'):
        atomic(out/'delivery.json',{**previous,'state':'already_smtp_accepted'})
        return previous
    metadata.update(receipt_path=receipt_path,receipt_blob=receipt_blob)
    try:
        if receipt_error:
            raise RuntimeError('remote acceptance status unavailable during preparation')
        if store is None:
            store, model = cached_inputs(root,repo)
        if model is None:
            raise ValueError('matching independently verified model required')
        payload = snapshot_mail.report(stage,day,Path(store),Path(model))
        if payload.get('late') or not payload['research'].get('model_ready'):
            raise ValueError('current report not ready for scheduled delivery')
        from mail_layout import content, render_text, render_html
        metadata.update(snapshot_release=payload['snapshot_release'],
                        model_release=payload['research']['model_release'],
                        information_cutoff=payload.get('information_cutoff'))
        subject, body, html_body = content(payload)['subject'], render_text(payload), render_html(payload)
    except Exception as exc:
        result = {'state':'report_unavailable','error_class':type(exc).__name__,
                  'stage':stage,'target_date':day,'recorded_at':c.stamp(),'smtp_accepted':False}
        atomic(out/'preparation.json',result)
        if missing_policy != 'brief':
            return result
        metadata.update(content_kind='missing_data_brief',error_class=type(exc).__name__)
        subject, body, html_body = missing_content(stage,day,type(exc).__name__)
    # Restores or rendering may have crossed the deadline: never publish late work.
    if c.now() >= due:
        result = {'state':'preparation_deadline_passed','smtp_accepted':False}
        atomic(out/'preparation.json',result)
        return result
    result = seal(root,stage,day,subject,body,html_body,metadata)
    atomic(out/'preparation.json',{'state':'prepared','prepared_at':result['prepared_at'],
                                 'content_kind':result['content_kind'],'smtp_accepted':False})
    return result


def read_ready(root, stage, day):
    out = slot(root,stage,day)
    active = json.loads((out/'active.json').read_text(encoding='utf-8'))
    bundle = local_file(out,active['bundle'])
    raw = (bundle/'manifest.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != active['manifest_sha256']:
        raise ValueError('prepared mail manifest hash differs')
    manifest = json.loads(raw)
    due = mail_timing.scheduled_at(stage,day)
    if (manifest['stage'] != stage or manifest['target_date'] != day or
        c.timestamp(manifest['prepared_at']) >= due or manifest['scheduled_at'] != due.isoformat()):
        raise ValueError('prepared mail date/stage/cutoff mismatch')
    if len(manifest['files']) != 2 or {v['path'] for v in manifest['files']} != {'report.txt','report.html'}:
        raise ValueError('unexpected prepared mail files')
    for item in manifest['files']:
        data = local_file(bundle,item['path']).read_bytes()
        if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('prepared mail body hash differs')
    return bundle, manifest


def deliver(stage, day, root, wait=False, dry_run=False):
    out = slot(root,stage,day)
    gate = mail_gate.check(day,preview=dry_run)
    if not gate['allowed']:
        atomic(out/'delivery.json',gate)
        return gate
    if wait and not dry_run:
        due = mail_timing.scheduled_at(stage,day)
        remaining = (due-c.now()).total_seconds()
        if remaining > 3600:
            raise ValueError('refuse to keep a delivery worker waiting over one hour')
        while 0 < remaining <= 3600:
            time.sleep(min(remaining,1))
            remaining = (due-c.now()).total_seconds()
    gate = mail_timing.check(stage,day,preview=dry_run)
    if not gate['allowed']:
        atomic(out/'last-attempt.json',gate)
        return gate
    receipt = out/'delivery.json'
    if receipt.is_file() and not dry_run:
        previous = json.loads(receipt.read_text(encoding='utf-8'))
        if previous.get('smtp_accepted') or previous.get('state') in NO_RESEND:
            return {**previous,'duplicate_suppressed':True}
    try:
        bundle, manifest = read_ready(root,stage,day)
    except (OSError,ValueError,KeyError) as exc:
        result = {'state':'no_valid_prepared_report','error_class':type(exc).__name__,'smtp_accepted':False}
        atomic(out/'last-attempt.json',result)
        return result
    lock = out/'delivery.lock'
    try:
        descriptor = os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError:
        return {'state':'delivery_locked','smtp_accepted':False}
    os.close(descriptor)
    try:
        # Another process may have completed after the earlier unlocked read.
        if receipt.is_file() and not dry_run:
            previous = json.loads(receipt.read_text(encoding='utf-8'))
            if previous.get('smtp_accepted') or previous.get('state') in NO_RESEND:
                return {**previous,'duplicate_suppressed':True}
        # Keep an in-flight receipt after an interrupted process; do not blindly resend.
        if not dry_run:
            atomic(receipt,{'state':'in_flight','stage':stage,'target_date':day,'smtp_accepted':False})
        smtp_result = out/(('preview-' if dry_run else 'smtp-')+uuid.uuid4().hex+'.json')
        command = [sys.executable,str(ROOT/'send_mail.py'),'--stage',stage,'--research-day',day,
            '--subject',manifest['subject'],'--body-file',str(bundle/'report.txt'),
            '--html-file',str(bundle/'report.html'),'--draft-out',str(out/'report.eml'),
            '--status-out',str(smtp_result)]
        if dry_run: command.append('--draft-only')
        result = subprocess.run(command,check=False)
        delivered = json.loads(smtp_result.read_text(encoding='utf-8')) if smtp_result.is_file() else {
            'state':'delivery_unknown','smtp_accepted':False,'sender_returncode':result.returncode}
        delivered.update(stage=stage,target_date=day,content_kind=manifest['content_kind'],
                         release_tag=manifest.get('snapshot_release'))
        if not dry_run:
            atomic(receipt,delivered)
            if delivered.get('smtp_accepted') and os.environ.get('MAIL_PUBLISH_RECEIPTS','true').lower() != 'false':
                payload = {'message':f'Record timed {stage} SMTP acceptance {day}','branch':state.BRANCH,
                    'content':base64.b64encode((json.dumps(delivered)+'\n').encode()).decode()}
                if manifest.get('receipt_blob'): payload['sha']=manifest['receipt_blob']
                atomic(out/'receipt-request.json',payload)
                try:
                    state.run('gh','api','--method','PUT',f"repos/{manifest['repository']}/contents/{manifest['receipt_path']}",
                              '--input',str(out/'receipt-request.json'))
                except Exception as exc:
                    delivered['receipt_publish_error']=type(exc).__name__
                    atomic(receipt,delivered)  # Acceptance remains authoritative locally; never resend.
        return delivered
    finally:
        lock.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stage',choices=mail_timing.SCHEDULES,required=True)
    p.add_argument('--day',default=None)
    p.add_argument('--state-dir',required=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare',action='store_true')
    mode.add_argument('--deliver',action='store_true')
    p.add_argument('--wait',action='store_true')
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--store'); p.add_argument('--model-store')
    p.add_argument('--repo',default=os.environ.get('GITHUB_REPOSITORY','cluuacc-gif/ashare-research'))
    p.add_argument('--missing-policy',choices=['skip','brief'],default=os.environ.get('MAIL_MISSING_POLICY','brief'))
    a = p.parse_args()
    day = a.day or c.now().date().isoformat()
    result = (prepare(a.stage,day,a.state_dir,a.repo,a.missing_policy,a.store,a.model_store) if a.prepare else
              deliver(a.stage,day,a.state_dir,a.wait,a.dry_run))
    print(json.dumps({k:result[k] for k in ('state','stage','target_date','smtp_accepted','prepared_at','content_kind') if k in result},ensure_ascii=False))
    return 1 if result.get('state') in ('failed','delivery_unknown','not_configured','recipient_refused','partial_acceptance') else 0


if __name__=='__main__':
    raise SystemExit(main())
