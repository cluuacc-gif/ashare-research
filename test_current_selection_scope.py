"""Current watchlists must not bypass the user scope through older research."""
from copy import deepcopy
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from current_selection_scope import filter_current_rows, filter_current_study
import snapshot_mail

BASE = '2026-10-23'
CUTOFF = BASE + 'T20:00:00+08:00'
TARGET = '2026-10-26'


def database(path):
    connection = sqlite3.connect(path)
    connection.executescript('''
        CREATE TABLE security_master(symbol TEXT PRIMARY KEY,name TEXT,board TEXT,last_seen_at TEXT);
        CREATE TABLE security_status_daily(symbol TEXT,trade_date TEXT,observed_at TEXT,
            is_st INTEGER,delisting INTEGER,trading_status TEXT,verified INTEGER,published_at TEXT);
    ''')
    entries = [('600001.SH', '正常主板', '主板', BASE+'T15:01:00+08:00'),
               ('688001.SH', '科创样本', '科创板', BASE+'T15:01:00+08:00'),
               ('300001.SZ', '创业样本一', '创业板', BASE+'T15:01:00+08:00'),
               ('301001.SZ', '创业样本二', '创业板', BASE+'T15:01:00+08:00'),
               ('302001.SZ', '未来创业代码', None, BASE+'T15:01:00+08:00'),
               ('600002.SH', ' ＊ＳＴ 风险', '主板', BASE+'T15:01:00+08:00'),
               ('600003.SH', '未改名风险股', '主板', BASE+'T15:01:00+08:00'),
               ('600004.SH', '已退风险股', '主板', BASE+'T15:01:00+08:00'),
               ('600005.SH', '未来名称', '主板', BASE+'T20:00:01+08:00'),
               ('600006.SH', '过旧名称', '主板', '2026-10-22T15:00:00+08:00')]
    connection.executemany('INSERT INTO security_master VALUES(?,?,?,?)', entries)
    for symbol, st, delisting in [('600003.SH', 1, 0), ('600004.SH', 0, 1)]:
        connection.execute('INSERT INTO security_status_daily VALUES(?,?,?,?,?,?,?,?)',
                           (symbol, BASE, BASE+'T15:10:00+08:00', st, delisting,
                            'traded', 1, BASE+'T15:00:00+08:00'))
    connection.commit()
    return connection


def candidates():
    return [{'symbol': symbol, 'name': name, 'p_win3': .8123, 'p_ge3': .4123,
             'quantity_ref': 100, 'context': 'trend', 'setup': 'trend_pullback_rejection'}
            for symbol, name in [('300001.SZ', '创业样本一'), ('301001.SZ', '创业样本二'),
                ('302001.SZ', '未来创业代码'), ('600002.SH', '普通旧简称'),
                ('600003.SH', '未改名风险股'), ('600004.SH', '已退风险股'),
                ('600005.SH', '未来名称'), ('600006.SH', '过旧名称'),
                ('600099.SH', '无元数据'), ('600001.SH', '正常主板'),
                ('688001.SH', '科创样本')]]


