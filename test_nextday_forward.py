"""Synthetic prospective-ledger invariants; these are not market returns."""
import copy
import datetime as dt
import sqlite3
import unittest

import nextday_forward as f


def cohort():
    return {'schema': 'nextday-frozen-cohort-v1', 'cohort_id': 'frozen-cohort-fixture',
        'base_trade_date': '2026-10-16', 'target_trade_date': '2026-10-19',
        'D2_trade_date': '2026-10-20', 'generated_at': '2026-10-18T16:00:00+08:00',
        'forward_eligible': True, 'base_candidates': [{'symbol': '000001.SZ', 'name': '合成样本',
            'base_close': 10., 'frozen_entry_floor': 9.9, 'frozen_entry_cap': 10.25,
            'frozen_quantity_shares': 900, 'confluence_eligible': True}]}


def db_fixture():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE daily_quotes(symbol,trade_date,open,high,low,close,volume,conflict,'
        'fetched_at,source_updated_at,finality)')
    for day, opening, close in [('2026-10-19', 10, 10.1), ('2026-10-20', 10.2, 10.4)]:
        db.execute('INSERT INTO daily_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('000001.SZ', day, opening, 10.6, 9.7, close, 1000, 0,
             day+'T16:00:00+08:00', day+'T15:00:00+08:00', 'post_close_provider'))
    return db


def evidence(day, price=10.):
    end = dt.datetime.fromisoformat(day+'T15:00:00+08:00')
    bars = []
    for stamp in f.expected_ends(day, end):
        finish = f.instant(stamp)
        bars.append({'start_at': (finish-dt.timedelta(minutes=1)).isoformat(), 'end_at': stamp,
            'open': price, 'high': price+.04, 'low': price-.02,
            'close': price+.02, 'volume': 100., 'amount': price*10000})
    return {'000001.SZ': {'symbol': '000001.SZ', 'observed_at': day+'T16:00:00+08:00',
        'raw_sha256': 'a'*64, 'source_url': 'https://example.invalid/synthetic', 'bars': bars}}


def arm(rows, name='base_0940'):
    return next(row for row in rows if row['arm'] == name)


