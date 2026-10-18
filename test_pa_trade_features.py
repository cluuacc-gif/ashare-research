import copy
import unittest
import pandas as pd
import probability_panel as panel
from pa_trade_features import enrich
from test_price_action_research import history_rows


class ContextFeatureTests(unittest.TestCase):
    def test_future_bars_do_not_change_past_context(self):
        rows=history_rows();before=enrich(rows);changed=copy.deepcopy(rows)
        for row in changed[261:]:row.update(open=9,high=9.5,low=8.7,close=9.2)
        pd.testing.assert_frame_equal(before.iloc[:261],enrich(changed).iloc[:261])

    def test_prior_resistance_excludes_signal_bar_and_previous_breakout(self):
        rows=history_rows();before=enrich(rows).iloc[-1]
        rows[-1]['high']+=1;rows[-2]['high']+=2
        after=enrich(rows).iloc[-1]
        self.assertEqual(before.breakout_level,after.breakout_level)
        self.assertEqual(before.prior_range_height,after.prior_range_height)
        self.assertNotEqual(before.prior20_high,after.prior20_high)

    def test_optional_enrichment_preserves_existing_universe_and_features(self):
        rows=history_rows();sessions=[row['trade_date'] for row in rows]
        original=panel.symbol_features(rows,'000001.SZ',sessions)
        enriched=panel.symbol_features(rows,'000001.SZ',sessions,feature_enricher=enrich)
        pd.testing.assert_frame_equal(original,enriched[original.columns])
        self.assertIn('prior60_high',enriched)

    def test_enrichment_cannot_overwrite_existing_features(self):
        rows=history_rows();sessions=[row['trade_date'] for row in rows]
        with self.assertRaises(ValueError):
            panel.symbol_features(rows,'000001.SZ',sessions,feature_enricher=lambda r:pd.DataFrame({'close':[9]*len(r)}))


if __name__=='__main__':unittest.main()
