#!/usr/bin/env python3
"""Context -> structure -> frozen risk -> T+1 replay. Daily reference events only."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

import collector as c
import gross_swing_strategy as gross
import price_action_research as action
import probability_research as probability
from probability_panel import panel
from probability_validation import evaluate_trade_metrics
from net_profit_strategy import prediction_timing
from pa_trade_features import enrich
from pa_trade_engine import SETUPS, RULE_DEFINITIONS, make_plan, floor_cent

VERSION='pa-context-trade-research-v1'
PROTOCOL={
    'schema':VERSION,'rule_definitions':RULE_DEFINITIONS,'thresholds_are_project_hypotheses':True,
    'context':'bull_trend_range_or_unclear_at_T_close',
    'setups':list(SETUPS),'fixed_trials':3,'future_confirmed_pivots':False,
    'entry':'D1_open_reference_inside_T_frozen_floor_cap_and_above_structure',
    'D1_open_range':'T_close_minus_1_percent_to_frozen_cap_max_plus_2.5_percent',
    'entry_quantity':'T_frozen_min_cash_limit_and_150CNY_stressed_risk_budget_100_share_lots',
    'reference_capital_cny':30000,'maximum_positions':3,'position_cash_limit':10000,
    'structural_risk_pct':[.008,.03],'additional_gap_stress_pct':.02,
    'gap_stress_is_loss_guarantee':False,'same_open_savings_or_cancellations_reallocated':False,
    'D2_close_proceeds_usable_at_D2_open':False,'unknowns_reserve_capacity':True,
    'D1_sell_allowed':False,'D1_red_is_entry_filter':False,
    'D1_failure':'D1_close_below_T_frozen_structural_stop_or_breakout_close_below_T_frozen_breakout_level',
    'D1_failure_action':'D2_open_reference_exit_even_if_D2_later_recovers',
    'otherwise_exit':'D2_structure_stop_or_T_frozen_3_or_6_percent_target_else_D2_close',
    'D2_same_bar_ambiguity':'stop_first_with_target_first_sensitivity',
    'target_from':'actual_D1_reference_buy_not_T_close',
    'target_selection':'3_percent_if_room_and_RR_ge1.5_else_6_if_bull_room_and_RR_ge1.8',
    'opening_breakout_and_retest_modes':'require_fresh_completed_intraday_evidence_not_daily_replay',
    'breakeven_formula':'risk/(target+risk)','breakeven_is_predicted_probability':False,
    'costs_included':False,'fees_cny':0,'slippage_bps':0,
    'split':[.5,.15,.15,.2],'early_65_percent':'development_only_no_threshold_search',
    'purge':'prior_D3_outcome_before_next_signal_block',
    'selection':'selection_only_20_closed_10_dates_no_unknowns_positive_date_and_moving5_familywise_lower',
    'diagnostic_selects_rules':False,'historical_fundflow_used':False,
    'historical_status_limits_actions_verified':False,'all_old_history_previously_exposed':True,
    'formal_trade_ready':False,'actual_win_rate':None,'execution_win_rate_80_validated':False,
    'engine_source_sha256':hashlib.sha256((Path(__file__).parent/'pa_trade_engine.py').read_text(encoding='utf-8').encode()).hexdigest(),
    'feature_source_sha256':hashlib.sha256((Path(__file__).parent/'pa_trade_features.py').read_text(encoding='utf-8').encode()).hexdigest(),
}


def _number(value):
    try:
        value=float(value)
        return value if math.isfinite(value) else None
    except (TypeError,ValueError):return None


def plans(frame,setup):
    """Cheap necessary T-only checks precede the complete shared rule engine."""
    if setup=='breakout_followthrough':
        keep=(frame.previous_close>frame.breakout_level)&(frame.close>frame.open)&(frame.close>=frame.previous_close)
    elif setup=='trend_pullback_rejection':
        keep=(frame.close>frame.open)&(frame.previous_close<frame.previous_open)&(frame.lower_wick_fraction>=.25)
    elif setup=='range_failed_breakout':
        keep=(frame.low<frame.prior20_low)&(frame.close>frame.open)&(frame.close_location>=.75)
    else:raise ValueError('unknown structural setup')
    candidates=frame.loc[keep]
    columns=list(candidates.columns);result=[]
    for values in candidates.itertuples(index=False,name=None):
        source=dict(zip(columns,values));plan=make_plan(source,setup)
        if plan['eligible']:
            plan.update(symbol=source['symbol'],date=source['date'],close=float(source['close']),
                        breakout_level=float(source['breakout_level']),
                        rank_score=100*(.4*source['close_location']+.2*min(max(source['efficiency20'],0),1)
                            +.2*min(max((source['relative_return5']+.02)/.07,0),1)
                            +.2*min(plan['reward_risk']/3,1)))
            result.append({**source,'trade_plan':plan})
    return pd.DataFrame(result,columns=columns+['trade_plan']) if result else pd.DataFrame(columns=columns+['trade_plan'])


def exit_reference(row,plan,buy,*,fixed_exit=False):
    """D1 failure is a D2 exit instruction, never an ex-post deletion of entry."""
    if fixed_exit:
        result=gross.exit_outcome(buy,row.day2_open,row.day2_high,row.day2_low,row.day2_close,.03,.02)
        return {**result,'D1_structure_failed':False,'exit_policy':'fixed_3pct_2pct_control'}
    stop=plan['structural_stop'];close=_number(row.day1_close)
    failed=close is not None and (close<=stop or
        (plan['setup']=='breakout_followthrough' and close<plan['breakout_level']))
    if failed:
        sell=float(row.day2_open)
        return {'reference_sell_price':sell,'optimistic_sell_price':sell,
                'gross_return':sell/buy-1,'optimistic_gross_return':sell/buy-1,
                'ambiguous_same_bar':False,'exit_reason':'D1_failed_structure_exit_D2_open_reference',
                'D1_structure_failed':True,'target_price':math.ceil(round(buy*(1+plan['target_pct'])*100,10))/100,
                'stop_price':stop,'execution_verified':False,'exit_policy':'structure_then_target_or_time',
                'source':'daily_ohlc_execution_proxy','fees_cny':0,'slippage_bps':0}
    result=gross.exit_outcome(buy,row.day2_open,row.day2_high,row.day2_low,row.day2_close,
                              plan['target_pct'],(buy-stop)/buy)
    return {**result,'D1_structure_failed':False,'exit_policy':'structure_then_target_or_time'}


def simulate(frame,*,fixed_exit=False):
    """At most three frozen plans; cash cannot be spent using later-open outcomes."""
    cash=30000.;settled_pnl=0.;positions=[];records=[]
    def check():
        if cash<-1e-6 or not math.isclose(cash+sum(p['reserved_cash'] for p in positions),30000+settled_pnl,abs_tol=1e-5):
            raise ValueError('PA cash conservation violated')
    for signal,daily in frame.groupby('date',sort=True):
        entries=daily.entry_date.dropna();entry=str(entries.iloc[0]) if len(entries) else None
        remaining=[]
        for position in positions:
            if entry and position['status']=='closed' and position['exit_date']<entry:
                cash+=position['sell_cash'];settled_pnl+=position['gross_pnl']
            else:remaining.append(position)
        positions=remaining;check()
        daily=daily.assign(_rank=daily.trade_plan.map(lambda p:p['rank_score'])).sort_values(['_rank','symbol'],ascending=[False,True])
        planned=cash;selected=0
        for row in daily.itertuples():
            if selected>=3 or len(positions)>=3:break
            if any(position['symbol']==row.symbol for position in positions):continue
            plan=row.trade_plan;cap=float(plan['entry_cap'])
            quantity=min(plan['quantity_ref'],int(min(10000,planned)/cap//100)*100)
            if quantity<=0:continue
            selected+=1;frozen=quantity*cap;planned-=frozen
            buy=_number(row.reference_buy_price)
            record={'signal_date':str(signal),'entry_date':row.entry_date,'exit_date':row.outcome_date,
                'symbol':row.symbol,'setup':plan['setup'],'pa_plan_id':plan.get('pa_plan_id'),
                'frozen_buy_limit_cap':cap,'frozen_entry_floor':plan['frozen_entry_floor'],
                'frozen_structural_stop':plan['structural_stop'],'frozen_target_pct':plan['target_pct'],
                'frozen_order_quantity':quantity,'frozen_budget':min(10000,planned+frozen),
                'frozen_capital':frozen,'cash_before_entry':cash,'reference_buy_price':buy,
                'first_day_green':bool(row.first_day_green) if pd.notna(row.first_day_green) else None,
                'day2_close_up':bool(row.day2_close_up) if pd.notna(row.day2_close_up) else None,
                'day2_touch3':bool(row.day2_touch3) if pd.notna(row.day2_touch3) else None,
                'day2_touch6':bool(row.day2_touch6) if pd.notna(row.day2_touch6) else None,
                'strength3_label':int(row.strength3_label) if pd.notna(row.strength3_label) else None,
                'day3_date':row.day3_date,'execution_verified':False,'costs_included':False,
                'source':'daily_ohlc_execution_proxy','breakeven_probability':plan['breakeven_probability'],
                'stress_breakeven_probability':plan['stress_breakeven_probability']}
            if bool(row.entry_evidence_valid) and buy is not None and (buy<plan['frozen_entry_floor']-1e-9 or buy>cap+1e-9):
                record.update(status='no_fill',reason='opening_outside_T_frozen_structure_range',
                              gross_return=None,gross_pnl=None,reserved_cash=0.)
                records.append(record);continue
            known=bool(row.entry_evidence_valid) and not bool(row.entry_one_price) and buy is not None and buy>plan['structural_stop']
            reserved=quantity*buy if known else frozen
            if not known or row.outcome_status!='closed':
                record.update(status='unresolved',reason=row.reason,gross_return=None,gross_pnl=None,
                              buy_cash=quantity*buy if known else None,shares=quantity if known else None,
                              cash_reservation_assumption=not known)
            else:
                result=exit_reference(row,plan,buy,fixed_exit=fixed_exit)
                record.update(result,status='closed',reason='daily_ohlc_execution_proxy',shares=quantity,
                              buy_cash=quantity*buy,sell_cash=quantity*result['reference_sell_price'],
                              gross_pnl=quantity*(result['reference_sell_price']-buy),cash_reservation_assumption=False)
            cash-=reserved;record.update(reserved_cash=reserved,cash_after_entry=cash)
            records.append(record);positions.append(record);check()
        if not selected:records.append({'status':'abstain','signal_date':str(signal),'reason':'no_signal_or_capacity'})
    return records


def metrics(records):
    metric=action.metrics(records);metric.pop('realized_equity_curve',None)
    metric['validation']=evaluate_trade_metrics(records,trials=3)
    metric['D1_structure_failures_exited_D2_open']=sum(row.get('D1_structure_failed') is True for row in records)
    return metric


def _write(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def run(db,base,output,input_sha256,*,probability_output=None):
    if dt.date.fromisoformat(base).isoformat()!=base or base>c.now().date().isoformat():raise ValueError('invalid base')
    if len(input_sha256)!=64 or gross._hash(db)!=input_sha256:raise ValueError('PA input hash mismatch')
    history,live,names,scanned=panel(db,base,feature_enricher=enrich)
    if probability_output:
        # One enriched market scan serves both experiments. The existing model
        # projects and copies its own columns, so neither caller mutates the other.
        probability.run(db,base,probability_output,input_sha256,panel_data=(history,live,names,scanned))
    history=history[probability.causal_entry_filter(history)].copy()
    live=live[probability.causal_entry_filter(live)].copy()
    chunks=probability.chronological_split(history)
    selection=[];prepared={}
    for setup in SETUPS:
        planned=plans(chunks['selection'],setup)
        metric=metrics(simulate(planned))
        selection.append({'setup':setup,'eligible_research':metric['validation']['selection_gate'],'metrics':metric})
    eligible=[row for row in selection if row['eligible_research']]
    chosen=max(eligible,key=lambda row:(row['metrics']['validation']['familywise_lower_mean_return'],row['metrics']['win_rate'])) if eligible else None
    selected=chosen['setup'] if chosen else None
    # Choice is fixed before diagnostic setup generation or execution.
    variants={'baseline_fixed_3pct_2pct':probability.simulate(chunks['diagnostic'])}
    for setup in SETUPS:
        prepared[setup]=plans(chunks['diagnostic'],setup)
        variants['fixed_'+setup]=simulate(prepared[setup])
    variants['selected_policy']=variants['fixed_'+selected] if chosen else []
    if chosen:variants['selected_setup_fixed_3pct_2pct']=simulate(prepared[selected],fixed_exit=True)
    diagnostic={key:metrics(records) for key,records in variants.items()}
    generated=c.stamp();timing=prediction_timing(base,generated)
    current=[];observations=[]
    for setup in SETUPS:
        current_frame=plans(live,setup)
        for row in current_frame.itertuples():
            plan=dict(row.trade_plan,symbol=row.symbol,name=names.get(row.symbol),close=float(row.close),
                      atr14=float(row.atr14),
                      generated_at=generated,base_trade_date=base,target_trade_date=timing['target_trade_date'],
                      input_database_sha256=input_sha256,formal_trade_ready=False,actual_fill_verified=False,
                      entry_mode='daily_open_reference_only',opening_breakout_retest_verified=False)
            if chosen and setup==selected:current.append(plan)
            observations.append({**plan,'quantity_ref':0,'observation_only':True})
    current.sort(key=lambda row:(-row['rank_score'],row['symbol']))
    observations.sort(key=lambda row:(-row['rank_score'],row['symbol']))
    from current_selection_scope import filter_current_study
    scoped = filter_current_study({'current_watchlist': current,
        'setup_observations': observations}, db, base, generated)
    current, observations = scoped['current_watchlist'], scoped['setup_observations']
    policy={'active':bool(chosen),'selected_setup':selected,'scope':'research_only',
            'selection_frozen_before_diagnostic':True,'formal_activation':False}
    definition=hashlib.sha256(json.dumps(PROTOCOL,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    summary={'schema':VERSION,'generated_at':generated,'base_trade_date':base,**timing,
        'input_database_sha256':input_sha256,'strategy_definition_sha256':definition,
        'formal_trade_ready':False,'actual_win_rate':None,'execution_win_rate_80_validated':False,
        'costs_included':False,'historical_fundflow_used':False,'validation_origin':'previously_exposed_development_diagnostic',
        'policy':policy,'protocol':PROTOCOL,'split':{key:probability.describe(q) for key,q in chunks.items()},
        'selection':selection,'diagnostic':diagnostic,'current_watchlist':current[:10],
        'setup_observations':observations[:6],'formal_candidates':[],
        'current_selection_scope':scoped['current_selection_scope'],
        'coverage':{'scanned':scanned,'history_candidates':len(history),'live_candidates':len(live),
                    'eligible_plans':len(observations),'current_selected':len(current)},
        'creator_reference_posture':'verified_official_video_topics_and_descriptions_no_complete_video_transcript',
        'limitations':[
            'Source videos establish referenced topics only; verified Brooks theory and our own thresholds are distinguished.',
            'This is a daily-bar adaptation, not a complete High2 state machine or verified reproduction of the creator strategy.',
            'Daily open reference entries do not backtest intraday breakout/retest confirmations; fresh minute and official quotes are required.',
            'Fixed thresholds and targets were frozen before this run; all old data remain exposed development history.',
            'The baseline shares dates, capital and universe but retains fixed3/2 exits; matching-setup exit control is separate.',
            'Mathematical break-even probabilities are required success thresholds, not forecasts or verified win rates.',
            'Structural invalidation is not a guaranteed stop; D1 cannot sell and D2 gaps or limits can exceed the stressed risk budget.',
            'Historical fundflow, PIT status, official limits, adjustment and actual fills are missing; no whole-system win-rate claim.',
            'D3 strength is auxiliary and cannot extend the fixed D2 exit; unknowns reserve capital and remain in denominators.'
        ]}
    if gross._hash(db)!=input_sha256:raise ValueError('PA input changed')
    out=Path(output);out.mkdir(parents=True,exist_ok=False)
    _write(out/'summary.json',summary);_write(out/'protocol.json',PROTOCOL)
    pd.DataFrame([dict(row,variant=key) for key,records in variants.items() for row in records]).to_csv(out/'diagnostic_trades.csv.gz',index=False,compression={'method':'gzip','mtime':0})
    _write(out/'current_plans.json',{'policy':policy,'rows':current,'observations':observations,
        'current_selection_scope':scoped['current_selection_scope']})
    print(json.dumps({'pa_trade_stage':'complete','policy':policy,'coverage':summary['coverage'],
                      'diagnostic':{key:{name:value.get(name) for name in ('n_attempts','n_closed','n_unresolved','win_rate','ge3_rate','ge6_rate','mean_closed_gross_return')} for key,value in diagnostic.items()}},ensure_ascii=False),flush=True)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True);parser.add_argument('--base',required=True)
    parser.add_argument('--input-sha256',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--probability-output')
    args=parser.parse_args();run(args.db,args.base,args.output,args.input_sha256,probability_output=args.probability_output)