class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.db = db_fixture(); self.addCleanup(self.db.close)

    def first(self, data=None):
        return f.evaluate(cohort(), evidence('2026-10-19') if data is None else data,
            self.db, '2026-10-19T17:00:00+08:00')

    def test_D1_observations_carried_to_D2_without_refetch_or_future_data(self):
        first = self.first()
        self.assertEqual(arm(first)['status'], 'pending_exit')
        final = f.evaluate(cohort(), evidence('2026-10-20', 10.3), self.db,
            '2026-10-20T17:00:00+08:00', first)
        row = arm(final)
        self.assertEqual(row['status'], 'reference_closed')
        self.assertAlmostEqual(row['gross_return'], .03)
        self.assertTrue(row['exit_ge3'])
        product = (1+row['D1_after_buy_return'])*(1+row['overnight_return'])*(1+row['D2_after_open_return'])
        self.assertAlmostEqual(product, 1+row['gross_return'])
        self.assertFalse(row['actual_fill_verified'])

    def test_D1_red_trade_not_deleted_and_D2_green_need_not_be_profit(self):
        self.db.execute("UPDATE daily_quotes SET close=9.8 WHERE trade_date='2026-10-19'")
        first = self.first(evidence('2026-10-19', 10.2))
        final = f.evaluate(cohort(), evidence('2026-10-20', 10.), self.db,
            '2026-10-20T17:00:00+08:00', first)
        row = arm(final)
        self.assertTrue(row['D2_close_up'])
        self.assertLess(row['gross_return'], 0)
        self.assertLess(row['D1_after_buy_return'], 0)

    def test_future_entry_is_pending_even_if_payload_contains_future_bars(self):
        rows = f.evaluate(cohort(), evidence('2026-10-19'), self.db, '2026-10-19T09:39:00+08:00')
        self.assertEqual(arm(rows)['status'], 'pending_entry')
        self.assertNotIn('reference_buy_price', arm(rows))

    def test_missing_minute_prefix_is_unknown_not_a_winner_or_silent_drop(self):
        data = evidence('2026-10-19'); data['000001.SZ']['bars'].pop(2)
        rows = self.first(data)
        self.assertEqual(len(rows), 6)
        self.assertEqual(arm(rows)['status'], 'unknown_entry')
        stats = f.summarize(rows)['base_0940']
        self.assertEqual(stats['unknown'], 1)
        self.assertIsNone(stats['reference_gross_profit_rate'])
        self.assertEqual(stats['reference_profit_fraction_including_unknown_as_not_won'], 0)

    def test_same_frozen_cohort_cannot_be_replaced_on_rerun(self):
        old = f.initialize(cohort(), '2026-10-16', '2026-10-19', 'a'*64, '2026-10-18T16:00:00+08:00')
        new = copy.deepcopy(old); new['current_cohort']['cohort_id'] = 'later-attempt'
        new['current_cohort']['base_candidates'] = []
        saved = f.advance(new, old, True, {}, self.db, '2026-10-18T17:00:00+08:00')
        self.assertEqual(saved['current_cohort']['cohort_id'], 'frozen-cohort-fixture')
        repeated = f.advance(new, saved, True, {}, self.db, '2026-10-18T17:01:00+08:00')
        self.assertEqual(len(repeated['tracked_cohorts']), 1)
        self.assertEqual(len(repeated['records']), 6)

    def test_late_publication_never_enters_prospective_registry(self):
        old = f.initialize(cohort(), '2026-10-16', '2026-10-19', 'a'*64, '2026-10-18T16:00:00+08:00')
        saved = f.advance(old, old, False, {}, self.db, '2026-10-18T17:00:00+08:00')
        self.assertEqual(saved['tracked_cohorts'], [])
        late = cohort(); late['generated_at'] = '2026-10-19T09:16:00+08:00'
        with self.assertRaises(ValueError):
            f.evaluate(late, {}, self.db, '2026-10-19T17:00:00+08:00')

    def test_unknown_D2_is_preserved_and_never_extended_to_D3(self):
        rows = f.evaluate(cohort(), {}, self.db, '2026-10-20T17:00:00+08:00', self.first())
        self.assertEqual(arm(rows)['status'], 'unknown_exit')
        self.assertEqual(arm(rows)['exit_date'], '2026-10-20')
        self.assertIsNone(arm(rows)['gross_return'])

    def test_D1_missing_entry_cannot_be_backfilled_after_seeing_D2(self):
        first=self.first({})
        late=evidence('2026-10-20',10.3)
        late['000001.SZ']['bars']=evidence('2026-10-19')['000001.SZ']['bars']+late['000001.SZ']['bars']
        rows=f.evaluate(cohort(),late,self.db,'2026-10-20T17:00:00+08:00',first)
        self.assertEqual(arm(rows)['status'],'unknown_entry')
        self.assertIsNone(arm(rows)['gross_return'])

    def test_late_exit_label_can_resolve_only_the_original_frozen_D2_time(self):
        unknown=f.evaluate(cohort(),{},self.db,'2026-10-20T17:00:00+08:00',self.first())
        data=evidence('2026-10-20',10.3)
        data['000001.SZ']['observed_at']='2026-10-21T17:00:00+08:00'
        rows=f.evaluate(cohort(),data,self.db,'2026-10-21T17:00:00+08:00',unknown)
        self.assertEqual(arm(rows)['status'],'reference_closed')
        self.assertEqual(arm(rows)['exit_date'],'2026-10-20')
        self.assertIn('2026-10-20T14:50',arm(rows)['exit_reference_at'])

    def test_outside_frozen_price_is_cancelled_without_replacement(self):
        rows = self.first(evidence('2026-10-19', 10.3))
        self.assertEqual(arm(rows)['status'], 'price_outside_bounds')
        self.assertNotIn('reference_buy_price', arm(rows))

    def test_one_price_day_and_zero_volume_minute_unresolved(self):
        self.db.execute("UPDATE daily_quotes SET open=10,high=10,low=10,close=10 WHERE trade_date='2026-10-19'")
        self.assertEqual(arm(self.first())['status'], 'unknown_entry')

    def test_bad_signal_prices_cannot_become_a_clean_no_signal_exclusion(self):
        rows = self.first(evidence('2026-10-19', 20.))
        self.assertEqual(arm(rows, 'confluence_opening_retest')['status'], 'unknown_entry')

    def test_zero_volume_entry_or_exit_is_unknown(self):
        data=evidence('2026-10-19')
        bar=next(row for row in data['000001.SZ']['bars'] if row['end_at'].endswith('09:41:00+08:00'))
        bar.update(volume=0.,amount=0.)
        self.assertEqual(arm(self.first(data))['status'],'unknown_entry')
        second=evidence('2026-10-20',10.3)
        bar=next(row for row in second['000001.SZ']['bars'] if row['end_at'].endswith('14:51:00+08:00'))
        bar.update(volume=0.,amount=0.)
        rows=f.evaluate(cohort(),second,self.db,'2026-10-20T17:00:00+08:00',self.first())
        self.assertEqual(arm(rows)['status'],'unknown_exit')

    def test_paired_timing_retains_unmatched_observations(self):
        first=self.first()
        second=evidence('2026-10-20',10.3)
        rows=f.evaluate(cohort(),second,self.db,'2026-10-20T17:00:00+08:00',first)
        pair=f.paired_timing(rows)['base']
        self.assertEqual(pair['both_closed'],1)
        self.assertAlmostEqual(pair['mean_return_1440_minus_0940'],0)
        arm(rows,'base_1440').update(status='unknown_exit',gross_return=None)
        pair=f.paired_timing(rows)['base']
        self.assertEqual(pair['both_closed'],0)
        self.assertEqual(pair['unmatched_or_pending'],1)

    def test_uncertainty_uses_distinct_dates_and_never_activates_rules(self):
        records=[]
        for i in range(40):
            day=(dt.date(2026,1,1)+dt.timedelta(days=i//2)).isoformat()
            value=.03 if i%3 else -.04
            records.append({'arm':'base_0940','status':'reference_closed','entry_date':day,
                'gross_return':value,'exit_ge3':value>=.03,'exit_ge6':False})
        result=f.summarize(records)['base_0940']
        self.assertEqual(result['distinct_entry_dates'],20)
        self.assertIn('moving5',result['bootstrap_interval']['method'])
        self.assertFalse(result['formal_trade_ready'])
        self.assertFalse(result['bootstrap_interval']['is_finite_sample_guarantee'])
        for row in records:row['entry_date']='2026-01-01'
        self.assertIsNone(f.summarize(records)['base_0940']['bootstrap_interval'])

    def test_confluence_is_a_mask_of_same_cohort_not_a_new_stock(self):
        item = cohort(); item['base_candidates'][0]['confluence_eligible'] = False
        rows = f.evaluate(item, evidence('2026-10-19'), self.db, '2026-10-19T17:00:00+08:00')
        self.assertEqual(arm(rows)['status'], 'pending_exit')
        self.assertEqual(arm(rows, 'confluence_0940')['status'], 'excluded_filter')
        self.assertEqual(len({row['symbol'] for row in rows}), 1)

    def test_trigger_cannot_use_the_entry_minute_to_confirm_itself(self):
        data = evidence('2026-10-19')
        bar = next(row for row in data['000001.SZ']['bars'] if row['end_at'].endswith('09:41:00+08:00'))
        bar.update(open=10.01, high=10.5, close=10.49)
        rows = self.first(data)
        self.assertEqual(arm(rows, 'confluence_opening_retest')['status'], 'no_signal')

    def test_protocol_changes_not_pooled_and_saved_bounds_cannot_change(self):
        old = f.initialize(cohort(), '2026-10-16', '2026-10-19', 'a'*64, '2026-10-18T16:00:00+08:00')
        old['protocol_sha256'] = 'old-protocol'
        saved = f.advance(old, old, True, {}, self.db, '2026-10-18T17:00:00+08:00')
        self.assertTrue(saved['previous_protocol_not_pooled'])
        first = self.first(); arm(first)['frozen_entry_cap'] = 99
        with self.assertRaises(ValueError):
            f.evaluate(cohort(), {}, self.db, '2026-10-20T17:00:00+08:00', first)

    def test_no_future_sample_means_no_probability_or_confidence_claim(self):
        for stats in f.summarize([]).values():
            self.assertIsNone(stats['actual_win_rate'])
            self.assertIsNone(stats['reference_gross_profit_rate'])
            self.assertIsNone(stats['bootstrap_interval'])
            self.assertFalse(stats['execution_win_rate_80_validated'])


if __name__ == '__main__': unittest.main()
