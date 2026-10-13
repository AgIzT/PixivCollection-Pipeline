"""Offline publication ordering with a thread-safe, observable R2 fixture.

Real upload planning and worker execution are tested; the object client has no
network implementation and sockets remain prohibited throughout each test.
"""

import collections
import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import test_x_bookmarks as baseline

pipeline = baseline.pipeline
PREFIX = 'x-smoke/publication/'
RAW_SECRET = 'synthetic-private-R2-key-DO-NOT-REPORT'


class ObservableR2:
    def __init__(self, media_keys, fail_once=(), concurrent_media=False):
        self.media_keys = set(media_keys)
        self.fail_once = set(fail_once)
        self.objects = {}
        self.events = []
        self.premature_metadata = []
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(len(media_keys)) if concurrent_media else None

    def list_objects(self, prefix):
        with self.lock:
            return {key: pipeline.RemoteObject(key, len(data), 'synthetic-etag')
                    for key, data in self.objects.items() if key.startswith(prefix)}

    def put_file(self, key, path):
        with self.lock:
            self.events.append(('start', key))
            if key not in self.media_keys and not self.media_keys <= set(self.objects):
                self.premature_metadata.append(key)
        if key in self.media_keys and self.barrier is not None:
            # A real ThreadPoolExecutor must start all three media workers.
            # This would time out if media writes were incorrectly serialized.
            self.barrier.wait(timeout=5)
        with self.lock:
            if key in self.fail_once:
                self.fail_once.remove(key)
                self.events.append(('failed', key))
                raise RuntimeError(RAW_SECRET)
            self.objects[key] = path.read_bytes()
            self.events.append(('success', key))


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('PIXIV_TEST_TMP'))
        self.root = Path(self.temp.name)
        self.network = patch.object(socket, 'socket',
                                    side_effect=AssertionError('Network prohibited'))
        self.network.start()
        (self.root / 'collection.json').write_text('{"fixture":true}', encoding='utf-8')
        (self.root / 'images.json').write_text('[]', encoding='utf-8')
        for directory, filename, data in (
            ('image/original', '900000000_p0.png', b'synthetic-original'),
            ('image/preview', '900000000_p0.webp', b'synthetic-preview'),
            ('image/thumbnail', '900000000_p0.webp', b'synthetic-thumbnail'),
        ):
            target = self.root / directory / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        self.media_keys = {PREFIX + key for key, _ in pipeline.iter_publish_files(self.root)
                           if key.startswith('image/')}
        self.metadata_keys = [PREFIX + name for name in pipeline.PUBLISH_FILES]

    def tearDown(self):
        self.network.stop()
        self.temp.cleanup()

    def upload(self, client, dry_run=False):
        output = io.StringIO()
        args = types.SimpleNamespace(dry_run_upload=dry_run, upload_workers=3)
        with patch.object(pipeline, 'build_r2_client', return_value=(client, PREFIX)), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            report = pipeline.upload_to_r2(args, self.root)
        return report, output.getvalue()

    def starts(self, client):
        return [key for event, key in client.events if event == 'start']

    def assert_safe_errors(self, report, output):
        self.assertNotIn(RAW_SECRET, json.dumps(report))
        self.assertNotIn(RAW_SECRET, output)
        for error in report['errors']:
            self.assertEqual(error['error'], 'Object upload failed')

    def test_all_concurrent_media_finish_before_ordered_metadata_and_dry_run_writes_nothing(self):
        client = ObservableR2(self.media_keys, concurrent_media=True)
        report, _ = self.upload(client)
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['deferred_metadata'], [])
        self.assertEqual(report['local_files'], 5)
        self.assertEqual(report['planned_uploads'], 5)
        self.assertEqual(len(report['uploaded']), 5)
        self.assertEqual(client.premature_metadata, [])
        events = client.events
        first_metadata_start = min(events.index(('start', key)) for key in self.metadata_keys)
        last_media_success = max(events.index(('success', key)) for key in self.media_keys)
        self.assertLess(last_media_success, first_metadata_start)
        self.assertEqual(self.starts(client)[-2:], self.metadata_keys)
        self.assertLess(events.index(('success', self.metadata_keys[0])),
                        events.index(('start', self.metadata_keys[1])))
        before_events = list(client.events)
        dry_report, _ = self.upload(client, dry_run=True)
        self.assertTrue(dry_report['dry_run'])
        self.assertEqual(dry_report['skipped_same_size'], 3)
        self.assertEqual(dry_report['planned_uploads'], 2)
        self.assertEqual(dry_report['uploaded'], [])
        self.assertEqual(client.events, before_events)

    def test_media_failure_defers_all_metadata_and_never_exposes_raw_error(self):
        failed_key = sorted(self.media_keys)[0]
        client = ObservableR2(self.media_keys, fail_once=[failed_key], concurrent_media=True)
        report, output = self.upload(client)
        self.assertEqual(set(self.starts(client)), self.media_keys)
        self.assertEqual(report['deferred_metadata'], self.metadata_keys)
        self.assertEqual(len(report['errors']), 1)
        self.assertEqual(report['errors'][0]['key'], failed_key)
        self.assertEqual(len(report['uploaded']), 2)
        self.assertTrue(set(client.objects) <= self.media_keys)
        self.assert_safe_errors(report, output)

    def test_retry_uploads_only_failed_media_and_two_metadata_objects(self):
        failed_key = sorted(self.media_keys)[0]
        client = ObservableR2(self.media_keys, fail_once=[failed_key])
        first, first_output = self.upload(client)
        self.assertEqual(first['deferred_metadata'], self.metadata_keys)
        self.assert_safe_errors(first, first_output)
        start_count = len(self.starts(client))
        second, _ = self.upload(client)
        retry_starts = self.starts(client)[start_count:]
        self.assertEqual(retry_starts, [failed_key] + self.metadata_keys)
        self.assertEqual(second['skipped_same_size'], 2)
        self.assertEqual(second['planned_uploads'], 3)
        self.assertEqual(second['errors'], [])
        self.assertEqual(second['deferred_metadata'], [])
        self.assertEqual({item['key'] for item in second['uploaded']},
                         {failed_key, *self.metadata_keys})
        counts = collections.Counter(self.starts(client))
        self.assertEqual(counts[failed_key], 2)
        for good_key in self.media_keys - {failed_key}:
            self.assertEqual(counts[good_key], 1)

    def test_collection_failure_stops_images_metadata_and_retry_keeps_successful_media(self):
        client = ObservableR2(self.media_keys, fail_once=[self.metadata_keys[0]])
        first, output = self.upload(client)
        self.assertEqual(first['deferred_metadata'], [self.metadata_keys[1]])
        self.assertNotIn(self.metadata_keys[1], self.starts(client))
        self.assertEqual(len(first['errors']), 1)
        self.assertEqual(first['errors'][0]['key'], self.metadata_keys[0])
        self.assertEqual(set(client.objects), self.media_keys)
        self.assertEqual(client.premature_metadata, [])
        self.assert_safe_errors(first, output)
        first_start_count = len(self.starts(client))
        second, _ = self.upload(client)
        self.assertEqual(self.starts(client)[first_start_count:], self.metadata_keys)
        self.assertEqual(second['skipped_same_size'], 3)
        self.assertEqual(second['planned_uploads'], 2)
        self.assertEqual(second['errors'], [])
        self.assertEqual(second['deferred_metadata'], [])
        counts = collections.Counter(self.starts(client))
        for media_key in self.media_keys:
            self.assertEqual(counts[media_key], 1)


if __name__ == '__main__':
    unittest.main()
