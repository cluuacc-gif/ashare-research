"""Frozen-stock D1/D2/D3 price-event evaluation, using the standard library only.

A reported opening price is not an order execution. This module never computes
an actual strategy win rate, and never removes a pending selected stock.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from trade_calendar import adjacent

SCHEMA = 'stock-strength-evaluation-v1'
TZ = timezone(timedelta(hours=8))
PRICE_EVENT = 'OPEN_REFERENCE_PRICE_EVENT'


def _stamp(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('stock-strength timestamps must include timezone')
    return result.astimezone(TZ)


def _known(value, cutoff):
    try:
        return _stamp(value)<=cutoff
    except (TypeError, ValueError, OverflowError):
        return False


def _positive(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value>0)


@contextmanager
def _connection(source):
    if isinstance(source, sqlite3.Connection):
        yield source
    else:
        connection = sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro', uri=True)
        try:
            yield connection
        finally:
            connection.close()


def _rows(db, sql, args=()):
    cursor = db.execute(sql, args)
    keys = [item[0] for item in cursor.description]
    for row in cursor:
        yield dict(zip(keys, row))


def _valid_bar(row, cutoff):
    if not row or row.get('conflict')!=0 or row.get('finality') not in ('historical_provider', 'post_close_provider'):
        return False
    if not _known(row.get('fetched_at'), cutoff):
        return False
    if row.get('source_updated_at') and not _known(row['source_updated_at'], cutoff):
        return False
    if not all(_positive(row.get(key)) for key in ('open', 'high', 'low', 'close', 'volume')):
        return False
    try:
        closing = datetime.combine(date.fromisoformat(row['trade_date']), time(15), TZ)
        # A daily close cannot be known before that day's close, even if a
        # malformed provider record labels it as final.
        if cutoff<closing or _stamp(row['fetched_at'])<closing:
            return False
        if row.get('source_updated_at') and _stamp(row['source_updated_at'])<closing:
            return False
    except (ValueError, TypeError, OverflowError):
        return False
    return (row['high']>=max(row['open'], row['close'], row['low'])
            and row['low']<=min(row['open'], row['close'], row['high']))


def _status(days, bars, cutoff):
    if any(cutoff<datetime.combine(date.fromisoformat(day), time(15), TZ) for day in days):
        return 'pending'
    return 'resolved' if all(day in bars for day in days) else 'unresolved'


def evaluate(db, forecast_outlook, cutoff):
    """Evaluate only the first three frozen primary stocks, without reranking.

    ``forecast_outlook`` needs base_trade_date, target_trade_date, generated_at,
    sector_stock_selection.primary. Unknown bars remain null. D2 outcomes are
    retained when D3 is still pending or missing.
    """
    known_at = _stamp(cutoff)
    base, first = forecast_outlook['base_trade_date'], forecast_outlook['target_trade_date']
    date.fromisoformat(base); date.fromisoformat(first)
    second, third = adjacent(first, 1), adjacent(adjacent(first, 1), 1)
    generated = _stamp(forecast_outlook['generated_at'])
    before_open = generated<datetime.combine(date.fromisoformat(first), time(9, 30), TZ)
    adjacent_target = adjacent(base, 1)==first
    eligible = before_open and adjacent_target and generated<=known_at
    selected = list(forecast_outlook.get('sector_stock_selection', {}).get('primary') or [])[:3]
    frozen = [{'symbol': row.get('symbol'), 'name': row.get('name'), 'close': row.get('close')}
              for row in selected]
    digest = hashlib.sha256(json.dumps({'base': base, 'target': first, 'generated_at': generated.isoformat(),
        'primary': frozen}, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    common = {'schema': SCHEMA, 'forecast_id': forecast_outlook.get('forecast_id'),
        'frozen_selection_sha256': digest, 'base_trade_date': base, 'target_trade_date': first,
        'D1_trade_date': first, 'D2_trade_date': second, 'D3_trade_date': third,
        'generated_at': generated.isoformat(), 'information_cutoff': known_at.isoformat(),
        'forward_eligible': bool(eligible), 'measurement': PRICE_EVENT,
        'opening_reference_is_actual_execution': False, 'actual_win_rate': None,
        'real_execution_win_rate': None, 'formal_trade_ready': False,
        'n_selected': len(selected), 'n_selected_beyond_three_ignored': max(0, len(forecast_outlook.get('sector_stock_selection', {}).get('primary') or [])-3)}
    if not eligible:
        return {**common, 'status': 'ineligible_forecast', 'rows': [],
            'reason': 'forecast_must_be_known_before_target_open_and_target_must_follow_base',
            'n_D2_resolved': 0, 'n_strength3_resolved': 0,
            'D2_open_reference_profitable_rate': None, 'strength3_rate': None}
    days = (first, second, third)
    symbols = {row.get('symbol') for row in selected if isinstance(row.get('symbol'), str)}
    bars_by_symbol = {symbol: {} for symbol in symbols}
    invalid = 0
    with _connection(db) as connection:
        for day in days:
            # Future target rows are never loaded into event calculations.
            if known_at<datetime.combine(date.fromisoformat(day), time(15), TZ):
                continue
            for row in _rows(connection, '''SELECT symbol,trade_date,open,high,low,close,volume,
                    conflict,fetched_at,source_updated_at,source,finality FROM daily_quotes
                    WHERE trade_date=?''', (day,)):
                if row['symbol'] not in symbols:
                    continue
                if _valid_bar(row, known_at):
                    bars_by_symbol[row['symbol']][day] = row
                else:
                    invalid += 1
    result, seen = [], set()
    for stock in frozen:
        symbol = stock.get('symbol')
        valid_selection = isinstance(symbol, str) and symbol not in seen and _positive(stock.get('close'))
        seen.add(symbol) if isinstance(symbol, str) else None
        bars = bars_by_symbol.get(symbol, {}) if valid_selection else {}
        first_bar, second_bar, third_bar = (bars.get(day) for day in days)
        d2_status = _status((first, second), bars, known_at) if valid_selection else 'unresolved'
        d3_status = _status(days, bars, known_at) if valid_selection else 'unresolved'
        d1_up = first_bar['close']>first_bar['open'] if first_bar else None
        d2_up = second_bar['close']>first_bar['close'] if first_bar and second_bar else None
        d3_up = third_bar['close']>second_bar['close'] if second_bar and third_bar else None
        gross = second_bar['close']/first_bar['open']-1 if d2_status=='resolved' else None
        strength2 = bool(d1_up and d2_up) if d2_status=='resolved' else None
        strength3 = bool(d1_up and d2_up and d3_up) if d3_status=='resolved' else None
        observations = {day: {key: bar[key] for key in ('open', 'close', 'source', 'fetched_at', 'source_updated_at', 'finality')}
                        for day, bar in bars.items()}
        result.append({**stock, 'valid_frozen_selection': valid_selection,
            'D1_close_above_open': d1_up, 'D2_close_above_D1_close': d2_up,
            'D3_close_above_D2_close': d3_up, 'strength2_label': strength2,
            'strength3_label': strength3, 'D2_price_event_status': d2_status,
            'strength3_status': d3_status, 'D2_open_reference_gross_return': gross,
            'D2_open_reference_profitable': gross>0 if gross is not None else None,
            'D2_open_reference_ge3': gross>=.03-1e-12 if gross is not None else None,
            'D2_open_reference_ge6': gross>=.06-1e-12 if gross is not None else None,
            'measurement': PRICE_EVENT, 'actual_win_rate': None, 'execution_verified': False,
            'one_price_bar_present': any(bar['high']==bar['low'] for bar in bars.values()),
            'missing_or_unavailable_dates': [day for day in days if day not in bars],
            'observations': observations})
    d2 = [row for row in result if row['D2_price_event_status']=='resolved']
    d3 = [row for row in result if row['strength3_status']=='resolved']
    count = lambda key, rows: sum(row[key] is True for row in rows)
    return {**common, 'status': ('resolved' if len(d3)==len(selected) and selected else
                                'pending' if any(row['strength3_status']=='pending' for row in result) else 'unresolved'),
        'rows': result, 'n_invalid_or_future_observations': invalid,
        'n_D2_resolved': len(d2), 'n_D2_pending': sum(row['D2_price_event_status']=='pending' for row in result),
        'n_D2_unresolved': sum(row['D2_price_event_status']=='unresolved' for row in result),
        'n_D2_open_reference_profitable': count('D2_open_reference_profitable', d2),
        'n_D2_open_reference_ge3': count('D2_open_reference_ge3', d2),
        'n_D2_open_reference_ge6': count('D2_open_reference_ge6', d2),
        'D2_open_reference_profitable_rate': count('D2_open_reference_profitable', d2)/len(d2) if d2 else None,
        'D2_open_reference_ge3_rate': count('D2_open_reference_ge3', d2)/len(d2) if d2 else None,
        'n_strength2': count('strength2_label', d2),
        'n_strength3_resolved': len(d3), 'n_strength3_pending': sum(row['strength3_status']=='pending' for row in result),
        'n_strength3_unresolved': sum(row['strength3_status']=='unresolved' for row in result),
        'n_strength3': count('strength3_label', d3),
        'strength3_rate': count('strength3_label', d3)/len(d3) if d3 else None,
        'event_rate_denominator': 'resolved_price_events_only_unknown_counts_reported_separately_not_trade_win_rate',
        'limitations': ['Opening reference prices are not executable fills; live opening confirmation and account executions are absent.',
                       'D2 close reference profits are price events, not the target/stop trading policy.',
                       'Unknown or future observations remain pending/unresolved, never counted as wins.',
                       'Price-event coverage and unknown counts must accompany every displayed rate.']}
