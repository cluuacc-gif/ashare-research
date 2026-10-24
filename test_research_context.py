"""Only timely, date-matched real vendor flows can reach the report."""
import copy
import json
import subprocess
import unittest
from unittest.mock import patch

import research_context as r

BASE='2026-10-16'
CUTOFF='2026-10-18T17:00:00+08:00'


def flow():
    return dict(schema='sector-fund-flow-v1',classification='eastmoney_industry',status='ready',
                base_trade_date=BASE,information_cutoff='2026-10-18T16:00:00+08:00',
                observed_at='2026-10-18T16:00:00+08:00',sectors=[dict(code='BK0001',name='fixture',
                trade_date=BASE,source_updated_at=BASE+'T15:00:00+08:00',
                observed_at='2026-10-18T15:59:00+08:00',main_net_inflow_yuan=10_000_000,
                members=[dict(symbol='000001.SZ',effective_date='2026-10-18',observed_at='2026-10-18T15:59:00+08:00')])])


class ContextTests(unittest.TestCase):
    def test_weekend_known_flow_valid_for_next_session_but_not_previous_friday(self):
        self.assertEqual(r.verified_flow(flow(),BASE,CUTOFF)['status'],'ready')
        self.assertEqual(r.verified_flow(flow(),BASE,BASE+'T18:00:00+08:00')['status'],'unavailable')

    def test_mixed_taxonomy_stale_date_unknown_and_future_time_fail_closed(self):
        for key,value in [('classification','sina_industry'),('base_trade_date','2026-10-15'),
                          ('observed_at',None),('observed_at','2026-10-19T09:00:00+08:00')]:
            with self.subTest(key=key):
                data=flow();data[key]=value
                self.assertEqual(r.verified_flow(data,BASE,CUTOFF)['sectors'],[])

    def test_future_member_or_wrong_vendor_quote_date_cannot_enter(self):
        for member in (True,False):
            data=flow()
            if member:data['sectors'][0]['members'][0]['observed_at']='2026-10-19T09:00:00+08:00'
            else:data['sectors'][0]['source_updated_at']='2026-10-15T15:00:00+08:00'
            self.assertEqual(r.verified_flow(data,BASE,CUTOFF)['status'],'unavailable')

    def test_source_unavailability_cannot_become_a_volume_proxy(self):
        data=flow();data['status']='unavailable'
        result=r.verified_flow(data,BASE,CUTOFF)
        self.assertEqual(result['sectors'],[])
        self.assertFalse(result['flow_is_price_volume_proxy'])

    def test_published_context_read_is_bounded_and_never_uses_smtp(self):
        snapshot=dict(base_trade_date=BASE,release_tag='fixture',database={'sha256':'a'*64})
        with patch.object(r.subprocess,'run',side_effect=subprocess.TimeoutExpired(['gh'],10)) as read:
            result=r.published_context(snapshot,CUTOFF)
        self.assertEqual(read.call_args.kwargs['timeout'],10)
        self.assertEqual(result['fund_flow']['status'],'unavailable')

    def test_large_pointer_members_are_expanded_before_the_existing_time_checks(self):
        from fund_flow_pointer import pack_members
        from test_market_outlook import large_flow
        data=large_flow()
        packed=pack_members(copy.deepcopy(data))
        result=r.verified_flow(packed,BASE,CUTOFF)
        self.assertEqual(result['status'],'ready')
        self.assertEqual(result['sectors'][0]['members'],data['sectors'][0]['members'])
        data['sectors'][0]['members'][0]['flow_trade_date']='2026-10-15'
        self.assertEqual(r.verified_flow(pack_members(data),BASE,CUTOFF)['status'],'unavailable')

    def test_corrupt_member_payload_fails_closed_without_an_extra_network_request(self):
        from fund_flow_pointer import pack_members
        from test_market_outlook import large_flow
        data=pack_members(large_flow())
        data['sectors'][0]['members_payload']['sha256']='0'*64
        with patch.object(r.subprocess,'run') as network:
            result=r.verified_flow(data,BASE,CUTOFF)
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(result['sectors'],[])
        network.assert_not_called()


if __name__=='__main__':unittest.main()
