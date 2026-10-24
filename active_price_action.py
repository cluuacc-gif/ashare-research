"""Versioned ex-ante price structures for the active manual rule, not probabilities."""
from __future__ import annotations
import statistics
import math

SCHEMA = 'active-price-action-v2'


def snapshot(rows, features, entry_cap=None):
    if not features.get('available') or len(rows) < 61:
        return {'schema': SCHEMA, 'available': False, 'patterns': [],
                'reasons': ['active_price_structure_history_unavailable']}
    ordered = sorted(rows, key=lambda row: row['trade_date'])
    if (len({row['trade_date'] for row in ordered}) != len(ordered)
            or features.get('as_of') != ordered[-1]['trade_date']):
        return {'schema': SCHEMA, 'available': False, 'patterns': [],
                'reasons': ['active_price_structure_asof_or_duplicate_mismatch']}
    required = ('atr14_pct','compression5_20','volume_ratio20','close_location','upper_wick_atr')
    if (any(not isinstance(features.get(key),(int,float)) or isinstance(features.get(key),bool)
            or not math.isfinite(features[key]) for key in required)
            or features['atr14_pct'] <= 0):
        return {'schema': SCHEMA, 'available': False, 'patterns': [],
                'reasons': ['active_price_structure_numeric_features_unavailable']}
    bars = ordered[-61:]
    last, prior = bars[-1], bars[-2]
    close = last['close']
    atr = features['atr14_pct'] * close
    before5 = bars[-6:-1]
    before3 = bars[-4:-1]
    bullish_recovery = last['close'] > last['open'] and last['close'] > prior['high']
    prior10_volume = statistics.mean(row['volume'] for row in bars[-14:-4])
    pullback_volume_ratio = statistics.mean(row['volume'] for row in before3) / prior10_volume
    pullback = (bullish_recovery and any(row['close'] < row['open'] for row in before3)
        and pullback_volume_ratio <= 1 and min(row['low'] for row in before3) >=
        statistics.mean(row['close'] for row in bars[-20:]) - .25 * atr)
    compact_breakout = (bullish_recovery and close >= max(row['high'] for row in before5)
        and features['compression5_20'] <= 1 and 1 <= features['volume_ratio20'] <= 2.5
        and 0 <= (close - max(row['high'] for row in before5)) / atr <= .5)
    patterns = (['reduced_volume_pullback_recovery'] if pullback else []) + (
        ['compact_breakout'] if compact_breakout else [])
    reasons = []
    if features['close_location'] < .65:
        reasons.append('active_close_acceptance_below_0.65')
    if features['upper_wick_atr'] > .5:
        reasons.append('active_upper_wick_exceeds_half_ATR')
    if not patterns:
        reasons.append('no_recovery_or_compact_breakout_structure')
    # A pivot is confirmed by two completed bars on each side, all at/before T.
    # There is no invented ceiling when none is identified; this is not proof
    # that a future 3% move is available or achievable.
    pivots = []
    for index in range(2, len(bars)-2):
        bar = bars[index]
        neighbors = bars[index-2:index] + bars[index+1:index+3]
        if (bar['high'] > max(row['high'] for row in neighbors)
                and bar['high'] - min(row['low'] for row in neighbors) >= .5 * atr):
            pivots.append(bar['high'])
    ceiling = min((high for high in pivots if entry_cap is not None and high > close), default=None)
    room = ceiling / entry_cap - 1 if ceiling is not None and entry_cap else None
    if ceiling is not None and entry_cap * 1.03 + .1 * atr > ceiling + 1e-9:
        reasons.append('confirmed_resistance_before_3percent_target_plus_buffer')
    return {'schema': SCHEMA, 'available': True, 'patterns': patterns,
        'pullback_volume_ratio': pullback_volume_ratio,
        'close_location': features['close_location'], 'upper_wick_atr': features['upper_wick_atr'],
        'support_last5_low': min(row['low'] for row in bars[-5:]),
        'confirmed_overhead_resistance': ceiling, 'room_from_entry_cap': room,
        'resistance_basis': 'strict_pivot_high_with_two_known_bars_each_side_last61',
        'no_identified_resistance_is_guaranteed_room': False,
        'reasons': reasons, 'score_is_probability': False}
