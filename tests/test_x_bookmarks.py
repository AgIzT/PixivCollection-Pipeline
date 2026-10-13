"""Offline regression tests: synthetic images, stubbed account and R2 adapters."""

import contextlib
import copy
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[1] / 'pixiv_collection'
sys.path.insert(0, str(PROJECT))

# The production crawler dependencies are optional for offline verification.
# Pillow image processing is real; account and logging integrations are stubbed.
class NoopLogger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class ForbiddenPixivAPI:
    def __init__(self, *args, **kwargs):
        raise AssertionError('No account access is allowed in offline tests')


sys.modules['loguru'] = types.SimpleNamespace(logger=NoopLogger())
sys.modules['pixivpy3'] = types.SimpleNamespace(AppPixivAPI=ForbiddenPixivAPI)

import archive_io
import collection
import ingest
import pipeline
import receiver
import x_bookmarks as x
from PIL import Image

FIXTURE = Path(__file__).parent / 'fixtures' / 'gallery_bookmarks.json'
MESSAGES = json.loads(FIXTURE.read_text('utf-8'))
POST_ID = str(MESSAGES[0][1]['tweet_id'])


class FakeAdapter:
    def __init__(self, messages=None):
        self.messages = copy.deepcopy(MESSAGES if messages is None else messages)
        self.complete = True
        self.downloads = []
        self.fail_once = set()
        self.discovery_error = False

    def discover(self):
        if self.discovery_error:
            raise x.SyncError('Synthetic discovery failure')
        return x.Discovery(copy.deepcopy(self.messages), self.complete)

    def download(self, url, destination):
        self.downloads.append(url)
        if url in self.fail_once:
            self.fail_once.remove(url)
            raise x.SyncError('Synthetic download failure')
        Image.new('RGB', (64, 48), (45, 120, 180)).save(destination, 'PNG')


