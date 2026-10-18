"""Causal completed-minute opening confirmation. Conditions never prove a fill."""
import datetime as dt
import math
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Shanghai')
MODES=('opening_breakout','breakout_retest')


def instant(value):
    try:
        parsed=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return parsed.astimezone(TZ) if parsed.tzinfo else None
    except (ValueError,TypeError):return None


def number(value):
    try:
        value=float(value)
        return value if math.isfinite(value) else None
    except (ValueError,TypeError):return None


def review(plan,bars,quote,decision_time,*,mode):
    result={'schema':'pa-opening-confirmation-v1','mode':mode,'symbol':plan.get('symbol'),
            'status':'waiting_for_verified_evidence','conditions_met_for_research':False,
            'actual_fill_verified':False,'formal_trade_ready':False,
            'minute_backtest_completed':False,'orders_submitted':False,'reasons':[]}
    def reject(reason):
        result['reasons'].append(reason);return result
    if mode not in MODES:return reject('unknown_entry_mode')
    now=instant(decision_time);frozen=instant(plan.get('generated_at'))
    if not now or not frozen:return reject('unknown_or_naive_decision_or_freeze_time')
    day=plan.get('target_trade_date')
    if now.date().isoformat()!=day or not dt.time(9,36)<=now.time()<=dt.time(9,45):
        return reject('outside_target_opening_window')
    opening=now.replace(hour=9,minute=30,second=0,microsecond=0)
    if frozen>=opening:return reject('plan_not_frozen_before_target_open')
    if plan.get('eligible') is not True or not plan.get('pa_plan_id'):
        return reject('frozen_structural_plan_missing')
    if not bars:return reject('completed_minute_bars_unavailable')
    ordered=[];last_end=None
    for bar in bars:
        start,end,known=(instant(bar.get(key)) for key in ('start_at','end_at','observed_at'))
        values={key:number(bar.get(key)) for key in ('open','high','low','close','volume')}
        if (bar.get('symbol')!=plan.get('symbol') or bar.get('complete') is not True
                or not all((start,end,known)) or start<opening or end-start!=dt.timedelta(minutes=1)
                or not start<end<=known<=now or (last_end and start!=last_end)
                or any(value is None or value<=0 for value in values.values())
                or values['high']<max(values['open'],values['close'],values['low'])
                or values['low']>min(values['open'],values['close'],values['high'])):
            return reject('minute_bars_nonfinal_missing_out_of_order_or_future')
        ordered.append({**values,'end':end});last_end=end
    if len(ordered)<6 or instant(bars[0]['start_at'])!=opening:
        return reject('opening_first_five_complete_minutes_missing')
    if (now-ordered[-1]['end']).total_seconds()>120:
        return reject('minute_confirmation_stale')
    if not isinstance(quote,dict):return reject('official_fresh_quote_missing')
    exchange,received=(instant(quote.get(key)) for key in ('exchange_timestamp','received_at'))
    status=quote.get('status') or {};limits=quote.get('limits') or {}
    if (quote.get('symbol')!=plan.get('symbol') or not exchange or not received
            or not ordered[-1]['end']<=exchange<=received<=now
            or (now-exchange).total_seconds()>60):return reject('quote_date_time_or_freshness_invalid')
    if (status.get('verified') is not True or status.get('is_st') is not False
            or status.get('suspended') is not False or status.get('delisting') is not False
            or status.get('trading_status')!='normal' or limits.get('official_verified') is not True):
        return reject('official_status_or_limits_unknown')
    last,ask,volume,upper,lower,previous=(number(value) for value in
        (quote.get('last'),quote.get('ask1'),quote.get('ask1_volume'),limits.get('limit_up'),
         limits.get('limit_down'),quote.get('prev_close')))
    if any(value is None or value<=0 for value in (last,ask,volume,upper,lower,previous)) or not lower<upper:
        return reject('invalid_visible_ask_or_limits')
    if abs(previous-plan['close'])>.011:return reject('quote_previous_close_differs_from_plan')
    if (not plan['frozen_entry_floor']<=last<=ask<=plan['entry_cap']
            or ask>upper-.02+1e-9 or last<=lower or last<=plan['structural_stop']):
        return reject('quote_outside_frozen_price_or_limit_constraints')
    level=max(row['high'] for row in ordered[:5]);recent=ordered[-1]
    def strong(bar,close_location):
        span=bar['high']-bar['low']
        return span>0 and bar['close']>bar['open'] and (bar['close']-bar['low'])/span>=close_location
    if mode=='opening_breakout':
        span=recent['high']-recent['low']
        passed=(strong(recent,.7) and recent['close']>=level+.01-1e-9
                and (recent['close']-recent['open'])/span>=.5)
    else:
        atr=number(plan.get('atr14'))
        if not atr or atr<=0:return reject('frozen_atr_for_retest_missing')
        earlier=ordered[5:-1]
        breaks=[index for index,bar in enumerate(earlier) if strong(bar,.7) and bar['close']>=level+.01-1e-9]
        passed=False
        if breaks:
            start=breaks[0]
            held=all(bar['close']>=level-.2*atr for bar in earlier[start:])
            passed=(held and level-.2*atr<=recent['low']<=level+.2*atr
                    and recent['close']>=level and strong(recent,.6))
    if not passed:return reject('no_completed_breakout_or_retest_confirmation')
    if ask<level:return reject('latest_quote_no_longer_above_opening_range')
    result.update(status='confirmed_conditions_research_only',conditions_met_for_research=True,
                  reference_price=ask,opening_range_high=level,confirmation_bar_end=recent['end'].isoformat(),
                  quantity_reference=plan['quantity_ref'],quote_time=exchange.isoformat())
    return result
