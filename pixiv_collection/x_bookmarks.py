"""Daily additive X bookmark backup; live access is explicitly opt-in.

gallery-dl is an external, replaceable adapter. Discovery, verified downloads,
queue acceptance, durable archive commit, and publication are separate states.
No source deletion, unbookmarking, date watermark, or first-known-item stop.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ingest_schema import ALLOWED_EXT, validate_manifest
from receiver import already_known, stage_item
from archive_io import atomic_json as _atomic_json

GALLERY_DL_VERSION = "1.32.16"
BOOKMARK_URL = "https://x.com/i/bookmarks"


class SyncError(RuntimeError):
    """Safe diagnostic: never contains subprocess output or authentication data."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    _atomic_json(path, value, indent=2)


def numeric_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SyncError("Invalid source identifier")
    result = str(value)
    if not re.fullmatch(r"[0-9]{1,30}", result):
        raise SyncError("Invalid source identifier")
    return result


def timestamp(value: Any, fallback: str) -> str:
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            # gallery-dl serializes its UTC datetime without an offset.
            return parsed.replace(tzinfo=parsed.tzinfo or dt.timezone.utc).isoformat()
        except ValueError:
            pass
    return fallback


def media_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "pbs.twimg.com"
            or parsed.username or parsed.password or parsed.port
            or not re.fullmatch(r"/media/[\w-]+(?:\.[a-z]+)?", parsed.path)):
        raise SyncError("Unsupported image URL")
    return value


def manifest_for(metadata: dict[str, Any], discovered_at: str) -> dict[str, Any]:
    if not isinstance(metadata, dict) or not isinstance(metadata.get('author'), dict):
        raise SyncError("Missing post or author metadata")
    source_id = numeric_id(metadata.get("tweet_id"))
    author = metadata.get("author") or {}
    author_id = numeric_id(author.get("id"))
    account = str(author.get("name") or "")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,50}", account):
        raise SyncError("Invalid author handle")
    content = str(metadata.get("content") or "")
    return {
        "source": "twitter", "source_id": source_id,
        "source_url": f"https://x.com/{account}/status/{source_id}",
        "title": content.splitlines()[0][:160] if content else "X bookmark",
        "caption": content,
        "created_at": timestamp(metadata.get("date"), discovered_at),
        "collected_at": timestamp(metadata.get("date_bookmarked"), discovered_at),
        "tags": [str(tag) for tag in metadata.get("hashtags", [])],
        "x_restrict": int(bool(metadata.get("sensitive"))),
        "bookmark": int(metadata.get("bookmark_count") or 0),
        "view": int(metadata.get("view_count") or 0),
        "author": {"source_uid": author_id, "name": str(author.get("nick") or account),
                   "account": account, "url": f"https://x.com/{account}"},
        "files": [],
    }


@dataclass
class Discovery:
    messages: list[Any]
    complete: bool


