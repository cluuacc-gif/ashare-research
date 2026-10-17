"""Synthetic storage-contract tests; these fixtures are never win-rate evidence."""

import base64
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import net_profit_store as store


class NetProfitStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='synthetic-net-profit-store-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / 'research'
        self.output.mkdir()
        self.snapshot = {
            'release_tag': 'synthetic-prepared-2026-10-16',
            'base_trade_date': '2026-10-16',
            'next_trade_date': '2026-10-19',
            'preparation_ready': True,
            'database': {'name': 'synthetic.sqlite3', 'bytes': 0, 'sha256': 'a' * 64},
        }
        self.safe = {
            'formal_trade_ready': False,
            'formal_prediction_allowed': False,
            'execution_win_rate_80_validated': False,
            'real_execution_win_rate': None,
        }
        self.protocol = {'fixture_kind': 'synthetic_unit_test_only', 'entry': 'next_session_open'}
        store.write(self.output / 'protocol.json', self.protocol)
        self.protocol_hash = store.ref(self.output / 'protocol.json')['sha256']
        common = {
            **self.safe,
            'base_trade_date': self.snapshot['base_trade_date'],
            'target_trade_date': self.snapshot['next_trade_date'],
            'input_database_sha256': self.snapshot['database']['sha256'],
            'protocol_sha256': self.protocol_hash,
        }
        self.summary = {**common, 'protocol': copy.deepcopy(self.protocol),
                        'fixture_kind': 'synthetic_unit_test_only'}
        self.predictions = {
            **common,
            'formal_candidates': [],
            'rows': [{'symbol': 'SYNTHETIC.TEST', 'probability_net_profit': 0.6,
                      'research_signal': False}],
        }
        store.write(self.output / 'summary.json', self.summary)
        store.write(self.output / 'predictions.json', self.predictions)
        store.write(self.output / 'source_manifest.json', self.snapshot)
        for name in store.FILES:
            if not (self.output / name).exists():
                (self.output / name).write_bytes(b'synthetic fixture, not market data\n')
        self.manifest = {
            **self.safe,
            'schema_version': store.SCHEMA,
            'release_tag': 'synthetic-net-profit-research-1',
            'snapshot_release': self.snapshot['release_tag'],
            'base_trade_date': self.snapshot['base_trade_date'],
            'target_trade_date': self.snapshot['next_trade_date'],
            'input_database_sha256': self.snapshot['database']['sha256'],
            'protocol_sha256': self.protocol_hash,
            'summary': copy.deepcopy(self.summary),
            'files': {name: store.ref(self.output / name) for name in store.FILES},
        }

    def rewrite(self, name, payload):
        store.write(self.output / name, payload)
        self.manifest['files'][name] = store.ref(self.output / name)

    def make_source_store(self):
        source_store = self.root / 'restored'
        source_store.mkdir()
        database = source_store / self.snapshot['database']['name']
        database.write_bytes(b'synthetic database bytes for checksum tests only')
        self.snapshot['database'] = store.ref(database)
        self.manifest['input_database_sha256'] = self.snapshot['database']['sha256']
        for name, payload in [('summary.json', self.summary), ('predictions.json', self.predictions)]:
            payload['input_database_sha256'] = self.snapshot['database']['sha256']
            self.rewrite(name, payload)
        self.manifest['summary'] = copy.deepcopy(self.summary)
        self.rewrite('source_manifest.json', self.snapshot)
        store.write(source_store / 'restore.json', {'verified': True, 'manifest': self.snapshot})
        return source_store

    def assert_no_production_write(self, calls):
        for args in calls:
            if args[:2] == ('gh', 'api') and '--method' in args:
                self.assertIn(f'repos/synthetic/repo/contents/{store.POINTER}', args)
                self.assertNotIn('repos/synthetic/repo/contents/state/current.json', args)
                self.assertNotIn('repos/synthetic/repo/contents/state/model-current.json', args)

    def test_valid_bundle_preserves_research_only_admission(self):
        summary, predictions = store.verify_bundle(self.output, self.manifest)
        self.assertEqual(summary, self.summary)
        self.assertEqual(predictions['formal_candidates'], [])
        for payload in (self.manifest, summary, predictions):
            for name, expected in self.safe.items():
                self.assertIs(payload[name], expected)

    def test_every_asset_checksum_is_verified(self):
        for name in store.FILES:
            with self.subTest(asset=name):
                path = self.output / name
                original = path.read_bytes()
                path.write_bytes(original + b'corruption')
                with self.assertRaisesRegex(ValueError, 'bytes/hash mismatch'):
                    store.verify_bundle(self.output, self.manifest)
                path.write_bytes(original)

    def test_same_size_asset_corruption_is_rejected(self):
        path = self.output / 'model.joblib'
        data = path.read_bytes()
        path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        with self.assertRaisesRegex(ValueError, 'bytes/hash mismatch'):
            store.verify_bundle(self.output, self.manifest)

    def test_asset_set_and_filename_must_be_exact(self):
        for mutation in ('missing', 'renamed', 'traversal', 'schema'):
            with self.subTest(mutation=mutation):
                manifest = copy.deepcopy(self.manifest)
                if mutation == 'missing':
                    manifest['files'].pop('report.html')
                elif mutation == 'renamed':
                    manifest['files']['report.html']['name'] = 'other.html'
                elif mutation == 'traversal':
                    manifest['files']['report.html']['name'] = '../report.html'
                else:
                    manifest['schema_version'] = 'unknown-schema'
                with self.assertRaises(ValueError):
                    store.verify_bundle(self.output, manifest)

    def test_payload_base_date_and_source_hash_must_match_manifest(self):
        for name in ('summary.json', 'predictions.json'):
            original = json.loads((self.output / name).read_text(encoding='utf-8'))
            for field, incorrect in [('base_trade_date', '2026-10-15'),
                                     ('input_database_sha256', 'b' * 64)]:
                with self.subTest(asset=name, field=field):
                    payload = {**original, field: incorrect}
                    self.rewrite(name, payload)
                    with self.assertRaisesRegex(ValueError, 'research (base date|source hash) mismatch'):
                        store.verify_bundle(self.output, self.manifest)
                    self.rewrite(name, original)

    def test_saved_summary_must_equal_pointer_summary(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['summary']['extra'] = 'not in artifact'
        with self.assertRaisesRegex(ValueError, 'pointer summary differs'):
            store.verify_bundle(self.output, manifest)

    def test_prepared_source_lineage_must_match(self):
        for field, incorrect in [('release_tag', 'another-release'),
                                 ('base_trade_date', '2026-10-15'),
                                 ('preparation_ready', False),
                                 ('database', {'sha256': 'b' * 64})]:
            with self.subTest(field=field):
                self.rewrite('source_manifest.json', {**self.snapshot, field: incorrect})
                with self.assertRaisesRegex(ValueError, 'snapshot lineage mismatch'):
                    store.verify_bundle(self.output, self.manifest)
                self.rewrite('source_manifest.json', self.snapshot)

    def test_protocol_pointer_hash_must_match_actual_asset(self):
        self.manifest['protocol_sha256'] = 'c' * 64
        with self.assertRaisesRegex(ValueError, 'protocol hash mismatch'):
            store.verify_bundle(self.output, self.manifest)

    def test_protocol_hash_must_match_both_json_payloads(self):
        for name in ('summary.json', 'predictions.json'):
            with self.subTest(asset=name):
                original = json.loads((self.output / name).read_text(encoding='utf-8'))
                self.rewrite(name, {**original, 'protocol_sha256': 'd' * 64})
                if name == 'summary.json':
                    self.manifest['summary'] = json.loads((self.output / name).read_text(encoding='utf-8'))
                with self.assertRaisesRegex(ValueError, 'protocol differs across artifacts'):
                    store.verify_bundle(self.output, self.manifest)
                self.rewrite(name, original)
                self.manifest['summary'] = copy.deepcopy(self.summary)

    def test_frozen_protocol_body_cannot_differ_from_summary(self):
        changed = {**self.summary, 'protocol': {'entry': 'different'}}
        self.rewrite('summary.json', changed)
        self.manifest['summary'] = changed
        with self.assertRaisesRegex(ValueError, 'protocol differs across artifacts'):
            store.verify_bundle(self.output, self.manifest)

    def test_admission_flags_cannot_be_true_or_missing_anywhere(self):
        for name in ('manifest', 'summary.json', 'predictions.json'):
            for flag in ('formal_trade_ready', 'formal_prediction_allowed',
                         'execution_win_rate_80_validated'):
                for value in (True, None):
                    with self.subTest(asset=name, flag=flag, value=value):
                        manifest = copy.deepcopy(self.manifest)
                        if name == 'manifest':
                            manifest[flag] = value
                        else:
                            original = json.loads((self.output / name).read_text(encoding='utf-8'))
                            payload = {**original, flag: value}
                            store.write(self.output / name, payload)
                            manifest['files'][name] = store.ref(self.output / name)
                            if name == 'summary.json':
                                manifest['summary'] = payload
                        with self.assertRaisesRegex(ValueError, 'cannot grant formal admission'):
                            store.verify_bundle(self.output, manifest)
                        if name != 'manifest':
                            store.write(self.output / name, original)

    def test_real_execution_win_rate_cannot_be_invented(self):
        for name in ('manifest', 'summary.json', 'predictions.json'):
            with self.subTest(asset=name):
                manifest = copy.deepcopy(self.manifest)
                if name == 'manifest':
                    manifest['real_execution_win_rate'] = 0.99
                else:
                    original = json.loads((self.output / name).read_text(encoding='utf-8'))
                    payload = {**original, 'real_execution_win_rate': 0.99}
                    store.write(self.output / name, payload)
                    manifest['files'][name] = store.ref(self.output / name)
                    if name == 'summary.json':
                        manifest['summary'] = payload
                with self.assertRaisesRegex(ValueError, 'cannot establish real execution win rate'):
                    store.verify_bundle(self.output, manifest)
                if name != 'manifest':
                    store.write(self.output / name, original)

    def test_formal_candidate_output_is_always_empty(self):
        self.rewrite('predictions.json', {**self.predictions, 'formal_candidates': ['SYNTHETIC.TEST']})
        with self.assertRaisesRegex(ValueError, 'must not output formally approved candidates'):
            store.verify_bundle(self.output, self.manifest)

    def test_probabilities_and_research_signal_are_typed_and_bounded(self):
        for probability, signal in [(True, False), (float('nan'), False),
                                    (-0.1, False), (1.1, False), (None, False), (0.5, 1)]:
            with self.subTest(probability=probability, signal=signal):
                rows = [{'symbol': 'SYNTHETIC.TEST', 'probability_net_profit': probability,
                         'research_signal': signal}]
                self.rewrite('predictions.json', {**self.predictions, 'rows': rows})
                with self.assertRaisesRegex(ValueError, 'invalid research probability/signal'):
                    store.verify_bundle(self.output, self.manifest)

    def test_duplicate_symbols_cannot_enter_prediction_bundle(self):
        self.rewrite('predictions.json', {**self.predictions, 'rows': self.predictions['rows'] * 2})
        with self.assertRaisesRegex(ValueError, 'rows/duplicates'):
            store.verify_bundle(self.output, self.manifest)

    def test_attach_success_adds_research_without_mutating_existing_model(self):
        model = {'model_id': 'production-model-must-stay', 'rows': [{'P1': 0.5}]}
        payload = {'model': model, 'research': {'existing': {'unchanged': True}}}
        before = copy.deepcopy(payload)
        with patch.object(store, 'pointer', return_value=(self.manifest, 'research-blob')) as fetch:
            attached = store.attach(payload, self.snapshot, repo='synthetic/repo')
        self.assertIs(attached, payload)
        self.assertIs(attached['model'], model)
        self.assertEqual(attached['model'], before['model'])
        self.assertEqual(attached['research']['existing'], before['research']['existing'])
        item = attached['research']['net_profit_strategy']
        self.assertEqual(item['status'], 'research_only')
        self.assertEqual(item['protocol_sha256'], self.protocol_hash)
        self.assertEqual(item['release_tag'], self.manifest['release_tag'])
        for name, expected in self.safe.items():
            self.assertIs(item[name], expected)
        fetch.assert_called_once_with('synthetic/repo', timeout=12)

    def test_attach_rejects_each_snapshot_mismatch_without_touching_model(self):
        for field, incorrect in [('snapshot_release', 'another-release'),
                                 ('input_database_sha256', 'b' * 64),
                                 ('base_trade_date', '2026-10-15')]:
            with self.subTest(field=field):
                payload = {'model': {'model_id': 'unchanged'}}
                manifest = {**self.manifest, field: incorrect}
                with patch.object(store, 'pointer', return_value=(manifest, 'blob')):
                    store.attach(payload, self.snapshot, repo='synthetic/repo')
                self.assertEqual(payload['model'], {'model_id': 'unchanged'})
                item = payload['research']['net_profit_strategy']
                self.assertEqual(item['status'], 'snapshot_mismatch')
                self.assertFalse(item['formal_trade_ready'])

    def test_attach_timeout_is_bounded_optional_and_leaves_model_intact(self):
        payload = {'model': {'model_id': 'unchanged'}}
        with patch.object(store, 'pointer', side_effect=subprocess.TimeoutExpired('gh', 12)) as fetch:
            store.attach(payload, self.snapshot, repo='synthetic/repo')
        self.assertEqual(payload['model'], {'model_id': 'unchanged'})
        self.assertEqual(payload['research']['net_profit_strategy']['status'], 'unavailable')
        self.assertEqual(payload['research']['net_profit_strategy']['reason'], 'TimeoutExpired')
        fetch.assert_called_once_with('synthetic/repo', timeout=12)

    def test_attach_rejects_summary_identity_tampering(self):
        for field, incorrect in [('base_trade_date', '2026-10-15'),
                                 ('input_database_sha256', 'b' * 64),
                                 ('protocol_sha256', 'c' * 64)]:
            with self.subTest(field=field):
                manifest = copy.deepcopy(self.manifest)
                manifest['summary'][field] = incorrect
                payload = {'model': {'model_id': 'unchanged'}}
                with patch.object(store, 'pointer', return_value=(manifest, 'blob')):
                    store.attach(payload, self.snapshot, repo='synthetic/repo')
                item = payload['research']['net_profit_strategy']
                self.assertEqual(item['status'], 'unavailable')
                self.assertEqual(item['reason'], 'ValueError')
                self.assertFalse(item['formal_prediction_allowed'])
                self.assertEqual(payload['model'], {'model_id': 'unchanged'})

    def test_attach_missing_pointer_and_unsafe_admission_fail_closed(self):
        for manifest in (None, {**self.manifest, 'formal_trade_ready': True}):
            with self.subTest(missing=manifest is None):
                payload = {'model': {'model_id': 'unchanged'}}
                with patch.object(store, 'pointer', return_value=(manifest, None)):
                    store.attach(payload, self.snapshot, repo='synthetic/repo')
                item = payload['research']['net_profit_strategy']
                self.assertEqual(item['status'], 'unavailable')
                self.assertFalse(item['formal_trade_ready'])
                self.assertIsNone(item['real_execution_win_rate'])
                self.assertEqual(payload['model'], {'model_id': 'unchanged'})

    def test_publish_refuses_advanced_source_before_any_external_mutation(self):
        source_store = self.make_source_store()
        advanced = {**self.snapshot, 'release_tag': 'newer-prepared-release'}
        with patch.object(store, 'pointer', return_value=(None, None)), \
                patch.object(store, 'data_pointer', return_value=(advanced, 'advanced-blob')), \
                patch.object(store, 'run') as remote:
            with self.assertRaisesRegex(ValueError, 'data snapshot advanced'):
                store.publish('synthetic/repo', self.output, source_store, 'synthetic-run')
        remote.assert_not_called()

    def fake_release_calls(self, calls):
        def invoke(*args):
            calls.append(args)
            if args[:3] == ('gh', 'release', 'download'):
                destination = Path(args[args.index('--dir') + 1])
                names = ([args[args.index('--pattern') + 1]] if '--pattern' in args else store.FILES)
                for name in names:
                    shutil.copyfile(self.output / name, destination / name)
            return ''
        return invoke

    def test_publish_race_after_asset_upload_retains_release_without_pointer_write(self):
        source_store = self.make_source_store()
        advanced = {**self.snapshot, 'release_tag': 'newer-prepared-release'}
        calls = []
        with patch.object(store, 'pointer', side_effect=[(None, None), (None, None)]), \
                patch.object(store, 'data_pointer', side_effect=[(self.snapshot, 'data-blob'),
                                                                (advanced, 'new-data-blob')]), \
                patch.object(store, 'run', side_effect=self.fake_release_calls(calls)):
            with self.assertRaisesRegex(ValueError, 'pointer advanced; immutable release retained'):
                store.publish('synthetic/repo', self.output, source_store, 'synthetic-run')
        self.assertTrue(any(call[:3] == ('gh', 'release', 'upload') for call in calls))
        self.assertFalse(any(call[:2] == ('gh', 'api') for call in calls))
        self.assert_no_production_write(calls)

    def test_successful_publication_writes_only_isolated_research_pointer(self):
        source_store = self.make_source_store()
        calls = []
        pointer_reads = 0

        def research_pointer(repo, timeout=15):
            nonlocal pointer_reads
            pointer_reads += 1
            if pointer_reads <= 2:
                return None, None
            saved = json.loads((self.output / 'net_profit_manifest.json').read_text(encoding='utf-8'))
            return saved, 'new-research-blob'

        with patch.object(store, 'pointer', side_effect=research_pointer), \
                patch.object(store, 'data_pointer', return_value=(self.snapshot, 'data-blob')), \
                patch.object(store, 'run', side_effect=self.fake_release_calls(calls)):
            manifest = store.publish('synthetic/repo', self.output, source_store, 'synthetic-run')
        self.assertFalse(manifest['formal_trade_ready'])
        self.assertIsNone(manifest['real_execution_win_rate'])
        writes = [call for call in calls if call[:2] == ('gh', 'api')]
        self.assertEqual(len(writes), 1)
        self.assert_no_production_write(calls)
        request = json.loads((self.output / 'net-profit-pointer-request.json').read_text(encoding='utf-8'))
        published = json.loads(base64.b64decode(request['content']))
        self.assertEqual(published, manifest)
        self.assertEqual(request['branch'], store.BRANCH)


if __name__ == '__main__':
    unittest.main()
