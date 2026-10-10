"""Synthetic control-flow fixtures only; never market/model acceptance."""
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import collector as c
import pipeline_stage
import pipeline_state
import send_mail
import snapshot_mail


class RoutingTest(unittest.TestCase):
    def test_closed_day_skips_all_later_stages(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d,'run_status.json').write_text(json.dumps({'status':'SKIPPED_MARKET_CLOSED','date':'2026-10-10'}))
            self.assertFalse(pipeline_stage.route(d)['has_quotes'])

    def test_zero_quote_success_label_cannot_start_preparation(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d,'run_status.json').write_text(json.dumps({'status':'REAL_DATA_PARTIAL','date':'2026-10-09','actual_new_quotes':0}))
            Path(d,'source_acceptance.json').write_text(json.dumps({'daily_quote_count':0}))
            with self.assertRaises(ValueError): pipeline_stage.route(d)


class QualityTest(unittest.TestCase):
    def make_db(self,path,dates):
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE security_master(symbol TEXT PRIMARY KEY)')
            db.execute('CREATE TABLE daily_quotes(symbol TEXT,trade_date TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL)')
            db.executemany('INSERT INTO security_master VALUES (?)',[(str(i),) for i in range(5000)])
            for day in dates:
                db.executemany('INSERT INTO daily_quotes VALUES (?,?,6,6.2,5.8,6.1,100)',[(str(i),day) for i in range(5000)])

    def test_holiday_is_not_an_incremental_gap(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'fixture.sqlite3';self.make_db(path,['2026-09-24','2026-09-28'])
            result=pipeline_stage.check_database(path,'2026-09-28','2026-09-24',5000)
            self.assertTrue(result['engineering_ready'])
            self.assertFalse(result['formal_data_ready'])

    def test_missing_session_fails_even_when_latest_has_full_coverage(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'fixture.sqlite3';self.make_db(path,['2026-09-24','2026-09-28','2026-09-30'])
            with self.assertRaisesRegex(ValueError,'missing_incremental_sessions'):
                pipeline_stage.check_database(path,'2026-09-30','2026-09-24',5000)

    def test_stale_date_and_historical_loss_fail(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'fixture.sqlite3';self.make_db(path,['2026-09-24'])
            with self.assertRaisesRegex(ValueError,'latest_date_mismatch'):
                pipeline_stage.check_database(path,'2026-09-28','2026-09-24',5000)
            with self.assertRaisesRegex(ValueError,'historical_row_loss'):
                pipeline_stage.check_database(path,'2026-09-24','2026-09-24',5001)


class StoreTest(unittest.TestCase):
    def test_service_failure_not_seed_fallback(self):
        with patch('pipeline_state.run',side_effect=subprocess.CalledProcessError(1,'gh',stderr='HTTP 403')):
            with self.assertRaises(subprocess.CalledProcessError): pipeline_state.pointer('fixture/repo')

    def test_corrupt_saved_asset_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'fixture';p.write_bytes(b'truncated')
            with self.assertRaises(ValueError): pipeline_state.verify_file(p,{'name':'fixture','bytes':10,'sha256':'0'*64})

    def test_concurrent_pointer_change_keeps_old_pointer(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);restored=root/'restored';out=root/'out';restored.mkdir();out.mkdir();(out/'frozen').mkdir()
            database=out/'fixture.sqlite3'
            with sqlite3.connect(database) as db:
                db.executescript(c.SCHEMA);db.execute(f'PRAGMA application_id={c.APP_ID}')
            sealed=pipeline_state.inspect_sealed(database)
            pipeline_state.write(restored/'restore.json',{'pointer_blob_sha':'old','manifest':{'handoff':{'name':'handoff.json'}}})
            pipeline_state.write(restored/'handoff.json',{'latest_successful_dataset':None})
            pipeline_state.write(out/'frozen/quality.json',{'base_trade_date':'2026-10-09'})
            (out/'frozen_inputs.zip').write_bytes(b'fixture-only')
            pipeline_state.write(out/'preparation.json',{'base_trade_date':'2026-10-09','next_trade_date':'2026-10-12',
                'generated_at':c.stamp(),'working_database':{**sealed,'path':str(database)},'daily_rows':0,'base_quotes':0,
                'engineering_quality':{'engineering_ready':True}})
            calls=[]
            def fake_run(*args):
                calls.append(args)
                if args[:3]==('gh','release','download'):
                    dest=Path(args[args.index('--dir')+1])
                    for p in (out/'published').iterdir(): shutil.copy2(p,dest/p.name)
                return ''
            with patch('pipeline_state.run',side_effect=fake_run),patch('pipeline_state.pointer',return_value=({},'changed')):
                with self.assertRaisesRegex(ValueError,'changed concurrently'):
                    pipeline_state.publish('fixture/repo',restored,out,'fixture')
            self.assertFalse(any('--method' in args for args in calls))
            self.assertTrue((out/'published/manifest.json').is_file())


class MailTest(unittest.TestCase):
    def test_explicit_draft_is_not_delivery(self):
        with tempfile.TemporaryDirectory() as d:
            body=Path(d)/'body.md';body.write_text('fixture')
            status=Path(d)/'status.json'
            argv=['send_mail.py','--subject','fixture','--body-file',str(body),
                  '--draft-out',str(Path(d)/'draft.eml'),'--status-out',str(status),'--draft-only']
            with patch('sys.argv',argv),patch.dict(os.environ,{},clear=True): self.assertEqual(send_mail.main(),0)
            self.assertFalse(json.loads(status.read_text())['smtp_accepted'])

    def test_missing_smtp_fails_even_if_draft_saved(self):
        with tempfile.TemporaryDirectory() as d:
            body=Path(d)/'body.md';body.write_text('fixture')
            argv=['send_mail.py','--subject','fixture','--body-file',str(body),'--draft-out',str(Path(d)/'draft.eml')]
            with patch('sys.argv',argv),patch.dict(os.environ,{},clear=True): self.assertEqual(send_mail.main(),2)

    def test_published_target_date_not_wall_clock_controls_report(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            manifest={'preparation_ready':True,'base_trade_date':'2026-10-09','base_quotes':5559,
                'release_tag':'fixture','generated_at':'2026-10-10T00:57:00+08:00',
                'database':{'name':'fixture.sqlite3'},'handoff':{'name':'handoff.json'},'frozen':{'name':'frozen.zip'}}
            (root/'restore.json').write_text(json.dumps({'manifest':manifest}))
            payload={'research':{'inventory':{}},'disclaimer':'fixture'}
            at=dt.datetime(2026,10,10,1,tzinfo=c.TZ)
            with patch('mail_reports.build',return_value=payload),patch('snapshot_mail.frozen_observations',return_value=([],{})),patch('snapshot_mail.c.now',return_value=at):
                result=snapshot_mail.report('evening','2026-10-09',root)
            self.assertTrue(result['late'])
            self.assertEqual(result['base_trade_date'],'2026-10-09')
            with self.assertRaisesRegex(ValueError,'required report base'):
                snapshot_mail.report('morning','2026-10-09',root)


if __name__=='__main__': unittest.main()