class GalleryDL:
    """CLI contract verified against the official Codeberg v1.32.16 tag."""

    def __init__(self, config: Path, executable: str = "gallery-dl", timeout: int = 1800):
        self.config = config
        self.executable = executable
        self.timeout = timeout
        self.verified = False

    def verify_version(self) -> None:
        version = self.run(self.command("--version"))
        if version.returncode or version.stdout.strip() != GALLERY_DL_VERSION:
            raise SyncError("Expected gallery-dl " + GALLERY_DL_VERSION)
        self.verified = True

    def command(self, *args: str) -> list[str]:
        return [self.executable, "--config-ignore", *args]

    def run(self, command: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(command, capture_output=True, text=True,
                                  encoding="utf-8", timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SyncError("gallery-dl unavailable or timed out") from exc

    def discover(self) -> Discovery:
        self.verify_version()
        if not self.config.is_file() or self.config.name.endswith(".example.json"):
            raise SyncError("An explicitly prepared private gallery-dl config is required")
        command = self.command("--config", str(self.config), "--dump-json", "--no-postprocessors")
        # Full bookmark traversal on every run. No automatic browser credential extraction.
        options = {
            "extractor.cookies-from-browser": None, "extractor.netrc": False,
            "extractor.username": None, "extractor.password": None,
            "extractor.cookies-update": False, "extractor.archive": None,
            "extractor.date-after": None, "extractor.date-before": None,
            "extractor.file-range": None, "extractor.post-range": None,
            "extractor.file-filter": None, "extractor.post-filter": None,
            "extractor.image-range": None, "extractor.image-filter": None,
            "extractor.whitelist-tags": None, "extractor.tags-whitelist": None,
            "extractor.blacklist-tags": None, "extractor.tags-blacklist": None,
            "extractor.abort": None, "extractor.terminate": None,
            "extractor.twitter.cursor": None, "extractor.twitter.videos": False,
            "extractor.twitter.previews": False, "extractor.twitter.cards": False,
            "extractor.twitter.articles": False, "extractor.twitter.quoted": False,
            "extractor.twitter.expand": False, "extractor.twitter.twitpic": False,
            "extractor.twitter.text-tweets": False, "extractor.twitter.transform": True,
            "extractor.twitter.retweets": "original", "extractor.twitter.replies": True,
            "extractor.twitter.ratelimit": "abort", "extractor.twitter.retries-api": 3,
            "extractor.twitter.size": ["orig", "4096x4096", "large"],
            "output.jsonl": False, "output.private": False,
        }
        for key, value in options.items():
            # Global options override even per-bookmark entries in a private
            # account config; only authentication settings remain user-owned.
            option = key if key.startswith('output.') else key.rsplit('.', 1)[-1]
            command.extend(["-o", option + "=" + json.dumps(value)])
        result = self.run(command + [BOOKMARK_URL])
        try:
            messages = json.loads(result.stdout)
            if not isinstance(messages, list):
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise SyncError("Invalid gallery-dl discovery response") from exc
        return Discovery(messages, result.returncode == 0 and not any(
            isinstance(m, list) and m and m[0] == -1 for m in messages))

    def download(self, url: str, destination: Path) -> None:
        if not self.verified:
            self.verify_version()
        # The dedicated public-media extractor needs no X account configuration.
        command = self.command("--no-postprocessors", "--no-skip", "--directory", str(destination.parent),
                               "--filename", destination.name, "--retries", "3", "--timeout", "30")
        for key, value in {
            "extractor.twitter.size": ["orig", "4096x4096", "large"],
            "extractor.netrc": False, "extractor.cookies-from-browser": None,
            "extractor.cookies": None, "extractor.archive": None,
        }.items():
            command.extend(["-o", key.rsplit('.', 1)[-1] + "=" + json.dumps(value)])
        result = self.run(command + [media_url(url)])
        if result.returncode or not destination.is_file():
            raise SyncError("Image download failed")


class FixtureAdapter:
    """Explicit offline fixture adapter. Never invokes gallery-dl or a network client."""

    def __init__(self, messages: Path, media: Path):
        self.messages, self.media = messages, media

    def discover(self) -> Discovery:
        return Discovery(json.loads(self.messages.read_text("utf-8")), True)

    def download(self, url: str, destination: Path) -> None:
        import shutil
        suffix = Path(destination.name.removesuffix('.download')).suffix
        source = self.media / (urlsplit(url).path.rsplit("/", 1)[-1].split(".")[0] + suffix)
        shutil.copyfile(source, destination)


def verify_image(path: Path) -> str:
    from PIL import Image
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 50 * 1024 ** 2:
        raise SyncError("Missing or oversized image")
    try:
        with Image.open(path) as image:
            image.verify()
    except Exception as exc:
        raise SyncError("Image integrity check failed") from exc
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PipelinePublisher:
    def __call__(self, root: Path, publication_id: str) -> bool:
        command = [sys.executable, str(Path(__file__).with_name("pipeline.py")),
                   "--root", str(root), "--ingest-only", "--publication-id", publication_id]
        try:
            result = subprocess.run(command, capture_output=True, timeout=7200, check=False)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


class BookmarkSync:
    def __init__(self, root: Path, adapter: Any, publisher: Any = None):
        self.root = root.resolve()
        self.adapter, self.publisher = adapter, publisher
        self.work = self.root / ".x-sync"
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_path = self.work / "state.json"
        self.state = (json.loads(self.state_path.read_text("utf-8")) if self.state_path.exists()
                      else {"version": 1, "items": {}, "last_complete_scan": None})
        if self.state.get("version") != 1:
            raise SyncError("Unsupported sync state version")

    def save(self) -> None:
        atomic_json(self.state_path, self.state)

    def discover(self) -> bool:
        discovery = self.adapter.discover()
        seen_at = now()
        errors = 0
        for message in discovery.messages:
            if isinstance(message, list) and message and message[0] in (-1, 6):
                errors += 1
                continue
            if not isinstance(message, list) or not message or message[0] not in (2, 3):
                continue
            try:
                metadata = message[-1]
                manifest = manifest_for(metadata, seen_at)
                source_id = manifest["source_id"]
                expected = int(metadata["count"])
                if not 1 <= expected <= 50:
                    raise SyncError("Invalid media count")
                item = self.state["items"].setdefault(source_id, {
                    "manifest": manifest, "media": {}, "expected": expected,
                    "phase": "discovered", "attempts": 0,
                })
                if item["phase"] in ("archived", "published"):
                    continue
                if item["expected"] != expected:
                    raise SyncError("Post media changed before backup completion")
                if message[0] == 3:
                    number = int(metadata["num"])
                    extension = str(metadata.get("extension") or "").lower()
                    if not 1 <= number <= expected or extension not in ALLOWED_EXT:
                        raise SyncError("Unsupported media record")
                    record = {"url": media_url(message[1]), "file": f"{number}.{extension}"}
                    item["media"].setdefault(str(number), record)
                item["last_seen"] = seen_at
            except (KeyError, TypeError, ValueError, SyncError):
                errors += 1
        complete = discovery.complete and errors == 0
        self.state["scan_complete"] = complete
        if complete:
            self.state["last_complete_scan"] = seen_at
        self.save()
        return complete

    def reconcile(self) -> None:
        collection_path = self.root / "collection.json"
        images_path = self.root / "images.json"
        if not collection_path.is_file() or not images_path.is_file():
            return
        collection = json.loads(collection_path.read_text("utf-8"))
        images = json.loads(images_path.read_text("utf-8"))
        if not isinstance(images, list):
            raise SyncError("Invalid archive export")
        exported = {}
        for image in images:
            exported.setdefault(str(image["id"]), set()).add(int(image["part"]))
        for source_id, item in self.state["items"].items():
            internal_id = collection.get("sources", {}).get("twitter:" + source_id)
            if internal_id is not None and exported.get(str(internal_id)) == set(range(item["expected"])):
                if item["phase"] != "published":
                    item["phase"] = "archived"
                item["internal_id"] = internal_id
                item.pop("error", None)

    def queue(self, source_id: str, item: dict[str, Any]) -> None:
        numeric_id(source_id)
        if set(item["media"]) != {str(n) for n in range(1, item["expected"] + 1)}:
            raise SyncError("Incomplete post discovery; retry full traversal")
        item["attempts"] += 1
        cache = self.work / "downloads" / source_id
        cache.mkdir(parents=True, exist_ok=True)
        blobs, files = [], []
        for number in range(1, item["expected"] + 1):
            record = item["media"][str(number)]
            if not re.fullmatch(str(number) + r"\.(?:jpg|jpeg|png|gif|webp|avif)", record['file']):
                raise SyncError('Invalid cached image filename')
            path = cache / record["file"]
            try:
                digest = verify_image(path)
                if record.get("sha256") and digest != record["sha256"]:
                    raise SyncError("Cached image changed")
            except SyncError:
                temporary = path.with_name(path.name + ".download")
                self.adapter.download(record["url"], temporary)
                digest = verify_image(temporary)
                os.replace(temporary, path)
            record["sha256"] = digest
            blobs.append(path.read_bytes())
            files.append({"file": record["file"], "origin_url": record["url"]})
            self.save()
        item["phase"] = "downloaded"
        item["manifest"]["files"] = files
        self.save()
        reason = validate_manifest(item["manifest"])
        if reason:
            raise SyncError("Invalid normalized manifest")
        # Trusted local process reuses the receiver's atomic staging helper.
        # No receiver credential is needed and no public endpoint is introduced.
        if already_known(self.root, "twitter", source_id) != "pending":
            stage_item(self.root, item["manifest"], blobs)
        item["phase"] = "queued"
        item.pop("error", None)
        self.save()

    def run(self) -> dict[str, Any]:
        discovery_ok = True
        try:
            discovery_ok = self.discover()
        except SyncError:
            discovery_ok = False
            self.state["scan_complete"] = False
            self.save()
        self.reconcile()
        for source_id, item in self.state["items"].items():
            if item["phase"] in ("archived", "published"):
                continue
            try:
                self.queue(source_id, item)
            except Exception:
                # Never copy arbitrary downloader exceptions or raw metadata into diagnostics.
                item["error"] = "Download or queue incomplete; retained for retry"
                self.save()
        publication_ok = False
        if self.publisher and any(i["phase"] in ("queued", "archived") for i in self.state["items"].values()):
            publication_id = uuid.uuid4().hex
            pipeline_ok = self.publisher(self.root, publication_id)
            self.reconcile()
            try:
                report = json.loads((self.root / "upload_report.json").read_text("utf-8"))
                publication_ok = (pipeline_ok and report.get("publication_id") == publication_id
                                  and report.get("dry_run") is False and report.get("errors") == [])
            except (OSError, ValueError):
                publication_ok = False
            if publication_ok:
                for item in self.state["items"].values():
                    if item["phase"] == "archived":
                        item["phase"] = "published"
                        item["publication_id"] = publication_id
        self.save()
        counts = {phase: sum(i["phase"] == phase for i in self.state["items"].values())
                  for phase in ("discovered", "downloaded", "queued", "archived", "published")}
        result = {"created_at": now(), "scan_complete": discovery_ok, "phases": counts,
                  "pending_retry": sum(bool(i.get("error")) for i in self.state["items"].values()),
                  "publication_confirmed": publication_ok}
        atomic_json(self.work / "report.json", result)
        return result


@contextmanager
def archive_lock(root: Path):
    """Share the existing pipeline lock; keep it held while the publisher runs."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.pipeline.lock').open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise SyncError('Another archive task is active') from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SyncError('Another archive task is active') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="Explicitly access X with a manually prepared config")
    mode.add_argument("--fixture", type=Path, help="Offline synthetic discovery fixture")
    parser.add_argument("--fixture-media", type=Path)
    parser.add_argument("--gallery-config", type=Path)
    parser.add_argument("--gallery-dl", default="gallery-dl")
    parser.add_argument("--publish", action="store_true", help="Run existing ingestion and R2 publication (live only)")
    args = parser.parse_args(argv)
    if args.live:
        if args.gallery_config is None:
            parser.error("--live requires --gallery-config; no automatic credential extraction")
        adapter = GalleryDL(args.gallery_config, args.gallery_dl)
    else:
        if args.fixture_media is None or args.publish:
            parser.error("Offline mode requires --fixture-media and forbids --publish")
        adapter = FixtureAdapter(args.fixture, args.fixture_media)
    # The publisher runs under this parent's lock; do not wrap this CLI in flock again.
    with archive_lock(args.root):
        report = BookmarkSync(args.root, adapter, PipelinePublisher() if args.publish else None).run()
    print(json.dumps(report, ensure_ascii=False))
    return int(not report["scan_complete"] or report["pending_retry"] > 0
               or (args.publish and any(report["phases"][p] for p in ("queued", "archived"))))


if __name__ == "__main__":
    raise SystemExit(main())
