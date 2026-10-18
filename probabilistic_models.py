#!/usr/bin/env python3
"""Fixed, chronological probability models for research, never a trade approval.

The caller must construct causal features and purge overlapping outcome horizons.
This module checks fit/calibration chronology and never performs a random split.
Cross-sectional rows receive equal total weight per signal date; their number is
not an independent sample size. Model disagreement is not a confidence interval.
"""
from __future__ import annotations

import copy
import warnings

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

VERSION = 'chronological-calibrated-probabilities-v1'
MODEL_NAMES = ('logistic', 'hgb', 'equal_ensemble')
SEED = 20261018
LOGISTIC_PARAMETERS = {
    'C': .2, 'penalty': 'l2', 'solver': 'lbfgs', 'max_iter': 600,
    'tol': 1e-5, 'class_weight': None, 'random_state': SEED,
}
HGB_PARAMETERS = {
    'loss': 'log_loss', 'learning_rate': .05, 'max_iter': 100,
    'max_depth': 3, 'max_leaf_nodes': 8, 'min_samples_leaf': 60,
    'l2_regularization': 10., 'early_stopping': False,
    'class_weight': None, 'random_state': SEED,
}
CALIBRATION_PARAMETERS = {
    'C': .2, 'penalty': 'l2', 'solver': 'lbfgs', 'max_iter': 300,
    'tol': 1e-6, 'class_weight': None, 'random_state': SEED,
}
PROTOCOL = {
    'schema': VERSION,
    'fixed_candidates': list(MODEL_NAMES),
    'fitted_base_model_count': 2,
    'hyperparameter_search': False,
    'logistic': LOGISTIC_PARAMETERS,
    'hgb': HGB_PARAMETERS,
    'calibration': CALIBRATION_PARAMETERS,
    'calibration_method': 'regularized_monotone_sigmoid_then_date_prior_shrinkage',
    'calibration_shrinkage_pseudo_dates': 20,
    'training_minimum_dates': 20,
    'training_minimum_per_class': 20,
    'training_minimum_dates_per_class': 10,
    'calibration_minimum_dates': 10,
    'calibration_minimum_per_class': 10,
    'calibration_minimum_dates_per_class': 5,
    'date_weight': 'n_valid_rows/(n_valid_dates*rows_on_same_date); mean=1',
    'date_weight_is_independent_sample_size': False,
    'class_frequency_reweighting': False,
    'preprocessing': 'training_only_0.5/99.5_percentile_clip_median_impute_weighted_standardize',
    'single_class_or_missing_endpoint': 'constant_date_smoothed_prior_available_false',
    'equal_ensemble': 'arithmetic_mean_of_two_calibrated_base_probabilities',
    'disagreement': 'absolute_difference_between_calibrated_base_probabilities',
    'disagreement_is_confidence_interval': False,
    'nested_endpoint_projection': 'p_ge3=min(p_ge3,p_win3); p_ge6_is_a_different_exit_policy',
    'smoothing': 'date_mean_fractional_Beta(1,1)_shrinkage_not_a_posterior_interval',
    'purging_of_outcome_horizons': 'caller_responsibility',
    'selection_and_diagnostic_data_used_for_fit': False,
    'formal_trade_ready': False,
    'claimed_80_percent_win_rate': False,
    'sources': [
        'https://scikit-learn.org/1.5/modules/calibration.html',
        'https://scikit-learn.org/1.5/modules/generated/sklearn.linear_model.LogisticRegression.html',
        'https://scikit-learn.org/1.5/modules/generated/sklearn.ensemble.HistGradientBoostingClassifier.html',
    ],
}


def date_balanced_weights(dates):
    """Every distinct date has identical total mass; the overall mean is one."""
    values = pd.Series(np.asarray(dates), dtype='object')
    if not len(values):
        return np.empty(0, dtype=float)
    if values.isna().any():
        raise ValueError('date_balanced_weights requires non-missing dates')
    count = values.groupby(values, dropna=False).transform('size').to_numpy(float)
    return len(values) / (values.nunique() * count)


def _dates(frame, name):
    if name not in frame:
        raise ValueError('missing signal date column: ' + name)
    dates = pd.to_datetime(frame[name], errors='raise', utc=True)
    if dates.isna().any():
        raise ValueError('signal dates cannot be missing')
    # Signals are daily. Intraday timestamps must not evade same-date checks.
    return dates.dt.strftime('%Y-%m-%d').to_numpy()


def _bound(dates):
    return {'rows': len(dates), 'dates': len(set(dates)),
            'first_date': min(dates) if len(dates) else None,
            'last_date': max(dates) if len(dates) else None}


def _numeric(frame, columns):
    missing = [name for name in columns if name not in frame]
    if missing:
        raise ValueError('missing probability features: ' + ','.join(missing))
    data = frame.loc[:, columns].apply(pd.to_numeric, errors='coerce').to_numpy(dtype=float)
    data[~np.isfinite(data)] = np.nan
    return data


