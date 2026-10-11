"""Research mail is allowed only on a verified exchange session in Shanghai time."""
import argparse
import json
import os
from pathlib import Path

import collector as c
from trade_calendar import is_session


def check(day=None, at=None, preview=False):
    at = (at or c.now()).astimezone(c.TZ)
    execution = at.date().isoformat()
    target = day or execution
    result = {'target_date':target, 'execution_date':execution,
              'checked_at':at.isoformat(), 'allowed':False, 'smtp_accepted':False}
    try:
        if not is_session(target):
            return {**result, 'state':'market_closed', 'reason':'target_session_closed'}
        if not preview and not is_session(execution):
            return {**result, 'state':'market_closed', 'reason':'execution_session_closed'}
    except ValueError:
        return {**result, 'state':'calendar_unverified', 'reason':'verified_exchange_calendar_unavailable'}
    return {**result, 'allowed':True, 'state':'preview_allowed' if preview else 'session_open', 'reason':None}


def save(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--day'); p.add_argument('--output',required=True)
    p.add_argument('--preview',action='store_true')
    a=p.parse_args(); result=check(a.day,preview=a.preview); save(a.output,result)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a') as stream:
            stream.write('send_allowed='+str(result['allowed']).lower()+'\n')
            stream.write('day='+result['target_date']+'\n')
    print(json.dumps(result,ensure_ascii=False))
