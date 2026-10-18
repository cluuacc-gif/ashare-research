import base64
import copy
import json
import subprocess
import unittest
from unittest.mock import patch
import market_outlook as m
import research_context as context
from market_outlook_report import render
from test_market_outlook import fixture


def study():
    return {'schema':'pa-context-trade-research-v1','generated_at':'2026-10-18T11:00:00+08:00',
        'base_trade_date':'2026-10-16','target_trade_date':'2026-10-19','input_database_sha256':'a'*64,
        'formal_trade_ready':False,'actual_win_rate':None,'execution_win_rate_80_validated':False,
        'policy':{'active':False},'coverage':{},'selection':[],'diagnostic':{}}


class PAIntegrationTests(unittest.TestCase):
    def test_verified_identity_and_cutoff(self):
        value,source=fixture();value['pa_trade_research']=study()
        self.assertIs(m.validate(value,source),value)

    def test_future_cross_snapshot_and_false_win_claims_rejected(self):
        for key,change in [('generated_at','2026-10-19T09:00:00+08:00'),
                           ('target_trade_date','2026-10-20'),('input_database_sha256','b'*64),
                           ('formal_trade_ready',True),('actual_win_rate',.82),('execution_win_rate_80_validated',True)]:
            value,source=fixture();value['pa_trade_research']=study();value['pa_trade_research'][key]=change
            with self.subTest(key=key),self.assertRaises(ValueError):m.validate(value,source)

    def test_archived_protocol_does_not_change_rendered_report(self):
        value,_=fixture();value['pa_trade_research']=study();value['pa_trade_research']['protocol']={'large':'x'*900000}
        pointer=m.compact_pointer({'outlook':value})
        self.assertLess(len(json.dumps(pointer)),20000)
        self.assertEqual(render(value),render(pointer['outlook']))
        self.assertFalse(pointer['outlook']['pa_trade_research']['execution_win_rate_80_validated'])

    def test_bounded_published_context_accepts_compact_pa_and_rejects_future(self):
        value,source=fixture();value['pa_trade_research']=study()
        manifest=m.compact_pointer({'outlook':value,'snapshot_release':source['release_tag'],
            'input_database_sha256':'a'*64,'base_trade_date':source['base_trade_date'],'assets_readback_verified_at':'verified'})
        envelope=json.dumps({'content':base64.b64encode(json.dumps(manifest).encode()).decode()})
        snapshot={'base_trade_date':'2026-10-16','release_tag':source['release_tag'],'database':source['database']}
        with patch.object(context.subprocess,'run',return_value=subprocess.CompletedProcess([],0,envelope,'')) as read:
            current=context.published_context(snapshot,'2026-10-18T12:00:00+08:00')
            self.assertEqual(current['pa_trade_research']['schema'],study()['schema'])
            self.assertEqual(read.call_args.kwargs['timeout'],10)
            early=context.published_context(snapshot,'2026-10-18T10:00:00+08:00')
            self.assertEqual(early['pa_trade_research']['status'],'unavailable')

    def test_compact_live_plan_retains_minute_confirmation_identity(self):
        from pa_trade_report import compact
        from test_pa_entry_confirmation import fixture as opening_fixture
        from pa_entry_confirmation import review
        plan,bars,quote,now=opening_fixture()
        data=study();data['policy']={'active':True,'selected_setup':'breakout_followthrough'}
        data['current_watchlist']=[plan]
        saved=compact(data)['current_watchlist'][0]
        self.assertTrue(review(saved,bars,quote,now,mode='opening_breakout')['conditions_met_for_research'])

    def test_malformed_optional_feed_does_not_block_preparing_report(self):
        import sqlite3
        from snapshot_mail import attach_market_outlook
        from test_pa_entry_confirmation import fixture as opening_fixture
        plan,_,_,now=opening_fixture();data=study();data['current_watchlist']=[plan]
        payload={'information_cutoff':now,'pa_opening_evidence':{'symbols':'invalid feed'}}
        with patch('snapshot_mail.sqlite3.connect',side_effect=sqlite3.DatabaseError('unavailable fixture')):
            attach_market_outlook(payload,'fixture.sqlite3','2026-10-16','2026-10-19',
                                  context_data={'pa_trade_research':data})
        self.assertEqual(payload['research']['pa_opening_confirmation']['confirmed'],[])


if __name__=='__main__':unittest.main()
