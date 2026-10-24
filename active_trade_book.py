"""Independent reference account and optional live evidence review; no orders.

The account records vendor price events from the active rule, never the user's
broker positions. Actual wins require actual trade receipts, which are absent.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import math
import statistics

from nextday_minutes import TZ, instant, number
from nextday_forward import expected_ends, minute_index, trigger, _daily, _inside_daily
from trade_calendar import adjacent

SCHEMA='active-trade-reference-book-v1'
ACTIVE_SCHEMA='active-tplus1-trade-system-v1'
OPEN_STATES=('pending_entry','pending_exit','unknown_entry','unknown_exit')


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()


def target_price(buy):
    return float((Decimal(str(buy))*Decimal('1.03')).quantize(Decimal('.01'),rounding=ROUND_CEILING))


def review_entry(plan,bars,quote,decision_time):
    """Live inputs must include their original known times and official status.

    Unlike after-close price-event replay, these inputs cannot use a subsequently
    observed minute or pretend that a qualifying quote establishes a fill.
    """
    result={'schema':'active-manual-entry-review-v1','system_enabled':True,
        'symbol':plan.get('symbol'),'active_plan_id':plan.get('active_plan_id'),
        'status':'awaiting_manual_confirmation','conditions_met':False,
        'order_execution_enabled':False,'actual_fill_verified':False,'orders_submitted':False,'reasons':[]}
    def reject(reason):
        result['reasons'].append(reason);return result
    from security_selection import normalized_name
    name = normalized_name(plan.get('name'))
    if str(plan.get('symbol','')).startswith('30') or 'ST' in name or '退' in name:
        return reject('excluded_chinext_ST_or_delisting_name')
    try:
        now=instant(decision_time);frozen=instant(plan['generated_at'])
        day=plan['target_trade_date'];decision=datetime.combine(datetime.fromisoformat(day).date(),time(14,40),TZ)
        if (now.date().isoformat()!=day or not decision<=now<=decision+timedelta(minutes=5)
                or frozen>=decision.replace(hour=9,minute=15)):
            return reject('outside_frozen_late_entry_window')
        if not plan.get('active_plan_id') or not isinstance(bars,list):return reject('frozen_plan_or_minutes_missing')
        index={}
        for row in bars:
            if not isinstance(row,dict):return reject('invalid_live_minutes')
            end=instant(row['end_at']);known=instant(row['observed_at'])
            if row.get('symbol')!=plan['symbol'] or row.get('complete') is not True or not end<=known<=now:
                return reject('live_minute_not_complete_or_known_at_decision')
            if end<=decision:
                if end.isoformat() in index:return reject('duplicate_live_minute')
                from nextday_forward import _valid_minute
                if not _valid_minute(row,day,now):return reject('invalid_live_minute_prices')
                index[end.isoformat()]=row
        ends=expected_ends(day,decision)
        if not all(stamp in index for stamp in ends):return reject('continuous_completed_prefix_missing')
        prefix=[index[stamp] for stamp in ends]
        if not trigger(prefix,'confluence_late_confirmation',plan['base_close']):return reject('late_confirmation_false')
        if not isinstance(quote,dict):return reject('fresh_broker_quote_missing')
        exchange,received=instant(quote['exchange_timestamp']),instant(quote['received_at'])
        if (quote.get('symbol')!=plan['symbol'] or not decision<=exchange<=received<=now
                or (now-exchange).total_seconds()>60):return reject('quote_stale_or_wrong_session')
        status,limits=quote.get('status'),quote.get('limits')
        if (not isinstance(status,dict) or not isinstance(limits,dict)
                or status.get('verified') is not True or status.get('is_st') is not False
                or status.get('suspended') is not False or status.get('delisting') is not False
                or status.get('trading_status')!='normal' or limits.get('official_verified') is not True):
            return reject('broker_status_or_official_limits_unverified')
        last,ask,volume,previous,upper,lower=[number(value) for value in (
            quote.get('last'),quote.get('ask1'),quote.get('ask1_volume'),quote.get('prev_close'),
            limits.get('limit_up'),limits.get('limit_down'))]
        if any(value is None or value<=0 for value in (last,ask,volume,previous,upper,lower)):
            return reject('invalid_visible_quote')
        quantity=number(plan.get('frozen_quantity_shares'))
        if (quantity is None or quantity<=0 or quantity%100 or
                (str(plan['symbol']).startswith(('688','689')) and quantity<200) or
                not 0<plan['structural_stop']<plan['frozen_entry_floor']<=plan['frozen_entry_cap']):
            return reject('invalid_frozen_allocation')
        signal_cap=float((Decimal(str(prefix[-1]['close']))*Decimal('1.003')).quantize(Decimal('.01'),rounding=ROUND_FLOOR))
        cap=min(plan['frozen_entry_cap'],signal_cap)
        if (abs(previous-plan['base_close'])>.011 or not lower<last<=ask<=upper-.02+1e-9
                or not plan['frozen_entry_floor']<=last<=ask<=cap
                or ask<=plan['structural_stop']):return reject('quote_outside_frozen_entry_limits')
        result.update(status='conditions_met_manual_execution',conditions_met=True,
            reference_ask=ask,maximum_buy_price=cap,reference_quantity=plan['frozen_quantity_shares'],
            confirmation_at=decision.isoformat(),quote_at=exchange.isoformat())
        return result
    except (KeyError,TypeError,ValueError,OverflowError):
        return reject('incomplete_or_invalid_live_evidence')


def evaluate(plan,evidence,db,cutoff,previous=None):
    """Reference buys/next-minute exits. User orders are never inferred."""
    now=instant(cutoff)
    row={'active_plan_id':plan['active_plan_id'],'symbol':plan['symbol'],
        'entry_date':plan['target_trade_date'],'exit_date':plan['D2_trade_date'],
        'frozen_at':plan['generated_at'],'frozen_entry_floor':plan['frozen_entry_floor'],
        'frozen_entry_cap':plan['frozen_entry_cap'],'structural_stop':plan['structural_stop'],
        'quantity':plan['frozen_quantity_shares'],'reserved_cash_cny':plan['frozen_reserved_cash_cny'],
        'sector_identity':plan['sector_identity'],'status':'pending_entry',
        'gross_return':None,'gross_pnl_reference':None,'actual_fill_verified':False,
        'actual_win_rate':None,'orders_submitted':False,'costs_included':False}
    if previous:
        for key in ('active_plan_id','symbol','entry_date','exit_date','frozen_at',
                'frozen_entry_floor','frozen_entry_cap','structural_stop','quantity','reserved_cash_cny'):
            if previous.get(key)!=row[key]:raise ValueError('active plan identity changed')
        row=deepcopy(previous)
    if row['status'] in ('reference_closed','no_signal','price_outside_bounds'):return row
    if (adjacent(plan['base_trade_date'],1)!=row['entry_date']
            or adjacent(row['entry_date'],1)!=row['exit_date']
            or instant(row['frozen_at'])>=datetime.combine(datetime.fromisoformat(row['entry_date']).date(),time(9,15),TZ)):
        raise ValueError('active plan must be frozen for adjacent future sessions')
    decision=datetime.combine(datetime.fromisoformat(row['entry_date']).date(),time(14,40),TZ)
    if 'reference_buy_price' not in row:
        if now<decision+timedelta(minutes=1):return row
        index,origin=minute_index(evidence,row['symbol'],row['entry_date'],now)
        evidence_deadline=datetime.combine(datetime.fromisoformat(row['exit_date']).date(),time(9,15),TZ)
        ends=expected_ends(row['entry_date'],decision)
        entry=index.get((decision+timedelta(minutes=1)).isoformat())
        if origin is None or instant(origin['observed_at'])>=evidence_deadline:
            row.update(status='unknown_entry',reason='entry_evidence_not_known_before_D2_deadline');return row
        daily=_daily(db,row['symbol'],row['entry_date'],now)
        if (entry is None or not all(stamp in index for stamp in ends) or daily is None
                or any(not _inside_daily(index[stamp],daily) for stamp in ends)
                or not _inside_daily(entry,daily) or entry['volume']<=0
                or abs(daily['close']/plan['base_close']-1)>.30):
            row.update(status='unknown_entry',reason='entry_minutes_or_daily_crosscheck_missing');return row
        prefix=[index[stamp] for stamp in ends]
        if not trigger(prefix,'confluence_late_confirmation',plan['base_close']):
            row.update(status='no_signal',reason='late_confirmation_false',entry_evidence=origin);return row
        cap=min(plan['frozen_entry_cap'],float((Decimal(str(prefix[-1]['close']))*Decimal('1.003')).quantize(
            Decimal('.01'),rounding=ROUND_FLOOR)))
        buy=entry['open']
        if not plan['frozen_entry_floor']<=buy<=cap or buy<=plan['structural_stop']:
            row.update(status='price_outside_bounds',reason='reference_buy_outside_price_limits',
                observed_reference_open=buy,entry_evidence=origin);return row
        row.update(status='pending_exit',reference_buy_price=buy,reference_entry_at=decision.isoformat(),
            entry_evidence=origin,D1_close=daily['close'],D1_after_buy_return=daily['close']/buy-1,
            D1_structure_failed=daily['close']<=plan['structural_stop'],frozen_profit_target=target_price(buy))
    exit_day=row['exit_date']
    exit_clock=datetime.combine(datetime.fromisoformat(exit_day).date(),time(14,50),TZ)
    if now<datetime.combine(exit_clock.date(),time(9,31),TZ):return row
    index,origin=minute_index(evidence,row['symbol'],exit_day,now)
    daily=_daily(db,row['symbol'],exit_day,now)
    # Every D2 minute up to the fixed time must be observable, so a missing
    # earlier stop cannot turn a later profit into a clean win.
    ends=expected_ends(exit_day,exit_clock+timedelta(minutes=1))
    if (daily is None or not all(stamp in index for stamp in ends)
            or any(not _inside_daily(index[stamp],daily) for stamp in ends)
            or abs(daily['close']/row['D1_close']-1)>.30 or origin is None):
        row.update(status='pending_exit' if now<exit_clock else 'unknown_exit',
            reason='D2_contiguous_minutes_or_daily_crosscheck_missing');return row
    bars=[index[stamp] for stamp in ends]
    first=next((bar for bar in bars if bar['volume']>0),None)
    if first is None:
        row.update(status='unknown_exit',reason='D2_no_tradable_reference_minute');return row
    stop=plan['structural_stop'];target=row['frozen_profit_target']
    chosen=None;reason=None
    if row['D1_structure_failed'] or first['open']<=stop or first['open']>=target:
        chosen=first;reason='D1_failure_or_D2_gap_first_minute_open_reference'
    else:
        for current,following in zip(bars[:-1],bars[1:]):
            if instant(following['start_at'])>=exit_clock:break
            if current['volume']<=0:continue
            stop_hit=current['low']<=stop;target_hit=current['high']>=target
            if stop_hit or target_hit:
                # `bars` already follows expected_ends, including 11:30->13:00.
                if following['volume']<=0:
                    row.update(status='unknown_exit',reason='trigger_next_minute_missing_or_untradable');return row
                chosen=following;reason='completed_minute_stop_next_open' if stop_hit else 'completed_minute_target_next_open'
                row['same_bar_stop_and_target']=stop_hit and target_hit
                break
    if chosen is None:
        chosen=bars[-1];reason='D2_fixed_1450_reference_exit'
    if chosen['volume']<=0:
        row.update(status='unknown_exit',reason='exit_reference_has_no_volume');return row
    sell=chosen['open'];buy=row['reference_buy_price'];gross=sell/buy-1
    row.update(status='reference_closed',reason=reason,reference_sell_price=sell,
        reference_exit_at=chosen['start_at'],exit_evidence=origin,gross_return=gross,
        gross_pnl_reference=row['quantity']*(sell-buy),exit_ge3=gross>=.03-1e-12,
        exit_ge6=gross>=.06-1e-12,overnight_return=daily['open']/row['D1_close']-1,
        D2_after_open_return=sell/daily['open']-1)
    return row


def account(records,initial=30000):
    closed=[row for row in records if row['status']=='reference_closed']
    active=[row for row in records if row['status'] in OPEN_STATES]
    realized=sum(row['gross_pnl_reference'] for row in closed)
    reserved=sum(row['reserved_cash_cny'] for row in active)
    cash=initial+realized-reserved
    if cash<-.001:raise ValueError('active reference account spent unavailable cash')
    values=[row['gross_return'] for row in closed]
    wins=sum(value>1e-12 for value in values)
    losses=[value for value in values if value<0]
    gains=[value for value in values if value>0]
    interval=None
    if values:
        n=len(values);p=wins/n;z=1.959963984540054
        center=(p+z*z/(2*n))/(1+z*z/n)
        half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
        interval=[max(0,center-half),min(1,center+half)]
    uncertain=sum(row['status'] in ('unknown_entry','unknown_exit') for row in records)
    return {'initial_capital_cny':initial,'available_cash_cny':round(max(0,cash),2),
        'reserved_cash_cny':round(reserved,2),'realized_reference_pnl_cny':round(realized,2),
        'open_positions':len(active),'reference_closed':len(closed),
        'reference_profit_rate':wins/len(values) if values else None,
        'reference_profit_rate_wilson95':interval,
        'reference_profit_fraction_unknown_as_not_won':wins/(len(values)+uncertain) if values or uncertain else None,
        'mean_gain_reference':statistics.mean(gains) if gains else None,
        'mean_loss_reference':statistics.mean(losses) if losses else None,
        'mean_reference_return':statistics.mean(values) if values else None,
        'reference_ge3_rate':sum(row['exit_ge3'] for row in closed)/len(closed) if closed else None,
        'reference_ge6_rate':sum(row['exit_ge6'] for row in closed)/len(closed) if closed else None,
        'unknown_positions':sum(row['status'].startswith('unknown_') for row in active),
        'status_counts':dict(Counter(row['status'] for row in records)),
        'actual_user_positions_known':False,'actual_win_rate':None,'is_actual_broker_account':False}


def roll(previous_study,admitted,evidence,db,cutoff,protocol_sha256):
    """Admit first actually published plans; preserve their prices and budget."""
    # A verified legacy publication can predate the active system entirely.
    # Its publication evidence must not manufacture an empty active cohort.
    if previous_study is None:
        previous={}
    elif isinstance(previous_study,dict):
        previous=previous_study
    else:
        raise ValueError('previous active study must be an object or absent')
    previous_policy = previous.get('protocol_sha256')
    migrating = bool(previous) and previous_policy != protocol_sha256
    if migrating:
        from active_trade_system import compatible_policy_migration
        if not compatible_policy_migration(previous_policy, protocol_sha256):
            raise ValueError('active policy changed; explicit account migration required, balances cannot reset')
    if previous:
        if previous.get('schema')!=ACTIVE_SCHEMA:
            raise ValueError('previous active study schema mismatch')
        _validate_frozen_decision(previous)
    book=deepcopy(previous.get('reference_book',{}))
    if not isinstance(book,dict):
        raise ValueError('previous active reference book must be an object')
    if book and (book.get('schema')!=SCHEMA or book.get('protocol_sha256')!=previous_policy
            or not isinstance(book.get('frozen_plan_dates'),list)
            or not isinstance(book.get('records'),list)):
        raise ValueError('previous active reference book identity or ledger is malformed')
    cohorts=book.get('frozen_plan_dates',[])
    date_policies=deepcopy(book.get('frozen_date_policy_ids',{}))
    if not isinstance(date_policies,dict):
        raise ValueError('active frozen policy identities must be an object')
    for item in cohorts:
        date_policies.setdefault(item['target_trade_date'],previous_policy)
    lineage=list(book.get('policy_lineage',[]))
    if previous_policy and previous_policy not in lineage:
        lineage.append(previous_policy)
    if protocol_sha256 not in lineage:
        lineage.append(protocol_sha256)
    for cohort in cohorts:
        _validate_frozen_decision(cohort)
    known={item['target_trade_date'] for item in cohorts}
    if admitted and previous and previous['target_trade_date'] not in known:
        target=datetime.fromisoformat(previous['target_trade_date']).date()
        if instant(previous['generated_at'])>=datetime.combine(target,time(9,15),TZ):
            raise ValueError('active admission must precede target session freeze deadline')
        decision_policy=previous.get('plan_policy_id') or previous_policy
        if decision_policy != previous_policy:
            from active_trade_system import compatible_policy_migration
            if not compatible_policy_migration(decision_policy,previous_policy):
                raise ValueError('frozen decision policy is not a supported carried legacy policy')
        date_policies[previous['target_trade_date']]=decision_policy
        cohorts.append({'target_trade_date':previous['target_trade_date'],
            'generated_at':previous['generated_at'],'plans':deepcopy(previous['plans'])})
    if len({item['target_trade_date'] for item in cohorts})!=len(cohorts):
        raise ValueError('duplicate active plan target dates')
    prior={row['active_plan_id']:row for row in book.get('records',[])}
    records=[]
    for cohort in cohorts:
        for plan in cohort['plans']:
            records.append(evaluate(plan,evidence,db,cutoff,prior.get(plan['active_plan_id'])))
    summary=account(records)
    active=[row for row in records if row['status'] in OPEN_STATES]
    return {'schema':SCHEMA,'protocol_sha256':protocol_sha256,'updated_at':instant(cutoff).isoformat(),
        'frozen_plan_dates':cohorts,'records':records,'account':summary,
        'frozen_date_policy_ids':date_policies,'policy_lineage':lineage,
        'selection_upgrade_migration': ({'from':previous_policy,'to':protocol_sha256,
            'account_reset':False,'frozen_decisions_preserved':True,
            'legacy_entry_exit_and_risk_rules_unchanged':True} if migrating else
            deepcopy(book.get('selection_upgrade_migration'))),
        'occupied_symbols':[row['symbol'] for row in active],
        'occupied_sectors':[row['sector_identity'] for row in active],
        'actual_win_rate':None,'order_execution_enabled':False}


def _validate_frozen_decision(value):
    """Distinguish an explicit empty decision from missing active state."""
    if not isinstance(value,dict):
        raise ValueError('active frozen decision must be an object')
    target=value.get('target_trade_date')
    try:
        day=datetime.fromisoformat(target).date()
        if day.isoformat()!=target:
            raise ValueError('noncanonical date')
        instant(value['generated_at'])
    except (KeyError,TypeError,ValueError,OverflowError) as error:
        raise ValueError('active frozen decision date or timestamp is malformed') from error
    if not isinstance(value.get('plans'),list):
        raise ValueError('active frozen decision requires an explicit plans list')
    required=('active_plan_id','symbol','base_trade_date','target_trade_date','D2_trade_date',
        'generated_at','frozen_entry_floor','frozen_entry_cap','structural_stop',
        'frozen_quantity_shares','frozen_reserved_cash_cny','sector_identity','base_close')
    for plan in value['plans']:
        if (not isinstance(plan,dict) or any(key not in plan for key in required)
                or plan['target_trade_date']!=target):
            raise ValueError('active frozen plan is malformed or targets a different session')
