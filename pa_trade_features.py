"""Trailing context and structure inputs. Future swings never confirm past signals."""
import numpy as np
import pandas as pd
from price_action_research import price_action_features


def enrich(rows):
    data=pd.DataFrame(rows).sort_values('trade_date').reset_index(drop=True)
    for key in ('open','high','low','close'):
        data[key]=pd.to_numeric(data[key],errors='coerce')
    extras=price_action_features(rows)
    span=(data.high-data.low).replace(0,np.nan)
    previous=data.close.shift(1)
    tr=pd.concat([data.high-data.low,(data.high-previous).abs(),(data.low-previous).abs()],axis=1).max(axis=1)
    atr=tr.rolling(14,min_periods=14).mean()
    ma20=data.close.rolling(20,min_periods=20).mean()
    body=(data.close-data.open).abs()/span
    overlap=(pd.concat([data.high,data.high.shift(1)],axis=1).min(axis=1)
             -pd.concat([data.low,data.low.shift(1)],axis=1).max(axis=1)).clip(lower=0)
    overlap=overlap/pd.concat([span,span.shift(1)],axis=1).min(axis=1)
    result=data[['open','high','low']].copy()
    result['atr14']=atr
    result['ma20_slope5']=ma20/ma20.shift(5)-1
    result['body_fraction']=body
    result['lower_wick_fraction']=(data[['open','close']].min(axis=1)-data.low)/span
    result['overlap5']=overlap.rolling(5,min_periods=5).mean()
    for n in (5,20,60):
        result['prior'+str(n)+'_low']=data.low.shift(1).rolling(n,min_periods=n).min()
    result['prior20_high']=data.high.shift(1).rolling(20,min_periods=20).max()
    result['prior60_high']=data.high.shift(1).rolling(60,min_periods=60).max()
    for key in ('open','high','low','close'):
        result['previous_'+key]=data[key].shift(1)
    result['previous_body_fraction']=body.shift(1)
    result['previous_close_location']=extras.close_location.shift(1)
    result['previous_range_atr']=(tr/atr).shift(1)
    result['breakout_level']=data.high.shift(2).rolling(20,min_periods=20).max()
    result['prior_range_height']=result.breakout_level-data.low.shift(2).rolling(20,min_periods=20).min()
    result['prior_efficiency20']=extras.efficiency20.shift(1)
    result['prior_overlap5']=result.overlap5.shift(1)
    return result.replace([np.inf,-np.inf],np.nan)
