"""Boundary and mocked-SMTP checks; fixtures never connect to a mail server."""
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mail_timing
import send_mail


def at(value):
    return dt.datetime.fromisoformat(value)


class TimingTests(unittest.TestCase):
    def test_exact_start_and_last_microsecond_allowed_but_end_rejected(self):
        for stage, minute in [('morning', '09:00'), ('auction', '09:25'), ('evening', '18:00')]:
            start = mail_timing.scheduled_at(stage, '2026-10-09')
            self.assertEqual(start.isoformat(), f'2026-10-09T{minute}:00+08:00')
            for instant in [start, start + dt.timedelta(seconds=59, microseconds=999999)]:
                self.assertTrue(mail_timing.check(stage, '2026-10-09', instant)['allowed'])
            self.assertEqual(mail_timing.check(stage, '2026-10-09', start - dt.timedelta(microseconds=1))['state'],
                             'before_send_window')
            self.assertEqual(mail_timing.check(stage, '2026-10-09', start + dt.timedelta(minutes=1))['state'],
                             'missed_window')

    def test_timezone_conversion_and_same_day_are_required(self):
        self.assertTrue(mail_timing.check('morning', '2026-10-09', at('2026-10-09T01:00:00+00:00'))['allowed'])
        for day in ['2026-10-08', '2026-10-12']:
            self.assertEqual(mail_timing.check('evening', day, at('2026-10-09T18:00:00+08:00'))['state'],
                             'wrong_research_day')
        self.assertEqual(mail_timing.check('evening', '2026-10-09', dt.datetime(2026, 10, 9, 18))['state'],
                         'invalid_clock')

    def test_invalid_stage_and_day_fail_closed(self):
        instant = at('2026-10-09T18:00:00+08:00')
        for stage in [None, '', 'late', 'EVENING']:
            self.assertEqual(mail_timing.check(stage, '2026-10-09', instant)['state'], 'invalid_stage')
        for day in [None, '2026-02-30', '20261009']:
            self.assertEqual(mail_timing.check('evening', day, instant)['state'], 'invalid_research_day')

    def test_offline_preview_has_no_submission_window(self):
        self.assertTrue(mail_timing.check(None, '2026-10-09', at('2026-10-17T12:00:00+08:00'), preview=True)['allowed'])


