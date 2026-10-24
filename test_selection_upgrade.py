"""V2 selection assumptions, risk exclusions and lossless account migration."""
import copy
import datetime as dt
import sqlite3
import unittest
from unittest import mock

import active_price_action as pa
import active_trade_book as book
import active_trade_system as active
import market_outlook
import security_selection as security
from price_action_snapshot import price_action_snapshot
from test_active_trade_system import fixtures, BASE, TARGET, CUTOFF, OBSERVED
from test_active_trade_book import plan, D1
from test_nextday_forward import db_fixture


class SecuritySelectionTests(unittest.TestCase):
    def setUp(self):
        self.metadata={'name':'普通股票','last_seen_at':OBSERVED,'board':'主板'}

    def reasons(self,symbol='000001.SZ',metadata=None):
        return security.rejection_reasons(symbol,self.metadata if metadata is None else metadata,CUTOFF,base=BASE)

    def test_all_chinext_prefixes_and_board_alias_are_excluded(self):
        for symbol in ('300001.SZ','301001.SZ','309799.SZ'):
            self.assertIn('excluded_chinext_board',self.reasons(symbol))
        other=copy.deepcopy(self.metadata);other['board']='ChiNext'
        self.assertIn('excluded_chinext_board',self.reasons(metadata=other))
        # UTC midnight boundary still belongs to BASE in Beijing.
        self.assertEqual(self.reasons(metadata={**self.metadata,'last_seen_at':'2026-10-15T16:30:00+00:00'}),[])
        for symbol in ('688001.SH','000001.SZ','600001.SH'):
            self.assertEqual(self.reasons(symbol),[])

    def test_ST_unicode_whitespace_delisting_and_unknown_metadata(self):
        for name in ('ST普通','*ST普通','Ｓ Ｔ普通','* ＳＴ 普通','普通退'):
            other={**self.metadata,'name':name}
            self.assertIn('excluded_ST_or_delisting_name',self.reasons(metadata=other))
        for other in ({},{**self.metadata,'name':' '},{**self.metadata,'last_seen_at':None},
                      {**self.metadata,'last_seen_at':TARGET+'T09:00:00+08:00'},
                      {**self.metadata,'last_seen_at':'2026-09-01T12:00:00+08:00'}):
            self.assertIn('security_name_or_asof_metadata_unknown',self.reasons(metadata=other))

    def test_known_official_risk_overrides_ordinary_master_name_without_future_leakage(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        db.execute('CREATE TABLE security_master(symbol TEXT,name TEXT,last_seen_at TEXT)')
        db.execute('INSERT INTO security_master VALUES(?,?,?)',('000001.SZ','普通股票',OBSERVED))
        db.execute('CREATE TABLE security_status_daily(symbol TEXT,trade_date TEXT,observed_at TEXT,is_st INTEGER,delisting INTEGER,trading_status TEXT,verified INTEGER,published_at TEXT)')
        db.execute('INSERT INTO security_status_daily VALUES(?,?,?,?,?,?,?,?)',
                   ('000001.SZ',BASE,OBSERVED,1,0,'normal',1,None))
        meta=security.load_metadata(db,['000001.SZ'],base=BASE,cutoff=CUTOFF)['000001.SZ']
        self.assertIn('excluded_known_official_ST_or_delisting_status',self.reasons(metadata=meta))
        db.execute('UPDATE security_status_daily SET observed_at=?',(TARGET+'T09:00:00+08:00',))
        meta=security.load_metadata(db,['000001.SZ'],base=BASE,cutoff=CUTOFF)['000001.SZ']
        self.assertEqual(self.reasons(metadata=meta),[])


class PriceStructureTests(unittest.TestCase):
    def setUp(self):
        db,*_=fixtures();self.addCleanup(db.close)
        db.row_factory=sqlite3.Row
        self.rows=[dict(row) for row in db.execute('SELECT * FROM daily_quotes WHERE symbol=? ORDER BY trade_date',('000001.SZ',))]

    def snapshot(self,rows=None,cap=10.02):
        rows=self.rows if rows is None else rows
        f=price_action_snapshot(rows)
        return pa.snapshot(rows,f,cap)

    def test_compact_breakout_is_explicit_and_order_independent(self):
        result=self.snapshot()
        self.assertIn('compact_breakout',result['patterns'])
        self.assertEqual(result,self.snapshot(list(reversed(self.rows))))
        self.assertFalse(result['score_is_probability'])
        self.assertFalse(result['no_identified_resistance_is_guaranteed_room'])

    def test_recovery_requires_a_prior_pullback_and_reduced_volume(self):
        rows=copy.deepcopy(self.rows)
        for row in rows[-4:-1]:
            row['open']=row['close']+.002
            row['volume']*=.7
        result=self.snapshot(rows)
        self.assertIn('reduced_volume_pullback_recovery',result['patterns'])
        for row in rows[-4:-1]:row['volume']*=3
        self.assertNotIn('reduced_volume_pullback_recovery',self.snapshot(rows)['patterns'])

    def test_long_upper_wick_and_no_price_recovery_do_not_qualify(self):
        rows=copy.deepcopy(self.rows);rows[-1]['high']=10.3
        self.assertIn('active_upper_wick_exceeds_half_ATR',self.snapshot(rows)['reasons'])
        rows=copy.deepcopy(self.rows);rows[-2]['high']=10.01
        self.assertIn('no_recovery_or_compact_breakout_structure',self.snapshot(rows)['reasons'])

    def test_resistance_between_close_and_possible_cap_is_not_silently_treated_as_broken(self):
        rows=copy.deepcopy(self.rows);rows[-10]['high']=10.1
        result=self.snapshot(rows,cap=10.2)
        self.assertEqual(result['confirmed_overhead_resistance'],10.1)
        self.assertIn('confirmed_resistance_before_3percent_target_plus_buffer',result['reasons'])
        rows[-10]['high']=10.8
        self.assertNotIn('confirmed_resistance_before_3percent_target_plus_buffer',self.snapshot(rows,cap=10.2)['reasons'])

    def test_zero_atr_null_ratios_and_future_asof_mismatch_are_unavailable(self):
        rows=copy.deepcopy(self.rows)
        for row in rows:row.update(open=10,high=10,low=10,close=10)
        self.assertFalse(self.snapshot(rows)['available'])
        rows[-1].update(open=10,high=10.1,low=10,close=10.05)
        self.assertFalse(self.snapshot(rows)['available'])
        f=price_action_snapshot(self.rows)
        future=copy.deepcopy(self.rows[-1]);future['trade_date']=TARGET
        result=pa.snapshot(self.rows+[future],f,10.02)
        self.assertFalse(result['available'])


class ActiveBuildEdgeTests(unittest.TestCase):
    def setUp(self):
        self.db,self.outlook,self.opening,self.flow=fixtures();self.addCleanup(self.db.close)
        clock=mock.patch('nextday_cohort._now',return_value=dt.datetime.fromisoformat('2026-10-20T15:00:00+08:00'))
        clock.start();self.addCleanup(clock.stop)

    def build(self):
        return active.build(self.db,BASE,TARGET,CUTOFF,self.outlook,self.opening,self.flow)

    def test_zero_atr_candidate_cannot_abort_the_whole_report(self):
        self.db.execute('UPDATE daily_quotes SET open=10,high=10,low=10,close=10 WHERE symbol=?',('000001.SZ',))
        result=self.build()
        row=next(item for item in result['candidate_evaluations'] if item['symbol']=='000001.SZ')
        self.assertIn('active_price_structure_numeric_features_unavailable',row['reasons'])
        self.assertFalse(row['eligible'])
        self.assertTrue(result['system_enabled'])

    def test_forged_normal_pool_name_cannot_bypass_master_risk_status(self):
        self.db.execute('UPDATE security_master SET name=? WHERE symbol=?',('Ｓ Ｔ风险','000001.SZ'))
        result=self.build()
        row=next(item for item in result['candidate_evaluations'] if item['symbol']=='000001.SZ')
        self.assertIn('excluded_ST_or_delisting_name',row['reasons'])
        self.assertFalse(row['eligible'])
        self.assertTrue(all(item['symbol']!='000001.SZ' for item in result['plans']))


class UpgradeAccountTests(unittest.TestCase):
    def setUp(self):
        self.db=db_fixture();self.addCleanup(self.db.close)

    def study(self,plans):
        return {'schema':book.ACTIVE_SCHEMA,'protocol_sha256':active.LEGACY_PROTOCOL_SHA256,
                'target_trade_date':D1,'generated_at':plan()['generated_at'],'plans':plans}

    def test_known_v1_migration_preserves_plan_account_and_settlement(self):
        previous=self.study([plan()])
        old=book.roll(previous,True,{},self.db,D1+'T08:00:00+08:00',active.LEGACY_PROTOCOL_SHA256)
        previous['reference_book']=old
        migrated=book.roll(previous,True,{},self.db,D1+'T08:01:00+08:00',active.PROTOCOL_SHA256)
        self.assertEqual(migrated['frozen_plan_dates'],old['frozen_plan_dates'])
        self.assertEqual(migrated['records'],old['records'])
        self.assertEqual(migrated['account'],old['account'])
        self.assertEqual(migrated['frozen_date_policy_ids'][D1],active.LEGACY_PROTOCOL_SHA256)
        self.assertFalse(migrated['selection_upgrade_migration']['account_reset'])
        self.assertTrue(migrated['selection_upgrade_migration']['legacy_entry_exit_and_risk_rules_unchanged'])
        # Migrating cannot turn a later source into a fresh account or new plan.
        rerun={**previous,'protocol_sha256':active.PROTOCOL_SHA256,'reference_book':migrated}
        again=book.roll(rerun,True,{},self.db,D1+'T08:02:00+08:00',active.PROTOCOL_SHA256)
        self.assertEqual(again['records'],old['records'])

    def test_empty_first_decision_blocks_same_day_v2_replacement(self):
        previous=self.study([])
        ledger=book.roll(previous,True,{},self.db,D1+'T08:00:00+08:00',active.PROTOCOL_SHA256)
        new={'plans':[plan()],'selection_rule_version':2,'protocol_sha256':active.PROTOCOL_SHA256,'coverage':{'conditional_plan_count':1}}
        frozen=market_outlook._preserve_first_published_active_decision(new,ledger,D1)
        self.assertEqual(frozen['plans'],[])
        self.assertEqual(new['plans'],[])
        self.assertEqual(new['upgrade_preview']['plans'],[plan()])
        self.assertFalse(new['upgrade_preview']['eligible_for_execution'])
        self.assertEqual(new['plan_policy_id'],active.LEGACY_PROTOCOL_SHA256)
        self.assertEqual(new['selection_upgrade_effective'],'next_unfrozen_target_session')
        from active_trade_report import compact,render
        new.update(schema=book.ACTIVE_SCHEMA,status='cash_no_qualifying_setup')
        self.assertEqual(compact(new),compact(compact(new)))
        self.assertEqual(render(new),render(compact(new)))
        self.assertIn('新版仅研究预览',render(new))

    def test_unknown_legacy_position_reserves_cash_through_selection_upgrade(self):
        previous=self.study([plan()])
        old=book.roll(previous,True,{},self.db,'2026-10-20T15:30:00+08:00',active.LEGACY_PROTOCOL_SHA256)
        self.assertEqual(old['records'][0]['status'],'unknown_entry')
        previous['reference_book']=old
        migrated=book.roll(previous,True,{},self.db,'2026-10-20T15:31:00+08:00',active.PROTOCOL_SHA256)
        self.assertEqual(migrated['account'],old['account'])
        self.assertEqual(migrated['occupied_symbols'],old['occupied_symbols'])
        self.assertEqual(migrated['occupied_sectors'],old['occupied_sectors'])
        self.assertEqual(migrated['account']['reserved_cash_cny'],3015)

    def test_unknown_policy_change_remains_a_hard_failure(self):
        previous=self.study([]);previous['protocol_sha256']='unrecognized-policy'
        with self.assertRaises(ValueError):
            book.roll(previous,True,{},self.db,D1+'T08:00:00+08:00',active.PROTOCOL_SHA256)

    def test_manual_entry_risk_filter_cannot_be_bypassed_with_an_old_or_handwritten_plan(self):
        for symbol,name in (('300001.SZ','普通股票'),('309799.SZ','普通股票'),('000001.SZ','Ｓ Ｔ风险')):
            candidate={**plan(),'symbol':symbol,'name':name}
            result=book.review_entry(candidate,[],{},D1+'T14:40:30+08:00')
            self.assertIn('excluded_chinext_ST_or_delisting_name',result['reasons'])


if __name__=='__main__':unittest.main()
