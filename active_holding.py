"""V3 frozen one/two-session minute-reference lifecycle; never broker fills."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_FLOOR

import active_regime as regime
from nextday_minutes import TZ, instant
from nextday_forward import expected_ends, minute_index, _daily, _inside_daily
from trade_calendar import adjacent


def evaluate(plan, evidence, db, cutoff, previous=None):
    from active_trade_book import digest, target_price
    now = instant(cutoff)
    first_sell, deadline = regime.schedule(plan)
    if (adjacent(plan['base_trade_date'],1) != plan['target_trade_date'] or
            instant(plan['generated_at']) >= datetime.combine(datetime.fromisoformat(plan['target_trade_date']).date(),time(9,15),TZ)):
        raise ValueError('v3 plan must be frozen before its adjacent entry session')
    row = {'active_plan_id':plan['active_plan_id'], 'symbol':plan['symbol'],
        'entry_date':plan['target_trade_date'], 'exit_date':deadline,
        'first_sell_trade_date':first_sell, 'setup_route':plan['setup_route'],
        'holding_policy_version':3, 'max_hold_after_entry_sessions':plan['max_hold_after_entry_sessions'],
        'frozen_at':plan['generated_at'], 'frozen_entry_floor':plan['frozen_entry_floor'],
        'frozen_entry_cap':plan['frozen_entry_cap'], 'structural_stop':plan['structural_stop'],
        'quantity':plan['frozen_quantity_shares'], 'reserved_cash_cny':plan['frozen_reserved_cash_cny'],
        'sector_identity':plan['sector_identity'], 'status':'pending_entry',
        'gross_return':None, 'gross_pnl_reference':None, 'actual_fill_verified':False,
        'actual_win_rate':None, 'orders_submitted':False, 'costs_included':False}
    if previous:
        for key in row:
            if key not in ('status','gross_return','gross_pnl_reference') and previous.get(key) != row[key]:
                raise ValueError('v3 frozen plan, allocation or holding deadline changed')
        row = deepcopy(previous)
    if row['status'] in ('reference_closed','no_signal','price_outside_bounds'):
        return row
    decision = datetime.combine(datetime.fromisoformat(row['entry_date']).date(),time(14,40),TZ)
    if 'reference_buy_price' not in row:
        if now < decision + timedelta(minutes=1):
            return row
        index, origin = minute_index(evidence,row['symbol'],row['entry_date'],now)
        # Two-session authorization never grants an extra day to backfill entry evidence.
        limit = datetime.combine(datetime.fromisoformat(first_sell).date(),time(9,15),TZ)
        ends = expected_ends(row['entry_date'],decision)
        entry = index.get((decision + timedelta(minutes=1)).isoformat())
        daily = _daily(db,row['symbol'],row['entry_date'],now)
        if origin is None or instant(origin['observed_at']) >= limit:
            row.update(status='unknown_entry',reason='entry_evidence_not_known_before_first_sell_day_deadline')
            return row
        if (entry is None or not all(stamp in index for stamp in ends) or daily is None
                or any(not _inside_daily(index[stamp],daily) for stamp in ends)
                or not _inside_daily(entry,daily) or entry['volume'] <= 0
                or abs(daily['close']/plan['base_close']-1) > .30):
            row.update(status='unknown_entry',reason='entry_minutes_or_daily_crosscheck_missing')
            return row
        prefix = [index[stamp] for stamp in ends]
        if not regime.entry_confirmation(prefix,plan['base_close'],plan['setup_route']):
            row.update(status='no_signal',reason='v3_individual_reclaim_or_route_confirmation_false',entry_evidence=origin)
            return row
        cap = min(plan['frozen_entry_cap'],float((Decimal(str(prefix[-1]['close']))*Decimal('1.003')).quantize(Decimal('.01'),rounding=ROUND_FLOOR)))
        buy = entry['open']
        if not max(plan['base_close'],plan['frozen_entry_floor']) <= buy <= cap or buy <= plan['structural_stop']:
            row.update(status='price_outside_bounds',reason='reference_buy_outside_v3_reclaimed_entry_limits',observed_reference_open=buy,entry_evidence=origin)
            return row
        row.update(status='pending_exit',reference_buy_price=buy,reference_entry_at=decision.isoformat(),
            entry_evidence=origin,D1_close=daily['close'],D1_after_buy_return=daily['close']/buy-1,
            D1_structure_failed=daily['close'] <= plan['structural_stop'],frozen_profit_target=target_price(buy),
            completed_hold_sessions={})
    days = [first_sell] if deadline == first_sell else [first_sell,deadline]
    checked = row.setdefault('completed_hold_sessions',{})
    last_close = row['D1_close']
    carry_trigger = row['D1_structure_failed']
    for day in days:
        old = checked.get(day)
        if old:
            if old.get('continued') is not True or not old.get('full_session_monitored'):
                raise ValueError('invalid persisted holding decision')
            last_close,carry_trigger = old['day_close'],old['next_open_trigger']
            continue
        fixed = datetime.combine(datetime.fromisoformat(day).date(),time(14,50),TZ)
        if now < fixed.replace(hour=9,minute=31):
            row.update(status='pending_exit',reason='waiting_for_first_tradable_sell_session')
            return row
        index, origin = minute_index(evidence,row['symbol'],day,now)
        daily = _daily(db,row['symbol'],day,now)
        ends = expected_ends(day,fixed + timedelta(minutes=1))
        if (daily is None or origin is None or not all(stamp in index for stamp in ends)
                or any(not _inside_daily(index[stamp],daily) for stamp in ends)
                or abs(daily['close']/last_close-1) > .30):
            row.update(status='pending_exit' if now < fixed else 'unknown_exit',reason='holding_session_continuous_minutes_or_daily_crosscheck_missing')
            return row
        bars = [index[stamp] for stamp in ends]
        frozen_decision = row.get('continuation_decisions',{}).get(day)
        if frozen_decision and (frozen_decision.get('at') != fixed.isoformat()
                or frozen_decision.get('completed_prefix_sha256') != digest(bars[:-1])):
            row.update(status='unknown_exit',reason='previously_frozen_continuation_prefix_changed')
            return row
        first = next((bar for bar in bars if bar['volume'] > 0),None)
        if first is None:
            row.update(status='unknown_exit',reason='holding_session_has_no_tradable_reference_minute')
            return row
        stop,target = plan['structural_stop'],row['frozen_profit_target']
        chosen,reason = None,None
        if carry_trigger or first['open'] <= stop or first['open'] >= target:
            chosen,reason = first,'prior_structure_or_closing_trigger_or_gap_next_open'
        else:
            for current,following in zip(bars[:-1],bars[1:]):
                if instant(following['start_at']) > fixed:
                    break
                if current['volume'] > 0 and (current['low'] <= stop or current['high'] >= target):
                    chosen = following
                    reason = 'completed_minute_stop_next_open' if current['low'] <= stop else 'completed_minute_target_next_open'
                    row['same_bar_stop_and_target'] = current['low'] <= stop and current['high'] >= target
                    break
        if chosen is None:
            prefix = bars[:-1]  # Last prefix bar ENDS 14:50. Never use the 14:50 bar's close.
            keep = day != deadline and regime.continuation(prefix,row['reference_buy_price'])
            decision_record = {'at':fixed.isoformat(), 'continued':keep,
                'completed_prefix_sha256':digest(prefix), 'uses_final_daily_close':False,
                'is_actual_manual_execution':False}
            row.setdefault('continuation_decisions',{})[day] = decision_record
            if day == first_sell:
                row['first_sell_continuation_decision'] = decision_record
            if not keep:
                chosen = bars[-1]
                reason = 'frozen_final_deadline_1450' if day == deadline else 'first_sell_day_continuation_failed_1450'
            else:
                # A pre-authorized continuation still has stop/target monitoring
                # after 14:50; a missing late bar cannot turn D3 into a clean win.
                complete_ends = expected_ends(day,fixed.replace(hour=15,minute=0))
                if (not all(stamp in index for stamp in complete_ends)
                        or any(not _inside_daily(index[stamp],daily) for stamp in complete_ends)):
                    row.update(status='pending_exit' if now < fixed.replace(hour=15,minute=0) else 'unknown_exit',reason='continued_first_sell_session_close_monitoring_missing')
                    return row
                full = [index[stamp] for stamp in complete_ends]
                for current,following in zip(full[:-1],full[1:]):
                    if instant(current['end_at']) < fixed:
                        continue
                    if current['volume'] > 0 and (current['low'] <= stop or current['high'] >= target):
                        chosen = following
                        reason = 'continued_session_stop_next_open' if current['low'] <= stop else 'continued_session_target_next_open'
                        row['same_bar_stop_and_target'] = current['low'] <= stop and current['high'] >= target
                        break
                if chosen is None:
                    last = full[-1]
                    carry_trigger = bool(daily['close'] <= stop or last['volume'] > 0 and (last['low'] <= stop or last['high'] >= target))
                    checked[day] = {'continued':True,'full_session_monitored':True,
                        'day_close':daily['close'],'next_open_trigger':carry_trigger,
                        'continuation_decision':decision_record,'evidence':origin}
                    last_close = daily['close']
                    continue
        if chosen['volume'] <= 0:
            row.update(status='unknown_exit',reason='trigger_or_deadline_reference_has_no_volume')
            return row
        sell,buy = chosen['open'],row['reference_buy_price']
        gross = sell/buy-1
        row.update(status='reference_closed',reason=reason,reference_sell_price=sell,
            reference_exit_at=chosen['start_at'],exit_evidence=origin,gross_return=gross,
            gross_pnl_reference=row['quantity']*(sell-buy),exit_ge3=gross >= .03-1e-12,
            exit_ge6=gross >= .06-1e-12,actual_exit_trade_date=day,
            holding_sessions_after_entry=1 if day == first_sell else 2)
        return row
    row.update(status='pending_exit',reason='preauthorized_second_sell_session_pending')
    return row
