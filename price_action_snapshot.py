"""Standard-library price-action math shared by cloud research and mail hosts.

Only trailing, already-known daily bars belong in this helper. Callers own
source/time/official-status checks. A mathematical structure is never an order,
an actual fill, or a calibrated probability of profit.
"""
from __future__ import annotations

import math
import statistics

SCHEMA = 'price-action-snapshot-v1'
PATTERNS = ('compression_breakout', 'trend_pullback', 'higher_lows')
FEATURES = ['atr14_pct', 'range_atr', 'close_location', 'body_atr', 'upper_wick_atr',
            'efficiency20', 'compression5_20', 'breakout_atr', 'higher_high_low3',
            'pullback_depth_atr', 'relative_return5', 'price_action_score',
            'ret1', 'ret5', 'ret20', 'ma5_gap', 'ma20_gap', 'median_amount20', 'volume_ratio20']


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _valid(row):
    if not isinstance(row, dict) or row.get('conflict') != 0:
        return False
    values = [_number(row.get(key)) for key in ('open', 'high', 'low', 'close', 'volume')]
    if any(value is None or value <= 0 for value in values):
        return False
    open_, high, low, close, _ = values
    return low <= min(open_, close) <= max(open_, close) <= high


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _clip(value):
    return max(0.0, min(1.0, value))


def _between(value, lower, upper):
    return value is not None and lower <= value <= upper


def _patterns(features):
    f = features
    common = (f['median_amount20'] is not None and f['median_amount20'] >= 20_000_000
        and _between(f['atr14_pct'], .008, .05)
        and f['relative_return5'] >= 0
        and f['price_action_score'] is not None and f['price_action_score'] >= 55)
    if not common:
        return []
    found = []
    if (f['compression5_20'] is not None and f['compression5_20'] <= .9
            and _between(f['breakout_atr'], -.2, .5)
            and f['close_location'] >= .65 and f['body_atr'] is not None and f['body_atr'] > 0
            and _between(f['volume_ratio20'], 1, 3.5) and _between(f['ret1'], -.01, .07)):
        found.append('compression_breakout')
    if (f['ret20'] > 0 and _between(f['ma20_gap'], 0, .12)
            and _between(f['ma5_gap'], -.03, .03) and _between(f['ret1'], -.03, .01)
            and f['close_location'] >= .45
            and f['volume_ratio20'] is not None and f['volume_ratio20'] <= 1
            and _between(f['pullback_depth_atr'], 0, 2.5) and f['efficiency20'] >= .2):
        found.append('trend_pullback')
    if (f['higher_high_low3'] >= 2 / 3 - 1e-12 and f['ret5'] >= 0
            and f['close_location'] >= .6 and f['upper_wick_atr'] is not None
            and f['upper_wick_atr'] <= .4 and _between(f['ma20_gap'], 0, .12)):
        found.append('higher_lows')
    return found


def price_action_snapshot(rows, *, market_return5=0, market_breadth=None, market_mean_return=None):
    """Compute the same ex-ante formula on every host without optional packages."""
    ordered = sorted(rows, key=lambda row: row['trade_date'])
    dates = [row['trade_date'] for row in ordered]
    if len(set(dates)) != len(dates):
        raise ValueError('duplicate price-action input dates')
    if len(ordered) < 61 or not all(_valid(row) for row in ordered[-61:]):
        return {'available': False, 'reason': 'requires_61_valid_trailing_bars',
                'price_action_score': None, 'eligible_patterns': [], 'research_signal': False}
    market5 = _number(market_return5)
    if market5 is None:
        raise ValueError('market_return5 must be finite')
    # All formula windows fit within the last 61 validated records. Earlier
    # records cannot influence a per-stock snapshot or its dependency footprint.
    selected = ordered[-61:]
    converted = [{key: float(row[key]) for key in ('open', 'high', 'low', 'close', 'volume')}
                 for row in selected]
    closes = [row['close'] for row in converted]
    last = converted[-1]
    true_ranges = [max(row['high'] - row['low'], abs(row['high'] - previous['close']),
                       abs(row['low'] - previous['close']))
                   for previous, row in zip(converted, converted[1:])]
    atr = statistics.mean(true_ranges[-14:])
    span = last['high'] - last['low']
    path = math.fsum(abs(new - old) for old, new in zip(closes[-21:-1], closes[-20:]))
    prior_tr20 = statistics.mean(true_ranges[-21:-1])
    prior_tr5 = statistics.mean(true_ranges[-6:-1])
    amounts = [_number(row.get('amount')) for row in selected[-20:]]
    median_amount = (statistics.median(amounts) if all(value is not None and value > 0 for value in amounts)
                     else None)
    f = {
        'atr14_pct': atr / last['close'], 'range_atr': _ratio(span, atr),
        'close_location': (last['close'] - last['low']) / span if span else .5,
        'body_atr': _ratio(last['close'] - last['open'], atr),
        'upper_wick_atr': _ratio(last['high'] - max(last['open'], last['close']), atr),
        'efficiency20': abs(closes[-1] - closes[-21]) / path if path else 0,
        'compression5_20': _ratio(prior_tr5, prior_tr20),
        'breakout_atr': _ratio(last['close'] - max(row['high'] for row in converted[-21:-1]), atr),
        'higher_high_low3': statistics.mean(float(new['high'] > old['high'] and new['low'] > old['low'])
            for old, new in zip(converted[-4:-1], converted[-3:])),
        'pullback_depth_atr': _ratio(max(row['high'] for row in converted[-11:-1]) - last['close'], atr),
        'ret1': closes[-1] / closes[-2] - 1, 'ret5': closes[-1] / closes[-6] - 1,
        'ret20': closes[-1] / closes[-21] - 1,
        'ma5_gap': closes[-1] / statistics.mean(closes[-5:]) - 1,
        'ma20_gap': closes[-1] / statistics.mean(closes[-20:]) - 1,
        'median_amount20': median_amount,
        'volume_ratio20': last['volume'] / statistics.mean(row['volume'] for row in converted[-21:-1]),
    }
    f['relative_return5'] = f['ret5'] - market5
    if f['range_atr'] is None or f['compression5_20'] is None:
        f['price_action_score'] = None
    else:
        f['price_action_score'] = (20 * _clip((f['ret20'] + .03) / .13)
            + 20 * _clip((f['relative_return5'] + .02) / .07)
            + 15 * _clip(f['efficiency20']) + 15 * _clip(f['close_location'])
            + 15 * _clip((3 - f['range_atr']) / 2)
            + 15 * _clip((1.2 - f['compression5_20']) / .5))
    patterns = _patterns(f)
    breadth, mean_return = _number(market_breadth), _number(market_mean_return)
    context_known = breadth is not None and mean_return is not None
    permitted = patterns if context_known and breadth >= .4 and mean_return >= -.01 else []
    return {'available': True, 'as_of': ordered[-1]['trade_date'], **f,
            'eligible_patterns': patterns, 'market_context_known': context_known,
            'market_return5_reference': market5, 'market_context_eligible_patterns': permitted,
            'research_signal': bool(permitted), 'score_is_probability': False,
            'formal_trade_ready': False}
