"""Fail closed outside the requested Shanghai research-mail minute.

This controls when the client may submit a message to SMTP. A recipient's
mail provider can still delay delivery after accepting that submission.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import collector as c


TZ = ZoneInfo('Asia/Shanghai')
SCHEDULES = {'morning': dt.time(9, 0), 'auction': dt.time(9, 25),
             'evening': dt.time(18, 0)}
WINDOW = dt.timedelta(minutes=1)


def scheduled_at(stage: str, day: str) -> dt.datetime:
    """Return the requested instant; reject invalid stages and ISO dates."""
    if stage not in SCHEDULES:
        raise ValueError('unknown research-mail stage')
    parsed = dt.date.fromisoformat(day)
    if parsed.isoformat() != day:
        raise ValueError('research day must be an ISO calendar date')
    return dt.datetime.combine(parsed, SCHEDULES[stage], tzinfo=TZ)


def check(stage, day, at=None, preview=False):
    """Allow previews or a same-day submission within the exact target minute.

    Exchange-session checks are deliberately separate: callers must apply
    ``mail_gate.check`` first, including for previews.
    """
    current = at if at is not None else c.now()
    result = {'target_date': day, 'stage': stage, 'allowed': False,
              'smtp_accepted': False, 'receipt_confirmed': False}
    if not isinstance(current, dt.datetime) or current.tzinfo is None or current.utcoffset() is None:
        return {**result, 'state': 'invalid_clock', 'reason': 'timezone_aware_clock_required'}
    current = current.astimezone(TZ)
    result.update(execution_date=current.date().isoformat(), checked_at=current.isoformat())
    # An explicit offline preview has no submission time or stage requirement.
    if preview:
        return {**result, 'allowed': True, 'state': 'preview_allowed', 'reason': None}
    if stage not in SCHEDULES:
        return {**result, 'state': 'invalid_stage', 'reason': 'valid_research_stage_required'}
    try:
        target = scheduled_at(stage, day)
    except (TypeError, ValueError):
        return {**result, 'state': 'invalid_research_day', 'reason': 'iso_research_day_required'}
    end = target + WINDOW
    result.update(scheduled_at=target.isoformat(), window_end_at=end.isoformat())
    if current.date() != target.date():
        return {**result, 'state': 'wrong_research_day', 'reason': 'target_date_differs_from_execution_date'}
    if current < target:
        return {**result, 'state': 'before_send_window', 'reason': 'research_mail_not_due'}
    if current >= end:
        return {**result, 'state': 'missed_window', 'reason': 'research_mail_deadline_passed'}
    return {**result, 'allowed': True, 'state': 'send_window_open', 'reason': None}
