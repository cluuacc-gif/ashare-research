"""Frozen two-level sector/stock research screen, without orders or win-rate claims.

Real money flow is usable only through the vendor's own timestamped constituents.
An unrelated industry taxonomy is retained as a separate technical fallback and
never receives a money-flow reward by matching Chinese industry names.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics

from price_action_snapshot import price_action_snapshot

SCHEMA = 'sector-stock-confluence-v1'
TZ = timezone(timedelta(hours=8))
APP_ID = 0x41534851
MIN_MEMBERS = 5
MIN_MEMBER_COVERAGE = .65
MAX_PRIMARY = 3
MAX_SECTOR_POSITIONS = 2
MAX_WATCHLIST = 5


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _instant(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(TZ) if result.tzinfo is not None else None
    except ValueError:
        return None


def _known(value, cutoff):
    instant = _instant(value)
    return instant is not None and instant <= cutoff


def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _clip(value, lower=0.0, upper=1.0):
    return max(lower, min(upper, value))


def _mean(rows, key):
    return statistics.mean(row[key] for row in rows)


def _cent_floor(value):
    return float(decimal.Decimal(str(value)).quantize(decimal.Decimal('.01'),
                                                     rounding=decimal.ROUND_FLOOR))


@contextmanager
def _connection(db):
    if isinstance(db, sqlite3.Connection):
        yield db
    else:
        connection = sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True)
        try:
            yield connection
        finally:
            connection.close()


def _history(connection, symbols, base, cutoff, minimum=21):
    """Read bounded known-at-cutoff bars, preserving gaps rather than filling them."""
    histories = defaultdict(list)
    start = (date.fromisoformat(base) - timedelta(days=190)).isoformat()
    symbols = sorted(set(symbols))
    for offset in range(0, len(symbols), 400):
        batch = symbols[offset:offset + 400]
        placeholders = ','.join('?' for _ in batch)
        cursor = connection.execute(f'''SELECT symbol,trade_date,open,high,low,close,
            volume,amount,conflict,fetched_at,source_updated_at,finality
            FROM daily_quotes WHERE symbol IN ({placeholders})
            AND trade_date>=? AND trade_date<=? ORDER BY symbol,trade_date''',
                                    [*batch, start, base])
        names = [column[0] for column in cursor.description]
        for values in cursor:
            row = dict(zip(names, values))
            numbers = {key: _number(row[key]) for key in ('open', 'high', 'low', 'close', 'volume', 'amount')}
            if (row['conflict'] != 0 or not _known(row['fetched_at'], cutoff)
                    or row['finality'] not in ('historical_provider', 'post_close_provider')
                    or any(value is None or value <= 0 for value in numbers.values())
                    or not numbers['low'] <= min(numbers['open'], numbers['close'])
                    <= max(numbers['open'], numbers['close']) <= numbers['high']):
                continue
            if row['source_updated_at']:
                source = _instant(row['source_updated_at'])
                if (source is None or source.date().isoformat() != row['trade_date']
                        or not source <= _instant(row['fetched_at']) <= cutoff):
                    continue
            histories[row['symbol']].append(row)
    # All selected sectors share the same actually covered sessions. A security
    # missing one of those sessions is not substituted with an older bar.
    sessions = sorted({row['trade_date'] for rows in histories.values() for row in rows})
    expected = sessions[-minimum:]
    valid = {}
    if len(expected) < minimum or expected[-1] != base:
        return valid
    for symbol, rows in histories.items():
        available = {row['trade_date']: row for row in rows}
        if any(day not in available for day in expected):
            continue
        recent = [available[day] for day in expected]
        if any(abs(b['close'] / a['close'] - 1) > .30 for a, b in zip(recent, recent[1:])):
            continue
        valid[symbol] = rows
    return valid


def _member_features(rows):
    recent = rows[-21:]
    last = recent[-1]
    close, prior = last['close'], recent[-2]['close']
    span = last['high'] - last['low']
    amount_ratio = last['amount'] / statistics.mean(row['amount'] for row in recent[:-1])
    return {'return_1d': close / prior - 1,
            'return_5d': close / recent[-6]['close'] - 1,
            'breadth': float(close > prior),
            'above_ma5': float(close > statistics.mean(row['close'] for row in recent[-5:])),
            'participation': float(close > prior and amount_ratio >= 1),
            'close_location': (close - last['low']) / span if span > 0 else .5,
            'amount_ratio20': amount_ratio, 'base_close': close}


def _technical_score(components):
    # Absolute transforms keep the score comparable when only some money-flow
    # boards have verified constituents. It is an observation score, not P(up).
    value = (.30 * _clip(.5 + components['relative_return_5d'] / .10)
             + .25 * _clip(.5 + components['breadth_excess'])
             + .20 * components['participation']
             + .15 * components['close_location']
             + .10 * components['above_ma5']) * 100
    return round(max(0, value - min(20, max(0, components['return_1d'] - .03) * 500)), 2)


def _phase(components):
    relative = components.get('relative_return_5d', 0)
    breadth = components.get('breadth', 0)
    excess = components.get('breadth_excess', 0)
    above = components.get('above_ma5', 0)
    if relative > 0 and breadth >= .45 and excess >= 0 and above >= .45:
        return 'strengthening', '相对走强'
    if (relative <= 0 and breadth >= .50 and excess >= .08 and above >= .50
            and components.get('participation', 0) >= .30
            and components.get('close_location', 0) >= .60):
        return 'emerging', '早期转强观察'
    return 'insufficient_confluence', '板块共振不足'


def _flow_valid(payload, base, cutoff):
    if not isinstance(payload, dict):
        return False, 'fund_flow_unavailable'
    if payload.get('schema') != 'sector-fund-flow-v1' or payload.get('classification') != 'eastmoney_industry':
        return False, 'unsupported_fund_flow_schema_or_taxonomy'
    if payload.get('base_date', payload.get('base_trade_date', payload.get('trade_date'))) != base:
        return False, 'fund_flow_base_date_mismatch'
    if payload.get('status') not in ('ready', 'partial', 'available'):
        return False, 'fund_flow_unavailable'
    known_at = payload.get('information_cutoff', payload.get('observed_at', payload.get('generated_at')))
    if not _known(known_at, cutoff):
        return False, 'fund_flow_timestamp_unknown_or_future'
    return True, None


def _flow_row(row, base, cutoff):
    if (not isinstance(row, dict) or row.get('trade_date') != base
            or not _known(row.get('observed_at'), cutoff)):
        return None
    updated = row.get('source_updated_at')
    if updated and (not _known(updated, cutoff) or _instant(updated).date().isoformat() != base
                    or _instant(updated).timetz().replace(tzinfo=None) < time(15)
                    or _instant(updated) > _instant(row['observed_at'])):
        return None
    net, ratio = _number(row.get('main_net_inflow_yuan')), _number(row.get('main_net_ratio_pct'))
    if net is None or ratio is None or not -100 <= ratio <= 100:
        return None
    value3, value5, value10 = [_number(row.get(key)) for key in
        ('main_net_3d_yuan', 'main_net_5d_yuan', 'main_net_10d_yuan')]
    # Comparisons of window sums first normalize to per-session flow. A missing
    # historical window is unknown, not zero or evidence of acceleration.
    acceleration = value3 / 3 - value5 / 5 if value3 is not None and value5 is not None else None
    flow_score = 100 * _clip(.5 + ratio / 20)
    if acceleration is not None:
        scale = max(abs(value3 / 3), abs(value5 / 5), 1)
        flow_score = .8 * flow_score + 20 * _clip(.5 + acceleration / (2 * scale))
    return {'main_net_inflow_yuan': net, 'main_net_ratio_pct': ratio,
            'main_net_3d_yuan': value3, 'main_net_5d_yuan': value5,
            'main_net_10d_yuan': value10,
            'per_session_acceleration_3d_vs_5d_yuan': acceleration,
            'flow_score': round(flow_score, 2), 'flow_positive': net > 0,
            'flow_acceleration_verified': acceleration is not None and acceleration > 0,
            'trade_date': base, 'observed_at': row['observed_at'],
            'source_updated_at': updated, 'source_url': row.get('source_url')}


def _individual_flow(member, base, cutoff):
    """A membership response is not evidence that its undated quote is current."""
    if (member.get('flow_status') != 'verified_current_date'
            or member.get('flow_trade_date') != base
            or not _known(member.get('flow_observed_at'), cutoff)):
        return None
    updated = _instant(member.get('source_updated_at'))
    value = _number(member.get('main_net_inflow_yuan'))
    if (updated is None or updated > cutoff or updated.date().isoformat() != base
            or updated.timetz().replace(tzinfo=None) < time(15) or value is None):
        return None
    if updated > _instant(member['flow_observed_at']):
        return None
    return {'main_net_inflow_yuan': value, 'trade_date': base,
            'observed_at': member['flow_observed_at'],
            'source_updated_at': member['source_updated_at'],
            'status': 'positive' if value > 0 else 'negative' if value < 0 else 'neutral',
            'source_url': member.get('source_url'),
            'date_evidence_source_url': member.get('flow_date_source_url'),
            'is_vendor_estimate_not_account_cash_flow': True}


def _flow_boards(connection, payload, base, cutoff, benchmark):
    valid, reason = _flow_valid(payload, base, cutoff)
    if not valid:
        return [], [], reason
    boards, actual_flow = [], []
    for row in payload.get('sectors', []):
        flow = _flow_row(row, base, cutoff)
        if flow is None:
            continue
        code, name = row.get('code'), row.get('name')
        if not isinstance(code, str) or not isinstance(name, str):
            continue
        actual_flow.append({'sector': name, 'sector_code': code,
                            'classification': 'eastmoney_industry', **flow})
        if row.get('membership_status') != 'complete':
            continue
        members = {}
        for member in row.get('members', []):
            if not isinstance(member, dict) or not isinstance(member.get('symbol'), str):
                continue
            try:
                effective = date.fromisoformat(member.get('effective_date'))
            except (TypeError, ValueError):
                continue
            if effective <= cutoff.date() and _known(member.get('observed_at'), cutoff):
                members[member['symbol']] = member
        reported = _number(row.get('vendor_member_total'))
        excluded = _number(row.get('excluded_non_ashare_count', 0))
        # complete is checked against the vendor's total rather than trusting the
        # count label. A truncated board cannot create fake breadth strength.
        if (not members or excluded is None or excluded < 0 or excluded != int(excluded)
                or (reported is not None and reported != len(members) + excluded)):
            continue
        boards.append({'sector': name, 'sector_code': code,
                       'classification': 'eastmoney_industry',
                       'members': members, 'flow': flow,
                       'member_flow_by_symbol': {symbol: _individual_flow(member, base, cutoff)
                                                 for symbol, member in members.items()},
                       'vendor_member_total': reported,
                       'excluded_non_ashare_count': excluded,
                       'flow_scope': 'vendor_all_share_classes',
                       'stock_selection_scope': 'A_share_only'})
    histories = _history(connection, [symbol for board in boards for symbol in board['members']], base, cutoff)
    features = {symbol: _member_features(rows) for symbol, rows in histories.items()}
    local_market = list(features.values())
    market5 = _number(benchmark.get('base_return_5d'))
    market_breadth = _number(benchmark.get('base_breadth'))
    if market5 is None:
        market5 = _mean(local_market, 'return_5d') if local_market else 0
    if market_breadth is None:
        market_breadth = _mean(local_market, 'breadth') if local_market else 0
    results = []
    for board in boards:
        kept = [symbol for symbol in board['members'] if symbol in features]
        coverage = len(kept) / len(board['members'])
        if len(kept) < MIN_MEMBERS or coverage < MIN_MEMBER_COVERAGE:
            continue
        rows = [features[symbol] for symbol in kept]
        components = {key: _mean(rows, key) for key in
            ('return_1d', 'return_5d', 'breadth', 'above_ma5', 'participation',
             'close_location', 'amount_ratio20')}
        components.update(relative_return_5d=components['return_5d'] - market5,
                          breadth_excess=components['breadth'] - market_breadth)
        technical = _technical_score(components)
        phase, label = _phase(components)
        flow = board['flow']
        score = round(.7 * technical + .3 * flow['flow_score'], 2)
        eligible = (phase != 'insufficient_confluence' and technical >= 55
                    and flow['flow_positive'] and flow['main_net_ratio_pct'] > 0
                    and components['return_1d'] <= .06)
        results.append({**board, 'members': kept, 'technical_score': technical,
                        'score': score, 'score_is_probability': False,
                        'flow_integrated': True, 'components': components,
                        'phase': phase, 'phase_label': label,
                        'eligible_for_stock_selection': eligible,
                        'member_count': len(kept), 'member_history_coverage': coverage,
                        'membership_snapshot_sha256': _digest(board['members']),
                        'benchmark_scope': ('frozen_sector_outlook_covered_market'
                            if benchmark else 'verified_money_flow_board_member_union')})
    actual_flow.sort(key=lambda row: (-row['main_net_inflow_yuan'], row['sector_code']))
    return results, actual_flow, None if results else 'verified_same_taxonomy_member_history_unavailable'


def _technical_boards(outlook, base, target, cutoff):
    if (not isinstance(outlook, dict) or outlook.get('status') != 'ready'
            or outlook.get('base_date') != base or outlook.get('target_date') != target
            or not _known(outlook.get('information_cutoff'), cutoff)):
        return [], 'sector_outlook_missing_or_date_cutoff_mismatch'
    frozen = outlook.get('frozen_members')
    if not isinstance(frozen, dict) or _digest(frozen) != outlook.get('constituents_sha256'):
        return [], 'sector_constituents_hash_unverified'
    boards = []
    for row in outlook.get('rankings', []):
        sector, score = row.get('sector'), _number(row.get('score'))
        if not isinstance(sector, str) or score is None or not 0 <= score <= 100:
            continue
        components = row.get('components', {})
        required = ('return_1d', 'relative_return_5d', 'breadth', 'breadth_excess',
                    'above_ma5', 'participation', 'close_location')
        if any(_number(components.get(key)) is None for key in required):
            continue
        members = []
        for member in frozen.get(sector, []):
            try:
                effective = date.fromisoformat(member.get('membership_effective_date'))
            except (AttributeError, TypeError, ValueError):
                continue
            if (isinstance(member, dict) and isinstance(member.get('symbol'), str)
                    and effective <= cutoff.date()
                    and _known(member.get('membership_observed_at'), cutoff)):
                members.append(member['symbol'])
        if len(members) < MIN_MEMBERS:
            continue
        phase, label = _phase(components)
        boards.append({'sector': sector, 'sector_code': None,
                       'classification': outlook['classification'],
                       'members': sorted(set(members)), 'score': score,
                       'technical_score': score, 'components': components,
                       'flow': None, 'flow_integrated': False,
                       'score_is_probability': False,
                       'phase': phase, 'phase_label': label,
                       'eligible_for_stock_selection': score >= 60
                           and phase != 'insufficient_confluence'
                           and components['return_1d'] <= .06,
                       'member_count': len(set(members)),
                       'member_history_coverage': row.get('coverage'),
                       'membership_snapshot_sha256': _digest(frozen.get(sector, [])),
                       'benchmark_scope': 'frozen_sector_outlook_covered_market'})
    return boards, None if boards else 'no_verified_technical_sector_board'


def _stock_action(rows, features, benchmark):
    """Use the same standard-library mathematical engine on every report host."""
    market5 = _number(benchmark.get('base_return_5d'))
    market_breadth = _number(benchmark.get('base_breadth'))
    market1 = _number(benchmark.get('base_return_1d'))
    snapshot = price_action_snapshot(rows, market_return5=market5 or 0,
        market_breadth=market_breadth, market_mean_return=market1)
    return snapshot if snapshot.get('available') is True else None


def generate(db, base: str, target: str, cutoff: str, sector_outlook: dict,
             opening_plan: dict, fund_flow: dict | None = None) -> dict:
    """Select stocks within verified strengthening sectors, then freeze budgets.

    Both a confirmed-flow board and a technical-only board are allowed as
    research watches. Their evidence tiers are explicit; they never share a
    taxonomy merely because their display names happen to be equal.
    """
    known_at = _instant(cutoff)
    if known_at is None:
        raise ValueError('explicit timezone-aware cutoff required')
    base_day, target_day = date.fromisoformat(base), date.fromisoformat(target)
    if (target_day <= base_day
            or known_at < datetime.combine(base_day, time(15), TZ)
            or known_at >= datetime.combine(target_day, time(9, 30), TZ)):
        raise ValueError('freeze after base close and before target continuous opening')
    if known_at > datetime.now(TZ) + timedelta(seconds=5):
        raise ValueError('information cutoff is in the future')
    result = {'schema': SCHEMA, 'base_date': base, 'target_date': target,
              'information_cutoff': known_at.isoformat(),
              'status': 'no_sector_confluence', 'watchlist': [], 'primary': [], 'reserves': [],
              'sector_board': [], 'actual_fund_flow': [],
              'flow_status': 'unavailable', 'flow_integrated': False,
              'formal_trade_eligible': False, 'actual_fill_verified': False,
              'win_rate_80_verified': False, 'gross_trade_win_rate': None,
              'score_is_probability': False, 'costs_included': False,
              'holding_sessions': {'entry': 'D1', 'exit': 'D2',
                  'strength_tracking': ['D1', 'D2', 'D3'],
                  'holding_extended_by_three_day_prediction': False},
              'definition': '先验证板块相对强度与资金证据，再在相同成分中挑选未过度伸展的价格行为；分数是研究排序。',
              'coverage': {}, 'rejected_candidates': [], 'limitations': [
                  '主力净流入是供应商按成交方向和订单规模估算的净额，并非交易所公布的真实资金账户流向。',
                  '早期转强是已发生的结构变化观察，尚未验证未来2—3日连续上涨或隔日盈利概率。',
                  '研究观察须由D1新鲜盘口、官方状态和涨跌停继续确认；可见卖盘不证明实际成交。',
                  'D1买入后不能当日卖出，风险参考只在D2执行；隔夜跳空可能越过参考止损。',
                  '最多3个独立预算名额、同板块最多2个；第4—5名只观察，不自动补位。']}
    if (not isinstance(opening_plan, dict) or opening_plan.get('base_date') != base
            or opening_plan.get('target_date') != target
            or not _known(opening_plan.get('information_cutoff'), known_at)):
        result['reason'] = 'opening_plan_missing_or_date_cutoff_mismatch'
        return result
    price_range = opening_plan.get('protocol', {}).get('entry_reference_price_range', [5, 10])
    if not isinstance(price_range, (list, tuple)) or len(price_range) != 2:
        result['reason'] = 'invalid_frozen_price_range'; return result
    price_min, price_max = [_number(value) for value in price_range]
    if price_min is None or price_max is None or not 0 < price_min <= price_max:
        result['reason'] = 'invalid_frozen_price_range'; return result
    result['entry_reference_price_range'] = [price_min, price_max]
    technical, technical_reason = _technical_boards(sector_outlook, base, target, known_at)
    # Invalid/future sector forecasts cannot donate a market regime through a
    # side channel after their constituent join has been rejected.
    benchmark = sector_outlook.get('benchmark', {}) if technical else {}
    candidates = opening_plan.get('candidate_pool', opening_plan.get('watchlist', []))
    if not isinstance(candidates, list):
        candidates = []
    with _connection(db) as connection:
        if connection.execute('PRAGMA application_id').fetchone()[0] != APP_ID:
            raise ValueError('original market database required')
        money_boards, actual_flow, flow_reason = _flow_boards(connection, fund_flow, base, known_at, benchmark)
        boards = sorted(money_boards + technical,
                        key=lambda row: (-row['score'], -int(row['flow_integrated']), row['sector']))
        eligible_boards = [board for board in boards if board['eligible_for_stock_selection']]
        if actual_flow:
            result['actual_fund_flow'] = actual_flow
            result['flow_status'] = 'verified_vendor_flow_with_members' if money_boards else 'verified_vendor_flow_separate_taxonomy'
        result['flow_reason'] = flow_reason
        result['flow_integrated'] = bool(money_boards)
        result['technical_sector_reason'] = technical_reason
        board_members = defaultdict(list)
        for board in eligible_boards:
            for symbol in board['members']:
                board_members[symbol].append(board)
        # We intentionally do not reward unrelated stand-alone technical names.
        aligned = [row for row in candidates if isinstance(row, dict)
                   and row.get('symbol') in board_members]
        histories = _history(connection, [row['symbol'] for row in aligned], base, known_at, minimum=61)
        ranked, rejected = [], defaultdict(int)
        def reject(candidate, reason, action=None):
            rejected[reason] += 1
            if len(result['rejected_candidates']) < 20:
                board = max(board_members[candidate['symbol']], key=lambda item: (item['flow_integrated'], item['score']))
                result['rejected_candidates'].append({'symbol': candidate['symbol'],
                    'name': candidate.get('name'), 'sector': board['sector'],
                    'sector_classification': board['classification'], 'reason': reason,
                    'price_action_score': action.get('price_action_score') if action else None,
                    'eligible_patterns': action.get('eligible_patterns', []) if action else []})
        for candidate in aligned:
            symbol = candidate['symbol']
            close = _number(candidate.get('close'))
            features = candidate.get('features', {})
            required = ('close_location', 'ma20_gap', 'ret5', 'amount_ratio20', 'median_amount20')
            if (candidate.get('feature_last_session') != base or close is None or not price_min <= close <= price_max
                    or any(_number(features.get(key)) is None for key in required)
                    or symbol not in histories):
                reject(candidate, 'candidate_history_or_features_unverified')
                continue
            if abs(histories[symbol][-1]['close'] - close) > .000001:
                reject(candidate, 'candidate_base_close_mismatch')
                continue
            action = _stock_action(histories[symbol], features, benchmark)
            if action is None:
                reject(candidate, 'price_action_features_unavailable')
                continue
            action_score = _number(action.get('price_action_score'))
            if action_score is None or action_score < 55:
                reject(candidate, 'price_action_quality_below_threshold', action)
                continue
            if action.get('available') is True and not action.get('eligible_patterns'):
                reject(candidate, 'no_predefined_price_action_structure', action)
                continue
            # A verified direct vendor-flow constituent outranks the same stock's
            # unrelated technical taxonomy, without transferring flow to it.
            board = max(board_members[symbol], key=lambda item: (item['flow_integrated'], item['score'], item['sector']))
            stock_flow = board.get('member_flow_by_symbol', {}).get(symbol)
            primary_eligible = (not board['flow_integrated']
                                or stock_flow is not None and stock_flow['status'] == 'positive')
            from opening_strength import frozen_cap
            cap = frozen_cap(close, price_max)
            if cap < price_min or cap < close * 1.002 - 1e-10:
                reject(candidate, 'frozen_cap_cannot_allow_required_opening_strength', action)
                continue
            supplied_cap = _number(candidate.get('frozen_buy_cap'))
            # Legacy narrow fixtures did not floor their input cap. Published
            # plans declare the numeric range and must preserve the exact cap.
            if 'protocol' in opening_plan and (supplied_cap is None or abs(supplied_cap - cap) > 1e-8):
                reject(candidate, 'candidate_frozen_cap_differs_from_declared_protocol', action)
                continue
            if int(10000 / cap / 100) * 100 < 100:
                reject(candidate, 'one_lot_exceeds_frozen_position_budget', action)
                continue
            liquidity = 100 * _clip(math.log10(max(1, features['median_amount20'] / 20_000_000)) / 1.5)
            score = round(.50 * board['score'] + .40 * action_score + .10 * liquidity, 2)
            row = deepcopy(candidate)
            row.update({'sector': board['sector'], 'sector_code': board['sector_code'],
                'sector_classification': board['classification'], 'sector_score': board['score'],
                'sector_phase': board['phase'], 'sector_phase_label': board['phase_label'],
                'sector_membership_evidence': 'same_vendor_frozen_members' if board['flow_integrated'] else 'frozen_technical_sector_members',
                'flow_integrated': board['flow_integrated'], 'sector_fund_flow': deepcopy(board['flow']),
                'individual_fund_flow': deepcopy(stock_flow),
                'primary_flow_condition_met': primary_eligible,
                'primary_flow_rejection_reason': (None if primary_eligible else
                    'individual_current_dated_flow_unknown' if stock_flow is None else
                    'individual_current_dated_flow_not_positive'),
                'research_score': score, 'price_action_score': action_score,
                'price_action': action, 'score_is_probability': False,
                'evidence_tier': 'flow_and_technical_sector_confluence' if board['flow_integrated'] else 'technical_sector_confluence_only',
                'frozen_buy_cap': cap, 'frozen_position_budget_cny': 10000,
                'frozen_quantity_shares': int(10000 / cap / 100) * 100,
                'risk_exit_reference': {'available_session': 'D2', 'stop_fraction': .02,
                    'stop_is_guaranteed': False, 'stop_reference': 'actual_D1_fill*(1-stop_fraction)',
                    'profit_reference_fractions': [.03, .06], 'profit_reference': 'actual_D1_fill*(1+target_fraction)',
                    'default_profit_target_fraction': .03, 'stretch_target_fraction': .06,
                    'stretch_target_requires_separate_predeclared_policy': True,
                    'd1_exit_allowed': False, 'd2_end_of_day_exit_if_no_prior_exit': True},
                'rationale': [f'{board["phase_label"]}板块中的可匹配成分，板块观察分{board["score"]:.1f}',
                    ('该供应商行业主力净流入和其同源成分已核验' if board['flow_integrated'] else '仅技术板块证据；未把其他行业分类的资金流套入本股'),
                    (f'本股同日主力估算净额{stock_flow["main_net_inflow_yuan"] / 1e4:+.0f}万元'
                        if stock_flow is not None else '本股当日资金净额未知；未用零值代替'),
                    f'价格行为观察分{action_score:.1f}；分数不是上涨概率',
                    '以D1买入、D2退出为执行周期；D3只核对后续强度，不据此延长持仓'],
                'formal_trade_eligible': False, 'actual_fill_verified': False})
            ranked.append(row)
    # A very weak covered market permits observation, but no primary research
    # budget. This is fixed before seeing target-session outcomes.
    market_breadth = _number(benchmark.get('base_breadth'))
    market1 = _number(benchmark.get('base_return_1d'))
    risk_off = market_breadth is not None and market1 is not None and market_breadth < .25 and market1 < -.015
    ranked.sort(key=lambda row: (-int(row['flow_integrated']),
                                -int(row['primary_flow_condition_met']), -row['research_score'], row['symbol']))
    selected, reserves, sector_counts, symbols = [], [], defaultdict(int), set()
    for row in ranked:
        if row['symbol'] in symbols:
            continue
        symbols.add(row['symbol'])
        key = row['sector_classification'], row['sector']
        if (not risk_off and row['primary_flow_condition_met'] and len(selected) < MAX_PRIMARY
                and sector_counts[key] < MAX_SECTOR_POSITIONS):
            selected.append(row)
            sector_counts[key] += 1
        elif len(reserves) < MAX_WATCHLIST - MAX_PRIMARY:
            reserves.append(row)
    for index, row in enumerate(selected + reserves, 1):
        primary = index <= len(selected)
        row.update(rank=index, budget_slot=index if primary else None,
                   is_reserve_only=not primary)
        if not primary:
            row['frozen_quantity_shares'] = 0
            row['frozen_position_budget_cny'] = 0
        row['frozen_reserved_cash_cny'] = round(row['frozen_quantity_shares'] * row['frozen_buy_cap'], 2)
    # Frozen members are retained for compact audit via their hash/count, while
    # the authoritative source forecast/flow payload keeps the full snapshot.
    result['sector_board'] = [{key: deepcopy(value) for key, value in board.items()
                              if key not in ('members', 'member_flow_by_symbol')}
                              for board in boards]
    result.update(status='conditional_research_only' if selected else 'no_sector_confluence',
                  primary=selected, reserves=reserves, watchlist=selected + reserves,
                  market_risk_off=risk_off)
    result['coverage'] = {'opening_candidate_pool': len(candidates),
        'verified_technical_sectors': len(technical), 'verified_flow_sectors': len(money_boards),
        'eligible_confluence_sectors': len(eligible_boards),
        'within_sector_candidates': len(aligned), 'ranked_sector_stocks': len(ranked),
        'verified_positive_individual_flow_candidates': sum(row.get('individual_fund_flow') is not None
            and row['individual_fund_flow']['status'] == 'positive' for row in ranked),
        'flow_candidates_without_positive_individual_flow': sum(row['flow_integrated']
            and not row['primary_flow_condition_met'] for row in ranked),
        'primary_count': len(selected), 'reserve_count': len(reserves),
        'rejections': dict(rejected)}
    result['ranking_protocol'] = {'sector_stock_weights': {'sector': .50, 'price_action': .40, 'liquidity': .10},
        'entry_reference_price_range': [price_min, price_max],
        'flow_sector_weights': {'technical': .70, 'verified_money_flow': .30},
        'minimum_price_action_score': 55, 'predefined_price_action_structure_required_when_engine_available': True,
        'maximum_same_sector_primary_positions': MAX_SECTOR_POSITIONS,
        'maximum_primary_positions': MAX_PRIMARY, 'initial_capital_cny': 30000,
        'budget_per_position_cny': 10000, 'same_session_budget_reallocation': False,
        'market_risk_off': 'covered_market_breadth<25% and mean_daily_return<-1.5%',
        'unknown_taxonomy_name_join_allowed': False,
        'flow_board_primary_requires_dated_positive_individual_flow': True,
        'unknown_or_nonpositive_individual_flow_is_reserve_only': True,
        'flow_score_is_calibrated_probability': False,
        'three_day_strength_objective': 'forward_track_only_not_an_entry_feature'}
    result['selection_id'] = _digest({key: result[key] for key in
        ('schema', 'base_date', 'target_date', 'information_cutoff', 'watchlist', 'sector_board')})
    return result
