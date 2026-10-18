#!/usr/bin/env python3
"""Transparent, causal price-action research; daily bars do not establish fills."""
from __future__ import annotations

import argparse
from contextlib import closing
import datetime as dt
import hashlib
import itertools
import json
import math
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd

import collector as c
import gross_swing_strategy as gross
from net_profit_strategy import _valid_bar, _one_price, prediction_timing

VERSION = 'price-action-tplus1-research-v1'
PATTERNS = ('compression_breakout', 'trend_pullback', 'higher_lows')
CAP_GAP = .025
FEATURES = ['atr14_pct', 'range_atr', 'close_location', 'body_atr', 'upper_wick_atr',
            'efficiency20', 'compression5_20', 'breakout_atr', 'higher_high_low3',
            'pullback_depth_atr', 'relative_return5', 'price_action_score']
PROTOCOL = {
    'schema': VERSION, 'decision': 'after_T_close', 'features_latest_session': 'T',
    'entry': 'D1_open_reference_if_5_to_10_and_not_above_T_frozen_cap',
    'frozen_buy_cap': 'min(T_close_times_1.025,10)', 'entry_max_gap': CAP_GAP,
    'exit': 'D2_predeclared_target_stop_else_close', 'target': .03, 'stop': .02,
    'same_day_stop_or_sale_allowed': False, 'costs_included': False,
    'fees_cny': 0, 'slippage_bps': 0,
    'entry_lot': 100, 'initial_capital': 30000, 'position_budget': 10000,
    'maximum_positions': 3, 'same_open_cancellation_or_savings_reallocated': False,
    'D2_close_proceeds_usable_at_D2_open': False,
    'one_price_or_missing_execution': 'unresolved_and_capacity_reserved',
    'same_bar_target_stop': 'stop_first_with_target_first_sensitivity',
    'patterns': list(PATTERNS), 'pattern_thresholds_selected_from_diagnostic': False,
    'feature_formulae': {
        'atr14': 'mean_14(max(high-low,abs(high-prev_close),abs(low-prev_close)))',
        'close_location': '(close-low)/(high-low), flat range 0.5',
        'body_atr': '(close-open)/ATR14',
        'upper_wick_atr': '(high-max(open,close))/ATR14',
        'efficiency20': 'abs(close-close_lag20)/sum_20(abs(close-close_lag1))',
        'compression5_20': 'mean(true_range_lag1,5)/mean(true_range_lag1,20)',
        'breakout_atr': '(close-max(high_lag1,20))/ATR14',
        'higher_high_low3': 'mean_3(high>high_lag1 AND low>low_lag1)',
        'relative_return5': 'stock_return5 minus compounded_equal_weight_market_daily_return5',
        'score': '20*trend+20*relative+15*efficiency+15*close_location+15*range_quality+15*compression_quality; each bounded 0..1',
    },
    'common_filter': {'minimum_median_amount20': 20000000, 'atr14_pct': [.008, .05],
                      'market_breadth_min': .4, 'market_mean_return_min': -.01,
                      'relative_return5_min': 0, 'score_min': 55},
    'pattern_rules': {
        'compression_breakout': {'compression5_20_max': .9, 'breakout_atr': [-.2, .5],
            'close_location_min': .65, 'body_atr_min_exclusive': 0,
            'volume_ratio20': [1, 3.5], 'ret1': [-.01, .07]},
        'trend_pullback': {'ret20_min_exclusive': 0, 'ma20_gap': [0, .12],
            'ma5_gap': [-.03, .03], 'ret1': [-.03, .01], 'close_location_min': .45,
            'volume_ratio20_max': 1, 'pullback_depth_atr': [0, 2.5],
            'efficiency20_min': .2},
        'higher_lows': {'higher_high_low3_min': 2/3, 'ret5_min': 0,
            'close_location_min': .6, 'upper_wick_atr_max': .4, 'ma20_gap': [0, .12]},
    },
    'selection_variants': 3, 'selection_min_closed': 20, 'selection_min_entry_dates': 10,
    'selection_requires_positive_expectancy_and_no_unknowns': True,
    'selection_ranking': 'date_cluster_mean_return_95pct_lower_then_win_rate_then_mean',
    'split': [.6, .2, .2], 'purge_horizon': 'D3_outcome_before_next_signal_block',
    'auxiliary_strength_label': 'D1_close>entry AND D2_close>D1_close AND D3_close>D2_close',
    'auxiliary_strength_not_entry_filter': True,
    'all_prior_history_is_exposed_development': True,
    'formal_activation': False, 'claimed_80_percent_win_rate': False,
    'historical_fundflow_used': False,
}


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _finite(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def price_action_features(rows):
    """All feature windows end at the row's own date; labels are absent here."""
    d = pd.DataFrame(rows).sort_values('trade_date').reset_index(drop=True)
    if d.trade_date.duplicated().any():
        raise ValueError('duplicate price-action input dates')
    for key in ('open', 'high', 'low', 'close', 'volume', 'amount'):
        d[key] = pd.to_numeric(d[key], errors='coerce')
    previous = d.close.shift(1)
    tr = pd.concat([d.high-d.low, (d.high-previous).abs(), (d.low-previous).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14, min_periods=14).mean()
    span = d.high-d.low
    extras = pd.DataFrame(index=d.index)
    extras['atr14_pct'] = atr/d.close
    extras['range_atr'] = span/atr
    extras['close_location'] = ((d.close-d.low)/span.replace(0, np.nan)).fillna(.5)
    extras['body_atr'] = (d.close-d.open)/atr
    extras['upper_wick_atr'] = (d.high-d[['open', 'close']].max(axis=1))/atr
    path = d.close.diff().abs().rolling(20, min_periods=20).sum()
    extras['efficiency20'] = ((d.close-d.close.shift(20)).abs()/path.replace(0, np.nan)).fillna(0)
    extras['compression5_20'] = tr.shift(1).rolling(5, min_periods=5).mean()/tr.shift(1).rolling(20, min_periods=20).mean()
    extras['breakout_atr'] = (d.close-d.high.shift(1).rolling(20, min_periods=20).max())/atr
    extras['higher_high_low3'] = ((d.high>d.high.shift(1)) & (d.low>d.low.shift(1))).astype(float).rolling(3, min_periods=3).mean()
    extras['pullback_depth_atr'] = (d.high.shift(1).rolling(10, min_periods=10).max()-d.close)/atr
    extras['ret1'] = d.close/previous-1
    extras['ret5'] = d.close/d.close.shift(5)-1
    extras['ret20'] = d.close/d.close.shift(20)-1
    extras['ma5_gap'] = d.close/d.close.rolling(5, min_periods=5).mean()-1
    extras['ma20_gap'] = d.close/d.close.rolling(20, min_periods=20).mean()-1
    amounts = d.amount.where(np.isfinite(d.amount) & (d.amount>0))
    extras['median_amount20'] = amounts.rolling(20, min_periods=20).median()
    extras['volume_ratio20'] = d.volume/d.volume.shift(1).rolling(20, min_periods=20).mean()
    return extras.replace([np.inf, -np.inf], np.nan)


def add_score(frame):
    """A bounded, predefined confluence score, deliberately not a probability."""
    d = frame.copy()
    clip = lambda series: series.clip(0, 1)
    d['price_action_score'] = (20*clip((d.ret20+.03)/.13)
        + 20*clip((d.relative_return5+.02)/.07)
        + 15*clip(d.efficiency20) + 15*clip(d.close_location)
        + 15*clip((3-d.range_atr)/2) + 15*clip((1.2-d.compression5_20)/.5))
    return d


def pattern_filter(frame, pattern, *, context_required=True):
    common = ((frame.median_amount20>=20000000) & frame.atr14_pct.between(.008, .05)
              & (frame.relative_return5>=0) & (frame.price_action_score>=55))
    if context_required:
        common &= (frame.market_breadth>=.4) & (frame.market_mean_return>=-.01)
    if pattern == 'compression_breakout':
        match = ((frame.compression5_20<=.9) & frame.breakout_atr.between(-.2, .5)
                 & (frame.close_location>=.65) & (frame.body_atr>0)
                 & frame.volume_ratio20.between(1, 3.5) & frame.ret1.between(-.01, .07))
    elif pattern == 'trend_pullback':
        match = ((frame.ret20>0) & frame.ma20_gap.between(0, .12)
                 & frame.ma5_gap.between(-.03, .03) & frame.ret1.between(-.03, .01)
                 & (frame.close_location>=.45) & (frame.volume_ratio20<=1)
                 & frame.pullback_depth_atr.between(0, 2.5) & (frame.efficiency20>=.2))
    elif pattern == 'higher_lows':
        match = ((frame.higher_high_low3>=2/3-1e-12) & (frame.ret5>=0)
                 & (frame.close_location>=.6) & (frame.upper_wick_atr<=.4)
                 & frame.ma20_gap.between(0, .12))
    else:
        raise ValueError('unknown price-action pattern')
    return common & match


def price_action_snapshot(rows, *, market_return5=0, market_breadth=None, market_mean_return=None):
    """Use the same light helper on research and independent email hosts."""
    from price_action_snapshot import price_action_snapshot as snapshot
    return snapshot(rows, market_return5=market_return5, market_breadth=market_breadth,
                    market_mean_return=market_mean_return)


def auxiliary_labels(entry_date, day2_date, day3_date, by_date):
    bars = [by_date.get(day) for day in (entry_date, day2_date, day3_date)]
    if not all(day for day in (entry_date, day2_date, day3_date)) or not all(_valid_bar(row) and not _one_price(row) for row in bars):
        return {'day3_date': day3_date, 'strength3_label': None, 'strength3_status': 'unresolved'}
    first, second, third = bars
    label = int(first['close']>first['open'] and second['close']>first['close'] and third['close']>second['close'])
    return {'day3_date': day3_date, 'strength3_label': label, 'strength3_status': 'resolved'}


def symbol_features(rows, symbol, sessions):
    frame = gross.symbol_features(rows, symbol, sessions)
    if frame.empty:
        return frame
    extras = price_action_features(rows)
    for key in extras:
        frame[key] = extras[key].reindex(frame.index)
    by_date = {row['trade_date']: row for row in rows}
    positions = {day: index for index, day in enumerate(sessions)}
    labels = []
    for row in frame.itertuples():
        index = positions[row.date]
        third = sessions[index+3] if index+3<len(sessions) else None
        labels.append(auxiliary_labels(row.entry_date, row.outcome_date, third, by_date))
    return pd.concat([frame, pd.DataFrame(labels, index=frame.index)], axis=1)


def panel(db_path, base):
    frames, market, scanned = [], [], 0
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA application_id').fetchone()[0] != c.APP_ID:
            raise ValueError('not the original market database')
        if db.execute('SELECT MAX(trade_date) FROM daily_quotes').fetchone()[0] != base:
            raise ValueError('base date differs from database')
        sessions = [r[0] for r in db.execute('SELECT DISTINCT trade_date FROM daily_quotes WHERE trade_date<=? ORDER BY trade_date', (base,))]
        positions = {day: index for index, day in enumerate(sessions)}
        cursor = db.execute('SELECT symbol,trade_date,open,high,low,close,volume,amount,conflict FROM daily_quotes WHERE trade_date<=? ORDER BY symbol,trade_date', (base,))
        for number, (symbol, group) in enumerate(itertools.groupby(cursor, key=lambda row: row['symbol']), 1):
            rows = [dict(row) for row in group]
            scanned += len(rows)
            data = pd.DataFrame(rows)
            valid = pd.Series([_valid_bar(row) for row in rows])
            indices = data.trade_date.map(positions)
            ret = data.close/data.close.shift(1)-1
            paired = valid & valid.shift(1, fill_value=False) & (indices-indices.shift(1)==1) & np.isfinite(ret)
            market.append(pd.DataFrame({'date': data.loc[paired, 'trade_date'], 'ret': ret[paired]}))
            if len(rows)>=250:
                frame = symbol_features(rows, symbol, sessions)
                if not frame.empty:
                    frames.append(frame)
            if number%1000==0:
                print(json.dumps({'price_action_symbols_processed': number, 'history_rows_scanned': scanned}), flush=True)
        names = {r['symbol']: r['name'] for r in db.execute('SELECT symbol,name FROM security_master')}
    if not frames:
        raise ValueError('no eligible price-action history')
    context = pd.concat(market, ignore_index=True).groupby('date').agg(
        market_mean_return=('ret', 'mean'), market_breadth=('ret', lambda x: float((x>0).mean())))
    context['market_return5'] = (1+context.market_mean_return).rolling(5, min_periods=5).apply(np.prod, raw=True)-1
    data = pd.concat(frames, ignore_index=True).merge(context, left_on='date', right_index=True, how='left')
    data['relative_return5'] = data.ret5-data.market_return5
    data = add_score(data).dropna(subset=FEATURES+['market_breadth', 'market_mean_return'])
    history = data[data.date<base].sort_values(['date', 'symbol']).reset_index(drop=True)
    live = data[data.date==base].sort_values('symbol').reset_index(drop=True)
    if history.empty or live.empty:
        raise ValueError('no historical or current price-action inputs')
    return history, live, names, scanned


def chronological_split(history):
    dates = sorted(history.date.unique())
    if len(dates)<150:
        raise ValueError('requires 150 independent historical signal dates')
    cuts = [dates[int(len(dates)*fraction)] for fraction in (.6, .8)]
    result = {}
    lower = None
    for name, upper in zip(('development', 'selection', 'diagnostic'), cuts+[None]):
        keep = pd.Series(True, index=history.index)
        if lower is not None:
            keep &= history.date>=lower
        if upper is not None:
            keep &= (history.date<upper) & history.day3_date.notna() & (history.day3_date<upper)
        result[name] = history[keep].copy().reset_index(drop=True)
        if result[name].empty:
            raise ValueError('empty price-action chronological block')
        lower = upper
    for left, right in zip(('development', 'selection'), ('selection', 'diagnostic')):
        if not result[left].day3_date.max()<result[right].date.min():
            raise ValueError('D3 purging boundary violated')
    return result


def simulate(frame, pattern=None):
    """Cash-conserving T-frozen plans. An unresolved position consumes capacity."""
    records, positions = [], []
    cash, settled_pnl = 30000.0, 0.0

    def check():
        reserved = sum(row['reserved_cash'] for row in positions)
        if cash < -1e-7 or not math.isclose(cash+reserved, 30000+settled_pnl, abs_tol=1e-6):
            raise ValueError('price-action portfolio cash conservation violated')

    for signal_day, daily in frame.groupby('date', sort=True):
        entries = daily.entry_date.dropna()
        entry_day = str(entries.iloc[0]) if len(entries) else None
        remaining = []
        for position in positions:
            if entry_day and position['status']=='closed' and position['exit_date']<entry_day:
                cash += position['sell_cash']; settled_pnl += position['gross_pnl']
            else:
                remaining.append(position)
        positions = remaining
        check()
        if pattern:
            daily = daily[pattern_filter(daily, pattern)]
        daily = daily.sort_values(['price_action_score', 'symbol'], ascending=[False, True])
        selected, planned_cash = 0, cash
        for row in daily.itertuples():
            if selected>=3 or len(positions)>=3:
                break
            if any(position['symbol']==row.symbol for position in positions):
                continue
            budget = min(10000, planned_cash)
            cap = min(float(row.close)*(1+CAP_GAP), 10.0)
            shares = int(budget/cap//100)*100
            if not shares:
                continue
            frozen = shares*cap
            planned_cash -= frozen
            buy = _finite(row.reference_buy_price)
            record = {'signal_date': str(signal_day), 'symbol': row.symbol,
                'entry_date': row.entry_date, 'exit_date': row.outcome_date,
                'price_action_score': float(row.price_action_score),
                'frozen_buy_limit_cap': cap, 'frozen_order_quantity': shares,
                'frozen_budget': budget, 'frozen_capital': frozen,
                'cash_before_entry': cash, 'reference_buy_price': buy,
                'first_day_green': bool(row.first_day_green) if pd.notna(row.first_day_green) else None,
                'day2_close_up': bool(row.day2_close_up) if pd.notna(row.day2_close_up) else None,
                'day2_touch3': bool(row.day2_touch3) if pd.notna(row.day2_touch3) else None,
                'day2_touch6': bool(row.day2_touch6) if pd.notna(row.day2_touch6) else None,
                'strength3_label': int(row.strength3_label) if pd.notna(row.strength3_label) else None,
                'day3_date': row.day3_date, 'execution_verified': False,
                'source': 'daily_ohlc_execution_proxy', 'costs_included': False}
            selected += 1
            if buy is not None and buy>0 and (not 5<=buy<=10 or buy>cap+1e-12):
                records.append({**record, 'status': 'no_fill', 'reason': 'predeclared_open_cap_or_price_range_cancel',
                                'reserved_cash': 0, 'gross_return': None, 'gross_pnl': None})
                continue
            known = bool(row.entry_evidence_valid) and not bool(row.entry_one_price) and buy is not None
            reserved = shares*buy if known else frozen
            if not known or row.outcome_status!='closed':
                record.update(status='unresolved', reason=row.reason, gross_return=None,
                    gross_pnl=None, buy_cash=shares*buy if known else None,
                    cash_reservation_assumption=not known, shares=shares if known else None)
            else:
                result = gross.exit_outcome(buy, row.day2_open, row.day2_high, row.day2_low,
                                           row.day2_close, .03, .02)
                record.update(result, status='closed', reason='daily_ohlc_execution_proxy',
                    shares=shares, buy_cash=shares*buy, sell_cash=shares*result['reference_sell_price'],
                    gross_pnl=shares*(result['reference_sell_price']-buy), cash_reservation_assumption=False)
            cash -= reserved
            record.update(reserved_cash=reserved, cash_after_entry=cash)
            records.append(record); positions.append(record)
            check()
        if not selected:
            records.append({'status': 'abstain', 'signal_date': str(signal_day),
                            'reason': 'no_eligible_signal_or_capacity'})
    return records


def metrics(records, *, bootstrap_samples=2000):
    result = gross.metrics(records, bootstrap_samples=bootstrap_samples)
    attempted = [row for row in records if row['status'] not in ('abstain', 'no_fill')]
    closed = [row for row in attempted if row['status']=='closed']
    gains = [row['gross_return'] for row in closed if row['gross_return']>0]
    losses = [row['gross_return'] for row in closed if row['gross_return']<0]
    returns = [row['gross_return'] for row in closed]
    known3 = [row for row in attempted if row.get('strength3_label') is not None]
    date_returns = {}
    for row in closed:
        date_returns.setdefault(row['entry_date'], []).append(row['gross_return'])
    mean_ci = None
    if date_returns and bootstrap_samples:
        groups = np.array([[sum(values), len(values)] for values in date_returns.values()], dtype=float)
        draws = np.random.default_rng(20261019).integers(0, len(groups), size=(bootstrap_samples, len(groups)))
        totals = groups[draws].sum(axis=1)
        mean_ci = [float(value) for value in np.quantile(totals[:, 0]/totals[:, 1], [.025, .975])]
    equity, peak, drawdown = 30000., 30000., 0.
    curve, loss_streak, max_loss_streak = [], 0, 0
    for row in sorted(closed, key=lambda item: (item['exit_date'], item['symbol'])):
        equity += row['gross_pnl']; peak = max(peak, equity)
        drawdown = max(drawdown, (peak-equity)/peak)
        loss_streak = loss_streak+1 if row['gross_return']<0 else 0
        max_loss_streak = max(max_loss_streak, loss_streak)
        curve.append({'exit_date': row['exit_date'], 'realized_equity_cny': round(equity, 4)})
    if len(curve)>64:
        curve = [curve[int(index)] for index in np.linspace(0, len(curve)-1, 64)]
    result.update(median_closed_gross_return=float(np.median(returns)) if returns else None,
        mean_win_return=float(np.mean(gains)) if gains else None,
        mean_loss_return=float(np.mean(losses)) if losses else None,
        payoff_ratio=float(np.mean(gains)/abs(np.mean(losses))) if gains and losses else None,
        profit_factor=float(sum(gains)/abs(sum(losses))) if losses else None,
        mean_return_date_cluster_ci95=mean_ci, max_consecutive_closed_losses=max_loss_streak,
        realized_equity_end_cny=equity, realized_equity_curve=curve,
        realized_closed_trade_max_drawdown=drawdown,
        equity_excludes_unresolved_mark_to_market=True,
        n_strength3_known=len(known3), n_strength3_unknown=len(attempted)-len(known3),
        strength3_rate=sum(row.get('strength3_label')==1 for row in attempted)/len(attempted) if attempted else None,
        strength3_known_rate=sum(row['strength3_label']==1 for row in known3)/len(known3) if known3 else None,
        strength3_is_separate_from_Tplus1_profit=True)
    return result


def _describe(frame):
    mature = frame.day3_date.dropna()
    return {'rows': len(frame), 'independent_dates': int(frame.date.nunique()),
        'first_signal_date': str(frame.date.min()), 'last_signal_date': str(frame.date.max()),
        'last_D3_outcome_date': str(mature.max()) if len(mature) else None}


def run(db_path, base, output, input_sha256):
    db_path, output = Path(db_path).resolve(), Path(output)
    if dt.date.fromisoformat(base).isoformat()!=base or base>c.now().date().isoformat():
        raise ValueError('invalid or future base date')
    if len(input_sha256)!=64 or any(char not in '0123456789abcdef' for char in input_sha256):
        raise ValueError('invalid input SHA256')
    if gross._hash(db_path)!=input_sha256:
        raise ValueError('price-action input hash mismatch')
    history, live, names, scanned = panel(db_path, base)
    chunks = chronological_split(history)
    selection = []
    for pattern in PATTERNS:
        report = metrics(simulate(chunks['selection'], pattern))
        eligible = (report['n_closed']>=20 and report['independent_entry_dates']>=10
            and report['n_unresolved']==0 and report['mean_gross_return'] is not None
            and report['mean_gross_return']>0)
        selection.append({'pattern': pattern, 'eligible_research': bool(eligible), 'metrics': report})
    eligible = [row for row in selection if row['eligible_research']]
    chosen = max(eligible, key=lambda row: (row['metrics']['mean_return_date_cluster_ci95'][0],
        row['metrics']['win_rate'], row['metrics']['mean_gross_return'])) if eligible else None
    selected_pattern = chosen['pattern'] if chosen else None
    # Frozen selection is complete before diagnostic evaluation; diagnostics never choose a pattern.
    variants = {'unfiltered_price_action_ranking_baseline': simulate(chunks['diagnostic']),
                'selected_policy': simulate(chunks['diagnostic'], selected_pattern) if chosen else []}
    for pattern in PATTERNS:
        variants['fixed_'+pattern] = simulate(chunks['diagnostic'], pattern)
    diagnostic = {name: metrics(rows) for name, rows in variants.items()}
    generated = c.stamp()
    protocol = {'schema': VERSION, 'frozen_at': generated, 'strategy_definition': PROTOCOL}
    definition_hash = hashlib.sha256(json.dumps(PROTOCOL, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    metadata = {'schema': VERSION, 'base_trade_date': base, 'generated_at': generated,
        **prediction_timing(base, generated), 'input_database_sha256': input_sha256,
        'strategy_definition_sha256': definition_hash, 'costs_included': False,
        'formal_trade_ready': False, 'formal_prediction_allowed': False,
        'actual_win_rate': None, 'execution_win_rate_80_validated': False,
        'research_ready': True, 'score_is_probability': False}
    policy = {'active': bool(chosen), 'selected_pattern': selected_pattern,
        'scope': 'research_only', 'selection_frozen_before_diagnostic': True,
        'inactive_reason': None if chosen else 'none_of_three_predeclared_rules_has_positive_selection_expectancy_with_sufficient_evidence'}
    rows = []
    ordered = live.sort_values(['price_action_score', 'symbol'], ascending=[False, True])
    for rank, (index, row) in enumerate(ordered.iterrows(), 1):
        patterns = [pattern for pattern in PATTERNS if bool(pattern_filter(live.loc[[index]], pattern).iloc[0])]
        rows.append({'symbol': row.symbol, 'name': names.get(row.symbol), 'rank': rank,
            'close': float(row.close), **{key: _finite(row[key]) for key in FEATURES},
            'eligible_patterns': patterns, 'research_signal': bool(chosen and selected_pattern in patterns),
            'frozen_buy_limit_cap': min(float(row.close)*(1+CAP_GAP), 10),
            'planned_target': .03, 'planned_stop': .02, 'exit_session': 'D2',
            'formal_trade_ready': False, 'score_is_probability': False})
    watchlist = [row for row in rows if row['eligible_patterns']][:10]
    summary = {**metadata, 'protocol': protocol, 'policy': policy,
        'split': {name: _describe(part) for name, part in chunks.items()},
        'selection_rules': selection, 'diagnostic': diagnostic,
        'history_rows_scanned': scanned, 'history_signal_rows': len(history),
        'prediction_count': len(rows), 'current_watchlist': watchlist,
        'formal_candidates': [], 'historical_fundflow_used': False,
        'validation_origin': 'previously_exposed_development_diagnostic_not_fresh_holdout',
        'auxiliary_label': PROTOCOL['auxiliary_strength_label'],
        'limitations': [
            'A score from 0 to 100 expresses technical confluence, not a probability or win rate.',
            'Price-action patterns are systematic hypotheses; overseas literature does not validate this A-share strategy.',
            'The three patterns and their thresholds are fixed; only selection-block outcomes choose a research pattern.',
            'All historical data were previously exposed; the final block is development diagnostic, not fresh holdout evidence.',
            'Funds and present-day sector memberships are absent from this historical backtest; no historical flow confirmation is invented.',
            'Unadjusted prices, present vendor census, uncertified historical status/limits/actions and survivorship remain limitations.',
            'Daily OHLC cannot demonstrate auction or intraday executable fills; unknown trades reserve capacity.',
            'T+1 forbids D1 risk exits; D2 opening gaps may exceed the declared 2 percent stop.',
            'The three-day strength label is separate from D2 trade profit and is never used as a realized-entry filter.',
            'Three-day strengthening and an 80 percent actual win rate are unverified; formal trading stays disabled.',
        ],
        'method_sources': [
            {'title': 'Lo, Mamaysky and Wang (2000), Foundations of Technical Analysis',
             'url': 'https://www.nber.org/papers/w7613', 'scope': 'systematic pattern hypothesis, US data; no claimed A-share transfer'},
            {'title': 'CME Group, Support and Resistance',
             'url': 'https://www.cmegroup.com/education/courses/trading-and-analysis/support-and-resistance',
             'scope': 'prior highs/lows as testable reference levels, not guaranteed support'},
        ]}
    if gross._hash(db_path)!=input_sha256:
        raise ValueError('price-action source changed during research')
    output.mkdir(parents=True, exist_ok=False)
    (output/'summary.json').write_bytes(_json_bytes(summary))
    (output/'protocol.json').write_bytes(_json_bytes(protocol))
    (output/'predictions.json').write_bytes(_json_bytes({**metadata, 'policy': policy, 'rows': rows}))
    live[['symbol', 'date']+FEATURES].to_csv(output/'prediction_inputs.csv.gz', index=False, compression={'method': 'gzip', 'mtime': 0})
    trades = [dict(row, audit_variant=name) for name, records in variants.items() for row in records]
    pd.DataFrame(trades).to_csv(output/'diagnostic_trades.csv.gz', index=False, compression={'method': 'gzip', 'mtime': 0})
    print(json.dumps({**metadata, 'policy': policy, 'diagnostic_counts': {name: {
        key: value[key] for key in ('n_attempts', 'n_closed', 'n_unresolved', 'win_rate', 'ge3_rate', 'mean_closed_gross_return', 'strength3_rate')}
        for name, value in diagnostic.items()}}, ensure_ascii=False), flush=True)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True); parser.add_argument('--base', required=True)
    parser.add_argument('--output', required=True); parser.add_argument('--input-sha256', required=True)
    args = parser.parse_args()
    run(args.db, args.base, args.output, args.input_sha256)
