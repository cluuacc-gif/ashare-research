"""Isolated outbox and delivery invariants; no network or SMTP is used."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import autonomous_mailer as a


DAY = '2026-10-09'
STAGE = 'evening'
REPO = 'fixture/research'


def instant(value):
    return dt.datetime.fromisoformat(value)


class AutonomousMailerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clock = [instant(DAY+'T17:50:00+08:00')]
        clock = patch('autonomous_mailer.c.now', side_effect=lambda: self.clock[0])
        clock.start(); self.addCleanup(clock.stop)

    def sealed(self):
        return a.seal(self.root, STAGE, DAY, 'fixture subject', 'fixture research text',
                      '<html><body>fixture research text</body></html>',
                      {'content_kind':'research', 'repository':REPO,
                       'receipt_path':f'state/delivery/{STAGE}-{DAY}.json',
                       'receipt_blob':None, 'snapshot_release':'fixture-data',
                       'formal_trade_ready':False})

    def out(self):
        return a.slot(self.root, STAGE, DAY)

    def bundle(self):
        active = json.loads((self.out()/'active.json').read_text(encoding='utf-8'))
        return self.out()/active['bundle']

    def rewrite_manifest(self, change):
        bundle = self.bundle()
        manifest = json.loads((bundle/'manifest.json').read_text(encoding='utf-8'))
        manifest.update(change)
        a.atomic(bundle/'manifest.json', manifest)
        a.atomic(self.out()/'active.json', {'bundle':bundle.name,
            'manifest_sha256':hashlib.sha256((bundle/'manifest.json').read_bytes()).hexdigest()})

    def report_payload(self):
        return {'late':False, 'snapshot_release':'fixture-data',
                'information_cutoff':DAY+'T17:45:00+08:00',
                'research':{'model_ready':True, 'model_release':'fixture-model'}}

    def prepare_patches(self):
        return (
            patch('autonomous_mailer.snapshot_mail.delivery_record', return_value=(
                f'state/delivery/{STAGE}-{DAY}.json', None, None)),
            patch('autonomous_mailer.snapshot_mail.report', return_value=self.report_payload()),
            patch('mail_layout.content', return_value={'subject':'fixture subject'}),
            patch('mail_layout.render_text', return_value='fixture research text'),
            patch('mail_layout.render_html', return_value='<html>fixture research text</html>'),
        )

    def prepare(self, missing_policy='skip'):
        return a.prepare(STAGE, DAY, self.root, REPO, missing_policy,
                         self.root/'absent-large-store', self.root/'absent-large-model')

    def sender(self, state='smtp_accepted', code=0):
        def invoke(command, **kwargs):
            self.assertEqual(Path(command[1]).name, 'send_mail.py')
            self.assertEqual(command[command.index('--stage')+1], STAGE)
            self.assertEqual(command[command.index('--research-day')+1], DAY)
            self.assertEqual(Path(command[command.index('--body-file')+1]), self.bundle()/'report.txt')
            self.assertEqual(Path(command[command.index('--html-file')+1]), self.bundle()/'report.html')
            status = Path(command[command.index('--status-out')+1])
            a.atomic(status, {'state':state, 'smtp_accepted':state=='smtp_accepted',
                              'receipt_confirmed':False})
            return subprocess.CompletedProcess(command, code)
        return invoke

    def test_early_seal_and_readback_keep_stage_date_and_small_parts(self):
        value = self.sealed()
        bundle, actual = a.read_ready(self.root, STAGE, DAY)
        self.assertEqual(value, actual)
        self.assertLess(instant(actual['prepared_at']), instant(actual['scheduled_at']))
        self.assertEqual({p.name for p in bundle.iterdir()}, {'manifest.json','report.txt','report.html'})
        self.assertEqual(actual['stage'], STAGE); self.assertEqual(actual['target_date'], DAY)
        self.assertFalse(actual['formal_trade_ready'])

    def test_modified_plain_or_html_body_is_rejected(self):
        for name in ['report.txt', 'report.html']:
            with self.subTest(name=name):
                self.sealed()
                with (self.bundle()/name).open('ab') as target:
                    target.write(b' tampered')
                with self.assertRaisesRegex(ValueError, 'body hash'):
                    a.read_ready(self.root, STAGE, DAY)

    def test_modified_manifest_is_rejected_before_using_its_contents(self):
        self.sealed()
        with (self.bundle()/'manifest.json').open('ab') as target:
            target.write(b' ')
        with self.assertRaisesRegex(ValueError, 'manifest hash'):
            a.read_ready(self.root, STAGE, DAY)

    def test_semantically_wrong_stage_date_and_cutoff_rejected_even_when_rehashed(self):
        changes = [{'stage':'morning'}, {'target_date':'2026-10-12'},
                   {'prepared_at':DAY+'T18:00:00+08:00'},
                   {'prepared_at':DAY+'T17:50:00'},
                   {'scheduled_at':DAY+'T18:01:00+08:00'}]
        for change in changes:
            with self.subTest(change=change):
                self.sealed(); self.rewrite_manifest(change)
                with self.assertRaises(ValueError):
                    a.read_ready(self.root, STAGE, DAY)

    def test_active_pointer_cannot_escape_its_slot(self):
        self.sealed()
        active = json.loads((self.out()/'active.json').read_text())
        active['bundle'] = '../outside'
        a.atomic(self.out()/'active.json', active)
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            a.read_ready(self.root, STAGE, DAY)

    def test_prepare_before_deadline_seals_research(self):
        p1,p2,p3,p4,p5 = self.prepare_patches()
        with p1, p2 as report, p3, p4, p5:
            value = self.prepare()
        self.assertEqual(value['content_kind'], 'research'); report.assert_called_once()
        self.assertEqual(a.read_ready(self.root,STAGE,DAY)[1]['snapshot_release'], 'fixture-data')

    def test_prepare_crossing_deadline_during_report_does_not_publish(self):
        p1,p2,p3,p4,p5 = self.prepare_patches()
        def slow_report(*_args):
            self.clock[0] = instant(DAY+'T18:00:00+08:00')
            return self.report_payload()
        with p1, p2 as report, p3, p4, p5:
            report.side_effect = slow_report
            value = self.prepare()
        self.assertEqual(value['state'], 'preparation_deadline_passed')
        self.assertFalse((self.out()/'active.json').exists())

    def test_prepare_at_deadline_never_loads_inputs_or_remote_receipts(self):
        self.clock[0] = instant(DAY+'T18:00:00+08:00')
        with (patch('autonomous_mailer.cached_inputs') as cached,
              patch('autonomous_mailer.snapshot_mail.report') as report,
              patch('autonomous_mailer.snapshot_mail.delivery_record') as receipt):
            value = self.prepare()
        self.assertEqual(value['state'], 'preparation_deadline_passed')
        cached.assert_not_called(); report.assert_not_called(); receipt.assert_not_called()

    def test_prepare_unavailable_skip_option_and_brief(self):
        p1,p2,p3,p4,p5 = self.prepare_patches()
        with p1, p2 as report, p3, p4, p5:
            report.side_effect = ValueError('fixture unavailable')
            result = self.prepare()
            self.assertEqual(result['state'],'report_unavailable')
            self.assertFalse((self.out()/'active.json').exists())
            result = self.prepare('brief')
        self.assertEqual(result['content_kind'],'missing_data_brief')
        self.assertFalse(a.read_ready(self.root,STAGE,DAY)[1]['formal_trade_ready'])

    def test_brief_is_ready_when_long_preparation_crosses_deadline(self):
        p1,p2,p3,p4,p5 = self.prepare_patches()
        def delayed_report(*args):
            self.clock[0] = instant(DAY+'T18:00:00+08:00')
            raise OSError('fixture stalled source')
        with p1,p2 as report,p3,p4,p5:
            report.side_effect=delayed_report
            result=self.prepare('brief')
        self.assertEqual(result['state'],'preparation_deadline_passed')
        _,ready=a.read_ready(self.root,STAGE,DAY)
        self.assertEqual(ready['content_kind'],'missing_data_brief')
        self.assertLess(ready['prepared_at'],DAY+'T18:00:00+08:00')

    def test_missed_delivery_does_not_read_report_restore_or_call_sender(self):
        self.clock[0] = instant(DAY+'T18:01:00+08:00')
        with (patch('autonomous_mailer.read_ready') as read,
              patch('autonomous_mailer.cached_inputs') as cached,
              patch('autonomous_mailer.state.restore') as restore,
              patch('autonomous_mailer.model_store.restore') as model,
              patch('autonomous_mailer.subprocess.run') as sender):
            value = a.deliver(STAGE,DAY,self.root)
        self.assertEqual(value['state'],'missed_window')
        for mock in [read,cached,restore,model,sender]: mock.assert_not_called()

    def test_accepted_and_uncertain_receipts_suppress_before_reading_body(self):
        self.clock[0] = instant(DAY+'T18:00:30+08:00')
        for state in ['smtp_accepted','in_flight','delivery_unknown','partial_acceptance']:
            with self.subTest(state=state):
                a.atomic(self.out()/'delivery.json', {'state':state,'smtp_accepted':state=='smtp_accepted'})
                with (patch('autonomous_mailer.read_ready') as read,
                      patch('autonomous_mailer.subprocess.run') as sender):
                    value = a.deliver(STAGE,DAY,self.root)
                self.assertTrue(value['duplicate_suppressed'])
                read.assert_not_called(); sender.assert_not_called()

    def test_delivery_uses_only_sealed_draft_without_data_or_model_restore(self):
        self.sealed(); self.clock[0] = instant(DAY+'T18:00:00+08:00')
        with (patch('autonomous_mailer.cached_inputs') as cached,
              patch('autonomous_mailer.state.restore') as restore,
              patch('autonomous_mailer.model_store.restore') as model,
              patch('autonomous_mailer.snapshot_mail.report') as render,
              patch('autonomous_mailer.subprocess.run', side_effect=self.sender()) as sender,
              patch('autonomous_mailer.state.run') as publish):
            value = a.deliver(STAGE,DAY,self.root)
        self.assertTrue(value['smtp_accepted']); sender.assert_called_once(); publish.assert_called_once()
        for mock in [cached,restore,model,render]: mock.assert_not_called()
        self.assertFalse((self.out()/'delivery.lock').exists())

    def test_receipt_publish_failure_never_loses_local_acceptance_or_resends(self):
        self.sealed(); self.clock[0] = instant(DAY+'T18:00:00+08:00')
        with (patch('autonomous_mailer.subprocess.run', side_effect=self.sender()) as sender,
              patch('autonomous_mailer.state.run', side_effect=OSError('fixture receipt unavailable'))):
            first = a.deliver(STAGE,DAY,self.root)
            second = a.deliver(STAGE,DAY,self.root)
        self.assertTrue(first['smtp_accepted']); self.assertIn('receipt_publish_error',first)
        self.assertTrue(second['duplicate_suppressed']); sender.assert_called_once()

    def test_interrupted_sender_keeps_in_flight_and_suppresses_restart(self):
        self.sealed(); self.clock[0] = instant(DAY+'T18:00:00+08:00')
        with patch('autonomous_mailer.subprocess.run', side_effect=OSError('fixture child interrupted')) as sender:
            with self.assertRaises(OSError):
                a.deliver(STAGE,DAY,self.root)
            second = a.deliver(STAGE,DAY,self.root)
        self.assertTrue(second['duplicate_suppressed']); sender.assert_called_once()

    def test_stale_child_result_cannot_replace_uncertain_new_attempt(self):
        self.sealed(); self.clock[0] = instant(DAY+'T18:00:00+08:00')
        a.atomic(self.out()/'smtp-result.json', {'state':'failed','smtp_accepted':False,'attempts':3})
        with patch('autonomous_mailer.subprocess.run',return_value=subprocess.CompletedProcess([],137)):
            value = a.deliver(STAGE,DAY,self.root)
        self.assertEqual(value['state'],'delivery_unknown')

    def test_receipt_is_rechecked_after_acquiring_lock(self):
        self.sealed(); self.clock[0] = instant(DAY+'T18:00:00+08:00')
        real_open = os.open
        def acceptance_before_lock(path, flags, mode=0o777, **kwargs):
            if Path(path).name == 'delivery.lock':
                a.atomic(self.out()/'delivery.json', {'state':'smtp_accepted','smtp_accepted':True})
            return real_open(path,flags,mode,**kwargs)
        with (patch('autonomous_mailer.os.open',side_effect=acceptance_before_lock),
              patch('autonomous_mailer.subprocess.run',side_effect=self.sender()) as sender,
              patch('autonomous_mailer.state.run')):
            result = a.deliver(STAGE,DAY,self.root)
        self.assertTrue(result.get('duplicate_suppressed')); sender.assert_not_called()


class CachePruneTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root/'cache'
        self.cache.mkdir()

    def restored(self, kind, day='2026-10-01', run=1, attempt=1, parent=None):
        path = (parent or self.cache)/f'{kind}-{day}-{run}-{attempt}'
        path.mkdir(parents=True)
        if kind == 'prepared':
            names = {'database':'market.sqlite3','handoff':'handoff.json',
                     'frozen':'frozen.zip','quality':'quality.json'}
            manifest = {'release_tag':path.name, **{key:{'name':name} for key,name in names.items()}}
            marker, manifest_file = 'restore.json', 'manifest.json'
            assets = names.values()
        else:
            manifest = {'release_tag':path.name,'files':{name:{'name':name} for name in a.model_store.FILES}}
            marker, manifest_file = 'model_restore.json', 'model_manifest.json'
            assets = a.model_store.FILES
        for name in assets:
            (path/name).write_bytes(b'fixture cached asset')
        a.atomic(path/manifest_file,manifest)
        a.atomic(path/marker,{'verified':True,'manifest':manifest})
        return path

    def fill_newer(self, kind):
        return [self.restored(kind,f'2026-10-{day:02d}',day) for day in (10,11,12)]

    def test_keep_current_and_three_newest_per_kind_remove_older_flat_verified(self):
        protected, old, newest = [], [], []
        for kind in ['prepared','model']:
            protected.append(self.restored(kind))
            old.append(self.restored(kind,'2026-10-02',2))
            newest.extend(self.fill_newer(kind))
        removed = a.prune_cache(self.cache,protected)
        self.assertEqual(set(removed), {path.name for path in old})
        self.assertTrue(all(path.is_dir() for path in protected+newest))
        self.assertTrue(all(not path.exists() for path in old))

    def test_sort_run_and_attempt_numerically_not_lexically(self):
        entries = [self.restored('prepared',run=run) for run in [1,2,9,10]]
        attempts = [self.restored('model',attempt=value) for value in [1,2,9,10]]
        removed = a.prune_cache(self.cache,set())
        self.assertEqual(set(removed),{entries[0].name,attempts[0].name})
        self.assertTrue(all(path.exists() for path in entries[1:]+attempts[1:]))

    def test_unknown_names_partial_missing_markers_and_unknown_files_are_untouched(self):
        self.fill_newer('prepared')
        unknown = self.restored('prepared','2026-10-02',2)
        unknown.rename(unknown.with_name('unrelated-user-folder'))
        partial = self.restored('prepared','2026-10-03',3)
        partial = partial.rename(partial.with_name(partial.name+'.partial-fixture'))
        missing = self.restored('prepared','2026-10-04',4)
        (missing/'restore.json').unlink()
        extra = self.restored('prepared','2026-10-05',5)
        (extra/'personal-notes.txt').write_text('must remain')
        nested = self.restored('prepared','2026-10-06',6)
        (nested/'nested').mkdir()
        (nested/'nested'/'keep.txt').write_text('must remain')
        invalid_date = self.restored('prepared','2026-02-30',7)
        before = {str(path.relative_to(self.cache)) for path in self.cache.rglob('*')}
        self.assertEqual(a.prune_cache(self.cache,set()),[])
        self.assertEqual(before,{str(path.relative_to(self.cache)) for path in self.cache.rglob('*')})
        self.assertTrue(partial.exists()); self.assertTrue(invalid_date.exists())

    def test_false_or_malformed_or_conflicting_verification_markers_are_untouched(self):
        self.fill_newer('model')
        unverified = self.restored('model','2026-10-02',2)
        marker = json.loads((unverified/'model_restore.json').read_text())
        marker['verified'] = 1  # A JSON number is not the explicit true marker.
        a.atomic(unverified/'model_restore.json',marker)
        malformed = self.restored('model','2026-10-03',3)
        (malformed/'model_restore.json').write_text('not JSON')
        wrong_identity = self.restored('model','2026-10-04',4)
        marker = json.loads((wrong_identity/'model_restore.json').read_text())
        marker['manifest']['release_tag'] = 'model-2026-10-04-999-1'
        a.atomic(wrong_identity/'model_restore.json',marker)
        a.atomic(wrong_identity/'model_manifest.json',marker['manifest'])
        self.assertEqual(a.prune_cache(self.cache,set()),[])
        self.assertTrue(all(path.exists() for path in [unverified,malformed,wrong_identity]))

    def test_directory_and_file_symlinks_are_never_followed_or_removed(self):
        self.fill_newer('prepared')
        outside = self.restored('prepared','2026-10-01',1,parent=self.root/'outside')
        link = self.cache/outside.name
        linked_file = self.restored('prepared','2026-10-02',2)
        target = self.root/'external-quality.json'
        target.write_bytes(b'outside file must remain')
        try:
            link.symlink_to(outside,target_is_directory=True)
            (linked_file/'quality.json').unlink()
            (linked_file/'quality.json').symlink_to(target)
        except (OSError,NotImplementedError) as exc:
            self.skipTest('host does not permit symlink fixtures: '+type(exc).__name__)
        self.assertEqual(a.prune_cache(self.cache,set()),[])
        self.assertTrue(link.is_symlink()); self.assertTrue(linked_file.is_dir())
        self.assertEqual(target.read_bytes(),b'outside file must remain')
        self.assertTrue((outside/'market.sqlite3').is_file())

    def test_link_detection_skips_whole_directory_before_any_unlink(self):
        self.fill_newer('prepared')
        linked_directory = self.restored('prepared','2026-10-01',1)
        linked_file = self.restored('prepared','2026-10-02',2)
        real_check = Path.is_symlink
        def reported_link(path):
            return path in {linked_directory,linked_file/'quality.json'} or real_check(path)
        with (patch.object(Path,'is_symlink',autospec=True,side_effect=reported_link),
              patch.object(Path,'unlink',autospec=True) as unlink):
            self.assertEqual(a.prune_cache(self.cache,set()),[])
            unlink.assert_not_called()
        self.assertTrue(linked_directory.is_dir()); self.assertTrue(linked_file.is_dir())

    def test_cleanup_cannot_touch_outbox_or_sources(self):
        self.fill_newer('prepared')
        old = self.restored('prepared','2026-10-02',2)
        for name in ['outbox','sources']:
            target = self.root/name
            target.mkdir()
            (target/'keep.txt').write_text('must remain')
        self.assertEqual(a.prune_cache(self.cache,set()),[old.name])
        for name in ['outbox','sources']:
            self.assertEqual((self.root/name/'keep.txt').read_text(),'must remain')

    def test_cached_inputs_prunes_only_after_both_verifications_pass(self):
        data = self.restored('prepared')
        model = self.restored('model')
        snapshot = json.loads((data/'restore.json').read_text())['manifest']
        snapshot.update(preparation_ready=True)
        snapshot['database']['sha256'] = 'fixture-sha'
        a.atomic(data/'restore.json',{'verified':True,'manifest':snapshot})
        bundle = json.loads((model/'model_restore.json').read_text())['manifest']
        bundle.update(snapshot_release=data.name,input_database_sha256='fixture-sha')
        a.atomic(model/'model_restore.json',{'verified':True,'manifest':bundle})
        with (patch('autonomous_mailer.state.pointer',return_value=(snapshot,None)),
              patch('autonomous_mailer.model_store.pointer',return_value=(bundle,None)),
              patch('autonomous_mailer.state.verify_file'),
              patch('autonomous_mailer.model_store.verify_bundle',side_effect=ValueError('fixture invalid')),
              patch('autonomous_mailer.prune_cache') as prune):
            with self.assertRaises(ValueError):
                a.cached_inputs(self.root,'fixture/research')
            prune.assert_not_called()
        with (patch('autonomous_mailer.state.pointer',return_value=(snapshot,None)),
              patch('autonomous_mailer.model_store.pointer',return_value=(bundle,None)),
              patch('autonomous_mailer.state.verify_file'),
              patch('autonomous_mailer.model_store.verify_bundle'),
              patch('autonomous_mailer.prune_cache') as prune):
            self.assertEqual(a.cached_inputs(self.root,'fixture/research'),(data,model))
            prune.assert_called_once_with(self.cache,{data,model})


if __name__ == '__main__':
    unittest.main()
