#!/usr/bin/env python3
"""Causal net-profit research; historical diagnostics never authorize trading."""
from __future__ import annotations

import argparse
from dataclasses import replace
import datetime as dt
import hashlib
import html
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
from strategy_validation import CostConfig, calculate_trade, validate_strategy
from trade_calendar import context as calendar_context

VERSION = 'net-profit-research-v1'
BUDGET = 10000.0
INITIAL_CAPITAL = 30000.0
THRESHOLDS = (.5, .6, .7, .8, .85, .9)
MODEL_PARAMETERS = {'max_iter': 80, 'max_leaf_nodes': 15, 'min_samples_leaf': 100,
                    'learning_rate': .06, 'l2_regularization': 5,
                    'random_state': 20261017, 'early_stopping': False}
FEATURES = price_model.FEATURES + ['median_amount20', 'market_mean_return', 'market_breadth']
PROTOCOL = {
    'schema': VERSION, 'decision': 'after_T_close', 'entry': 'T_plus_1_open_reference',
    'exit': 'T_plus_2_close_reference', 'position_budget_cny': BUDGET,
    'initial_capital_cny': INITIAL_CAPITAL, 'external_cash_topups': False,
    'entry_lot_shares': 100, 'maximum_positions': 3,
    'exit_close_cash_unavailable_at_same_session_open': True,
    'same_day_stop_or_ohlc_touch_fill': False,
    'label': 'net_pnl_strictly_positive_after_assumed_itemized_costs',
    'split_fractions': [.5, .15, .15, .2], 'purge': 'prior_outcome_date_before_next_signal_date',
    'selection_thresholds': list(THRESHOLDS), 'fixed_shadow_threshold': .8,
    'filters': {'median_amount20_min': 20000000, 'ret5_max': .15, 'abs_ret1_max': .07,
                'market_breadth_min': .4, 'market_mean_return_min': -.01},
    'research_policy_min_completed_trades': 200, 'research_policy_min_entry_dates': 60,
    'research_policy_date_block_95pct_lower_strictly_above': .8,
    'historical_test_posture': 'previously_exposed_history_development_diagnostic_only',
    'fresh_forward_signal_start_not_before': '2026-10-19',
    'unresolved': 'retain_in_selected_attempt_denominator_and_reserve_uncertain_position',
    'official_pit_and_account_execution_required_for_formal_acceptance': True,
    'official_trading_calendar_verified': False,
    'features': FEATURES,
    'estimator': 'sklearn.ensemble.HistGradientBoostingClassifier',
    'estimator_parameters': MODEL_PARAMETERS,
    'calibrator': 'IsotonicRegression(y_min=0,y_max=1,out_of_bounds=clip)',
    'retraining_rule': 'On each verified prepared snapshot, refit on the first 50 percent of signal dates; '
                       'calibrate on the next 15 percent; select thresholds on the next 15 percent; '
                       'diagnose on the final 20 percent. Purge outcome dates across each boundary. '
                       'Historical diagnostics are exposed development data. Publish a separate immutable '
                       'release per run; no automatic forward settlement or real-execution acceptance.',
}


def _valid_bar(row):
    if row is None:
        return False
    try:
        o, h, l, cl, vol = [float(row[k]) for k in ('open', 'high', 'low', 'close', 'volume')]
        return (all(math.isfinite(x) and x > 0 for x in (o, h, l, cl, vol))
                and h >= max(o, l, cl) and l <= min(o, h, cl) and row['conflict'] == 0)
    except (KeyError, TypeError, ValueError):
        return False


def _one_price(row):
    return abs(float(row['high']) - float(row['low'])) < 1e-9


