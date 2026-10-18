"""Publication evidence invariants; fixture values are not market predictions."""
import copy
import datetime as dt
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import market_outlook as m
from market_outlook_report import render


def fixture():
    source={'release_tag':'prepared-fixture','base_trade_date':'2026-10-16',
            'next_trade_date':'2026-10-19','database':{'name':'market.sqlite3','sha256':'a'*64}}
    identity={'base_trade_date':'2026-10-16','target_trade_date':'2026-10-19',
              'information_cutoff':'2026-10-18T12:00:00+08:00'}
    value={'schema':'market-outlook-v1',**identity,'generated_at':'2026-10-18T12:00:01+08:00',
           'snapshot_release':source['release_tag'],'input_database_sha256':'a'*64,
           'formal_trade_ready':False,'actual_win_rate':None,'execution_win_rate_80_validated':False,
           'sector_outlook':dict(identity,rankings=[]),'opening_strength_plan':dict(identity,watchlist=[])}
    return value,source


class PublicationTests(unittest.TestCase):
    def test_weekend_prediction_targets_next_verified_session(self):
        value,source=fixture()
        self.assertIs(m.validate(value,source),value)

    def test_large_frozen_constituents_are_archived_outside_current_pointer(self):
        value,_=fixture()
        value['sector_outlook']['frozen_members']={'sector':['x'*2_000_000]}
        manifest={'schema':'market-outlook-publication-v1','outlook':value}
        pointer=m.compact_pointer(manifest)
        self.assertLess(len(json.dumps(pointer)),5000)
        self.assertNotIn('frozen_members',pointer['outlook']['sector_outlook'])
        self.assertIn('frozen_members',manifest['outlook']['sector_outlook'])
        self.assertEqual(render(pointer['outlook']),render(value))

    def test_oversized_summary_is_rejected_before_publication(self):
        value,_=fixture()
        value['unbounded_summary']='x'*900_000
        with self.assertRaises(ValueError):
            m.compact_pointer({'outlook':value})

    def test_compaction_preserves_stock_flow_facts_and_archives_growing_ledgers(self):
        value,_=fixture()
        value['fund_flow']={'sectors':[{'history':[1], 'field_evidence':'large evidence',
            'members':[{'symbol':'000001.SZ','source_url':'https://example.invalid',
                        'raw_sha256':'a'*64,'flow_status':'verified_current_date',
                        'flow_trade_date':'2026-10-16','main_net_inflow_yuan':100}]}]}
        value['sector_forward_tracking']={'resolved':[{'large':'audit'}],'resolved_dates':1}
        pointer=m.compact_pointer({'outlook':value})
        part=pointer['outlook']['fund_flow']['sectors'][0]
        self.assertNotIn('field_evidence',part)
        self.assertNotIn('source_url',part['members'][0])
        self.assertEqual(part['members'][0]['main_net_inflow_yuan'],100)
        self.assertNotIn('resolved',pointer['outlook']['sector_forward_tracking'])
        self.assertEqual(render(pointer['outlook']),render(value))

    def test_stock_tracking_keeps_D2_price_event_when_D3_is_still_pending(self):
        value,source=fixture()
        old={'sector_stock_selection':{'primary':[{'symbol':'000001.SZ','close':6}]}}
        prior={'forward_eligible':True,'target_trade_date':'2026-10-16','release_tag':'first',
               'files':{'outlook.json':{'name':'outlook.json'}}}
        measured={'status':'pending','target_trade_date':'2026-10-16','n_D2_resolved':1,
                  'n_D2_open_reference_profitable':1,'n_strength3_resolved':0,'n_strength3_pending':1,
                  'actual_win_rate':None,'rows':[]}
        with patch.object(m,'archived_outlook',return_value={'target_trade_date':'2026-10-16'}),\
             patch('stock_strength_tracking.evaluate',return_value=measured):
            m.stock_forward_tracking(value,source,'fixture-store','fixture/repo',prior,old)
        ledger=value['stock_strength_forward_tracking']
        self.assertEqual(ledger['n_D2_resolved'],1)
        self.assertEqual(ledger['D2_open_reference_profitable_rate'],1)
        self.assertIsNone(ledger['strength3_rate'])
        self.assertIsNone(ledger['actual_win_rate'])
        self.assertEqual(len(ledger['pending']),1)

    def test_identity_cutoff_and_trade_claims_rejected(self):
        value,source=fixture()
        changes=[('input_database_sha256','b'*64),('target_trade_date','2026-10-20'),
                 ('information_cutoff','2026-10-16T14:59:59+08:00'),
                 ('information_cutoff','2026-10-18T12:00:02+08:00'),
                 ('information_cutoff','2026-10-18T12:00:00'),
                 ('formal_trade_ready',True),('actual_win_rate',.81),
                 ('execution_win_rate_80_validated',True)]
        for key,new in changes:
            with self.subTest(key=key,new=new),self.assertRaises(ValueError):
                m.validate({**value,key:new},source)

    def test_optional_section_cannot_smuggle_future_cutoff(self):
        value,source=fixture()
        value['sector_outlook']['information_cutoff']='2026-10-19T15:00:00+08:00'
        with self.assertRaises(ValueError):
            m.validate(value,source)

    def test_bundle_hash_and_rendered_report_must_both_match(self):
        value,source=fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            m.state.write(root/'outlook.json',value)
            m.state.write(root/'source_manifest.json',source)
            (root/'report.html').write_text(render(value),encoding='utf-8')
            manifest={'outlook':value,'files':{name:m.state.ref(root/name) for name in m.ASSETS}}
            self.assertEqual(m.verify_bundle(root,manifest),value)
            (root/'report.html').write_text('tampered HTML',encoding='utf-8')
            with self.assertRaises(ValueError):
                m.verify_bundle(root,manifest)
            # Even an updated asset hash must not bless a different displayed report.
            manifest['files']['report.html']=m.state.ref(root/'report.html')
            with self.assertRaises(ValueError):
                m.verify_bundle(root,manifest)

    def test_unknown_pointer_network_failure_cannot_become_empty(self):
        import subprocess
        failure=subprocess.CalledProcessError(1,['gh'],stderr='HTTP 403')
        with patch.object(m.state,'run',side_effect=failure),self.assertRaises(subprocess.CalledProcessError):
            m.read_pointer('fixture/repo')

    def test_html_escapes_source_labels_and_ticker_names(self):
        value,_=fixture()
        value['sector_outlook']['rankings']=[{'industry':'<script>sector</script>','score':66,
            'drivers':['<img src=x onerror=alert(1)>'],'risks':['risk']}]
        value['opening_strength_plan']['watchlist']=[{'symbol':'000001.SZ','name':'<script>stock</script>'}]
        body=render(value)
        self.assertNotIn('<script>',body)
        self.assertIn('&lt;script&gt;sector',body)
        self.assertIn('不是上涨概率',body)
        self.assertIn('独立云主机尚未部署',body)

    def tracking_case(self,status='resolved', previous=None):
        value,source=fixture()
        frozen={'forecast_id':'frozen-first','target_date':'2026-10-16','status':'ready'}
        raw=json.dumps({'sector_outlook':frozen}).encode()
        asset={'name':'outlook.json','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
        prior={'release_tag':'first-release','forward_eligible':True,'files':{'outlook.json':asset},
               'outlook':{'sector_outlook':frozen,'sector_forward_tracking':previous or {}}}
        result={'forecast_id':'frozen-first','target_date':'2026-10-16','status':status,
                'forward_eligible':True,'top3_evaluated_sectors':3,'top3_relative_strength_hits':2,
                'top3_absolute_up_hits':1,'top3_top_quartile_hits':1,'missing_stocks':1 if status!='resolved' else 0}
        def download(*args):
            destination=Path(args[args.index('--dir')+1])/'outlook.json'
            destination.write_bytes(raw)
            return ''
        with patch.object(m,'read_pointer',return_value=(prior,'blob')),patch.object(m.state,'run',side_effect=download) as run:
            with patch('sector_forecast.evaluate',return_value=result) as evaluate:
                m.forward_tracking(value,source,'fixture-store','fixture/repo')
        return value,run,evaluate

    def test_forward_tracking_uses_frozen_release_and_separate_outcomes(self):
        value,run,evaluate=self.tracking_case()
        ledger=value['sector_forward_tracking']
        self.assertEqual(ledger['resolved_dates'],1)
        self.assertEqual(ledger['relative_strength_hit_rate'],2/3)
        self.assertEqual(ledger['absolute_up_rate'],1/3)
        self.assertIsNone(ledger['gross_trade_win_rate'])
        self.assertEqual(evaluate.call_args.args[1]['forecast_id'],'frozen-first')
        run.assert_called_once()

    def test_unresolved_forecast_cannot_improve_hit_rate(self):
        value,_,_=self.tracking_case('unresolved')
        ledger=value['sector_forward_tracking']
        self.assertEqual(ledger['resolved_dates'],0)
        self.assertIsNone(ledger['relative_strength_hit_rate'])
        self.assertEqual(ledger['pending'][0]['forecast_id'],'frozen-first')

    def test_repeat_run_does_not_count_same_target_twice(self):
        first,_,_=self.tracking_case()
        second,run,evaluate=self.tracking_case(previous=first['sector_forward_tracking'])
        self.assertEqual(second['sector_forward_tracking']['resolved_dates'],1)
        run.assert_not_called()
        evaluate.assert_not_called()


if __name__=='__main__':
    unittest.main()
