#!/usr/bin/env python3
"""Causal 5--100 CNY panel with explicit T+1 policy-label endpoints.

Retrospective raw vendor bars are research evidence, not historical PIT or fills.
The existing 5--10 CNY model's universe and defaults are deliberately independent.
"""
from __future__ import annotations

from contextlib import closing
import gc
import itertools
import json
import math
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd

import collector as c
import gross_swing_strategy as gross
from net_profit_strategy import _valid_bar, _one_price
import price_action_research as action

VERSION = 'probability-wide-panel-v1'
PRICE_MIN = 5.0
PRICE_MAX = 100.0
ENTRY_MAX_GAP = .025
FEATURES = list(dict.fromkeys(gross.FEATURES + action.FEATURES))
LABEL_ENDPOINTS = {
    'label_profit3': 'D2_TP3_SL2_stop_first_gross_return_gt_0',
    'label_ge3': 'D2_TP3_SL2_stop_first_gross_return_ge_0.03',
    'label_ge6': 'D2_TP6_SL2_stop_first_gross_return_ge_0.06_separate_policy',
    'strength3_label': 'D1_close_gt_D1_open_and_D2_close_gt_D1_close_and_D3_close_gt_D2_close',
}
LIMITATIONS = [
    'Retrospective vendor census has survivorship bias; historical PIT membership is unverified.',
    'Unadjusted prices: official historical status, price limits and corporate actions are unverified.',
    'The 30 percent raw-jump exclusion is a data guard, not an adjustment or official-limit verification.',
    'Observed market-session calendar is not a certified exchange calendar.',
    'Daily OHLC open/touch references do not establish actual orders or fills.',
    'No historical fund-flow or present-day sector membership is backfilled into training.',
    'All prior history has been exposed to development; chronological diagnostics are not fresh holdout evidence.',
    'Future D1, D2 and D3 observations are labels only and never model features.',
]


def frozen_cap(close):
    """Use a predeclared cent-price cap, independent of D1 observations."""
    return math.floor((min(float(close)*1.025, PRICE_MAX)+1e-10)*100)/100


def policy_labels(signal_date, symbol, entry_date, exit_date, entry, exit_, signal_close):
    result = gross.label_outcome(signal_date, symbol, entry_date, exit_date,
                                entry, exit_, signal_close)
    for key in ('label_profit', 'label_ge3', 'label_ge6'):
        result['raw_'+key] = result[key]
    result.update(label_profit3=None, label_ge3=None, label_ge6=None,
                  policy_gross_return3=None, policy_gross_return6=None,
                  policy_optimistic_return3=None, policy_optimistic_return6=None,
                  policy_ambiguous3=None, policy_ambiguous6=None,
                  policy_exit_reason3=None, policy_exit_reason6=None,
                  entry_cancelled=False, policy_label_status='unresolved',
                  frozen_buy_limit_cap=frozen_cap(signal_close))
    # Cancellation must depend on a valid opening observation, not on an eventual
    # D2 return or an invalid/missing bar. It is neither a win nor a negative label.
    if result['entry_evidence_valid']:
        buy = float(result['reference_buy_price'])
        if buy < PRICE_MIN or buy > result['frozen_buy_limit_cap']+1e-12:
            result.update(entry_cancelled=True, policy_label_status='cancelled',
                          policy_reason='D1_open_outside_T_frozen_cap_or_minimum')
            return result
    if result['outcome_status'] != 'closed':
        result['policy_reason'] = result['reason']
        return result
    buy = float(result['reference_buy_price'])
    for suffix, target in (('3', .03), ('6', .06)):
        outcome = gross.exit_outcome(buy, result['day2_open'], result['day2_high'],
                                    result['day2_low'], result['day2_close'], target, .02)
        result['policy_gross_return'+suffix] = outcome['gross_return']
        result['policy_optimistic_return'+suffix] = outcome['optimistic_gross_return']
        result['policy_ambiguous'+suffix] = outcome['ambiguous_same_bar']
        result['policy_exit_reason'+suffix] = outcome['exit_reason']
    result.update(label_profit3=int(result['policy_gross_return3']>0),
                  label_ge3=int(result['policy_gross_return3']>=.03-1e-12),
                  label_ge6=int(result['policy_gross_return6']>=.06-1e-12),
                  policy_label_status='closed', policy_reason='daily_ohlc_policy_proxy')
    return result


