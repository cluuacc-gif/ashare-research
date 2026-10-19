"""Pre-registered T+1 minute-reference research, independent of order execution.

Only an actually published, timely frozen cohort enters the forward ledger.
The six arms are fixed; none can be promoted by this module. Unknown entries
and exits survive reruns. Historical minute observations are not live quotes.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, time, timedelta
import hashlib
import json
import math
import random
import statistics

from nextday_minutes import TZ, instant, number
from trade_calendar import adjacent

SCHEMA = 'nextday-forward-research-v1'
ARMS = ('base_0940', 'base_1440', 'confluence_0940', 'confluence_1440',
        'confluence_opening_retest', 'confluence_late_confirmation')
RULES = {
    'version': 'nextday-matched-minutes-20261019-v1', 'fixed_comparisons': 6,
    'primary_arm': 'confluence_opening_retest', 'arms': list(ARMS),
    'entry_decisions': {'morning': '09:40', 'afternoon': '14:40'},
    'entry_reference': 'open_of_one_minute_bar_STARTING_at_decision_time',
    'exit_reference': 'D2_open_of_one_minute_bar_STARTING_at_14:50',
    'signal_uses': 'completed_minutes_ENDING_no_later_than_decision',
    'opening_retest': {'opening_range_minutes': 5, 'earlier_breakout_above_range_cny': .01,
        'retest_tolerance_of_frozen_close': .002, 'latest_bar_bullish': True,
        'latest_close_location_minimum': .6,
        'intervening_close_below_level_minus_tolerance_invalidates': True},
    'late_confirmation': {'above_session_open': True,
        'above_volume_weighted_mean_completed_bar_close': True,
        'weighted_bar_close_is_exact_VWAP': False,
        'last5_minimum_low_at_least_prior5_minimum_low': True,
        'latest_bar_bullish': True, 'latest_close_location_minimum': .6},
    'primary_endpoint': 'D2_reference_exit_divided_by_D1_reference_entry_minus_one_gt_zero',
    'secondary_endpoints': ['D2_exit_return_ge_3pct', 'D2_exit_return_ge_6pct'],
    'D1_sell_allowed': False, 'D3_extends_holding': False,
    'target_or_stop_intrabar_path_inferred': False, 'fees_cny': 0, 'slippage_bps': 0,
    'same_cohort_same_exit_controls': True, 'filtered_cohort_is_subset_without_replacement': True,
    'units': 'independent_frozen_stock_observations_not_portfolio_return',
    'entry_and_exit_need_positive_minute_volume': True,
    'daily_one_price_entry_or_exit_is_unresolved': True,
    'minute_raw_prices_cross_checked_with_verified_daily_range': True,
    'raw_30pct_close_jump_is_unresolved_not_assumed_profit': True,
    'unknowns_retained': True, 'first_published_cohort_per_target_and_protocol': True,
    'historical_backfill_allowed': False, 'model_predicts_probability': False,
    'D1_entry_evidence_deadline_exclusive': 'D2_09:15_Asia_Shanghai',
    'late_exit_label_recovery_at_original_D2_timestamp_allowed': True,
    'bootstrap': {'resampling_unit': 'D1_entry_date_and_moving5_active_entry_dates', 'draws': 2000, 'seed': 20261018,
        'family_alpha': .05, 'comparisons': 6, 'minimum_closed': 40, 'minimum_dates': 20},
    'automatic_policy_activation': False, 'actual_fill_verified': False,
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


from nextday_cohort import PROTOCOL as COHORT_RULES
RULES['cohort_protocol_sha256'] = digest(COHORT_RULES)
PROTOCOL_SHA256 = digest(RULES)


def initialize(cohort, base, target, database_sha256, cutoff):
    return {'schema': SCHEMA, 'base_trade_date': base, 'target_trade_date': target,
        'input_database_sha256': database_sha256, 'generated_at': instant(cutoff).isoformat(),
        'protocol_sha256': PROTOCOL_SHA256, 'protocol': deepcopy(RULES),
        'current_cohort': deepcopy(cohort), 'tracked_cohorts': [], 'records': [],
        'summary': summarize([]), 'paired_timing': paired_timing([]), 'prospective_only': True,
        'formal_trade_ready': False, 'actual_win_rate': None,
        'execution_win_rate_80_validated': False, 'costs_included': False,
        'orders_submitted': False, 'smtp_sent': False,
        'limitations': [
            '分钟接口盘后取得原始未复权OHLC，仅用于固定时点参考价格事件，不证明当时看到或实际成交。',
            '官方历史状态、限价、复权和退市完整样本仍有缺口，实际成交胜率保持未知。',
            '本次开始逐日冻结；不把已经看过的历史补写为未来样本。',
            '六组观察不是同一资金账户的组合收益；禁止把重叠观察视为独立交易日。',
            '未接入实时行情与独立云主机，不能提供准点盘中触发或保证准点邮件。']}


def expected_ends(day, until):
    result = []
    for start, finish in ((time(9, 31), time(11, 30)), (time(13, 1), time(15))):
        value = datetime.combine(datetime.fromisoformat(day).date(), start, TZ)
        last = datetime.combine(value.date(), finish, TZ)
        while value <= last and value <= until:
            result.append(value.isoformat())
            value += timedelta(minutes=1)
    return result


def _valid_minute(row, day, cutoff):
    if not isinstance(row, dict):
        return False
    try:
        end, start = instant(row['end_at']), instant(row['start_at'])
        values = {key: number(row.get(key)) for key in ('open', 'high', 'low', 'close', 'volume', 'amount')}
        return (end.date().isoformat() == day and end <= cutoff
            and end-start == timedelta(minutes=1)
            and (time(9, 31) <= end.time() <= time(11, 30)
                 or time(13, 1) <= end.time() <= time(15))
            and all(values[key] is not None and values[key] > 0 for key in ('open', 'high', 'low', 'close'))
            and all(values[key] is not None and values[key] >= 0 for key in ('volume', 'amount'))
            and values['low'] <= min(values['open'], values['close'])
                <= max(values['open'], values['close']) <= values['high'])
    except (KeyError, ValueError, TypeError):
        return False


def minute_index(evidence, symbol, day, cutoff):
    item = evidence.get(symbol) or {}
    if item.get('symbol') != symbol or not item.get('bars'):
        return {}, None
    try:
        observed = instant(item['observed_at'])
        if observed > cutoff or len(item.get('raw_sha256', '')) != 64:
            return {}, None
    except (KeyError, ValueError, TypeError):
        return {}, None
    rows = [row for row in item['bars'] if _valid_minute(row, day, cutoff)]
    if len({row['end_at'] for row in rows}) != len(rows):
        return {}, None
    provenance = {key: item.get(key) for key in ('raw_sha256', 'raw_asset', 'source_url', 'observed_at')}
    provenance['live_seen_at_decision_verified'] = False
    return {row['end_at']: row for row in rows}, provenance


def trigger(prefix, arm, frozen_close):
    """Prefix must be complete; the entry minute is deliberately unavailable."""
    if arm not in ARMS[-2:]:
        return True
    last = prefix[-1]
    span = last['high']-last['low']
    if last['close'] <= last['open'] or span <= 0 or (last['close']-last['low'])/span < .6:
        return False
    if arm == 'confluence_opening_retest':
        level = max(row['high'] for row in prefix[:5])
        tolerance = .002*frozen_close
        breaks = [i for i in range(5, len(prefix)-1) if prefix[i]['close'] >= level+.01-1e-9]
        if not breaks:
            return False
        first = breaks[0]
        return (level-tolerance <= last['low'] <= level+tolerance
            and last['close'] >= level
            and all(row['close'] >= level-tolerance for row in prefix[first+1:-1]))
    volume = sum(row['volume'] for row in prefix)
    weighted = sum(row['close']*row['volume'] for row in prefix)/volume if volume > 0 else math.inf
    return (len(prefix) >= 10 and last['close'] >= prefix[0]['open']
        and last['close'] >= weighted
        and min(row['low'] for row in prefix[-5:]) >= min(row['low'] for row in prefix[-10:-5]))


def _daily(db, symbol, day, cutoff):
    from stock_strength_tracking import _valid_bar
    cursor = db.execute('SELECT symbol,trade_date,open,high,low,close,volume,conflict,'
        'fetched_at,source_updated_at,finality FROM daily_quotes WHERE symbol=? AND trade_date=?', (symbol, day))
    raw = cursor.fetchone()
    if raw is None:
        return None
    row = dict(zip((x[0] for x in cursor.description), raw))
    return row if _valid_bar(row, cutoff) else None


def _inside_daily(bar, daily):
    return (daily is not None and daily['high'] > daily['low']
        and daily['low']-.011 <= bar['low'] <= bar['high'] <= daily['high']+.011)


def _new_record(cohort, candidate, arm):
    return {'record_id': digest([cohort['cohort_id'], PROTOCOL_SHA256, candidate['symbol'], arm]),
        'cohort_id': cohort['cohort_id'], 'protocol_sha256': PROTOCOL_SHA256,
        'symbol': candidate['symbol'], 'arm': arm,
        'signal_date': cohort['base_trade_date'], 'entry_date': cohort['target_trade_date'],
        'exit_date': cohort['D2_trade_date'], 'frozen_at': cohort['generated_at'],
        'frozen_entry_floor': candidate['frozen_entry_floor'],
        'frozen_entry_cap': candidate['frozen_entry_cap'],
        'frozen_quantity_shares': candidate['frozen_quantity_shares'],
        'status': 'pending_entry', 'gross_return': None,
        'actual_fill_verified': False, 'actual_execution_win_rate': None,
        'source': 'after_close_vendor_minute_reference', 'costs_included': False}


def evaluate(cohort, evidence, db, cutoff, previous_records=()):
    """Freeze entry observations on D1 and carry them until D2 is observable."""
    now = instant(cutoff)
    if (cohort.get('forward_eligible') is not True
            or instant(cohort['generated_at']) >= datetime.combine(
                datetime.fromisoformat(cohort['target_trade_date']).date(), time(9, 15), TZ)
            or adjacent(cohort['base_trade_date'], 1) != cohort['target_trade_date']
            or adjacent(cohort['target_trade_date'], 1) != cohort['D2_trade_date']):
        raise ValueError('only timely adjacent-session frozen cohorts may be evaluated')
    previous = {row['record_id']: deepcopy(row) for row in previous_records}
    result = []
    for candidate in cohort.get('base_candidates', []):
        symbol = candidate['symbol']
        for arm in ARMS:
            record = _new_record(cohort, candidate, arm)
            saved = previous.get(record['record_id'])
            if saved:
                if any(saved.get(key) != record[key] for key in ('cohort_id', 'protocol_sha256',
                        'symbol', 'arm', 'entry_date', 'exit_date', 'frozen_at',
                        'frozen_quantity_shares', 'frozen_entry_floor', 'frozen_entry_cap')):
                    raise ValueError('forward record frozen identity changed')
                record = saved
            if record['status'] in ('reference_closed', 'excluded_filter', 'no_signal', 'price_outside_bounds'):
                result.append(record); continue
            if arm.startswith('confluence_') and candidate.get('confluence_eligible') is not True:
                record.update(status='excluded_filter', reason='T_frozen_confluence_mask_false')
                result.append(record); continue
            clock = time(14, 40) if arm in ('base_1440', 'confluence_1440', 'confluence_late_confirmation') else time(9, 40)
            decision = datetime.combine(datetime.fromisoformat(record['entry_date']).date(), clock, TZ)
            entry_end = decision+timedelta(minutes=1)
            if 'reference_buy_price' not in record:
                if now < entry_end:
                    result.append(record); continue
                index, origin = minute_index(evidence, symbol, record['entry_date'], now)
                evidence_deadline=datetime.combine(datetime.fromisoformat(record['exit_date']).date(),time(9,15),TZ)
                if origin is None or instant(origin['observed_at'])>=evidence_deadline:
                    record.update(status='unknown_entry',reason='D1_evidence_not_captured_before_D2_opening_deadline')
                    result.append(record);continue
                prefix_ends = expected_ends(record['entry_date'], decision)
                # Even the unconditional timing control requires a complete
                # pre-decision path; holes cannot quietly select easy winners.
                if not all(end in index for end in prefix_ends) or entry_end.isoformat() not in index:
                    record.update(status='unknown_entry', reason='completed_minute_prefix_or_entry_missing')
                    result.append(record); continue
                prefix = [index[end] for end in prefix_ends]
                daily = _daily(db, symbol, record['entry_date'], now)
                if (daily is None or any(not _inside_daily(item, daily) for item in prefix)
                        or abs(daily['close']/candidate['base_close']-1) > .30):
                    record.update(status='unknown_entry', reason='signal_prefix_daily_crosscheck_missing')
                    result.append(record); continue
                if not trigger(prefix, arm, candidate['base_close']):
                    record.update(status='no_signal', reason='pre_registered_completed_bar_trigger_false',
                        entry_evidence=origin)
                    result.append(record); continue
                bar = index[entry_end.isoformat()]
                if (not _inside_daily(bar, daily) or bar['volume'] <= 0
                        or abs(daily['close']/candidate['base_close']-1) > .30):
                    record.update(status='unknown_entry', reason='entry_daily_crosscheck_or_positive_volume_missing')
                    result.append(record); continue
                buy = bar['open']
                if not candidate['frozen_entry_floor'] <= buy <= candidate['frozen_entry_cap']:
                    record.update(status='price_outside_bounds', reason='reference_open_outside_T_frozen_bounds',
                        entry_evidence=origin, observed_entry_open=buy)
                    result.append(record); continue
                record.update(status='pending_exit', reference_buy_price=buy,
                    entry_reference_at=decision.isoformat(), entry_evidence=origin,
                    D1_close=daily['close'], D1_open=daily['open'],
                    D1_after_buy_return=daily['close']/buy-1)
            exit_start = datetime.combine(datetime.fromisoformat(record['exit_date']).date(), time(14, 50), TZ)
            if now < exit_start+timedelta(minutes=1):
                record.update(status='pending_exit'); result.append(record); continue
            index, origin = minute_index(evidence, symbol, record['exit_date'], now)
            bar = index.get((exit_start+timedelta(minutes=1)).isoformat())
            daily = _daily(db, symbol, record['exit_date'], now)
            if (bar is None or not _inside_daily(bar, daily) or bar['volume'] <= 0
                    or abs(daily['close']/record['D1_close']-1) > .30):
                record.update(status='unknown_exit', reason='D2_minute_daily_crosscheck_or_volume_missing')
                result.append(record); continue
            buy, sell = record['reference_buy_price'], bar['open']
            gross = sell/buy-1
            record.update(status='reference_closed', reason='fixed_D2_minute_price_event_not_actual_fill',
                reference_sell_price=sell, exit_reference_at=exit_start.isoformat(), exit_evidence=origin,
                gross_return=gross, positive_gross=gross > 1e-12, exit_ge3=gross >= .03-1e-12,
                exit_ge6=gross >= .06-1e-12, D2_open=daily['open'],
                overnight_return=daily['open']/record['D1_close']-1,
                D2_after_open_return=sell/daily['open']-1,
                D2_close_up=daily['close'] > record['D1_close'],
                gross_pnl_reference=candidate['frozen_quantity_shares']*(sell-buy))
            result.append(record)
    return result


def summarize(records):
    output = {}
    for arm in ARMS:
        rows = [row for row in records if row['arm'] == arm]
        closed = [row for row in rows if row['status'] == 'reference_closed']
        unknown = [row for row in rows if row['status'].startswith('unknown_')]
        values = [row['gross_return'] for row in closed]
        winners = [value for value in values if value > 1e-12]
        losers = [value for value in values if value <= 1e-12]
        wins = sum(value > 1e-12 for value in values)
        dates = sorted({row['entry_date'] for row in closed})
        count = len(closed)
        summary = {'scheduled_observations': len(rows), 'reference_closed': count,
            'distinct_entry_dates': len(dates), 'unknown': len(unknown),
            'pending': sum(row['status'].startswith('pending_') for row in rows),
            'excluded_filter': sum(row['status'] == 'excluded_filter' for row in rows),
            'no_signal': sum(row['status'] == 'no_signal' for row in rows),
            'price_outside_bounds': sum(row['status'] == 'price_outside_bounds' for row in rows),
            'reference_gross_profit_rate': wins/count if count else None,
            'reference_ge3_rate': sum(row['exit_ge3'] for row in closed)/count if count else None,
            'reference_ge6_rate': sum(row['exit_ge6'] for row in closed)/count if count else None,
            'mean_gross_return': statistics.mean(values) if values else None,
            'worst_gross_return': min(values) if values else None,
            'average_winning_return': statistics.mean(winners) if winners else None,
            'average_nonwinning_return': statistics.mean(losers) if losers else None,
            'coverage_fraction_closed': count/len(rows) if rows else None,
            'reference_profit_fraction_including_unknown_as_not_won': wins/(count+len(unknown)) if count+len(unknown) else None,
            'bootstrap_interval': None, 'confidence_status': 'insufficient_independent_future_dates',
            'actual_win_rate': None, 'formal_trade_ready': False,
            'execution_win_rate_80_validated': False}
        for component in ('D1_after_buy_return','overnight_return','D2_after_open_return'):
            component_values=[row[component] for row in closed if row.get(component) is not None]
            summary['mean_'+component]=statistics.mean(component_values) if component_values else None
        if count >= 40 and len(dates) >= 20:
            groups = defaultdict(list)
            for row in closed:
                groups[row['entry_date']].append(row['gross_return'])
            rng = random.Random(RULES['bootstrap']['seed'])
            intervals=[]
            for moving in (False,True):
                means,rates=[],[]
                for _ in range(2000):
                    if moving:
                        chosen=[]
                        while len(chosen)<len(dates):
                            start=rng.randrange(max(1,len(dates)-4))
                            chosen.extend(dates[start:start+5])
                        chosen=chosen[:len(dates)]
                    else:
                        chosen=rng.choices(dates,k=len(dates))
                    drawn=[value for day in chosen for value in groups[day]]
                    means.append(statistics.mean(drawn))
                    rates.append(sum(value>1e-12 for value in drawn)/len(drawn))
                tail=int(2000*.05/(2*6))
                intervals.append({'gross_mean':[sorted(means)[tail],sorted(means)[-tail-1]],
                    'reference_profit_rate':[sorted(rates)[tail],sorted(rates)[-tail-1]]})
            summary['bootstrap_interval'] = {'method': 'entry_date_cluster_and_moving5_Bonferroni_six_approximate_envelope',
                **{key:[min(part[key][0] for part in intervals),max(part[key][1] for part in intervals)]
                   for key in ('gross_mean','reference_profit_rate')},
                'unknowns_excluded_from_interval_count': len(unknown),
                'stationarity_assumed': True, 'serial_day_dependence_not_fully_resolved': True,
                'is_finite_sample_guarantee': False}
            summary['confidence_status'] = 'approximate_interval_only_no_activation'
        output[arm] = summary
    return output


def paired_timing(records):
    """Matched reference observations, with unmatched cases explicitly counted."""
    result={}
    for group in ('base','confluence'):
        pairs=defaultdict(dict)
        for row in records:
            if row['arm'] in (group+'_0940',group+'_1440'):
                pairs[(row['cohort_id'],row['symbol'])][row['arm']]=row
        differences=[];win_changes=[]
        for pair in pairs.values():
            early,late=pair.get(group+'_0940'),pair.get(group+'_1440')
            if early and late and early['status']==late['status']=='reference_closed':
                differences.append(late['gross_return']-early['gross_return'])
                win_changes.append(int(late['gross_return']>1e-12)-int(early['gross_return']>1e-12))
        result[group]={'both_closed':len(differences),'unmatched_or_pending':len(pairs)-len(differences),
            'mean_return_1440_minus_0940':statistics.mean(differences) if differences else None,
            'profit_fraction_1440_minus_0940':statistics.mean(win_changes) if win_changes else None,
            'conditional_on_both_reference_entries_and_exits':True,
            'is_population_causal_effect':False,'selects_policy':False}
    return result


def advance(study, previous_study, prior_publication_eligible, evidence, db, cutoff):
    """Keep the earliest timely publication per target; never rewrite a cohort."""
    value = deepcopy(study)
    prior = previous_study if isinstance(previous_study, dict) else {}
    if prior and (prior.get('schema') != SCHEMA or prior.get('protocol_sha256') != PROTOCOL_SHA256):
        # A changed experiment cannot pool or silently replay older rules.
        prior = {}
        value['previous_protocol_not_pooled'] = True
    cohorts = deepcopy(prior.get('tracked_cohorts', []))
    old = prior.get('current_cohort') or {}
    known = {row['target_trade_date'] for row in cohorts}
    if (prior_publication_eligible and old.get('forward_eligible') is True
            and old.get('target_trade_date') not in known):
        cohorts.append(deepcopy(old))
    if len({row['target_trade_date'] for row in cohorts}) != len(cohorts):
        raise ValueError('duplicate target cohorts in forward registry')
    first = next((row for row in cohorts if row['target_trade_date'] == value['target_trade_date']), None)
    if first:
        value['current_cohort'] = deepcopy(first)
        value['earliest_current_cohort_preserved'] = True
    records = []
    for cohort in cohorts:
        records.extend(evaluate(cohort, evidence, db, cutoff, prior.get('records', [])))
    value.update(tracked_cohorts=cohorts, records=records, summary=summarize(records), paired_timing=paired_timing(records),
        tracked_dates=len(cohorts), earliest_forecast_per_target_session=True,
        updated_at=instant(cutoff).isoformat())
    return value
