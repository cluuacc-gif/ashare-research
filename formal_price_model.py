#!/usr/bin/env python3
"""Production price/volume model. Runtime readiness is separate from trade admission."""
import argparse
import datetime as dt
import importlib.metadata
import itertools
import json
import os
from pathlib import Path
import sqlite3

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from threadpoolctl import threadpool_limits

import collector as c
from pipeline_state import write, ref, verify_file
from trade_calendar import context

VERSION = 'price-volume-production-v1'
PREDICTION_APP_ID = 0x41534D50
FEATURES = ['close', 'ret1', 'ret3', 'ret5', 'ret10', 'ret20', 'ret60',
            'ma5_gap', 'ma10_gap', 'ma20_gap', 'ma60_gap', 'range', 'gap',
            'volume_ratio5', 'volume_ratio20', 'volatility20']
LABELS = {'P1': 'next_observed_market_session_raw_close_return_gt_0',
          'P2': 'next_observed_market_session_raw_close_return_ge_0.05'}
LIMITATIONS = [
    'Current vendor census has survivorship bias; historical point-in-time membership is unverified.',
    'Raw unadjusted prices: corporate actions, historical ST/status and official limits are unverified.',
    'Historical market sessions come from the observed panel, not a certified exchange calendar.',
    'P1/P2 concern close-to-close price returns, not executable entry/exit, T+1 net profit or limit-touch probability.',
    'Price/volume model only; news, sectors and funds are not model inputs.',
    'No 80% performance threshold is used for runtime readiness; measured performance may be poor.',
]


def features(rows, symbol, market_index):
    """Causal trailing inputs only; reject missing/invalid/conflicting bars in the lookback."""
    d = pd.DataFrame(rows).sort_values('trade_date').reset_index(drop=True)
    if d.trade_date.duplicated().any():
        raise ValueError('duplicate history dates')
    for key in ('open', 'high', 'low', 'close', 'volume'):
        d[key] = pd.to_numeric(d[key], errors='coerce')
    finite = np.isfinite(d[['open', 'high', 'low', 'close', 'volume']]).all(axis=1)
    valid = (finite & (d[['open', 'high', 'low', 'close']] > 0).all(axis=1)
             & (d.volume > 0) & (d.high >= d[['open', 'close', 'low']].max(axis=1))
             & (d.low <= d[['open', 'close', 'high']].min(axis=1)) & (d.conflict == 0))
    x = pd.DataFrame({'symbol': symbol, 'date': d.trade_date, 'close': d.close})
    for n in (1, 3, 5, 10, 20, 60):
        x['ret' + str(n)] = d.close / d.close.shift(n) - 1
    for n in (5, 10, 20, 60):
        x['ma' + str(n) + '_gap'] = d.close / d.close.rolling(n, min_periods=n).mean() - 1
    x['range'] = (d.high - d.low) / d.close.shift(1)
    x['gap'] = d.open / d.close.shift(1) - 1
    for n in (5, 20):
        x['volume_ratio' + str(n)] = d.volume / d.volume.shift(1).rolling(n, min_periods=n).mean()
    x['volatility20'] = x.ret1.rolling(20, min_periods=20).std()
    positions = d.trade_date.map(market_index)
    # 61 consecutive observed market dates are required for 60-session returns.
    window_ok = ((positions - positions.shift(60) == 60)
                 & (valid.rolling(61, min_periods=61).sum() == 61))
    x['history_bars'] = valid.cumsum()
    x['outcome_date'] = d.trade_date.shift(-1)
    x['forward_return'] = d.close.shift(-1) / d.close - 1
    x['label_valid'] = valid.shift(-1, fill_value=False) & (positions.shift(-1) - positions == 1)
    x['P1'] = (x.forward_return > 0).astype(int)
    x['P2'] = (x.forward_return >= .05 - 1e-12).astype(int)
    x = x[window_ok & (x.history_bars >= 250) & x.close.between(5, 10)]
    return x.replace([np.inf, -np.inf], np.nan).dropna(subset=FEATURES)