def symbol_features(rows, symbol, sessions, *, feature_enricher=None):
    """Trailing inputs at T; preserve unresolved/cancelled rows for honest replay."""
    data = pd.DataFrame(rows).sort_values('trade_date').reset_index(drop=True)
    if data.empty:
        return pd.DataFrame()
    if data.trade_date.duplicated().any():
        raise ValueError('duplicate probability-panel input dates')
    for key in ('open', 'high', 'low', 'close', 'volume', 'amount'):
        data[key] = pd.to_numeric(data[key], errors='coerce')
    ordered = data.to_dict('records')
    valid = pd.Series([_valid_bar(row) for row in ordered], index=data.index)
    market_index = {day: index for index, day in enumerate(sessions)}
    positions = data.trade_date.map(market_index)
    frame = pd.DataFrame({'symbol': symbol, 'date': data.trade_date, 'close': data.close})
    for n in (1, 3, 5, 10, 20, 60):
        frame['ret'+str(n)] = data.close/data.close.shift(n)-1
    for n in (5, 10, 20, 60):
        frame['ma'+str(n)+'_gap'] = data.close/data.close.rolling(n, min_periods=n).mean()-1
    frame['range'] = (data.high-data.low)/data.close.shift(1)
    frame['gap'] = data.open/data.close.shift(1)-1
    for n in (5, 20):
        frame['volume_ratio'+str(n)] = data.volume/data.volume.shift(1).rolling(n, min_periods=n).mean()
    frame['volatility20'] = frame.ret1.rolling(20, min_periods=20).std()
    frame['history_bars'] = valid.cumsum()
    amounts = data.amount.where(np.isfinite(data.amount) & (data.amount>0))
    frame['median_amount20'] = amounts.rolling(20, min_periods=20).median()
    frame['candle_body'] = data.close/data.open-1
    frame['upper_shadow'] = (data.high-data[['open', 'close']].max(axis=1))/data.close
    frame['amount_ratio20'] = amounts/amounts.shift(1).rolling(20, min_periods=20).mean()
    frame['range_ratio20'] = frame['range']/frame['range'].shift(1).rolling(20, min_periods=20).median()
    extras = action.price_action_features(ordered)
    for key in extras:
        frame[key] = extras[key]
    if feature_enricher is not None:
        extra=feature_enricher(ordered)
        if len(extra)!=len(data) or set(extra.columns)&set(frame.columns):
            raise ValueError('context enrichment must add aligned new columns only')
        for key in extra:
            frame[key]=extra[key].reindex(data.index)
    raw_jumps = frame.ret1.abs().gt(.30).astype(int)
    window_ok = ((positions-positions.shift(60)==60)
        & valid.rolling(61, min_periods=61).sum().eq(61)
        & raw_jumps.rolling(61, min_periods=61).sum().eq(0))
    one_price = (data.high-data.low).abs().lt(1e-9)
    causal_features = [key for key in FEATURES if key not in
                       ('market_mean_return', 'market_breadth', 'relative_return5', 'price_action_score')]
    frame = frame[window_ok & frame.history_bars.ge(250)
                  & frame.close.between(PRICE_MIN, PRICE_MAX) & ~one_price]
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna(subset=causal_features)
    if frame.empty:
        return frame
    by_date = {row['trade_date']: row for row in ordered}
    labels = []
    for row in frame.itertuples():
        index = market_index[row.date]
        entry_day = sessions[index+1] if index+1<len(sessions) else None
        exit_day = sessions[index+2] if index+2<len(sessions) else None
        third_day = sessions[index+3] if index+3<len(sessions) else None
        result = policy_labels(row.date, symbol, entry_day, exit_day,
                               by_date.get(entry_day), by_date.get(exit_day), row.close)
        result.update(action.auxiliary_labels(entry_day, exit_day, third_day, by_date))
        labels.append(result)
    return pd.concat([frame, pd.DataFrame(labels, index=frame.index)], axis=1)


