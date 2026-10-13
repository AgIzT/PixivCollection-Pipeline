"""Offline multi-account regressions with shared durable image archives.

Every image is synthetic, the publisher stubs R2, and socket access is banned.
Configuration paths are placeholders: no credential file is read by tests.
"""

import collections
import contextlib
import copy
import io
import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_x_bookmarks as baseline

x = baseline.x
GiB = 1024 ** 3
DiskUsage = collections.namedtuple('DiskUsage', 'total used free')


def messages_for(source_id=baseline.POST_ID, dimensions=True):
    messages = copy.deepcopy(baseline.MESSAGES)
    for message in messages:
        metadata = message[-1]
        metadata['tweet_id'] = source_id
        metadata['quote_id'] = 0
        if dimensions:
            metadata['width'], metadata['height'] = 64, 48
    return messages


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('PIXIV_TEST_TMP'))
        self.root = Path(self.temp.name)
        self.network = patch.object(socket, 'socket',
                                    side_effect=AssertionError('Network prohibited'))
        self.network.start()
        self.disk = patch('shutil.disk_usage',
                          return_value=DiskUsage(100 * GiB, 10 * GiB, 90 * GiB))
        self.disk.start()
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.disk.stop()
        self.network.stop()
        self.temp.cleanup()

    def state(self):
        return json.loads((self.root / '.x-sync' / 'state.json').read_text('utf-8'))

    def collection(self):
        return json.loads((self.root / 'collection.json').read_text('utf-8'))

    def exported(self):
        return json.loads((self.root / 'images.json').read_text('utf-8'))

    def assert_post_accounts(self, source_id, expected):
        state_accounts = self.state()['items'][source_id]['manifest']['bookmark_accounts']
        self.assertEqual(state_accounts, sorted(expected))
        archive = self.collection()
        image_id = archive['sources']['twitter:' + source_id]
        self.assertEqual(archive['images'][str(image_id)]['data']['bookmark_accounts'],
                         sorted(expected))
        parts = [part for part in self.exported() if part['id'] == image_id]
        self.assertEqual({part['part'] for part in parts}, {0, 1})
        for part in parts:
            self.assertEqual(part['bookmark_accounts'], sorted(expected))

    def test_shared_posts_merge_account_labels_without_duplicate_downloads(self):
        primary = baseline.FakeAdapter(messages_for() + messages_for('100'))
        secondary = baseline.FakeAdapter(messages_for() + messages_for('101'))
        adapter = x.MultiGalleryDL([('primary', primary), ('secondary', secondary)])
        report = x.BookmarkSync(self.root, adapter, baseline.FakePublisher()).run()
        self.assertTrue(adapter.strict_dimensions)
        self.assertIsNone(adapter.max_posts)
        self.assertTrue(report['scan_complete'])
        self.assertEqual(report['phases']['published'], 3)
        self.assertEqual(len(primary.downloads) + len(secondary.downloads), 6)
        self.assertEqual(len(self.exported()), 6)
        self.assertEqual(len(list((self.root / 'image' / 'original').iterdir())), 6)
        self.assert_post_accounts(baseline.POST_ID, ['primary', 'secondary'])
        self.assert_post_accounts('100', ['primary'])
        self.assert_post_accounts('101', ['secondary'])
        for name in ('primary', 'secondary'):
            self.assertTrue(report['accounts'][name]['scan_complete'])
            self.assertTrue(self.state()['accounts'][name]['configured'])
            self.assertIsNotNone(self.state()['accounts'][name]['last_complete_scan'])

    def test_one_account_failure_keeps_other_account_work_and_redacts_diagnostics(self):
        secret = 'synthetic-private-token-DO-NOT-REPORT'
        private_path = '/private/not-read/session-secret.json'

        class FailingAdapter(baseline.FakeAdapter):
            def discover(self):
                raise RuntimeError(secret + ' ' + private_path)

        primary = FailingAdapter()
        secondary = baseline.FakeAdapter(messages_for())
        adapter = x.MultiGalleryDL([('primary', primary), ('secondary', secondary)])
        report = x.BookmarkSync(self.root, adapter, baseline.FakePublisher()).run()
        self.assertFalse(report['scan_complete'])
        self.assertEqual(report['phases']['published'], 1)
        self.assertFalse(report['accounts']['primary']['scan_complete'])
        self.assertTrue(report['accounts']['secondary']['scan_complete'])
        self.assertIsNone(self.state()['last_complete_scan'])
        self.assertIsNone(self.state()['accounts']['primary']['last_complete_scan'])
        self.assertIsNotNone(self.state()['accounts']['secondary']['last_complete_scan'])
        self.assert_post_accounts(baseline.POST_ID, ['secondary'])
        diagnostics = json.dumps(report) + (self.root / '.x-sync' / 'state.json').read_text('utf-8')
        self.assertNotIn(secret, diagnostics)
        self.assertNotIn(private_path, diagnostics)
        self.assertEqual(len(primary.downloads), 2)
        self.assertEqual(secondary.downloads, [])

    def test_partial_scan_and_normalization_errors_do_not_advance_account_checkpoint(self):
        primary = baseline.FakeAdapter(messages_for())
        primary.complete = False
        secondary = baseline.FakeAdapter(messages_for('101'))
        adapter = x.MultiGalleryDL([('primary', primary), ('secondary', secondary)])
        publisher = baseline.FakePublisher()
        report = x.BookmarkSync(self.root, adapter, publisher).run()
        self.assertFalse(report['scan_complete'])
        self.assertEqual(report['phases']['published'], 2)
        self.assertFalse(report['accounts']['primary']['scan_complete'])
        self.assertTrue(report['accounts']['secondary']['scan_complete'])
        self.assertIsNone(self.state()['last_complete_scan'])
        self.assertIsNone(self.state()['accounts']['primary']['last_complete_scan'])
        primary.complete = True
        primary.messages = messages_for('100', dimensions=False)
        report = x.BookmarkSync(self.root, adapter, publisher).run()
        self.assertFalse(report['scan_complete'])
        self.assertFalse(report['accounts']['primary']['scan_complete'])
        self.assertTrue(report['accounts']['secondary']['scan_complete'])
        self.assertEqual(report['pending_retry'], 1)
        self.assertIsNone(self.state()['accounts']['primary']['last_complete_scan'])
        self.assertIsNone(self.state()['last_complete_scan'])
        self.assertEqual(self.state()['items']['100']['phase'], 'discovered')

    def test_legacy_published_post_gains_only_observed_account_and_retries_metadata_upload(self):
        legacy = baseline.FakeAdapter(messages_for())
        x.BookmarkSync(self.root, legacy, baseline.FakePublisher()).run()
        legacy_manifest = self.state()['items'][baseline.POST_ID]['manifest']
        self.assertFalse(legacy_manifest.get('bookmark_accounts'))
        old_registry = copy.deepcopy(self.collection()['sources'])
        originals = self.root / 'image' / 'original'
        old_files = {path.name: path.read_bytes() for path in originals.iterdir()}
        primary = baseline.FakeAdapter([])
        secondary = baseline.FakeAdapter(messages_for())
        adapter = x.MultiGalleryDL([('primary', primary), ('secondary', secondary)])
        failed = x.BookmarkSync(self.root, adapter, baseline.FakePublisher(fail=True)).run()
        self.assertEqual(failed['phases']['archived'], 1)
        self.assertFalse(failed['publication_confirmed'])
        self.assertEqual(primary.downloads + secondary.downloads, [])
        self.assert_post_accounts(baseline.POST_ID, ['secondary'])
        primary.messages = secondary.messages = []
        publisher = baseline.FakePublisher()
        recovered = x.BookmarkSync(self.root, adapter, publisher).run()
        self.assertTrue(recovered['publication_confirmed'])
        self.assertEqual(recovered['phases']['published'], 1)
        self.assertEqual(publisher.calls, 1)
        self.assertEqual(primary.downloads + secondary.downloads, [])
        self.assertEqual(self.collection()['sources'], old_registry)
        self.assertEqual({path.name: path.read_bytes() for path in originals.iterdir()}, old_files)
        self.assert_post_accounts(baseline.POST_ID, ['secondary'])

    def test_empty_or_unconfigured_accounts_never_remove_archive_or_existing_labels(self):
        primary = baseline.FakeAdapter(messages_for())
        secondary = baseline.FakeAdapter(messages_for())
        adapter = x.MultiGalleryDL([('primary', primary), ('secondary', secondary)])
        publisher = baseline.FakePublisher()
        x.BookmarkSync(self.root, adapter, publisher).run()
        before = {name: (self.root / name).read_bytes()
                  for name in ('collection.json', 'images.json')}
        primary.messages = secondary.messages = []
        x.BookmarkSync(self.root, adapter, publisher).run()
        previous_checkpoint = self.state()['accounts']['secondary']['last_complete_scan']
        one_account = x.MultiGalleryDL([('primary', primary)])
        report = x.BookmarkSync(self.root, one_account, publisher).run()
        self.assertEqual(set(report['accounts']), {'primary'})
        self.assertFalse(self.state()['accounts']['secondary']['configured'])
        self.assertEqual(self.state()['accounts']['secondary']['last_complete_scan'],
                         previous_checkpoint)
        self.assertEqual(publisher.calls, 1)
        self.assert_post_accounts(baseline.POST_ID, ['primary', 'secondary'])
        for name, contents in before.items():
            self.assertEqual((self.root / name).read_bytes(), contents)
        self.assertEqual(len(list((self.root / 'image' / 'original').iterdir())), 2)

        # The legacy entry point has no named sessions. Its use must not leave
        # stale configured flags, or remove previously observed labels.
        x.BookmarkSync(self.root, baseline.FakeAdapter([]), publisher).run()
        self.assertTrue(all(not account['configured'] for account in self.state()['accounts'].values()))
        self.assert_post_accounts(baseline.POST_ID, ['primary', 'secondary'])

    def test_multi_account_public_download_uses_first_adapter_without_credentials(self):
        first_config = self.root / 'not-read-first-session.json'
        second_config = self.root / 'not-read-second-session.json'
        primary = x.GalleryDL(first_config)
        secondary = x.GalleryDL(second_config)
        primary.verified = secondary.verified = True
        adapter = x.MultiGalleryDL([('primary', primary), ('secondary', secondary)])
        destination = self.root / 'sample.png.download'
        calls = []

        def run(command):
            calls.append(command)
            destination.write_bytes(b'synthetic-output')
            return subprocess.CompletedProcess(command, 0, '', '')

        with patch.object(primary, 'run', side_effect=run), \
                patch.object(secondary, 'run', side_effect=AssertionError('Wrong public downloader')):
            adapter.download(baseline.MESSAGES[1][1], destination)
        self.assertTrue(adapter.strict_dimensions)
        self.assertIsNone(adapter.max_posts)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('--config', calls[0])
        self.assertNotIn(str(first_config), calls[0])
        self.assertNotIn(str(second_config), calls[0])
        self.assertIn('--config-ignore', calls[0])
        self.assertIn('cookies=null', calls[0])

    def assert_cli_rejected(self, arguments):
        with contextlib.redirect_stderr(io.StringIO()), \
                patch.object(x, 'archive_lock', return_value=contextlib.nullcontext()) as lock, \
                patch.object(x, 'BookmarkSync') as sync, \
                self.assertRaises(SystemExit) as raised:
            x.main(arguments)
        self.assertNotEqual(raised.exception.code, 0)
        lock.assert_not_called()
        sync.assert_not_called()

    def test_account_cli_rejects_ambiguous_or_invalid_configuration_before_locking(self):
        prefix = ['--root', str(self.root), '--live']
        path = str(self.root / 'not-read-session.json')
        second_path = str(self.root / 'not-read-second-session.json')
        cases = [
            ['--gallery-config', path, '--account-config', 'primary=' + path],
            ['--account-config', 'primary=' + path, '--account-config', 'primary=' + second_path],
            ['--account-config', 'primary=' + path, '--account-config', 'secondary=' + path],
            ['--account-config', path],
            ['--account-config', 'primary='],
            ['--account-config', 'bad/name=' + path],
            ['--account-config', '_invalid=' + path],
            ['--account-config', ('a' * 33) + '=' + path],
            ['--account-config', 'primary=' + path, '--max-posts', '2'],
        ]
        for case in cases:
            with self.subTest(arguments=case):
                self.assert_cli_rejected(prefix + case)
        self.assert_cli_rejected(['--root', str(self.root), '--fixture', str(baseline.FIXTURE),
                                  '--fixture-media', str(self.root),
                                  '--account-config', 'primary=' + path])

    def test_cli_builds_separate_session_adapters_and_keeps_legacy_single_entry(self):
        first_path = self.root / 'not-read-first-session.json'
        second_path = self.root / 'not-read-second-session.json'
        primary = baseline.FakeAdapter(messages_for())
        secondary = baseline.FakeAdapter(messages_for())
        report = {'scan_complete': True, 'sample_complete': False, 'pending_retry': 0,
                  'phases': {phase: 0 for phase in
                             ('discovered', 'downloaded', 'queued', 'archived', 'published')}}
        with patch.object(x, 'GalleryDL', side_effect=[primary, secondary]) as gallery, \
                patch.object(x, 'MultiGalleryDL') as multi, \
                patch.object(x, 'BookmarkSync') as sync, \
                patch.object(x, 'archive_lock', return_value=contextlib.nullcontext()):
            sync.return_value.run.return_value = report
            result = x.main(['--root', str(self.root), '--live',
                             '--account-config', 'primary=' + str(first_path),
                             '--account-config', 'secondary=' + str(second_path)])
        self.assertEqual(result, 0)
        self.assertEqual([call.args[0] for call in gallery.call_args_list],
                         [first_path, second_path])
        self.assertEqual(multi.call_args.args[0],
                         [('primary', primary), ('secondary', secondary)])
        with patch.object(x, 'GalleryDL', return_value=primary) as gallery, \
                patch.object(x, 'MultiGalleryDL') as multi, \
                patch.object(x, 'BookmarkSync') as sync, \
                patch.object(x, 'archive_lock', return_value=contextlib.nullcontext()):
            sync.return_value.run.return_value = report
            result = x.main(['--root', str(self.root), '--live',
                             '--gallery-config', str(first_path)])
        self.assertEqual(result, 0)
        gallery.assert_called_once()
        multi.assert_not_called()


if __name__ == '__main__':
    unittest.main()
