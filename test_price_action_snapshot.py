"""Lightweight runtime checks plus parity with the research vectorized formulas."""
import copy
import datetime as dt
import math
from pathlib import Path
import random
import subprocess
import sys
import unittest

import price_action_snapshot as light


def bars(seed=1, count=100):
    rng = random.Random(seed)
    result, previous = [], 7.0
    for index in range(count):
        open_ = previous * (1 + rng.uniform(-.008, .008))
        close = open_ * (1 + rng.uniform(-.018, .022))
        high = max(open_, close) * (1 + rng.uniform(.001, .015))
        low = min(open_, close) * (1 - rng.uniform(.001, .015))
        volume = rng.uniform(3e6, 8e6)
        result.append({'trade_date': (dt.date(2026, 1, 1) + dt.timedelta(days=index)).isoformat(),
            'open': open_, 'high': high, 'low': low, 'close': close,
            'volume': volume, 'amount': volume * close, 'conflict': 0})
        previous = close
    return result


def trend_bars():
    result = bars()
    for index, row in enumerate(result):
        close = 6.0 + index * .008
        row.update(open=close - .015, high=close + .025, low=close - .10,
                   close=close, volume=5e6, amount=35e6)
    return result


class LightweightSnapshotTests(unittest.TestCase):
    def test_runtime_imports_only_standard_library(self):
        folder = str(Path(__file__).resolve().parent)
        code = ('import sys;sys.path.insert(0,sys.argv[1]);'
                'import price_action_snapshot,sector_stock_selection;'
                'assert not any(n in sys.modules for n in ("numpy","pandas","sklearn","joblib"))')
        process = subprocess.run([sys.executable, '-S', '-c', code, folder],
                                 capture_output=True, text=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_positive_pattern_is_observation_without_known_market_context(self):
        result = light.price_action_snapshot(trend_bars())
        self.assertIn('higher_lows', result['eligible_patterns'])
        self.assertFalse(result['market_context_known'])
        self.assertFalse(result['research_signal'])
        self.assertFalse(result['score_is_probability'])
        self.assertFalse(result['formal_trade_ready'])
        confirmed = light.price_action_snapshot(trend_bars(), market_breadth=.6, market_mean_return=0)
        self.assertTrue(confirmed['research_signal'])

    def test_past_older_rows_cannot_change_trailing_snapshot(self):
        original = bars()
        before = light.price_action_snapshot(original)
        for row in original[:-61]:
            row.update(close=None, amount=None, volume=None)
        self.assertEqual(before, light.price_action_snapshot(original))

    def test_conflicts_and_missing_tail_are_rejected_without_imputation(self):
        original = bars()
        original[-20]['conflict'] = 1
        self.assertFalse(light.price_action_snapshot(original)['available'])
        self.assertFalse(light.price_action_snapshot(bars(count=60))['available'])

    def test_missing_amount_is_unknown_liquidity_and_cannot_create_pattern(self):
        original = trend_bars()
        original[-1]['amount'] = None
        result = light.price_action_snapshot(original)
        self.assertTrue(result['available'])
        self.assertIsNone(result['median_amount20'])
        self.assertEqual(result['eligible_patterns'], [])

    def test_duplicate_dates_rejected_and_flat_range_is_finite(self):
        original = trend_bars()
        original.append(copy.deepcopy(original[-1]))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            light.price_action_snapshot(original)
        original = bars()
        for row in original:
            row.update(open=7, high=7, low=7, close=7)
        result = light.price_action_snapshot(original)
        self.assertEqual(result['close_location'], .5)
        self.assertEqual(result['efficiency20'], 0)
        self.assertIsNone(result['price_action_score'])
        self.assertFalse(result['research_signal'])


try:
    import price_action_research as reference
except ImportError:
    reference = None


@unittest.skipIf(reference is None, 'vectorized research dependencies not installed on this host')
class VectorizedParityTests(unittest.TestCase):
    def reference_snapshot(self, rows, market5, breadth, mean_return):
        frame = reference.price_action_features(rows).iloc[[-1]].copy()
        frame['relative_return5'] = frame.ret5 - market5
        frame['market_breadth'] = breadth
        frame['market_mean_return'] = mean_return
        frame = reference.add_score(frame)
        final = frame.iloc[0]
        numeric = {key: reference._finite(final[key]) for key in light.FEATURES}
        numeric['eligible_patterns'] = [pattern for pattern in light.PATTERNS
            if bool(reference.pattern_filter(frame, pattern, context_required=False).iloc[0])]
        known = breadth is not None and mean_return is not None
        numeric['market_context_eligible_patterns'] = [pattern for pattern in numeric['eligible_patterns']
            if known and bool(reference.pattern_filter(frame, pattern).iloc[0])]
        return numeric

    def test_same_price_action_math_and_patterns_across_research_and_sender(self):
        samples = [bars(seed) for seed in range(32)] + [trend_bars()]
        count_patterns = 0
        for number, rows in enumerate(samples):
            for context in ((0, .6, .001), (.02, .2, -.03), (0, None, None)):
                with self.subTest(sample=number, context=context):
                    market5, breadth, mean_return = context
                    expected = self.reference_snapshot(rows, market5, breadth, mean_return)
                    actual = light.price_action_snapshot(rows, market_return5=market5,
                        market_breadth=breadth, market_mean_return=mean_return)
                    for key in light.FEATURES:
                        if expected[key] is None:
                            self.assertIsNone(actual[key], key)
                        else:
                            self.assertTrue(math.isclose(actual[key], expected[key], rel_tol=1e-11, abs_tol=1e-10),
                                            f'{key}: {actual[key]} vs {expected[key]}')
                    self.assertEqual(actual['eligible_patterns'], expected['eligible_patterns'])
                    self.assertEqual(actual['market_context_eligible_patterns'], expected['market_context_eligible_patterns'])
                    count_patterns += len(actual['eligible_patterns'])
        self.assertGreater(count_patterns, 0)

    def test_flat_and_missing_amount_have_same_unknown_outcomes(self):
        samples = [bars(), trend_bars()]
        for row in samples[0]:
            row.update(open=7, high=7, low=7, close=7)
        samples[1][-1]['amount'] = None
        for rows in samples:
            expected = self.reference_snapshot(rows, 0, .6, 0)
            actual = light.price_action_snapshot(rows, market_breadth=.6, market_mean_return=0)
            for key in light.FEATURES:
                if expected[key] is None:
                    self.assertIsNone(actual[key], key)
                else:
                    self.assertAlmostEqual(actual[key], expected[key], places=10)
            self.assertEqual(actual['eligible_patterns'], expected['eligible_patterns'])


if __name__ == '__main__':
    unittest.main()
