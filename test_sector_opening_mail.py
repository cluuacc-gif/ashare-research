"""Report boundaries: future/stale evidence cannot become next-session advice."""
import datetime as dt
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import mail_layout
import snapshot_mail


def report_payload(stage='evening'):
    return {'stage':stage, 'target_date':'2026-10-16' if stage=='evening' else '2026-10-19',
            'base_trade_date':'2026-10-16', 'calendar':{'next_session':'2026-10-19'},
            'information_cutoff':'2026-10-18T18:00:00+08:00',
            'generated_at':'2026-10-18T18:00:01+08:00',
            'research':{'inventory':{'base_quotes':1}, 'formal_trade_ready':False}}


def sector():
    return {'schema':'sector-outlook-v1', 'base_date':'2026-10-16',
            'target_date':'2026-10-19', 'information_cutoff':'2026-10-18T10:00:00+00:00',
            'status':'ready', 'rankings':[{'rank':1, 'sector':'FIXTURE<sector>',
                'score':82.4, 'member_count':30, 'coverage':.9,
                'drivers':['relative momentum', 'breadth'], 'risks':['concentration']}],
            'validation':{'status':'forward_tracking_only'}}


def opening():
    return {'schema':'opening-strength-research-v1', 'base_date':'2026-10-16',
            'target_date':'2026-10-19', 'information_cutoff':'2026-10-18T18:00:00+08:00',
            'status':'conditional_research_only', 'live_signal':False,
            'formal_trade_eligible':False,
            'watchlist':[{'symbol':'FIXTURE.SH', 'name':'FIXTURE<stock>', 'close':6.,
                'sector':'FIXTURE<sector>', 'research_score':99,
                'entry_conditions':['fresh open confirmation', 'within frozen buy cap'],
                'cancel_conditions':['locked limit-up', 'unverified status']}],
            'opening_confirmation':{'verified':False}}


class RenderingBoundaries(unittest.TestCase):
    def test_evening_reports_relative_sector_forecast_and_forward_limit(self):
        value=report_payload(); value['research']['sector_outlook']=sector()
        content=mail_layout.content(value)
        section=next(s for s in content['sections'] if s['title']=='次日板块强弱展望')
        self.assertEqual(len(section['rows']),1)
        self.assertEqual(section['rows'][0][2],'82.4')
        self.assertIn('不是上涨概率或交易胜率',section['note'])
        self.assertIn('下一交易日开始跟踪',section['note'])
        self.assertIn('2026-10-19',section['note'])
        self.assertNotIn('82.4%',mail_layout.render_text(value))
        html=mail_layout.render_html(value)
        self.assertIn('FIXTURE&lt;sector&gt;',html)
        self.assertNotIn('FIXTURE<sector>',html)

    def test_stale_session_or_future_evidence_is_not_shown(self):
        cases=[('base_date','2026-10-15'),('target_date','2026-10-20'),
               ('information_cutoff','2026-10-18T18:00:01+08:00'),
               ('information_cutoff','2026-10-18T18:00:00'),('schema','unknown')]
        for key,item in cases:
            with self.subTest(key=key,item=item):
                value=report_payload(); forecast=sector(); forecast[key]=item
                value['research']['sector_outlook']=forecast
                text=mail_layout.render_text(value)
                self.assertNotIn('FIXTURE<sector>',text)
                self.assertIn('暂不列出次日强势排名',text)

    def test_missing_forecast_keeps_regular_report_available(self):
        value=report_payload()
        self.assertIn('次日板块强弱展望',mail_layout.render_text(value))
        self.assertIn('隔日毛收益策略',mail_layout.render_text(value))
        self.assertIn('暂不列出',mail_layout.render_html(value))

    def test_opening_watch_pool_is_not_an_execution_or_probability_claim(self):
        for stage in ['morning','auction']:
            value=report_payload(stage); value['research']['opening_strength']=opening()
            value['research']['sector_outlook']=sector()
            text=mail_layout.render_text(value)
            self.assertIn('FIXTURE.SH',text)
            self.assertIn('开盘转强条件观察',text)
            self.assertIn('不生成可执行买入指令',text)
            self.assertIn('locked limit-up',text)
            self.assertNotIn('99%',text)
            self.assertNotIn('次日板块强弱展望',text)
            self.assertIn('FIXTURE&lt;stock&gt;',mail_layout.render_html(value))

    def test_future_opening_input_hides_entire_watch_pool(self):
        value=report_payload('auction'); forecast=opening()
        forecast['information_cutoff']='2026-10-19T09:31:00+08:00'
        value['information_cutoff']='2026-10-19T09:25:00+08:00'
        value['research']['opening_strength']=forecast
        self.assertNotIn('FIXTURE.SH',mail_layout.render_text(value))


