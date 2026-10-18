#!/usr/bin/env python3
"""Prior-close opening watch plans; opening quotes never establish actual fills.

This module uses only the standard library. A plan is independent research, not
an enabled trading policy. It deliberately does not reuse the failed historical
gross-strategy policy or claim that its research score is a probability.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import decimal
import json
import math
from pathlib import Path
import sqlite3
import statistics
from zoneinfo import ZoneInfo

VERSION = 'opening-strength-research-v1'
TZ = ZoneInfo('Asia/Shanghai')
APP_ID = 0x41534851
MAXIMUM_WATCHLIST = 5
MINIMUM_BARS = 250
MINIMUM_MEDIAN_AMOUNT20 = 20_000_000
MAXIMUM_OPEN_GAP = .025
MAXIMUM_QUOTE_AGE_SECONDS = 60
PROTOCOL = {
    'schema': VERSION, 'features_end': 'base_session_close',
    'entry_observation_window': ['09:30:00', '09:35:00'],
    'opening_snapshot_maximum_age_seconds': MAXIMUM_QUOTE_AGE_SECONDS,
    'entry_reference_price_range': [5, 10],
    'minimum_prior_bars': MINIMUM_BARS,
    'minimum_prior_bars_fields': ['open', 'high', 'low', 'close', 'volume'],
    'amount_feature_window_bars': 21,
    'minimum_median_amount20_cny': MINIMUM_MEDIAN_AMOUNT20,
    'opening_gap_range': [0, MAXIMUM_OPEN_GAP],
    'frozen_cap': 'floor_to_cent(min(base_close*1.025,10))',
    'live_reference_strength': 'last>=open*1.002',
    'near_upper_limit_buffer_cny': .02,
    'maximum_watchlist': MAXIMUM_WATCHLIST,
    'score_is_probability': False, 'costs_included': False,
    'd1_close_is_feature': False, 'd1_high_is_feature': False,
    'd1_green_required_before_entry': False,
    'daily_prices_or_opening_quotes_prove_fill': False,
    'same_day_exit': False, 'exit_day': 'second_trading_session_after_base',
    'take_profit_references': [.03, .06],
    'initial_capital_cny': 30000, 'position_budget_cny': 10000,
    'maximum_positions': 3, 'lot_shares': 100,
    'same_session_budget_reallocation': False,
    'formal_policy_enabled': False, 'win_rate_80_verified': False,
}


def _now():
    return dt.datetime.now(TZ)


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _instant(value):
    if not isinstance(value, str):
        return None
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(TZ) if result.tzinfo is not None else None
    except ValueError:
        return None


def _known_by(value, cutoff):
    instant = _instant(value)
    return instant is not None and instant <= cutoff


def _cent_floor(value):
    return float(decimal.Decimal(str(value)).quantize(decimal.Decimal('.01'),
                                                     rounding=decimal.ROUND_FLOOR))


@contextlib.contextmanager
def _connection(database):
    if isinstance(database, sqlite3.Connection):
        yield database
        return
    path = Path(database).resolve()
    with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        yield connection


def _rows(connection, sql, values=()):
    cursor = connection.execute(sql, values)
    keys = [column[0] for column in cursor.description]
    return [dict(zip(keys, row)) for row in cursor]


def _valid_bar(row):
    values = [_number(row.get(key)) for key in ('open', 'high', 'low', 'close', 'volume')]
    if any(value is None or value <= 0 for value in values) or row.get('conflict'):
        return False
    open_, high, low, close, _ = values
    return low <= min(open_, close) <= max(open_, close) <= high


def _final_source_time_valid(row, cutoff):
    if row.get('finality') not in ('historical_provider', 'post_close_provider'):
        return False
    if not row.get('source_updated_at'):
        return True
    source_at = _instant(row['source_updated_at'])
    fetched_at = _instant(row.get('fetched_at'))
    return (source_at is not None and fetched_at is not None
            and source_at.date().isoformat() == row['trade_date']
            and source_at <= fetched_at <= cutoff)


def _sector_membership(connection, cutoff, classification=None):
    """Current forward context only; never apply today's members to a backtest."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'industry_members' not in tables:
        return {}, 'industry_members_table_missing'
    rows = _rows(connection, '''SELECT symbol,classification,industry,effective_date,observed_at,source_url
        FROM industry_members WHERE effective_date<=? ORDER BY observed_at DESC''',
                 (cutoff.date().isoformat(),))
    members = {}
    for row in rows:
        if classification and row['classification'] != classification:
            continue
        if not _known_by(row['observed_at'], cutoff):
            continue
        # Prefer the same public industry taxonomy used by the sector forecast.
        if row['symbol'] not in members or (row['classification'] == 'sina_industry'
                                            and members[row['symbol']]['classification'] != 'sina_industry'):
            members[row['symbol']] = row
    return members, None if members else 'no_membership_observed_before_cutoff'


