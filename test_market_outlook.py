"""Publication evidence invariants; fixture values are not market predictions."""
import copy
import datetime as dt
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fund_flow_pointer as fp
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
    def test_legacy_holding_wording_keeps_exact_rendered_bytes(self):
        from test_active_trade_report import study
        expected = {None: 'f898f92397bdb210571f8a4d1ccf0275f20ba9c2f89f095d5c320513bc2e3dc7',
                    1: 'c7a629947febb2ae5181d1b52be5567650db28dbb0389b87ba4395e2fdfa7ba6',
                    2: 'c01b4144cd8c60fbec2973edfdfb7876842a74045a1c55f2e1f3a9db1760ac5d'}
        for version, digest in expected.items():
            with self.subTest(version=version):
                value, _ = fixture()
                if version is not None:
                    value['active_trade_system'] = study()
                    value['active_trade_system']['selection_rule_version'] = version
                self.assertEqual(hashlib.sha256(render(value).encode()).hexdigest(), digest)

    def test_v3_overview_keeps_route_deadlines_and_labels_old_research(self):
        from test_active_trade_report import study
        for route, sessions, deadline in (('trend_continuation', 2, '2026-10-21'),
                                         ('weak_to_strong_rebound', 1, '2026-10-20')):
            with self.subTest(route=route):
                value, _ = fixture()
                active = value['active_trade_system'] = study()
                active.update(selection_rule_version=3, presentation_revision=2,
                    market_context={'route': route})
                active['plans'][0].update(holding_policy_version=3, setup_route=route,
                    max_hold_after_entry_sessions=sessions, D2_trade_date='2026-10-20',
                    planned_exit_trade_date=deadline)
                body = render(value)
                self.assertIn('买后1至2个交易日按冻结路线退出', body)
                self.assertIn('反弹／过热防守最迟买后第1交易日退出', body)
                self.assertIn('趋势延续最迟买后第2交易日退出', body)
                self.assertIn('旧T+1研究口径，只描述历史对照', body)
                self.assertIn('旧T+1研究目标：', body)
                self.assertIn(deadline, body)
                self.assertNotIn('T+1主目标：', body)
                self.assertNotIn('目标仍是提高第1天买入、第2天卖出', body)
                self.assertNotIn('买入与次日退出的研究顺序', body)
                self.assertEqual(render(m.compact_pointer({'outlook': value})['outlook']), body)

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


def large_flow():
    """128 boards with 3,000 complete members, rather than a tiny toy pointer."""
    from test_research_context import flow
    value = flow()
    template = value['sectors'][0]
    value['sectors'] = []
    for board_index in range(128):
        board = copy.deepcopy(template)
        board.update(code=f'BK{board_index:04d}', name=f'行业{board_index}',
                     main_net_inflow_yuan=128_000_000-board_index*1_000_000,
                     main_net_ratio_pct=1, main_net_3d_yuan=200_000_000,
                     main_net_5d_yuan=300_000_000, membership_status='complete')
        board['members'] = []
        if board_index < 12:
            for index in range(250):
                board['members'].append({
                    'symbol': f'{board_index*250+index:06d}.SZ', 'name': f'股票{index}',
                    'effective_date': '2026-10-18',
                    'observed_at': '2026-10-18T15:59:00+08:00',
                    'flow_status': 'verified_current_date', 'flow_trade_date': '2026-10-16',
                    'flow_observed_at': '2026-10-18T15:59:00+08:00',
                    'source_updated_at': '2026-10-16T15:00:00+08:00',
                    'main_net_inflow_yuan': index*1234567-100000000,
                    'source_url': 'https://example.invalid/members', 'raw_sha256': 'a'*64,
                    'flow_date_source_url': 'https://example.invalid/member-dates',
                    'flow_date_raw_sha256': 'b'*64})
        board['member_count'] = len(board['members'])
        board['vendor_member_total'] = len(board['members'])
        value['sectors'].append(board)
    return value


