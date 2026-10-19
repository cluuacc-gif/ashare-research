"""Enabled decision plans stay distinct from actual or validated trading."""
import base64
import copy
import datetime as dt
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import active_trade_system as active
import market_outlook as outlook
import research_context as context
from active_trade_report import compact, render
from test_active_trade_system import fixtures, BASE, TARGET, CUTOFF
from test_market_outlook import fixture


def study():
    db, sectors, opening, flows = fixtures()
    try:
        with patch('nextday_cohort._now', return_value=dt.datetime.fromisoformat('2026-10-20T15:00:00+08:00')):
            return active.build(db,BASE,TARGET,CUTOFF,sectors,opening,flows,input_sha256='a'*64)
    finally:db.close()


class ActiveIntegrationTests(unittest.TestCase):
    def test_validation_accepts_enabled_rule_but_rejects_actual_or_validated_claims(self):
        value,source=fixture();value['information_cutoff']=CUTOFF
        value['generated_at']='2026-10-18T14:00:01+08:00'
        for key in ('sector_outlook','opening_strength_plan'):value[key]['information_cutoff']=CUTOFF
        value['active_trade_system']=study()
        self.assertIs(outlook.validate(value,source),value)
        for key,bad in [('system_enabled',False),('mode','broker_execution'),
                ('order_execution_enabled',True),('empirical_validation_passed',True),
                ('formal_trade_ready',True),('actual_win_rate',.99),
                ('execution_win_rate_80_validated',True),('protocol_sha256','b'*64),
                ('input_database_sha256','b'*64),('target_trade_date','2026-10-20'),
                ('generated_at','2026-10-18T14:01:00+08:00')]:
            badvalue=copy.deepcopy(value);badvalue['active_trade_system'][key]=bad
            with self.subTest(key=key),self.assertRaises(ValueError):outlook.validate(badvalue,source)

    def test_full_book_is_archived_but_report_and_conditional_plans_survive(self):
        value,_=fixture();value['active_trade_system']=study()
        value['active_trade_system']['reference_book']={'records':[{'raw':'x'*1000000}]}
        pointer=outlook.compact_pointer({'outlook':value})
        current=pointer['outlook']['active_trade_system']
        self.assertLess(len(json.dumps(pointer)),35000)
        self.assertNotIn('reference_book',current)
        self.assertTrue(pointer['outlook']['forward_ledgers_archived'])
        self.assertEqual(render(current),render(value['active_trade_system']))
        self.assertTrue(current['plans'])

    def test_mail_reads_matching_enabled_plans_once_without_collecting(self):
        value,source=fixture();value['active_trade_system']=study()
        value['active_trade_system']['generated_at']='2026-10-18T11:00:00+08:00'
        manifest=outlook.compact_pointer({'outlook':value,'snapshot_release':source['release_tag'],
            'input_database_sha256':'a'*64,'base_trade_date':BASE,
            'assets_readback_verified_at':'verified','release_tag':'active-fixture'})
        envelope=json.dumps({'content':base64.b64encode(json.dumps(manifest).encode()).decode()})
        snapshot={'base_trade_date':BASE,'release_tag':source['release_tag'],'database':source['database']}
        with patch.object(context.subprocess,'run',return_value=subprocess.CompletedProcess([],0,envelope,'')) as read, \
                patch('nextday_minutes.capture') as capture:
            current=context.published_context(snapshot,'2026-10-18T12:00:00+08:00')
            self.assertTrue(current['active_trade_system']['plans'])
            read.assert_called_once();capture.assert_not_called()
            tooearly=context.published_context(snapshot,'2026-10-18T10:00:00+08:00')
            self.assertTrue(tooearly['active_trade_system']['system_enabled'])
            self.assertFalse(tooearly['active_trade_system']['plans'])

    def test_unavailable_context_keeps_system_enabled_without_old_plan(self):
        snapshot={'base_trade_date':BASE,'release_tag':'not-matching','database':{'sha256':'a'*64}}
        with patch.object(context.subprocess,'run',side_effect=subprocess.TimeoutExpired('gh',10)):
            current=context.published_context(snapshot,CUTOFF)
        safe=current['active_trade_system']
        self.assertTrue(safe['system_enabled']);self.assertEqual(safe['plans'],[])
        self.assertFalse(safe['order_execution_enabled'])

    def test_capture_includes_active_due_names_only_after_verified_publication(self):
        current=study()
        prior={'release_tag':'active-first','forward_eligible':True,
            'outlook':{'active_trade_system':current}}
        _,source=fixture();source['base_trade_date']=TARGET
        with tempfile.TemporaryDirectory() as root,patch.object(outlook,'read_pointer',return_value=(prior,'blob')), \
                patch.object(outlook,'verify_pointer_publication',return_value={'verified_before_target_deadline':True}), \
                patch('nextday_minutes.capture',return_value={}) as capture:
            result=outlook.prepare_nextday_minutes('fixture/repo',source,root)
            self.assertEqual(capture.call_args.args[0],sorted(row['symbol'] for row in current['plans']))
            self.assertTrue(result['prior_publication_verified'])
            prior['forward_eligible']=False
            outlook.prepare_nextday_minutes('fixture/repo',source,root)
            self.assertEqual(capture.call_args.args[0],[])


if __name__=='__main__':unittest.main()
