"""Offline scheduler checks: no account access, real local locks and fixtures."""

import contextlib
import datetime as dt
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_x_bookmarks as baseline
import x_bookmarks_daily as daily

x = baseline.x


class Clock:
    def __init__(self, hour=3, minute=20, second=0, day=13):
        self.moment = dt.datetime(2026, 10, day, hour, minute, second, tzinfo=daily.BEIJING)
        self.pauses = []

    def now(self):
        return self.moment

    def sleep(self, seconds):
        self.pauses.append(seconds)
        self.moment += dt.timedelta(seconds=seconds)


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("PIXIV_TEST_TMP"))
        self.root = Path(self.temp.name)
        self.work = self.root / ".x-sync"
        self.clock = Clock()
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.out = contextlib.redirect_stdout(self.stdout)
        self.err = contextlib.redirect_stderr(self.stderr)
        self.out.__enter__()
        self.err.__enter__()
        self.network = patch.object(socket, "socket", side_effect=AssertionError("Network prohibited"))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.err.__exit__(None, None, None)
        self.out.__exit__(None, None, None)
        self.temp.cleanup()

    def run_daily(self, main, args=None):
        return daily.run_daily(
            self.root, args or ["--live"], clock=self.clock.now,
            sleep=self.clock.sleep, sync_main=main,
        )

    def report(self):
        return json.loads((self.work / "daily-report.json").read_text("utf-8"))

    def state(self):
        return json.loads((self.work / "daily-state.json").read_text("utf-8"))

    def test_outside_window_does_not_start_main_or_mark_an_attempt(self):
        calls = []

        def forbidden_main(*args, **kwargs):
            calls.append(True)
            raise AssertionError("Must not access an account")

        for hour in (2, 6):
            self.clock.moment = self.clock.moment.replace(hour=hour)
            self.assertEqual(self.run_daily(forbidden_main), 0)
            self.assertEqual(self.report()["outcome"], "outside_window")
        self.assertEqual(calls, [])
        self.assertFalse((self.work / "daily-state.json").exists())

    def test_pipeline_lock_callback_rechecks_after_crossing_six(self):
        self.clock = Clock(hour=5, minute=59, second=59)
        discoveries = []

        def locked_main(argv, before_sync):
            with x.archive_lock(self.root):
                self.clock.moment += dt.timedelta(seconds=1)
                if not before_sync():
                    return 0
                discoveries.append(True)
                return 0

        self.assertEqual(self.run_daily(locked_main), 0)
        self.assertEqual(discoveries, [])
        self.assertEqual(self.report()["outcome"], "outside_window")
        self.assertFalse((self.work / "daily-state.json").exists())

    def test_archive_contention_retries_then_runs_only_one_daily_batch(self):
        calls, discoveries = [], []
        secret = "synthetic-secret-not-for-logs"

        def contended_main(argv, before_sync):
            calls.append(True)
            if len(calls) < 3:
                raise x.ArchiveBusy(secret)
            with x.archive_lock(self.root):
                self.assertTrue(before_sync())
                discoveries.append(True)
                x.atomic_json(self.work / "report.json", {
                    "phases": {"published": 2, "queued": 0, "archived": secret},
                    "downloaded_bytes": 1200, "pending_retry": 0,
                    "scan_complete": True, "publication_confirmed": True,
                    "raw_exception": secret, "argv": argv,
                })
                return 0

        self.assertEqual(self.run_daily(contended_main), 0)
        self.assertEqual(self.clock.pauses, [60, 60])
        self.assertEqual(discoveries, [True])
        self.assertEqual(self.report()["phases"], {"published": 2, "queued": 0})
        self.assertEqual(self.state()["attempts"]["2026-10-13"]["outcome"], "success")
        self.assertEqual(self.run_daily(contended_main), 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.report()["outcome"], "already_attempted")
        diagnostics = self.stdout.getvalue() + self.stderr.getvalue()
        diagnostics += (self.work / "daily-state.json").read_text("utf-8")
        diagnostics += (self.work / "daily-report.json").read_text("utf-8")
        self.assertNotIn(secret, diagnostics)

    def test_archive_contention_stops_at_six_without_spending_the_attempt(self):
        self.clock = Clock(hour=5, minute=59, second=45)
        calls = []

        def busy_main(argv, before_sync):
            calls.append(True)
            raise x.ArchiveBusy("Another archive task is active")

        self.assertEqual(self.run_daily(busy_main), 0)
        self.assertEqual(calls, [True])
        self.assertEqual(self.clock.pauses, [15])
        self.assertEqual(self.clock.moment.hour, 6)
        self.assertEqual(self.report()["outcome"], "archive_busy_window_closed")
        self.assertFalse((self.work / "daily-state.json").exists())

    def test_failed_batch_is_redacted_not_repeated_and_next_date_may_run(self):
        secret = "synthetic-auth-token-DO-NOT-LOG"
        calls = []

        def broken_main(argv, before_sync):
            with x.archive_lock(self.root):
                self.assertTrue(before_sync())
                calls.append(True)
                print(secret)
                print(secret, file=sys.stderr)
                raise ConnectionError(secret)

        self.assertEqual(self.run_daily(broken_main), 1)
        self.assertEqual(self.report()["outcome"], "failed")
        self.assertEqual(self.state()["attempts"]["2026-10-13"]["outcome"], "failed")
        self.assertEqual(self.run_daily(broken_main), 0)
        self.assertEqual(calls, [True])
        self.assertEqual(self.report()["attempt_outcome"], "failed")
        self.clock.moment += dt.timedelta(days=1)

        def next_day_main(argv, before_sync):
            with x.archive_lock(self.root):
                self.assertTrue(before_sync())
                calls.append(True)
                # Batch started at 03:20 and may safely finish after 06:00.
                self.clock.moment = self.clock.moment.replace(hour=7)
                return 0

        self.assertEqual(self.run_daily(next_day_main), 0)
        self.assertEqual(calls, [True, True])
        self.assertEqual(self.state()["attempts"]["2026-10-14"]["outcome"], "success")
        self.assertEqual(self.report()["outcome"], "success")
        diagnostics = self.stdout.getvalue() + self.stderr.getvalue()
        diagnostics += (self.work / "daily-state.json").read_text("utf-8")
        diagnostics += (self.work / "daily-report.json").read_text("utf-8")
        self.assertNotIn(secret, diagnostics)

    def test_actual_offline_main_runs_with_both_lock_layers(self):
        media = self.root / "synthetic-media"
        media.mkdir()
        for stem in ("fixture_one", "fixture_two"):
            baseline.Image.new("RGB", (64, 48), (10, 90, 180)).save(media / (stem + ".png"))
        args = ["--fixture", str(baseline.FIXTURE), "--fixture-media", str(media)]
        self.assertEqual(self.run_daily(x.main, args), 0)
        self.assertTrue((self.root / ".pipeline.lock").exists())
        self.assertTrue((self.work / "daily.lock").exists())
        self.assertEqual(self.report()["phases"]["queued"], 1)
        self.assertTrue(self.report()["scan_complete"])
        self.assertEqual(len(list((self.root / "pending").glob("*.json"))), 1)

    def test_second_daily_wrapper_does_not_stomp_active_report(self):
        calls = []
        self.work.mkdir()
        x.atomic_json(self.work / "daily-report.json", {"outcome": "running"})

        def forbidden_main(*args, **kwargs):
            calls.append(True)
            return 0

        with daily.daily_lock(self.work):
            self.assertEqual(self.run_daily(forbidden_main), 0)
        self.assertEqual(calls, [])
        self.assertEqual(self.report(), {"outcome": "running"})
        self.assertIn('"outcome": "daily_busy"', self.stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
