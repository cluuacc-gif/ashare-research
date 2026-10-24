"""An enabled, fixed-rule T+1 system producing conditional manual trade plans.

No historical pass rate switches this system off.  The output is a frozen
decision rule, not a confirmed signal, broker instruction, fill, or calibrated
profit probability.  This module is read-only and uses the standard library.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json

import nextday_cohort as cohort
import sector_stock_selection as sector
from price_action_snapshot import price_action_snapshot
from active_price_action import snapshot as structure_snapshot
from security_selection import load_metadata, rejection_reasons

SCHEMA = 'active-tplus1-trade-system-v1'
MAX_CANDIDATES = 300
PROTOCOL = {
    'schema': 'active-tplus1-manual-protocol-v1',
    'ownership': 'independent_project_fixed_rules',
    'activation': 'enabled_independently_of_empirical_validation_or_legacy_selection',
    'mode': 'conditional_manual_execution',
    'candidate_universe': 'up_to_300_valid_opening_pool_candidates_no_top12_restriction',
    'features_end': 'T_close_with_all_sources_observed_at_or_before_information_cutoff',
    'freeze_deadline_exclusive': 'D1 09:15:00 Asia/Shanghai',
    'calendar': 'D1_is_verified_session_immediately_after_T_and_D2_immediately_after_D1',
    'market_gate': {'breadth_gte': .5, 'return_1d_gte': 0, 'return_5d_gte': 0},
    'sector_gate': {
        'phase': 'strengthening', 'breadth_gte': .55, 'relative_return_5d_gt': 0,
        'minimum_valid_members': 5, 'minimum_history_coverage': .65,
        'actual_same_vendor_flow': 'both_main_net_inflow_and_ratio_strictly_positive',
        'membership': 'same_vendor_frozen_constituents_no_cross_taxonomy_name_join',
        'unknown_flow': 'observation_only_zero_quantity',
    },
    'leadership': 'positive_stock_minus_board_return5_and_top_ceil(30%_full_valid_board_members)',
    'price_action': {
        'history_bars': 61, 'price_range_cny': [5, 100],
        'median_amount20_gte_cny': 50000000, 'atr14_over_close_range': [.008, .04],
        'ret20_gt': 0, 'ma20_gap_range': [0, .08], 'ret1_range': [-.02, .04],
        'close_location_gte': .6, 'legacy_pattern_or_55_score_required': False,
    },
    'rank': {
        'formula': '.5*clip(stock_minus_board_return5/.05,0,1)+.3*close_location+.2*clip(efficiency20,0,1)',
        'normalization': 'fixed_absolute_transforms_no_cross_sectional_refit',
        'tie_break': 'symbol_ascending', 'is_probability': False,
    },
    'structural_stop': 'floor_cent(min(T_last5_daily_lows)-.15*ATR14)',
    'target_pct': .03, 'secondary_observation_target_pct': .06,
    'secondary_target_promised_or_used_for_entry': False,
    'minimum_reward_risk': 1.5,
    'structural_risk_pct_range': [.008, .02],
    'entry_cap': 'floor_cent(min(Tclose*1.025,100,stop/.98)); exact_decimal_joint_3%_target_RR1.5',
    'entry_floor': 'max(5,ceil_cent(Tclose*.99),ceil_cent(stop/.992)); require floor<=cap',
    'overhead_resistance_gate': 'none; prior60_high_is_descriptive_not_an_invented_target',
    'account': {
        'reference_cash_cny': 30000, 'maximum_total_names': 2,
        'maximum_names_per_exact_sector_identity': 1, 'cash_per_name_limit_cny': 10000,
        'risk_budget_per_name_cny': 150, 'additional_gap_stress_pct': .02,
        'quantity': 'floor_to_100_shares(min(frozen_cash_per_name/cap,150/(cap-stop+cap*.02)))',
        'minimum_order': 'official_STAR_688_689_buy_minimum_200_shares; project_uses_100_share_steps; '
                         'never_upsize_beyond_frozen_cash_or_risk',
        'allocation': 'rank_at_freeze_no_forced_quota_no_intraday_reallocation',
    },
    'entry': {
        'session': 'D1', 'decision_time': '14:40', 'arm_id': 'confluence_late_confirmation',
        'manual_review_window': 'D1_14:40_through_14:45_Asia_Shanghai_with_fresh_quote',
        'maximum_entry_price': 'min(T_frozen_cap,floor_cent(completed_14:40_confirmation_close*1.003))',
        'reference_entry': 'D1_OPEN_of_minute_STARTING_14:40_ending_14:41_after_completed_prefix; '
                           'archived_reference_convention_is_not_the_manual_quote_or_an_actual_fill',
        'coverage': 'complete_continuous_session_completed_minute_prefix_through_14:40_preserving_lunch_gap',
        'confirmation': 'latest_completed_bar_bullish_close>=session_open_and_close_location>=.6; '
                        'close>=volume_weighted_mean_completed_bar_close_proxy; '
                        'min(last5_completed_lows)>=min(previous5_completed_lows)',
        'manual_quote': 'fresh_broker_quote_after_confirmation_must_be_inside_frozen_floor_cap_and_above_stop',
        'official_status': 'verify_exchange_non_ST_non_halted_non_limit_before_buy; unknown_blocks_manual_entry',
        'quantity': 'only_the_frozen_100_share_lot_quantity_no_reallocation_if_other_plan_does_not_trigger',
        'live_feed_connected': False, 'broker_connected': False,
        'condition_is_confirmed_signal': False,
    },
    'exit': {
        'earliest_session': 'D2', 'same_day_sale': False,
        'D1_close_stop': 'if_known_D1_close<=frozen_stop_exit_at_D2_first_tradable_minute_open',
        'D2_gap': 'first_available_D2_open<=stop_or>=actual_buy_price*1.03_exits_at_that_reference_open',
        'intraday_trigger': 'completed_D2_minute_low<=stop_or_high>=actual_buy_price*1.03; stop_wins_if_both',
        'intraday_reference': 'exit_at_OPEN_of_next_available_consecutive_tradable_minute_bar_never_at_exact_target',
        'no_trigger': 'fixed_D2_minute_STARTING_14:50_OPEN_ending_14:51_no_price_trigger_needed',
        'missing_or_unfillable': 'retain_unknown_or_pending_never_invent_a_fill_or_drop_the_position',
        'references_are_actual_fills': False,
    },
    'empirical_validation_passed': False, 'actual_win_rate': None,
    'execution_win_rate_80_validated': False,
}
# Keep the exact prior protocol identity: published decisions and their capital
# survive this selection-only upgrade. Entry, sizing and exits are unchanged.
LEGACY_PROTOCOL = deepcopy(PROTOCOL)
LEGACY_PROTOCOL_SHA256 = cohort._digest(LEGACY_PROTOCOL)
PROTOCOL['schema'] = 'active-tplus1-manual-protocol-v2'
PROTOCOL['universe_exclusions'] = 'all_30_prefix_or_ChiNext_board; NFKC_whitespace_removed_ST_and_delisting_names; unknown_or_future_master_metadata_blocks'
PROTOCOL['price_action']['structure_required'] = 'reduced_volume_pullback_recovery_or_compact_breakout_v2'
PROTOCOL['price_action']['close_location_gte'] = .65
PROTOCOL['price_action']['upper_wick_atr_lte'] = .5
PROTOCOL['price_action']['structure_v2'] = {
    'recovery_bar': 'Tclose>Topen_and_Tclose>prior_bar_high',
    'pullback': 'any_red_bar_in_prior3; mean(prior3_volume)<=mean(bars[-14:-4]_volume); '
                'min(prior3_lows)>=MA20-.25*ATR14',
    'compact_breakout': 'Tclose>=max(prior5_highs); prior5_vs_prior20_TR_ratio<=1; '
                        'volume_ratio20_in_[1,2.5]; (Tclose-max(prior5_highs))/ATR14_in_[0,.5]',
    'resistance_pivot': 'strict_high_above_two_completed_bars_each_side; '
                        'prominence_above_min_neighbor_low>=.5*ATR14; pivot>Tclose',
    'resistance_target_buffer_ATR': .1,
    'no_identified_resistance': 'unknown_future_room_not_guaranteed',
}
PROTOCOL['rank']['formula'] = '.35*clip(relative_return5/.05,0,1)+.25*close_location+.15*clip(efficiency20,0,1)+.15*structure_quality+.10*room_quality'
PROTOCOL['rank']['structure_quality'] = '1_for_pullback_recovery_else_clip(1-max(0,breakout_atr_prior20)/.5,0,1)'
PROTOCOL['rank']['room_quality'] = '.5_when_no_identified_pivot_else_clip((pivot/cap-1)/.06,0,1)'
PROTOCOL['overhead_resistance_gate'] = 'nearest_confirmed_last61_pivot_high_above_known_T_close_must_clear_cap*1.03+.1*ATR; no_identified_pivot_is_not_guaranteed_room'
PROTOCOL['migration'] = 'only_known_v1_selection_upgrade; preserve_first_published_target_decision_and_entire_reference_account; legacy_positions_keep_unchanged_exit_rules'
PROTOCOL_SHA256 = cohort._digest(PROTOCOL)


def compatible_policy_migration(previous_id, next_id):
    return previous_id == LEGACY_PROTOCOL_SHA256 and next_id == PROTOCOL_SHA256


def _decimal(value):
    number = sector._number(value)
    if number is None:
        raise ValueError('finite numeric value required')
    return Decimal(str(value))


def _floor(value):
    return value.quantize(Decimal('.01'), rounding=ROUND_FLOOR)


def _ceil(value):
    return value.quantize(Decimal('.01'), rounding=ROUND_CEILING)


def _bounds(close, stop):
    """Jointly solve the fixed target/RR envelope using decimal cent ticks."""
    close, stop = _decimal(close), _decimal(stop)
    cap = _floor(min(close * Decimal('1.025'), Decimal('100'), stop / Decimal('.98')))
    floor = max(Decimal('5'), _ceil(close * Decimal('.99')), _ceil(stop / Decimal('.992')))
    valid = (0 < stop < floor <= cap and
             Decimal('.008') <= (cap - stop) / cap <= Decimal('.02'))
    return floor, cap, valid


def _quantity(cap, stop, cash):
    limit = min(Decimal('10000'), cash)
    stressed_per_share = cap - stop + cap * Decimal('.02')
    if cap <= 0 or stressed_per_share <= 0 or limit <= 0:
        return 0
    shares = min(limit / cap, Decimal('150') / stressed_per_share)
    return int((shares / 100).to_integral_value(rounding=ROUND_FLOOR)) * 100


def _sector_key(identity):
    if isinstance(identity, str):
        try:
            identity = json.loads(identity)
        except (TypeError, ValueError):
            return identity
    if not isinstance(identity, dict):
        return None
    return json.dumps({key: identity.get(key) for key in ('classification', 'sector_code', 'sector')},
                      ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _price_action_reasons(features):
    if not features.get('available'):
        return [features.get('reason') or 'requires_61_valid_trailing_bars']
    checks = (
        (features.get('median_amount20') is not None and features['median_amount20'] >= 50000000,
         'median_amount20_below_50million_or_unknown'),
        (.008 <= features['atr14_pct'] <= .04, 'atr14_pct_outside_0.008_to_0.04'),
        (features['ret20'] > 0, 'ret20_not_positive'),
        (0 <= features['ma20_gap'] <= .08, 'ma20_gap_outside_0_to_0.08'),
        (-.02 <= features['ret1'] <= .04, 'ret1_outside_minus0.02_to_0.04'),
        (features['close_location'] >= .6, 'close_location_below_0.6'),
    )
    return [reason for passed, reason in checks if not passed]


def _plan_id(plan, policy_id):
    # Database byte hashes, arbitrary caller fields, outcome labels and fills
    # are deliberately absent. Each included field was known at the T freeze.
    identity = {key: plan[key] for key in (
        'symbol', 'sector_identity', 'base_trade_date', 'target_trade_date', 'D2_trade_date',
        'base_close', 'frozen_entry_floor', 'frozen_entry_cap', 'structural_stop', 'target_pct',
        'frozen_quantity_shares', 'frozen_reserved_cash_cny', 'frozen_reserved_risk_cny',
        'atr14', 'rank_score', 'evidence')}
    return 'active-' + cohort._digest({'policy_id': policy_id, 'T_known_plan': identity})[:24]


def build(db, base, target, cutoff, sector_outlook, opening_plan, fund_flow=None, *,
          input_sha256=None, available_cash=30000, occupied_symbols=(), occupied_sectors=()):
    """Build up to two frozen conditional plans; an empty set leaves us enabled.

    ``available_cash`` is unreserved cash supplied by the caller. Existing names
    consume the two-name limit, and existing exact sector identities block a
    second position in that sector. No funds are moved and no orders are sent.
    """
    cash = _decimal(available_cash)
    if cash < 0:
        raise ValueError('available_cash cannot be negative')
    occupied = set(occupied_symbols)
    if any(not isinstance(symbol, str) for symbol in occupied):
        raise ValueError('occupied_symbols must contain symbol strings')
    used_sectors = {key for identity in occupied_sectors if (key := _sector_key(identity)) is not None}
    frozen = cohort.build(db, base, target, cutoff, sector_outlook, opening_plan,
                          fund_flow, input_sha256=input_sha256, max_candidates=MAX_CANDIDATES)
    protocol = deepcopy(PROTOCOL)
    policy_id = PROTOCOL_SHA256
    result = {
        'schema': SCHEMA, 'system_enabled': True, 'signal_generation_enabled': True,
        'mode': 'conditional_manual_execution', 'order_execution_enabled': False,
        'empirical_validation_passed': False, 'formal_trade_ready': False,
        'actual_win_rate': None, 'execution_win_rate_80_validated': False,
        'actual_orders_submitted': False, 'reference_fills_are_actual_fills': False,
        'broker_connected': False, 'live_feed_connected': False, 'score_is_probability': False,
        'base_trade_date': base, 'target_trade_date': target, 'D2_trade_date': frozen['D2_trade_date'],
        'generated_at': frozen['generated_at'], 'information_cutoff': frozen['information_cutoff'],
        'selection_rule_version': 2, 'maximum_selected_names': 2,
        'plan_policy_id': PROTOCOL_SHA256, 'new_policy_applies_to_new_frozen_dates_only': True,
        'input_database_sha256': input_sha256, 'protocol': protocol,
        'policy_id': policy_id, 'protocol_sha256': policy_id,
        'status': 'cash_no_qualifying_setup', 'reason': None,
        'source_forward_eligible': frozen['forward_eligible'],
        'source_forward_ineligible_reasons': list(frozen['forward_ineligible_reasons']),
        'source_reasons': {key: frozen.get(key) for key in
                           ('reason', 'sector_outlook_reason', 'technical_sector_reason', 'flow_reason')},
        'market_gate': deepcopy(frozen['market_gate']), 'plans': [], 'candidate_evaluations': [],
        'available_cash_cny': float(cash), 'occupied_symbols': sorted(occupied),
        'free_position_slots': max(0, 2 - len(occupied)),
        'coverage': {**deepcopy(frozen['coverage']), 'active_evaluated_count': 0,
                     'active_qualifying_count': 0, 'conditional_plan_count': 0,
                     'price_action_passed_count': 0, 'active_rejections': {}},
    }
    source_ok = (frozen['forward_eligible'] and frozen['sector_outlook_reason'] is None
                 and frozen.get('reason') not in
                 ('opening_plan_missing_or_date_cutoff_mismatch', 'opening_candidate_pool_missing_no_watchlist_substitution'))
    if not source_ok:
        result['status'] = 'waiting_for_valid_source'
        result['reason'] = (frozen['forward_ineligible_reasons'][0] if frozen['forward_ineligible_reasons']
                            else frozen['sector_outlook_reason'] or frozen.get('reason'))
    rows = frozen['base_candidates']
    known_at = sector._instant(cutoff)
    with sector._connection(db) as connection:
        histories = sector._history(connection, [row['symbol'] for row in rows], base, known_at, minimum=61)
        metadata = load_metadata(connection, [row['symbol'] for row in rows], base=base, cutoff=cutoff)
    rejected, qualifying = Counter(), []
    market = result['market_gate']
    for row in rows:
        reasons = list(row['reasons'])
        reasons.extend(rejection_reasons(row['symbol'], metadata.get(row['symbol']), cutoff, base=base))
        if not row['flow_known']:
            reasons.append('actual_same_vendor_flow_unknown_observation_only')
        elif not row['flow_credit']:
            reasons.append('actual_same_vendor_net_and_ratio_must_both_be_positive')
        history = histories.get(row['symbol'], [])
        try:
            features = price_action_snapshot(history, market_return5=market.get('base_return_5d') or 0,
                market_breadth=market.get('base_breadth'), market_mean_return=market.get('base_return_1d'))
        except ValueError:
            features = {'available': False, 'reason': 'invalid_or_duplicate_price_action_history'}
        pa_reasons = _price_action_reasons(features)
        reasons.extend(pa_reasons)
        if not pa_reasons:
            result['coverage']['price_action_passed_count'] += 1
        if not source_ok:
            reasons.append('source_window_or_required_source_invalid')
        evaluation = {'symbol': row['symbol'], 'name': row['name'],
            'sector_identity': deepcopy(row['sector_identity']), 'base_close': row['base_close'],
            'status': 'observation_only', 'eligible': False, 'frozen_quantity_shares': 0,
            'flow_known': row['flow_known'], 'flow_credit': row['flow_credit'],
            'leadership_rank': row['leadership_rank'], 'leadership_member_count': row['leadership_member_count'],
            'leadership_top_count': row['leadership_top_count'], 'relative_return_5d': row['relative_return_5d'],
            'price_action': {key: features.get(key) for key in
                ('available', 'median_amount20', 'atr14_pct', 'ret20', 'ma20_gap', 'ret1', 'close_location', 'efficiency20')},
            'reasons': [], 'rank_score': None}
        if features.get('available'):
            relative = row['relative_return_5d']
            score = None
            atr = features['atr14_pct'] * row['base_close']
            stop = _floor(min(_decimal(bar['low']) for bar in history[-5:]) - Decimal('.15') * _decimal(atr))
            floor, cap, bounds_ok = _bounds(row['base_close'], stop)
            structure = structure_snapshot(history, features, float(cap))
            reasons.extend(structure['reasons'])
            room = structure.get('room_from_entry_cap')
            structure_quality = (0 if not structure['available'] else
                                 1 if 'reduced_volume_pullback_recovery' in structure['patterns'] else
                                 max(0, 1 - max(0, features['breakout_atr']) / .5))
            score = (.35 * max(0, min(1, (relative or 0) / .05))
                     + .25 * features['close_location'] + .15 * max(0, min(1, features['efficiency20']))
                     + .15 * structure_quality + .10 * (.5 if room is None else max(0, min(1, room / .06))))
            evaluation['price_action_structure'] = structure
            evaluation.update(rank_score=score, atr14=atr, structural_stop=float(stop),
                              frozen_entry_floor=float(floor), frozen_entry_cap=float(cap))
            if not bounds_ok:
                reasons.append('no_joint_3percent_target_rr1.5_cent_price_envelope')
            if not reasons:
                evaluation['eligible'] = True
                evaluation['status'] = 'qualifying_frozen_setup'
                prior_high = max(bar['high'] for bar in history[-61:-1])
                evidence = {
                    'history61_sha256': cohort._digest([cohort._pick(bar, cohort.BAR_FIELDS) for bar in history[-61:]]),
                    'board_id': row['board_id'], 'membership_snapshot_sha256': row['membership_snapshot_sha256'],
                    'flow_evidence_tier': row['evidence_tier'], 'relative_return_5d': relative,
                    'leadership_rank': row['leadership_rank'], 'leadership_member_count': row['leadership_member_count'],
                    'leadership_top_count': row['leadership_top_count'],
                    'price_action': deepcopy(evaluation['price_action']),
                    'price_action_structure': deepcopy(structure),
                    'security_metadata': deepcopy(metadata.get(row['symbol'])),
                    'prior60_high_descriptive': prior_high,
                    'prior60_high_room_from_cap_descriptive': prior_high / float(cap) - 1,
                }
                qualifying.append((evaluation, floor, cap, stop, evidence))
        evaluation['reasons'] = list(dict.fromkeys(reasons))
        rejected.update(evaluation['reasons'])
        result['candidate_evaluations'].append(evaluation)
    qualifying.sort(key=lambda item: (-item[0]['rank_score'], item[0]['symbol']))
    remaining_cash = cash
    free_slots = result['free_position_slots']
    for evaluation, floor, cap, stop, evidence in qualifying:
        symbol = evaluation['symbol']
        identity_key = _sector_key(evaluation['sector_identity'])
        reason = ('symbol_already_occupied' if symbol in occupied else
                  'exact_sector_already_occupied_or_planned' if identity_key in used_sectors else
                  'two_name_limit_including_existing_positions' if len(result['plans']) >= free_slots else None)
        quantity = _quantity(cap, stop, remaining_cash) if reason is None else 0
        if reason is None and quantity < 100:
            reason = 'cash_or_stressed_risk_budget_below_one_board_lot'
        if reason is None and symbol.startswith(('688', '689')) and quantity < 200:
            reason = 'STAR_minimum_200_shares_exceeds_frozen_cash_or_risk_budget'
        if reason:
            evaluation['status'] = 'qualifying_but_unallocated'
            evaluation['reasons'].append(reason)
            rejected[reason] += 1
            continue
        reserved_cash = cap * quantity
        reserved_risk = (cap - stop + cap * Decimal('.02')) * quantity
        plan = {'symbol': symbol, 'name': evaluation['name'],
            'sector_identity': deepcopy(evaluation['sector_identity']), 'base_trade_date': base,
            'target_trade_date': target, 'D2_trade_date': frozen['D2_trade_date'],
            'generated_at': frozen['generated_at'], 'base_close': evaluation['base_close'],
            'frozen_entry_floor': float(floor), 'frozen_entry_cap': float(cap),
            'structural_stop': float(stop), 'target_pct': .03,
            'frozen_quantity_shares': quantity, 'frozen_reserved_cash_cny': float(reserved_cash),
            'frozen_reserved_risk_cny': float(reserved_risk), 'risk_budget_cny': 150,
            'gap_stress_pct': .02, 'atr14': evaluation['atr14'], 'rank_score': evaluation['rank_score'],
            'entry_arm_id': 'confluence_late_confirmation', 'entry_decision_time': '14:40',
            'entry_confirmed': False, 'status': 'conditional_manual_plan',
            'actual_fill': None, 'formal_trade_ready': False, 'actual_win_rate': None,
            'execution_win_rate_80_validated': False, 'score_is_probability': False,
            'evidence': evidence}
        plan['active_plan_id'] = _plan_id(plan, policy_id)
        result['plans'].append(plan)
        evaluation.update(status='conditional_manual_plan', active_plan_id=plan['active_plan_id'],
                          frozen_quantity_shares=quantity)
        used_sectors.add(identity_key)
        remaining_cash -= reserved_cash
    result['coverage'].update(active_evaluated_count=len(rows), active_qualifying_count=len(qualifying),
        conditional_plan_count=len(result['plans']), active_rejections=dict(sorted(rejected.items())))
    result['unreserved_cash_cny'] = float(remaining_cash)
    if source_ok:
        result['status'] = 'conditional_plans_ready' if result['plans'] else 'cash_no_qualifying_setup'
        result['reason'] = None if result['plans'] else 'no_qualifying_unoccupied_affordable_setup'
    return result
