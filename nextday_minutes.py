"""Bounded, archived vendor minute observations. Never infer a live order fill."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import urllib.parse
import urllib.request

TZ = timezone(timedelta(hours=8))
SCHEMA = 'nextday-minute-observations-v1'
MAX_SYMBOLS = 48


def instant(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('minute evidence needs a timezone')
    return parsed.astimezone(TZ)


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def endpoint(symbol):
    if not isinstance(symbol, str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', symbol):
        raise ValueError('invalid A-share symbol')
    code, exchange = symbol.split('.')
    # Do not guess an undocumented Beijing market mapping.
    if exchange == 'BJ':
        raise ValueError('Beijing minute adapter not verified')
    query = urllib.parse.urlencode({'secid': ('1.' if exchange == 'SH' else '0.') + code,
        'klt': 1, 'fqt': 0, 'beg': '20200101', 'end': '20500101', 'lmt': 1500,
        'fields1': 'f1,f2,f3,f4,f5,f6',
        'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'})
    return 'https://push2his.eastmoney.com/api/qt/stock/kline/get?' + query


def parse(raw, symbol, observed_at, *, source_url=None):
    """f51=end minute; f52/f53/f54/f55=O/C/H/L; reject zero opens.

    The five-day trends endpoint sometimes returns historical open=0. This
    adapter intentionally uses the OHLC endpoint and never fills those zeros
    with a later close. The provider may expose only its latest session.
    """
    observed = instant(observed_at)
    payload = json.loads(raw)
    data = payload.get('data')
    if (not isinstance(data, dict) or data.get('code') != symbol.split('.')[0]
            or data.get('market') != (1 if symbol.endswith('.SH') else 0)
            or not isinstance(data.get('klines'), list)):
        raise ValueError('minute response identity or shape mismatch')
    result = []
    seen = set()
    for line in data['klines']:
        parts = line.split(',') if isinstance(line, str) else []
        if len(parts) < 7:
            raise ValueError('malformed OHLC minute row')
        end = datetime.strptime(parts[0], '%Y-%m-%d %H:%M').replace(tzinfo=TZ)
        clock = end.time()
        if not (time(9, 31) <= clock <= time(11, 30) or time(13, 1) <= clock <= time(15)):
            raise ValueError('minute outside continuous-session boundaries')
        if end > observed:
            continue  # never expose an unfinished or future minute
        if end in seen:
            raise ValueError('duplicate minute timestamps')
        seen.add(end)
        opening, close, high, low, volume, amount = [number(x) for x in parts[1:7]]
        if (any(x is None or x <= 0 for x in (opening, close, high, low))
                or volume is None or amount is None or volume < 0 or amount < 0
                or not low <= min(opening, close) <= max(opening, close) <= high
                or (volume == 0) != (amount == 0)):
            raise ValueError('invalid or zero-open minute OHLCV')
        result.append({'start_at': (end-timedelta(minutes=1)).isoformat(),
            'end_at': end.isoformat(), 'open': opening, 'high': high, 'low': low,
            'close': close, 'volume': volume, 'amount': amount})
    if result != sorted(result, key=lambda row: row['end_at']):
        raise ValueError('minute timestamps not monotone')
    if not result:
        raise ValueError('no completed OHLC minutes')
    return {'schema': SCHEMA, 'symbol': symbol, 'observed_at': observed.isoformat(),
        'source_url': source_url or endpoint(symbol), 'raw_sha256': hashlib.sha256(raw).hexdigest(),
        'retrieval_mode': 'after_close_retrospective_vendor_observation',
        'live_seen_at_decision_verified': False, 'official_status_and_limits_verified': False,
        'actual_fill_verified': False, 'bars': result}


def capture(symbols, directory, *, fetch=None, clock=None):
    """At most 48 symbols, four readers, one 8-second request per symbol.

    Failures are explicit per symbol. This runs in the after-close research
    publisher, never in SMTP preparation or as a punctual intraday scheduler.
    """
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    symbols = sorted(set(symbols))
    if len(symbols) > MAX_SYMBOLS:
        raise ValueError('minute capture symbol budget exceeded')
    clock = clock or (lambda: datetime.now(TZ))

    def read(url):
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://quote.eastmoney.com/'})
        with urllib.request.urlopen(req, timeout=8) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError('minute response exceeds size limit')
        return raw

    def one(symbol):
        try:
            url = endpoint(symbol)
            raw = (fetch or read)(url)
            if not isinstance(raw, bytes):
                raise ValueError('minute response must be bytes')
            observed = clock().astimezone(TZ).isoformat()
            digest = hashlib.sha256(raw).hexdigest()
            name = symbol.replace('.', '_') + '-' + digest[:16] + '.json'
            # Archive the original response even if validation subsequently fails.
            (root/name).write_bytes(raw)
            value = parse(raw, symbol, observed, source_url=url)
            value['raw_asset'] = name
            return symbol, value
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return symbol, {'schema': SCHEMA, 'symbol': symbol, 'status': 'unavailable',
                'reason': type(exc).__name__ + ': ' + str(exc)[:180], 'bars': [],
                'actual_fill_verified': False}

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = dict(pool.map(one, symbols))
    manifest = {'schema': SCHEMA, 'symbols_requested': len(symbols),
        'symbols_available': sum(bool(row.get('bars')) for row in results.values()),
        'generated_at': clock().astimezone(TZ).isoformat(), 'symbols': results,
        'smtp_sent': False, 'orders_submitted': False}
    (root/'capture.json').write_text(json.dumps(manifest, ensure_ascii=False,
        indent=2, allow_nan=False)+'\n', encoding='utf-8')
    return results
