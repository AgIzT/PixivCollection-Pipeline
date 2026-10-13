"""Offline quote-bookmark regressions with real image ingestion and export.

Synthetic outer and referenced posts have different artists. Account access
and R2 are stubbed, and sockets are prohibited throughout every test.
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
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import test_x_bookmarks as baseline

x = baseline.x
GiB = 1024 ** 3
DiskUsage = collections.namedtuple('DiskUsage', 'total used free')
REFERENCED_ID = '100'


def quote_messages(wrapper_id, original_id=baseline.POST_ID, different_author=False):
    messages = copy.deepcopy(baseline.MESSAGES)
    for message in messages:
        metadata = message[-1]
        metadata['tweet_id'] = original_id
        metadata['quote_id'] = wrapper_id
        if different_author:
            metadata['author'] = {'id': 20002, 'name': 'QuotedArtist',
                                  'nick': 'Referenced artist'}
            metadata['content'] = 'Synthetic referenced artwork'
    return messages


def direct_context(source_id=baseline.POST_ID, account='FixtureAuthor'):
    return {'kind': 'direct', 'source_id': source_id,
            'source_url': 'https://x.com/' + account + '/status/' + source_id}


def quote_context(wrapper_id):
    return {'kind': 'quote', 'source_id': str(wrapper_id),
            'source_url': 'https://x.com/i/web/status/' + str(wrapper_id)}


class QuoteTests(unittest.TestCase):
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

    def assert_contexts(self, actual, expected):
        self.assertEqual(len(actual), len(expected))
        self.assertEqual({tuple(sorted(item.items())) for item in actual},
                         {tuple(sorted(item.items())) for item in expected})

    def assert_archive_contexts(self, source_id, expected):
        archive = self.collection()
        image_id = archive['sources']['twitter:' + source_id]
        self.assert_contexts(archive['images'][str(image_id)]['data']['bookmark_contexts'],
                             expected)
        parts = [part for part in self.exported() if part['id'] == image_id]
        self.assertTrue(parts)
        for part in parts:
            self.assert_contexts(part['bookmark_contexts'], expected)

    def test_quote_manifest_preserves_original_artist_and_wrapper_context(self):
        metadata = quote_messages(baseline.POST_ID, REFERENCED_ID, True)[0][-1]
        manifest = x.manifest_for(metadata, 'fallback')
        self.assertEqual(manifest['source_id'], REFERENCED_ID)
        self.assertEqual(manifest['author']['source_uid'], '20002')
        self.assertEqual(manifest['author']['name'], 'Referenced artist')
        self.assertEqual(manifest['author']['url'], 'https://x.com/QuotedArtist')
        self.assertEqual(manifest['source_url'],
                         'https://x.com/QuotedArtist/status/' + REFERENCED_ID)
        self.assert_contexts(manifest['bookmark_contexts'],
                             [quote_context(baseline.POST_ID)])

    def test_zero_quote_id_is_a_direct_bookmark_and_zero_context_is_rejected(self):
        for quote_id in (0, '0'):
            with self.subTest(quote_id=quote_id):
                metadata = copy.deepcopy(baseline.MESSAGES[0][-1])
                metadata['quote_id'] = quote_id
                manifest = x.manifest_for(metadata, 'fallback')
                self.assert_contexts(manifest['bookmark_contexts'], [direct_context()])
        self.assert_contexts(x.merge_bookmark_contexts([quote_context('0'), direct_context()]),
                             [direct_context()])

    def test_outer_one_image_and_quoted_two_images_keep_separate_artists(self):
        outer = copy.deepcopy(baseline.MESSAGES[:2])
        for message in outer:
            message[-1]['count'] = 1
        referenced = quote_messages(baseline.POST_ID, REFERENCED_ID, True)
        adapter = baseline.FakeAdapter(outer + referenced)
        report = x.BookmarkSync(self.root, adapter, baseline.FakePublisher()).run()
        self.assertEqual(report['phases']['published'], 2)
        self.assertEqual(len(adapter.downloads), 3)
        archive = self.collection()
        outer_id = archive['sources']['twitter:' + baseline.POST_ID]
        referenced_id = archive['sources']['twitter:' + REFERENCED_ID]
        self.assertNotEqual(outer_id, referenced_id)
        parts = self.exported()
        self.assertEqual({part['part'] for part in parts if part['id'] == outer_id}, {0})
        referenced_parts = [part for part in parts if part['id'] == referenced_id]
        self.assertEqual({part['part'] for part in referenced_parts}, {0, 1})
        for part in referenced_parts:
            self.assertEqual(part['author']['name'], 'Referenced artist')
            self.assertEqual(part['author_url'], 'https://x.com/QuotedArtist')
            self.assertEqual(part['source_url'],
                             'https://x.com/QuotedArtist/status/' + REFERENCED_ID)
        self.assert_archive_contexts(baseline.POST_ID, [direct_context()])
        self.assert_archive_contexts(REFERENCED_ID, [quote_context(baseline.POST_ID)])
        self.assert_contexts(self.state()['items'][REFERENCED_ID]['manifest']['bookmark_contexts'],
                             [quote_context(baseline.POST_ID)])

    def test_duplicate_quote_contexts_merge_before_ingestion(self):
        messages = quote_messages('200') + quote_messages('200') + quote_messages('201')
        adapter = baseline.FakeAdapter(messages)
        report = x.BookmarkSync(self.root, adapter, baseline.FakePublisher()).run()
        self.assertEqual(report['phases']['published'], 1)
        self.assertEqual(len(adapter.downloads), 2)
        expected = [quote_context('200'), quote_context('201')]
        self.assert_contexts(self.state()['items'][baseline.POST_ID]['manifest']['bookmark_contexts'],
                             expected)
        self.assert_archive_contexts(baseline.POST_ID, expected)

    def test_later_quote_context_republishes_metadata_without_redownloading(self):
        adapter = baseline.FakeAdapter()
        publisher = baseline.FakePublisher()
        self.assertEqual(x.BookmarkSync(self.root, adapter, publisher).run()['phases']['published'], 1)
        first_downloads = list(adapter.downloads)
        adapter.messages = quote_messages('200')
        report = x.BookmarkSync(self.root, adapter, publisher).run()
        self.assertEqual(report['phases']['published'], 1)
        self.assertTrue(report['publication_confirmed'])
        self.assertEqual(publisher.calls, 2)
        self.assertEqual(adapter.downloads, first_downloads)
        expected = [direct_context(), quote_context('200')]
        self.assert_contexts(self.state()['items'][baseline.POST_ID]['manifest']['bookmark_contexts'],
                             expected)
        self.assert_archive_contexts(baseline.POST_ID, expected)

    def test_crash_after_metadata_replacement_retries_publication_after_restart(self):
        class SyntheticCrash(BaseException):
            pass

        adapter = baseline.FakeAdapter()
        publisher = baseline.FakePublisher()
        x.BookmarkSync(self.root, adapter, publisher).run()
        first_downloads = list(adapter.downloads)
        adapter.messages = quote_messages('200')
        original_writer = x.atomic_json

        def crash_after_export(path, value):
            original_writer(path, value)
            if Path(path) == self.root / 'images.json':
                raise SyntheticCrash('Interrupted after both metadata replacements')

        with patch.object(x, 'atomic_json', side_effect=crash_after_export), \
                self.assertRaises(SyntheticCrash):
            x.BookmarkSync(self.root, adapter, publisher).run()
        self.assertEqual(publisher.calls, 1)
        self.assertEqual(self.state()['items'][baseline.POST_ID]['phase'], 'archived')
        expected = [direct_context(), quote_context('200')]
        self.assert_archive_contexts(baseline.POST_ID, expected)
        adapter.messages = []
        report = x.BookmarkSync(self.root, adapter, publisher).run()
        self.assertTrue(report['publication_confirmed'])
        self.assertEqual(report['phases']['published'], 1)
        self.assertEqual(publisher.calls, 2)
        self.assertEqual(adapter.downloads, first_downloads)
        self.assert_archive_contexts(baseline.POST_ID, expected)

    def test_duplicate_ingest_manifest_adds_context_without_new_image_ids(self):
        x.BookmarkSync(self.root, baseline.FakeAdapter(), baseline.FakePublisher()).run()
        before_sources = self.collection()['sources']
        metadata = quote_messages('200')[0][-1]
        manifest = x.manifest_for(metadata, 'fallback')
        manifest['files'] = self.state()['items'][baseline.POST_ID]['manifest']['files']
        cache = self.root / '.x-sync' / 'downloads' / baseline.POST_ID
        blobs = [(cache / part['file']).read_bytes() for part in manifest['files']]
        baseline.receiver.stage_item(self.root, manifest, blobs)
        report = baseline.pipeline.run_ingest(types.SimpleNamespace(), self.root)
        self.assertEqual(report['merged'], 0)
        self.assertEqual(report['duplicate'], 1)
        self.assertEqual(self.collection()['sources'], before_sources)
        self.assertEqual(len(list((self.root / 'image' / 'original').iterdir())), 2)
        self.assert_archive_contexts(baseline.POST_ID,
                                     [direct_context(), quote_context('200')])

    def test_empty_source_list_retains_all_existing_contexts(self):
        adapter = baseline.FakeAdapter(baseline.MESSAGES + quote_messages('200'))
        publisher = baseline.FakePublisher()
        x.BookmarkSync(self.root, adapter, publisher).run()
        before = {(self.root / name): (self.root / name).read_bytes()
                  for name in ('collection.json', 'images.json')}
        adapter.messages = []
        x.BookmarkSync(self.root, adapter, publisher).run()
        self.assertEqual(publisher.calls, 1)
        for path, contents in before.items():
            self.assertEqual(path.read_bytes(), contents)
        expected = [direct_context(), quote_context('200')]
        self.assert_contexts(self.state()['items'][baseline.POST_ID]['manifest']['bookmark_contexts'],
                             expected)
        self.assert_archive_contexts(baseline.POST_ID, expected)

    def test_discovery_requests_quoted_images_and_quoted_expansion(self):
        config = self.root / 'gallery-dl.local.json'
        config.write_text('{}', encoding='utf-8')
        adapter = x.GalleryDL(config)
        calls = []

        def run(command):
            calls.append(command)
            output = (x.GALLERY_DL_VERSION + '\n' if '--version' in command
                      else json.dumps(baseline.MESSAGES))
            return subprocess.CompletedProcess(command, 0, output, '')

        with patch.object(adapter, 'run', side_effect=run):
            self.assertTrue(adapter.discover().complete)
        self.assertIn('quoted=true', calls[1])
        self.assertIn('quoted-expand=true', calls[1])


if __name__ == '__main__':
    unittest.main()
