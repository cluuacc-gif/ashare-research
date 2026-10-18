import copy
import datetime as dt
import unittest
from pa_entry_confirmation import review,TZ


def fixture():
    plan={'symbol':'000001.SZ','target_trade_date':'2026-10-19','generated_at':'2026-10-18T21:00:00+08:00',
          'eligible':True,'pa_plan_id':'frozen','close':20.,'entry_cap':20.3,'frozen_entry_floor':19.8,
          'structural_stop':19.6,'quantity_ref':100,'atr14':.4}
    start=dt.datetime(2026,10,19,9,30,tzinfo=TZ);bars=[]
    for index in range(6):
        left=start+dt.timedelta(minutes=index);end=left+dt.timedelta(minutes=1)
        bars.append({'symbol':plan['symbol'],'start_at':left.isoformat(),'end_at':end.isoformat(),
                     'observed_at':end.isoformat(),'complete':True,'open':20.,'high':20.05,
                     'low':19.98,'close':20.02,'volume':10000})
    bars[-1].update(open=20.04,high=20.16,low=20.03,close=20.15)
    quote={'symbol':plan['symbol'],'exchange_timestamp':'2026-10-19T09:36:00+08:00',
           'received_at':'2026-10-19T09:36:01+08:00','last':20.15,'ask1':20.16,'ask1_volume':100,
           'prev_close':20.,'status':{'verified':True,'is_st':False,'suspended':False,
           'delisting':False,'trading_status':'normal'},'limits':{'official_verified':True,'limit_up':22.,'limit_down':18.}}
    return plan,bars,quote,'2026-10-19T09:36:02+08:00'


class ConfirmationTests(unittest.TestCase):
    def test_completed_breakout_conditions_do_not_prove_fill(self):
        result=review(*fixture(),mode='opening_breakout')
        self.assertTrue(result['conditions_met_for_research'])
        self.assertFalse(result['actual_fill_verified']);self.assertFalse(result['formal_trade_ready'])

    def test_incomplete_future_or_missing_minute_blocks_cannot_confirm(self):
        for mutation in ('incomplete','future','missing'):
            plan,bars,quote,now=fixture()
            if mutation=='incomplete':bars[-1]['complete']=False
            elif mutation=='future':bars[-1]['observed_at']='2026-10-19T09:37:00+08:00'
            else:del bars[2]
            self.assertFalse(review(plan,bars,quote,now,mode='opening_breakout')['conditions_met_for_research'])

    def test_no_live_official_evidence_means_wait(self):
        plan,bars,quote,now=fixture();quote['status']['verified']=False
        self.assertFalse(review(plan,bars,quote,now,mode='opening_breakout')['conditions_met_for_research'])
        self.assertFalse(review(plan,[],None,now,mode='breakout_retest')['conditions_met_for_research'])

    def test_locked_or_expensive_ask_is_not_an_entry(self):
        plan,bars,quote,now=fixture();quote['ask1']=22.;quote['last']=22.
        self.assertFalse(review(plan,bars,quote,now,mode='opening_breakout')['conditions_met_for_research'])

    def test_retest_requires_later_completed_bar_not_same_bar_path(self):
        plan,bars,quote,now=fixture()
        self.assertFalse(review(plan,bars,quote,now,mode='breakout_retest')['conditions_met_for_research'])
        final=copy.deepcopy(bars[-1]);final.update(start_at='2026-10-19T09:36:00+08:00',end_at='2026-10-19T09:37:00+08:00',
            observed_at='2026-10-19T09:37:00+08:00',open=20.06,high=20.12,low=20.04,close=20.10)
        bars.append(final);quote.update(exchange_timestamp=final['end_at'],received_at='2026-10-19T09:37:01+08:00',last=20.1,ask1=20.11)
        result=review(plan,bars,quote,'2026-10-19T09:37:02+08:00',mode='breakout_retest')
        self.assertTrue(result['conditions_met_for_research'])

    def test_plan_created_after_open_is_not_a_frozen_plan(self):
        plan,bars,quote,now=fixture();plan['generated_at']='2026-10-19T09:31:00+08:00'
        self.assertFalse(review(plan,bars,quote,now,mode='opening_breakout')['conditions_met_for_research'])


if __name__=='__main__':unittest.main()
