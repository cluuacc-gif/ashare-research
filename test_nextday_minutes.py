"""Synthetic transport/causality checks, never investment-performance evidence."""
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

import nextday_minutes as m


def payload(lines=None):
    return json.dumps({'data': {'code': '000001', 'market': 0, 'klines': lines or [
        '2026-10-16 09:31,10,10.02,10.03,9.99,100,100100,0,0,0,0',
        '2026-10-16 09:32,10.02,10.03,10.04,10,100,100300,0,0,0,0']}}).encode()


class MinuteTests(unittest.TestCase):
    def test_true_ohlc_schema_and_end_timestamp(self):
        result = m.parse(payload(), '000001.SZ', '2026-10-16T16:00:00+08:00')
        self.assertEqual(result['bars'][0]['start_at'], '2026-10-16T09:30:00+08:00')
        self.assertEqual(result['bars'][0]['high'], 10.03)
        self.assertFalse(result['actual_fill_verified'])
        self.assertFalse(result['live_seen_at_decision_verified'])

    def test_zero_open_cannot_be_replaced_by_later_close(self):
        with self.assertRaises(ValueError):
            m.parse(payload(['2026-10-16 09:31,0,10,10,10,100,100000']),
                '000001.SZ', '2026-10-16T16:00:00+08:00')

    def test_duplicate_or_unsorted_minutes_rejected(self):
        rows = json.loads(payload())['data']['klines']
        for lines in ([rows[0], rows[0]], rows[::-1]):
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                m.parse(payload(lines), '000001.SZ', '2026-10-16T16:00:00+08:00')

    def test_incomplete_minutes_not_exposed(self):
        result = m.parse(payload(), '000001.SZ', '2026-10-16T09:31:20+08:00')
        self.assertEqual(len(result['bars']), 1)

    def test_wrong_security_or_market_is_rejected(self):
        for field, value in [('code', '000002'), ('market', 1)]:
            data = json.loads(payload()); data['data'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                m.parse(json.dumps(data).encode(), '000001.SZ', '2026-10-16T16:00:00+08:00')

    def test_auction_or_lunch_rows_not_continuous_minutes(self):
        for clock in ('09:30', '12:00', '13:00'):
            with self.subTest(clock=clock), self.assertRaises(ValueError):
                m.parse(payload([f'2026-10-16 {clock},10,10,10,10,1,1000']),
                    '000001.SZ', '2026-10-16T16:00:00+08:00')

    def test_network_failure_is_archived_as_missing_not_a_fabricated_price(self):
        def fail(url): raise OSError('offline fixture')
        with tempfile.TemporaryDirectory() as root:
            result = m.capture(['000001.SZ'], root, fetch=fail)
            self.assertEqual(result['000001.SZ']['status'], 'unavailable')
            self.assertEqual(result['000001.SZ']['bars'], [])
            self.assertTrue((Path(root)/'capture.json').exists())

    def test_raw_response_preserved_and_hash_linked(self):
        with tempfile.TemporaryDirectory() as root:
            result = m.capture(['000001.SZ'], root, fetch=lambda _: payload(),
                clock=lambda: dt.datetime(2026, 10, 16, 16, tzinfo=m.TZ))['000001.SZ']
            self.assertEqual((Path(root)/result['raw_asset']).read_bytes(), payload())

    def test_unknown_exchange_mapping_and_excess_fetch_budget_fail_closed(self):
        with self.assertRaises(ValueError): m.endpoint('920001.BJ')
        with self.assertRaises(ValueError): m.endpoint('000001.SZ&token=x')
        with tempfile.TemporaryDirectory() as root, self.assertRaises(ValueError):
            m.capture([f'{i:06}.SZ' for i in range(49)], root)


if __name__ == '__main__': unittest.main()