def _labels(frame, column):
    if column not in frame:
        return np.full(len(frame), np.nan)
    numeric = pd.to_numeric(frame[column], errors='coerce').to_numpy(float)
    present = np.isfinite(numeric)
    if np.any(~np.isin(numeric[present], [0., 1.])):
        raise ValueError('binary endpoint contains a non-binary label: ' + column)
    return numeric


def _prior(labels, dates):
    known = np.isfinite(labels)
    if not np.any(known):
        return .5
    daily = pd.DataFrame({'date': dates[known], 'label': labels[known]}).groupby('date').label.mean()
    return float((daily.sum() + 1.) / (len(daily) + 2.))


def _preprocess_fit(values, dates):
    lower = np.zeros(values.shape[1])
    upper = np.zeros(values.shape[1])
    median = np.zeros(values.shape[1])
    for k in range(values.shape[1]):
        valid = values[:, k][np.isfinite(values[:, k])]
        if len(valid):
            lower[k], upper[k] = np.quantile(valid, [.005, .995])
            median[k] = float(np.median(valid))
    clipped = np.clip(values, lower, upper)
    filled = np.where(np.isfinite(clipped), clipped, median)
    scaler = StandardScaler().fit(filled, sample_weight=date_balanced_weights(dates))
    return {'lower': lower, 'upper': upper, 'median': median, 'scaler': scaler}


def _transform(preprocessor, values):
    filled = np.where(np.isfinite(values),
                      np.clip(values, preprocessor['lower'], preprocessor['upper']),
                      preprocessor['median'])
    return preprocessor['scaler'].transform(filled) if len(values) else filled


def _logit(probabilities):
    probabilities = np.clip(np.asarray(probabilities, dtype=float), 1e-6, 1-1e-6)
    return np.log(probabilities / (1-probabilities)).reshape(-1, 1)


def _fit_endpoint(train_x, train_y, train_dates, calibration_x,
                  calibration_y, calibration_dates, model_name):
    t_known = np.isfinite(train_y)
    c_known = np.isfinite(calibration_y)
    yt, dt = train_y[t_known], train_dates[t_known]
    yc, dc = calibration_y[c_known], calibration_dates[c_known]
    prior = _prior(calibration_y, calibration_dates) if np.any(c_known) else _prior(train_y, train_dates)
    result = {
        'model': None, 'calibrator': None, 'prior': prior, 'shrinkage': 0.,
        'available': False, 'model_state': 'constant_prior_only',
        'calibration_state': 'insufficient_support',
        'train': {**_bound(dt), 'positive': int(np.sum(yt == 1)), 'negative': int(np.sum(yt == 0)),
                  'positive_dates': len(set(dt[yt == 1])), 'negative_dates': len(set(dt[yt == 0]))},
        'calibration': {**_bound(dc), 'positive': int(np.sum(yc == 1)), 'negative': int(np.sum(yc == 0)),
                        'positive_dates': len(set(dc[yc == 1])), 'negative_dates': len(set(dc[yc == 0]))},
    }
    if (len(set(dt)) < PROTOCOL['training_minimum_dates']
            or min(np.sum(yt == 0), np.sum(yt == 1)) < PROTOCOL['training_minimum_per_class']
            or min(len(set(dt[yt == 0])), len(set(dt[yt == 1]))) < PROTOCOL['training_minimum_dates_per_class']):
        return result
    estimator = (LogisticRegression(**LOGISTIC_PARAMETERS) if model_name == 'logistic'
                 else HistGradientBoostingClassifier(**HGB_PARAMETERS))
    with warnings.catch_warnings():
        warnings.simplefilter('error', ConvergenceWarning)
        try:
            estimator.fit(train_x[t_known], yt.astype(int), sample_weight=date_balanced_weights(dt))
        except ConvergenceWarning:
            result['model_state'] = 'nonconverged_prior_only'
            return result
    result['model'] = estimator
    result['model_state'] = 'fitted'
    if (len(set(dc)) < PROTOCOL['calibration_minimum_dates']
            or min(np.sum(yc == 0), np.sum(yc == 1)) < PROTOCOL['calibration_minimum_per_class']
            or min(len(set(dc[yc == 0])), len(set(dc[yc == 1]))) < PROTOCOL['calibration_minimum_dates_per_class']):
        return result
    scores = _logit(estimator.predict_proba(calibration_x[c_known])[:, 1])
    if float(np.ptp(scores)) < 1e-10:
        result['calibration_state'] = 'constant_model_score_prior_only'
        return result
    calibrator = LogisticRegression(**CALIBRATION_PARAMETERS)
    with warnings.catch_warnings():
        warnings.simplefilter('error', ConvergenceWarning)
        try:
            calibrator.fit(scores, yc.astype(int), sample_weight=date_balanced_weights(dc))
        except ConvergenceWarning:
            result['calibration_state'] = 'nonconverged_prior_only'
            return result
    if float(calibrator.coef_[0, 0]) <= 0:
        result['calibration_state'] = 'nonpositive_slope_prior_only'
        return result
    result.update({
        'calibrator': calibrator, 'available': True,
        'calibration_state': 'monotone_sigmoid_with_date_prior_shrinkage',
        'shrinkage': len(set(dc)) / (len(set(dc)) + PROTOCOL['calibration_shrinkage_pseudo_dates']),
    })
    return result


