"""Admission boundary tests use fixture metadata, never market performance."""
import copy
from datetime import datetime, timezone
from html.parser import HTMLParser
import unittest

import outlook_admission as admission


def fixture(base='2026-10-16', target='2026-10-19'):
    return {'preparation_ready': True, 'base_trade_date': base,
            'next_trade_date': target, 'release_tag': f'prepared-{base}-fixture-1',
            'generated_at': f'{base}T18:00:00+08:00',
            'published_at': f'{base}T18:05:00+08:00',
            'database': {'name': 'market.sqlite3', 'sha256': 'a' * 64, 'bytes': 123}}


def at(value):
    return datetime.fromisoformat(value)


class AdmissionTests(unittest.TestCase):
    def test_sunday_preserves_friday_base_and_monday_target(self):
        source = fixture()
        original = copy.deepcopy(source)
        result = admission.evaluate(source, at('2026-10-18T13:50:00+08:00'))
        self.assertTrue(result['ready'])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['base_trade_date'], '2026-10-16')
        self.assertEqual(result['target_trade_date'], '2026-10-19')
        self.assertEqual(source, original)

    def test_exclusive_0915_boundary(self):
        for timestamp, ready in [('09:14:59.999999', True), ('09:15:00', False),
                                 ('09:15:00.000001', False)]:
            with self.subTest(timestamp=timestamp):
                result = admission.evaluate(fixture(), at(f'2026-10-19T{timestamp}+08:00'))
                self.assertIs(result['ready'], ready)
                self.assertEqual(result['status'], 'ready' if ready else 'waiting_for_next_prepared_close')

    def test_monday_intraday_waits_without_fabricating_new_close(self):
        result = admission.evaluate(fixture(), at('2026-10-19T13:50:00+08:00'))
        self.assertEqual(result['status'], 'waiting_for_next_prepared_close')
        self.assertEqual(result['base_trade_date'], '2026-10-16')
        self.assertEqual(result['target_trade_date'], '2026-10-19')
        self.assertIn('09:15', result['reason'])

    def test_monday_after_close_still_waits_with_friday_source(self):
        result = admission.evaluate(fixture(), at('2026-10-19T18:30:00+08:00'))
        self.assertEqual(result['status'], 'waiting_for_next_prepared_close')

    def test_new_monday_prepared_close_targets_tuesday(self):
        result = admission.evaluate(fixture('2026-10-19', '2026-10-20'),
                                    at('2026-10-19T18:30:00+08:00'))
        self.assertTrue(result['ready'])
        self.assertEqual(result['target_trade_date'], '2026-10-20')

    def test_real_time_in_another_timezone_has_same_result(self):
        result = admission.evaluate(fixture(), at('2026-10-19T01:15:00+00:00'))
        self.assertEqual(result['checked_at'], '2026-10-19T09:15:00+08:00')
        self.assertFalse(result['ready'])

    def test_unprepared_missing_or_malformed_metadata_fails_closed(self):
        changes = [('preparation_ready', False), ('preparation_ready', 1),
                   ('base_trade_date', '2026-10-17'), ('next_trade_date', '2026-10-20'),
                   ('release_tag', 'prepared-2026-10-19-fixture-1'),
                   ('generated_at', '2026-10-16T14:59:59+08:00'),
                   ('generated_at', '2026-10-16T18:00:00'),
                   ('published_at', '2026-10-18T13:51:00+08:00'),
                   ('published_at', '2026-10-16T17:59:00+08:00'),
                   ('database', {'name': '../market.sqlite3', 'sha256': 'a' * 64}),
                   ('database', {'name': 'market.sqlite3', 'sha256': 'wrong'}),
                   ('database_latest_date', '2026-10-19'),
                   ('information_cutoff', '2026-10-19T10:00:00+08:00')]
        for key, value in changes:
            source = fixture()
            source[key] = value
            with self.subTest(key=key, value=value):
                result = admission.evaluate(source, at('2026-10-18T13:50:00+08:00'))
                self.assertEqual(result['status'], 'unverified_source')
                self.assertFalse(result['ready'])
        for value in (None, {}, [], 'manifest'):
            self.assertEqual(admission.evaluate(value, at('2026-10-18T13:50:00+08:00'))['status'],
                             'unverified_source')

    def test_future_base_cannot_pass_before_close(self):
        source = fixture('2026-10-19', '2026-10-20')
        source.update(generated_at='2026-10-19T13:00:00+08:00',
                      published_at='2026-10-19T13:05:00+08:00')
        self.assertEqual(admission.evaluate(source, at('2026-10-19T13:50:00+08:00'))['status'],
                         'unverified_source')

    def test_unknown_calendar_year_fails_closed_for_source_and_clock(self):
        for source, now in [(fixture('2026-12-31', '2027-01-01'),
                             '2026-12-31T19:00:00+08:00'),
                            (fixture(), '2027-01-04T08:00:00+08:00')]:
            with self.subTest(now=now):
                self.assertEqual(admission.evaluate(source, at(now))['status'], 'unverified_source')

    def test_naive_or_missing_clock_is_rejected(self):
        for now in (datetime(2026, 10, 18, 12), None, '2026-10-18T12:00:00+08:00'):
            with self.subTest(now=now), self.assertRaises(ValueError):
                admission.evaluate(fixture(), now)

    def test_manifest_hash_is_never_claimed_as_downloaded_database_verification(self):
        source = fixture()
        source['database']['sha256'] = 'b' * 64
        result = admission.evaluate(source, at('2026-10-18T13:50:00+08:00'))
        self.assertTrue(result['manifest_metadata_checked'])
        self.assertFalse(result['database_hash_verified'])
        self.assertFalse(result['database_identity_verified'])

    def test_every_status_preserves_research_only_gates(self):
        for source, now in [(fixture(), '2026-10-18T12:00:00+08:00'),
                            (fixture(), '2026-10-19T13:50:00+08:00'),
                            ({}, '2026-10-18T12:00:00+08:00')]:
            result = admission.evaluate(source, at(now))
            for field in ('formal_trade_ready', 'execution_win_rate_80_validated',
                          'main_policy_active', 'smtp_enabled', 'orders_enabled',
                          'smtp_sent', 'orders_submitted'):
                self.assertIs(result[field], False)
            self.assertIsNone(result['actual_win_rate'])


