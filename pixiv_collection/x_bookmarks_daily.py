"""Run one X archive batch per Beijing date, starting only inside its window.

The daily lock is separate from the pipeline lock held by x_bookmarks.main.
The locked callback records the attempt before any account request.  A started
batch may finish after the window closes; failed batches resume on a later day.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import x_bookmarks as x


BEIJING = ZoneInfo("Asia/Shanghai")


class DailyBusy(RuntimeError):
    """Another daily wrapper is active; contains no caller-supplied data."""


class DailyStateError(RuntimeError):
    """Do not risk a duplicate batch when its durable state is unreadable."""


class SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, "Invalid daily scheduler arguments\n")


def beijing_now() -> dt.datetime:
    return dt.datetime.now(BEIJING)


def local_time(clock: Callable[[], dt.datetime]) -> dt.datetime:
    moment = clock()
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise DailyStateError("Scheduler clock requires a timezone")
    return moment.astimezone(BEIJING)


def in_window(moment: dt.datetime, start: int, end: int) -> bool:
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + dt.timedelta(hours=start) <= moment < midnight + dt.timedelta(hours=end)


@contextlib.contextmanager
def daily_lock(work: Path):
    """Prevent two wrappers from evaluating/recording the same daily attempt."""
    work.mkdir(parents=True, exist_ok=True)
    with (work / "daily.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise DailyBusy("Daily scheduler is active") from None
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise DailyBusy("Daily scheduler is active") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {"version": 1, "attempts": {}}
    except (OSError, ValueError):
        raise DailyStateError("Daily state unavailable") from None
    if not isinstance(state, dict) or state.get("version") != 1:
        raise DailyStateError("Invalid daily state")
    attempts = state.get("attempts")
    if not isinstance(attempts, dict):
        raise DailyStateError("Invalid daily attempts")
    for date, entry in attempts.items():
        try:
            if dt.date.fromisoformat(date).isoformat() != date:
                raise ValueError()
        except (TypeError, ValueError):
            raise DailyStateError("Invalid daily date") from None
        if not isinstance(entry, dict) or entry.get("outcome") not in {"running", "success", "failed"}:
            raise DailyStateError("Invalid daily outcome")
    return state


def report_stamp(path: Path):
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size, stat.st_ino
    except FileNotFoundError:
        return None


def safe_counts(path: Path, previous_stamp) -> dict[str, Any]:
    """Copy only known counters from a report written by this batch."""
    try:
        if report_stamp(path) == previous_stamp:
            return {}
        source = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(source, dict):
        return {}
    result = {}
    phases = source.get("phases")
    if isinstance(phases, dict):
        result["phases"] = {
            phase: phases[phase]
            for phase in ("discovered", "downloaded", "queued", "archived", "published")
            if type(phases.get(phase)) is int and phases[phase] >= 0
        }
    for key in ("downloaded_bytes", "pending_retry"):
        if type(source.get(key)) is int and source[key] >= 0:
            result[key] = source[key]
    for key in ("scan_complete", "publication_confirmed"):
        if type(source.get(key)) is bool:
            result[key] = source[key]
    return result


def run_daily(
    root: Path,
    sync_args: list[str],
    window_start: int = 3,
    window_end: int = 6,
    *,
    clock: Callable[[], dt.datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    sync_main: Callable[..., int] | None = None,
) -> int:
    clock = clock or beijing_now
    sleep = sleep or time.sleep
    sync_main = sync_main or x.main
    work = Path(root) / ".x-sync"
    report = {"outcome": "scheduler_error"}
    code = 1
    try:
        if not 0 <= window_start < window_end <= 24:
            raise DailyStateError("Invalid scheduler window")
        with daily_lock(work):
            state_path = work / "daily-state.json"
            state = load_state(state_path)
            attempted = False
            attempt_date = None
            skipped = None
            archive_was_busy = False
            sync_report = work / "report.json"
            old_report = report_stamp(sync_report)

            def before_sync() -> bool:
                nonlocal attempted, attempt_date, skipped
                moment = local_time(clock)
                if not in_window(moment, window_start, window_end):
                    skipped = "outside_window"
                    return False
                date = moment.date().isoformat()
                if date in state["attempts"]:
                    skipped = "already_attempted"
                    return False
                started = moment.isoformat()
                state["attempt_date"] = date
                state["attempt_started_at"] = started
                state["attempts"][date] = {"started_at": started, "outcome": "running"}
                # Must succeed while x.main holds the pipeline lock and before
                # discovery starts. A killed process leaves a running marker.
                x.atomic_json(state_path, state)
                attempted, attempt_date = True, date
                return True

            while True:
                moment = local_time(clock)
                report = {"created_at": moment.isoformat(), "date": moment.date().isoformat()}
                if not in_window(moment, window_start, window_end):
                    report["outcome"] = "archive_busy_window_closed" if archive_was_busy else "outside_window"
                    code = 0
                    break
                if report["date"] in state["attempts"]:
                    report["outcome"] = "already_attempted"
                    report["attempt_outcome"] = state["attempts"][report["date"]]["outcome"]
                    code = 0
                    break
                try:
                    # x.main owns .pipeline.lock. Do not acquire it outside
                    # main; its publication child needs that ownership model.
                    with open(os.devnull, "w", encoding="utf-8") as sink:
                        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                            result = sync_main(["--root", str(root), *sync_args], before_sync=before_sync)
                    if not attempted:
                        report["outcome"] = skipped or "not_started"
                        code = 0 if skipped else 1
                        break
                    code = 0 if result == 0 else 1
                    report["outcome"] = "success" if code == 0 else "failed"
                except x.ArchiveBusy:
                    if attempted:
                        report["outcome"], code = "failed", 1
                    else:
                        archive_was_busy = True
                        moment = local_time(clock)
                        if not in_window(moment, window_start, window_end):
                            report["outcome"], code = "archive_busy_window_closed", 0
                            break
                        midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
                        remaining = (midnight + dt.timedelta(hours=window_end) - moment).total_seconds()
                        sleep(min(60, remaining))
                        continue
                except (Exception, SystemExit, KeyboardInterrupt):
                    # Never log exception text, config paths, argv or raw
                    # child output, including invalid-command diagnostics.
                    report["outcome"], code = "failed" if attempted else "scheduler_error", 1
                if attempted:
                    finished = local_time(clock).isoformat()
                    state["attempts"][attempt_date].update(
                        outcome=report["outcome"], finished_at=finished
                    )
                    x.atomic_json(state_path, state)
                    report.update(attempt_date=attempt_date, finished_at=finished)
                    report.update(safe_counts(sync_report, old_report))
                break
            x.atomic_json(work / "daily-report.json", report)
    except DailyBusy:
        # Do not overwrite the active wrapper's report without the daily lock.
        report, code = {"outcome": "daily_busy"}, 0
    except (Exception, SystemExit, KeyboardInterrupt):
        report, code = {"outcome": "scheduler_error"}, 1
    print(json.dumps(report, ensure_ascii=False))
    return code


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--window-start", type=int, default=3)
    parser.add_argument("--window-end", type=int, default=6)
    args, remaining = parser.parse_known_args(argv)
    if not 0 <= args.window_start < args.window_end <= 24:
        parser.error("Invalid scheduler window")
    return run_daily(args.root, remaining, args.window_start, args.window_end)


if __name__ == "__main__":
    raise SystemExit(main())
