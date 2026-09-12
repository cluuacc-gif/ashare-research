#!/usr/bin/env python3
"""Exploratory chronological validation on replay-verified real bars; never a formal model."""
import argparse
import gzip
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
import joblib

import collector as c
from short_term import digest, write_json

VERSION="diagnostic-price-volume-1.0"
FEATURES=["close","ret1","ret3","ret5","ret10","ret20","ret60",
          "ma5_gap","ma10_gap","ma20_gap","ma60_gap","range","gap",
          "volume_ratio5","volume_ratio20","volatility20"]
GROUPS={"price":["close"],"returns":["ret1","ret3","ret5","ret10","ret20","ret60"],
        "moving_averages":["ma5_gap","ma10_gap","ma20_gap","ma60_gap"],
        "volume":["volume_ratio5","volume_ratio20"],"range_volatility":["range","gap","volatility20"]}

def build_frame(index):
    frames=[]
    for item in index["items"]:
        if item["ohlcv_bars"]<250: continue
        payload=json.loads(gzip.decompress(Path(item["normalized_local_path"]).read_bytes()))
        d=pd.DataFrame(payload["rows"],columns=payload["fields"]).sort_values("trade_date")
        for col in ("open","high","low","close","volume"):
            d[col]=pd.to_numeric(d[col],errors="coerce")
        x=pd.DataFrame({"symbol":item["symbol"],"date":d.trade_date,"close":d.close})
        for n in (1,3,5,10,20,60): x["ret"+str(n)]=d.close/d.close.shift(n)-1
        for n in (5,10,20,60): x["ma"+str(n)+"_gap"]=d.close/d.close.rolling(n,min_periods=n).mean()-1
        x["range"]=(d.high-d.low)/d.close.shift(1)
        x["gap"]=d.open/d.close.shift(1)-1
        for n in (5,20): x["volume_ratio"+str(n)]=d.volume/d.volume.shift(1).rolling(n,min_periods=n).mean()
        x["volatility20"]=x.ret1.rolling(20,min_periods=20).std()
        x["outcome_date"]=d.trade_date.shift(-1)
        x["forward_return"]=d.close.shift(-1)/d.close-1
        x["P1"]=(x.forward_return>0).astype(int)
        x["P2"]=(x.forward_return>=.05-1e-12).astype(int)
        x=x[(d.close>=5)&(d.close<=10)&x.outcome_date.notna()&(x.outcome_date<=index["target_date"])]
        x=x.replace([np.inf,-np.inf],np.nan).dropna(subset=FEATURES+["forward_return"])
        frames.append(x)
    if not frames: return pd.DataFrame()
    return pd.concat(frames,ignore_index=True).sort_values(["date","symbol"]).reset_index(drop=True)

def wilson(k,n):
    if not n: return None
    z=1.959963984540054;p=k/n;den=1+z*z/n
    delta=z*np.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    center=(p+z*z/(2*n))/den
    return [float(center-delta),float(center+delta)]

def date_interval(frame,value):
    grouped=frame.groupby("date")[value].agg(["sum","count"]).to_numpy()
    if len(grouped)<5: return None
    rng=np.random.default_rng(20260912)
    picks=rng.integers(0,len(grouped),size=(500,len(grouped)))
    estimates=grouped[picks,0].sum(axis=1)/grouped[picks,1].sum(axis=1)
    return [float(v) for v in np.quantile(estimates,[.025,.975])]

def metrics(d,label,p):
    z=d[["date"]].copy();z["actual"]=d[label].to_numpy();z["pred"]=p
    z["squared_error"]=(z.pred-z.actual)**2
    z["bias"]=z.pred-z.actual
    bins=[]
    for lo in range(0,100,5):
        mask=(z.pred>=lo/100)&(z.pred<((lo+5)/100) if lo<95 else z.pred<=1)
        v=z[mask]
        if not len(v): continue
        n=len(v);k=int(v.actual.sum())
        bins.append({"lower":lo/100,"upper":(lo+5)/100,"n":n,"k":k,
          "independent_dates":int(v.date.nunique()),"predicted_mean":float(v.pred.mean()),
          "observed_frequency":k/n,"brier":float(v.squared_error.mean()),
          "calibration_bias":float(v.bias.mean()),
          "wilson_independence_assumption_only":wilson(k,n),
          "date_cluster_frequency_95_interval":date_interval(v,"actual"),
          "date_cluster_bias_95_interval":date_interval(v,"bias")})
    return {"n":len(z),"independent_dates":int(z.date.nunique()),
            "positive_count":int(z.actual.sum()),"actual_frequency":float(z.actual.mean()),
            "predicted_mean":float(z.pred.mean()),"brier":float(z.squared_error.mean()),
            "calibration_bias":float(z.bias.mean()),"bins_5_percentage_points":bins}

