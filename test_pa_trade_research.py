import unittest
import pandas as pd
import pa_trade_research as p
from pa_trade_engine import make_plan
from test_pa_trade_engine import breakout
from test_price_action_research import candidate


def row(**kwargs):
    kwargs={'buy':20,'day1_close':20.1,'day2_open':20.1,'day2_high':21,
            'day2_low':19.9,'day2_close':20.5,**kwargs}
    source=candidate(**kwargs);source.update(breakout())
    plan=make_plan(source,'breakout_followthrough')
    if not plan['eligible']:raise AssertionError(plan['reasons'])
    source['trade_plan']={**plan,'rank_score':80,'breakout_level':source['breakout_level']}
    return source


class PATradeReplayTests(unittest.TestCase):
    def test_red_failed_d1_stays_in_denominator_and_exits_d2_open(self):
        source=row(day1_close=19.7,day2_open=19.5,day2_low=19.4,day2_high=21,day2_close=20.8)
        records=p.simulate(pd.DataFrame([source]));trade=records[0]
        self.assertEqual(trade['status'],'closed');self.assertTrue(trade['D1_structure_failed'])
        self.assertEqual(trade['reference_sell_price'],19.5)
        self.assertEqual(trade['exit_date'],'2026-01-05')
        self.assertLess(trade['gross_return'],0)
        self.assertEqual(p.metrics(records)['n_attempts'],1)

    def test_same_day_buy_sale_never_added_and_same_bar_stop_is_conservative(self):
        source=row(day2_open=20,day2_high=21,day2_low=19.5,day2_close=20.8)
        trade=p.simulate(pd.DataFrame([source]))[0]
        self.assertFalse(trade['D1_structure_failed']);self.assertTrue(trade['ambiguous_same_bar'])
        self.assertEqual(trade['reference_sell_price'],source['trade_plan']['structural_stop'])
        self.assertGreater(trade['optimistic_gross_return'],0)

    def test_gap_below_frozen_floor_cancels_instead_of_catching_failure(self):
        source=row(buy=19.7,day1_close=19.8)
        trade=p.simulate(pd.DataFrame([source]))[0]
        self.assertEqual(trade['status'],'no_fill')

    def test_quantity_and_budget_are_frozen_no_same_open_replacement(self):
        sources=[row(symbol=f'{i:06}.SZ') for i in range(4)]
        sources[0]['reference_buy_price']=21
        trades=p.simulate(pd.DataFrame(sources))
        self.assertEqual(len(trades),3);self.assertEqual(trades[0]['status'],'no_fill')
        for trade in trades:self.assertEqual(trade['frozen_order_quantity'],sources[0]['trade_plan']['quantity_ref'])

    def test_unknown_execution_reserves_cash_and_position_capacity(self):
        sources=[row(symbol=f'{i:06}.SZ') for i in range(3)]
        for source in sources:source.update(entry_one_price=True,outcome_status='unresolved')
        sources.append(row(signal='2026-01-06',entry='2026-01-07',exit_='2026-01-08',symbol='999999.SZ'))
        trades=p.simulate(pd.DataFrame(sources))
        self.assertEqual(sum(trade['status']=='unresolved' for trade in trades),3)
        self.assertEqual(trades[-1]['status'],'abstain')
        self.assertEqual(trades[0]['reserved_cash'],trades[0]['frozen_capital'])

    def test_invalid_entry_quote_cannot_become_retrospective_cancellation(self):
        source=row();source.update(reference_buy_price=22,entry_evidence_valid=False,outcome_status='unresolved')
        self.assertEqual(p.simulate(pd.DataFrame([source]))[0]['status'],'unresolved')

    def test_d3_missing_does_not_delete_closed_d2_trade(self):
        source=row();source.update(strength3_label=None,day3_date=None)
        trade=p.simulate(pd.DataFrame([source]))[0]
        self.assertEqual(trade['status'],'closed');self.assertIsNone(trade['strength3_label'])

    def test_fixed_exit_control_keeps_the_same_entry_plan(self):
        source=row(day1_close=19.7,day2_open=20.,day2_high=21.,day2_low=19.9,day2_close=20.8)
        structural=p.simulate(pd.DataFrame([source]))[0]
        fixed=p.simulate(pd.DataFrame([source]),fixed_exit=True)[0]
        self.assertEqual(structural['frozen_order_quantity'],fixed['frozen_order_quantity'])
        self.assertEqual(structural['reference_buy_price'],fixed['reference_buy_price'])
        self.assertTrue(structural['D1_structure_failed']);self.assertFalse(fixed['D1_structure_failed'])
        self.assertEqual(structural['reference_sell_price'],20.)
        self.assertGreater(fixed['gross_return'],0)

    def test_setup_prefilter_does_not_remove_a_shared_engine_valid_plan(self):
        source=row();result=p.plans(pd.DataFrame([{key:value for key,value in source.items() if key!='trade_plan'}]),'breakout_followthrough')
        self.assertEqual(len(result),1)
        self.assertTrue(result.iloc[0].trade_plan['eligible'])


if __name__=='__main__':unittest.main()
