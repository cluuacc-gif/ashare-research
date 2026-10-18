"""Contract tests for actual vendor flows, dates, identity and frozen evidence."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import sector_fund_flow as flow

BASE = '2026-10-16'
OBSERVED = '2026-10-18T16:00:00+08:00'
CUTOFF = '2026-10-18T17:00:00+08:00'
UPDATED = int(datetime.fromisoformat('2026-10-16T16:05:00+08:00').timestamp())


def vendor_request(url, timeout):
    query = parse_qs(urlsplit(url).query)
    if 'daykline' in url:
        payload = {'rc': 0, 'data': {'code': 'BK0422', 'market': 90,
            'klines': ['2026-10-15,10,-5,-5,4,6,1,-.5,-.5,.4,.6,100,1',
                       '2026-10-16,20,-10,-10,8,12,2,-1,-1,.8,1.2,102,2',
                       '2026-10-19,90,-40,-50,30,60,9,-4,-5,3,6,111,9']}}
    elif query.get('code', [''])[0].startswith('b:'):
        key = query['key'][0]
        payload = {'rc': 0, 'data': {'total': 3, 'diff': [
            {'f12': '000001', 'f13': 0, 'f14': 'A股甲', key: UPDATED if key == 'f124' else 11},
            {'f12': '600001', 'f13': 1, 'f14': 'A股乙', key: UPDATED if key == 'f124' else 12},
            {'f12': '900001', 'f13': 1, 'f14': 'B股丙', key: UPDATED if key == 'f124' else 1}]}}
    else:
        key = query['key'][0]
        values = {'f62': 2000, 'f184': 250, 'f267': 3000, 'f268': 210,
                  'f164': 2500, 'f165': 150, 'f174': 2000, 'f175': 100,
                  'f3': 123, 'f109': 234, 'f160': 345,
                  'f66': 800, 'f72': 1200, 'f6': 80000, 'f124': UPDATED}
        payload = {'rc': 0, 'data': {'total': 1, 'diff': [
            {'f12': 'BK0422', 'f13': 90, 'f14': '物流', key: values[key]}]}}
    return json.dumps(payload, ensure_ascii=False).encode('utf-8'), OBSERVED


class SectorFundFlowTests(unittest.TestCase):
    def collect(self, **kwargs):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / 'frozen'
        with patch.object(flow, '_request', side_effect=vendor_request):
            value = flow.collect(output, BASE, cutoff=CUTOFF, **kwargs)
        return value, output

    def test_real_fields_keep_units_and_three_day_identity(self):
        value, _ = self.collect()
        sector = value['sectors'][0]
        self.assertEqual(value['status'], 'ready')
        self.assertEqual(sector['main_net_inflow_yuan'], 2000)
        self.assertEqual(sector['main_net_ratio_pct'], 2.50)
        self.assertEqual(sector['return_1d_pct'], 1.23)
        self.assertEqual(sector['main_net_3d_yuan'], 3000)
        self.assertEqual(sector['main_net_5d_yuan'], 2500)
        self.assertEqual(sector['main_net_10d_yuan'], 2000)
        self.assertEqual(sector['trade_date'], BASE)
        self.assertTrue(sector['components_reconciled'])
        self.assertFalse(sector['score_is_probability'])

    def test_membership_is_observed_now_not_backdated_to_quotes(self):
        value, _ = self.collect()
        sector = value['sectors'][0]
        self.assertEqual(sector['membership_status'], 'complete')
        self.assertEqual(sector['member_count'], 2)
        self.assertEqual(sector['vendor_member_total'], 3)
        self.assertEqual(sector['excluded_non_ashare_count'], 1)
        self.assertEqual(sector['membership_scope'], 'A_share_only')
        self.assertEqual(sector['members'][0]['effective_date'], '2026-10-18')
        self.assertEqual({row['symbol'] for row in sector['members']}, {'000001.SZ', '600001.SH'})
        self.assertTrue(all(row['flow_status'] == 'verified_current_date' for row in sector['members']))
        self.assertEqual(sector['members'][0]['main_net_inflow_yuan'], 11)
        self.assertEqual(sector['members'][0]['flow_trade_date'], BASE)

    def test_stock_money_flow_not_accepted_when_its_own_date_is_stale(self):
        def stale_members(url, timeout):
            raw, observed = vendor_request(url, timeout)
            if 'key=f124' in url and 'code=b' in url:
                payload = json.loads(raw)
                for row in payload['data']['diff']:
                    row['f124'] = UPDATED - 86400
                raw = json.dumps(payload).encode()
            return raw, observed
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with patch.object(flow, '_request', side_effect=stale_members):
            value = flow.collect(Path(temporary.name) / 'stale-members', BASE, cutoff=CUTOFF)
        members = value['sectors'][0]['members']
        self.assertEqual(len(members), 2)
        self.assertTrue(all(row['flow_status'] == 'date_unverified' for row in members))
        self.assertTrue(all(row['main_net_inflow_yuan'] is None for row in members))

    def test_current_history_excludes_future_targets_and_has_no_pit_claim(self):
        value, _ = self.collect()
        history = value['sectors'][0]['history']
        self.assertEqual([row['trade_date'] for row in history], ['2026-10-15', BASE])
        self.assertTrue(all(not row['historical_live_availability_verified'] for row in history))

    def test_provided_earlier_cutoff_does_not_backdate_response(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / 'frozen'
        with patch.object(flow, '_request', side_effect=vendor_request):
            value = flow.collect(output, BASE, cutoff='2026-10-16T18:00:00+08:00')
        self.assertEqual(value['status'], 'unavailable')
        self.assertEqual(value['sectors'], [])
        self.assertIn('observation_after_cutoff:f62', value['blockers'])

    def test_no_vendor_feed_means_no_synthetic_money_flow(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / 'frozen'
        with patch.object(flow, '_request', side_effect=TimeoutError('test offline')):
            value = flow.collect(output, BASE, cutoff=CUTOFF)
        self.assertEqual(value['status'], 'unavailable')
        self.assertEqual(value['sectors'], [])
        self.assertIn('no_current_dated_vendor_money_flow', value['blockers'])
        self.assertEqual(flow.load_verified(output), value)

    def test_old_vendor_date_rejected(self):
        def stale(url, timeout):
            raw, observed = vendor_request(url, timeout)
            if 'key=f124' in url:
                payload = json.loads(raw)
                payload['data']['diff'][0]['f124'] = UPDATED - 86400
                raw = json.dumps(payload).encode()
            return raw, observed
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with patch.object(flow, '_request', side_effect=stale):
            value = flow.collect(Path(temporary.name) / 'stale', BASE, cutoff=CUTOFF)
        self.assertEqual(value['sectors'], [])

    def test_inconsistent_large_order_components_rejected(self):
        def conflicting(url, timeout):
            raw, observed = vendor_request(url, timeout)
            if 'key=f66' in url:
                payload = json.loads(raw)
                payload['data']['diff'][0]['f66'] = 999999
                raw = json.dumps(payload).encode()
            return raw, observed
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with patch.object(flow, '_request', side_effect=conflicting):
            value = flow.collect(Path(temporary.name) / 'conflicting', BASE, cutoff=CUTOFF)
        self.assertEqual(value['sectors'], [])
        self.assertIn('main_components_conflict:BK0422', value['blockers'])

    def test_raw_response_bytes_are_covered_by_hash(self):
        _, output = self.collect()
        with (output / 'raw/industries-f62.json').open('ab') as stream:
            stream.write(b' ')
        with self.assertRaisesRegex(ValueError, 'raw fund-flow evidence checksum mismatch'):
            flow.load_verified(output)

    def test_payload_bytes_are_covered_by_hash(self):
        _, output = self.collect()
        with (output / 'fund_flow.json').open('ab') as stream:
            stream.write(b' ')
        with self.assertRaisesRegex(ValueError, 'artifact checksum mismatch'):
            flow.load_verified(output)

    def test_evidence_path_traversal_rejected_even_after_rehash(self):
        _, output = self.collect()
        manifest = json.loads((output / 'evidence_manifest.json').read_text(encoding='utf-8'))
        manifest['evidence'][0]['raw_file'] = '../outside.json'
        flow._write_json(output / 'evidence_manifest.json', manifest)
        digest = hashlib.sha256((output / 'evidence_manifest.json').read_bytes()).hexdigest()
        value = json.loads((output / 'fund_flow.json').read_text(encoding='utf-8'))
        value['evidence_manifest_sha256'] = digest
        flow._write_json(output / 'fund_flow.json', value)
        flow._write_json(output / 'checksums.json', {'fund_flow.json': hashlib.sha256(
            (output / 'fund_flow.json').read_bytes()).hexdigest(), 'evidence_manifest.json': digest})
        with self.assertRaisesRegex(ValueError, 'unsafe evidence path'):
            flow.load_verified(output)

    def test_invalid_limits_do_not_create_output(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / 'bad'
        with self.assertRaises(ValueError):
            flow.collect(output, BASE, max_members=100)
        self.assertFalse(output.exists())

    def test_existing_snapshot_not_overwritten(self):
        _, output = self.collect()
        with self.assertRaises(FileExistsError):
            flow.collect(output, BASE)


if __name__ == '__main__':
    unittest.main()
