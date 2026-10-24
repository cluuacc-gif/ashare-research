"""Cutoff-aware candidate risk screen; never asserts official normal status."""
from __future__ import annotations
import datetime as dt
import re
import unicodedata


def _instant(value):
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(dt.timezone(dt.timedelta(hours=8))) if result.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def normalized_name(value):
    return ''.join(unicodedata.normalize('NFKC', value).split()).upper() if isinstance(value, str) else ''


def load_metadata(connection, symbols, base=None, cutoff=None):
    columns = {row[1] for row in connection.execute('PRAGMA table_info(security_master)')}
    required = {'symbol', 'name', 'last_seen_at'}
    if not required <= columns:
        return {}
    fields = ['symbol', 'name', 'last_seen_at'] + (['board'] if 'board' in columns else [])
    result = {}
    symbols = sorted(set(symbols))
    for offset in range(0, len(symbols), 400):
        batch = symbols[offset:offset + 400]
        marks = ','.join('?' for _ in batch)
        for row in connection.execute('SELECT '+','.join(fields)+' FROM security_master WHERE symbol IN ('+marks+')', batch):
            result[row[0]] = dict(zip(fields, row))
    status_columns = {row[1] for row in connection.execute('PRAGMA table_info(security_status_daily)')}
    status_fields = ['symbol','observed_at','is_st','delisting','trading_status','verified','published_at']
    if base and cutoff and set(status_fields + ['trade_date']) <= status_columns:
        known_at = _instant(cutoff)
        for offset in range(0, len(symbols), 400):
            batch = symbols[offset:offset + 400]
            marks = ','.join('?' for _ in batch)
            query = ('SELECT '+','.join(status_fields)+' FROM security_status_daily WHERE trade_date=? '
                     'AND symbol IN ('+marks+') ORDER BY observed_at')
            for row in connection.execute(query, [base,*batch]):
                status = dict(zip(status_fields,row))
                observed, published = _instant(status['observed_at']), _instant(status['published_at'])
                if (status['verified'] == 1 and observed is not None and known_at is not None
                        and observed <= known_at and (status['published_at'] is None or
                            published is not None and published <= observed)):
                    result.setdefault(row[0], {})['known_official_status'] = status
    return result


def rejection_reasons(symbol, metadata, cutoff, *, base=None):
    reasons = []
    code = symbol.split('.')[0] if isinstance(symbol, str) else ''
    board = normalized_name(metadata.get('board')) if isinstance(metadata, dict) else ''
    if code.startswith('30') or board in ('创业板', 'CHINEXT'):
        reasons.append('excluded_chinext_board')
    if not re.fullmatch(r'[0-9]{6}\.(SZ|SH|BJ)', symbol or ''):
        reasons.append('invalid_security_symbol')
    metadata = metadata if isinstance(metadata, dict) else {}
    name = normalized_name(metadata.get('name'))
    observed, known_at = _instant(metadata.get('last_seen_at')), _instant(cutoff)
    if (not name or observed is None or known_at is None or observed > known_at
            or base is not None and observed.date().isoformat() < base):
        reasons.append('security_name_or_asof_metadata_unknown')
    status = metadata.get('known_official_status') or {}
    if status.get('is_st') == 1 or status.get('delisting') == 1:
        reasons.append('excluded_known_official_ST_or_delisting_status')
    if 'ST' in name or '退' in name:
        reasons.append('excluded_ST_or_delisting_name')
    return reasons
