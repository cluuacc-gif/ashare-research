"""Fixed market-context routes and precommitted 1/2-session rules, not forecasts."""
from __future__ import annotations

import math

TREND = 'trend_continuation'
REBOUND = 'weak_to_strong_rebound'
ROUTES = (TREND, REBOUND)


def classify(market):
    keys = ('base_breadth', 'base_return_1d', 'base_return_5d')
    values = [market.get(key) for key in keys]
    if (any(not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) for value in values) or not 0 <= values[0] <= 1
            or not -.3 <= values[1] <= .3 or not -.8 <= values[2] <= 1):
        return {'route': None, 'reasons': ['market_context_unknown'], 'is_probability': False}
    breadth, day, week = values
    if breadth < .2 or day < -.04 or week < -.10:
        return {'route': None, 'reasons': ['extreme_market_stress_watch_only'], 'is_probability': False}
    overheated = breadth >= .8 and day > .02 and week > .06
    route = REBOUND if overheated else TREND if breadth >= .45 and day >= -.01 and week >= -.03 else REBOUND
    return {'route': route, 'state':'overheated_defensive' if overheated else 'trend' if route==TREND else 'weak_rebound', 'reasons': [], 'is_probability': False,
            'T_day_direction_is_a_prediction': False,
            'entry_requires_D1_individual_stock_confirmation': True}


def candidate_reasons(row, boards, market):
    context = classify(market)
    # The old six-arm cohort remains immutable. Only this new active policy
    # replaces its old market gate; source, flow and leadership vetoes survive.
    removed = set(market.get('reasons', ()))
    if context['route'] == REBOUND:
        removed |= {'sector_phase_not_strengthening', 'sector_breadth_below_0.55'}
    reasons = [reason for reason in row['reasons'] if reason not in removed]
    reasons.extend(context['reasons'])
    if context['route'] == REBOUND:
        board = boards.get(row.get('board_id'), {})
        components = board.get('components', {})
        breadth, day = components.get('breadth'), components.get('return_1d')
        if (not isinstance(breadth,(int,float)) or isinstance(breadth,bool)
                or not math.isfinite(breadth) or not .4 <= breadth <= 1):
            reasons.append('rebound_board_breadth_below_0.4_or_unknown')
        if (not isinstance(day,(int,float)) or isinstance(day,bool)
                or not math.isfinite(day) or not -.02 <= day <= .3):
            reasons.append('rebound_board_daily_loss_beyond_0.02_or_unknown')
    return context, reasons


def entry_confirmation(prefix, frozen_close, route):
    from nextday_forward import trigger
    if route not in ROUTES or not prefix or not trigger(prefix, 'confluence_late_confirmation', frozen_close):
        return False
    last = prefix[-1]
    # A bounce from a weak opening is not a recovery of the prior close.
    if not frozen_close <= last['close'] <= frozen_close * 1.035:
        return False
    if route == REBOUND:
        span = last['high'] - last['low']
        return (len(prefix) >= 30 and span > 0 and (last['close'] - last['low']) / span >= .7
                and last['close'] >= max(row['high'] for row in prefix[:30]))
    return True


def continuation(prefix, buy):
    """Only completed first-sell-session bars ending at/before 14:50 enter here."""
    if len(prefix) < 10 or prefix[-1]['volume'] <= 0:
        return False
    volume = sum(row['volume'] for row in prefix)
    if volume <= 0:
        return False
    weighted = sum(row['close'] * row['volume'] for row in prefix) / volume
    return (prefix[-1]['close'] >= buy and prefix[-1]['close'] >= weighted
            and min(row['low'] for row in prefix[-5:]) >= min(row['low'] for row in prefix[-10:-5]))


def schedule(plan):
    """Validate the frozen deadline; a losing trade cannot request a new one."""
    from trade_calendar import adjacent
    if plan.get('holding_policy_version') != 3 or plan.get('setup_route') not in ROUTES:
        raise ValueError('unrecognized holding policy')
    sessions = plan.get('max_hold_after_entry_sessions')
    expected = 1 if plan['setup_route'] == REBOUND else 2
    if type(sessions) is not int or sessions != expected:
        raise ValueError('holding period must match the precommitted route')
    first = adjacent(plan['target_trade_date'], 1)
    deadline = first if sessions == 1 else adjacent(first, 1)
    if plan.get('D2_trade_date') != first or plan.get('planned_exit_trade_date') != deadline:
        raise ValueError('holding dates changed or are not verified adjacent sessions')
    return first, deadline