def panel(db_path, base):
    frames, current, scanned = [], [], 0
    with sqlite3.connect(Path(db_path).as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA application_id').fetchone()[0] != c.APP_ID:
            raise ValueError('not original market database')
        latest = db.execute('SELECT MAX(trade_date) FROM daily_quotes').fetchone()[0]
        if latest != base:
            raise ValueError('database date differs from snapshot')
        dates = [r[0] for r in db.execute('SELECT DISTINCT trade_date FROM daily_quotes WHERE trade_date<=? ORDER BY trade_date', (base,))]
        market_index = {day: i for i, day in enumerate(dates)}
        cursor = db.execute('SELECT symbol,trade_date,open,high,low,close,volume,conflict FROM daily_quotes WHERE trade_date<=? ORDER BY symbol,trade_date', (base,))
        for symbol, group in itertools.groupby(cursor, key=lambda r: r['symbol']):
            rows = [dict(r) for r in group]
            scanned += len(rows)
            if len(rows) < 250:
                continue
            f = features(rows, symbol, market_index)
            frames.append(f[(f.date < base) & f.label_valid & f.outcome_date.notna() & (f.outcome_date <= base)])
            current.append(f[f.date == base])
            if len(frames) % 1000 == 0:
                print(json.dumps({'history_symbols_processed': len(frames), 'rows_scanned': scanned}), flush=True)
        names = {r['symbol']: r['name'] for r in db.execute('SELECT symbol,name FROM security_master')}
    if not frames or not current:
        raise ValueError('insufficient real market panel')
    history = pd.concat(frames, ignore_index=True).sort_values(['date', 'symbol']).reset_index(drop=True)
    live = pd.concat(current, ignore_index=True).sort_values('symbol').reset_index(drop=True)
    if history.empty or live.empty:
        raise ValueError('no trainable history or no prediction inputs')
    return history, live, names, scanned


def split(history):
    dates = sorted(history.date.unique())
    if len(dates) < 150:
        raise ValueError('fewer than 150 independent historical dates')
    a, b = dates[int(len(dates) * .6)], dates[int(len(dates) * .8)]
    train = history[(history.date < a) & (history.outcome_date < a)].copy()
    cal = history[(history.date >= a) & (history.date < b) & (history.outcome_date < b)].copy()
    test = history[history.date >= b].copy()
    if any(len(q) < 1000 or q.date.nunique() < 30 for q in (train, cal, test)):
        raise ValueError('insufficient chronological train/calibration/test samples')
    if not (train.outcome_date.max() < cal.date.min() and cal.outcome_date.max() < test.date.min()):
        raise ValueError('overlapping outcome boundaries')
    return train, cal, test


def describe(q):
    return {'rows': len(q), 'independent_dates': int(q.date.nunique()),
            'first_input_date': str(q.date.min()), 'last_input_date': str(q.date.max()),
            'last_outcome_date': str(q.outcome_date.max())}


def predict(bundle, frame):
    probabilities = {}
    with threadpool_limits(limits=2):
        for label, parts in bundle['models'].items():
            raw = parts['estimator'].predict_proba(frame[FEATURES])[:, 1]
            probabilities[label] = np.asarray(parts['calibrator'].predict(raw))
    probabilities['P2'] = np.minimum(probabilities['P2'], probabilities['P1'])
    if any(not np.isfinite(p).all() or (p < 0).any() or (p > 1).any() for p in probabilities.values()):
        raise ValueError('invalid model probabilities')
    return probabilities


def evaluate(test, p, cal):
    result = {}
    ranked = test[['date', 'symbol']].copy()
    ranked['P1_probability'] = p['P1']; ranked['P2_probability'] = p['P2']
    top = ranked.sort_values(['date', 'P2_probability', 'P1_probability', 'symbol'],
                             ascending=[True, False, False, True]).groupby('date').head(10).index
    for label in LABELS:
        y = test[label].to_numpy()
        base = float(cal[label].mean())
        bins = []
        for lo in range(0, 100, 10):
            mask = (p[label] >= lo/100) & (p[label] <= 1 if lo == 90 else p[label] < (lo+10)/100)
            if mask.any():
                bins.append({'lower': lo/100, 'upper': (lo+10)/100, 'n': int(mask.sum()),
                             'mean_prediction': float(p[label][mask].mean()), 'observed_frequency': float(y[mask].mean())})
        by_date = test[['date', label]].copy(); by_date['probability'] = p[label]
        daily = by_date.groupby('date').agg(actual=(label, 'mean'), predicted=('probability', 'mean'))
        result[label] = {'n': len(y), 'independent_dates': int(test.date.nunique()),
                         'actual_frequency': float(y.mean()), 'predicted_mean': float(p[label].mean()),
                         'brier': float(np.mean((p[label]-y)**2)),
                         'calibration_block_constant_baseline_brier': float(np.mean((base-y)**2)),
                         'top10_event_frequency': float(test.loc[top, label].mean()),
                         'top10_n': len(top), 'probability_bins': bins,
                         'date_level_calibration_bias_mean': float((daily.predicted-daily.actual).mean())}
    return result


def write_prediction_db(path, rows, metadata):
    with sqlite3.connect(path) as db:
        db.execute(f'PRAGMA application_id={PREDICTION_APP_ID}')
        db.executescript('''CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE model_predictions(model_id TEXT,base_trade_date TEXT,target_trade_date TEXT,
            symbol TEXT,name TEXT,close REAL,p_up REAL,p_ge5 REAL,rank INTEGER,
            generated_at TEXT,generated_before_open INTEGER,formal_trade_ready INTEGER,
            PRIMARY KEY(model_id,target_trade_date,symbol));''')
        db.executemany('INSERT INTO metadata VALUES (?,?)', [(k, json.dumps(v, ensure_ascii=False)) for k, v in metadata.items()])
        db.executemany('INSERT INTO model_predictions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            [(metadata['model_id'], metadata['base_trade_date'], metadata['target_trade_date'],
              r['symbol'], r['name'], r['close'], r['P1'], r['P2'], r['rank'],
              metadata['generated_at'], int(metadata['generated_before_open']), 0) for r in rows])


