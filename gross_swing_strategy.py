#!/usr/bin/env python3
"""Causal overnight gross-return research. Daily bars never establish real fills."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import itertools
import json
import math
from pathlib import Path
import sqlite3

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from threadpoolctl import threadpool_limits

import collector as c
import formal_price_model as price_model
from net_profit_strategy import _valid_bar, _one_price, prediction_timing, split

VERSION = 'gross-swing-research-v2'
BUDGET = 10000.0
INITIAL_CAPITAL = 30000.0
FEATURES = price_model.FEATURES + [
    'median_amount20', 'market_mean_return', 'market_breadth', 'candle_body',
    'close_location', 'upper_shadow', 'amount_ratio20', 'range_ratio20']
LABELS = {'p_profit': 'label_profit', 'p_ge3': 'label_ge3', 'p_ge6': 'label_ge6',
          'p_day1_green': 'label_day1_green'}
MODEL_PARAMETERS = {'max_iter': 80, 'max_leaf_nodes': 15, 'min_samples_leaf': 100,
                    'learning_rate': .06, 'l2_regularization': 5,
                    'random_state': 20261018, 'early_stopping': False}
FALLBACK = {'entry_rule': 'ordinary', 'target': .03, 'stop': .02, 'threshold': .5}
PROTOCOL = {
    'schema': VERSION, 'objective': 'gross_profit_3_to_6_percent',
    'decision': 'after_T_close', 'entry': 'D1_next_observed_session_open_reference',
    'exit': 'D2_following_observed_session_predeclared_target_stop_else_close',
    'first_day_green_required': False,
    'first_day_green_diagnostic_only': True,
    'costs_included': False, 'fees_cny': 0, 'slippage_bps': 0,
    'gross_target': {'profitable_trade': 'sell_price_strictly_above_buy_price',
                     'target_returns': [.03, .045, .06],
                     'target_win_rate_strictly_greater_than': .8},
    'entry_reference_price_range': [5, 10], 'entry_max_gap_over_T_close': .03,
    'frozen_at_T_buy_limit_cap': 'min(T_close_times_1.03,10.00)',
    'quantity_rule': 'whole_100_share_lots_affordable_at_T_frozen_buy_cap_before_D1_open',
    'same_signal_day_budget_rule': 'deduct_frozen_quantity_times_cap_from_planned_cash_before_open_evidence',
    'same_day_open_price_savings_or_cancellations_reallocated': False,
    'entry_cancellation': 'D1_open_above_frozen_buy_limit_cap_or_open_reference_below_5',
    'entry_open_below_5_filter_is_auction_execution_evidence': False,
    'opening_reference_is_actual_auction_fill': False,
    'entry_cancelled_not_wins_or_losses_but_planned_signals_retained': True,
    'same_day_exit': False, 'profit_targets': [.03, .045, .06], 'stop_losses': [.02, .03],
    'target_rounding': 'up_to_next_0.01_cny', 'stop_rounding': 'down_to_next_0.01_cny',
    'exit_gap_rule': 'D2_open_beyond_target_or_stop_exits_at_open_reference',
    'same_bar_target_stop': 'stop_first_conservative_and_target_first_optimistic_sensitivity',
    'daily_touch_is_actual_execution': False,
    'one_price_or_missing_bars': 'unresolved_selected_attempt_reserves_capacity_no_retroactive_removal',
    'initial_capital_cny': INITIAL_CAPITAL, 'position_budget_cny': BUDGET,
    'external_cash_topups': False, 'entry_lot_shares': 100, 'maximum_positions': 3,
    'exit_cash_unavailable_at_same_session_open': True,
    'entry_rules': ['ordinary', 'bullish', 'pullback'],
    'common_filter': {'median_amount20_min': 20000000, 'volatility20_max': .05,
                      'market_breadth_min': .35, 'market_mean_return_min': -.015},
    'ordinary_filter': {'ret1': [-.04, .05], 'abs_candle_body_max': .04,
                        'range_max': .09, 'close_location': [.35, .9]},
    'bullish_filter': {'ret1': [0, .05], 'candle_body_min_exclusive': 0,
                       'range_max': .09, 'close_location': [.55, .95]},
    'pullback_filter': {'ret1': [-.04, .01], 'ma20_gap': [-.05, .08],
                        'ret20_min': 0, 'close_location': [.35, .9]},
    'selection_thresholds': [.5, .6, .7, .8], 'threshold_endpoint': 'p_profit',
    'ranking_score': '0.6*p_profit+0.4*p_ge3',
    'selection_grid_size': 72, 'research_selection_min_trades': 20,
    'research_selection_min_entry_dates': 10,
    'research_selection_score': 'maximize_binomial_Wilson_lower_then_ge3_rate_then_mean_return',
    'research_selection_score_is_independent_date_confidence_bound': False,
    'fallback_parameters': FALLBACK,
    'split_fractions': [.5, .15, .15, .2],
    'purge': 'prior_D2_outcome_date_before_next_signal_date',
    'diagnostic_may_select_parameters': False,
    'historical_test_posture': 'previously_exposed_development_diagnostic_not_fresh_holdout',
    'formal_min_trades': 200, 'formal_min_entry_dates': 60,
    'formal_date_block_95pct_lower_strictly_above': .8,
    'official_pit_limits_status_actions_and_actual_execution_required': True,
    'features': FEATURES, 'label_endpoints': {
        'p_profit': 'D2_close_over_D1_open_strictly_positive',
        'p_ge3': 'D2_close_over_D1_open_at_least_0.03',
        'p_ge6': 'D2_close_over_D1_open_at_least_0.06',
        'p_day1_green': 'D1_close_over_D1_open_strictly_positive'},
    'probabilities_are_not_policy_exit_win_probabilities': True,
    'estimator': 'HistGradientBoostingClassifier', 'estimator_parameters': MODEL_PARAMETERS,
    'calibrator': 'IsotonicRegression_out_of_bounds_clip',
}


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def strategy_definition_sha256():
    return hashlib.sha256(json.dumps(PROTOCOL, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def label_outcome(signal_date, symbol, entry_date, exit_date, entry, exit_, signal_close):
    """Forward prices are labels/evidence only, never contemporaneous model features."""
    result = {'entry_date': entry_date, 'outcome_date': exit_date,
              'outcome_status': 'unresolved', 'reason': 'future_market_session_not_observed',
              'reference_buy_price': _number(entry.get('open')) if entry else None,
              'day1_close': _number(entry.get('close')) if entry else None,
              'entry_evidence_valid': False, 'exit_evidence_valid': False,
              'entry_one_price': False, 'exit_one_price': False,
              'day2_open': None, 'day2_high': None, 'day2_low': None, 'day2_close': None,
              'first_day_green': None, 'day2_close_up': None,
              'day2_touch3': None, 'day2_touch6': None,
              'label_profit': None, 'label_ge3': None, 'label_ge6': None,
              'label_day1_green': None}
    if not entry_date:
        return result
    if not _valid_bar(entry):
        result['reason'] = 'missing_invalid_suspended_or_conflicting_entry_bar'
        return result
    result['entry_evidence_valid'] = True
    result['entry_one_price'] = _one_price(entry)
    buy = float(entry['open'])
    result['first_day_green'] = bool(float(entry['close']) > buy)
    if result['entry_one_price']:
        result['reason'] = 'one_price_entry_execution_unresolved'
        return result
    result['label_day1_green'] = int(result['first_day_green'])
    if not exit_date:
        return result
    if not _valid_bar(exit_):
        result['reason'] = 'missing_invalid_suspended_or_conflicting_exit_bar'
        return result
    result['exit_evidence_valid'] = True
    result['exit_one_price'] = _one_price(exit_)
    for key in ('open', 'high', 'low', 'close'):
        result['day2_' + key] = float(exit_[key])
    result['day2_touch3'] = bool(float(exit_['high']) >= buy * 1.03 - 1e-12)
    result['day2_touch6'] = bool(float(exit_['high']) >= buy * 1.06 - 1e-12)
    result['day2_close_up'] = bool(float(exit_['close']) > float(entry['close']))
    if result['exit_one_price']:
        result['reason'] = 'one_price_exit_execution_unresolved'
        return result
    ret = float(exit_['close']) / buy - 1
    result.update(outcome_status='closed', reason='daily_open_close_label_proxy',
                  label_profit=int(ret > 0), label_ge3=int(ret >= .03 - 1e-12),
                  label_ge6=int(ret >= .06 - 1e-12))
    return result


def symbol_features(rows, symbol, sessions):
    index = {day: i for i, day in enumerate(sessions)}
    frame = price_model.features(rows, symbol, index)
    if frame.empty:
        return frame
    data = pd.DataFrame(rows).sort_values('trade_date').reset_index(drop=True)
    amounts = pd.to_numeric(data.amount, errors='coerce')
    amounts = amounts.where(np.isfinite(amounts) & (amounts > 0))
    amplitude = (data.high - data.low) / data.close.shift(1)
    extras = pd.DataFrame(index=data.index)
    extras['median_amount20'] = amounts.rolling(20, min_periods=20).median()
    extras['candle_body'] = data.close / data.open - 1
    span = data.high - data.low
    extras['close_location'] = ((data.close - data.low) / span.replace(0, np.nan)).fillna(.5)
    extras['upper_shadow'] = (data.high - data[['open', 'close']].max(axis=1)) / data.close
    extras['amount_ratio20'] = amounts / amounts.shift(1).rolling(20, min_periods=20).mean()
    extras['range_ratio20'] = amplitude / amplitude.shift(1).rolling(20, min_periods=20).median()
    frame = frame.drop(columns=['outcome_date', 'forward_return', 'label_valid', 'P1', 'P2'])
    frame = pd.concat([frame, extras.reindex(frame.index)], axis=1)
    by_date = {r['trade_date']: r for r in rows}
    labels = []
    for row in frame.itertuples():
        pos = index[row.date]
        entry = sessions[pos + 1] if pos + 1 < len(sessions) else None
        exit_ = sessions[pos + 2] if pos + 2 < len(sessions) else None
        labels.append(label_outcome(row.date, symbol, entry, exit_, by_date.get(entry),
                                    by_date.get(exit_), row.close))
    result = pd.concat([frame, pd.DataFrame(labels, index=frame.index)], axis=1)
    return result.replace([np.inf, -np.inf], np.nan)


def panel(db_path, base):
    frames, market = [], []
    path = Path(db_path).resolve()
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA application_id').fetchone()[0] != c.APP_ID:
            raise ValueError('not original market database')
        if db.execute('SELECT MAX(trade_date) FROM daily_quotes').fetchone()[0] != base:
            raise ValueError('database date differs from requested base')
        sessions = [r[0] for r in db.execute('SELECT DISTINCT trade_date FROM daily_quotes WHERE trade_date<=? ORDER BY trade_date', (base,))]
        market_index = {day: i for i, day in enumerate(sessions)}
        cursor = db.execute('SELECT symbol,trade_date,open,high,low,close,volume,amount,conflict FROM daily_quotes WHERE trade_date<=? ORDER BY symbol,trade_date', (base,))
        scanned = symbols = 0
        for symbol, group in itertools.groupby(cursor, key=lambda r: r['symbol']):
            rows = [dict(r) for r in group]
            scanned += len(rows); symbols += 1
            data = pd.DataFrame(rows)
            positions = data.trade_date.map(market_index)
            values = pd.to_numeric(data.close, errors='coerce')
            valid = pd.Series([_valid_bar(r) for r in rows])
            ret = values / values.shift(1) - 1
            valid_ret = valid & valid.shift(1, fill_value=False) & (positions - positions.shift(1) == 1) & np.isfinite(ret)
            market.append(pd.DataFrame({'date': data.loc[valid_ret, 'trade_date'], 'ret': ret[valid_ret]}))
            if len(rows) >= 250:
                frame = symbol_features(rows, symbol, sessions)
                if not frame.empty:
                    frames.append(frame)
            if symbols % 1000 == 0:
                print(json.dumps({'symbols_processed': symbols, 'history_rows_scanned': scanned}), flush=True)
        names = {r['symbol']: r['name'] for r in db.execute('SELECT symbol,name FROM security_master')}
    if not frames:
        raise ValueError('no eligible real history')
    context = pd.concat(market, ignore_index=True).groupby('date').agg(
        market_mean_return=('ret', 'mean'), market_breadth=('ret', lambda x: float((x > 0).mean())))
    data = pd.concat(frames, ignore_index=True).merge(context, left_on='date', right_index=True, how='left')
    # HGBT accepts missing liquidity inputs; missing liquidity fails entry filters.
    data = data.dropna(subset=[key for key in FEATURES if key not in ('median_amount20', 'amount_ratio20')])
    history = data[data.date < base].sort_values(['date', 'symbol']).reset_index(drop=True)
    live = data[data.date == base].sort_values('symbol').reset_index(drop=True)
    if history.empty or live.empty:
        raise ValueError('no historical research or current prediction inputs')
    return history, live, names, scanned


def describe(frame):
    mature = frame.outcome_date.dropna()
    return {'rows': len(frame), 'labeled_closed_rows': int((frame.outcome_status == 'closed').sum()),
            'unresolved_rows': int((frame.outcome_status != 'closed').sum()),
            'independent_dates': int(frame.date.nunique()), 'first_input_date': str(frame.date.min()),
            'last_input_date': str(frame.date.max()), 'last_outcome_date': str(mature.max()) if len(mature) else None}


def entry_filter(frame, rule):
    common = ((frame.median_amount20 >= 20000000) & (frame.volatility20 <= .05)
              & (frame.market_breadth >= .35) & (frame.market_mean_return >= -.015))
    if rule == 'ordinary':
        pattern = (frame.ret1.between(-.04, .05) & (frame.candle_body.abs() <= .04)
                   & (frame['range'] <= .09) & frame.close_location.between(.35, .9))
    elif rule == 'bullish':
        pattern = (frame.ret1.between(0, .05) & (frame.candle_body > 0)
                   & (frame['range'] <= .09) & frame.close_location.between(.55, .95))
    elif rule == 'pullback':
        pattern = (frame.ret1.between(-.04, .01) & frame.ma20_gap.between(-.05, .08)
                   & (frame.ret20 >= 0) & frame.close_location.between(.35, .9))
    else:
        raise ValueError('unknown pre-entry rule')
    return common & pattern


def exit_outcome(buy, day2_open, day2_high, day2_low, day2_close, target, stop):
    """Conservative OHLC proxy, with explicit ambiguity sensitivity; never a fill claim."""
    if not (0 < target < 1 and 0 < stop < 1 and buy > 0):
        raise ValueError('invalid price/target/stop')
    tp = math.ceil(round(buy * (1 + target) * 100, 10)) / 100
    sl = math.floor(round(buy * (1 - stop) * 100, 10)) / 100
    ambiguous = False
    if day2_open >= tp:
        sell, optimistic, reason = day2_open, day2_open, 'target_gap_open_reference'
    elif day2_open <= sl:
        sell, optimistic, reason = day2_open, day2_open, 'stop_gap_open_reference'
    elif day2_high >= tp and day2_low <= sl:
        sell, optimistic, reason = sl, tp, 'ambiguous_same_bar_stop_first'
        ambiguous = True
    elif day2_low <= sl:
        sell, optimistic, reason = sl, sl, 'stop_touch_reference'
    elif day2_high >= tp:
        sell, optimistic, reason = tp, tp, 'target_touch_reference'
    else:
        sell, optimistic, reason = day2_close, day2_close, 'D2_close_reference'
    return {'reference_sell_price': sell, 'optimistic_sell_price': optimistic,
            'gross_return': sell / buy - 1, 'optimistic_gross_return': optimistic / buy - 1,
            'ambiguous_same_bar': ambiguous, 'exit_reason': reason,
            'target_price': tp, 'stop_price': sl, 'execution_verified': False,
            'source': 'daily_ohlc_execution_proxy', 'fees_cny': 0, 'slippage_bps': 0}


def simulate(frame, parameters=None, *, baseline=False):
    """Preselect before D1; unknown executions reserve funds instead of vanishing."""
    parameters = dict(FALLBACK if parameters is None else parameters)
    records, positions = [], []
    cash, settled_pnl = INITIAL_CAPITAL, 0.0
    frame = frame.copy()
    frame['_rank_score'] = .6 * frame.p_profit + .4 * frame.p_ge3

    def check_cash():
        reserved = sum(row['reserved_cash'] for row in positions)
        if cash < -1e-7 or not math.isclose(cash + reserved, INITIAL_CAPITAL + settled_pnl,
                                          rel_tol=1e-10, abs_tol=1e-7):
            raise ValueError('portfolio cash conservation violated')

    for signal_day, daily in frame.groupby('date', sort=True):
        entries = daily.entry_date.dropna()
        entry_day = str(entries.iloc[0]) if len(entries) else None
        if entry_day:
            remaining = []
            for position in positions:
                if position['status'] == 'closed' and position['exit_date'] < entry_day:
                    cash += position['sell_cash']; settled_pnl += position['gross_pnl']
                else:
                    remaining.append(position)
            positions = remaining
            check_cash()
        if not baseline:
            daily = daily[(daily.p_profit >= parameters['threshold'])
                          & entry_filter(daily, parameters['entry_rule'])]
        daily = daily.sort_values(['_rank_score', 'symbol'], ascending=[False, True])
        selected = 0
        # Freeze all concurrent order quantities from T's cash and limit caps. D1
        # price improvements or cancellations cannot finance another T-time order.
        planned_cash = cash
        for row in daily.itertuples():
            if selected >= 3 or len(positions) >= 3:
                break
            if any(p['symbol'] == row.symbol for p in positions):
                continue
            budget = min(BUDGET, planned_cash)
            if budget <= 0:
                break
            cap = min(float(row.close) * 1.03, 10.0)
            shares = int(budget / cap // 100) * 100
            if shares == 0:
                continue
            frozen_capital = shares * cap
            planned_cash -= frozen_capital
            buy = _number(row.reference_buy_price)
            common = {'signal_date': str(signal_day), 'symbol': row.symbol,
                      'entry_date': row.entry_date, 'exit_date': row.outcome_date,
                      'p_profit': float(row.p_profit), 'p_ge3': float(row.p_ge3),
                      'p_ge6': float(row.p_ge6), 'p_day1_green': float(row.p_day1_green),
                      'cash_before_entry': cash, 'reference_buy_price': buy,
                      'frozen_buy_limit_cap': cap, 'frozen_budget': budget,
                      'frozen_order_quantity': shares, 'frozen_capital': frozen_capital,
                      'first_day_green': bool(row.first_day_green) if pd.notna(row.first_day_green) else None,
                      'day2_close_up': bool(getattr(row, 'day2_close_up', None))
                          if pd.notna(getattr(row, 'day2_close_up', None)) else None,
                      'day2_touch3': bool(row.day2_touch3) if pd.notna(row.day2_touch3) else None,
                      'day2_touch6': bool(row.day2_touch6) if pd.notna(row.day2_touch6) else None,
                      'execution_verified': False, 'source': 'daily_ohlc_execution_proxy',
                      'fees_cny': 0, 'slippage_bps': 0, 'initial_capital': INITIAL_CAPITAL}
            # Only the opening reference and T's close can cancel a predeclared order.
            if buy is not None and buy > 0 and (not 5 <= buy <= 10 or buy > row.close * 1.03 + 1e-12):
                records.append({**common, 'status': 'no_fill', 'reason': 'frozen_buy_cap_or_open_reference_price_range_cancel',
                                'gross_return': None, 'gross_pnl': None, 'reserved_cash': 0})
                selected += 1
                continue
            known_entry = bool(row.entry_evidence_valid) and not bool(row.entry_one_price) and shares is not None
            reserved = shares * buy if known_entry else frozen_capital
            if row.outcome_status != 'closed' or not known_entry:
                record = {**common, 'status': 'unresolved', 'reason': row.reason,
                          'gross_return': None, 'gross_pnl': None, 'shares': shares if known_entry else None,
                          'cash_reservation_assumption': not known_entry,
                          'buy_cash': shares * buy if known_entry else None}
            else:
                result = exit_outcome(buy, row.day2_open, row.day2_high, row.day2_low,
                                      row.day2_close, parameters['target'], parameters['stop'])
                record = {**common, **result, 'status': 'closed', 'reason': 'daily_ohlc_execution_proxy',
                          'shares': shares, 'buy_cash': shares * buy,
                          'sell_cash': shares * result['reference_sell_price'],
                          'gross_pnl': shares * (result['reference_sell_price'] - buy),
                          'cash_reservation_assumption': False}
            cash -= reserved
            record.update(reserved_cash=reserved, cash_after_entry=cash)
            records.append(record); positions.append(record); selected += 1
            check_cash()
        if not selected:
            records.append({'status': 'abstain', 'signal_date': str(signal_day),
                            'reason': 'no_eligible_signal_or_capacity'})
    return records


def _wilson_lower(wins, n):
    if not n:
        return 0.0
    z = 1.959963984540054
    p = wins / n
    return (p + z*z/(2*n) - z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / (1 + z*z/n)


def metrics(records, *, bootstrap_samples=2000):
    signals = [row for row in records if row['status'] != 'abstain']
    attempted = [row for row in signals if row['status'] != 'no_fill']
    closed = [row for row in attempted if row['status'] == 'closed']
    n = len(attempted)
    wins = sum(row['gross_return'] > 0 for row in closed)
    green = [row for row in attempted if row.get('first_day_green') is True]
    not_green = [row for row in attempted if row.get('first_day_green') is False]
    known_green = [row for row in attempted if row.get('first_day_green') is not None]

    def proportion(predicate, rows=attempted):
        return sum(predicate(row) for row in rows) / len(rows) if rows else None

    def profit_at_least(row, target):
        return row['status'] == 'closed' and row['gross_return'] >= target - 1e-12

    by_date = {}
    for row in attempted:
        key = row.get('entry_date') or 'unobserved:' + row['signal_date']
        by_date.setdefault(key, [0, 0]); by_date[key][0] += 1
        by_date[key][1] += int(row['status'] == 'closed' and row['gross_return'] > 0)
    ci = None
    if by_date and bootstrap_samples:
        groups = np.asarray(list(by_date.values()), dtype=float)
        rng = np.random.default_rng(20261018)
        draws = rng.integers(0, len(groups), size=(bootstrap_samples, len(groups)))
        totals = groups[draws].sum(axis=1)
        ci = [float(x) for x in np.quantile(totals[:, 1] / totals[:, 0], [.025, .975])]
    returns = [row['gross_return'] for row in closed]
    return {'n_signals': len(signals), 'n_cancelled': len(signals) - n,
            'n_attempts': n, 'n_selected_attempts': n, 'n_closed': len(closed),
            'n_entry_reference_known': sum(row.get('buy_cash') is not None for row in attempted),
            'n_unresolved': n - len(closed), 'n_wins': wins,
            'n_losses': sum(row['gross_return'] < 0 for row in closed),
            'n_flat': sum(row['gross_return'] == 0 for row in closed),
            'independent_entry_dates': len(by_date),
            'n_abstain_dates': sum(row['status'] == 'abstain' for row in records),
            'win_rate': wins / n if n else None,
            'closed_win_rate': wins / len(closed) if closed else None,
            'ge3_rate': proportion(lambda row: profit_at_least(row, .03)),
            'ge6_rate': proportion(lambda row: profit_at_least(row, .06)),
            'day2_touch3_rate': proportion(lambda row: row.get('day2_touch3') is True),
            'day2_touch6_rate': proportion(lambda row: row.get('day2_touch6') is True),
            'n_known_touch': sum(row.get('day2_touch3') is not None for row in attempted),
            'first_day_green_rate': len(green) / n if n else None,
            'n_known_first_day': len(known_green),
            'first_day_green_conditional': {
                'n': len(green), 'win_rate': proportion(
                    lambda row: row['status'] == 'closed' and row['gross_return'] > 0, green),
                'ge3_rate': proportion(lambda row: profit_at_least(row, .03), green),
                'ge6_rate': proportion(lambda row: profit_at_least(row, .06), green)},
            'first_day_not_green_conditional': {
                'n': len(not_green), 'win_rate': proportion(
                    lambda row: row['status'] == 'closed' and row['gross_return'] > 0, not_green),
                'ge3_rate': proportion(lambda row: profit_at_least(row, .03), not_green),
                'ge6_rate': proportion(lambda row: profit_at_least(row, .06), not_green)},
            'day2_close_up_rate': proportion(lambda row: row.get('day2_close_up') is True),
            'mean_gross_return': float(np.mean(returns)) if closed and len(closed) == n else None,
            'mean_closed_gross_return': float(np.mean(returns)) if closed else None,
            'gross_pnl_cny': float(sum(row['gross_pnl'] for row in closed)),
            'date_block_bootstrap': {'win_rate_ci95': ci, 'samples': bootstrap_samples,
                                     'unit': 'entry_date', 'unresolved_count_as_nonwins': True},
            'selection_score_wilson_lower': _wilson_lower(wins, n),
            'ambiguity_count': sum(bool(row.get('ambiguous_same_bar')) for row in closed),
            'optimistic_win_rate_upper_proxy': proportion(
                lambda row: row['status'] == 'closed' and row['optimistic_gross_return'] > 0),
            'denominator': 'closed_plus_unresolved_selected_attempts_excludes_predeclared_open_cancellations',
            'costs_included': False, 'actual_win_rate': None,
            'formal_trade_ready': False}


def predict(bundle, frame):
    result = {}
    with threadpool_limits(limits=2):
        for endpoint, parts in bundle['models'].items():
            raw = parts['estimator'].predict_proba(frame[FEATURES])[:, 1]
            result[endpoint] = np.asarray(parts['calibrator'].predict(raw))
    result['p_ge3'] = np.minimum(result['p_ge3'], result['p_profit'])
    result['p_ge6'] = np.minimum(result['p_ge6'], result['p_ge3'])
    if any(not np.isfinite(values).all() or (values < 0).any() or (values > 1).any()
           for values in result.values()):
        raise ValueError('invalid calibrated probability')
    return result


def evaluate(frame, probabilities, calibration):
    result = {}
    for endpoint, label in LABELS.items():
        available = frame[label].notna()
        y = frame.loc[available, label].to_numpy(dtype=float)
        p = probabilities[endpoint][available.to_numpy()]
        baseline = float(calibration[label].mean())
        bins = []
        for lo in range(0, 100, 10):
            mask = (p >= lo/100) & (p <= 1 if lo == 90 else p < (lo+10)/100)
            if mask.any():
                bins.append({'lower': lo/100, 'upper': (lo+10)/100, 'n': int(mask.sum()),
                             'mean_prediction': float(p[mask].mean()), 'observed_frequency': float(y[mask].mean())})
        result[endpoint] = {'label': PROTOCOL['label_endpoints'][endpoint], 'n': len(y),
                            'actual_frequency': float(y.mean()) if len(y) else None,
                            'predicted_mean': float(p.mean()) if len(p) else None,
                            'brier': float(np.mean((p-y)**2)) if len(y) else None,
                            'calibration_constant_baseline_brier': float(np.mean((baseline-y)**2)) if len(y) else None,
                            'probability_bins': bins}
    return result


def run(db_path, base, output, input_sha256):
    db_path, output = Path(db_path).resolve(), Path(output)
    if dt.date.fromisoformat(base).isoformat() != base or base > c.now().date().isoformat():
        raise ValueError('invalid or future base date')
    if len(input_sha256) != 64 or any(ch not in '0123456789abcdef' for ch in input_sha256):
        raise ValueError('input SHA256 must be lowercase hexadecimal')
    if _hash(db_path) != input_sha256:
        raise ValueError('input database hash mismatch')
    history, live, names, scanned = panel(db_path, base)
    chunks = split(history)
    bundle = {'version': VERSION, 'features': FEATURES, 'models': {}}
    for endpoint, label in LABELS.items():
        train = chunks['train'][chunks['train'][label].notna()]
        calibration = chunks['calibration'][chunks['calibration'][label].notna()]
        if any(len(part) < 1000 or part.date.nunique() < 30 or part[label].nunique() != 2
               or (part[label] == 1).sum() < 30 or (part[label] == 0).sum() < 30
               for part in (train, calibration)):
            raise ValueError('insufficient chronological endpoint labels: ' + endpoint)
        estimator = HistGradientBoostingClassifier(**MODEL_PARAMETERS)
        with threadpool_limits(limits=2):
            estimator.fit(train[FEATURES], train[label].astype(int))
            raw = estimator.predict_proba(calibration[FEATURES])[:, 1]
        calibrator = IsotonicRegression(y_min=0, y_max=1, out_of_bounds='clip').fit(raw, calibration[label])
        bundle['models'][endpoint] = {'estimator': estimator, 'calibrator': calibrator}
        print(json.dumps({'endpoint_trained': endpoint}), flush=True)
    probabilities = {}
    for key in ('selection', 'diagnostic'):
        probabilities[key] = predict(bundle, chunks[key])
        for endpoint, values in probabilities[key].items():
            chunks[key][endpoint] = values
    live_p = predict(bundle, live)
    for endpoint, values in live_p.items():
        live[endpoint] = values
    grid = []
    for rule, target, stop, threshold in itertools.product(PROTOCOL['entry_rules'],
            PROTOCOL['profit_targets'], PROTOCOL['stop_losses'], PROTOCOL['selection_thresholds']):
        parameters = {'entry_rule': rule, 'target': target, 'stop': stop, 'threshold': threshold}
        report = metrics(simulate(chunks['selection'], parameters), bootstrap_samples=0)
        eligible = (report['n_closed'] >= 20 and report['independent_entry_dates'] >= 10
                    and report['n_unresolved'] == 0 and report['mean_gross_return'] is not None
                    and report['mean_gross_return'] > 0)
        keep = ('n_signals', 'n_cancelled', 'n_attempts', 'n_closed', 'n_unresolved', 'n_wins',
                'independent_entry_dates', 'win_rate', 'ge3_rate', 'ge6_rate',
                'mean_gross_return', 'selection_score_wilson_lower')
        grid.append({'parameters': parameters, 'eligible_research': bool(eligible),
                     'score': report['selection_score_wilson_lower'],
                     'metrics': {key: report[key] for key in keep}})
    eligible = [row for row in grid if row['eligible_research']]
    chosen = max(eligible, key=lambda row: (row['score'], row['metrics']['ge3_rate'],
                                           row['metrics']['mean_gross_return'])) if eligible else None
    parameters = chosen['parameters'] if chosen else dict(FALLBACK)
    policy = {'active': bool(chosen), 'selected_parameters': chosen['parameters'] if chosen else None,
              'fallback_parameters': dict(FALLBACK), 'diagnostic_parameters': parameters,
              'inactive_reason': None if chosen else 'insufficient_selection_evidence',
              'selection_only': True, 'selected_before_diagnostic_evaluation': True,
              'active_scope': 'research_only_never_formal_trade_authorization',
              'historical_80_percent_requirement_passed': False}
    diagnostic = chunks['diagnostic']
    variants = {'baseline': simulate(diagnostic, FALLBACK, baseline=True),
                'selected_policy': simulate(diagnostic, parameters) if chosen else [],
                'fixed_reference': simulate(diagnostic, FALLBACK)}
    reports = {key: metrics(records) for key, records in variants.items()}
    generated = c.stamp()
    protocol = {'schema': VERSION, 'strategy_definition': PROTOCOL,
                'frozen_at': generated, 'historical_development_cutoff': base}
    protocol_sha = hashlib.sha256(_json_bytes(protocol)).hexdigest()
    metadata = {'schema': VERSION, 'strategy_version': VERSION,
                'objective': 'gross_profit_3_to_6_percent', 'gross_target': PROTOCOL['gross_target'],
                'costs_included': False, 'base_trade_date': base, 'generated_at': generated,
                **prediction_timing(base, generated), 'input_database_sha256': input_sha256,
                'research_ready': True, 'formal_trade_ready': False, 'formal_prediction_allowed': False,
                'actual_win_rate': None, 'real_execution_win_rate': None,
                'execution_win_rate_80_validated': False, 'protocol_sha256': protocol_sha,
                'strategy_definition_sha256': strategy_definition_sha256()}
    summary = {**metadata, 'protocol': protocol, 'policy': policy,
               'split': {key: describe(part) for key, part in chunks.items()},
               'selection_grid': grid, 'diagnostic': reports,
               'endpoints': evaluate(diagnostic, probabilities['diagnostic'], chunks['calibration']),
               'features': FEATURES, 'history_rows_scanned': scanned,
               'history_signal_rows': len(history), 'history_symbols': int(history.symbol.nunique()),
               'prediction_count': len(live),
               'validation_origin': 'previously_exposed_historical_development_diagnostic_not_fresh_holdout',
               'formal_blockers': ['official_point_in_time_universe_status_limits_actions_unverified',
                                   'actual_execution_evidence_unavailable',
                                   'new_forward_execution_track_record_unavailable'],
               'limitations': [
                   'Gross research excludes all fees and slippage; it is not net investment profit.',
                   'D1 opening reference cancellations use only opening price and T close.',
                   'The T-frozen buy cap is min(T close times 1.03,10); no auction fill is established.',
                   'Daily OHLC cannot prove whether orders could buy, sell or reach an intraday target.',
                   'One-price, missing and conflicting bars remain unresolved and reserve capacity.',
                   'D1 rising is reported conditionally; D1 losing trades stay in the main denominator.',
                   'Probability endpoints concern D2 close, not policy target/stop execution.',
                   'Seventy-two variants were searched on selection history; selection results are optimistic.',
                   'Historical data were previously exposed; final diagnostics are not fresh holdout proof.',
                   'Current vendor census and raw corporate action handling retain survivorship and adjustment risks.',
                   'An 80 percent real win rate has not been established.']}
    live['_rank_score'] = .6 * live.p_profit + .4 * live.p_ge3
    ordered = live.sort_values(['_rank_score', 'symbol'], ascending=[False, True])
    eligible_live = entry_filter(live, parameters['entry_rule']) & (live.p_profit >= parameters['threshold'])
    fallback_live = entry_filter(live, FALLBACK['entry_rule']) & (live.p_profit >= FALLBACK['threshold'])
    policy_count = shadow_count = 0
    prediction_rows = []
    for rank, (index, row) in enumerate(ordered.iterrows(), 1):
        research = bool(policy_count < 3 and eligible_live.loc[index])
        shadow = bool(shadow_count < 3 and fallback_live.loc[index])
        policy_count += int(research); shadow_count += int(shadow)
        prediction_rows.append({'symbol': row.symbol, 'name': names.get(row.symbol),
            'close': float(row.close), 'rank': rank,
            **{endpoint: float(row[endpoint]) for endpoint in LABELS},
            'research_signal': research, 'policy_signal': bool(chosen and research),
            'shadow_signal': shadow, 'formal_trade_ready': False})
    summary['current_watchlist'] = [
        {**row, 'entry_proxy_notes':
            'T-close research observation only; D1 opening reference must respect the frozen buy cap. '
            'Official limits, order priority and actual auction execution are unverified.'}
        for row in prediction_rows if row['research_signal']][:3]
    if _hash(db_path) != input_sha256:
        raise ValueError('database changed while researching')
    output.mkdir(parents=True, exist_ok=False)
    bundle.update(metadata, protocol=protocol, policy=policy)
    joblib.dump(bundle, output/'model.joblib', compress=3)
    restored = predict(joblib.load(output/'model.joblib'), live)
    if any(not np.array_equal(live_p[key], restored[key]) for key in LABELS):
        raise ValueError('saved model prediction reload differs')
    summary['model_save_reload_verified'] = True
    (output/'summary.json').write_bytes(_json_bytes(summary))
    (output/'protocol.json').write_bytes(_json_bytes(protocol))
    (output/'predictions.json').write_bytes(_json_bytes(
        {**metadata, 'policy': policy, 'formal_candidates': [], 'rows': prediction_rows}))
    live[['symbol', 'date'] + FEATURES].to_csv(output/'prediction_inputs.csv.gz', index=False,
                                            compression={'method': 'gzip', 'mtime': 0})
    trades = [dict(row, audit_variant=key) for key, rows in variants.items() for row in rows]
    pd.DataFrame(trades).to_csv(output/'diagnostic_trades.csv.gz', index=False,
                               compression={'method': 'gzip', 'mtime': 0})
    from gross_swing_report import render_report
    (output/'report.html').write_text(render_report(summary), encoding='utf-8')
    print(json.dumps({**metadata, 'policy': policy, 'diagnostic': reports}, ensure_ascii=False), flush=True)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True); parser.add_argument('--base', required=True)
    parser.add_argument('--output', required=True); parser.add_argument('--input-sha256', required=True)
    arguments = parser.parse_args()
    run(arguments.db, arguments.base, arguments.output, arguments.input_sha256)
