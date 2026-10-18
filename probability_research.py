#!/usr/bin/env python3
"""Fixed, calibrated, date-purged T+1 probability experiment. Research only."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

import collector as c
import gross_swing_strategy as gross
import price_action_research as action
from net_profit_strategy import prediction_timing
from probability_panel import panel, frozen_cap
from probabilistic_models import fit_bundle, predict_bundle, PROTOCOL as MODEL_PROTOCOL
from probability_validation import evaluate_predictions, evaluate_trade_metrics

VERSION = 'calibrated-tplus1-probability-v1'
TARGETS = {'p_win3':'label_profit3', 'p_ge3':'label_ge3',
           'p_ge6':'label_ge6', 'p_strength3':'strength3_label'}
FEATURES = list(dict.fromkeys(gross.FEATURES + action.FEATURES))
EVIDENCE_COLUMNS = ['date','symbol','entry_date','outcome_date','reference_buy_price',
                    'entry_evidence_valid','entry_one_price','outcome_status','reason',
                    'day2_open','day2_high','day2_low','day2_close','first_day_green',
                    'day2_close_up','day2_touch3','day2_touch6','day3_date',
                    'policy_label_status']+list(TARGETS.values())
MODELS = ('logistic','hgb','equal_ensemble')
GATES = {'balanced': {'win':.55,'ge3':.25,'disagreement':.15},
         'selective': {'win':.70,'ge3':.35,'disagreement':.10}}
POLICIES = tuple({'id':model+'_'+gate,'model':model,'gate':gate}
                 for model in MODELS for gate in GATES)
PROTOCOL = {
    'schema': VERSION, 'feature_latest_session':'T_after_close',
    'price_universe':[5,100], 'entry':'D1_open_reference_with_frozen_limit',
    'frozen_cap':'floor_cent(min(T_close*1.025,100))',
    'exit':'D2_TP3_SL2_else_close', 'alternative_exit':'D2_TP6_SL2_else_close',
    'same_bar_ambiguity':'stop_first_with_target_first_sensitivity',
    'D1_sale_allowed':False, 'costs_included':False,
    'capital':30000,'position_budget':10000,'lot_size':100,'max_positions':3,
    'cancelled_or_saved_cash_reallocated_at_same_open':False,
    'D2_close_proceeds_available_at_D2_open':False,
    'unknown_execution':'reserves_cash_and_capacity_not_dropped',
    'targets':TARGETS, 'probability_condition':'valid_cap_eligible_D1_open_and_valid_D2_bar_reference',
    'p_ge6_is_separate_6pct_exit_policy':True,
    'three_day_strength_is_auxiliary_not_holding_extension':True,
    'features':FEATURES,'models':list(MODELS),'fixed_selection_policies':list(POLICIES),
    'gates':GATES,'split':[.5,.15,.15,.2],
    'purge':'all_prior_D3_outcomes_strictly_before_next_signal_block',
    'training_cap_per_date':500,'training_cap_rule':'fixed_SHA256(symbol)_order_no_outcomes',
    'rank_score':'.55*p_win3+.30*p_ge3+.15*p_strength3-.20*win_model_disagreement',
    'abstain_if_feature_clip_fraction_above':.25,'abstain_if_feature_missing_fraction_above':.10,
    'diagnostic_used_for_selection':False,
    'selection_gate':'20_closed_10_dates_no_unknowns_positive_date_and_moving5_block_familywise_mean_lower',
    'trial_count':len(POLICIES),'intervals':'approximate_dependent_data_diagnostics_not_finite_sample_guarantees',
    'historical_fundflow_used':False,'current_industry_members_backfilled':False,
    'validation_origin':'previously_exposed_development_not_fresh_holdout',
    'formal_trade_ready':False,'execution_win_rate_80_validated':False,
    'probability_model_protocol':MODEL_PROTOCOL,
}


def write(path, value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def finite(value):
    try:
        value=float(value)
        return value if math.isfinite(value) else None
    except (ValueError,TypeError):
        return None


def causal_entry_filter(frame):
    """Uses T-only evidence; future cancellation/labels never select candidates."""
    return ((frame.median_amount20>=20_000_000)&(frame.volatility20<=.05)
            &frame.ret1.between(-.04,.06)&frame.ma20_gap.between(-.06,.15)
            &frame.close_location.between(.25,.95)&(frame.market_breadth>=.30)
            &(frame.market_mean_return>=-.02))


def chronological_split(history):
    dates=sorted(history.date.unique())
    if len(dates)<180:
        raise ValueError('requires 180 historical signal dates')
    cuts=[dates[int(len(dates)*q)] for q in (.5,.65,.8)]
    chunks={};lower=None
    for name,upper in zip(('train','calibration','selection','diagnostic'),cuts+[None]):
        keep=pd.Series(True,index=history.index)
        if lower is not None:keep &= history.date>=lower
        if upper is not None:keep &= (history.date<upper)&history.day3_date.notna()&(history.day3_date<upper)
        chunks[name]=history[keep].copy().reset_index(drop=True)
        if chunks[name].empty:raise ValueError('empty probability block '+name)
        lower=upper
    for left,right in zip(list(chunks)[:-1],list(chunks)[1:]):
        if not chunks[left].day3_date.max()<chunks[right].date.min():
            raise ValueError('D3 label boundary overlap')
    return chunks


def date_cap(frame, maximum=500):
    """Predetermined symbol hash; no label, return or ticker-order optimisation."""
    q=frame.copy()
    keys={s:hashlib.sha256(str(s).encode()).hexdigest() for s in q.symbol.unique()}
    q['_sample_key']=q.symbol.map(keys)
    return q.sort_values(['date','_sample_key']).groupby('date',sort=False).head(maximum).drop(columns='_sample_key').reset_index(drop=True)


def scored(frame, predictions, model):
    q=frame.copy()
    for key,values in predictions['models'][model].items():q[key]=values
    q['model_disagreement']=predictions['disagreement']['p_win3']
    q['probability_support']=all(predictions['available'][model][key] for key in ('p_win3','p_ge3','p_strength3'))
    q['feature_clip_fraction']=predictions['feature_clip_fraction']
    q['feature_missing_fraction']=predictions['feature_missing_fraction']
    q['probability_rank_score']=.55*q.p_win3+.30*q.p_ge3+.15*q.p_strength3-.20*q.model_disagreement
    return q


def policy_mask(frame, policy):
    gate=GATES[policy['gate']]
    return ((frame.p_win3>=gate['win'])&(frame.p_ge3>=gate['ge3'])
            &(frame.model_disagreement<=gate['disagreement'])&frame.probability_support
            &(frame.feature_clip_fraction<=.25)&(frame.feature_missing_fraction<=.10))


def simulate(frame, policy=None, *, target=.03):
    """T-frozen quantities and cash, D1 entry then D2 exit only; no future reranking."""
    cash=30000.;settled_pnl=0.;positions=[];records=[]
    def check():
        if cash < -1e-6 or not math.isclose(cash+sum(p['reserved_cash'] for p in positions),30000+settled_pnl,abs_tol=1e-5):
            raise ValueError('probability portfolio cash conservation violated')
    for signal_day,daily in frame.groupby('date',sort=True):
        dates=daily.entry_date.dropna();entry_day=str(dates.iloc[0]) if len(dates) else None
        remaining=[]
        for position in positions:
            if entry_day and position['status']=='closed' and position['exit_date']<entry_day:
                cash+=position['sell_cash'];settled_pnl+=position['gross_pnl']
            else:remaining.append(position)
        positions=remaining;check()
        if policy is not None:daily=daily[policy_mask(daily,policy)]
        ranking='probability_rank_score' if policy is not None else 'price_action_score'
        daily=daily.sort_values([ranking,'symbol'],ascending=[False,True])
        selected=0;planned_cash=cash
        for row in daily.itertuples():
            if selected>=3 or len(positions)>=3:break
            if any(p['symbol']==row.symbol for p in positions):continue
            budget=min(10000.,planned_cash)
            cap=frozen_cap(row.close)
            shares=int(budget/cap//100)*100
            if not shares:continue
            frozen=shares*cap;planned_cash-=frozen;selected+=1
            buy=finite(row.reference_buy_price)
            record={'signal_date':str(signal_day),'entry_date':row.entry_date,'exit_date':row.outcome_date,
                    'symbol':row.symbol,'frozen_buy_limit_cap':cap,'frozen_order_quantity':shares,
                    'frozen_budget':budget,'frozen_capital':frozen,'reference_buy_price':buy,
                    'price_action_score':float(row.price_action_score),'cash_before_entry':cash,
                    'first_day_green':bool(row.first_day_green) if pd.notna(row.first_day_green) else None,
                    'day2_close_up':bool(row.day2_close_up) if pd.notna(row.day2_close_up) else None,
                    'day2_touch3':bool(row.day2_touch3) if pd.notna(row.day2_touch3) else None,
                    'day2_touch6':bool(row.day2_touch6) if pd.notna(row.day2_touch6) else None,
                    'strength3_label':int(row.strength3_label) if pd.notna(row.strength3_label) else None,
                    'day3_date':row.day3_date,'execution_verified':False,'costs_included':False,
                    'source':'daily_ohlc_execution_proxy','target':target,'stop':.02}
            for key in TARGETS:
                record[key]=finite(getattr(row,key,None))
            if bool(row.entry_evidence_valid) and buy is not None and buy>0 and (not 5<=buy<=100 or buy>cap+1e-12):
                record.update(status='no_fill',reason='predeclared_open_cap_or_range_cancel',reserved_cash=0.,gross_return=None,gross_pnl=None)
                records.append(record);continue
            known=bool(row.entry_evidence_valid) and not bool(row.entry_one_price) and buy is not None and buy>0
            reserved=shares*buy if known else frozen
            if not known or row.outcome_status!='closed':
                record.update(status='unresolved',reason=row.reason,gross_return=None,gross_pnl=None,
                              buy_cash=shares*buy if known else None,shares=shares if known else None,
                              cash_reservation_assumption=not known)
            else:
                result=gross.exit_outcome(buy,row.day2_open,row.day2_high,row.day2_low,row.day2_close,target,.02)
                record.update(result,status='closed',reason='daily_ohlc_execution_proxy',shares=shares,
                              buy_cash=shares*buy,sell_cash=shares*result['reference_sell_price'],
                              gross_pnl=shares*(result['reference_sell_price']-buy),cash_reservation_assumption=False)
            cash-=reserved;record.update(reserved_cash=reserved,cash_after_entry=cash)
            records.append(record);positions.append(record);check()
        if not selected:records.append({'status':'abstain','signal_date':str(signal_day),'reason':'no_eligible_signal_or_capacity'})
    return records


def metrics(records, trials=6):
    result=action.metrics(records)
    result['validation']=evaluate_trade_metrics(records,trials=trials,forward_only=False,actual_execution=False,pit_verified=False)
    result.pop('realized_equity_curve',None)
    return result


def describe(frame):
    return {'rows':len(frame),'signal_dates':int(frame.date.nunique()),
            'first_signal_date':str(frame.date.min()),'last_signal_date':str(frame.date.max()),
            'last_D3_outcome':str(frame.day3_date.dropna().max()) if frame.day3_date.notna().any() else None,
            'known_profit_labels':int(frame.label_profit3.notna().sum())}


def run(db,base,output,input_sha256,*,panel_data=None):
    if dt.date.fromisoformat(base).isoformat()!=base or base>c.now().date().isoformat():raise ValueError('future or invalid base')
    if len(input_sha256)!=64 or gross._hash(db)!=input_sha256:raise ValueError('input database hash mismatch')
    history,live,names,scanned=panel(db,base) if panel_data is None else panel_data
    columns=list(dict.fromkeys(FEATURES+EVIDENCE_COLUMNS))
    history=history[columns].copy();live=live[columns].copy()
    # All supervised endpoints share the declared entry/execution conditioning.
    # The execution replay still retains unknown/cancelled candidates themselves.
    history.loc[history.policy_label_status!='closed','strength3_label']=np.nan
    raw_count=len(history);raw_live=len(live)
    history=history[causal_entry_filter(history)].reset_index(drop=True)
    live=live[causal_entry_filter(live)].reset_index(drop=True)
    if live.empty:raise ValueError('no causal live probability inputs')
    chunks=chronological_split(history)
    train=date_cap(chunks['train']);calibration=date_cap(chunks['calibration'])
    bundle=fit_bundle(train,calibration,FEATURES,TARGETS,date_column='date')
    print(json.dumps({'probability_stage':'models_fitted','train':describe(train),'calibration':describe(calibration)}),flush=True)
    selection_predictions=predict_bundle(bundle,chunks['selection'])
    selection=[];selection_scored={}
    for model in MODELS:selection_scored[model]=scored(chunks['selection'],selection_predictions,model)
    for policy in POLICIES:
        report=metrics(simulate(selection_scored[policy['model']],policy))
        gate=report['validation']['selection_gate']
        selection.append({'policy':policy,'eligible_research':bool(gate),'metrics':report})
    eligible=[row for row in selection if row['eligible_research']]
    chosen=max(eligible,key=lambda r:(r['metrics']['validation']['familywise_moving_block_mean_lower'],r['metrics']['win_rate'])) if eligible else None
    # Policy choice is frozen here, before diagnostic model calls or outcomes.
    chosen_policy=chosen['policy'] if chosen else None
    diagnostic_predictions=predict_bundle(bundle,chunks['diagnostic'])
    diagnostic_scored={model:scored(chunks['diagnostic'],diagnostic_predictions,model) for model in MODELS}
    variants={'same_universe_price_action_baseline':simulate(diagnostic_scored['equal_ensemble'])}
    for policy in POLICIES:variants[policy['id']]=simulate(diagnostic_scored[policy['model']],policy)
    variants['selected_policy']=simulate(diagnostic_scored[chosen_policy['model']],chosen_policy) if chosen else []
    if chosen:variants['selected_policy_6pct_exit']=simulate(diagnostic_scored[chosen_policy['model']],chosen_policy,target=.06)
    diagnostic={key:metrics(value) for key,value in variants.items()}
    baseline_rates={key:float(train.groupby('date')[label].mean().dropna().mean()) if train[label].notna().any() else .5 for key,label in TARGETS.items()}
    calibration_diagnostics={model:evaluate_predictions(diagnostic_scored[model],endpoints=TARGETS,date_col='date',baseline_probabilities=baseline_rates) for model in MODELS}
    # Stability slices do not change thresholds, estimator weights or policy.
    dates=sorted(chunks['diagnostic'].date.unique());stability=[]
    for index,block in enumerate(np.array_split(dates,3)):
        block_dates=set(block.tolist())
        records=[row for row in variants['selected_policy'] if row['signal_date'] in block_dates]
        stability.append({'block':index+1,'first_date':str(block[0]) if len(block) else None,
                          'last_date':str(block[-1]) if len(block) else None,'metrics':metrics(records)})
    live_predictions=predict_bundle(bundle,live)
    live_model=chosen_policy['model'] if chosen else 'equal_ensemble'
    current=scored(live,live_predictions,live_model)
    current=current.sort_values(['probability_rank_score','symbol'],ascending=[False,True])
    rows=[]
    for row in current.itertuples():
        rows.append({'symbol':row.symbol,'name':names.get(row.symbol),'close':float(row.close),
                     **{key:float(getattr(row,key)) if live_predictions['available'][live_model][key] else None for key in TARGETS},
                     'model_disagreement':float(row.model_disagreement),'ranking_score':float(row.probability_rank_score),
                     'probability_support':bool(row.probability_support),
                     'endpoint_availability':live_predictions['available'][live_model],
                     'feature_clip_fraction':float(row.feature_clip_fraction),
                     'frozen_buy_limit_cap':frozen_cap(row.close),
                     'research_eligible':bool(chosen and policy_mask(pd.DataFrame([row._asdict()]),chosen_policy).iloc[0]),
                     'probabilities_are_conditional_daily_reference_events':True,'formal_trade_ready':False})
    generated=c.stamp()
    definition=hashlib.sha256(json.dumps(PROTOCOL,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    policy={'active':bool(chosen),'chosen':chosen_policy,'scope':'research_only',
            'selection_frozen_before_diagnostic':True,'formal_activation':False,
            'inactive_reason':None if chosen else 'none_passed_fixed_selection_evidence_and_positive_lower_expectancy'}
    metadata={'schema':VERSION,'generated_at':generated,'base_trade_date':base,
              **prediction_timing(base,generated),'input_database_sha256':input_sha256,
              'strategy_definition_sha256':definition,'formal_trade_ready':False,'actual_win_rate':None,
              'execution_win_rate_80_validated':False,'costs_included':False,'historical_fundflow_used':False,
              'validation_origin':PROTOCOL['validation_origin']}
    summary={**metadata,'policy':policy,'protocol':PROTOCOL,'split':{key:describe(q) for key,q in chunks.items()},
             'sampled_fit_blocks':{'train':describe(train),'calibration':describe(calibration)},
             'model_metadata':bundle.get('metadata',{}),'selection':selection,'diagnostic':diagnostic,
             'endpoint_availability':live_predictions['available'],
             'probability_diagnostics':calibration_diagnostics,'selected_stability':stability,
             'coverage':{'history_rows_scanned':scanned,'wide_universe_history_rows':raw_count,
                         'causal_history_candidates':len(history),'wide_universe_live_rows':raw_live,
                         'causal_live_candidates':len(live),'current_research_eligible':sum(row['research_eligible'] for row in rows)},
             'current_watchlist':[row for row in rows if row['research_eligible']][:10],
             'formal_candidates':[],
             'limitations':[
                 'All old history was already exposed; chronological diagnostics are development evidence, not a new untouched holdout.',
                 'Current census survivorship, raw unadjusted prices, missing historical official status/limits/corporate actions remain.',
                 'Probabilities condition on cap-eligible reference entry and known D2 daily bars, not on personal order execution.',
                 'Targets are supervised on fixed D2 exit rules; p_ge6 uses the separate 6 percent exit policy.',
                 'Unknowns remain in execution attempt denominators and reserve cash; cancelled entries are separately counted.',
                 'Date and moving-five-date bootstraps are approximations and not universal statistical guarantees.',
                 'Six fixed candidates increase selection bias; familywise bootstrap adjustment is approximate, not a proof against all prior research.',
                 'No historical point-in-time fund flow exists; money flow is retained as a separate current evidence layer.',
                 'No conformal exchangeability assumption or guaranteed 80 percent trading win rate is claimed.',
                 'Formal trading needs forward evidence, verified execution and point-in-time official inputs; no automated orders.'
             ]}
    if gross._hash(db)!=input_sha256:raise ValueError('database changed during experiment')
    out=Path(output);out.mkdir(parents=True,exist_ok=False)
    write(out/'summary.json',summary);write(out/'protocol.json',PROTOCOL)
    write(out/'predictions.json',{**metadata,'policy':policy,'rows':rows})
    joblib.dump(bundle,out/'models.joblib',compress=3)
    live[['date','symbol']+FEATURES].to_csv(out/'prediction_inputs.csv.gz',index=False,compression={'method':'gzip','mtime':0})
    pd.DataFrame([dict(row,variant=key) for key,records in variants.items() for row in records]).to_csv(out/'diagnostic_trades.csv.gz',index=False,compression={'method':'gzip','mtime':0})
    print(json.dumps({'probability_stage':'complete','policy':policy,'coverage':summary['coverage'],
                      'diagnostic':{key:{k:v.get(k) for k in ('n_attempts','n_closed','n_unresolved','win_rate','ge3_rate','ge6_rate','mean_closed_gross_return')} for key,v in diagnostic.items()}},ensure_ascii=False),flush=True)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True);parser.add_argument('--base',required=True)
    parser.add_argument('--output',required=True);parser.add_argument('--input-sha256',required=True)
    args=parser.parse_args();run(args.db,args.base,args.output,args.input_sha256)
