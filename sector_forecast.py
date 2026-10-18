"""Causal next-session sector watch ranking and frozen-constituent evaluation.

This standard-library module produces an explicitly uncalibrated research ranking.
It does not reconstruct past sector membership or claim a historical success rate.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics
from typing import Any

SCHEMA = 'sector-outlook-v1'
METHOD = 'relative-strength-participation-v1'
TZ = timezone(timedelta(hours=8))
MIN_MEMBERS = 5
MIN_COVERAGE = .65
MIN_SECTORS = 3
WINDOW = 21
WEIGHTS = {'relative_return_5d': .35, 'breadth_excess': .25,
           'participation': .20, 'close_location': .10, 'above_ma5': .10}


def _stamp(value: Any) -> datetime:
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('timestamps must include a timezone')
    return result.astimezone(TZ)


def _known(value: Any, cutoff: datetime) -> bool:
    try:
        return _stamp(value) <= cutoff
    except (TypeError, ValueError, OverflowError):
        return False


def _finite(value: Any, positive: bool = False) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (float, int))
            and math.isfinite(value) and (not positive or value > 0))


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def _connection(source):
    if isinstance(source, sqlite3.Connection):
        yield source
    else:
        connection = sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)
        try:
            yield connection
        finally:
            connection.close()


def _rows(db, sql, args=()):
    cursor = db.execute(sql, args)
    keys = [column[0] for column in cursor.description]
    for row in cursor:
        yield dict(zip(keys, row))


def _membership(db, cutoff):
    latest, rejected = {}, 0
    for row in _rows(db, 'SELECT symbol,classification,industry,effective_date,observed_at,source_url FROM industry_members'):
        try:
            effective = date.fromisoformat(row['effective_date'])
        except (TypeError, ValueError):
            rejected += 1
            continue
        if (effective > cutoff.date() or not _known(row['observed_at'], cutoff)
                or not row['industry'] or not row['classification']):
            rejected += 1
            continue
        key = row['classification'], row['symbol']
        version = effective, _stamp(row['observed_at'])
        previous = latest.get(key)
        if previous is None or version > previous[0]:
            latest[key] = version, [row]
        elif version == previous[0]:
            previous[1].append(row)
    groups, ambiguous = defaultdict(dict), 0
    for (classification, symbol), (_, observations) in latest.items():
        if len({row['industry'] for row in observations}) != 1:
            ambiguous += 1
            continue
        groups[classification][symbol] = observations[-1]
    return groups, {'excluded_unknown_or_future_rows': rejected,
                    'ambiguous_symbol_memberships': ambiguous}


def _valid_bar(row, cutoff):
    if (row.get('conflict') != 0 or not _known(row.get('fetched_at'), cutoff)
            or row.get('finality') not in ('historical_provider', 'post_close_provider')):
        return False
    if row.get('source_updated_at') and not _known(row['source_updated_at'], cutoff):
        return False
    if not all(_finite(row.get(key), True) for key in ('open', 'high', 'low', 'close', 'amount')):
        return False
    return (row['high'] >= max(row['open'], row['close']) >= row['low']
            and row['high'] >= min(row['open'], row['close']) >= row['low'])


def _features(db, base, cutoff):
    # Fetch only a bounded trailing window; no target-session values enter features.
    start = (date.fromisoformat(base) - timedelta(days=100)).isoformat()
    by_symbol, dates, sources = defaultdict(dict), set(), set()
    excluded = 0
    for row in _rows(db, '''SELECT symbol,trade_date,open,high,low,close,amount,
            conflict,fetched_at,source_updated_at,source,finality FROM daily_quotes
            WHERE trade_date>=? AND trade_date<=?''', (start, base)):
        if not _valid_bar(row, cutoff):
            excluded += 1
            continue
        by_symbol[row['symbol']][row['trade_date']] = row
        dates.add(row['trade_date'])
        sources.add(row['source'])
    sessions = sorted(dates)[-WINDOW:]
    if len(sessions) < WINDOW or sessions[-1] != base:
        return {}, sessions, sorted(sources), excluded
    result = {}
    for symbol, bars in by_symbol.items():
        if any(day not in bars for day in sessions):
            continue
        values = [bars[day] for day in sessions]
        closes = [row['close'] for row in values]
        returns = [b / a - 1 for a, b in zip(closes, closes[1:])]
        # Large raw discontinuities can be corporate actions rather than sector signals.
        if any(abs(value) > .30 for value in returns):
            continue
        last = values[-1]
        span = last['high'] - last['low']
        amount_ratio = last['amount'] / statistics.mean(row['amount'] for row in values[:-1])
        result[symbol] = {
            'base_close': last['close'], 'return_1d': returns[-1],
            'return_5d': closes[-1] / closes[-6] - 1,
            'return_20d': closes[-1] / closes[0] - 1,
            'above_ma5': float(closes[-1] > statistics.mean(closes[-5:])),
            'up': float(returns[-1] > 0),
            'participation': float(returns[-1] > 0 and amount_ratio >= 1),
            'close_location': (last['close'] - last['low']) / span if span > 0 else .5,
            'amount_ratio20': amount_ratio,
        }
    return result, sessions, sorted(sources), excluded


def _percentile(value, values):
    if len(values) <= 1:
        return .5
    lower = sum(other < value for other in values)
    equal = sum(other == value for other in values)
    return (lower + (equal - 1) / 2) / (len(values) - 1)


def generate(db, base: str, nextsession: str, cutoff: str) -> dict:
    """Return a watch ranking using only rows observed by the explicit cutoff.

    ``db`` may be an existing read-only connection or a filesystem path. Membership
    may have been learned after ``base`` when the forecast is genuinely issued later;
    ``information_cutoff`` and ``generated_at`` expose that distinction.
    """
    base_day, target_day = date.fromisoformat(base), date.fromisoformat(nextsession)
    known_at = _stamp(cutoff)
    created = datetime.now(TZ)
    if target_day <= base_day:
        raise ValueError('target must be after base')
    if known_at < datetime.combine(base_day, time(15), TZ):
        raise ValueError('daily close is unavailable before 15:00 on base date')
    if known_at >= datetime.combine(target_day, time(9, 30), TZ):
        raise ValueError('outlook cutoff must precede target continuous opening')
    if known_at > created + timedelta(seconds=5):
        raise ValueError('information cutoff is in the future')
    payload = {
        'schema': SCHEMA, 'schema_version': SCHEMA, 'method': METHOD,
        'base_date': base, 'target_date': nextsession,
        'base_trade_date': base, 'target_trade_date': nextsession,
        'information_cutoff': known_at.isoformat(), 'generated_at': created.isoformat(),
        'status': 'insufficient_data', 'classification': None,
        'forecast_definition': '次日板块等权收益相对已覆盖股票等权基准的强弱排序；绝对上涨单独记录',
        'score_definition': '0–100 技术观察评分，不是上涨概率或交易胜率',
        'no_probability_claim': True, 'formal_trade_ready': False,
        'forward_eligible': created < datetime.combine(target_day, time(9, 30), TZ),
        'rankings': [], 'frozen_members': {}, 'coverage': {}, 'sources': {},
        'validation': {'status': 'forward_tracking_only', 'historical_win_rate': None,
                       'forward_resolved_dates': 0, 'relative_strength_hit_rate': None,
                       'absolute_up_rate': None, 'gross_trade_win_rate': None,
                       'note': '使用发布前冻结的成分与昨收，下一交易日收盘后核对；不以当前成分回填历史胜率。'},
        'methodology': {
            'weights': WEIGHTS, 'min_members': MIN_MEMBERS,
            'minimum_member_history_coverage': MIN_COVERAGE,
            'required_market_sessions': WINDOW,
            'rank_input': '当前时点可知成分的最近21个交易日未复权行情；不含次日数据',
            'overheat_penalty': '当日板块平均涨幅超过3%开始扣分，最多15分',
            'forward_labels': {'relative_strength': 'sector_equal_weight_return > frozen_covered_market_equal_weight_return',
                               'top_quartile': 'sector_return_rank <= ceil(frozen_sector_count/4)',
                               'absolute_up': 'sector_equal_weight_return > 0'},
            'missing_target_quotes': '成分缺失则该板块不结案；基准不完整时不计相对强弱命中；所有板块齐备才计前四分之一命中',
        },
        'limitations': ['评分未经历史样本外验证，不提供80%胜率或3%–6%收益承诺。',
                       '采用当前可知的供应商行业分类，成分历史与退市股票覆盖不足。',
                       '未复权行情可能受公司行动影响；未纳入资金流、新闻催化与官方交易状态。',
                       '相对强势可与板块绝对下跌同时发生；板块方向不等于个股可成交利润。'],
    }
    with _connection(db) as connection:
        groups, membership_audit = _membership(connection, known_at)
        features, sessions, quote_sources, excluded_quotes = _features(connection, base, known_at)
    providers = []
    for classification, members in groups.items():
        sectors = defaultdict(list)
        for symbol, row in members.items():
            sectors[row['industry']].append(symbol)
        eligible = {name: symbols for name, symbols in sectors.items()
                    if sum(symbol in features for symbol in symbols) >= MIN_MEMBERS
                    and sum(symbol in features for symbol in symbols) / len(symbols) >= MIN_COVERAGE}
        providers.append((classification, members, eligible))
    available = [provider for provider in providers if len(provider[2]) >= MIN_SECTORS]
    payload['coverage'] = {**membership_audit, 'eligible_stock_histories': len(features),
                           'known_market_sessions': len(sessions),
                           'first_feature_date': sessions[0] if sessions else None,
                           'last_feature_date': sessions[-1] if sessions else None,
                           'excluded_quote_rows': excluded_quotes,
                           'provider_sector_counts': {name: len(sectors) for name, _, sectors in providers},
                           'covered_sectors': 0, 'covered_stocks': 0}
    payload['sources'] = {'daily_quote_sources': quote_sources, 'membership_urls': []}
    if not available:
        payload['reason'] = '截至报告信息截止时间，可用行业成分或连续21个交易日行情不足，暂不发布次日板块排序。'
        return payload
    priority = {'sina_industry': 2, 'exchange_industry': 1}
    classification, members, sectors = max(available,
        key=lambda item: (priority.get(item[0], 0), len(item[2]), item[0]))
    frozen, excluded_members = {}, 0
    for sector, symbols in sorted(sectors.items()):
        kept = sorted(symbol for symbol in symbols if symbol in features)
        excluded_members += len(symbols) - len(kept)
        frozen[sector] = [{'symbol': symbol, 'base_close': features[symbol]['base_close'],
                           'membership_effective_date': members[symbol]['effective_date'],
                           'membership_observed_at': members[symbol]['observed_at']}
                          for symbol in kept]
    market_symbols = sorted({row['symbol'] for rows in frozen.values() for row in rows})
    market_1d = statistics.mean(features[symbol]['return_1d'] for symbol in market_symbols)
    market_5d = statistics.mean(features[symbol]['return_5d'] for symbol in market_symbols)
    market_breadth = statistics.mean(features[symbol]['up'] for symbol in market_symbols)
    raw = []
    for sector, rows in frozen.items():
        values = [features[row['symbol']] for row in rows]
        mean = lambda key: statistics.mean(value[key] for value in values)
        breadth = mean('up')
        raw.append({'sector': sector, 'member_count': len(rows),
                    'coverage': len(rows) / len(sectors[sector]),
                    'components': {'return_1d': mean('return_1d'), 'return_5d': mean('return_5d'),
                                   'relative_return_5d': mean('return_5d') - market_5d,
                                   'return_20d': mean('return_20d'), 'breadth': breadth,
                                   'breadth_excess': breadth - market_breadth,
                                   'participation': mean('participation'),
                                   'close_location': mean('close_location'),
                                   'above_ma5': mean('above_ma5'),
                                   'amount_ratio20': mean('amount_ratio20')}})
    for row in raw:
        component = row['components']
        ranks = {key: _percentile(component[key], [other['components'][key] for other in raw])
                 for key in WEIGHTS}
        penalty = min(15.0, max(0.0, component['return_1d'] - .03) * 500)
        row['score'] = round(max(0.0, sum(WEIGHTS[key] * ranks[key] for key in WEIGHTS) * 100 - penalty), 2)
        row['overheat_penalty'] = round(penalty, 2)
        row['drivers'] = [f"近5日相对覆盖市场 {component['relative_return_5d']:+.2%}",
                          f"成分上涨比例 {component['breadth']:.0%}，高于市场 {component['breadth_excess']:+.0%}",
                          f"放量上涨成分占比 {component['participation']:.0%}"]
        row['risks'] = []
        row['invalidation'] = '次日开盘若板块扩散与相对强度回落，则撤销优先观察；缺乏可成交价格时不入选。'
        if component['return_1d'] > .03:
            row['risks'].append('当日涨幅较大，已扣除追高分；次日高开需重新确认。')
        if component['breadth'] < .5:
            row['risks'].append('上涨成分不足一半，板块扩散较弱。')
        if row['coverage'] < .9:
            row['risks'].append('部分成分缺少连续历史，排序覆盖不完整。')
        if component['relative_return_5d'] <= 0:
            row['risks'].append('近5日仍弱于覆盖市场，属于转强观察。')
    ranked = sorted(raw, key=lambda row: (-row['score'], row['sector']))
    for index, row in enumerate(ranked, 1):
        row['rank'] = index
    metadata_rows = [members[symbol] for symbol in market_symbols]
    payload.update({'status': 'ready', 'classification': classification, 'rankings': ranked,
                    'frozen_members': frozen, 'constituents_sha256': _digest(frozen),
                    'benchmark': {'definition': '冻结覆盖股票等权，不代表全A或官方行业指数',
                                  'member_count': len(market_symbols),
                                  'base_return_1d': market_1d, 'base_return_5d': market_5d,
                                  'base_breadth': market_breadth}})
    payload['coverage'].update({'covered_sectors': len(ranked), 'covered_stocks': len(market_symbols),
                                'excluded_incomplete_sector_members': excluded_members,
                                'selected_classification_members': len(members)})
    payload['sources'].update({'membership_urls': sorted({row['source_url'] for row in metadata_rows}),
                               'membership_latest_observed_at': max(_stamp(row['observed_at']) for row in metadata_rows).isoformat(),
                               'membership_effective_after_price_base': any(row['effective_date'] > base for row in metadata_rows)})
    payload['forecast_id'] = _digest({key: payload[key] for key in
        ('schema', 'method', 'base_date', 'target_date', 'information_cutoff', 'constituents_sha256', 'rankings')})
    return payload


def evaluate(db, forecast: dict, cutoff: str) -> dict:
    """Evaluate frozen sectors without fetching/reconstructing a later membership set."""
    if forecast.get('schema') != SCHEMA or forecast.get('status') != 'ready':
        raise ValueError('ready sector-outlook-v1 forecast required')
    frozen = forecast['frozen_members']
    if _digest(frozen) != forecast.get('constituents_sha256'):
        raise ValueError('frozen constituent hash differs')
    known_at = _stamp(cutoff)
    target = forecast['target_date']
    if known_at < datetime.combine(date.fromisoformat(target), time(15), TZ):
        raise ValueError('target close is unavailable before 15:00')
    symbols = {row['symbol'] for rows in frozen.values() for row in rows}
    returns = {}
    with _connection(db) as connection:
        for row in _rows(connection, '''SELECT symbol,trade_date,open,high,low,close,amount,
                conflict,fetched_at,source_updated_at,source,finality FROM daily_quotes
                WHERE trade_date=?''', (target,)):
            if row['symbol'] in symbols and _valid_bar(row, known_at):
                returns[row['symbol']] = row['close']
    base_closes = {row['symbol']: row['base_close'] for rows in frozen.values() for row in rows}
    stock_returns = {symbol: close / base_closes[symbol] - 1 for symbol, close in returns.items()}
    complete_market = len(stock_returns) == len(symbols)
    benchmark = statistics.mean(stock_returns.values()) if complete_market else None
    results = []
    for sector, members in frozen.items():
        missing = sorted(row['symbol'] for row in members if row['symbol'] not in stock_returns)
        gross = statistics.mean(stock_returns[row['symbol']] for row in members) if not missing else None
        results.append({'sector': sector, 'status': 'resolved' if not missing else 'unresolved',
                        'missing_symbols': missing, 'return': gross,
                        'absolute_up': gross > 0 if gross is not None else None,
                        'relative_strength': gross > benchmark if gross is not None and benchmark is not None else None,
                        'top_quartile': None})
    if all(row['status'] == 'resolved' for row in results):
        ordered = sorted(results, key=lambda row: (-row['return'], row['sector']))
        # Tied outcomes receive the same competition rank; no arbitrary tie-break hit.
        for row in ordered:
            rank = 1 + sum(other['return'] > row['return'] for other in ordered)
            row['realized_rank'] = rank
            row['top_quartile'] = rank <= math.ceil(len(ordered) / 4)
    top_sectors = {row['sector'] for row in forecast['rankings'][:3]}
    top = [row for row in results if row['sector'] in top_sectors]
    return {'schema': 'sector-outlook-evaluation-v1', 'forecast_id': forecast.get('forecast_id'),
            'base_date': forecast['base_date'], 'target_date': target,
            'information_cutoff': known_at.isoformat(),
            'forward_eligible': forecast.get('forward_eligible') is True,
            'status': 'resolved' if complete_market else 'unresolved',
            'benchmark_return': benchmark, 'expected_stocks': len(symbols),
            'observed_stocks': len(stock_returns), 'missing_stocks': len(symbols) - len(stock_returns),
            'sectors': results, 'top3_evaluated_sectors': len(top),
            'top3_relative_strength_hits': sum(row['relative_strength'] is True for row in top) if complete_market else None,
            'top3_absolute_up_hits': sum(row['absolute_up'] is True for row in top) if all(row['status'] == 'resolved' for row in top) else None,
            'top3_top_quartile_hits': sum(row['top_quartile'] is True for row in top) if complete_market else None,
            'gross_trade_win_rate': None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--base', required=True)
    parser.add_argument('--nextsession', required=True)
    parser.add_argument('--cutoff', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = generate(args.db, args.base, args.nextsession, args.cutoff)
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'base_date': args.base,
                      'target_date': args.nextsession, 'sectors': len(result['rankings'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