def _sector_scores(forecast, base, target, cutoff):
    if not isinstance(forecast, dict):
        return {}, 'sector_forecast_unavailable'
    forecast_base = forecast.get('base_date', forecast.get('base_trade_date'))
    forecast_target = forecast.get('target_date', forecast.get('target_session'))
    if forecast_base != base or forecast_target != target:
        return {}, 'sector_forecast_date_mismatch'
    known_at = forecast.get('information_cutoff')
    if not _known_by(known_at, cutoff):
        return {}, 'sector_forecast_cutoff_unknown_or_future'
    if forecast.get('status') in ('unavailable', 'failed', 'blocked'):
        return {}, 'sector_forecast_unavailable'
    rankings = forecast.get('rankings', [])
    scores = {}
    for row in rankings:
        sector = row.get('sector', row.get('industry'))
        score = _number(row.get('score'))
        if isinstance(sector, str) and score is not None and 0 <= score <= 100:
            scores[sector] = score
    return scores, None if scores else 'sector_forecast_has_no_usable_rankings'


def _technical_features(rows):
    if len(rows) < MINIMUM_BARS or not all(_valid_bar(row) for row in rows[-MINIMUM_BARS:]):
        return None
    current = rows[-1]
    if float(current['high']) <= float(current['low']):
        return None
    recent = rows[-21:]
    amounts = [_number(row.get('amount')) for row in recent]
    if any(value is None or value <= 0 for value in amounts):
        return None
    # The 250-bar history requirement concerns valid OHLC and volume. The
    # liquidity features use exactly 21 amounts; older missing amounts are not
    # imputed, converted or included in a calculation.
    closes = [float(row['close']) for row in recent]
    close = closes[-1]
    span = float(current['high']) - float(current['low'])
    daily_returns = [closes[index] / closes[index - 1] - 1 for index in range(len(closes) - 20, len(closes))]
    return {
        'ret1': close / closes[-2] - 1, 'ret5': close / closes[-6] - 1,
        'ret20': close / closes[-21] - 1,
        'ma5_gap': close / statistics.mean(closes[-5:]) - 1,
        'ma20_gap': close / statistics.mean(closes[-20:]) - 1,
        'median_amount20': statistics.median(amounts[-20:]),
        'amount_ratio20': amounts[-1] / statistics.mean(amounts[-21:-1]),
        'volatility20': statistics.pstdev(daily_returns),
        'close_location': (close - float(current['low'])) / span,
        'range_fraction': span / closes[-2],
        'distance_below_prior_high20': max(float(row['high']) for row in recent[:-1]) / close - 1,
    }


def _pattern(features):
    if (features['median_amount20'] < MINIMUM_MEDIAN_AMOUNT20 or features['volatility20'] > .045
            or features['range_fraction'] > .09 or not -.035 <= features['ret1'] <= .06
            or not .45 <= features['close_location'] <= .95
            or not .6 <= features['amount_ratio20'] <= 3
            or not -.03 <= features['ma20_gap'] <= .10):
        return None
    if (0 <= features['ret1'] <= .06 and features['close_location'] >= .6
            and features['amount_ratio20'] >= .9):
        return '温和转强'
    if (-.035 <= features['ret1'] <= .01 and features['ret20'] > 0
            and features['ma20_gap'] <= .06):
        return '上升趋势回踩'
    if -.02 <= features['ret1'] <= .03 and -.03 <= features['ret5'] <= .08:
        return '平稳蓄势'
    return None


