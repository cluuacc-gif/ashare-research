"""Isolated control-flow tests. No synthetic market observations or production writes."""
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import cloud_daily
import trade_calendar as cal
import two_stage


class CalendarTest(unittest.TestCase):
    def test_friday_to_monday(self):
        self.assertEqual(cal.adjacent("2026-09-11", 1), "2026-09-14")

    def test_monday_uses_friday(self):
        self.assertEqual(cal.adjacent("2026-09-14", -1), "2026-09-11")

    def test_mid_autumn(self):
        self.assertEqual(cal.adjacent("2026-09-24", 1), "2026-09-28")

    def test_national_day(self):
        self.assertEqual(cal.adjacent("2026-09-30", 1), "2026-10-08")

    def test_makeup_weekend_stays_closed(self):
        self.assertFalse(cal.is_session("2026-10-10"))
        self.assertFalse(cal.is_session("2026-09-20"))

    def test_next_year_fails_closed(self):
        with self.assertRaises(ValueError):
            cal.is_session("2027-01-04")

    def test_weekend_makes_no_network_calls(self):
        at = dt.datetime(2026, 9, 12, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as root, patch("cloud_daily.c.now", return_value=at), patch("cloud_daily.c.ak_call", side_effect=AssertionError("network prohibited")):
            result = cloud_daily.collect(Path(root)/"out")
        self.assertEqual(result["status"], "SKIPPED_MARKET_CLOSED")

    def test_before_18_rejected(self):
        at = dt.datetime(2026, 9, 14, 17, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as root, patch("cloud_daily.c.now", return_value=at):
            with self.assertRaisesRegex(ValueError, "18:00"):
                cloud_daily.collect(Path(root)/"out")

    def test_stale_request_rejected(self):
        at = dt.datetime(2026, 9, 14, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
        with self.assertRaisesRegex(ValueError, "stale/future"):
            cloud_daily.request_for_today({"schema_version": "1.0", "operation": "evening_prepare", "target_date": "2026-09-11"}, at)

    def test_plain_daily_does_not_bootstrap(self):
        at = dt.datetime(2026, 9, 14, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(cloud_daily.request_for_today(None, at)["bootstrap_symbols"], [])

    def test_evening_request_enumerates_markets_without_duplicates(self):
        # Control-flow fixture only, no quote values and no database writes.
        inv = {"sha256": "a"*64, "missing_250_symbols": ["600000.SH", "000001.SZ", "920000.BJ"]}
        with patch("two_stage.inventory", return_value=inv):
            result = two_stage.evening_request("unused", "2026-09-14T18:00:00+08:00")
        self.assertEqual(result["bootstrap_symbols"], inv["missing_250_symbols"])


if __name__ == "__main__":
    unittest.main()
