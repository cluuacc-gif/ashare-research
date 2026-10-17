#!/usr/bin/env python3
"""Reports consume one independently restored immutable snapshot; exact paths."""
import argparse
import base64
import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys

import collector as c
from pipeline_state import run, write, BRANCH
from report_contract import frozen_observations, news_before
from trade_calendar import context


def delivery_record(repo, stage, day):
    path = f'state/delivery/{stage}-{day}.json'
    try:
        raw = json.loads(run('gh', 'api', f'repos/{repo}/contents/{path}?ref={BRANCH}'))
        return path, json.loads(base64.b64decode(raw['content'])), raw['sha']
    except subprocess.CalledProcessError as exc:
        if 'HTTP 404' in (exc.stderr or ''): return path, None, None
        raise


def report(stage, day, store, model_store=None):
    saved = json.loads((store/'restore.json').read_text())['manifest']
    if not saved.get('preparation_ready'):
        raise ValueError('unprepared seed cannot generate daily research')
    expected = day if stage == 'evening' else context(day)['previous_session']
    if saved['base_trade_date'] != expected:
        raise ValueError('snapshot date differs from required report base')
    from mail_reports import build
    # The 09:25 model observation uses prior-close inputs. Do not call the legacy
    # auction backtest renderer, which can mix proxy prices and static edge claims.
    payload = build('morning' if stage == 'auction' else stage, store/saved['database']['name'], day, store/saved['handoff']['name'], None)
    payload['stage'] = stage
    if stage == 'auction':
        payload.update(auction_verified=False, information_cutoff=day+'T09:25:00+08:00')
    band, quality = frozen_observations(store/saved['frozen']['name'], expected)
    payload['research']['price_band_5_10'] = band
    payload['research']['frozen_quality'] = quality
    payload['research']['inventory']['base_quotes'] = saved['base_quotes']
    payload['snapshot_release'] = saved['release_tag']
    payload['base_trade_date'] = expected
    payload['engineering_preparation_ready'] = True
    if model_store is not None:
        from model_store import attach
        attach(payload, model_store, saved)
        payload['disclaimer'] = payload['disclaimer'].replace('无正式概率，', '已附价量模型概率，')
    at = c.now()
    from mail_timing import scheduled_at, WINDOW
    late = at >= scheduled_at(stage,day) + WINDOW
    payload['late'] = late
    if stage == 'evening':
        payload['information_cutoff'] = saved['generated_at']
    else:
        # Never retrieve a future-cutoff headline just because a preview is for tomorrow.
        cutoff = min(at,dt.datetime.fromisoformat(payload['information_cutoff']))
        payload['information_cutoff'] = cutoff.isoformat()
        with sqlite3.connect((store/saved['database']['name']).as_uri()+'?mode=ro',uri=True) as db:
            db.row_factory = sqlite3.Row
            news = news_before(db,cutoff.isoformat())
        payload['public_news_until_0925' if stage == 'auction' else 'public_news_until_0900'] = news
    if late:
        payload['disclaimer'] = '迟到补发／回顾；不计入事前预测。' + payload['disclaimer']
    return payload


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stage', choices=['evening','morning','auction'], required=True)
    p.add_argument('--day', required=True)
    p.add_argument('--store', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--model-store')
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    out, store = Path(a.output).resolve(), Path(a.store).resolve()
    out.mkdir(parents=True, exist_ok=True)
    from mail_gate import check
    gate = check(a.day,preview=a.dry_run)
    if not gate['allowed']:
        write(out/'delivery.json', gate)
        return 0
    from mail_timing import check as timing_check
    timing = timing_check(a.stage,a.day,preview=a.dry_run)
    if not timing['allowed']:
        write(out/'delivery.json',timing)
        return 0
    repo = os.environ.get('GITHUB_REPOSITORY','cluuacc-gif/ashare-research')
    path, previous, blob = delivery_record(repo,a.stage,a.day) if not a.dry_run else ('',None,None)
    if previous and previous.get('smtp_accepted'):
        write(out/'delivery.json', {**previous,'state':'already_smtp_accepted','duplicate_suppressed':True})
        return 0
    value = report(a.stage,a.day,store,a.model_store)
    from mail_layout import content, render_text, render_html, VERSION
    value.update(preview=a.dry_run,mail_layout_version=VERSION)
    body = out/f'{a.stage}-{a.day}.txt'
    html_body = out/f'{a.stage}-{a.day}.html'
    write(out/f'{a.stage}-{a.day}.json',value)
    rendered = render_text(value)
    body.write_text(rendered,encoding='utf-8')
    (out/f'{a.stage}-{a.day}.md').write_text(rendered,encoding='utf-8')
    html_body.write_text(render_html(value),encoding='utf-8')
    command = [sys.executable,str(Path(__file__).with_name('send_mail.py')),'--subject',content(value)['subject'],
               '--body-file',str(body),'--html-file',str(html_body),'--research-day',a.day,
               '--stage',a.stage,
               '--draft-out',str(out/'report.eml'),'--status-out',str(out/'delivery.json')]
    if a.dry_run: command.append('--draft-only')
    subprocess.run(command,check=True)
    delivered = json.loads((out/'delivery.json').read_text())
    if not a.dry_run:
        if delivered['state'] in ('market_closed','calendar_unverified','missed_window',
                                  'before_send_window','wrong_research_day','invalid_stage'): return 0
        if not delivered['smtp_accepted']: raise ValueError('SMTP did not accept report')
        delivered.update(target_date=a.day,stage=a.stage,release_tag=value['snapshot_release'])
        payload = {'message':f'Record {a.stage} SMTP acceptance {a.day}','branch':BRANCH,
                   'content':base64.b64encode((json.dumps(delivered)+'\n').encode()).decode()}
        if blob: payload['sha']=blob
        write(out/'delivery-request.json',payload)
        run('gh','api','--method','PUT',f'repos/{repo}/contents/{path}','--input',str(out/'delivery-request.json'))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
