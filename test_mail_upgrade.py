"""Email rendering and market-session controls. Fixtures are not market evidence."""
import datetime as dt
from email.parser import BytesParser
from email import policy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import collector as c
import mail_gate
import mail_layout
import mail_reports
import send_mail
import snapshot_mail


def payload(stage='evening'):
    return {'stage':stage,'target_date':'2026-10-09','base_trade_date':'2026-10-09',
        'generated_at':'2026-10-09T18:05:00+08:00','information_cutoff':'2026-10-09T18:00:00+08:00',
        'research':{'model_ready':True,'formal_trade_ready':False,'inventory':{'base_quotes':5500},
            'price_band_5_10':[{'symbol':'FIXTURE.SH','amount':100000000,'is_st':None}],
            'model_predictions':{'target_trade_date':'2026-10-12','prediction_count':1,
                'rows':[{'symbol':'FIXTURE.SH','name':'<script>unsafe & name</script>','close':6.12,'P1':.432,'P2':.123,'rank':1}]},
            'model_validation':{'split':{'test':{'independent_dates':67,'first_input_date':'2026-06-29','last_input_date':'2026-10-08'}},
                'endpoints':{'P1':{'n':100,'top10_event_frequency':.5,'brier':.25,'calibration_block_constant_baseline_brier':.24}}}}}