class FakePublisher:
    def __init__(self, fail=False, stale=False):
        self.fail, self.stale = fail, stale
        self.calls = 0

    def __call__(self, root, publication_id):
        self.calls += 1
        def uploaded(args, archive_root):
            return {'created_at': x.now(), 'dry_run': False,
                    'uploaded': [], 'errors': ['synthetic failure'] if self.fail else [],
                    'local_files': len(list(pipeline.iter_publish_files(archive_root)))}
        try:
            with patch.object(pipeline, 'load_default_env_files'), patch.dict(os.environ, {}, clear=True), \
                    patch.object(pipeline, 'upload_to_r2', uploaded), \
                    patch.object(ingest, 'disk_state', return_value={'level': 'ok'}):
                result = pipeline.main(['--root', str(root), '--ingest-only',
                                        '--publication-id', publication_id])
        except RuntimeError:
            return False
        if self.stale:
            report = json.loads((root / 'upload_report.json').read_text('utf-8'))
            report['publication_id'] = 'stale-fixture-receipt'
            archive_io.atomic_json(root / 'upload_report.json', report)
        return result == 0


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('PIXIV_TEST_TMP'))
        self.root = Path(self.temp.name)
        self.network = patch.object(socket, 'socket', side_effect=AssertionError('Network prohibited'))
        self.network.start()
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.network.stop()
        self.temp.cleanup()

    def sync(self, adapter=None, publisher=None):
        return x.BookmarkSync(self.root, adapter or FakeAdapter(), publisher)

    def state(self):
        return json.loads((self.root / '.x-sync' / 'state.json').read_text('utf-8'))

    def test_author_links_large_id_and_utc_metadata(self):
        manifest = x.manifest_for(MESSAGES[0][1], 'fallback')
        self.assertEqual(manifest['source_id'], POST_ID)
        self.assertEqual(manifest['author']['source_uid'], '10001')
        self.assertEqual(manifest['author']['name'], 'Synthetic author')
        self.assertEqual(manifest['source_url'], 'https://x.com/FixtureAuthor/status/' + POST_ID)
        self.assertEqual(manifest['created_at'], '2020-01-01T12:00:00+00:00')

    def test_float_identifier_is_rejected(self):
        with self.assertRaises(x.SyncError):
            x.numeric_id(float(POST_ID))

    def test_complete_multiphase_backup_and_part_deduplication(self):
        adapter = FakeAdapter(MESSAGES + [MESSAGES[1]])
        report = self.sync(adapter, FakePublisher()).run()
        self.assertEqual(report['phases']['published'], 1)
        self.assertEqual(len(adapter.downloads), 2)
        images = json.loads((self.root / 'images.json').read_text('utf-8'))
        self.assertEqual({i['part'] for i in images}, {0, 1})
        self.assertEqual(len({i['id'] for i in images}), 1)
        self.assertEqual(len(list((self.root / 'image' / 'original').iterdir())), 2)
        self.assertEqual(images[0]['author_url'], 'https://x.com/FixtureAuthor')

    def test_failed_second_image_retries_without_redownloading_first(self):
        adapter = FakeAdapter()
        adapter.fail_once.add(MESSAGES[2][1])
        first = self.sync(adapter).run()
        self.assertEqual(first['pending_retry'], 1)
        self.assertFalse((self.root / 'pending').exists())
        second = self.sync(adapter).run()
        self.assertEqual(second['phases']['queued'], 1)
        self.assertEqual(adapter.downloads.count(MESSAGES[1][1]), 1)
        self.assertEqual(adapter.downloads.count(MESSAGES[2][1]), 2)

    def test_retry_survives_unbookmarking_or_discovery_failure(self):
        adapter = FakeAdapter()
        adapter.fail_once.add(MESSAGES[2][1])
        self.sync(adapter).run()
        adapter.messages = []
        adapter.discovery_error = True
        report = self.sync(adapter, FakePublisher()).run()
        self.assertFalse(report['scan_complete'])
        self.assertEqual(report['phases']['published'], 1)

    def test_unbookmarking_never_deletes_published_archive(self):
        self.sync(publisher=FakePublisher()).run()
        old_collection = (self.root / 'collection.json').read_bytes()
        old_files = {p.name: p.read_bytes() for p in (self.root / 'image' / 'original').iterdir()}
        report = self.sync(FakeAdapter([]), FakePublisher()).run()
        self.assertEqual(report['phases']['published'], 1)
        self.assertEqual(old_collection, (self.root / 'collection.json').read_bytes())
        self.assertEqual(old_files, {p.name: p.read_bytes() for p in (self.root / 'image' / 'original').iterdir()})

    def test_old_post_new_bookmark_after_first_known_post_is_not_skipped(self):
        self.sync(publisher=FakePublisher()).run()
        older = copy.deepcopy(MESSAGES)
        for message in older:
            message[-1]['tweet_id'] = 100
            message[-1]['date'] = '2010-01-01 00:00:00'
        report = self.sync(FakeAdapter(MESSAGES + older), FakePublisher()).run()
        self.assertEqual(report['phases']['published'], 2)
        self.assertIn('twitter:100', json.loads((self.root / 'collection.json').read_text('utf-8'))['sources'])

    def test_partial_page_preserves_discovered_post_for_retry(self):
        adapter = FakeAdapter(MESSAGES[:2] + [[-1, {'error': 'synthetic'}]])
        adapter.complete = False
        report = self.sync(adapter).run()
        self.assertFalse(report['scan_complete'])
        self.assertIsNone(self.state()['last_complete_scan'])
        self.assertEqual(report['pending_retry'], 1)
        self.assertFalse((self.root / 'pending').exists())
        adapter.messages, adapter.complete = copy.deepcopy(MESSAGES), True
        self.assertEqual(self.sync(adapter).run()['phases']['queued'], 1)

    def test_upload_failure_keeps_archive_and_retries_publication(self):
        adapter = FakeAdapter()
        first = self.sync(adapter, FakePublisher(fail=True)).run()
        self.assertEqual(first['phases']['archived'], 1)
        self.assertEqual(first['phases']['published'], 0)
        second = self.sync(FakeAdapter([]), FakePublisher()).run()
        self.assertEqual(second['phases']['published'], 1)

    def test_stale_publication_receipt_is_not_success(self):
        report = self.sync(publisher=FakePublisher(stale=True)).run()
        self.assertEqual(report['phases']['archived'], 1)
        self.assertFalse(report['publication_confirmed'])

    def test_consumed_marker_without_registry_is_not_known(self):
        path = self.root / 'pending' / 'consumed'
        path.mkdir(parents=True)
        (path / ('twitter-' + POST_ID + '.json')).write_text('{}')
        self.assertIsNone(receiver.already_known(self.root, 'twitter', POST_ID))

    def test_failed_metadata_save_does_not_consume_queue(self):
        self.sync().run()
        pipeline.ensure_dirs(self.root)
        with patch.object(collection.PixivCollection, 'save_data', side_effect=OSError('synthetic disk failure')), \
                patch.object(ingest, 'disk_state', return_value={'level': 'ok'}):
            with self.assertRaises(OSError):
                pipeline.run_ingest(types.SimpleNamespace(), self.root)
        self.assertTrue((self.root / 'pending' / ('twitter-' + POST_ID + '.json')).exists())
        self.assertFalse((self.root / 'pending' / 'consumed').exists())

    def test_export_failure_recovery_regenerates_export_without_duplicate(self):
        self.sync().run()
        pipeline.ensure_dirs(self.root)
        with patch.object(collection.PixivCollection, 'export', side_effect=OSError('synthetic export failure')), \
                patch.object(ingest, 'disk_state', return_value={'level': 'ok'}):
            with self.assertRaises(OSError):
                pipeline.run_ingest(types.SimpleNamespace(), self.root)
        self.assertTrue((self.root / 'collection.json').exists())
        self.assertFalse((self.root / 'images.json').exists())
        self.assertTrue((self.root / 'pending' / ('twitter-' + POST_ID + '.json')).exists())
        report = self.sync(FakeAdapter([]), FakePublisher()).run()
        self.assertEqual(report['phases']['published'], 1)
        self.assertEqual(len(json.loads((self.root / 'images.json').read_text('utf-8'))), 2)
        self.assertEqual(len(list((self.root / 'image' / 'original').iterdir())), 2)

    def test_atomic_write_failure_preserves_previous_metadata(self):
        path = self.root / 'metadata.json'
        path.write_text('{"previous":true}')
        with patch.object(archive_io.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                archive_io.atomic_json(path, {'new': True})
        self.assertEqual(path.read_text(), '{"previous":true}')
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_same_archive_lock_rejects_parallel_run(self):
        with x.archive_lock(self.root):
            with self.assertRaises(x.SyncError):
                with x.archive_lock(self.root):
                    self.fail('Concurrent lock unexpectedly acquired')

    def test_gallery_cli_zero_exit_error_message_is_incomplete(self):
        config = self.root / 'gallery-dl.local.json'
        config.write_text('{}')
        adapter = x.GalleryDL(config)
        calls = []
        def run(command):
            calls.append(command)
            output = '1.32.16\n' if '--version' in command else json.dumps(MESSAGES + [[-1, {'error': 'synthetic'}]])
            return subprocess.CompletedProcess(command, 0, output, 'private output must never be echoed')
        with patch.object(adapter, 'run', side_effect=run):
            result = adapter.discover()
        self.assertFalse(result.complete)
        self.assertIn('--config-ignore', calls[1])
        self.assertIn('cursor=null', calls[1])
        self.assertIn('date-after=null', calls[1])
        self.assertIn('cookies-from-browser=null', calls[1])
        self.assertIn('image-range=null', calls[1])
        self.assertIn('blacklist-tags=null', calls[1])
        self.assertIn('text-tweets=false', calls[1])
        self.assertIn('transform=true', calls[1])
        self.assertNotIn('--abort', calls[1])

    def test_gallery_version_is_pinned(self):
        adapter = x.GalleryDL(self.root / 'private.json')
        with patch.object(adapter, 'run', return_value=subprocess.CompletedProcess([], 0, 'old-version', '')):
            with self.assertRaises(x.SyncError):
                adapter.discover()

    def test_media_download_never_receives_account_config(self):
        config = self.root / 'private.json'
        adapter = x.GalleryDL(config)
        adapter.verified = True
        destination = self.root / 'image.download'
        def run(command):
            self.assertNotIn('--config', command)
            self.assertNotIn(str(config), command)
            self.assertIn('--config-ignore', command)
            self.assertIn('cookies=null', command)
            self.assertIn('--no-skip', command)
            destination.write_bytes(b'synthetic-output')
            return subprocess.CompletedProcess(command, 0, '', '')
        with patch.object(adapter, 'run', side_effect=run):
            adapter.download(MESSAGES[1][1], destination)

    def test_unsafe_media_and_cache_paths_are_rejected(self):
        with self.assertRaises(x.SyncError):
            x.media_url('https://evil.invalid/image.png')
        with self.assertRaises(x.SyncError):
            x.media_url('https://user:password@pbs.twimg.com/media/fixture?format=png')
        self.sync().discover()
        sync = self.sync()
        item = sync.state['items'][POST_ID]
        item['media']['1']['file'] = '../escape.png'
        with self.assertRaises(x.SyncError):
            sync.queue(POST_ID, item)

    def test_live_mode_requires_explicit_config(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            x.main(['--root', str(self.root), '--live'])

    def test_archive_mode_is_preserved_by_atomic_replacement(self):
        path = self.root / 'metadata.json'
        path.write_text('{}')
        old_mode = path.stat().st_mode & 0o777
        archive_io.atomic_json(path, {'replacement': True})
        self.assertEqual(path.stat().st_mode & 0o777, old_mode)

    def test_ack_interruption_after_directory_move_is_recoverable(self):
        self.sync().run()
        pipeline.ensure_dirs(self.root)
        original_rename = Path.rename
        def rename(path, destination):
            if path.name == 'twitter-' + POST_ID + '.json':
                raise OSError('Synthetic interruption before manifest acknowledgment')
            return original_rename(path, destination)
        with patch.object(Path, 'rename', rename), patch.object(ingest, 'disk_state', return_value={'level': 'ok'}):
            with self.assertRaises(OSError):
                pipeline.run_ingest(types.SimpleNamespace(), self.root)
        report = self.sync(FakeAdapter([]), FakePublisher()).run()
        self.assertEqual(report['phases']['published'], 1)
        self.assertFalse((self.root / 'pending' / ('twitter-' + POST_ID + '.json')).exists())
        self.assertEqual(len(list((self.root / 'image' / 'original').iterdir())), 2)

    def test_corrupt_cached_image_is_downloaded_again(self):
        adapter = FakeAdapter()
        adapter.fail_once.add(MESSAGES[2][1])
        self.sync(adapter).run()
        (self.root / '.x-sync' / 'downloads' / POST_ID / '1.png').write_bytes(b'corrupt synthetic fixture')
        self.assertEqual(self.sync(adapter).run()['phases']['queued'], 1)
        self.assertEqual(adapter.downloads.count(MESSAGES[1][1]), 2)

    def test_partial_export_cannot_acknowledge_multiphoto_queue(self):
        self.sync().run()
        pipeline.ensure_dirs(self.root)
        original_export = collection.PixivCollection.export
        def partial_export(client, path):
            original_export(client, path)
            images = json.loads(Path(path).read_text('utf-8'))
            archive_io.atomic_json(path, images[:1])
        with patch.object(collection.PixivCollection, 'export', partial_export), \
                patch.object(ingest, 'disk_state', return_value={'level': 'ok'}):
            with self.assertRaises(RuntimeError):
                pipeline.run_ingest(types.SimpleNamespace(), self.root)
        self.assertTrue((self.root / 'pending' / ('twitter-' + POST_ID + '.json')).exists())
        self.assertEqual(self.sync(FakeAdapter([]), FakePublisher()).run()['phases']['published'], 1)

    def test_ingest_failure_requeues_cached_images_after_unbookmarking(self):
        adapter = FakeAdapter()
        with patch.object(ingest, 'derive_variants', side_effect=OSError('Synthetic processing failure')):
            first = self.sync(adapter, FakePublisher()).run()
        self.assertEqual(first['phases']['published'], 0)
        self.assertTrue((self.root / 'pending' / 'failed' / ('twitter-' + POST_ID + '.json')).exists())
        adapter.messages = []
        self.assertEqual(self.sync(adapter, FakePublisher()).run()['phases']['published'], 1)
        self.assertEqual(len(adapter.downloads), 2)

    def test_offline_fixture_cli_queues_synthetic_images_only(self):
        media = self.root / 'fixture-media'
        media.mkdir()
        for name in ['fixture_one.png', 'fixture_two.png']:
            Image.new('RGB', (64, 48), (45, 120, 180)).save(media / name)
        self.assertEqual(x.main(['--root', str(self.root), '--fixture', str(FIXTURE),
                                 '--fixture-media', str(media)]), 0)
        self.assertEqual(self.state()['items'][POST_ID]['phase'], 'queued')


if __name__ == '__main__':
    unittest.main()
