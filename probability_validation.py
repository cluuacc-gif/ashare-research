"""Probability and selective-trade diagnostics, never a live-execution certificate.

Stocks observed on the same date are not independent trials.  Bootstrap entire
entry dates, and also contiguous blocks of five active entry dates, retaining
all stocks in each date.  These percentile intervals remain approximations:
nonstationary markets, repeated historical research, and unknown fills prevent
a finite-sample trading-win-rate guarantee.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd


VERSION = 'probability-validation-v1'
DEFAULT_ENDPOINTS = {
    'p_win3': 'label_profit3', 'p_ge3': 'label_ge3',
    'p_ge6': 'label_ge6', 'p_strength3': 'strength3_label',
}
FIXED_THRESHOLDS = (.5, .6, .7, .8, .9)
SOURCES = [
    {'title': 'scikit-learn 1.5 probability calibration',
     'url': 'https://scikit-learn.org/1.5/modules/calibration.html',
     'used_for': 'held-out calibration and proper scoring rules; Brier is not pure calibration error'},
    {'title': 'Guo et al. (2017), On Calibration of Modern Neural Networks',
     'url': 'https://proceedings.mlr.press/v70/guo17a.html',
     'used_for': 'motivation for measuring probability reliability, not evidence of stock predictability'},
    {'title': 'Politis and Romano (1994), The Stationary Bootstrap',
     'url': 'https://doi.org/10.1080/01621459.1994.10476870',
     'used_for': 'dependent-time-series resampling motivation; our fixed moving-block variant is different'},
    {'title': 'White (2000), A Reality Check for Data Snooping',
     'url': 'https://doi.org/10.1111/1468-0262.00152',
     'used_for': 'record model searches and avoid treating reused data as a fresh test; no Reality Check implementation claimed'},
    {'title': 'Geifman and El-Yaniv (2019), SelectiveNet',
     'url': 'https://proceedings.mlr.press/v97/geifman19a.html',
     'used_for': 'report risk together with coverage; no neural network or IID selective guarantee claimed'},
]


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        return value if dt.date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def _numeric_column(frame, column):
    if column not in frame:
        return np.full(len(frame), np.nan)
    return pd.to_numeric(frame[column], errors='coerce').to_numpy(dtype=float)


def _probabilities(frame, column):
    probabilities = _numeric_column(frame, column)
    finite = np.isfinite(probabilities)
    if np.any(finite & ((probabilities < 0) | (probabilities > 1))):
        raise ValueError('probabilities outside [0, 1]: ' + column)
    return probabilities


def _labels(frame, column):
    labels = _numeric_column(frame, column)
    if np.any(np.isfinite(labels) & ~np.isin(labels, [0, 1])):
        raise ValueError('binary labels required: ' + column)
    return labels


def _score(labels, probabilities, weights=None):
    if not len(labels):
        return {'brier_score': None, 'log_loss': None, 'observed_frequency': None,
                'mean_probability': None}
    clipped = np.clip(probabilities, 1e-12, 1 - 1e-12)
    return {
        'brier_score': float(np.average((probabilities - labels) ** 2, weights=weights)),
        'log_loss': float(np.average(-(labels * np.log(clipped)
                         + (1-labels) * np.log(1-clipped)), weights=weights)),
        'observed_frequency': float(np.average(labels, weights=weights)),
        'mean_probability': float(np.average(probabilities, weights=weights)),
    }


def evaluate_predictions(frame, endpoints=None, *, date_col='date',
                         baseline_probabilities=None):
    """Score existing probability columns against binary outcome columns.

    ``endpoints`` maps probability column -> target column.  Unknown outcomes are
    counted and omitted only from proper scores, never recoded as observed losses.
    Optional constant baselines must be supplied from training data; this function
    never estimates a comparison baseline using the scored outcomes.
    """
    frame = pd.DataFrame(frame)
    endpoints = DEFAULT_ENDPOINTS if endpoints is None else dict(endpoints)
    baselines = baseline_probabilities or {}
    dates = frame.get(date_col, pd.Series([None] * len(frame))).map(_date).to_numpy()
    output = {}
    for probability_col, label_col in endpoints.items():
        probability = _probabilities(frame, probability_col)
        label = _labels(frame, label_col)
        scored = np.isfinite(probability) & np.isfinite(label)
        labels, probabilities = label[scored], probability[scored]
        known_dates = dates[scored]
        point = _score(labels, probabilities)
        valid_dates = np.array([value is not None for value in known_dates], dtype=bool)
        daily = _score(np.array([]), np.array([]))
        n_dates = 0
        if valid_dates.any():
            _, inverse, counts = np.unique(known_dates[valid_dates],
                                          return_inverse=True, return_counts=True)
            weights = 1 / counts[inverse]
            daily = _score(labels[valid_dates], probabilities[valid_dates], weights)
            n_dates = len(counts)
        bins = []
        bin_index = np.minimum((probabilities * 10).astype(int), 9)
        error = 0.0
        for index in range(10):
            selected = bin_index == index
            count = int(selected.sum())
            item = {'lower_inclusive': index / 10, 'upper': (index + 1) / 10,
                    'upper_inclusive': index == 9, 'n': count,
                    'mean_probability': None, 'observed_frequency': None}
            if count:
                item['mean_probability'] = float(probabilities[selected].mean())
                item['observed_frequency'] = float(labels[selected].mean())
                error += count * abs(item['mean_probability']-item['observed_frequency'])
            bins.append(item)
        baseline = _number(baselines.get(probability_col))
        if baseline is not None and not 0 <= baseline <= 1:
            raise ValueError('baseline probability outside [0, 1]')
        baseline_score = _score(labels, np.full(len(labels), baseline)) if baseline is not None else None
        brier_skill = None
        if baseline_score and baseline_score['brier_score'] and point['brier_score'] is not None:
            brier_skill = 1-point['brier_score']/baseline_score['brier_score']
        output[probability_col] = {
            'target_column': label_col, 'n_candidates': len(frame),
            'n_scored': int(scored.sum()), 'n_unknown_labels': int((~np.isfinite(label)).sum()),
            'n_missing_predictions': int((~np.isfinite(probability)).sum()),
            'n_scored_missing_date': int((~valid_dates).sum()), 'entry_date_clusters': n_dates,
            **point, 'equal_date_weighted': daily, 'reliability_fixed_ten_bins': bins,
            'expected_calibration_error': error/len(labels) if len(labels) else None,
            'training_only_baseline_probability': baseline,
            'training_only_baseline_scores': baseline_score, 'brier_skill_vs_training_baseline': brier_skill,
            'scoring_rules_mix_calibration_and_discrimination': True,
            'scores_are_not_trade_execution_win_rates': True,
        }
    return {'schema': VERSION, 'endpoints': output, 'date_column': date_col,
            'reliability_bins_fixed_before_outcomes': True,
            'equal_date_weighting_is_not_an_independence_claim': True}


def risk_coverage(frame, probability_col, label_col, *, date_col='date',
                  thresholds=FIXED_THRESHOLDS):
    """Fixed-threshold descriptive risk/coverage; this function selects nothing.

    Missing labels remain in each selected denominator as conservative nonwins.
    Missing prediction rows remain in the candidate coverage denominator.
    """
    frame = pd.DataFrame(frame)
    probability, label = _probabilities(frame, probability_col), _labels(frame, label_col)
    dates = frame.get(date_col, pd.Series([None] * len(frame))).map(_date).to_numpy()
    rows = []
    for threshold in thresholds:
        threshold = _number(threshold)
        if threshold is None or not 0 <= threshold <= 1:
            raise ValueError('invalid selective threshold')
        selected = np.isfinite(probability) & (probability >= threshold)
        count, known = int(selected.sum()), int(np.isfinite(label[selected]).sum())
        wins = int((label[selected] == 1).sum())
        hit = wins/count if count else None
        rows.append({'threshold': threshold, 'n_candidates': len(frame), 'n_selected': count,
                     'coverage': count/len(frame) if len(frame) else None,
                     'n_known_labels': known, 'n_unknown_labels': count-known,
                     'selected_date_clusters': len({day for day in dates[selected] if day}),
                     'conservative_hit_rate': hit,
                     'conservative_error_rate': 1-hit if hit is not None else None,
                     'known_label_hit_rate': wins/known if known else None})
    return {'schema': VERSION, 'thresholds': rows, 'used_for_parameter_selection': False,
            'unknowns_count_as_nonwins': True, 'coverage_denominator': 'all_input_candidates',
            'actual_execution_probability': False}


def _bootstrap(groups, *, samples, block_length, seed):
    """Resample whole dates; groups columns are denominator, wins, closed, sum r."""
    count = len(groups)
    if not count or not samples:
        return None
    rng = np.random.default_rng(seed)
    if block_length == 1:
        draws = rng.integers(0, count, size=(samples, count))
    else:
        length = min(block_length, count)
        nblocks = math.ceil(count/length)
        starts = rng.integers(0, count-length+1, size=(samples, nblocks))
        draws = (starts[:, :, None] + np.arange(length)).reshape(samples, -1)[:, :count]
    totals = groups[draws].sum(axis=1)
    win = totals[:, 1] / totals[:, 0]
    mean = np.divide(totals[:, 3], totals[:, 2], out=np.full(samples, np.nan),
                     where=totals[:, 2] > 0)
    return {'win': win, 'mean': mean[np.isfinite(mean)]}


def _interval(values, quantiles=(.025, .975)):
    return [float(value) for value in np.quantile(values, quantiles)] if len(values) else None


def evaluate_trade_metrics(records, *, trials=6, bootstrap_samples=2000, block_length=5,
                           forward_only=False, actual_execution=False, pit_verified=False):
    """Conservative trade denominator and approximate dependence-aware gates.

    Selection uses the smaller of IID-date and moving-five-date Bonferroni
    percentile lower mean-return bounds.  ``trials`` is the number of fixed
    policies tried in this experiment, not a correction for all past experiments.
    Formal activation is always false: passing an approximate research statistic
    cannot certify an 80% real trading win rate.
    """
    if isinstance(trials, bool) or int(trials) != trials or trials < 1:
        raise ValueError('trials must be a positive integer')
    if isinstance(bootstrap_samples, bool) or int(bootstrap_samples) != bootstrap_samples or bootstrap_samples < 0:
        raise ValueError('bootstrap_samples must be a nonnegative integer')
    if isinstance(block_length, bool) or int(block_length) != block_length or block_length < 1:
        raise ValueError('block_length must be a positive integer')
    records = pd.DataFrame(records).to_dict('records') if isinstance(records, pd.DataFrame) else list(records)
    signals = [row for row in records if row.get('status') != 'abstain']
    attempted = [row for row in signals if row.get('status') != 'no_fill']
    closed = [row for row in attempted if row.get('status') == 'closed'
              and _number(row.get('gross_return')) is not None]
    closed_ids = {id(row) for row in closed}
    n, n_closed = len(attempted), len(closed)
    returns = [_number(row['gross_return']) for row in closed]
    wins = sum(value > 0 for value in returns)
    missing_dates, by_date = 0, {}
    for index, row in enumerate(attempted):
        day = _date(row.get('entry_date'))
        if day is None:
            missing_dates += 1
            day = 'unknown:' + str(row.get('signal_date', index))
        bucket = by_date.setdefault(day, np.zeros(4))
        bucket[0] += 1
        if id(row) in closed_ids:
            value = _number(row['gross_return'])
            bucket[1] += value > 0
            bucket[2] += 1
            bucket[3] += value
    groups = np.asarray([by_date[day] for day in sorted(by_date)], dtype=float)
    cluster = _bootstrap(groups, samples=int(bootstrap_samples), block_length=1, seed=2026101901)
    moving = _bootstrap(groups, samples=int(bootstrap_samples), block_length=int(block_length), seed=2026101902)
    alpha = .05 / int(trials)
    cluster_lower = float(np.quantile(cluster['mean'], alpha)) if cluster and len(cluster['mean']) else None
    moving_lower = float(np.quantile(moving['mean'], alpha)) if moving and len(moving['mean']) else None
    lower = min(cluster_lower, moving_lower) if cluster_lower is not None and moving_lower is not None else None
    blockers = []
    if n_closed < 20:
        blockers.append('fewer_than_20_closed_trades')
    if len(by_date) < 10:
        blockers.append('fewer_than_10_entry_date_clusters')
    if n_closed != n:
        blockers.append('unresolved_selected_attempts')
    if missing_dates:
        blockers.append('missing_entry_dates')
    if lower is None or lower <= 0:
        blockers.append('familywise_lower_mean_not_strictly_positive')
    if bootstrap_samples * alpha < 10:
        blockers.append('insufficient_bootstrap_tail_resolution')
    formal_blockers = ['approximate_historical_inference_does_not_certify_80_percent']
    if not forward_only:
        formal_blockers.append('history_previously_exposed_not_new_forward_test')
    if not actual_execution:
        formal_blockers.append('actual_order_fill_evidence_missing')
    if not pit_verified:
        formal_blockers.append('official_point_in_time_status_limits_actions_missing')
    if n_closed < 200:
        formal_blockers.append('fewer_than_200_closed_trades')
    if len(by_date) < 60:
        formal_blockers.append('fewer_than_60_entry_date_clusters')
    win_lower = min(cluster['win'].size and float(np.quantile(cluster['win'], .025)),
                    moving['win'].size and float(np.quantile(moving['win'], .025))) if cluster and moving else None
    if win_lower is None or win_lower <= .8:
        formal_blockers.append('date_dependence_diagnostic_lower_win_rate_not_above_80_percent')
    if n_closed != n or missing_dates:
        formal_blockers.append('unresolved_outcomes_or_entry_dates')
    return {
        'schema': VERSION, 'n_signals': len(signals), 'n_cancelled': len(signals)-n,
        'n_attempts': n, 'n_closed': n_closed, 'n_unresolved': n-n_closed,
        'n_wins': wins, 'entry_date_clusters': len(by_date), 'n_missing_entry_dates': missing_dates,
        'win_rate': wins/n if n else None,
        'closed_win_rate': wins/n_closed if n_closed else None,
        'ge3_rate': sum(value >= .03-1e-12 for value in returns)/n if n else None,
        'ge6_rate': sum(value >= .06-1e-12 for value in returns)/n if n else None,
        'mean_closed_gross_return': float(np.mean(returns)) if returns else None,
        'mean_gross_return': float(np.mean(returns)) if returns and n_closed == n else None,
        'descriptive_beta11_shrunk_win_rate': (wins+1)/(n+2) if n else None,
        'beta_shrinkage_is_not_a_dependent_trade_posterior_or_confidence_bound': True,
        'date_cluster_win_ci95': _interval(cluster['win']) if cluster else None,
        'moving_block_win_ci95': _interval(moving['win']) if moving else None,
        'date_cluster_mean_ci95': _interval(cluster['mean']) if cluster else None,
        'moving_block_mean_ci95': _interval(moving['mean']) if moving else None,
        'familywise_date_cluster_mean_lower': cluster_lower,
        'familywise_moving_block_mean_lower': moving_lower,
        'familywise_lower_mean_return': lower,
        'selection_gate': not blockers, 'selection_blockers': blockers,
        'bootstrap': {'samples': int(bootstrap_samples), 'seed_date_cluster': 2026101901,
                      'seed_moving_block': 2026101902, 'unit': 'all_stocks_of_entry_date',
                      'moving_block_length_active_entry_dates': int(block_length),
                      'fixed_policies_tried': int(trials), 'one_sided_alpha_per_policy': alpha,
                      'multiple_comparison_method': 'Bonferroni_percentile_lower_mean_approximation',
                      'covers_past_research_trials': False,
                      'date_clusters_are_not_claimed_independent': True,
                      'moving_blocks_require_local_stationarity_and_weak_dependence': True,
                      'confidence_intervals_are_approximate_not_finite_sample_guarantees': True},
        'unknowns_count_as_nonwins': True,
        'denominator': 'closed_plus_unresolved_attempts_excluding_predeclared_no_fill_and_abstain',
        'formal_80_gate': False, 'formal_80_blockers': formal_blockers,
        'actual_win_rate': None, 'real_execution_win_rate': None,
        'formal_trade_ready': False, 'costs_included': False,
    }
