#!/usr/bin/env python3
"""Exchange-announcement calendar. Unknown years fail closed, never use workdays."""
import datetime as dt
from pathlib import Path
import json

CALENDAR = Path(__file__).with_name("exchange_calendar_2026.json")


def load_calendar():
    value = json.loads(CALENDAR.read_text(encoding="utf-8"))
    if {s["exchange"] for s in value["sources"]} != {"SH", "SZ", "BJ"}:
        raise ValueError("three exchange sources required")
    return value


def is_session(day):
    day = dt.date.fromisoformat(str(day))
    value = load_calendar()
    if day.year != value["year"]:
        raise ValueError("exchange calendar must be refreshed for this year")
    if day.weekday() >= 5:
        return False
    return not any(a <= day.isoformat() <= b for a, b in value["closed_ranges"])


def adjacent(day, direction):
    if direction not in (-1, 1):
        raise ValueError("direction must be -1 or 1")
    value = dt.date.fromisoformat(str(day))
    for _ in range(32):
        value += dt.timedelta(days=direction)
        if is_session(value):
            return value.isoformat()
    raise ValueError("no verified adjacent session")


def context(day):
    day = dt.date.fromisoformat(str(day)).isoformat()
    result = {"date": day, "is_session": is_session(day), "calendar_verified": True,
              "basis": "SH/SZ/BJ annual exchange closure announcements; no government make-up workdays",
              "calendar_file": CALENDAR.name, "year": load_calendar()["year"],
              "sources": load_calendar()["sources"]}
    # Adjacent years remain unknown rather than extrapolating holiday dates.
    for key, direction in (("previous_session", -1), ("next_session", 1)):
        try:
            result[key] = adjacent(day, direction)
        except ValueError:
            result[key] = None
    return result
