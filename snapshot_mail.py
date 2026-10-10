#!/usr/bin/env python3
"""Reports consume one independently restored immutable snapshot; exact paths."""
import argparse
import base64
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys

import collector as c
from pipeline_state import run, write, BRANCH
from report_contract import frozen_observations
from trade_calendar import context


def delivery_record(repo, stage, day):
    path = f'state/delivery/{stage}-{day}.json'
    try:
        raw = json.loads(run('gh', 'api', f'repos/{repo}/contents/{path}?ref={BRANCH}'))
        return path, json.loads(base64.b64decode(raw['content'])), raw['sha']
    except subprocess.CalledProcessError as exc:
        if 'HTTP 404' in (exc.stderr or ''): return path, None, None
        raise


def report(stage, day, store):
    saved = json.loads((store/'restore.json').read_text())['manifest']
    if not saved.get('preparation_ready'):
        raise ValueError('unprepared seed cannot generate daily research')
    expected = day if stage == 'evening' else context(day)['previous_session']
    if saved['base_trade_date'] != expected:
        raise ValueError('snapshot date differs from required report base')
    from mail_reports import build
    payload = build(stage, store/saved['database']['name'], day, store/saved['handoff']['name'], None)
    band, quality = frozen_observations(store/saved['frozen']['name'], expected)
    payload['research']['price_band_5_10'] = band
    payload['research']['frozen_quality'] = quality
    payload['research']['inventory']['base_quotes'] = saved['base_quotes']
    payload['snapshot_release'] = saved['release_tag']
    payload['base_trade_date'] = expected
    payload['engineering_preparation_ready'] = True
    at = c.now()
    late = at.date().isoformat() != day or (stage == 'morning' and at.time() >= dt.time(9,20))
    payload['late'] = late
    if stage == 'evening':
        payload['information_cutoff'] = saved['generated_at']
    if late:
        payload['disclaimer'] = '迟到补发／回顾；不计入事前预测。' + payload['disclaimer']
    return payload


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stage', choices=['evening','morning'], required=True)
    p.add_argument('--day', required=True)
    p.add_argument('--store', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    out, store = Path(a.output).resolve(), Path(a.store).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if not context(a.day)['is_session']:
        write(out/'delivery.json', {'state':'market_closed','target_date':a.day})
        return 0
    repo = os.environ.get('GITHUB_REPOSITORY','cluuacc-gif/ashare-research')
    path, previous, blob = delivery_record(repo,a.stage,a.day) if not a.dry_run else ('',None,None)
    if previous and previous.get('smtp_accepted'):
        write(out/'delivery.json', {**previous,'state':'already_smtp_accepted','duplicate_suppressed':True})
        return 0
    value = report(a.stage,a.day,store)
    from mail_reports import render_markdown
    body = out/f'{a.stage}-{a.day}.md'
    write(out/f'{a.stage}-{a.day}.json',value)
    rendered = render_markdown(value)
    banner = '迟到补发／回顾' if value['late'] else '研究简报'
    body.write_text(f'> {banner}；行情已自动入库、检查、冻结并独立读回。正式数据与模型准入仍未通过。\n\n'+rendered,encoding='utf-8')
    command = [sys.executable,'send_mail.py','--subject',f'A股研究 {a.day} {banner}',
               '--body-file',str(body),'--draft-out',str(out/'report.eml'),'--status-out',str(out/'delivery.json')]
    if a.dry_run: command.append('--draft-only')
    subprocess.run(command,check=True)
    delivered = json.loads((out/'delivery.json').read_text())
    if not a.dry_run:
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
