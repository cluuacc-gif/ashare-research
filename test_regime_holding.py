"""Synthetic behavior tests for v3 routes and frozen sell deadlines, not returns."""
import copy
import datetime as dt
import unittest

import active_regime as regime
import active_trade_system as active
import active_trade_book as book
import active_trade_report as report
from test_active_trade_system import fixtures, BASE, TARGET, CUTOFF
from test_active_trade_book import plan as old_plan, entry_evidence, minute
from test_nextday_forward import db_fixture, evidence

D1, D2, D3 = '2026-10-19', '2026-10-20', '2026-10-21'


def plan(route=regime.TREND):
    value=old_plan()
    value.update(holding_policy_version=3,setup_route=route,
        max_hold_after_entry_sessions=1 if route==regime.REBOUND else 2,
        planned_exit_trade_date=D2 if route==regime.REBOUND else D3)
    return value


class RegimeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.db,self.outlook,self.opening,self.flow=fixtures()
        self.addCleanup(self.db.close)

    def build(self, **kwargs):
        return active.build(self.db,BASE,TARGET,CUTOFF,self.outlook,self.opening,self.flow,**kwargs)

    def test_previous_red_market_is_not_itself_an_order_or_total_veto(self):
        self.outlook['benchmark'].update(base_breadth=.42,base_return_1d=-.004,base_return_5d=-.01)
        value=self.build()
        self.assertEqual(value['market_context']['route'],regime.REBOUND)
        self.assertEqual(len(value['plans']),1)
        selected=value['plans'][0]
        self.assertEqual(selected['max_hold_after_entry_sessions'],1)
        self.assertEqual(selected['planned_exit_trade_date'],selected['D2_trade_date'])
        self.assertLessEqual(selected['frozen_reserved_cash_cny'],5000)
        self.assertLessEqual(selected['frozen_reserved_risk_cny'],75)
        self.assertFalse(selected['entry_confirmed'])
        self.assertFalse(value['market_context']['T_day_direction_is_a_prediction'])
        # Missing actual flow still blocks: red-market classification is no buy signal.
        missing=active.build(self.db,BASE,TARGET,CUTOFF,self.outlook,self.opening,None)
        self.assertFalse(missing['plans'])

    def test_green_market_requires_independent_quality_and_precommits_deadline(self):
        value=self.build()
        self.assertEqual(len(value['plans']),2)
        self.assertTrue(all(item['setup_route']==regime.TREND for item in value['plans']))
        self.assertTrue(all(item['planned_exit_trade_date']==D3 for item in value['plans']))
        for key in ('base_breadth','base_return_1d','base_return_5d'):
            other=copy.deepcopy(self.outlook);other['benchmark'][key]=None
            self.assertFalse(active.build(self.db,BASE,TARGET,CUTOFF,other,self.opening,self.flow)['plans'])
        # A large T rally cannot make an extended stock pass its independent PA checks.
        self.db.execute('UPDATE daily_quotes SET open=open*1.2,high=high*1.2,low=low*1.2,close=close*1.2 WHERE trade_date=?',(BASE,))
        self.assertFalse(self.build()['plans'])

    def test_invalid_context_is_not_laundered_by_removing_old_combined_reasons(self):
        for change in ({'base_breadth':1.2},{'base_breadth':float('nan')},
                       {'base_return_1d':.9},{'base_return_5d':None},{'base_return_1d':-.05}):
            market={'base_breadth':.5,'base_return_1d':0,'base_return_5d':0,**change}
            self.assertIsNone(regime.classify(market)['route'])

    def test_rebound_slot_includes_existing_positions_and_funnel_is_cumulative(self):
        self.outlook['benchmark'].update(base_breadth=.42,base_return_1d=-.004,base_return_5d=-.01)
        blocked=self.build(occupied_symbols=('999999.SZ',),occupied_routes=(regime.REBOUND,))
        self.assertFalse(blocked['plans'])
        value=self.build()['coverage']['decision_funnel']
        keys=('regime_context_passed','sector_and_leadership_passed','price_action_structure_passed',
              'entry_envelope_passed','all_selection_conditions_passed','cash_and_lot_allocated')
        values=[value.get(key,0) for key in keys]
        self.assertEqual(values,sorted(values,reverse=True))

    def test_overheated_market_cannot_authorize_full_risk_two_day_breakout_chasing(self):
        self.outlook['benchmark'].update(base_breadth=.9,base_return_1d=.03,base_return_5d=.08)
        context=regime.classify(self.outlook['benchmark'])
        self.assertEqual(context['state'],'overheated_defensive')
        self.assertEqual(context['route'],regime.REBOUND)
        self.assertFalse(self.build()['plans'])  # Fixture is a compact breakout, not a pullback.
        evaluations=self.build()['candidate_evaluations']
        self.assertTrue(any('overheated_market_requires_pullback_recovery_no_breakout_chase' in row['reasons'] for row in evaluations))

    def test_normal_strength_remains_a_two_day_continuation_candidate(self):
        self.assertEqual(regime.classify({'base_breadth':.7,'base_return_1d':.015,'base_return_5d':.05})['route'],regime.TREND)