def shares_within_budget(price, costs, budget=None):
    """Whole lots, including entry slippage and entry fees, must fit the budget."""
    budget = BUDGET if budget is None else float(budget)
    if not math.isfinite(budget) or budget < 0:
        raise ValueError('budget must be finite and nonnegative')
    shares = int(budget / (price * (1 + costs.slippage_bps / 10000)) // 100) * 100
    while shares > 0:
        trial = calculate_trade(price, price, shares, '2000-01-03', '2000-01-04', config=costs)
        if trial['buy_cash'] <= budget + 1e-9:
            return shares
        shares -= 100
    return 0


def outcome(signal_date, symbol, entry_date, exit_date, entry, exit_, costs=None):
    """Unknown entry/exit evidence remains an unresolved attempt, never a label 0/1."""
    costs = costs or CostConfig()
    common = {'signal_date': signal_date, 'symbol': symbol, 'entry_date': entry_date,
              'outcome_date': exit_date, 'exit_date': exit_date, 'outcome_status': 'unresolved',
              'status': 'unresolved', 'net_return': None, 'net_pnl': None, 'net_profit_label': None,
              'reference_buy_price': None, 'reference_sell_price': None, 'shares': None,
              'buy_cash': None, 'cash_reservation_assumption': True}
    if not entry_date:
        return {**common, 'reason': 'future_market_session_not_observed'}
    if not _valid_bar(entry):
        return {**common, 'reason': 'missing_invalid_suspended_or_conflicting_future_bar'}
    if _one_price(entry):
        return {**common, 'reason': 'one_price_bar_execution_unresolved'}
    shares = shares_within_budget(float(entry['open']), costs)
    if shares == 0:
        return {**common, 'reason': 'insufficient_budget_for_one_lot'}
    entry_cost = calculate_trade(float(entry['open']), float(entry['open']), shares,
                                '2000-01-03', '2000-01-04', config=costs)
    common.update(reference_buy_price=float(entry['open']), shares=shares,
                  buy_cash=entry_cost['buy_cash'], cash_reservation_assumption=False)
    if not exit_date:
        return {**common, 'reason': 'future_market_session_not_observed'}
    if not _valid_bar(exit_):
        return {**common, 'reason': 'missing_invalid_suspended_or_conflicting_future_bar'}
    if _one_price(exit_):
        return {**common, 'reason': 'one_price_bar_execution_unresolved'}
    trade = calculate_trade(float(entry['open']), float(exit_['close']), shares,
                            entry_date, exit_date, config=costs)
    return {**common, **trade, 'outcome_status': 'closed', 'net_profit_label': int(trade['win']),
            'observed_session_continuity_verified': True,
            'official_calendar_verified': False, 'reason': 'daily_open_close_cost_proxy'}


def symbol_features(rows, symbol, sessions):
    """Only trailing values enter FEATURES; forward bars appear in outcome columns only."""
    index = {day: i for i, day in enumerate(sessions)}
    frame = price_model.features(rows, symbol, index)
    data = pd.DataFrame(rows).sort_values('trade_date').reset_index(drop=True)
    amounts = pd.to_numeric(data.amount, errors='coerce')
    amounts = amounts.where(np.isfinite(amounts) & (amounts > 0))
    frame['median_amount20'] = amounts.rolling(20, min_periods=20).median().reindex(frame.index)
    by_date = {r['trade_date']: r for r in rows}
    records = []
    for row in frame.itertuples():
        pos = index[row.date]
        entry = sessions[pos + 1] if pos + 1 < len(sessions) else None
        exit_ = sessions[pos + 2] if pos + 2 < len(sessions) else None
        result = outcome(row.date, symbol, entry, exit_, by_date.get(entry), by_date.get(exit_))
        records.append({key: result.get(key) for key in ('entry_date', 'outcome_date', 'outcome_status',
            'net_return', 'net_pnl', 'net_profit_label', 'reference_buy_price', 'reference_sell_price',
            'shares', 'buy_cash', 'cash_reservation_assumption', 'reason')})
    if not records:
        return frame
    # Existing close-to-close labels must not be accidentally used by this strategy.
    frame = frame.drop(columns=['outcome_date', 'forward_return', 'label_valid', 'P1', 'P2'])
    labels = pd.DataFrame(records, index=frame.index)
    return pd.concat([frame, labels], axis=1)


def panel(db_path, base):
    path = Path(db_path).resolve()
    frames, market = [], []
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA application_id').fetchone()[0] != c.APP_ID:
            raise ValueError('not the original market database')
        if db.execute('SELECT MAX(trade_date) FROM daily_quotes').fetchone()[0] != base:
            raise ValueError('database date differs from requested base')
        sessions = [r[0] for r in db.execute('SELECT DISTINCT trade_date FROM daily_quotes WHERE trade_date<=? ORDER BY trade_date', (base,))]
        market_index = {d: i for i, d in enumerate(sessions)}
        cursor = db.execute('SELECT symbol,trade_date,open,high,low,close,volume,amount,conflict FROM daily_quotes WHERE trade_date<=? ORDER BY symbol,trade_date', (base,))
        scanned = symbols = 0
        for symbol, group in itertools.groupby(cursor, key=lambda r: r['symbol']):
            rows = [dict(r) for r in group]
            scanned += len(rows); symbols += 1
            data = pd.DataFrame(rows)
            values = pd.to_numeric(data.close, errors='coerce')
            positions = data.trade_date.map(market_index)
            valid = pd.Series([_valid_bar(r) for r in rows])
            ret = values / values.shift(1) - 1
            valid_ret = valid & valid.shift(1, fill_value=False) & (positions - positions.shift(1) == 1) & np.isfinite(ret)
            market.append(pd.DataFrame({'date': data.loc[valid_ret, 'trade_date'], 'ret': ret[valid_ret]}))
            if len(rows) >= 250:
                f = symbol_features(rows, symbol, sessions)
                if not f.empty:
                    frames.append(f)
            if symbols % 1000 == 0:
                print(json.dumps({'symbols_processed': symbols, 'history_rows_scanned': scanned}), flush=True)
        names = {r['symbol']: r['name'] for r in db.execute('SELECT symbol,name FROM security_master')}
    if not frames:
        raise ValueError('no eligible real history')
    context = pd.concat(market, ignore_index=True).groupby('date').agg(
        market_mean_return=('ret', 'mean'), market_breadth=('ret', lambda x: float((x > 0).mean())))
    data = pd.concat(frames, ignore_index=True).merge(context, left_on='date', right_index=True, how='left')
    # Missing liquidity is kept for shadow attempt coverage; it fails the fixed filter.
    data = data.dropna(subset=[f for f in FEATURES if f != 'median_amount20'])
    history = data[data.date < base].sort_values(['date', 'symbol']).reset_index(drop=True)
    live = data[data.date == base].sort_values('symbol').reset_index(drop=True)
    if history.empty or live.empty:
        raise ValueError('no research history or current prediction inputs')
    return history, live, names, scanned


def split(history):
    dates = sorted(history.date.unique())
    if len(dates) < 200:
        raise ValueError('fewer than 200 distinct historical signal dates')
    cuts = [dates[int(len(dates) * x)] for x in (.5, .65, .8)]
    chunks = {}
    lower = None
    for key, upper in zip(('train', 'calibration', 'selection', 'diagnostic'), cuts + [None]):
        keep = pd.Series(True, index=history.index)
        if lower is not None:
            keep &= history.date >= lower
        if upper is not None:
            keep &= (history.date < upper) & history.outcome_date.notna() & (history.outcome_date < upper)
        chunks[key] = history[keep].copy().reset_index(drop=True)
        if chunks[key].empty:
            raise ValueError('empty chronological block: ' + key)
        lower = upper
    for left, right in zip(list(chunks)[:-1], list(chunks)[1:]):
        if not chunks[left].outcome_date.max() < chunks[right].date.min():
            raise ValueError('overlapping outcome boundaries')
    return chunks


def describe(frame):
    mature = frame.outcome_date.dropna()
    return {'rows': len(frame), 'labeled_closed_rows': int((frame.outcome_status == 'closed').sum()),
            'unresolved_rows': int((frame.outcome_status != 'closed').sum()),
            'independent_dates': int(frame.date.nunique()), 'first_input_date': str(frame.date.min()),
            'last_input_date': str(frame.date.max()), 'last_outcome_date': str(mature.max()) if len(mature) else None}


def fixed_filter(frame):
    return ((frame.median_amount20 >= 20000000) & (frame.ret5 <= .15) & (frame.ret1.abs() <= .07)
            & (frame.market_breadth >= .4) & (frame.market_mean_return >= -.01))


def simulate(frame, threshold=None, use_filter=True, costs=None):
    """Select before entry; uncertain positions consume capacity until evidence resolves."""
    costs = costs or CostConfig()
    records, positions = [], []
    cash, settled_pnl = INITIAL_CAPITAL, 0.0

    def check_cash():
        reserved = sum(r['reserved_cash'] for r in positions)
        resources = INITIAL_CAPITAL + settled_pnl
        if cash < -1e-7 or cash > resources + 1e-7 or not math.isclose(
                cash + reserved, resources, rel_tol=1e-10, abs_tol=1e-7):
            raise ValueError('portfolio cash conservation violated')

    for day, daily in frame.groupby('date', sort=True):
        entries = daily.entry_date.dropna()
        entry_day = str(entries.iloc[0]) if len(entries) else None
        if entry_day:
            remaining = []
            for position in positions:
                # A future close is settled only after that close has occurred.
                if position['status'] == 'closed' and position['exit_date'] < entry_day:
                    cash += position['sell_cash']
                    settled_pnl += position['net_pnl']
                else:
                    remaining.append(position)
            positions = remaining
            check_cash()
        if threshold is not None:
            daily = daily[daily.probability_net_profit >= threshold]
        if use_filter:
            daily = daily[fixed_filter(daily)]
        daily = daily.sort_values(['probability_net_profit', 'symbol'], ascending=[False, True])
        selected = 0
        for row in daily.itertuples():
            if len(positions) >= 3 or selected >= 3:
                break
            if any(r['symbol'] == row.symbol for r in positions):
                continue
            common = {'signal_date': row.date, 'symbol': row.symbol,
                      'entry_date': row.entry_date, 'exit_date': row.outcome_date,
                      'probability_net_profit': float(row.probability_net_profit),
                      'cash_before_entry': cash, 'initial_capital': INITIAL_CAPITAL}
            budget = min(BUDGET, cash)
            if budget <= 0:
                break
            if row.outcome_status != 'closed':
                buy = getattr(row, 'reference_buy_price', None)
                known_entry = buy is not None and math.isfinite(float(buy)) and float(buy) > 0
                if known_entry:
                    shares = shares_within_budget(float(buy), costs, budget=budget)
                    if not shares:
                        continue
                    entry_cost = calculate_trade(float(buy), float(buy), shares,
                                                '2000-01-03', '2000-01-04', config=costs)
                    reserved = entry_cost['buy_cash']
                else:
                    shares, reserved = None, budget
                record = {**common, 'status': 'unresolved', 'reason': row.reason,
                          'net_pnl': None, 'net_return': None, 'win': False,
                          'shares': shares, 'reference_buy_price': float(buy) if known_entry else None,
                          'buy_cash': reserved if known_entry else None,
                          'cash_reservation_assumption': not known_entry,
                          'reservation_basis': 'reference_open_with_assumed_entry_costs' if known_entry
                                               else 'conservative_budget_reserve_unknown_entry'}
            else:
                shares = shares_within_budget(float(row.reference_buy_price), costs, budget=budget)
                if not shares:
                    continue
                record = {**common, **calculate_trade(float(row.reference_buy_price),
                    float(row.reference_sell_price), shares, row.entry_date, row.outcome_date, config=costs),
                    'cash_reservation_assumption': False,
                    'reservation_basis': 'reference_open_with_assumed_entry_costs'}
                record['official_calendar_verified'] = False
                reserved = record['buy_cash']
            cash -= reserved
            record.update(reserved_cash=reserved, cash_after_entry=cash)
            positions.append(record); records.append(record); selected += 1
            check_cash()
        if not selected:
            records.append({'status': 'abstain', 'signal_date': str(day),
                            'reason': 'no_eligible_signal_or_capacity', 'net_pnl': None, 'net_return': None})
    return records


def policy_eligible(report):
    boot = report['date_block_bootstrap']['win_rate_ci95']
    return (report['n_trades'] >= 200 and report['independent_entry_dates'] >= 60
            and report['n_unresolved'] == 0 and report['win_rate'] is not None
            and report['win_rate'] > .8 and boot is not None and boot[0] > .8
            and report['mean_net_return'] is not None and report['mean_net_return'] > 0)


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _write(path, data):
    Path(path).write_bytes(_json_bytes(data))


def _json_bytes(data):
    """One serialization for saved protocol bytes and their identity."""
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def strategy_definition_sha256(costs=None):
    definition = {**PROTOCOL, 'costs': (costs or CostConfig()).to_dict()}
    return hashlib.sha256(json.dumps(definition, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def prediction_timing(base, generated_at):
    target = calendar_context(base)['next_session']
    if not target:
        raise ValueError('no verified target session available')
    generated = dt.datetime.fromisoformat(generated_at)
    if generated.tzinfo is None:
        raise ValueError('generated_at must include its timezone')
    target_open = dt.datetime.combine(dt.date.fromisoformat(target), dt.time(9, 30), c.TZ)
    return {'target_trade_date': target,
            'generated_before_target_open': generated < target_open}


def _probabilities(estimator, calibrator, frame):
    with threadpool_limits(limits=2):
        result = np.asarray(calibrator.predict(estimator.predict_proba(frame[FEATURES])[:, 1]))
    if not np.isfinite(result).all() or (result < 0).any() or (result > 1).any():
        raise ValueError('invalid calibrated net-profit probability')
    return result


def run(db_path, base, output, input_sha256):
    db_path = Path(db_path).resolve(); output = Path(output)
    if dt.date.fromisoformat(base).isoformat() != base or base > c.now().date().isoformat():
        raise ValueError('invalid or future base date')
    if len(input_sha256) != 64 or any(x not in '0123456789abcdef' for x in input_sha256):
        raise ValueError('input SHA256 must be 64 lowercase hex characters')
    if _hash(db_path) != input_sha256:
        raise ValueError('input database hash mismatch')
    history, live, names, scanned = panel(db_path, base)
    chunks = split(history)
    train = chunks['train'][chunks['train'].outcome_status == 'closed']
    calibration = chunks['calibration'][chunks['calibration'].outcome_status == 'closed']
    if any(len(f) < 1000 or f.date.nunique() < 30 or f.net_profit_label.nunique() != 2 for f in (train, calibration)):
        raise ValueError('insufficient chronological net-profit training/calibration samples')
    estimator = HistGradientBoostingClassifier(**MODEL_PARAMETERS)
    with threadpool_limits(limits=2):
        estimator.fit(train[FEATURES], train.net_profit_label.astype(int))
        raw = estimator.predict_proba(calibration[FEATURES])[:, 1]
    calibrator = IsotonicRegression(y_min=0, y_max=1, out_of_bounds='clip').fit(raw, calibration.net_profit_label)
    for key in ('selection', 'diagnostic'):
        chunks[key]['probability_net_profit'] = _probabilities(estimator, calibrator, chunks[key])
    live['probability_net_profit'] = _probabilities(estimator, calibrator, live)
    costs = CostConfig()
    selection = []
    for threshold in THRESHOLDS:
        report = validate_strategy(simulate(chunks['selection'], threshold, costs=costs), config=costs)
        selection.append({'threshold': threshold, 'research_policy_eligible': policy_eligible(report), 'validation': report})
    passing = [r for r in selection if r['research_policy_eligible']]
    chosen = max(passing, key=lambda r: (r['validation']['date_block_bootstrap']['win_rate_ci95'][0],
                                         r['validation']['mean_net_return'], r['threshold'])) if passing else None
    threshold = chosen['threshold'] if chosen else .8
    policy = {'active': bool(chosen), 'selected_threshold': chosen['threshold'] if chosen else None,
              'shadow_threshold': .8, 'diagnostic_threshold': threshold,
              'inactive_reason': None if chosen else 'insufficient_selection_evidence',
              'active_scope': 'research_only_never_formal_trade_authorization'}
    diagnostic = chunks['diagnostic']
    variants = {
        'baseline_top3': simulate(diagnostic, threshold=None, use_filter=False, costs=costs),
        'shadow_diagnostic': simulate(diagnostic, .8, costs=costs),
        'selected_policy_diagnostic': simulate(diagnostic, threshold, costs=costs) if chosen else [],
        'stress_20bp': simulate(diagnostic, .8, costs=replace(costs, slippage_bps=20)),
    }
    reports = {key: validate_strategy(rows, config=replace(costs, slippage_bps=20) if key == 'stress_20bp' else costs)
               for key, rows in variants.items()}
    generated = c.stamp()
    protocol = {**PROTOCOL, 'costs': costs.to_dict(), 'frozen_before_new_forward_observations': generated,
                'historical_development_cutoff': base}
    protocol_sha = hashlib.sha256(_json_bytes(protocol)).hexdigest()
    metadata = {'schema': VERSION, 'strategy_version': VERSION, 'base_trade_date': base,
        'generated_at': generated, **prediction_timing(base, generated),
        'input_database_sha256': input_sha256, 'research_ready': True,
        'formal_trade_ready': False, 'formal_prediction_allowed': False,
        'real_execution_win_rate': None, 'execution_win_rate_80_validated': False,
        'protocol_sha256': protocol_sha, 'strategy_definition_sha256': strategy_definition_sha256(costs)}
    summary = {**metadata, 'protocol': protocol, 'policy': policy,
        'split': {key: describe(frame) for key, frame in chunks.items()},
        'selection_thresholds': selection, **reports, 'features': FEATURES,
        'history_rows_scanned': scanned, 'history_signal_rows': len(history),
        'history_symbols': int(history.symbol.nunique()), 'prediction_count': len(live),
        'validation_origin': 'previously_exposed_historical_development_diagnostic_not_fresh_holdout',
        'formal_blockers': ['official_pit_universe_status_limits_actions_unverified',
            'broker_fills_and_actual_fee_evidence_unavailable', 'new_forward_execution_track_record_unavailable'],
        'limitations': ['Daily OHLC cannot establish actual order execution or intraday path.',
            'Current vendor census has survivorship bias; no verified historical membership.',
            'Raw prices and corporate actions remain unverified.',
            'Observed market sessions are not a certified exchange calendar.',
            'Unresolved selected trades consume capacity and count as non-wins.',
            'All historical results are development diagnostics; a probability is not a guaranteed win rate.',
            'The 80% target is not validated by successful code execution.']}
    rows = []
    eligible = fixed_filter(live)
    ordered = live.sort_values(['probability_net_profit', 'symbol'], ascending=[False, True])
    shadow_count = policy_count = 0
    for rank, (index, row) in enumerate(ordered.iterrows(), 1):
        shadow_signal = bool(shadow_count < 3 and eligible.loc[index] and row.probability_net_profit >= .8)
        policy_signal = bool(chosen and policy_count < 3 and eligible.loc[index] and row.probability_net_profit >= threshold)
        shadow_count += int(shadow_signal)
        policy_count += int(policy_signal)
        rows.append({'symbol': row.symbol, 'name': names.get(row.symbol), 'close': float(row.close),
                     'probability_net_profit': float(row.probability_net_profit), 'rank': rank,
                     'research_signal': shadow_signal, 'shadow_signal': shadow_signal,
                     'policy_signal': policy_signal,
                     'formal_trade_ready': False})
    if _hash(db_path) != input_sha256:
        raise ValueError('market database changed during research')
    output.mkdir(parents=True, exist_ok=False)
    bundle = {'version': VERSION, 'features': FEATURES, 'estimator': estimator,
              'calibrator': calibrator, 'policy': policy, 'protocol': protocol, **metadata}
    joblib.dump(bundle, output/'model.joblib', compress=3)
    restored = joblib.load(output/'model.joblib')
    if not np.array_equal(live.probability_net_profit.to_numpy(),
                          _probabilities(restored['estimator'], restored['calibrator'], live)):
        raise ValueError('saved model predictions differ after reload')
    summary['model_save_reload_verified'] = True
    _write(output/'summary.json', summary); _write(output/'protocol.json', protocol)
    _write(output/'predictions.json', {**metadata, 'policy': policy, 'formal_candidates': [], 'rows': rows})
    live[['symbol', 'date'] + FEATURES].to_csv(output/'prediction_inputs.csv.gz', index=False,
        compression={'method': 'gzip', 'mtime': 0})
    trades = [dict(row, audit_variant=key) for key, records in variants.items() for row in records]
    pd.DataFrame(trades).to_csv(output/'diagnostic_trades.csv.gz', index=False,
        compression={'method': 'gzip', 'mtime': 0})
    from strategy_report import render_report
    document = render_report(summary)
    (output/'report.html').write_text(document, encoding='utf-8')
    print(json.dumps({**metadata, 'policy': policy, 'historical_shadow_win_rate': reports['shadow_diagnostic']['win_rate']}, ensure_ascii=False), flush=True)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True); parser.add_argument('--base', required=True)
    parser.add_argument('--output', required=True); parser.add_argument('--input-sha256', required=True)
    args = parser.parse_args()
    run(args.db, args.base, args.output, args.input_sha256)
