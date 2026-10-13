"""Offline regressions for bounded X samples and isolated publication.

No account or R2 access is allowed. The existing synthetic CLI protocol and
real Pillow/staging implementation are reused without changing baseline tests.
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
MiB = 1024 ** 2
GiB = 1024 ** 3
DiskUsage = collections.namedtuple('DiskUsage', 'total used free')


class SampleAdapter(baseline.FakeAdapter):
    def __init__(self, messages=None, max_posts=1):
        super().__init__(messages)
        self.max_posts = max_posts

    def discover(self):
        return x.Discovery(copy.deepcopy(self.messages), self.complete,
                           full_scan=False)


def sized_messages(width=64, height=48):
    messages = copy.deepcopy(baseline.MESSAGES)
    for message in messages:
        message[-1]['width'] = width
        message[-1]['height'] = height
    return messages


class IsolationTests(unittest.TestCase):
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

    def test_gallery_sample_limits_posts_and_requires_original_size(self):
        config = self.root / 'gallery-dl.local.json'
        config.write_text('{}', encoding='utf-8')
        adapter = x.GalleryDL(config, max_posts=2)
        calls = []

        def run(command):
            calls.append(command)
            output = (x.GALLERY_DL_VERSION + '\n' if '--version' in command
                      else json.dumps(baseline.MESSAGES))
            return subprocess.CompletedProcess(command, 0, output, '')

        with patch.object(adapter, 'run', side_effect=run):
            discovery = adapter.discover()
        self.assertTrue(discovery.complete)
        self.assertFalse(discovery.full_scan)
        self.assertIn('post-range="1-2"', calls[1])
        self.assertIn('size=["orig"]', calls[1])
        self.assertIn('file-range=null', calls[1])
        self.assertIn('image-range=null', calls[1])

    def test_public_media_download_also_forbids_size_fallback(self):
        adapter = x.GalleryDL(self.root / 'private.json')
        adapter.verified = True
        destination = self.root / 'sample.png.download'
        calls = []

        def run(command):
            calls.append(command)
            destination.write_bytes(b'synthetic-output')
            return subprocess.CompletedProcess(command, 0, '', '')

        with patch.object(adapter, 'run', side_effect=run):
            adapter.download(baseline.MESSAGES[1][1], destination)
        self.assertIn('size=["orig"]', calls[0])
        self.assertNotIn('--config', calls[0])

    def test_generated_commands_are_accepted_by_real_gallery_dl_parser(self):
        try:
            from gallery_dl import option, version
        except ImportError:
            self.skipTest('Optional gallery-dl is not installed')
        self.assertEqual(version.__version__, x.GALLERY_DL_VERSION)
        config = self.root / 'gallery-dl.local.json'
        config.write_text('{}', encoding='utf-8')
        adapter = x.GalleryDL(config, max_posts=5, page_size=2)
        destination = self.root / 'sample.png.download'
        parsed = []

        def run(command):
            if '--version' in command:
                return subprocess.CompletedProcess(command, 0,
                                                   version.__version__ + '\n', '')
            # Use the installed downloader's actual argument definitions. An
            # unsupported generated flag raises SystemExit and fails this test.
            with contextlib.redirect_stderr(io.StringIO()):
                arguments = option.build_parser().parse_args(command[1:])
            parsed.append(arguments)
            if '--dump-json' in command:
                output = json.dumps(baseline.MESSAGES)
            else:
                destination.write_bytes(b'synthetic-output')
                output = ''
            return subprocess.CompletedProcess(command, 0, output, '')

        with patch.object(adapter, 'run', side_effect=run):
            self.assertTrue(adapter.discover().complete)
            adapter.download(baseline.MESSAGES[1][1], destination)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0].urls, [x.BOOKMARK_URL])
        self.assertEqual(parsed[1].urls, [baseline.MESSAGES[1][1]])

    def test_small_page_size_overrides_request_limit_without_cutting_media(self):
        config = self.root / 'gallery-dl.local.json'
        config.write_text('{}', encoding='utf-8')
        adapter = x.GalleryDL(config, max_posts=5, page_size=2)
        calls = []

        def run(command):
            calls.append(command)
            output = (x.GALLERY_DL_VERSION + '\n' if '--version' in command
                      else json.dumps(baseline.MESSAGES))
            return subprocess.CompletedProcess(command, 0, output, '')

        with patch.object(adapter, 'run', side_effect=run):
            result = adapter.discover()
        self.assertTrue(result.complete)
        self.assertFalse(result.full_scan)
        self.assertIn('limit=2', calls[1])
        self.assertIn('post-range="1-5"', calls[1])
        self.assertIn('file-range=null', calls[1])
        self.assertIn('metadata-extractor=null', calls[1])
        self.assertIn('metadata-http=null', calls[1])
        self.assertEqual(len([m for m in result.messages if m[0] == 3]), 2)

    def test_sample_completeness_never_advances_full_scan_checkpoint(self):
        report = x.BookmarkSync(self.root, SampleAdapter()).run()
        self.assertEqual(report['mode'], 'sample')
        self.assertTrue(report['sample_complete'])
        self.assertFalse(report['scan_complete'])
        self.assertIsNone(self.state()['last_complete_scan'])
        self.assertEqual(report['phases']['queued'], 1)

    def test_fixture_sample_also_marks_discovery_as_bounded(self):
        adapter = x.FixtureAdapter(baseline.FIXTURE, self.root)
        adapter.max_posts = 1
        self.assertFalse(adapter.discover().full_scan)

    def test_sample_retry_retains_selection_after_unbookmarking(self):
        adapter = SampleAdapter()
        adapter.fail_once.add(baseline.MESSAGES[2][1])
        first = x.BookmarkSync(self.root, adapter).run()
        self.assertEqual(first['pending_retry'], 1)
        adapter.messages = []
        second = x.BookmarkSync(self.root, adapter).run()
        self.assertEqual(second['phases']['queued'], 1)
        self.assertEqual(adapter.downloads.count(baseline.MESSAGES[1][1]), 1)
        self.assertEqual(adapter.downloads.count(baseline.MESSAGES[2][1]), 2)

    def test_sample_does_not_download_preexisting_out_of_scope_posts(self):
        older = copy.deepcopy(baseline.MESSAGES)
        for message in older:
            message[-1]['tweet_id'] = 100
        x.BookmarkSync(self.root,
                       baseline.FakeAdapter(baseline.MESSAGES + older)).discover()
        adapter = SampleAdapter()
        report = x.BookmarkSync(self.root, adapter).run()
        items = self.state()['items']
        self.assertEqual(items['100']['phase'], 'discovered')
        self.assertEqual(items[baseline.POST_ID]['phase'], 'queued')
        self.assertEqual(len(adapter.downloads), 2)
        self.assertFalse((self.root / 'pending' / 'twitter-100.json').exists())
        self.assertTrue(report['sample_complete'])

    def test_original_dimensions_are_recorded_and_checked_for_every_part(self):
        adapter = baseline.FakeAdapter(sized_messages())
        report = x.BookmarkSync(self.root, adapter).run()
        self.assertEqual(report['phases']['queued'], 1)
        item = self.state()['items'][baseline.POST_ID]
        self.assertEqual(set(item['media']), {'1', '2'})
        for media in item['media'].values():
            self.assertEqual(media['source_dimensions'], [64, 48])
            self.assertEqual(media['actual_dimensions'], [64, 48])
            self.assertIn('sha256', media)

    def test_smaller_valid_image_is_not_accepted_as_an_original(self):
        adapter = baseline.FakeAdapter(sized_messages(128, 96))
        report = x.BookmarkSync(self.root, adapter).run()
        self.assertEqual(report['pending_retry'], 1)
        self.assertEqual(report['phases']['queued'], 0)
        self.assertFalse((self.root / 'pending' /
                          ('twitter-' + baseline.POST_ID + '.json')).exists())

    def test_strict_live_adapter_rejects_old_cache_without_source_dimensions(self):
        x.BookmarkSync(self.root, baseline.FakeAdapter()).discover()
        cache = self.root / '.x-sync' / 'downloads' / baseline.POST_ID
        cache.mkdir(parents=True)
        for number in (1, 2):
            baseline.Image.new('RGB', (64, 48), (45, 120, 180)).save(
                cache / (str(number) + '.png'))
        before = {path.name: path.read_bytes() for path in cache.iterdir()}
        adapter = baseline.FakeAdapter([])
        adapter.strict_dimensions = True
        report = x.BookmarkSync(self.root, adapter).run()
        self.assertEqual(report['pending_retry'], 1)
        self.assertEqual(report['phases']['queued'], 0)
        self.assertEqual(adapter.downloads, [])
        self.assertEqual(before,
                         {path.name: path.read_bytes() for path in cache.iterdir()})
        self.assertFalse((self.root / 'pending').exists())

    def test_disk_reserve_is_checked_before_any_download(self):
        adapter = baseline.FakeAdapter()
        with patch('shutil.disk_usage',
                   return_value=DiskUsage(100 * GiB, 95 * GiB, 5 * GiB + 49 * MiB)):
            report = x.BookmarkSync(self.root, adapter,
                                    min_free_bytes=5 * GiB).run()
        self.assertEqual(adapter.downloads, [])
        self.assertEqual(report['pending_retry'], 1)
        self.assertFalse((self.root / 'pending').exists())

    def test_download_budget_stops_work_before_network_or_staging(self):
        adapter = baseline.FakeAdapter()
        report = x.BookmarkSync(self.root, adapter,
                                max_download_bytes=49 * MiB).run()
        self.assertEqual(adapter.downloads, [])
        self.assertEqual(report['pending_retry'], 1)
        self.assertEqual(report['phases']['queued'], 0)
        self.assertFalse((self.root / 'pending').exists())

    def test_cached_part_does_not_consume_next_run_download_budget(self):
        adapter = baseline.FakeAdapter()
        adapter.fail_once.add(baseline.MESSAGES[2][1])
        first = x.BookmarkSync(self.root, adapter,
                               max_download_bytes=100 * MiB).run()
        self.assertEqual(first['pending_retry'], 1)
        second = x.BookmarkSync(self.root, adapter,
                                max_download_bytes=50 * MiB).run()
        self.assertEqual(second['phases']['queued'], 1)
        self.assertEqual(adapter.downloads.count(baseline.MESSAGES[1][1]), 1)
        self.assertEqual(adapter.downloads.count(baseline.MESSAGES[2][1]), 2)

    def test_isolated_publisher_passes_explicit_r2_target(self):
        publisher = x.PipelinePublisher(r2_prefix='x-smoke/regression/',
                                        r2_bucket='isolated-fixture-bucket')
        with patch.object(x.subprocess, 'run',
                          return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertTrue(publisher(self.root, 'fixture-receipt'))
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--r2-prefix') + 1],
                         'x-smoke/regression/')
        self.assertEqual(command[command.index('--r2-bucket') + 1],
                         'isolated-fixture-bucket')
        self.assertEqual(command[command.index('--publication-id') + 1],
                         'fixture-receipt')
        self.assertIn('--ingest-only', command)

    def test_out_of_scope_pending_item_blocks_sample_publication(self):
        older = copy.deepcopy(baseline.MESSAGES)
        for message in older:
            message[-1]['tweet_id'] = 100
        x.BookmarkSync(self.root, baseline.FakeAdapter(older)).run()
        old_manifest = self.root / 'pending' / 'twitter-100.json'
        before = old_manifest.read_bytes()
        publisher = baseline.FakePublisher()
        report = x.BookmarkSync(self.root, SampleAdapter(), publisher).run()
        self.assertTrue(report['publication_blocked'])
        self.assertFalse(report['publication_confirmed'])
        self.assertEqual(publisher.calls, 0)
        self.assertEqual(old_manifest.read_bytes(), before)
        self.assertEqual(self.state()['items']['100']['phase'], 'queued')
        self.assertFalse((self.root / 'collection.json').exists())

    def assert_cli_rejected_before_sync(self, *extra, root=None):
        args = ['--root', str(root or self.root), '--live',
                '--gallery-config', str(self.root / 'private.json')]
        with contextlib.redirect_stderr(io.StringIO()), \
                patch.object(x, 'BookmarkSync') as sync, \
                patch.object(x, 'archive_lock', return_value=contextlib.nullcontext()) as lock, \
                self.assertRaises(SystemExit) as raised:
            x.main(args + list(extra))
        self.assertNotEqual(raised.exception.code, 0)
        sync.assert_not_called()
        lock.assert_not_called()

    def test_sample_publish_requires_explicit_smoke_prefix(self):
        self.assert_cli_rejected_before_sync('--max-posts', '2', '--publish')
        self.assert_cli_rejected_before_sync('--max-posts', '2', '--publish',
                                             '--r2-prefix', 'production/')

    def test_sample_publish_rejects_production_root(self):
        for root in (Path(x.__file__).parent,
                     Path(x.__file__).parent / 'unsafe-sample'):
            with self.subTest(root=str(root)):
                self.assert_cli_rejected_before_sync('--max-posts', '2', '--publish',
                                                     '--r2-prefix', 'x-smoke/regression/',
                                                     root=root)

    def test_sample_post_limit_is_bounded(self):
        for count in ('0', '11'):
            with self.subTest(count=count):
                self.assert_cli_rejected_before_sync('--max-posts', count)

    def test_page_size_cli_bounds_and_sample_default(self):
        for count in ('0', '51'):
            with self.subTest(invalid_page_size=count):
                self.assert_cli_rejected_before_sync('--max-posts', '2',
                                                     '--page-size', count)
        report = {'sample_complete': True, 'scan_complete': False,
                  'pending_retry': 0,
                  'phases': {phase: 0 for phase in
                             ('discovered', 'downloaded', 'queued', 'archived', 'published')}}
        for extra, expected in (([], 2), (['--page-size', '4'], 4)):
            with self.subTest(expected_page_size=expected), \
                    patch.object(x, 'GalleryDL') as gallery, \
                    patch.object(x, 'BookmarkSync') as sync, \
                    patch.object(x, 'archive_lock', return_value=contextlib.nullcontext()):
                sync.return_value.run.return_value = report
                result = x.main(['--root', str(self.root), '--live', '--max-posts', '2',
                                 '--gallery-config', str(self.root / 'private.json')] + extra)
                self.assertEqual(result, 0)
                self.assertEqual(gallery.call_args.kwargs['page_size'], expected)
                self.assertEqual(gallery.call_args.kwargs['max_posts'], 2)


if __name__ == '__main__':
    unittest.main()