class ReportTests(unittest.TestCase):
    def test_report_describes_clock_actual_dates_fixed_plan_and_pending_work(self):
        result = admission.evaluate(fixture(), at('2026-10-19T13:50:00+08:00'))
        report = admission.render(result)
        for text in ('2026-10-19 13:50:00 北京时间', '2026-10-16', '2026-10-19',
                     '09:15', '09:40', '14:40', '14:50', '16:35', '可能排队或延迟',
                     '首个未来样本', '官方时点状态', '实时行情', '独立云主机',
                     '实际胜率未知', '主要交易策略尚未启用', '费用口径固定为0'):
            self.assertIn(text, report)
        self.assertIn('<html lang="zh-CN">', report)
        self.assertIn('</html>', report)
        self.assertNotIn('<script', report)

    def test_all_untrusted_status_strings_are_escaped(self):
        hostile = '<script>alert("x")</script>&'
        result = {key: hostile for key in ('status', 'reason', 'checked_at',
            'base_trade_date', 'target_trade_date', 'prospective_deadline', 'source_release')}
        report = admission.render(result)
        self.assertNotIn('<script>', report)
        self.assertIn('&lt;script&gt;', report)
        self.assertIn('&quot;x&quot;', report)
        parser = HTMLParser()
        parser.feed(report)
        parser.close()

    def test_transport_check_never_counts_as_trade_or_win_evidence(self):
        result = admission.evaluate(fixture(), at('2026-10-19T13:50:00+08:00'))
        for available, label in [(0, '本次不可用'), (1, '已接通')]:
            result['minute_transport'] = {'symbols_requested': 1, 'symbols_available': available,
                'validation_scope': 'transport_only_not_trading_performance'}
            report = admission.render(result)
            self.assertIn(label, report)
            self.assertIn('不计为新交易样本', report)
            self.assertIn('不验证收益或胜率', report)


if __name__ == '__main__':
    unittest.main()