def _research_score(features, sector_score):
    # Fixed ex-ante ranking, not fitted to the previously exposed diagnostic set.
    location = max(0, 1 - abs(features['close_location'] - .75) / .3)
    trend = max(0, min(1, (features['ma20_gap'] + .03) / .08))
    volume = max(0, 1 - abs(features['amount_ratio20'] - 1.3) / 1.7)
    low_extension = max(0, 1 - max(0, features['ret5']) / .10)
    liquidity = min(1, math.log10(max(1, features['median_amount20'] / MINIMUM_MEDIAN_AMOUNT20)) / 1.5)
    technical = 100 * (.25 * location + .2 * trend + .2 * volume + .2 * low_extension + .15 * liquidity)
    # Missing sector evidence carries no ranking reward or penalty.
    return round(technical if sector_score is None else .8 * technical + .2 * sector_score, 2)


def build_plan(db_path, base_date, target_session, information_cutoff,
               sector_forecast=None, input_sha256=None, *, include_pool=False):
    """Build a frozen conditional watchlist from T-close information only.

    db_path may also be a caller-owned read-only sqlite3.Connection. This module
    performs no writes and no network requests.
    """
    cutoff = _instant(information_cutoff)
    if cutoff is None:
        raise ValueError('information_cutoff must be an explicit timezone-aware timestamp')
    try:
        base_day, target_day = dt.date.fromisoformat(base_date), dt.date.fromisoformat(target_session)
    except (TypeError, ValueError) as error:
        raise ValueError('base and target must be ISO dates') from error
    if target_day <= base_day or cutoff.date() < base_day:
        raise ValueError('target must follow base and cutoff cannot predate base')
    if cutoff > _now():
        raise ValueError('information cutoff cannot be in the future')
    if cutoff >= dt.datetime.combine(target_day, dt.time(9, 30), tzinfo=TZ):
        raise ValueError('prior-close opening plan must be frozen before target session 09:30')
    if cutoff.date() == base_day and cutoff.timetz().replace(tzinfo=None) < dt.time(15):
        raise ValueError('same-session close features are unavailable before 15:00')
    with _connection(db_path) as connection:
        if connection.execute('PRAGMA application_id').fetchone()[0] != APP_ID:
            raise ValueError('not the original market database')
        candidates = _rows(connection, '''SELECT q.symbol,q.close,m.name,q.fetched_at,q.source_updated_at
            FROM daily_quotes q LEFT JOIN security_master m ON q.symbol=m.symbol
            WHERE q.trade_date=? AND q.close BETWEEN 5 AND 10 AND q.conflict=0
            ORDER BY q.symbol''', (base_date,))
        scores, sector_reason = _sector_scores(sector_forecast, base_date, target_session, cutoff)
        classification = sector_forecast.get('classification') if scores else None
        members, membership_reason = _sector_membership(connection, cutoff, classification)
        frozen_members = {}
        if scores and isinstance(sector_forecast.get('frozen_members'), dict):
            for sector, sector_rows in sector_forecast['frozen_members'].items():
                if sector not in scores or not isinstance(sector_rows, list):
                    continue
                for member in sector_rows:
                    if isinstance(member, dict) and isinstance(member.get('symbol'), str):
                        frozen_members[member['symbol']] = (sector, member)
        pool, rejections = [], {}
        def reject(reason):
            rejections[reason] = rejections.get(reason, 0) + 1
        for row in candidates:
            name = row.get('name') or ''
            if not name or 'ST' in name.upper() or '退' in name:
                reject('name_based_risk_or_missing_name'); continue
            if not _known_by(row.get('fetched_at'), cutoff):
                reject('base_quote_not_observed_before_cutoff'); continue
            history = _rows(connection, '''SELECT trade_date,open,high,low,close,volume,amount,
                conflict,fetched_at,source_updated_at,finality FROM daily_quotes WHERE symbol=? AND trade_date<=?
                ORDER BY trade_date DESC LIMIT 270''', (row['symbol'], base_date))
            history.reverse()
            history = history[-MINIMUM_BARS:]
            if any(not _known_by(item.get('fetched_at'), cutoff) for item in history):
                reject('history_not_observed_before_cutoff'); continue
            if any(not _final_source_time_valid(item, cutoff) for item in history):
                reject('history_not_final_or_invalid_source_time'); continue
            features = _technical_features(history)
            if features is None:
                reject('insufficient_or_invalid_prior_bars'); continue
            pattern = _pattern(features)
            if pattern is None:
                reject('technical_watch_conditions_not_met'); continue
            membership = members.get(row['symbol'])
            sector = membership['industry'] if membership else None
            frozen_member = frozen_members.get(row['symbol'])
            if frozen_member and abs((_number(frozen_member[1].get('base_close')) or -1) - float(row['close'])) < .000001:
                sector = frozen_member[0]
                membership = {'classification': classification, 'industry': sector,
                              'source': 'sector_forecast_frozen_members',
                              'observed_at': sector_forecast['information_cutoff']}
            sector_score = scores.get(sector)
            cap = _cent_floor(min(float(row['close']) * (1 + MAXIMUM_OPEN_GAP), 10))
            quantity = int(10000 / cap / 100) * 100
            pool.append({
                'symbol': row['symbol'], 'name': name, 'sector': sector, 'close': float(row['close']),
                'pattern': pattern, 'research_score': _research_score(features, sector_score),
                'score_is_probability': False, 'sector_score': sector_score,
                'sector_membership': membership, 'features': {key: round(value, 8) for key, value in features.items()},
                'feature_last_session': base_date, 'frozen_buy_cap': cap,
                'frozen_position_budget_cny': 10000, 'frozen_quantity_shares': quantity,
                'frozen_reserved_cash_cny': round(quantity * cap, 2),
                'entry_conditions': ['下一交易日09:30—09:35取得带交易所时间的最新盘口',
                    '开盘较前收涨0%—2.5%，实时价格较开盘至少上涨0.2%',
                    f'参考买价5元至{cap:.2f}元，且距官方涨停价至少0.02元',
                    '当日官方正常交易状态、涨跌停价与可见卖盘均核验通过'],
                'cancel_conditions': ['高开超过2.5%或价格超过冻结上限',
                    '涨停封死、停牌、风险状态或盘口过时', '条件不足即放弃，当日不补选、不追加预算'],
                'rationale': [f'{pattern}；近20日成交额中位数{features["median_amount20"] / 1e8:.2f}亿元',
                    f'前收相对20日均线{features["ma20_gap"]:+.1%}，距前20日高点{features["distance_below_prior_high20"]:+.1%}',
                    ('已纳入本期板块展望的相对强弱评分' if sector_score is not None else '暂无可匹配的板块展望，排序仅用价量特征')],
                'formal_trade_eligible': False, 'actual_fill_verified': False,
            })
    pool.sort(key=lambda item: (-item['research_score'], -item['features']['median_amount20'], item['symbol']))
    watchlist = copy.deepcopy(pool[:MAXIMUM_WATCHLIST])
    for index, item in enumerate(watchlist, 1):
        item['rank'] = index
        # Only the first three candidates receive separately reserved research budgets.
        item['budget_slot'] = index if index <= 3 else None
        item['is_reserve_only'] = index > 3
        if index > 3:
            item['frozen_quantity_shares'] = 0
            item['frozen_reserved_cash_cny'] = 0
    result = {
        'schema': VERSION, 'base_date': base_date, 'target_date': target_session,
        'base_trade_date': base_date, 'target_trade_date': target_session,
        'information_cutoff': cutoff.isoformat(), 'input_sha256': input_sha256,
        'status': 'conditional_research_only' if watchlist else 'no_candidate_conditions_met',
        'live_signal': False, 'formal_trade_eligible': False, 'actual_fill_verified': False,
        'win_rate_80_verified': False, 'historical_win_rate': None,
        'score_is_probability': False, 'costs_included': False,
        'holding_plan': 'D1买入、D2按预设3%—6%目标或风险条件观察退出；D1不可卖出',
        'research_policy_independent_of_ineligible_gross_model': True,
        'protocol': PROTOCOL, 'watchlist': watchlist,
        'sector_overlay': {'available': bool(scores), 'reason': sector_reason or membership_reason,
            'classification': classification or 'sina_industry_preferred',
            'covered_candidates': sum(row['sector_score'] is not None for row in pool),
            'membership_observed_before_cutoff_required': True,
            'membership_is_current_forward_context_not_historical_backfill': True},
        'coverage': {'price_band_candidates': len(candidates), 'technical_candidates': len(pool),
            'watchlist_count': len(watchlist), 'rejections': rejections},
        'opening_confirmation': {'status': 'not_observed', 'confirmed': [],
            'reason': '需下一交易日09:30之后的新鲜盘口，09:25仅能观察集合竞价'},
        'limitations': ['研究排序尚未通过隔日真实成交验证，分数不是胜率',
            '名称风险筛查不能替代当日官方状态、涨跌停与复权核验',
            '次日可见卖盘不能证明个人订单已成交，成交与退出需另外记录',
            '最多3个独立预算名额，第4—5名仅供观察，不自动补位'],
    }
    if include_pool:
        result['candidate_pool'] = copy.deepcopy(pool)
    return result