class CurrentScopeTests(unittest.TestCase):
    def test_scope_uses_restored_current_metadata_and_preserves_numeric_model_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'scope.sqlite'
            connection = database(path)
            before = candidates()
            selected, scope = filter_current_rows(before, connection, BASE, CUTOFF)
            self.assertEqual([row['symbol'] for row in selected], ['600001.SH', '688001.SH'])
            self.assertEqual(selected[0]['p_win3'], .8123)
            self.assertEqual(selected[0]['p_ge3'], .4123)
            self.assertEqual(scope['input_count'], 11)
            self.assertEqual(scope['rejected_count'], 9)
            self.assertFalse(scope['historical_statistics_apply_to_new_scope'])
            self.assertFalse(scope['model_probabilities_recomputed'])
            self.assertEqual(before, candidates())
            connection.close()
            with closing(sqlite3.connect(path)) as readback:
                self.assertEqual(readback.execute('SELECT COUNT(*) FROM security_master').fetchone()[0], 10)

    def test_payload_risk_cannot_be_laundered_through_normal_master_name(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = database(Path(directory)/'scope.sqlite')
            for extra in ({'name': 's t风险'}, {'board': 'ChiNext'}, {'is_st': True}, {'delisting': 1}):
                with self.subTest(extra=extra):
                    selected, _ = filter_current_rows([dict(symbol='600001.SH', **extra)], connection, BASE, CUTOFF)
                    self.assertEqual(selected, [])
            connection.close()

    def test_study_filter_never_changes_old_diagnostics_protocol_or_input(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = database(Path(directory)/'scope.sqlite')
            history = {'n_attempts': 99, 'win_rate': .71, 'symbols': ['300001.SZ', '600002.SH']}
            raw = {'diagnostic': history, 'selection': [{'model': 'old', 'win_rate': .8}],
                   'protocol': {'universe': 'old_research_universe'},
                   'split': {'diagnostic': {'rows': 999}}, 'current_watchlist': candidates(),
                   'setup_observations': candidates(), 'formal_candidates': []}
            old = deepcopy(raw)
            selected = filter_current_study(raw, connection, BASE, CUTOFF)
            for field in ('diagnostic', 'selection', 'protocol', 'split'):
                self.assertEqual(selected[field], old[field])
            self.assertEqual(raw, old)
            self.assertEqual(len(selected['current_watchlist']), 2)
            self.assertEqual(len(selected['setup_observations']), 2)
            self.assertFalse(selected['current_selection_scope']['historical_diagnostics_recomputed'])
            connection.close()

    def test_missing_restored_metadata_blocks_current_candidates_without_creating_database(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory)/'absent.sqlite'
            selected, scope = filter_current_rows(candidates(), missing, BASE, CUTOFF)
            self.assertEqual(selected, [])
            self.assertFalse(missing.exists())
            self.assertEqual(scope['reason'], 'restored_security_metadata_unavailable')

    def test_future_official_risk_observation_does_not_rewrite_current_known_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = database(Path(directory)/'scope.sqlite')
            connection.execute('INSERT INTO security_status_daily VALUES(?,?,?,?,?,?,?,?)',
                ('600001.SH', BASE, BASE+'T20:00:01+08:00', 1, 0, 'traded', 1,
                 BASE+'T20:00:00+08:00'))
            selected, _ = filter_current_rows([{'symbol': '600001.SH'}], connection, BASE, CUTOFF)
            self.assertEqual([row['symbol'] for row in selected], ['600001.SH'])
            self.assertEqual(selected[0]['name'], '正常主板')
            connection.close()

    def test_name_freshness_uses_shanghai_trading_date_for_utc_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = database(Path(directory)/'scope.sqlite')
            # This is already October 23 in Shanghai, although the UTC date is 22.
            connection.execute('UPDATE security_master SET last_seen_at=? WHERE symbol=?',
                               ('2026-10-22T16:30:00Z', '600001.SH'))
            selected, _ = filter_current_rows([{'symbol': '600001.SH'}], connection, BASE, CUTOFF)
            self.assertEqual([row['symbol'] for row in selected], ['600001.SH'])
            connection.close()

    def test_mail_refilters_old_context_before_opening_review_and_current_model_presentation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'scope.sqlite'
            database(path).close()
            study = {'schema': 'pa-context-trade-research-v1', 'current_watchlist': candidates(),
                     'setup_observations': candidates(), 'policy': {'active': True},
                     'diagnostic': {'old_universe_win_rate': .72}}
            context = {key: deepcopy(study) for key in
                       ('price_action_research', 'probability_research', 'pa_trade_research')}
            original_context = deepcopy(context)
            models = {'prediction_count': 11, 'rows': candidates(), 'model_id': 'unchanged-model'}
            original_models = deepcopy(models)
            payload = {'information_cutoff': CUTOFF, 'research': {'model_predictions': models,
                       'price_band_5_10': candidates(), 'gross_swing_strategy': deepcopy(study)},
                       'pa_opening_evidence': {'symbols': {row['symbol']: {} for row in candidates()}}}
            with (patch('pa_entry_confirmation.review', return_value={'conditions_met_for_research': False}) as review,
                  patch('sector_forecast.generate', return_value={'rankings': []}),
                  patch('opening_strength.generate', return_value={'watchlist': []}),
                  patch('sector_stock_selection.generate', return_value={'watchlist': []})):
                result = snapshot_mail.attach_market_outlook(payload, path, BASE, TARGET, context_data=context)
            self.assertEqual([call.args[0]['symbol'] for call in review.call_args_list],
                             ['600001.SH', '688001.SH'])
            research = result['research']
            for key in ('price_action_research', 'probability_research', 'pa_trade_research', 'gross_swing_strategy'):
                self.assertEqual([r['symbol'] for r in research[key]['current_watchlist']],
                                 ['600001.SH', '688001.SH'])
                self.assertEqual(research[key]['diagnostic'], study['diagnostic'])
            self.assertEqual([r['symbol'] for r in research['model_predictions']['rows']],
                             ['600001.SH', '688001.SH'])
            self.assertEqual(len(research['price_band_5_10']), 2)
            self.assertEqual(context, original_context)
            self.assertEqual(models, original_models)
            self.assertEqual(research['model_predictions']['prediction_count'], 11)
            self.assertFalse(research['pa_opening_confirmation']['confirmed'])


if __name__ == '__main__':
    unittest.main()
