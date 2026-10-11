"""Synthetic boundary/persistence fixtures only, never model acceptance samples."""
import copy
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
import formal_price_model as model
import model_store


def bars(n=330):
    return [{'trade_date':(dt.date(2020,1,1)+dt.timedelta(days=i)).isoformat(),
             'open':6+i*.001,'close':6+i*.001,'high':6.2+i*.001,'low':5.8+i*.001,
             'volume':100000+i,'conflict':0} for i in range(n)]


class FeatureTests(unittest.TestCase):
    def test_future_price_cannot_change_prior_features(self):
        rows=bars(); index={r['trade_date']:i for i,r in enumerate(rows)}
        first=model.features(rows,'fixture',index)
        changed=copy.deepcopy(rows)
        changed[-1].update(close=8,high=8.2)
        second=model.features(changed,'fixture',index)
        pd.testing.assert_frame_equal(first.iloc[:-1][model.FEATURES],second.iloc[:-1][model.FEATURES])
        self.assertNotEqual(first.iloc[-2].forward_return,second.iloc[-2].forward_return)

    def test_250_bars_required_at_each_historical_input_date(self):
        rows=bars(); result=model.features(rows,'fixture',{r['trade_date']:i for i,r in enumerate(rows)})
        self.assertEqual(result.date.min(),rows[249]['trade_date'])
        self.assertTrue((result.history_bars>=250).all())

    def test_missing_market_session_rejects_window_and_label(self):
        rows=bars(); index={r['trade_date']:i for i,r in enumerate(rows)}
        missing=rows[280]['trade_date']
        result=model.features(rows[:280]+rows[281:],'fixture',index)
        self.assertNotIn(rows[-1]['trade_date'],set(result.date))
        self.assertFalse(result[result.date==rows[279]['trade_date']].iloc[0].label_valid)
        self.assertNotIn(missing,set(result.date))

    def test_conflicting_or_zero_volume_outcome_not_labeled(self):
        for key,value in [('conflict',1),('volume',0)]:
            rows=bars();rows[-1][key]=value
            result=model.features(rows,'fixture',{r['trade_date']:i for i,r in enumerate(rows)})
            self.assertFalse(result.iloc[-1].label_valid)
            self.assertNotIn(rows[-1]['trade_date'],set(result.date))

    def test_latest_unrevealed_outcome_never_is_a_negative_label(self):
        rows=bars();result=model.features(rows,'fixture',{r['trade_date']:i for i,r in enumerate(rows)})
        self.assertFalse(result.iloc[-1].label_valid)
        self.assertTrue(pd.isna(result.iloc[-1].outcome_date))


class SplitTests(unittest.TestCase):
    def test_boundary_labels_purged_and_test_after_calibration(self):
        dates=[(dt.date(2020,1,1)+dt.timedelta(days=i)).isoformat() for i in range(301)]
        frame=pd.DataFrame([{'date':day,'outcome_date':dates[i+1],'symbol':str(s)} for i,day in enumerate(dates[:-1]) for s in range(20)])
        train,cal,test=model.split(frame)
        self.assertLess(train.outcome_date.max(),cal.date.min())
        self.assertLess(cal.outcome_date.max(),test.date.min())
        self.assertLess(len(train)+len(cal)+len(test),len(frame))

    def test_insufficient_independent_dates_fail(self):
        with self.assertRaisesRegex(ValueError,'150'):
            model.split(pd.DataFrame({'date':['2020-01-01']*10000}))


class StoreTests(unittest.TestCase):
    def test_prediction_sqlite_contains_only_new_generation(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'fixture.sqlite3'
            metadata={'model_id':'fixture','base_trade_date':'2026-10-09','target_trade_date':'2026-10-12',
                      'generated_at':'2026-10-11T13:00:00+08:00','generated_before_open':True}
            rows=[{'symbol':'fixture','name':'fixture','close':6.,'P1':.5,'P2':.1,'rank':1}]
            model.write_prediction_db(path,rows,metadata)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('PRAGMA application_id').fetchone()[0],model.PREDICTION_APP_ID)
                self.assertEqual(db.execute('SELECT COUNT(*),MAX(formal_trade_ready) FROM model_predictions').fetchone(),(1,0))

    def test_report_refuses_wrong_data_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d,'model_restore.json').write_text(json.dumps({'verified':True,'manifest':
                {'snapshot_release':'old','input_database_sha256':'oldhash'}}))
            with self.assertRaisesRegex(ValueError,'snapshots differ'):
                model_store.attach({'research':{}},d,{'release_tag':'new','database':{'sha256':'newhash'}})

    def test_publish_refuses_source_advanced_during_training(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d,'predictions.json').write_text(json.dumps({'snapshot_release':'old','input_database_sha256':'oldhash'}))
            with patch('model_store.pointer',return_value=(None,None)),patch('model_store.data_pointer',return_value=(
                {'release_tag':'new','database':{'sha256':'newhash'}},'blob')),patch('model_store.run') as run:
                with self.assertRaisesRegex(ValueError,'advanced during training'): model_store.publish('fixture/repo',d,'fixture')
                run.assert_not_called()


if __name__=='__main__': unittest.main()