def train(store, output):
    store, out = Path(store), Path(output)
    saved = json.loads((store/'restore.json').read_text())
    snapshot = saved['manifest']
    if not saved.get('verified') or not snapshot.get('preparation_ready'):
        raise ValueError('training requires independently restored prepared snapshot')
    base, target = snapshot['base_trade_date'], snapshot['next_trade_date']
    if context(base)['next_session'] != target:
        raise ValueError('prediction target is not next trading session')
    if base > c.now().date().isoformat():
        raise ValueError('future base date')
    database = store/snapshot['database']['name']
    verify_file(database, snapshot['database'])
    out.mkdir(parents=True, exist_ok=False)
    d, live, names, scanned = panel(database, base)
    train_data, cal, test = split(d)
    bundle = {'version': VERSION, 'features': FEATURES, 'labels': LABELS, 'models': {},
              'base_trade_date': base, 'split': {k: describe(q) for k,q in [('train',train_data),('calibration',cal),('test',test)]}}
    for label in LABELS:
        if train_data[label].nunique() != 2 or cal[label].sum() < 30 or (cal[label] == 0).sum() < 30:
            raise ValueError('insufficient real class outcomes: ' + label)
        model = HistGradientBoostingClassifier(max_iter=80, max_leaf_nodes=15, min_samples_leaf=100,
            learning_rate=.06, l2_regularization=5, random_state=20261011, early_stopping=False)
        with threadpool_limits(limits=2):
            model.fit(train_data[FEATURES], train_data[label])
            raw = model.predict_proba(cal[FEATURES])[:,1]
        calibrator = IsotonicRegression(y_min=0, y_max=1, out_of_bounds='clip').fit(raw, cal[label])
        bundle['models'][label] = {'estimator': model, 'calibrator': calibrator}
        print(json.dumps({'endpoint_trained': label}), flush=True)
    test_p, live_p = predict(bundle, test), predict(bundle, live)
    joblib.dump(bundle, out/'model.joblib', compress=3)
    restored_p = predict(joblib.load(out/'model.joblib'), live)
    if any(not np.array_equal(live_p[k], restored_p[k]) for k in LABELS):
        raise ValueError('saved estimator readback predictions differ')
    # Recheck immutable input after fitting; no training operation writes market data.
    verify_file(database, snapshot['database'])
    generated = c.stamp()
    model_id = VERSION + '-' + base + '-' + ref(out/'model.joblib')['sha256'][:16]
    forward = dt.datetime.fromisoformat(generated) < dt.datetime.combine(dt.date.fromisoformat(target), dt.time(9, 30), c.TZ)
    rows = [{'symbol': r.symbol, 'name': names.get(r.symbol), 'close': float(r.close),
             'P1': float(live_p['P1'][i]), 'P2': float(live_p['P2'][i]), 'history_bars': int(r.history_bars)}
            for i,r in live.iterrows()]
    rows.sort(key=lambda r: (-r['P2'], -r['P1'], r['symbol']))
    for rank, row in enumerate(rows, 1): row['rank'] = rank
    metadata = {'model_id': model_id, 'model_version': VERSION, 'base_trade_date': base,
        'target_trade_date': target, 'generated_at': generated, 'generated_before_open': forward,
        'snapshot_release': snapshot['release_tag'], 'input_database_sha256': snapshot['database']['sha256'],
        'model_ready': True, 'formal_trade_ready': False, 'formal_prediction_allowed': False,
        'prediction_scope': 'next_session_raw_close_return', 'labels': LABELS,
        'prediction_count': len(rows), 'execution_win_rate': None,
        'execution_win_rate_80_validated': False, 'win_rate_gate': 'deferred_by_user',
        'unavailable_endpoints': {'P3a': 'official limit history unavailable', 'P3b': 'official limit history unavailable',
                                  'E1_E2_net_profit': 'executable fills and costs unavailable'}}
    validation = {**metadata, 'status': 'TRAIN_SAVE_RELOAD_PREDICT_VERIFIED',
        'validation_origin': 'chronological_historical_holdout_not_forward_execution',
        'split': bundle['split'], 'purged_time_split_verified': True,
        'performance_is_runtime_gate': False, 'history_rows_scanned': scanned,
        'real_labeled_samples': len(d), 'sample_symbols': int(d.symbol.nunique()),
        'features': FEATURES, 'endpoints': evaluate(test, test_p, cal),
        'limitations': LIMITATIONS, 'forward_resolved_dates': 0,
        'runtime_versions': {k: importlib.metadata.version(k) for k in ('numpy','pandas','scikit-learn','joblib')},
        'github_commit': os.environ.get('GITHUB_SHA'), 'github_run_id': os.environ.get('GITHUB_RUN_ID')}
    write(out/'validation.json', validation)
    write(out/'predictions.json', {**metadata, 'rows': rows})
    write(out/'input_manifest.json', snapshot)
    live[['symbol'] + FEATURES].to_csv(out/'prediction_inputs.csv.gz', index=False,
                                     compression={'method':'gzip','mtime':0})
    write_prediction_db(out/'predictions.sqlite3', rows, metadata)
    holdout = test[['symbol','date','outcome_date','forward_return','P1','P2']].copy()
    for label in LABELS: holdout[label+'_probability'] = test_p[label]
    holdout.to_csv(out/'historical_holdout.csv.gz', index=False, compression={'method':'gzip','mtime':0})
    handoff = json.loads((store/snapshot['handoff']['name']).read_text())
    handoff.update(model_ready=True, formal_trade_ready=False, latest_model=metadata)
    handoff['model_artifacts'] = {key: ref(out/name) for key,name in
        [('estimator','model.joblib'),('validation','validation.json'),('predictions','predictions.json'),('prediction_database','predictions.sqlite3')]}
    write(out/'model_handoff.json', handoff)
    print(json.dumps({k:v for k,v in validation.items() if k not in ('endpoints','limitations')}, ensure_ascii=False), flush=True)
    return metadata


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True); parser.add_argument('--output', required=True)
    args = parser.parse_args(); train(args.store, args.output)