def fit_bundle(train_df, calibration_df, feature_columns, targets, date_column='signal_date'):
    """Fit two fixed models per target and calibrate only on strictly later dates.

    targets maps public probability names to binary label columns. Unknown labels
    remain unknown and are omitted independently for each endpoint. The caller
    must purge D2/D3 outcomes at split boundaries before invoking this function.
    """
    columns = list(feature_columns)
    targets = dict(targets)
    if not columns or len(set(columns)) != len(columns):
        raise ValueError('feature columns must be nonempty and unique')
    if set(columns) & (set(targets.values()) | {date_column}):
        raise ValueError('labels and signal date cannot be probability features')
    if not len(train_df):
        raise ValueError('probability training block is empty')
    if not targets:
        raise ValueError('probability targets are empty')
    train_dates, calibration_dates = _dates(train_df, date_column), _dates(calibration_df, date_column)
    if len(calibration_dates) and max(train_dates) >= min(calibration_dates):
        raise ValueError('calibration dates must be strictly later than all training dates')
    train_values, calibration_values = _numeric(train_df, columns), _numeric(calibration_df, columns)
    preprocessor = _preprocess_fit(train_values, train_dates)
    train_x = _transform(preprocessor, train_values)
    calibration_x = _transform(preprocessor, calibration_values)
    fitted = {'logistic': {}, 'hgb': {}}
    # Reproducible and bounded CPU use on cloud and desktop. No internal random
    # validation split: HGB early stopping is explicitly disabled.
    with threadpool_limits(limits=1):
        for endpoint, column in targets.items():
            train_y, calibration_y = _labels(train_df, column), _labels(calibration_df, column)
            for name in ('logistic', 'hgb'):
                fitted[name][endpoint] = _fit_endpoint(train_x, train_y, train_dates,
                    calibration_x, calibration_y, calibration_dates, name)
    endpoint_metadata = {
        name: {endpoint: {key: value for key, value in fitted[name][endpoint].items()
                         if key not in ('model', 'calibrator')}
               for endpoint in targets}
        for name in fitted}
    return {
        'schema': VERSION, 'features': columns, 'targets': targets,
        'date_column': date_column, 'preprocessor': preprocessor, 'models': fitted,
        'metadata': {'protocol': copy.deepcopy(PROTOCOL), 'train': _bound(train_dates),
                     'calibration': _bound(calibration_dates), 'endpoints': endpoint_metadata},
    }


def predict_bundle(bundle, frame):
    """Return probability arrays, support flags and model disagreement diagnostics.

    Support flags must be checked before treating a prediction as model-based.
    A high smoothed prior from a single-class block is not a validated forecast.
    """
    if bundle.get('schema') != VERSION:
        raise ValueError('unsupported probability bundle schema')
    values = _numeric(frame, bundle['features'])
    x = _transform(bundle['preprocessor'], values)
    predictions, available = {}, {}
    with threadpool_limits(limits=1):
        for name in ('logistic', 'hgb'):
            predictions[name], available[name] = {}, {}
            for endpoint, fitted in bundle['models'][name].items():
                probability = np.full(len(frame), fitted['prior'], dtype=float)
                if len(frame) and fitted['available']:
                    raw = _logit(fitted['model'].predict_proba(x)[:, 1])
                    calibrated = fitted['calibrator'].predict_proba(raw)[:, 1]
                    probability = fitted['shrinkage'] * calibrated + (1-fitted['shrinkage']) * fitted['prior']
                predictions[name][endpoint] = np.clip(probability, 1e-6, 1-1e-6)
                available[name][endpoint] = bool(fitted['available'])
            if 'p_ge3' in predictions[name] and 'p_win3' in predictions[name]:
                predictions[name]['p_ge3'] = np.minimum(predictions[name]['p_ge3'], predictions[name]['p_win3'])
    predictions['equal_ensemble'] = {
        endpoint: (predictions['logistic'][endpoint] + predictions['hgb'][endpoint]) / 2
        for endpoint in bundle['targets']}
    available['equal_ensemble'] = {
        endpoint: available['logistic'][endpoint] and available['hgb'][endpoint]
        for endpoint in bundle['targets']}
    disagreement = {
        endpoint: np.abs(predictions['logistic'][endpoint] - predictions['hgb'][endpoint])
        for endpoint in bundle['targets']}
    outside = np.isfinite(values) & ((values < bundle['preprocessor']['lower']) | (values > bundle['preprocessor']['upper']))
    return {
        'models': predictions, 'available': available, 'disagreement': disagreement,
        'feature_clip_fraction': outside.mean(axis=1),
        'feature_missing_fraction': (~np.isfinite(values)).mean(axis=1),
        'disagreement_is_confidence_interval': False,
        'formal_trade_ready': False,
    }