class MemberCompactionTests(unittest.TestCase):
    def test_large_current_pointer_keeps_all_members_and_identical_report(self):
        value, _ = fixture()
        value['fund_flow'] = large_flow()
        manifest = {'schema': 'market-outlook-publication-v1', 'outlook': value,
                    'files': {'outlook.json': {'sha256': 'c'*64, 'bytes': 9_000_000}}}
        pointer = m.compact_pointer(manifest)
        self.assertLess(len(json.dumps(pointer, ensure_ascii=False, indent=2).encode()), 200_000)
        self.assertEqual(render(pointer['outlook']), render(value))
        self.assertEqual(pointer['files'], manifest['files'])
        self.assertEqual(len(value['fund_flow']['sectors'][0]['members']), 250)
        restored = fp.unpack_members(pointer['outlook']['fund_flow'])
        self.assertEqual(sum(len(board['members']) for board in restored['sectors']), 3000)
        for full, expanded in zip(value['fund_flow']['sectors'], restored['sectors']):
            for original, member in zip(full['members'], expanded['members']):
                self.assertEqual(member, original)
        self.assertEqual(m.compact_pointer(pointer), pointer)

    def test_packing_is_lossless_and_preserves_zero_negative_unknown_flow(self):
        original = large_flow()
        original['sectors'][0]['members'][0]['main_net_inflow_yuan'] = None
        original['sectors'][0]['members'][1]['main_net_inflow_yuan'] = 0
        compact = fp.pack_members(copy.deepcopy(original))
        self.assertEqual(fp.unpack_members(compact), original)
        self.assertIn('members_payload', compact['sectors'][0])
        self.assertNotIn('members', compact['sectors'][0])

    def test_existing_future_member_checks_still_apply_after_expansion(self):
        from research_context import verified_flow
        from test_research_context import BASE, CUTOFF
        original = large_flow()
        compact = fp.pack_members(copy.deepcopy(original))
        self.assertEqual(verified_flow(fp.unpack_members(compact), BASE, CUTOFF)['status'], 'ready')
        original['sectors'][0]['members'][0]['flow_observed_at'] = '2026-10-19T09:00:00+08:00'
        malformed = fp.pack_members(original)
        self.assertEqual(verified_flow(fp.unpack_members(malformed), BASE, CUTOFF)['status'], 'unavailable')

    def test_checksum_count_schema_encoding_and_size_cannot_silently_drop_members(self):
        original = fp.pack_members(large_flow())
        for key, replacement in [('sha256', '0'*64), ('member_count', 249),
                                 ('schema', 'unknown'), ('zlib_base64', 'invalid!'),
                                 ('sector_code', 'BK9999'), ('base_trade_date', '2026-10-15'),
                                 ('uncompressed_bytes', 2), ('uncompressed_bytes', True),
                                 ('uncompressed_bytes', fp.MAX_BOARD_BYTES+1)]:
            with self.subTest(key=key, replacement=replacement):
                value = copy.deepcopy(original)
                value['sectors'][0]['members_payload'][key] = replacement
                with self.assertRaises(ValueError): fp.unpack_members(value)

    def test_duplicate_or_trailing_member_representations_are_rejected(self):
        import base64
        import zlib
        original = fp.pack_members(large_flow())
        duplicate = copy.deepcopy(original)
        duplicate['sectors'][0]['members'] = []
        with self.assertRaises(ValueError): fp.unpack_members(duplicate)
        trailing = copy.deepcopy(original)
        payload = trailing['sectors'][0]['members_payload']
        packed = base64.b64decode(payload['zlib_base64']) + zlib.compress(b'[]')
        payload['zlib_base64'] = base64.b64encode(packed).decode()
        with self.assertRaises(ValueError): fp.unpack_members(trailing)

    def test_decompression_bomb_and_total_expansion_budget_are_bounded(self):
        import base64
        import zlib
        original = fp.pack_members(large_flow())
        bomb = copy.deepcopy(original)
        payload = bomb['sectors'][0]['members_payload']
        payload['uncompressed_bytes'] = 32
        payload['zlib_base64'] = base64.b64encode(zlib.compress(b' '*(fp.MAX_BOARD_BYTES+1))).decode()
        with self.assertRaises(ValueError): fp.unpack_members(bomb)
        first_size = original['sectors'][0]['members_payload']['uncompressed_bytes']
        with patch.object(fp, 'MAX_TOTAL_BYTES', first_size):
            with self.assertRaises(ValueError): fp.unpack_members(original)


if __name__=='__main__':
    unittest.main()
