"""Meaningful leakage, endpoint-support and portability checks for probabilities."""
import io
import unittest

import joblib
import numpy as np
import pandas as pd

import probabilistic_models as m


TARGETS = {'p_win3': 'label_profit3', 'p_ge3': 'label_ge3',
           'p_ge6': 'label_ge6', 'p_strength3': 'strength3_label'}


def data(first, days, seed=7):
    rng = np.random.default_rng(seed)
    n = days * 20
    dates = np.repeat(pd.date_range(first, periods=days).strftime('%Y-%m-%d'), 20)
    x = rng.normal(size=n)
    u = rng.uniform(size=n)
    return pd.DataFrame({'date': dates, 'x': x, 'missing': np.nan,
        'label_profit3': (u < 1/(1+np.exp(-1.8*x))).astype(float),
        'label_ge3': (u < .35/(1+np.exp(-x))).astype(float),
        'label_ge6': (u < .25/(1+np.exp(-x))).astype(float),
        'strength3_label': (u < .3/(1+np.exp(-x))).astype(float)})


class ProbabilityModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.train = data('2025-01-01', 30)
        cls.calibration = data('2025-03-01', 20, 42)
        cls.bundle = m.fit_bundle(cls.train, cls.calibration, ['x', 'missing'], TARGETS, date_column='date')

    def test_equal_date_mass_despite_cross_section_size(self):
        dates = ['a']*2 + ['b']*20 + ['c']*3
        weights = m.date_balanced_weights(dates)
        self.assertAlmostEqual(float(weights.mean()), 1)
        masses = [weights[np.asarray(dates) == day].sum() for day in ['a', 'b', 'c']]
        np.testing.assert_allclose(masses, [25/3]*3)

    def test_rejects_chronology_overlap_and_target_feature(self):
        with self.assertRaisesRegex(ValueError, 'strictly later'):
            m.fit_bundle(self.train, self.train, ['x'], TARGETS, date_column='date')
        with self.assertRaisesRegex(ValueError, 'labels'):
            m.fit_bundle(self.train, self.calibration, ['label_profit3'], TARGETS, date_column='date')

    def test_probabilities_learn_direction_but_preserve_nested_endpoint(self):
        result = m.predict_bundle(self.bundle, pd.DataFrame({'x': [-2., 2.], 'missing': [np.nan, np.nan]}))
        self.assertEqual(set(result['models']), set(m.MODEL_NAMES))
        for name in m.MODEL_NAMES:
            self.assertTrue(result['available'][name]['p_win3'])
            p = result['models'][name]
            self.assertGreater(p['p_win3'][1], p['p_win3'][0])
            self.assertTrue(np.all(p['p_ge3'] <= p['p_win3']))
            self.assertTrue(all(np.all((a > 0) & (a < 1)) for a in p.values()))
        self.assertFalse(result['formal_trade_ready'])
        self.assertFalse(result['disagreement_is_confidence_interval'])

    def test_training_preprocessing_cannot_see_calibration_extremes(self):
        altered = self.calibration.copy()
        altered['x'] = 1e12
        bundle = m.fit_bundle(self.train, altered, ['x', 'missing'], TARGETS, date_column='date')
        for name in ['lower', 'upper', 'median']:
            np.testing.assert_array_equal(bundle['preprocessor'][name], self.bundle['preprocessor'][name])
        np.testing.assert_array_equal(bundle['preprocessor']['scaler'].mean_, self.bundle['preprocessor']['scaler'].mean_)

    def test_single_class_never_masquerades_as_supported_high_confidence(self):
        train, calibration = self.train.copy(), self.calibration.copy()
        train['label_profit3'] = 1
        calibration['label_profit3'] = 1
        bundle = m.fit_bundle(train, calibration, ['x'], {'p_win3': 'label_profit3'}, date_column='date')
        result = m.predict_bundle(bundle, pd.DataFrame({'x': [0., 1.]}))
        for name in m.MODEL_NAMES:
            self.assertFalse(result['available'][name]['p_win3'])
            np.testing.assert_allclose(result['models'][name]['p_win3'], 21/22)

    def test_endpoint_missing_labels_are_not_converted_to_losses(self):
        train, calibration = self.train.copy(), self.calibration.copy()
        train['label_ge6'] = np.nan
        calibration['label_ge6'] = np.nan
        bundle = m.fit_bundle(train, calibration, ['x'], TARGETS, date_column='date')
        result = m.predict_bundle(bundle, self.calibration)
        self.assertFalse(result['available']['equal_ensemble']['p_ge6'])
        np.testing.assert_allclose(result['models']['logistic']['p_ge6'], .5)
        self.assertEqual(bundle['metadata']['endpoints']['hgb']['p_ge6']['train']['rows'], 0)

    def test_many_positive_rows_on_one_date_are_insufficient_support(self):
        train, calibration = self.train.copy(), self.calibration.copy()
        train['label_profit3'] = (train.date == train.date.iloc[0]).astype(float)
        bundle = m.fit_bundle(train, calibration, ['x'], {'p_win3': 'label_profit3'}, date_column='date')
        for name in ['logistic', 'hgb']:
            support = bundle['metadata']['endpoints'][name]['p_win3']
            self.assertEqual(support['train']['positive'], 20)
            self.assertEqual(support['train']['positive_dates'], 1)
            self.assertFalse(support['available'])

    def test_empty_predictions_and_nonbinary_label_rejection(self):
        result = m.predict_bundle(self.bundle, self.calibration.iloc[:0])
        self.assertEqual(len(result['models']['logistic']['p_win3']), 0)
        invalid = self.train.copy()
        invalid.loc[0, 'label_profit3'] = 2
        with self.assertRaisesRegex(ValueError, 'non-binary'):
            m.fit_bundle(invalid, self.calibration, ['x'], TARGETS, date_column='date')

    def test_joblib_round_trip_is_exact(self):
        before = m.predict_bundle(self.bundle, self.calibration)
        stream = io.BytesIO()
        joblib.dump(self.bundle, stream)
        stream.seek(0)
        after = m.predict_bundle(joblib.load(stream), self.calibration)
        for name in m.MODEL_NAMES:
            for endpoint in TARGETS:
                np.testing.assert_array_equal(before['models'][name][endpoint], after['models'][name][endpoint])

    def test_different_six_percent_policy_is_not_forced_under_three_percent(self):
        bundle = m.fit_bundle(self.train, self.calibration, ['x'], TARGETS, date_column='date')
        for name in ['logistic', 'hgb']:
            for endpoint, prior in [('p_ge3', .1), ('p_ge6', .8)]:
                bundle['models'][name][endpoint].update({'available': False, 'prior': prior})
        result = m.predict_bundle(bundle, pd.DataFrame({'x': [0.]}))
        self.assertGreater(result['models']['equal_ensemble']['p_ge6'][0], result['models']['equal_ensemble']['p_ge3'][0])


if __name__ == '__main__':
    unittest.main()