def run(index_path,output):
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    index=json.loads(Path(index_path).read_text())
    d=build_frame(index)
    result={"schema_version":"1.0","model_version":VERSION,"kind":"diagnostic/historical_research",
            "generated_at":c.stamp(),"github_run_id":os.environ.get("GITHUB_RUN_ID"),
            "target_date":index["target_date"],"verified_index_sha256":digest(Path(index_path)),
            "formal_model_ready":False,"model_ready":False,"execution_win_rate_80_validated":False,
            "formal_predictions_created":0,"data_source":"only raw-response-replay-verified real history",
            "P3a":{"status":"not_evaluable","reason":"historical official limit prices/status unavailable"},
            "P3b":{"status":"not_evaluable","reason":"historical official limit prices/status unavailable"},
            "limitations":["Current surviving vendor census is not a historical full-market point-in-time universe.",
               "Historical ST, suspension, delisting, corporate actions and complete exchange sessions remain unverified.",
               "Next observed bar may follow a suspension; this is not certified next-exchange-session labeling.",
               "P1/P2 raw close-return research does not evaluate conditional fills, fees, slippage or E+1/E+2 net profit.",
               "Sector, events, funds and official daily limit prices are unavailable; no full-factor model was trained.",
               "Only one prespecified exploratory price/volume model is tested; no daily Top10 or probabilities are published.",
               "Wilson intervals assume independent observations; date-cluster intervals are additionally reported.",
               "No production database or original cross-day handoff is written by this job."]}
    if d.empty or d.date.nunique()<200:
        result.update(status="INSUFFICIENT_REAL_SAMPLE",real_samples=len(d),independent_dates=int(d.date.nunique()) if not d.empty else 0)
        write_json(out/"model_validation.json",result);print(json.dumps(result));return
    dates=sorted(d.date.unique())
    a=dates[int(len(dates)*.6)];b=dates[int(len(dates)*.8)]
    train=d[(d.date<a)&(d.outcome_date<a)].copy()
    cal=d[(d.date>=a)&(d.date<b)&(d.outcome_date<b)].copy()
    test=d[d.date>=b].copy()
    def describe(q):return {"n":len(q),"dates":int(q.date.nunique()),"first":str(q.date.min()),"last":str(q.date.max()),"last_outcome":str(q.outcome_date.max())}
    result.update(status="EXPLORATORY_TIME_SPLIT_COMPUTED",real_samples=len(d),independent_dates=len(dates),
                  sample_symbols=int(d.symbol.nunique()),split={"train":describe(train),"calibration":describe(cal),"test":describe(test)},
                  split_rule="Chronological 60/20/20 dates; outcomes at/after the following block boundary purged.",
                  features=FEATURES, endpoints={},variable_checks=[],calibration_windows={})
    if min(len(train),len(cal),len(test))<1000 or min(train.date.nunique(),cal.date.nunique(),test.date.nunique())<30:
        result["status"]="INSUFFICIENT_REAL_SAMPLE"
        write_json(out/"model_validation.json",result);print(json.dumps(result));return
    models={}; predictions=test[["symbol","date","outcome_date","forward_return","P1","P2"]].copy()
    for label in ("P1","P2"):
        if train[label].nunique()<2 or cal[label].sum()<30:
            result["endpoints"][label]={"status":"insufficient_class_outcomes"};continue
        model=HistGradientBoostingClassifier(max_iter=80,max_leaf_nodes=15,min_samples_leaf=100,
                       learning_rate=.06,l2_regularization=5,random_state=20260912,early_stopping=False)
        model.fit(train[FEATURES],train[label])
        raw_cal=model.predict_proba(cal[FEATURES])[:,1]
        calibrator=IsotonicRegression(y_min=0,y_max=1,out_of_bounds="clip").fit(raw_cal,cal[label])
        raw_test=model.predict_proba(test[FEATURES])[:,1]
        calibrated=calibrator.predict(raw_test)
        models[label]={"estimator":model,"calibrator":calibrator}
        predictions[label+"_raw"]=raw_test
        predictions[label+"_calibrated"]=calibrated
    if set(models)=={"P1","P2"}:
        predictions["P2_calibrated"]=np.minimum(predictions.P2_calibrated,predictions.P1_calibrated)
    for label,parts in models.items():
        raw=predictions[label+"_raw"].to_numpy();calibrated=predictions[label+"_calibrated"].to_numpy()
        baseline=np.full(len(test),float(cal[label].mean()))
        result["endpoints"][label]={"raw":metrics(test,label,raw),"calibrated":metrics(test,label,calibrated),
           "calibration_block_constant_baseline":metrics(test,label,baseline),
           "baseline_scope":"same incomplete observed test panel; not full-market excess ability"}
        for window in (20,60,120,250):
            selected=set(sorted(test.date.unique())[-window:])
            mask=test.date.isin(selected).to_numpy()
            result["calibration_windows"].setdefault(str(window),{})[label]={
               "requested_historical_test_dates":window,"actual_historical_test_dates":int(test.loc[mask,"date"].nunique()),
               "effective_formal_forward_dates":0,"origin":"historical_holdout_not_forward_predictions",
               "metrics":metrics(test.loc[mask],label,calibrated[mask])}
        original_error=(raw-test[label].to_numpy())**2
        rng=np.random.default_rng(20260912)
        for group,cols in GROUPS.items():
            shuffled=test[FEATURES].copy()
            for _,positions in test.groupby("date").indices.items():
                values=shuffled.iloc[positions][cols].to_numpy()
                shuffled.iloc[positions,shuffled.columns.get_indexer(cols)]=values[rng.permutation(len(positions))]
            perm=parts["estimator"].predict_proba(shuffled)[:,1]
            delta=(perm-test[label].to_numpy())**2-original_error
            frame=pd.DataFrame({"date":test.date.to_numpy(),"delta":delta})
            day=frame.groupby("date").delta.mean().to_numpy()
            sign=rng.choice([-1,1],size=(2000,len(day)))
            p=float((1+((sign*day).mean(axis=1)>=day.mean()).sum())/2001)
            result["variable_checks"].append({"endpoint":label,"group":group,
                "mean_brier_increase_after_within_date_permutation":float(delta.mean()),
                "date_cluster_95_interval":date_interval(frame,"delta"),"one_sided_date_sign_permutation_p":p,
                "interpretation":"predictive association in observed panel only; no causality or full-market claim"})
    checks=result["variable_checks"]
    sorted_checks=sorted(checks,key=lambda q:q["one_sided_date_sign_permutation_p"])
    adjusted=0.0
    for i,row in enumerate(sorted_checks):
        adjusted=max(adjusted,min(1.0,row["one_sided_date_sign_permutation_p"]*(len(checks)-i)))
        row["holm_adjusted_p"]=adjusted
        row["observed_panel_evidence"]=adjusted<.05 and row["mean_brier_increase_after_within_date_permutation"]>0
    predictions.to_parquet(out/"diagnostic_holdout_predictions.parquet",index=False)
    joblib.dump({"version":VERSION,"formal_model_ready":False,"features":FEATURES,"models":models,
                 "train_last":train.date.max(),"calibration_last":cal.date.max(),"test_first":test.date.min()},
                 out/"diagnostic_model.joblib",compress=3)
    result["artifacts"]=[{"path":p.name,"sha256":digest(p),"bytes":p.stat().st_size}
                         for p in (out/"diagnostic_holdout_predictions.parquet",out/"diagnostic_model.joblib")]
    write_json(out/"model_validation.json",result)
    lines=["# 真实历史样本探索性模型验证","","**正式模型未通过；未验证80%条件单净盈利胜率。**","",
           "已按日期先训练、后校准、再测试，仅使用此前重放校验通过的真实OHLCV。","",
           "| 数据块 | 样本数 | 交易观测日期数 | 起止 |","|---|---:|---:|---|"]
    for name,q in result["split"].items():lines.append("| "+name+" | "+str(q["n"])+" | "+str(q["dates"])+" | "+q["first"]+"至"+q["last"]+" |")
    lines+=["","| 终点 | 校准后Brier | 同观察面板常数基准Brier | 校准偏差 |","|---|---:|---:|---:|"]
    for label,q in result["endpoints"].items():
        if "calibrated" in q:lines.append("| "+label+" | "+format(q["calibrated"]["brier"],".5f")+" | "+format(q["calibration_block_constant_baseline"]["brier"],".5f")+" | "+format(q["calibrated"]["calibration_bias"],".5f")+" |")
    lines+=["","P3a/P3b缺历史官方限价，不能验证。条件单成交、持有E+1/E+2的费用与滑点缺少执行证据，80%净盈利目标未通过。",
            "当前名单存在幸存者偏差，停牌后的下一条日线不一定是下一交易所交易日；此结果只能说明有限历史面板的探索性表现。",
            "20/60/120/250窗口为历史留出测试，正式前瞻交易日仍为0，二者不混记。",
            "5个百分点分箱、Wilson描述区间、按日期聚类区间、Brier、变量分组置换和Holm多重比较校正在JSON中。",""]
    (out/"A股真实样本模型验证.md").write_text("\n".join(lines),encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k not in ("endpoints","variable_checks","calibration_windows","limitations")},ensure_ascii=False),flush=True)

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--index",required=True);p.add_argument("--output",required=True)
    a=p.parse_args();run(a.index,a.output)
