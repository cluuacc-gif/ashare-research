"""Production report boundaries: Shanghai time, current snapshot and causal inputs."""
import datetime as dt
import json
import sqlite3
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo
from trade_calendar import context

TZ = ZoneInfo('Asia/Shanghai')


def boundary(stage, day, latest, at=None):
    at = at or dt.datetime.now(TZ)
    at = at.astimezone(TZ)
    cal = context(day)
    base = day if stage == 'evening' else cal['previous_session']
    blockers = []
    if at.date().isoformat() != day:
        blockers.append('target_date_differs_from_execution_date')
    if latest != base:
        blockers.append('base_trade_date_not_current')
    deadlines = {'morning': dt.time(9, 20), 'auction': dt.time(9, 28)}
    late = stage in deadlines and at.time().replace(tzinfo=None) >= deadlines[stage]
    if late:
        blockers.append('late_report_excluded_from_forward_results')
    if stage == 'evening' and at.time().replace(tzinfo=None) < dt.time(18):
        blockers.append('evening_before_1800')
    return {'expected_base_trade_date': base, 'latest_trade_date': latest,
            'generated_at': at.isoformat(), 'late': late, 'blockers': blockers,
            'forward_eligible': not blockers and stage != 'evening'}


def verified_auction(snapshot, day, at):
    try:
        started = dt.datetime.fromisoformat(snapshot['started_at']).astimezone(TZ)
        ended = dt.datetime.fromisoformat(snapshot['finished_at']).astimezone(TZ)
        at = at.astimezone(TZ)
        return (snapshot.get('usable') is True and started.date().isoformat() == day
                and dt.time(9, 25) <= started.time().replace(tzinfo=None)
                and ended.time().replace(tzinfo=None) <= dt.time(9, 28)
                and dt.timedelta(0) <= at - ended <= dt.timedelta(seconds=90)
                and snapshot.get('symbols_received', 0) > 0)
    except (KeyError, ValueError, TypeError):
        return False


def frozen_observations(path, base):
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read('manifest.json'))
        if manifest['base_trade_date'] != base:
            raise ValueError('frozen input date differs from required base')
        # Validate internal members before using them, in addition to outer asset hash.
        import hashlib
        for item in manifest['files']:
            raw = z.read(item['path'])
            if len(raw) != item['bytes'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
                raise ValueError('frozen member hash mismatch')
        quality = json.loads(z.read('quality.json'))
        rows = [json.loads(line) for line in z.read('price_interval_observed_NOT_CANDIDATES.jsonl').splitlines()]
        quotes = {r['symbol']: r for r in (json.loads(line) for line in z.read('base_quotes_observed.jsonl').splitlines())}
        result = []
        for item in rows:
            q = quotes[item['symbol']]
            result.append({'symbol': item['symbol'], 'name': item.get('name'), 'close': q['close'],
                           'amount': q.get('amount'), 'volume': q.get('volume'),
                           'open_to_close': q['close']/q['open']-1 if q.get('open') else None,
                           'bars': item.get('history_bars'), 'is_st': None, 'industry': None,
                           'limit_like_days': None, 'official_status_verified': False})
        result.sort(key=lambda r: (r['amount'] is None, -(r['amount'] or 0), r['symbol']))
        return result, quality


def news_before(db, cutoff):
    limit = dt.datetime.fromisoformat(cutoff).astimezone(TZ)
    start = limit - dt.timedelta(days=4)
    result = []
    for r in db.execute('SELECT event_id,title,published_at,retrieved_at,source_url,fact_status FROM news_events'):
        try:
            published = dt.datetime.fromisoformat(r['published_at'])
            retrieved = dt.datetime.fromisoformat(r['retrieved_at'])
            if published.tzinfo is None or retrieved.tzinfo is None:
                continue  # An unknown/date-only publication time is not an exact cutoff fact.
            if start <= published.astimezone(TZ) <= limit and retrieved.astimezone(TZ) <= limit:
                result.append(dict(r))
        except (TypeError, ValueError):
            continue
    return sorted(result, key=lambda x: (x['published_at'], x['event_id']))