class SnapshotIntegration(unittest.TestCase):
    def test_real_modules_generate_matching_sector_and_opening_payloads(self):
        # Artificial price histories verify integration only, never forecast quality.
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'frozen.sqlite'
            with closing(sqlite3.connect(path)) as db:
                db.execute('PRAGMA application_id=1095977041')
                db.executescript('''
                    CREATE TABLE security_master(symbol TEXT PRIMARY KEY,name TEXT);
                    CREATE TABLE industry_members(symbol TEXT,classification TEXT,industry TEXT,
                        effective_date TEXT,observed_at TEXT,source_url TEXT);
                    CREATE TABLE daily_quotes(symbol TEXT,trade_date TEXT,open REAL,high REAL,
                        low REAL,close REAL,volume REAL,amount REAL,conflict INTEGER,
                        fetched_at TEXT,source_updated_at TEXT,source TEXT,finality TEXT,
                        PRIMARY KEY(symbol,trade_date));
                ''')
                start=dt.date(2026,10,16)-dt.timedelta(days=269)
                for index in range(15):
                    symbol=f'{index+1:06}.SZ'
                    db.execute('INSERT INTO security_master VALUES(?,?)',(symbol,f'研究样本{index+1}'))
                    db.execute('INSERT INTO industry_members VALUES(?,?,?,?,?,?)',
                        (symbol,'sina_industry',f'人工板块{index//5}','2026-10-17',
                         '2026-10-17T12:00:00+08:00','https://example.invalid/fixture'))
                    for day in range(270):
                        close=6+index*.1+day*.001
                        db.execute('INSERT INTO daily_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (symbol,(start+dt.timedelta(days=day)).isoformat(),close-.02,
                             close+.05,close-.15,close,5e6,30e6+index*1e6,0,
                             '2026-10-17T12:00:00+08:00',None,'synthetic_fixture','historical_provider'))
                db.commit()
            value=report_payload('morning')
            value['information_cutoff']='2026-10-18T14:00:00+08:00'
            snapshot_mail.attach_market_outlook(value,path,'2026-10-16','2026-10-19','fixture-hash')
            self.assertEqual(value['research']['sector_outlook']['status'],'ready')
            self.assertLessEqual(len(value['research']['opening_strength']['watchlist']),5)
            self.assertTrue(value['research']['opening_strength']['sector_overlay']['available'])
            self.assertEqual(value['research']['opening_strength']['input_sha256'],'fixture-hash')
            self.assertIn('开盘转强条件观察',mail_layout.render_text(value))
            self.assertFalse(value['research']['sector_stock_selection']['formal_trade_eligible'])
            self.assertFalse(value['research']['formal_trade_ready'])

    def test_generators_share_readonly_database_and_exact_report_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'frozen.sqlite'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE fixture(value TEXT)')
                db.execute("INSERT INTO fixture VALUES('frozen')")
                db.commit()
            calls=[]
            def generate_sector(db,base,target,cutoff):
                self.assertEqual(db.execute('SELECT value FROM fixture').fetchone()[0],'frozen')
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("INSERT INTO fixture VALUES('forbidden')")
                calls.append(('sector',base,target,cutoff))
                result=sector(); result['information_cutoff']=cutoff
                return result
            def generate_opening(db,base,target,cutoff,sector_outlook,auction_snapshot,include_pool,price_min,price_max):
                self.assertEqual(sector_outlook['rankings'][0]['score'],82.4)
                self.assertIsNone(auction_snapshot)
                calls.append(('opening',base,target,cutoff))
                result=opening(); result['information_cutoff']=cutoff
                return result
            modules={'sector_forecast':types.SimpleNamespace(generate=generate_sector),
                     'opening_strength':types.SimpleNamespace(generate=generate_opening)}
            value=report_payload(); value['snapshot_release']='frozen-fixture'
            with patch.dict(sys.modules,modules):
                snapshot_mail.attach_market_outlook(value,path,'2026-10-16','2026-10-19','fixture-hash')
            self.assertEqual([item[0] for item in calls],['sector','opening'])
            self.assertEqual({item[3] for item in calls},{value['information_cutoff']})
            self.assertFalse(value['research']['formal_trade_ready'])
            self.assertFalse(value['research']['opening_strength']['live_signal'])
            self.assertEqual(value['research']['sector_outlook']['input_sha256'],'fixture-hash')

    def test_optional_source_failure_stays_visible_and_cannot_enable_trading(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'frozen.sqlite'
            with closing(sqlite3.connect(path)): pass
            def unavailable(*args,**kwargs): raise ValueError('fixture evidence missing')
            modules={'sector_forecast':types.SimpleNamespace(generate=unavailable),
                     'opening_strength':types.SimpleNamespace(generate=unavailable)}
            value=report_payload()
            with patch.dict(sys.modules,modules):
                snapshot_mail.attach_market_outlook(value,path,'2026-10-16','2026-10-19')
            self.assertEqual(value['research']['sector_outlook']['status'],'unavailable')
            self.assertFalse(value['research']['opening_strength']['formal_trade_eligible'])
            self.assertIn('暂不列出次日强势排名',mail_layout.render_text(value))

    def test_evening_allows_timely_context_while_prices_stay_frozen_and_morning_never_uses_future_time(self):
        cases=[('evening','2026-10-16','2026-10-16T17:00:00+08:00','2026-10-16T17:00:00+08:00'),
               ('morning','2026-10-19','2026-10-19T08:55:00+08:00','2026-10-19T08:55:00+08:00'),
               ('auction','2026-10-19','2026-10-19T09:35:00+08:00','2026-10-19T09:25:00+08:00')]
        for stage,day,now,expected in cases:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                store=Path(directory)
                saved={'preparation_ready':True,'base_trade_date':'2026-10-16',
                       'database':{'name':'fixture.sqlite','sha256':'fixture-hash'},
                       'handoff':{'name':'handoff.json'},'frozen':{'name':'frozen.json'},
                       'base_quotes':1,'release_tag':'fixture-release',
                       'generated_at':'2026-10-16T16:45:00+08:00'}
                (store/'restore.json').write_text(json.dumps({'manifest':saved}),encoding='utf-8')
                with closing(sqlite3.connect(store/'fixture.sqlite')): pass
                value=report_payload(stage)
                value.update(disclaimer='无正式概率，fixture', information_cutoff=day+'T09:00:00+08:00')
                value['research']['inventory']={}
                with (patch('mail_reports.build',return_value=value),
                      patch('gross_swing_store.attach'),
                      patch('snapshot_mail.frozen_observations',return_value=([],{})),
                      patch('snapshot_mail.news_before',return_value=[]),
                      patch('snapshot_mail.c.now',return_value=dt.datetime.fromisoformat(now)),
                      patch('research_context.published_context',return_value={}),
                      patch('snapshot_mail.attach_market_outlook') as attach):
                    result=snapshot_mail.report(stage,day,store)
                self.assertEqual(result['information_cutoff'],expected)
                self.assertEqual(attach.call_args.args[2:4],('2026-10-16','2026-10-19'))
                self.assertEqual(attach.call_args.kwargs['input_sha256'],'fixture-hash')


if __name__=='__main__': unittest.main()