def confirm_plan(plan, snapshot, information_cutoff):
    """Check a frozen plan against fresh timestamped evidence, without orders.

    Snapshot contract: kind='opening_quote_snapshot_v1', observed_at (receiver
    time), quotes=[{symbol,exchange_timestamp,received_at,open,last,prev_close,
    ask1,ask1_volume,status:{verified,is_st,delisting,trading_status},
    limits:{official_verified,limit_up,limit_down}}]. High/low/close are ignored.
    """
    cutoff = _instant(information_cutoff)
    result = {'status': 'unverified', 'confirmed': [], 'rejected': [],
              'information_cutoff': information_cutoff, 'actual_fill_verified': False,
              'formal_trade_eligible': False, 'live_signal': False}
    if cutoff is None or plan.get('schema') != VERSION:
        result['reason'] = 'invalid_plan_or_cutoff'; return result
    if cutoff > _now():
        result['reason'] = 'opening_confirmation_cutoff_is_future'; return result
    frozen_at = _instant(plan.get('information_cutoff'))
    if frozen_at is None or frozen_at > cutoff:
        result['reason'] = 'plan_not_frozen_before_observation'; return result
    if cutoff.date().isoformat() != plan.get('target_date'):
        result['reason'] = 'not_target_session'; return result
    target_open = dt.datetime.combine(cutoff.date(), dt.time(9, 30), tzinfo=TZ)
    if frozen_at >= target_open:
        result['reason'] = 'plan_not_frozen_before_session_open'; return result
    if cutoff.timetz().replace(tzinfo=None) < dt.time(9, 30):
        result.update(status='preopen_watch_only', reason='09:25不能确认连续竞价转强或实际成交')
        return result
    if cutoff.timetz().replace(tzinfo=None) > dt.time(9, 35):
        result['reason'] = 'outside_frozen_opening_observation_window'; return result
    if not isinstance(snapshot, dict) or snapshot.get('kind') != 'opening_quote_snapshot_v1':
        result['reason'] = 'timestamped_opening_snapshot_missing'; return result
    observed = _instant(snapshot.get('observed_at'))
    if observed is None or observed > cutoff or (cutoff - observed).total_seconds() > MAXIMUM_QUOTE_AGE_SECONDS:
        result['reason'] = 'snapshot_missing_future_or_stale_timestamp'; return result
    raw_quotes = snapshot.get('quotes', [])
    if not isinstance(raw_quotes, list):
        result['reason'] = 'invalid_quote_collection'; return result
    quotes = {row.get('symbol'): row for row in raw_quotes if isinstance(row, dict)}
    for candidate in plan.get('watchlist', []):
        symbol = candidate['symbol']
        quote = quotes.get(symbol)
        reason = None
        if candidate.get('is_reserve_only'):
            reason = 'reserve_only_no_budget_slot'
        elif quote is None:
            reason = 'symbol_quote_missing'
        else:
            exchange_at, received_at = _instant(quote.get('exchange_timestamp')), _instant(quote.get('received_at'))
            if (exchange_at is None or received_at is None or exchange_at.date().isoformat() != plan['target_date']
                    or exchange_at.timetz().replace(tzinfo=None) < dt.time(9, 30)
                    or not frozen_at <= exchange_at <= received_at <= observed <= cutoff
                    or (cutoff - exchange_at).total_seconds() > MAXIMUM_QUOTE_AGE_SECONDS):
                reason = 'symbol_timestamp_missing_future_or_stale'
            status, limits = quote.get('status'), quote.get('limits')
            status = status if isinstance(status, dict) else {}
            limits = limits if isinstance(limits, dict) else {}
            if reason is None and not (status.get('verified') is True and status.get('is_st') is False
                    and status.get('delisting') is False and status.get('trading_status') == 'normal'
                    and limits.get('official_verified') is True):
                reason = 'official_status_or_limits_not_verified'
            numbers = {key: _number(quote.get(key)) for key in ('open', 'last', 'prev_close', 'ask1', 'ask1_volume')}
            upper, lower = _number(limits.get('limit_up')), _number(limits.get('limit_down'))
            if reason is None and (any(value is None or value <= 0 for value in numbers.values())
                                   or upper is None or lower is None or not 0 < lower < upper):
                reason = 'invalid_price_limit_or_visible_ask'
            if reason is None:
                close = float(candidate['close'])
                if abs(numbers['prev_close'] - close) > .011:
                    reason = 'previous_close_differs_from_frozen_reference'
                elif not -1e-10 <= numbers['open'] / close - 1 <= MAXIMUM_OPEN_GAP + 1e-10:
                    reason = 'opening_gap_outside_frozen_range'
                elif not 5 <= numbers['last'] <= candidate['frozen_buy_cap'] or numbers['ask1'] > candidate['frozen_buy_cap']:
                    reason = 'price_outside_frozen_cap'
                elif numbers['last'] < numbers['open'] * 1.002:
                    reason = 'opening_strength_not_observed'
                elif (max(numbers['open'], numbers['last'], numbers['ask1']) > upper - .02 + 1e-10
                      or numbers['last'] <= lower or numbers['ask1'] < numbers['last']):
                    reason = 'near_limit_or_inconsistent_visible_ask'
        if reason:
            result['rejected'].append({'symbol': symbol, 'reason': reason})
        else:
            result['confirmed'].append({'symbol': symbol, 'name': candidate['name'],
                'reference_price': numbers['ask1'], 'exchange_timestamp': quote['exchange_timestamp'],
                'frozen_quantity_shares': candidate['frozen_quantity_shares'],
                'conditions_met_for_research': True, 'actual_fill_verified': False,
                'formal_trade_eligible': False})
    result['status'] = 'evidence_verified_research_only' if result['confirmed'] else 'no_confirmed_conditions'
    result['reason'] = '盘口仅证明观察条件；不证明成交，也不启用交易策略'
    return result


def generate(db, base, target, cutoff, sector_outlook=None, auction_snapshot=None, include_pool=False):
    plan = build_plan(db, base, target, cutoff, sector_outlook,include_pool=include_pool)
    if auction_snapshot is not None:
        plan['opening_confirmation'] = confirm_plan(plan, auction_snapshot, cutoff)
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--base', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--cutoff', required=True)
    parser.add_argument('--input-sha256')
    parser.add_argument('--sector-forecast')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    sector = json.loads(Path(args.sector_forecast).read_text(encoding='utf-8')) if args.sector_forecast else None
    plan = build_plan(args.db, args.base, args.target, args.cutoff, sector, args.input_sha256)
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps({'status': plan['status'], 'count': len(plan['watchlist']),
                      'formal_trade_eligible': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
