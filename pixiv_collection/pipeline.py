from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import getpass
import hashlib
import hmac
import json
import mimetypes
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


S3_NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
PUBLISH_FILES = ("collection.json", "images.json")
PUBLISH_DIRS = ("image/original", "image/preview", "image/thumbnail")


def load_local_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_default_env_files() -> None:
    candidates = [
        Path(__file__).with_name("pipeline.local.env"),
        Path.cwd() / "pipeline.local.env",
    ]
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        load_local_env_file(resolved)


def env_value(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def ensure_dirs(root: Path) -> None:
    for path in PUBLISH_DIRS:
        (root / path).mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "backups").mkdir(parents=True, exist_ok=True)


def backup_metadata(root: Path) -> Path:
    backup_dir = root / "backups" / timestamp()
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name in PUBLISH_FILES:
        path = root / name
        if path.exists():
            shutil.copy2(path, backup_dir / name)
    return backup_dir


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def image_filename(item: dict[str, Any]) -> str:
    return f"{int(item['id'])}_p{int(item['part'])}.{item['ext']}"


def preview_filename(item: dict[str, Any]) -> str:
    return f"{int(item['id'])}_p{int(item['part'])}.webp"


def list_file_names(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {item.name for item in path.iterdir() if item.is_file()}


def collection_file_names(collection: dict[str, Any]) -> set[str]:
    files = collection.get("files", {})
    if not isinstance(files, dict):
        return set()
    return set(files)


def collection_image_ids(collection: dict[str, Any]) -> set[int]:
    images = collection.get("images", {})
    result: set[int] = set()
    if not isinstance(images, dict):
        return result
    for key in images:
        try:
            result.add(int(key))
        except ValueError:
            pass
    return result


def validate_archive(root: Path) -> dict[str, Any]:
    collection_path = root / "collection.json"
    images_path = root / "images.json"
    original_dir = root / "image" / "original"
    preview_dir = root / "image" / "preview"
    thumbnail_dir = root / "image" / "thumbnail"

    images = load_json(images_path) if images_path.exists() else []
    if not isinstance(images, list):
        raise ValueError("images.json must contain a JSON array")
    collection = load_json(collection_path) if collection_path.exists() else {}
    if not isinstance(collection, dict):
        raise ValueError("collection.json must contain a JSON object")

    original_files = list_file_names(original_dir)
    preview_files = list_file_names(preview_dir)
    thumbnail_files = list_file_names(thumbnail_dir)

    expected_original = {image_filename(item) for item in images}
    expected_preview = {preview_filename(item) for item in images}
    expected_thumbnail = expected_preview.copy()
    collection_files = collection_file_names(collection)
    image_ids = {int(item["id"]) for item in images}
    collection_ids = collection_image_ids(collection)

    duplicate_keys: list[str] = []
    seen_keys: set[str] = set()
    for item in images:
        key = f"{int(item['id'])}_p{int(item['part'])}"
        if key in seen_keys:
            duplicate_keys.append(key)
        seen_keys.add(key)

    problems = {
        "missing_required_files": [
            name for name in PUBLISH_FILES if not (root / name).exists()
        ],
        "missing_originals_from_images_json": sorted(expected_original - original_files),
        "missing_previews_from_images_json": sorted(expected_preview - preview_files),
        "missing_thumbnails_from_images_json": sorted(
            expected_thumbnail - thumbnail_files
        ),
        "missing_originals_from_collection_json": sorted(collection_files - original_files),
        "images_json_files_not_in_collection_json": sorted(
            expected_original - collection_files
        ),
        "collection_files_not_in_images_json": sorted(
            collection_files - expected_original
        ),
        "images_json_ids_not_in_collection_json": sorted(image_ids - collection_ids),
        "collection_image_ids_not_in_images_json": sorted(collection_ids - image_ids),
        "duplicate_images_json_entries": sorted(duplicate_keys),
        "extra_originals_not_in_images_json": sorted(original_files - expected_original),
    }
    report = {
        "root": str(root),
        "summary": {
            "images_json_entries": len(images),
            "images_json_illusts": len(image_ids),
            "collection_files": len(collection_files),
            "collection_images": len(collection_ids),
            "local_original_files": len(original_files),
            "local_preview_files": len(preview_files),
            "local_thumbnail_files": len(thumbnail_files),
        },
        "problems": problems,
    }
    return report


def archive_has_critical_problem(report: dict[str, Any]) -> bool:
    problems = report["problems"]
    critical_keys = (
        "missing_required_files",
        "missing_originals_from_images_json",
        "missing_previews_from_images_json",
        "missing_thumbnails_from_images_json",
        "missing_originals_from_collection_json",
        "images_json_files_not_in_collection_json",
        "collection_files_not_in_images_json",
        "images_json_ids_not_in_collection_json",
        "collection_image_ids_not_in_images_json",
        "duplicate_images_json_entries",
    )
    return any(problems[key] for key in critical_keys)


def write_validation_report(root: Path, report: dict[str, Any]) -> None:
    output = root / "archive_report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print("Archive validation summary:")
    for key, value in report["summary"].items():
        print(f"  {key}: {value}")
    problem_count = 0
    for key, values in report["problems"].items():
        if values:
            problem_count += len(values)
            print(f"  {key}: {len(values)}")
            for value in values[:12]:
                print(f"    - {value}")
            if len(values) > 12:
                print(f"    ... {len(values) - 12} more")
    if problem_count == 0:
        print("  problems: none")
    print(f"Validation report written: {output}")


def prompt_pixiv_credentials(args: argparse.Namespace) -> bool:
    if args.skip_crawl:
        return True
    if not args.user_id:
        raw = input("Pixiv user id: ").strip()
        if raw:
            try:
                args.user_id = int(raw)
            except ValueError:
                print("Pixiv user id must be a number.", file=sys.stderr)
                return False
    if not args.refresh_token:
        args.refresh_token = getpass.getpass("Pixiv refresh token: ").strip()
    if not args.user_id or not args.refresh_token:
        print("Pixiv credentials are required for crawling.", file=sys.stderr)
        return False
    return True


def run_crawler(args: argparse.Namespace, root: Path) -> None:
    try:
        from collection import PixivCollection
    except Exception as exc:
        print("Unable to import crawler dependencies.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        raise

    backup_dir = backup_metadata(root)
    print(f"Backed up metadata to {backup_dir}")

    client = PixivCollection()
    log_file = root / "logs" / f"pipeline_{int(time.time())}.log"
    client.add_logger(str(log_file), level=args.log_level)
    client.set_path(
        {
            "original": str(root / "image" / "original"),
            "preview": str(root / "image" / "preview"),
            "thumbnail": str(root / "image" / "thumbnail"),
        }
    )
    collection_path = root / "collection.json"
    if collection_path.exists():
        client.read_data(str(collection_path))

    if not client.init(refresh_token=args.refresh_token):
        raise RuntimeError("Pixiv API initialization failed")

    if args.public_pages > 0:
        client.download_bookmark(
            user_id=args.user_id, type="public", max_page=args.public_pages
        )
    if args.private_pages > 0:
        client.download_bookmark(
            user_id=args.user_id, type="private", max_page=args.private_pages
        )

    client.diff()
    client.update()
    client.generate_preview(overwrite=args.overwrite_preview)
    client.generate_thumbnail(overwrite=args.overwrite_thumbnail)
    client.clean()
    client.save_data(str(root / "collection.json"))
    client.export(str(root / "images.json"))
    print(f"Crawler log written: {log_file}")


def normalize_prefix(prefix: str) -> str:
    return prefix.strip().strip("/")


def join_key(prefix: str, key: str) -> str:
    key = key.replace("\\", "/").lstrip("/")
    prefix = normalize_prefix(prefix)
    if prefix:
        return f"{prefix}/{key}" if key else prefix
    return key


def encode_uri(path: str) -> str:
    return urllib.parse.quote(path, safe="/-_.~")


def encode_query(params: dict[str, str]) -> str:
    parts = []
    for key in sorted(params):
        value = params[key]
        parts.append(
            f"{urllib.parse.quote(key, safe='-_.~')}="
            f"{urllib.parse.quote(value, safe='-_.~')}"
        )
    return "&".join(parts)


def xml_text(element: ET.Element, name: str, default: str = "") -> str:
    child = element.find(f"{{{S3_NAMESPACE}}}{name}")
    if child is None:
        child = element.find(name)
    if child is None or child.text is None:
        return default
    return child.text


@dataclass(frozen=True)
class R2Config:
    endpoint_url: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    region: str = "auto"


@dataclass(frozen=True)
class RemoteObject:
    key: str
    size: int
    etag: str


class R2Client:
    def __init__(self, config: R2Config, timeout: int = 60):
        self.config = config
        self.timeout = timeout
        self.endpoint = urllib.parse.urlparse(config.endpoint_url.rstrip("/"))
        if self.endpoint.scheme not in ("http", "https") or not self.endpoint.netloc:
            raise ValueError(f"Invalid R2 endpoint: {config.endpoint_url}")

    def _signing_key(self, date_stamp: str) -> bytes:
        key_date = hmac.new(
            ("AWS4" + self.config.secret_access_key).encode("utf-8"),
            date_stamp.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        key_region = hmac.new(
            key_date, self.config.region.encode("utf-8"), hashlib.sha256
        ).digest()
        key_service = hmac.new(key_region, b"s3", hashlib.sha256).digest()
        return hmac.new(key_service, b"aws4_request", hashlib.sha256).digest()

    def request(
        self,
        method: str,
        object_key: str = "",
        query: dict[str, str] | None = None,
        payload: bytes = b"",
        extra_headers: dict[str, str] | None = None,
    ) -> bytes:
        now = dt.datetime.now(dt.UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        bucket_path = urllib.parse.quote(self.config.bucket, safe="-_.~")
        canonical_uri = f"/{bucket_path}"
        if object_key:
            canonical_uri += "/" + encode_uri(object_key)

        query = query or {}
        canonical_query = encode_query(query)
        payload_hash = hashlib.sha256(payload).hexdigest()
        headers = {
            "host": self.endpoint.netloc,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if extra_headers:
            headers.update({key.lower(): value for key, value in extra_headers.items()})

        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(
            f"{name}:{headers[name].strip()}\n" for name in sorted(headers)
        )
        canonical_request = "\n".join(
            [
                method,
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )
        credential_scope = f"{date_stamp}/{self.config.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(
            self._signing_key(date_stamp),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers["authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.config.access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        url = urllib.parse.urlunparse(
            (
                self.endpoint.scheme,
                self.endpoint.netloc,
                canonical_uri,
                "",
                canonical_query,
                "",
            )
        )
        request = urllib.request.Request(
            url, data=payload if method in ("PUT", "POST") else None, method=method
        )
        for key, value in headers.items():
            request.add_header(key, value)

        for attempt in range(1, 6):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code in (429, 500, 502, 503, 504) and attempt < 5:
                    time.sleep(min(10, attempt * 1.5))
                    continue
                raise RuntimeError(
                    f"R2 {method} {object_key or '/'} failed: "
                    f"status={exc.code} body={body[:500]}"
                ) from exc
            except urllib.error.URLError as exc:
                if attempt < 5:
                    time.sleep(min(10, attempt * 1.5))
                    continue
                raise RuntimeError(
                    f"R2 {method} {object_key or '/'} failed before HTTP response: {exc.reason}"
                ) from exc
        raise RuntimeError(f"R2 {method} {object_key or '/'} failed")

    def list_objects(self, prefix: str) -> dict[str, RemoteObject]:
        result: dict[str, RemoteObject] = {}
        continuation_token = ""
        while True:
            query = {"list-type": "2", "max-keys": "1000", "prefix": prefix}
            if continuation_token:
                query["continuation-token"] = continuation_token
            body = self.request("GET", query=query)
            root = ET.fromstring(body)
            for item in root.findall(f"{{{S3_NAMESPACE}}}Contents") or root.findall(
                "Contents"
            ):
                key = xml_text(item, "Key")
                if not key or key.endswith("/"):
                    continue
                result[key] = RemoteObject(
                    key=key,
                    size=int(xml_text(item, "Size", "0")),
                    etag=xml_text(item, "ETag").strip('"'),
                )
            if xml_text(root, "IsTruncated").lower() != "true":
                break
            continuation_token = xml_text(root, "NextContinuationToken")
            if not continuation_token:
                break
        return result

    def put_file(self, key: str, path: Path) -> None:
        payload = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.request(
            "PUT",
            object_key=key,
            payload=payload,
            extra_headers={"content-type": content_type},
        )


def iter_publish_files(root: Path) -> Iterable[tuple[str, Path]]:
    for name in PUBLISH_FILES:
        path = root / name
        if path.is_file():
            yield name, path
    for directory in PUBLISH_DIRS:
        base = root / directory
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and not path.name.endswith(".partial"):
                yield path.relative_to(root).as_posix(), path


def build_r2_client(args: argparse.Namespace) -> tuple[R2Client, str]:
    bucket = args.r2_bucket or env_value("R2_BUCKET", "CLOUDFLARE_R2_BUCKET")
    account_id = args.cloudflare_account_id or env_value(
        "CLOUDFLARE_ACCOUNT_ID", "R2_ACCOUNT_ID"
    )
    endpoint = args.r2_endpoint_url or env_value(
        "R2_ENDPOINT_URL", "CLOUDFLARE_R2_ENDPOINT_URL"
    )
    access_key = args.r2_access_key_id or env_value("AWS_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID")
    secret_key = args.r2_secret_access_key or env_value(
        "AWS_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY"
    )
    if not endpoint and account_id:
        endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
    missing = []
    if not bucket:
        missing.append("R2_BUCKET")
    if not endpoint:
        missing.append("R2_ENDPOINT_URL or CLOUDFLARE_ACCOUNT_ID")
    if not access_key:
        missing.append("AWS_ACCESS_KEY_ID")
    if not secret_key:
        missing.append("AWS_SECRET_ACCESS_KEY")
    if missing:
        raise RuntimeError("Missing R2 config: " + ", ".join(missing))
    client = R2Client(
        R2Config(
            endpoint_url=endpoint,
            bucket=bucket,
            access_key_id=access_key,
            secret_access_key=secret_key,
        ),
        timeout=args.r2_timeout,
    )
    return client, normalize_prefix(args.r2_prefix or env_value("R2_PREFIX") or "")


def collect_remote_index(client: R2Client, prefix: str) -> dict[str, RemoteObject]:
    remote: dict[str, RemoteObject] = {}
    for name in PUBLISH_FILES:
        remote.update(client.list_objects(join_key(prefix, name)))
    for directory in PUBLISH_DIRS:
        remote.update(client.list_objects(join_key(prefix, directory) + "/"))
    return remote


def upload_to_r2(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    client, prefix = build_r2_client(args)
    local_files = list(iter_publish_files(root))
    print(f"Found {len(local_files)} local publish files.")
    print("Listing R2 objects...")
    remote = collect_remote_index(client, prefix)

    pending: list[tuple[str, Path, int, str]] = []
    skipped = 0
    for relative_key, path in local_files:
        key = join_key(prefix, relative_key)
        size = path.stat().st_size
        remote_obj = remote.get(key)
        if remote_obj and remote_obj.size == size:
            skipped += 1
            continue
        reason = "missing" if remote_obj is None else f"size {remote_obj.size}->{size}"
        pending.append((key, path, size, reason))

    total_bytes = sum(item[2] for item in pending)
    print(
        f"R2 upload plan: upload={len(pending)}, skip_same_size={skipped}, bytes={total_bytes}"
    )
    for key, _path, size, reason in pending[:30]:
        print(f"  upload {key} ({size} bytes, {reason})")
    if len(pending) > 30:
        print(f"  ... {len(pending) - 30} more")

    report = {
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "prefix": prefix,
        "local_files": len(local_files),
        "skipped_same_size": skipped,
        "planned_uploads": len(pending),
        "planned_upload_bytes": total_bytes,
        "uploaded": [],
        "errors": [],
        "dry_run": args.dry_run_upload,
    }
    if args.dry_run_upload or not pending:
        return report

    def _upload(item: tuple[str, Path, int, str]) -> dict[str, Any]:
        key, path, size, reason = item
        client.put_file(key, path)
        return {"key": key, "size": size, "reason": reason}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.upload_workers)) as executor:
        futures = {executor.submit(_upload, item): item for item in pending}
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            completed += 1
            item = futures[future]
            try:
                uploaded = future.result()
                report["uploaded"].append(uploaded)
                print(f"[{completed}/{len(pending)}] uploaded {uploaded['key']}")
            except Exception as exc:
                key = item[0]
                report["errors"].append({"key": key, "error": str(exc)})
                print(f"[{completed}/{len(pending)}] error {key}: {exc}")

    return report


def write_upload_report(root: Path, report: dict[str, Any]) -> None:
    output = root / "upload_report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(f"Upload report written: {output}")
    if report["errors"]:
        raise RuntimeError(f"R2 upload finished with {len(report['errors'])} errors")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PixivCollection crawl -> local archive -> R2 upload pipeline."
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--user-id", type=int, default=int(env_value("PIXIV_USER_ID") or "0"))
    parser.add_argument("--refresh-token", default=env_value("PIXIV_REFRESH_TOKEN", "REFRESH_TOKEN"))
    parser.add_argument(
        "--public-pages",
        type=int,
        default=9999,
        help="Public bookmark pages to scan.",
    )
    parser.add_argument(
        "--private-pages",
        type=int,
        default=9999,
        help="Private bookmark pages to scan.",
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--overwrite-preview", action="store_true")
    parser.add_argument("--overwrite-thumbnail", action="store_true")
    parser.add_argument("--skip-crawl", action="store_true")
    parser.add_argument("--skip-upload", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--dry-run-upload", action="store_true")
    parser.add_argument("--upload-workers", type=int, default=4)
    parser.add_argument("--r2-bucket", default="")
    parser.add_argument("--r2-prefix", default="")
    parser.add_argument("--r2-endpoint-url", default="")
    parser.add_argument("--cloudflare-account-id", default="")
    parser.add_argument("--r2-access-key-id", default="")
    parser.add_argument("--r2-secret-access-key", default="")
    parser.add_argument("--r2-timeout", type=int, default=60)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_default_env_files()
    args = build_arg_parser().parse_args(argv)
    root = Path(args.root).resolve()
    ensure_dirs(root)

    if args.validate_only:
        report = validate_archive(root)
        write_validation_report(root, report)
        return 1 if archive_has_critical_problem(report) else 0

    if not args.skip_crawl:
        if not prompt_pixiv_credentials(args):
            return 2
        run_crawler(args, root)

    report = validate_archive(root)
    write_validation_report(root, report)
    if archive_has_critical_problem(report):
        print("Archive validation failed; upload is blocked.", file=sys.stderr)
        return 1

    if not args.skip_upload:
        upload_report = upload_to_r2(args, root)
        write_upload_report(root, upload_report)
    else:
        print("Upload skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