def panel(db_path, base, *, feature_enricher=None):
    """Return history, live, names, scanned; source is opened SQLite read-only."""
    frames, market, scanned = [], [], 0
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA application_id').fetchone()[0] != c.APP_ID:
            raise ValueError('not the original market database')
        if db.execute('SELECT MAX(trade_date) FROM daily_quotes').fetchone()[0] != base:
            raise ValueError('base date differs from probability-panel database')
        sessions = [row[0] for row in db.execute('SELECT DISTINCT trade_date FROM daily_quotes WHERE trade_date<=? ORDER BY trade_date', (base,))]
        positions = {day: index for index, day in enumerate(sessions)}
        cursor = db.execute('SELECT symbol,trade_date,open,high,low,close,volume,amount,conflict FROM daily_quotes WHERE trade_date<=? ORDER BY symbol,trade_date', (base,))
        for number, (symbol, group) in enumerate(itertools.groupby(cursor, key=lambda row: row['symbol']), 1):
            rows = [dict(row) for row in group]
            scanned += len(rows)
            data = pd.DataFrame(rows)
            close = pd.to_numeric(data.close, errors='coerce')
            valid = pd.Series([_valid_bar(row) for row in rows])
            index = data.trade_date.map(positions)
            returns = close/close.shift(1)-1
            paired = (valid & valid.shift(1, fill_value=False) & (index-index.shift(1)==1)
                      & np.isfinite(returns) & returns.abs().le(.30))
            market.append(pd.DataFrame({'date': data.loc[paired, 'trade_date'], 'ret': returns[paired]}))
            if len(rows)>=250:
                frame = symbol_features(rows, symbol, sessions,feature_enricher=feature_enricher)
                if not frame.empty:
                    frames.append(frame)
            if number%1000==0:
                print(json.dumps({'probability_symbols_processed': number, 'history_rows_scanned': scanned}), flush=True)
        names = {row['symbol']: row['name'] for row in db.execute('SELECT symbol,name FROM security_master')}
    if not frames:
        raise ValueError('no eligible wide-universe probability history')
    # Do not retain thousands of per-stock frames while constructing additional
    # copies of the wider panel on the bounded-memory cloud runner.
    market_data = pd.concat(market, ignore_index=True, copy=False)
    market.clear()
    gc.collect()
    context = market_data.groupby('date').agg(
        market_mean_return=('ret', 'mean'), market_breadth=('ret', lambda values: float((values>0).mean())))
    del market_data
    gc.collect()
    context['market_return5'] = (1+context.market_mean_return).rolling(5, min_periods=5).apply(np.prod, raw=True)-1
    data = pd.concat(frames, ignore_index=True, copy=False)
    frames.clear()
    gc.collect()
    # Date mapping adds only three numeric columns. A wide merge would copy the
    # entire label/evidence table, even though its row order must stay unchanged.
    for key in context:
        data[key] = data.date.map(context[key])
    data['relative_return5'] = data.ret5-data.market_return5
    # Same fixed formula as action.add_score, applied in place to avoid that
    # helper's defensive whole-frame copy. A regression test compares both.
    clip = lambda values: values.clip(0, 1)
    data['price_action_score'] = (20*clip((data.ret20+.03)/.13)
        + 20*clip((data.relative_return5+.02)/.07)
        + 15*clip(data.efficiency20) + 15*clip(data.close_location)
        + 15*clip((3-data.range_atr)/2) + 15*clip((1.2-data.compression5_20)/.5))
    data.dropna(subset=FEATURES, inplace=True)
    # SQLite already constrained all dates <= base. Detach only the small live
    # subset and reuse the main allocation for history instead of copying it.
    current = data.date.eq(base)
    live = data.loc[current].sort_values('symbol').reset_index(drop=True)
    data.drop(index=data.index[current], inplace=True)
    del current
    data.sort_values(['date', 'symbol'], inplace=True)
    data.reset_index(drop=True, inplace=True)
    history = data
    if history.empty or live.empty:
        raise ValueError('no historical or live wide-universe probability inputs')
    return history, live, names, scanned