class GateTests(unittest.TestCase):
    def at(self,day,hour=10):
        return dt.datetime.fromisoformat(day+'T'+f'{hour:02d}'+':00:00+08:00')

    def test_weekend_and_exchange_holidays_skip(self):
        for day in ['2026-10-11','2026-10-03','2026-10-06','2026-02-18','2026-09-25']:
            self.assertFalse(mail_gate.check(day,at=self.at(day))['allowed'])

    def test_shanghai_date_governs_utc_crossing_midnight(self):
        # Sunday 16:01 UTC is Monday 00:01 Shanghai.
        result=mail_gate.check(at=dt.datetime(2026,10,11,16,1,tzinfo=dt.timezone.utc))
        self.assertTrue(result['allowed']);self.assertEqual(result['execution_date'],'2026-10-12')

    def test_delayed_friday_email_not_sent_on_sunday(self):
        result=mail_gate.check('2026-10-09',at=self.at('2026-10-11'))
        self.assertFalse(result['allowed']);self.assertEqual(result['reason'],'execution_session_closed')

    def test_makeup_workday_is_closed_and_monday_is_open(self):
        self.assertFalse(mail_gate.check('2026-10-10',at=self.at('2026-10-10'))['allowed'])
        self.assertTrue(mail_gate.check('2026-10-12',at=self.at('2026-10-12'))['allowed'])

    def test_unknown_calendar_fails_closed(self):
        self.assertEqual(mail_gate.check('2027-01-04',at=self.at('2027-01-04'))['state'],'calendar_unverified')

    def test_preview_allowed_but_closed_target_still_skipped(self):
        at=self.at('2026-10-11')
        self.assertTrue(mail_gate.check('2026-10-09',at=at,preview=True)['allowed'])
        self.assertFalse(mail_gate.check('2026-10-11',at=at,preview=True)['allowed'])

    def test_sender_skips_before_body_read_and_smtp(self):
        with tempfile.TemporaryDirectory() as d:
            status=Path(d)/'delivery.json'
            argv=['send_mail.py','--subject','fixture','--body-file',str(Path(d)/'missing.txt'),
                  '--research-day','2026-10-09','--status-out',str(status)]
            with (patch('sys.argv',argv),patch('mail_gate.c.now',return_value=self.at('2026-10-11')),
                  patch('send_mail.smtplib.SMTP') as smtp,patch('send_mail.smtplib.SMTP_SSL') as ssl):
                self.assertEqual(send_mail.main(),0);smtp.assert_not_called();ssl.assert_not_called()
            self.assertFalse(json.loads(status.read_text())['smtp_accepted'])

    def test_report_all_stages_skip_without_loading_database(self):
        for stage in ['evening','morning','auction']:
            with tempfile.TemporaryDirectory() as d:
                argv=['snapshot_mail.py','--stage',stage,'--day','2026-10-11','--store',d+'/missing','--output',d+'/out']
                with (patch('sys.argv',argv),patch('snapshot_mail.report') as report,
                      patch('snapshot_mail.subprocess.run') as sender):
                    self.assertEqual(snapshot_mail.main(),0);report.assert_not_called();sender.assert_not_called()
                self.assertFalse(list(Path(d,'out').glob('*.html')))

    def test_smtp_login_crossing_into_closed_day_cannot_send(self):
        with tempfile.TemporaryDirectory() as d:
            body=Path(d)/'body.txt';body.write_text('fixture')
            status=Path(d)/'delivery.json'
            clock=[self.at('2026-10-09',23)]
            argv=['send_mail.py','--subject','fixture','--body-file',str(body),
                  '--research-day','2026-10-09','--status-out',str(status),'--mail-to','fixture@example.invalid']
            env={'SMTP_HOST':'fixture.invalid','SMTP_PORT':'465','SMTP_USER':'fixture','SMTP_PASSWORD':'fixture'}
            with (patch('sys.argv',argv),patch.dict(os.environ,env,clear=True),
                  patch('mail_gate.c.now',side_effect=lambda:clock[0]),patch('send_mail.smtplib.SMTP_SSL') as relay):
                smtp=relay.return_value.__enter__.return_value
                smtp.login.side_effect=lambda *args:clock.__setitem__(0,self.at('2026-10-10',0))
                self.assertEqual(send_mail.main(),0);smtp.send_message.assert_not_called()
            self.assertEqual(json.loads(status.read_text())['state'],'market_closed')

    def test_legacy_cli_does_not_emit_closed_notice_or_reuse_stale_body(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for suffix in ('.md','.txt','.html','.json'):(root/('auction-2026-10-11'+suffix)).write_text('stale')
            argv=['mail_reports.py','--stage','auction','--day','2026-10-11','--db',d+'/missing','--output-dir',d]
            with patch('sys.argv',argv),patch('mail_reports.build') as build:
                self.assertEqual(mail_reports.main(),0);build.assert_not_called()
            self.assertFalse((root/'auction-2026-10-11.md').exists())
            self.assertTrue((root/'auction-2026-10-11.skip.json').exists())


class RenderingTests(unittest.TestCase):
    def test_html_escapes_untrusted_content_and_preserves_probability_semantics(self):
        value=payload(); html=mail_layout.render_html(value); text=mail_layout.render_text(value)
        self.assertIn('&lt;script&gt;',html);self.assertNotIn('<script>',html)
        for body in [html,text]:
            self.assertIn('43.2%',body);self.assertIn('12.3%',body);self.assertIn('净盈利胜率',body)
            self.assertIn('2026-10-12',body);self.assertIn('Brier',body)
        self.assertNotIn('挂单纪律',html)

    def test_missing_official_status_not_false_or_zero(self):
        value=payload();value['research'].pop('model_predictions');value['research']['model_ready']=False
        value['research']['price_band_5_10'][0].update(name='fixture',close=6)
        self.assertIn('未知',mail_layout.render_text(value))

    def test_preview_subject_not_a_late_live_report(self):
        value=payload();value.update(preview=True,late=True)
        self.assertIn('预览',mail_layout.content(value)['subject'])
        self.assertNotIn('迟到回顾',mail_layout.content(value)['subtitle'])

    def test_auction_observation_does_not_publish_proxy_orders_or_static_edge(self):
        value=payload('auction');value['action_card']=[{'symbol':'OLD','limit_price':99}]
        value['measured_edge']={'claimed_winrate':.85}
        text=mail_layout.render_text(value)
        self.assertIn('当前竞价输入未核验',text);self.assertNotIn('85%',text);self.assertNotIn('OLD',text)

    def test_multipart_roundtrip_has_readable_chinese_and_both_formats(self):
        value=payload();subject=mail_layout.content(value)['subject']
        message=send_mail.build_message(subject,mail_layout.render_text(value),'fixture@example.invalid',
                                       'bot@example.invalid',mail_layout.render_html(value))
        saved=BytesParser(policy=policy.default).parsebytes(message.as_bytes())
        self.assertEqual(saved.get_content_type(),'multipart/alternative')
        self.assertEqual(str(saved['Subject']),subject)
        self.assertIn('模型观察 Top10',saved.get_body(preferencelist=('html',)).get_content())
        self.assertIn('模型观察 Top10',saved.get_body(preferencelist=('plain',)).get_content())


if __name__=='__main__':unittest.main()
