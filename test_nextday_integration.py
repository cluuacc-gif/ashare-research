"""Publication identity, bounded mail reads and automatic prospective capture."""
import base64
import copy
import datetime as dt
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import market_outlook as m
import research_context as context
from market_outlook_report import render
from nextday_forward import initialize
from test_market_outlook import fixture
from test_nextday_forward import cohort


def study():
    return initialize(cohort(), '2026-10-16', '2026-10-19', 'a'*64, '2026-10-18T11:00:00+08:00')


class NextdayIntegrationTests(unittest.TestCase):
    def test_small_synthetic_sealed_snapshot_runs_real_generation_and_compaction(self):
        from test_opening_strength import market
        db=market()
        try:
            db.execute("ALTER TABLE daily_quotes ADD COLUMN source TEXT DEFAULT 'synthetic_fixture'")
            db.commit()
            with tempfile.TemporaryDirectory() as root:
                path=Path(root)/'market.sqlite3'
                destination=sqlite3.connect(path)
                db.backup(destination);destination.close()
                _,source=fixture()
                source.update(preparation_ready=True,database=m.state.ref(path))
                m.state.write(Path(root)/'restore.json',{'verified':True,'manifest':source})
                value,_=m.generate(root,cutoff='2026-10-18T14:00:00+08:00')
                self.assertEqual(value['nextday_research']['schema'],study()['schema'])
                self.assertGreater(len(value['nextday_research']['current_cohort']['base_candidates']),0)
                self.assertEqual(render(value),render(m.compact_pointer({'outlook':value})['outlook']))
                self.assertFalse(value['nextday_research']['formal_trade_ready'])
                self.assertTrue(value['active_trade_system']['system_enabled'])
                # The real initial publication reads a verified legacy outlook
                # that has research ledgers but no independent-system payload.
                legacy=copy.deepcopy(value);legacy.pop('active_trade_system')
                initial,_=m.generate(root,cutoff='2026-10-18T14:00:30+08:00',active_context={
                    'prior_value':legacy,'prior_publication_verified':True,'evidence':{}})
                first=initial['active_trade_system']
                self.assertTrue(first['system_enabled'])
                self.assertEqual(first['reference_book']['frozen_plan_dates'],[])
                self.assertEqual(first['reference_book']['records'],[])
                self.assertEqual(first['reference_account']['initial_capital_cny'],30000)
                self.assertNotIn('current_plan_preserved_from_first_publication',first)
                broken=copy.deepcopy(legacy);broken['active_trade_system']=[]
                with self.assertRaisesRegex(ValueError,'must be an object or absent'):
                    m.generate(root,cutoff='2026-10-18T14:00:40+08:00',active_context={
                        'prior_value':broken,'prior_publication_verified':True,'evidence':{}})
                prior={'prior_value':value,'prior_publication_verified':False,'evidence':{}}
                with self.assertRaisesRegex(ValueError,'publication is unverified'):
                    m.generate(root,cutoff='2026-10-18T14:01:00+08:00',active_context=prior)
                prior['prior_publication_verified']=True
                rerun,_=m.generate(root,cutoff='2026-10-18T14:01:00+08:00',active_context=prior)
                frozen=rerun['active_trade_system']
                self.assertTrue(frozen['current_plan_preserved_from_first_publication'])
                self.assertEqual(frozen['plans'],value['active_trade_system']['plans'])
                self.assertEqual(frozen['first_published_plan_generated_at'],value['active_trade_system']['generated_at'])
        finally:db.close()

    def test_shared_buy_cap_preserves_exact_cent_arithmetic(self):
        from opening_strength import frozen_cap
        from nextday_cohort import _bounds
        self.assertEqual(frozen_cap(6.,100),6.15)
        self.assertEqual(frozen_cap(5.6,100),5.74)
        for cents in range(500,10001):
            close=cents/100
            self.assertEqual(frozen_cap(close,100),_bounds(close)[1])

    def test_identity_and_trade_claims_verified(self):
        value, source = fixture(); value['nextday_research'] = study()
        self.assertIs(m.validate(value, source), value)
        for field, bad in [('schema', 'unknown'), ('protocol_sha256', 'x'*64),
                ('generated_at', '2026-10-19T09:00:00+08:00'),
                ('target_trade_date', '2026-10-20'), ('input_database_sha256', 'b'*64),
                ('formal_trade_ready', True), ('actual_win_rate', .8),
                ('execution_win_rate_80_validated', True)]:
            changed=copy.deepcopy(value);changed['nextday_research'][field]=bad
            with self.subTest(field=field),self.assertRaises(ValueError):m.validate(changed,source)

    def test_large_ledger_archived_with_identical_rendering_and_bounded_pointer(self):
        value,_=fixture();value['nextday_research']=study()
        value['nextday_research']['records']=[{'large':'x'*900000}]
        pointer=m.compact_pointer({'outlook':value})
        self.assertLess(len(json.dumps(pointer)),35000)
        self.assertNotIn('records',pointer['outlook']['nextday_research'])
        self.assertTrue(pointer['outlook']['forward_ledgers_archived'])
        self.assertEqual(render(value),render(pointer['outlook']))

    def test_mail_context_reuses_single_bounded_read_and_rejects_future(self):
        value,source=fixture();value['nextday_research']=study()
        manifest=m.compact_pointer({'outlook':value,'snapshot_release':source['release_tag'],
            'input_database_sha256':'a'*64,'base_trade_date':source['base_trade_date'],
            'assets_readback_verified_at':'verified','release_tag':'test-release'})
        envelope=json.dumps({'content':base64.b64encode(json.dumps(manifest).encode()).decode()})
        snapshot={'base_trade_date':'2026-10-16','release_tag':source['release_tag'],'database':source['database']}
        with patch.object(context.subprocess,'run',return_value=subprocess.CompletedProcess([],0,envelope,'')) as read:
            current=context.published_context(snapshot,'2026-10-18T12:00:00+08:00')
            self.assertEqual(current['nextday_research']['schema'],study()['schema'])
            read.assert_called_once();self.assertEqual(read.call_args.kwargs['timeout'],10)
            early=context.published_context(snapshot,'2026-10-18T10:00:00+08:00')
            self.assertEqual(early['nextday_research']['status'],'unavailable')

    def test_minute_capture_only_for_previously_published_due_cohorts(self):
        priorstudy=study()
        prior={'release_tag':'old-release','forward_eligible':True,
            'outlook':{'nextday_research':priorstudy}}
        _,source=fixture()
        with tempfile.TemporaryDirectory() as root,patch.object(m,'read_pointer',return_value=(prior,'blob')), \
                patch.object(m,'verify_pointer_publication',return_value={'verified_before_target_deadline':True}):
            with patch('nextday_minutes.capture',return_value={}) as capture:
                m.prepare_nextday_minutes('fixture/repo',source,root)
                self.assertEqual(capture.call_args.args[0],[])
                source['base_trade_date']='2026-10-19'
                m.prepare_nextday_minutes('fixture/repo',source,root)
                self.assertEqual(capture.call_args.args[0],['000001.SZ'])
                prior['forward_eligible']=False
                m.prepare_nextday_minutes('fixture/repo',source,root)
                self.assertEqual(capture.call_args.args[0],[])

    def test_publication_commit_after_deadline_cannot_admit_a_forward_cohort(self):
        prior={'target_trade_date':'2026-10-19','assets_readback_verified_at':'verified'}
        for stamp,expected in [('2026-10-19T01:14:59Z',True),('2026-10-19T01:15:00Z',False)]:
            envelope=json.dumps([{'sha':'a'*40,'commit':{'committer':{'date':stamp}}}])
            with self.subTest(stamp=stamp),patch.object(m,'read_pointer',return_value=(prior,'blob')), \
                    patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],0,envelope,'')) as read:
                self.assertEqual(m.verify_pointer_publication('fixture/repo',prior) is not None,expected)
                self.assertEqual(read.call_args.kwargs['timeout'],10)

    def test_late_publication_retains_previous_pointer_without_creating_release(self):
        value,source=fixture()
        with tempfile.TemporaryDirectory() as root:
            m.state.write(Path(root)/'outlook.json',value)
            m.state.write(Path(root)/'source_manifest.json',source)
            with patch.object(m.state,'pointer',return_value=(source,'source-blob')), \
                    patch.object(m,'read_pointer',return_value=(None,None)), \
                    patch.object(m.c,'now',return_value=dt.datetime.fromisoformat('2026-10-19T09:15:00+08:00')), \
                    patch.object(m.state,'run') as mutation:
                with self.assertRaisesRegex(ValueError,'window has closed'):
                    m.publish('fixture/repo',root,'test-run')
                mutation.assert_not_called()

    def test_new_study_adds_mail_summary_without_network_capture_at_send_time(self):
        import mail_layout
        from snapshot_mail import attach_market_outlook
        payload={'information_cutoff':'2026-10-18T12:00:00+08:00','stage':'morning',
            'base_trade_date':'2026-10-16','target_date':'2026-10-19'}
        with patch('snapshot_mail.sqlite3.connect',side_effect=sqlite3.DatabaseError('synthetic unavailable DB')), \
                patch('nextday_minutes.capture') as capture:
            attach_market_outlook(payload,'fixture.sqlite3','2026-10-16','2026-10-19',
                context_data={'nextday_research':study()})
            capture.assert_not_called()
        html=mail_layout.render_html(payload)
        self.assertIn('隔日盈利前向研究',html)
        self.assertIn('不是买入名单',html)
        self.assertIn('主动规则系统已启用',html)


if __name__=='__main__':unittest.main()