class SenderTimingTests(unittest.TestCase):
    def setup_case(self, root, *, stage='evening', day='2026-10-09', missing=False, preview=False):
        body = root / 'body.txt'
        if not missing:
            body.write_text('fixture research report', encoding='utf-8')
        status = root / 'delivery.json'
        argv = ['send_mail.py', '--subject', 'fixture', '--body-file', str(body),
                '--research-day', day, '--status-out', str(status), '--mail-to', 'fixture@example.invalid']
        if stage is not None:
            argv.extend(['--stage', stage])
        if preview:
            argv.extend(['--draft-only', '--draft-out', str(root / 'preview.eml')])
        return argv, status

    def smtp_environment(self, port='465'):
        return patch.dict(os.environ, {'SMTP_HOST': 'fixture.invalid', 'SMTP_PORT': port,
                                      'SMTP_USER': 'fixture', 'SMTP_PASSWORD': 'fixture'}, clear=True)

    def test_early_late_missing_stage_and_wrong_day_skip_before_body_or_smtp(self):
        cases = [
            ('evening', '2026-10-09', '2026-10-09T17:59:59+08:00', 'before_send_window'),
            ('evening', '2026-10-09', '2026-10-09T18:01:00+08:00', 'missed_window'),
            (None, '2026-10-09', '2026-10-09T18:00:00+08:00', 'invalid_stage'),
            ('wrong', '2026-10-09', '2026-10-09T18:00:00+08:00', 'invalid_stage'),
            ('morning', '2026-10-09', '2026-10-12T09:00:00+08:00', 'wrong_research_day'),
        ]
        for stage, day, clock, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                argv, status = self.setup_case(Path(directory), stage=stage, day=day, missing=True)
                with (patch('sys.argv', argv), patch('mail_gate.c.now', return_value=at(clock)),
                      patch('send_mail.smtplib.SMTP') as smtp, patch('send_mail.smtplib.SMTP_SSL') as secure):
                    self.assertEqual(send_mail.main(), 0)
                smtp.assert_not_called(); secure.assert_not_called()
                value = json.loads(status.read_text(encoding='utf-8'))
                self.assertEqual(value['state'], expected)
                self.assertFalse(value['smtp_accepted'])

    def test_closed_session_has_priority_over_missing_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            argv, status = self.setup_case(Path(directory), stage=None, missing=True)
            with patch('sys.argv', argv), patch('mail_gate.c.now', return_value=at('2026-10-10T18:00:00+08:00')):
                self.assertEqual(send_mail.main(), 0)
            self.assertEqual(json.loads(status.read_text())['state'], 'market_closed')

    def test_both_smtp_transports_recheck_after_login(self):
        for port, transport in [('465', 'SMTP_SSL'), ('587', 'SMTP')]:
            with self.subTest(port=port), tempfile.TemporaryDirectory() as directory:
                argv, status = self.setup_case(Path(directory))
                clock = [at('2026-10-09T18:00:59+08:00')]
                with (patch('sys.argv', argv), self.smtp_environment(port),
                      patch('mail_gate.c.now', side_effect=lambda: clock[0]),
                      patch('send_mail.smtplib.' + transport) as relay):
                    smtp = relay.return_value.__enter__.return_value
                    smtp.login.side_effect = lambda *_: clock.__setitem__(0, at('2026-10-09T18:01:00+08:00'))
                    self.assertEqual(send_mail.main(), 0)
                    smtp.login.assert_called_once(); smtp.send_message.assert_not_called()
                value = json.loads(status.read_text())
                self.assertEqual(value['state'], 'missed_window'); self.assertEqual(value['attempts'], 1)

    def test_login_crossing_into_closed_day_keeps_market_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            argv, status = self.setup_case(Path(directory))
            clock = [at('2026-10-09T18:00:00+08:00')]
            with (patch('sys.argv', argv), self.smtp_environment(),
                  patch('mail_gate.c.now', side_effect=lambda: clock[0]),
                  patch('send_mail.smtplib.SMTP_SSL') as relay):
                smtp = relay.return_value.__enter__.return_value
                smtp.login.side_effect = lambda *_: clock.__setitem__(0, at('2026-10-10T00:00:00+08:00'))
                self.assertEqual(send_mail.main(), 0); smtp.send_message.assert_not_called()
            self.assertEqual(json.loads(status.read_text())['state'], 'market_closed')

    def test_login_crossing_into_another_open_day_cannot_submit_old_report(self):
        with tempfile.TemporaryDirectory() as directory:
            argv, status = self.setup_case(Path(directory), day='2026-10-12')
            clock = [at('2026-10-12T18:00:00+08:00')]
            with (patch('sys.argv', argv), self.smtp_environment(),
                  patch('mail_gate.c.now', side_effect=lambda: clock[0]),
                  patch('send_mail.smtplib.SMTP_SSL') as relay):
                smtp = relay.return_value.__enter__.return_value
                smtp.login.side_effect = lambda *_: clock.__setitem__(0, at('2026-10-13T18:00:00+08:00'))
                self.assertEqual(send_mail.main(), 0); smtp.send_message.assert_not_called()
            self.assertEqual(json.loads(status.read_text())['state'], 'wrong_research_day')

    def test_smtp_failure_past_deadline_stops_retries_and_preserves_missed_status(self):
        with tempfile.TemporaryDirectory() as directory:
            argv, status = self.setup_case(Path(directory))
            clock = [at('2026-10-09T18:00:59+08:00')]
            with (patch('sys.argv', argv), self.smtp_environment(),
                  patch('mail_gate.c.now', side_effect=lambda: clock[0]),
                  patch('send_mail.smtplib.SMTP_SSL') as relay):
                smtp = relay.return_value.__enter__.return_value
                def failed_login(*_credentials):
                    clock[0] = at('2026-10-09T18:01:00+08:00')
                    raise OSError('fixture transport disconnected')
                smtp.login.side_effect = failed_login
                self.assertEqual(send_mail.main(), 0)
                relay.assert_called_once(); smtp.send_message.assert_not_called()
            value = json.loads(status.read_text())
            self.assertEqual(value['state'], 'missed_window'); self.assertFalse(value['smtp_accepted'])

    def test_next_retry_rechecks_clock_before_new_smtp_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            argv, status = self.setup_case(Path(directory))
            calls = [0]
            def clock():
                calls[0] += 1
                return at('2026-10-09T18:00:59+08:00' if calls[0] <= 6 else '2026-10-09T18:01:00+08:00')
            with (patch('sys.argv', argv), self.smtp_environment(), patch('mail_gate.c.now', side_effect=clock),
                  patch('send_mail.smtplib.SMTP_SSL') as relay):
                smtp = relay.return_value.__enter__.return_value
                smtp.login.side_effect = OSError('fixture disconnected')
                self.assertEqual(send_mail.main(), 0)
                relay.assert_called_once(); smtp.send_message.assert_not_called()
            self.assertEqual(json.loads(status.read_text())['state'], 'missed_window')

    def test_connection_lost_after_data_is_unknown_and_never_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            argv, status = self.setup_case(Path(directory))
            draft = Path(directory) / 'uncertain.eml'
            argv.extend(['--draft-out', str(draft)])
            with (patch('sys.argv', argv), self.smtp_environment(),
                  patch('mail_gate.c.now', return_value=at('2026-10-09T18:00:30+08:00')),
                  patch('send_mail.smtplib.SMTP_SSL') as relay):
                smtp = relay.return_value.__enter__.return_value
                smtp.send_message.side_effect = send_mail.smtplib.SMTPServerDisconnected('fixture lost DATA acknowledgement')
                self.assertEqual(send_mail.main(), 1)
                relay.assert_called_once(); smtp.send_message.assert_called_once()
            value = json.loads(status.read_text())
            self.assertEqual(value['state'], 'delivery_unknown')
            self.assertEqual(value['attempts'], 1)
            self.assertFalse(value['smtp_accepted']); self.assertTrue(draft.is_file())

    def test_valid_minute_allows_one_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            argv, status = self.setup_case(Path(directory))
            with (patch('sys.argv', argv), self.smtp_environment(),
                  patch('mail_gate.c.now', return_value=at('2026-10-09T18:00:00+08:00')),
                  patch('send_mail.smtplib.SMTP_SSL') as relay):
                smtp = relay.return_value.__enter__.return_value
                smtp.send_message.return_value = {}
                self.assertEqual(send_mail.main(), 0); smtp.send_message.assert_called_once()
            self.assertTrue(json.loads(status.read_text())['smtp_accepted'])

    def test_partial_recipient_refusal_is_recorded_without_addresses_or_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            argv, status = self.setup_case(Path(directory))
            argv[argv.index('--mail-to')+1] = 'accepted@example.invalid, refused@example.invalid'
            with (patch('sys.argv', argv), self.smtp_environment(),
                  patch('mail_gate.c.now', return_value=at('2026-10-09T18:00:00+08:00')),
                  patch('send_mail.smtplib.SMTP_SSL') as relay):
                smtp = relay.return_value.__enter__.return_value
                smtp.send_message.return_value = {'refused@example.invalid': (550, b'fixture rejected')}
                self.assertEqual(send_mail.main(), 1)
                relay.assert_called_once(); smtp.send_message.assert_called_once()
            raw = status.read_text(encoding='utf-8')
            value = json.loads(raw)
            self.assertEqual(value['state'], 'partial_acceptance')
            self.assertTrue(value['accepted_some']); self.assertFalse(value['smtp_accepted'])
            self.assertEqual(value['attempts'], 1)
            self.assertNotIn('@example.invalid', raw)

    def test_preview_ignores_send_time_and_missing_stage_but_closed_target_stays_closed(self):
        for day, expected in [('2026-10-09', 'draft_only'), ('2026-10-10', 'market_closed')]:
            with self.subTest(day=day), tempfile.TemporaryDirectory() as directory:
                argv, status = self.setup_case(Path(directory), stage=None, day=day, preview=True)
                with (patch('sys.argv', argv), patch('mail_gate.c.now', return_value=at('2026-10-17T12:00:00+08:00')),
                      patch('send_mail.smtplib.SMTP_SSL') as relay):
                    self.assertEqual(send_mail.main(), 0); relay.assert_not_called()
                self.assertEqual(json.loads(status.read_text())['state'], expected)


if __name__ == '__main__':
    unittest.main()