class FrozenHoldingTests(unittest.TestCase):
    def setUp(self):
        self.db=db_fixture();self.addCleanup(self.db.close)
        self.db.execute('UPDATE daily_quotes SET open=10.05,close=10.1,high=10.6,low=9.7 WHERE trade_date=?',(D2,))
        self.db.execute('INSERT INTO daily_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('000001.SZ',D3,10.1,10.6,9.5,10.15,1000,0,D3+'T16:00:00+08:00',D3+'T15:00:00+08:00','post_close_provider'))

    def entered(self, frozen=None):
        return book.evaluate(plan() if frozen is None else frozen,entry_evidence(),self.db,D1+'T17:00:00+08:00')

    def test_purchase_day_never_sells_even_if_bars_cross_the_target(self):
        data=entry_evidence();minute(data,'14:55')['high']=10.5
        value=book.evaluate(plan(),data,self.db,D1+'T17:00:00+08:00')
        self.assertEqual(value['status'],'pending_exit')
        self.assertNotIn('reference_sell_price',value)

    def test_failed_first_sell_day_exits_instead_of_extending_a_loss(self):
        value=book.evaluate(plan(),evidence(D2,9.9),self.db,D2+'T17:00:00+08:00',self.entered())
        self.assertEqual(value['status'],'reference_closed')
        self.assertEqual(value['reference_exit_at'],D2+'T14:50:00+08:00')
        self.assertLess(value['gross_return'],0)
        self.assertFalse(value['first_sell_continuation_decision']['continued'])

    def test_strong_first_sell_day_can_carry_only_until_frozen_second_deadline(self):
        first=book.evaluate(plan(),evidence(D2,10.05),self.db,D2+'T17:00:00+08:00',self.entered())
        self.assertEqual(first['status'],'pending_exit')
        self.assertTrue(first['completed_hold_sessions'][D2]['full_session_monitored'])
        # The persisted first day is sufficient; never refetch/fabricate missing old bars.
        final=book.evaluate(plan(),evidence(D3,10.1),self.db,D3+'T17:00:00+08:00',first)
        self.assertEqual(final['status'],'reference_closed')
        self.assertEqual(final['reference_exit_at'],D3+'T14:50:00+08:00')
        self.assertEqual(final['holding_sessions_after_entry'],2)
        self.assertTrue(final['first_sell_continuation_decision']['continued'])

    def test_rebound_requires_reclaim_and_never_uses_second_day_authorization(self):
        p=plan(regime.REBOUND)
        bad=self.entered(p)
        self.assertEqual(bad['status'],'no_signal')  # Has not broken the first 30-minute high.
        data=entry_evidence();minute(data,'14:40').update(close=10.045,high=10.05,low=10.)
        first=book.evaluate(p,data,self.db,D1+'T17:00:00+08:00')
        self.assertEqual(first['status'],'pending_exit')
        final=book.evaluate(p,evidence(D2,10.05),self.db,D2+'T17:00:00+08:00',first)
        self.assertEqual(final['reference_exit_at'],D2+'T14:50:00+08:00')
        self.assertEqual(final['holding_sessions_after_entry'],1)

    def test_late_entry_evidence_cannot_be_backfilled_before_the_later_exit_deadline(self):
        data=entry_evidence();data['000001.SZ']['observed_at']=D2+'T10:00:00+08:00'
        value=book.evaluate(plan(),data,self.db,D2+'T17:00:00+08:00')
        self.assertEqual(value['status'],'unknown_entry')

    def test_future_final_daily_close_is_not_used_for_1450_continuation(self):
        prior=self.entered();data=evidence(D2,10.05)
        self.db.execute('UPDATE daily_quotes SET close=9.99 WHERE trade_date=?',(D2,))
        for bar in data['000001.SZ']['bars']:
            if bar['end_at']>D2+'T14:50:00+08:00':
                bar.update(close=9.99,low=9.97)
        value=book.evaluate(plan(),data,self.db,D2+'T17:00:00+08:00',prior)
        self.assertEqual(value['status'],'pending_exit')
        self.assertTrue(value['first_sell_continuation_decision']['continued'])
        self.assertFalse(value['first_sell_continuation_decision']['uses_final_daily_close'])

    def test_stops_still_apply_after_first_day_extension_decision(self):
        data=evidence(D2,10.05);minute(data,'14:56').update(low=9.8,close=9.9)
        value=book.evaluate(plan(),data,self.db,D2+'T17:00:00+08:00',self.entered())
        self.assertEqual(value['status'],'reference_closed')
        self.assertEqual(value['reference_exit_at'],D2+'T14:56:00+08:00')
        self.assertEqual(value['reason'],'continued_session_stop_next_open')

    def test_missing_first_day_monitor_or_unfillable_final_minute_stays_unknown(self):
        data=evidence(D2,10.05);data['000001.SZ']['bars'].pop(-1)
        value=book.evaluate(plan(),data,self.db,D2+'T17:00:00+08:00',self.entered())
        self.assertEqual(value['status'],'unknown_exit')
        self.assertGreater(book.account([value])['reserved_cash_cny'],0)
        first=book.evaluate(plan(),evidence(D2,10.05),self.db,D2+'T17:00:00+08:00',self.entered())
        end=evidence(D3,10.1);minute(end,'14:51')['volume']=0
        final=book.evaluate(plan(),end,self.db,D3+'T17:00:00+08:00',first)
        self.assertEqual(final['status'],'unknown_exit')

    def test_deadline_and_route_cannot_change_after_entry(self):
        entered=self.entered()
        for update in ({'planned_exit_trade_date':'2026-10-22'}, {'max_hold_after_entry_sessions':3},
                       {'setup_route':regime.REBOUND,'max_hold_after_entry_sessions':1,'planned_exit_trade_date':D2}):
            with self.assertRaises(ValueError):
                book.evaluate({**plan(),**update},evidence(D2,10.05),self.db,D2+'T17:00:00+08:00',entered)

    def test_unfillable_weak_day_decision_cannot_be_revised_into_two_day_hold(self):
        data=evidence(D2,9.9);minute(data,'14:51')['volume']=0
        first=book.evaluate(plan(),data,self.db,D2+'T17:00:00+08:00',self.entered())
        self.assertEqual(first['status'],'unknown_exit')
        self.assertFalse(first['first_sell_continuation_decision']['continued'])
        changed=book.evaluate(plan(),evidence(D2,10.05),self.db,D2+'T18:00:00+08:00',first)
        self.assertEqual(changed['status'],'unknown_exit')
        self.assertEqual(changed['reason'],'previously_frozen_continuation_prefix_changed')
        self.assertFalse(changed['first_sell_continuation_decision']['continued'])

    def test_old_trend_day_without_close_monitoring_cannot_skip_to_a_later_winner(self):
        first=book.evaluate(plan(),evidence(D3,10.5),self.db,D3+'T17:00:00+08:00',self.entered())
        self.assertEqual(first['status'],'unknown_exit')
        self.assertNotIn('reference_sell_price',first)

    def test_live_reclaim_is_not_just_a_bounce_above_a_low_open(self):
        from test_active_trade_book import live_inputs
        rows,quote=live_inputs()
        for row in rows:
            for key in ('open','high','low','close'):
                row[key]-=.1
        result=book.review_entry(plan(),rows,quote,D1+'T14:40:30+08:00')
        self.assertFalse(result['conditions_met'])
        rows,quote=live_inputs();quote.update(last=9.99,ask1=10.)
        self.assertFalse(book.review_entry(plan(),rows,quote,D1+'T14:40:30+08:00')['conditions_met'])

    def test_stop_has_priority_on_a_bar_that_also_touches_target(self):
        data=evidence(D2,10.05);minute(data,'10:00').update(low=9.8,high=10.4)
        value=book.evaluate(plan(),data,self.db,D2+'T17:00:00+08:00',self.entered())
        self.assertEqual(value['reason'],'completed_minute_stop_next_open')
        self.assertTrue(value['same_bar_stop_and_target'])
        self.assertEqual(value['reference_exit_at'],D2+'T10:00:00+08:00')

    def test_known_v2_migration_preserves_old_exit_date_and_cash(self):
        old={'schema':book.ACTIVE_SCHEMA,'protocol_sha256':active.PREVIOUS_PROTOCOL_SHA256,
             'target_trade_date':D1,'generated_at':old_plan()['generated_at'],'plans':[old_plan()]}
        previous=book.roll(old,True,{},self.db,D1+'T08:00:00+08:00',active.PREVIOUS_PROTOCOL_SHA256)
        old['reference_book']=previous
        migrated=book.roll(old,True,{},self.db,D1+'T08:01:00+08:00',active.PROTOCOL_SHA256)
        self.assertEqual(migrated['records'],previous['records'])
        self.assertEqual(migrated['account'],previous['account'])
        self.assertEqual(migrated['records'][0]['exit_date'],D2)


class PresentationTests(unittest.TestCase):
    def test_real_legacy_policy_hashes_stay_exact_and_v2_renderer_is_unchanged(self):
        self.assertEqual(active.PREVIOUS_PROTOCOL_SHA256,'0193d67387c95cda520fe038811be682380dbfea960b89409149de0ceca62846')
        from test_active_trade_report import study
        value=study();value['selection_rule_version']=2
        import hashlib
        self.assertEqual(hashlib.sha256(report.render(value).encode()).hexdigest(),'b4f507af41e787cd1ec4a97e8159513af8b97ea7530c48af7062e2931c5e1002')

    def test_v3_validation_never_reuses_legacy_wins(self):
        old={'status':'reference_closed','gross_return':.04,'exit_ge3':True}
        unknown={'holding_policy_version':3,'setup_route':regime.TREND,'status':'unknown_exit'}
        closed={'holding_policy_version':3,'setup_route':regime.REBOUND,'status':'reference_closed','gross_return':.01}
        stats=book.v3_validation_stats([old,unknown,closed])
        self.assertEqual(stats['routes'][regime.TREND]['n_closed'],0)
        self.assertEqual(stats['routes'][regime.TREND]['n_unknown'],1)
        self.assertIsNone(stats['routes'][regime.TREND]['reference_gross_profit_rate'])
        self.assertEqual(stats['routes'][regime.REBOUND]['reference_gross_profit_rate'],1)
        self.assertEqual(stats['routes'][regime.REBOUND]['reference_exit_ge3_rate'],0)
        self.assertFalse(stats['historical_legacy_results_included'])

    def test_v3_full_compact_html_and_explicit_holding_information_match(self):
        from test_active_trade_report import study
        value=study();value.update(selection_rule_version=3,plans=[plan()],
            market_context=regime.classify({'base_breadth':.5,'base_return_1d':0,'base_return_5d':0}),
            v3_validation_stats=book.v3_validation_stats([]))
        compact=report.compact(value)
        self.assertEqual(report.render(value),report.render(compact))
        self.assertEqual(compact,report.compact(compact))
        self.assertIn(D3,report.render(value))
        self.assertIn('旧规则交易不混入',report.render(value))


if __name__ == '__main__':
    unittest.main()
