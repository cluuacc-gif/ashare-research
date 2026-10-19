"""Freeze one prospective T+1 research cohort before looking at D1/D2 outcomes.

The cohort is an observation universe, not a portfolio.  Confluence is a mask
on that same universe, never another selection or an estimated probability.
Only the standard library and existing standard-library research helpers are
used; this module performs no writes, network requests, or order submission.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import math

import sector_stock_selection as sector
import trade_calendar

SCHEMA = 'nextday-frozen-cohort-v1'
TZ = timezone(timedelta(hours=8))
MAX_COHORT = 12
PROTOCOL = {
    'schema': 'nextday-matched-cohort-protocol-v1',
    'hypothesis_status': 'fixed_prospective_hypotheses_not_validated',
    'features_end': 'base_session_close_with_observation_timestamps_at_or_before_freeze',
    'freeze_deadline_exclusive': 'D1 09:15:00 Asia/Shanghai',
    'universe': 'top12_valid_opening_pool_by_research_score_desc_then_symbol',
    'observation_universe_not_portfolio': True,
    'confluence_is_mask_of_same_base_cohort': True,
    'substitution_or_reranking_after_confluence': False,
    'price_range_cny': [5, 100],
    'frozen_entry_floor': 'max(5,ceil_to_cent(base_close*0.99))',
    'frozen_entry_cap': 'floor_to_cent(min(base_close*1.025,100))',
    'observation_position_budget_cny': 10000,
    'quantity': 'floor(10000/frozen_entry_cap/100)*100',
    'market_gate': {'base_breadth_gte': .5, 'base_return_1d_gte': 0,
                    'base_return_5d_gte': 0, 'unknown_fails': True},
    'sector_gate': {'phase': 'strengthening', 'relative_return_5d_gt': 0,
                    'breadth_gte': .55, 'minimum_members': 5,
                    'minimum_member_history_coverage': .65,
                    'same_vendor_known_flow_requires_positive_net_and_ratio': True,
                    'technical_fallback_flow': 'unknown_without_flow_credit',
                    'cross_taxonomy_name_join': False,
                    'legacy_technical_or_price_action_score_gate': False},
    'leadership_gate': {'stock_minus_board_return_5d_gt': 0,
                        'descending_rank_lte': 'ceil(0.30*valid_board_member_count)',
                        'rank_universe': 'all_valid_frozen_board_members_not_candidate_pool',
                        'ties': 'return_5d_desc_then_symbol',
                        'leadership_percentile': 'descending_rank/valid_member_count; smaller_is_stronger'},
    'price_structure': 'optional_descriptive_observation_not_a_gate',
    'arms': [
        {'arm_id': 'base_0940', 'mask': 'base', 'decision_time': '09:40'},
        {'arm_id': 'base_1440', 'mask': 'base', 'decision_time': '14:40'},
        {'arm_id': 'confluence_0940', 'mask': 'confluence', 'decision_time': '09:40'},
        {'arm_id': 'confluence_1440', 'mask': 'confluence', 'decision_time': '14:40'},
        {'arm_id': 'confluence_opening_retest', 'mask': 'confluence', 'decision_time': '09:40',
         'trigger': {'first_range_completed_through': '09:35',
                     'prior_breakout_completed_bars': ['09:36', '09:39'],
                     'prior_breakout': 'close>=first5_high+0.01',
                     'retest': '09:40 bullish bar; abs(low-first5_high)<=0.002*base_close; close>=first5_high; close_location>=0.6',
                     'invalidation': 'no intervening completed close<first5_high-0.002*base_close'}},
        {'arm_id': 'confluence_late_confirmation', 'mask': 'confluence', 'decision_time': '14:40',
         'trigger': {'coverage': 'complete continuous-session prefix, preserving scheduled lunch gap',
                     'bar': 'latest bullish; close>=session_open; close_location>=0.6',
                     'mean': 'close>=volume_weighted_mean_completed_bar_close; proxy_not_exact_VWAP',
                     'lows': 'min(last5_lows)>=min(previous5_lows)'}},
    ],
    'entry_session': 'D1', 'entry_reference': 'open_of_one_minute_bar_STARTING_at_decision_time_after_completed_prefix_ENDING_at_decision',
    'exit_session': 'D2', 'exit_decision_time': '14:50',
    'exit_reference': 'D2_open_of_one_minute_bar_STARTING_at_14:50',
    'same_day_exit': False, 'intraday_profit_target_exits': False,
    'fees_cny': 0, 'primary_outcome': 'D2_exit_reference_price>D1_entry_reference_price',
    'secondary_outcomes': ['actual_exit_return>=0.03', 'actual_exit_return>=0.06'],
    'missing_execution_evidence': 'unresolved_or_unfillable_never_invent_a_fill',
    'reference_fills_are_actual_fills': False,
    'outcome_threshold_search': False, 'claims_80_percent_win_rate': False,
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _now():
    return datetime.now(TZ)


def _bounds(close):
    value = Decimal(str(close))
    floor = max(Decimal('5'), (value * Decimal('.99')).quantize(Decimal('.01'), rounding=ROUND_CEILING))
    cap = min(value * Decimal('1.025'), Decimal('100')).quantize(Decimal('.01'), rounding=ROUND_FLOOR)
    quantity = int((Decimal('10000') / cap / 100).to_integral_value(rounding=ROUND_FLOOR)) * 100
    return float(floor), float(cap), quantity


def _pick(row, keys):
    return {key: row[key] for key in keys if key in row}


MEMBER_FIELDS = ('symbol', 'base_close', 'membership_effective_date', 'membership_observed_at')
FLOW_MEMBER_FIELDS = ('symbol', 'effective_date', 'observed_at', 'source_url',
    'flow_status', 'flow_trade_date', 'flow_observed_at', 'source_updated_at',
    'main_net_inflow_yuan', 'flow_date_source_url')
FLOW_ROW_FIELDS = ('code', 'name', 'trade_date', 'observed_at', 'source_updated_at',
    'source_url', 'main_net_inflow_yuan', 'main_net_ratio_pct', 'main_net_3d_yuan',
    'main_net_5d_yuan', 'main_net_10d_yuan', 'membership_status', 'vendor_member_total',
    'excluded_non_ashare_count', 'membership_scope', 'vendor_membership_scope')
COMPONENT_FIELDS = ('return_1d', 'return_5d', 'relative_return_5d', 'breadth',
    'breadth_excess', 'above_ma5', 'participation', 'close_location', 'amount_ratio20')
BAR_FIELDS = ('symbol', 'trade_date', 'open', 'high', 'low', 'close', 'volume',
              'amount', 'conflict', 'fetched_at', 'source_updated_at', 'finality')


def _outlook(payload, base, target, cutoff):
    """Verify the existing membership seal, then drop unrelated/outcome fields."""
    if (not isinstance(payload, dict) or payload.get('schema') != 'sector-outlook-v1'
            or payload.get('status') != 'ready' or payload.get('base_date') != base
            or payload.get('target_date') != target
            or not sector._known(payload.get('information_cutoff'), cutoff)
            or not isinstance(payload.get('classification'), str)
            or not payload['classification']):
        return None, 'sector_outlook_missing_or_date_cutoff_taxonomy_mismatch'
    raw = payload.get('frozen_members')
    if not isinstance(raw, dict):
        return None, 'sector_constituents_hash_unverified'
    frozen = {name: [_pick(member, MEMBER_FIELDS) for member in members if isinstance(member, dict)]
              for name, members in raw.items() if isinstance(name, str) and isinstance(members, list)}
    # Unknown future labels cannot poison or alter a seal of the known fields.
    if payload.get('constituents_sha256') not in (_digest(raw), _digest(frozen)):
        return None, 'sector_constituents_hash_unverified'
    result = _pick(payload, ('schema', 'status', 'base_date', 'target_date',
                            'information_cutoff', 'classification'))
    result.update(frozen_members=frozen, constituents_sha256=_digest(frozen), rankings=[])
    for row in payload.get('rankings', []):
        if isinstance(row, dict):
            kept = _pick(row, ('sector', 'score', 'coverage', 'member_count'))
            components = row.get('components')
            kept['components'] = _pick(components, COMPONENT_FIELDS) if isinstance(components, dict) else {}
            result['rankings'].append(kept)
    benchmark = payload.get('benchmark')
    benchmark = benchmark if isinstance(benchmark, dict) else {}
    result['benchmark'] = {key: sector._number(benchmark.get(key)) for key in
                          ('base_breadth', 'base_return_1d', 'base_return_5d')}
    return result, None


def _flow(payload):
    if not isinstance(payload, dict):
        return None
    result = _pick(payload, ('schema', 'classification', 'base_date', 'base_trade_date',
        'trade_date', 'status', 'information_cutoff', 'observed_at', 'generated_at'))
    result['sectors'] = []
    for row in payload.get('sectors', []):
        if isinstance(row, dict):
            kept = _pick(row, FLOW_ROW_FIELDS)
            kept['members'] = [_pick(member, FLOW_MEMBER_FIELDS)
                               for member in row.get('members', []) if isinstance(member, dict)]
            result['sectors'].append(kept)
    return result


def _market_gate(outlook):
    benchmark = outlook.get('benchmark', {}) if outlook else {}
    values = {key: sector._number(benchmark.get(key)) for key in
              ('base_breadth', 'base_return_1d', 'base_return_5d')}
    reasons = [key + '_unknown' for key, value in values.items() if value is None]
    if values['base_breadth'] is not None and not .5 <= values['base_breadth'] <= 1:
        reasons.append('base_breadth_below_0.5_or_invalid')
    for key in ('base_return_1d', 'base_return_5d'):
        if values[key] is not None and values[key] < 0:
            reasons.append(key + '_negative')
    return {**values, 'known': all(value is not None for value in values.values()),
            'eligible': not reasons, 'reasons': reasons,
            'benchmark_scope': 'verified_frozen_sector_outlook_covered_market'}


def _board_snapshot(board, histories, benchmark, frozen):
    symbols = sorted(set(board['members']))
    members = frozen.get(board['sector'], []) if not board['flow_integrated'] else None
    declared = {row.get('symbol'): row for row in members} if members is not None else {}
    valid = {}
    for symbol in symbols:
        if symbol not in histories:
            continue
        feature = sector._member_features(histories[symbol])
        # A technical membership seal includes its base quote; do not silently
        # replace it with a revised historical quote after the freeze.
        if members is not None:
            supplied = sector._number(declared.get(symbol, {}).get('base_close'))
            if supplied is None or abs(supplied - feature['base_close']) > 1e-6:
                continue
        valid[symbol] = feature
    frozen_count = len(declared) if members is not None else len(symbols)
    coverage = len(valid) / frozen_count if frozen_count else 0
    declared_coverage = sector._number(board.get('member_history_coverage'))
    coverage = min(coverage, declared_coverage) if declared_coverage is not None else 0
    components = {key: sum(row[key] for row in valid.values()) / len(valid) for key in
                  ('return_1d', 'return_5d', 'breadth', 'above_ma5', 'participation',
                   'close_location', 'amount_ratio20')} if valid else {}
    market5 = sector._number(benchmark.get('base_return_5d'))
    market_breadth = sector._number(benchmark.get('base_breadth'))
    components['relative_return_5d'] = (components['return_5d'] - market5
        if valid and market5 is not None else None)
    components['breadth_excess'] = (components['breadth'] - market_breadth
        if valid and market_breadth is not None else None)
    phase = (sector._phase(components)[0]
             if all(components.get(key) is not None for key in ('relative_return_5d', 'breadth_excess'))
             else 'unknown')
    flow = _pick(board.get('flow') or {}, ('main_net_inflow_yuan', 'main_net_ratio_pct',
        'trade_date', 'observed_at', 'source_updated_at', 'source_url')) or None
    reasons = []
    if len(valid) < 5: reasons.append('sector_valid_members_below_5')
    if coverage < .65: reasons.append('sector_history_coverage_below_0.65_or_unknown')
    if phase != 'strengthening': reasons.append('sector_phase_not_strengthening')
    if components.get('relative_return_5d') is None or components['relative_return_5d'] <= 0:
        reasons.append('sector_relative_return_5d_not_positive_or_unknown')
    if components.get('breadth', 0) < .55: reasons.append('sector_breadth_below_0.55')
    if flow is not None and (flow['main_net_inflow_yuan'] <= 0 or flow['main_net_ratio_pct'] <= 0):
        reasons.append('same_vendor_actual_flow_not_positive')
    ranked = sorted(valid, key=lambda symbol: (-valid[symbol]['return_5d'], symbol))
    identities = {'classification': board['classification'], 'sector_code': board.get('sector_code'),
                  'sector': board['sector']}
    snapshot = {'sector_identity': identities, 'member_count': len(valid),
        'frozen_member_count': frozen_count, 'member_history_coverage': coverage,
        'membership_snapshot_sha256': board['membership_snapshot_sha256'],
        'history_snapshot_sha256': _digest({symbol: [_pick(row, BAR_FIELDS) for row in histories[symbol][-21:]]
                                          for symbol in sorted(valid)}),
        'components': components, 'phase': phase, 'sector_gate_eligible': not reasons,
        'reasons': reasons, 'flow_known': flow is not None, 'sector_fund_flow': flow,
        'flow_credit': flow is not None and flow['main_net_inflow_yuan'] > 0 and flow['main_net_ratio_pct'] > 0,
        'evidence_tier': 'same_vendor_flow_and_technical' if flow is not None else 'technical_only_flow_unknown',
        'leadership_returns_5d': [{'symbol': symbol, 'return_5d': valid[symbol]['return_5d']}
                                  for symbol in ranked],
        'leadership_top_count': math.ceil(.30 * len(ranked))}
    snapshot['board_id'] = _digest(snapshot)
    return snapshot, valid, {symbol: index for index, symbol in enumerate(ranked, 1)}


def build(db, base, target, cutoff, sector_outlook, opening_plan, fund_flow=None, *, input_sha256=None):
    """Return a sealed, prospective same-cohort research design and T-only rows."""
    known_at = sector._instant(cutoff)
    if known_at is None:
        raise ValueError('explicit timezone-aware cutoff required')
    base_day, target_day = date.fromisoformat(base), date.fromisoformat(target)
    if target_day <= base_day or known_at < datetime.combine(base_day, time(15), TZ):
        raise ValueError('target must follow base and cutoff must follow base close')
    if known_at > _now() + timedelta(seconds=5):
        raise ValueError('information cutoff cannot be in the future')
    calendar_reason, d2, adjacent = None, None, False
    try:
        adjacent = trade_calendar.is_session(base) and trade_calendar.adjacent(base, 1) == target
        d2 = trade_calendar.adjacent(target, 1) if trade_calendar.is_session(target) else None
    except ValueError:
        calendar_reason = 'exchange_calendar_unverified'
    forward_reasons = []
    if not adjacent: forward_reasons.append(calendar_reason or 'target_not_adjacent_verified_session')
    if known_at >= datetime.combine(target_day, time(9, 15), TZ):
        forward_reasons.append('freeze_not_before_target_09_15')
    protocol = deepcopy(PROTOCOL)
    protocol_hash = _digest(protocol)
    outlook, sector_reason = _outlook(sector_outlook, base, target, known_at)
    market = _market_gate(outlook)
    result = {'schema': SCHEMA, 'base_trade_date': base, 'target_trade_date': target,
        'D2_trade_date': d2, 'generated_at': known_at.isoformat(),
        'information_cutoff': known_at.isoformat(), 'input_database_sha256': input_sha256,
        'protocol': protocol, 'protocol_id': protocol_hash, 'protocol_sha256': protocol_hash,
        'forward_eligible': not forward_reasons, 'forward_ineligible_reasons': forward_reasons,
        'status': 'no_base_candidates', 'reason': None, 'base_candidates': [], 'sector_boards': [],
        'market_gate': market, 'sector_outlook_reason': sector_reason,
        'formal_trade_ready': False, 'actual_win_rate': None,
        'execution_win_rate_80_validated': False, 'reference_fills_are_actual_fills': False,
        'actual_orders_submitted': False, 'score_is_probability': False,
        'hypothesis_status': 'fixed_prospective_hypotheses_not_validated',
        'coverage': {'candidate_pool_count': 0, 'valid_pool_count': 0, 'base_cohort_count': 0,
                     'market_gate_passed_count': 0, 'sector_gate_passed_count': 0,
                     'leadership_gate_passed_count': 0, 'confluence_count': 0,
                     'rejections': {}}}
    plan_valid = (isinstance(opening_plan, dict) and opening_plan.get('base_date') == base
        and opening_plan.get('target_date') == target
        and sector._known(opening_plan.get('information_cutoff'), known_at))
    if not plan_valid:
        result['reason'] = 'opening_plan_missing_or_date_cutoff_mismatch'
        return _seal(result)
    pool = opening_plan.get('candidate_pool')
    if not isinstance(pool, list):
        result['reason'] = 'opening_candidate_pool_missing_no_watchlist_substitution'
        return _seal(result)
    result['coverage']['candidate_pool_count'] = len(pool)
    technical, technical_reason = sector._technical_boards(outlook, base, target, known_at) if outlook else ([], sector_reason)
    result['technical_sector_reason'] = technical_reason
    rejected = Counter()
    with sector._connection(db) as connection:
        if connection.execute('PRAGMA application_id').fetchone()[0] != sector.APP_ID:
            raise ValueError('original market database required')
        flow_boards, _, flow_reason = sector._flow_boards(connection, _flow(fund_flow), base,
            known_at, outlook.get('benchmark', {}) if outlook else {})
        result['flow_reason'] = flow_reason
        boards = flow_boards + technical
        symbols = [row.get('symbol') for row in pool if isinstance(row, dict) and isinstance(row.get('symbol'), str)]
        symbols.extend(symbol for board in boards for symbol in board['members'])
        histories = sector._history(connection, symbols, base, known_at)
        valid = []
        seen = set()
        for row in sorted((item for item in pool if isinstance(item, dict)),
                          key=lambda item: (str(item.get('symbol', '')), -float(sector._number(item.get('research_score')) or 0))):
            symbol, close, score = row.get('symbol'), sector._number(row.get('close')), sector._number(row.get('research_score'))
            if not isinstance(symbol, str) or symbol in seen:
                rejected['invalid_or_duplicate_symbol'] += 1; continue
            seen.add(symbol)
            if close is None or not 5 <= close <= 100 or score is None or not 0 <= score <= 100:
                rejected['invalid_price_or_research_score'] += 1; continue
            if row.get('feature_last_session') != base or symbol not in histories:
                rejected['candidate_history_or_feature_session_unverified'] += 1; continue
            if abs(histories[symbol][-1]['close'] - close) > 1e-6:
                rejected['candidate_base_close_mismatch'] += 1; continue
            floor, cap, quantity = _bounds(close)
            supplied_cap = sector._number(row.get('frozen_buy_cap'))
            if supplied_cap is None or abs(supplied_cap - cap) > 1e-8:
                rejected['candidate_frozen_cap_differs_from_protocol'] += 1; continue
            if cap < floor or quantity < 100:
                rejected['no_affordable_lot_within_frozen_bounds'] += 1; continue
            valid.append({'symbol': symbol, 'name': row.get('name') if isinstance(row.get('name'), str) else '',
                'base_close': close, 'research_score': score, 'frozen_entry_floor': floor,
                'frozen_entry_cap': cap, 'frozen_buy_cap': cap, 'frozen_quantity_shares': quantity,
                'frozen_reserved_cash_cny': float(Decimal(str(cap)) * quantity),
                'stock_features': sector._member_features(histories[symbol]),
                'history_snapshot_sha256': _digest([_pick(item, BAR_FIELDS) for item in histories[symbol][-21:]])})
        valid.sort(key=lambda row: (-row['research_score'], row['symbol']))
        cohort = valid[:MAX_COHORT]
        result['coverage']['valid_pool_count'] = len(valid)
        contexts = []
        for board in sorted(boards, key=lambda item: (-int(item['flow_integrated']),
                item['classification'], str(item.get('sector_code') or ''), item['sector'])):
            snapshot, features, ranks = _board_snapshot(board, histories,
                outlook.get('benchmark', {}) if outlook else {}, outlook.get('frozen_members', {}) if outlook else {})
            contexts.append((board, snapshot, features, ranks))
        used_boards = {}
        for candidate in cohort:
            candidate.update(sector_identity=None, sector=None, sector_classification=None,
                membership_snapshot_sha256=None, board_id=None, relative_return_5d=None,
                leadership_percentile=None, leadership_rank=None, leadership_member_count=0,
                leadership_top_count=0, sector_gate_eligible=False, leadership_gate_eligible=False,
                flow_known=False, flow_credit=False, evidence_tier='sector_unknown')
            matching = [context for context in contexts if candidate['symbol'] in context[0]['members']]
            reasons = list(market['reasons'])
            if not matching:
                reasons.append('verified_frozen_sector_membership_unavailable')
            else:
                _, board, features, ranks = matching[0]
                used_boards[board['board_id']] = board
                symbol = candidate['symbol']
                rank = ranks.get(symbol)
                relative = (candidate['stock_features']['return_5d'] - board['components']['return_5d']
                            if rank is not None else None)
                leader = (rank is not None and relative > 0 and rank <= board['leadership_top_count'])
                candidate.update(sector_identity=deepcopy(board['sector_identity']),
                    sector=board['sector_identity']['sector'], sector_classification=board['sector_identity']['classification'],
                    membership_snapshot_sha256=board['membership_snapshot_sha256'], board_id=board['board_id'],
                    relative_return_5d=relative, leadership_percentile=rank / len(ranks) if rank else None,
                    leadership_rank=rank, leadership_member_count=len(ranks), leadership_top_count=board['leadership_top_count'],
                    sector_gate_eligible=board['sector_gate_eligible'], leadership_gate_eligible=leader,
                    flow_known=board['flow_known'], flow_credit=board['flow_credit'], evidence_tier=board['evidence_tier'])
                reasons.extend(board['reasons'])
                if relative is None or relative <= 0: reasons.append('stock_minus_board_return_5d_not_positive_or_unknown')
                if rank is None or rank > board['leadership_top_count']:
                    reasons.append('stock_not_top_30_percent_of_full_board_history')
            candidate.update(confluence_eligible=not reasons, reasons=reasons,
                formal_trade_ready=False, actual_win_rate=None, execution_win_rate_80_validated=False)
        result['base_candidates'] = cohort
        result['sector_boards'] = sorted(used_boards.values(), key=lambda row: row['board_id'])
    result['coverage'].update(base_cohort_count=len(cohort), market_gate_passed_count=len(cohort) if market['eligible'] else 0,
        sector_gate_passed_count=sum(row['sector_gate_eligible'] for row in cohort),
        leadership_gate_passed_count=sum(row['leadership_gate_eligible'] for row in cohort),
        confluence_count=sum(row['confluence_eligible'] for row in cohort), rejections=dict(sorted(rejected.items())))
    result['status'] = 'frozen_prospective_research_only' if cohort else 'no_base_candidates'
    result['reason'] = None if cohort else 'no_valid_opening_pool_candidates'
    return _seal(result)


def _seal(result):
    # Source database bytes and unrelated report metadata are provenance, not
    # features.  D1/D2 labels and arbitrary caller keys never enter this seal.
    identity = {key: result[key] for key in ('schema', 'base_trade_date', 'target_trade_date',
        'D2_trade_date', 'generated_at', 'protocol_sha256', 'forward_eligible',
        'forward_ineligible_reasons', 'base_candidates', 'sector_boards', 'market_gate')}
    value = _digest(identity)
    result.update(cohort_id=value, cohort_sha256=value)
    return result
