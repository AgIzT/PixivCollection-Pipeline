"""pending 队列的接收端：油猴脚本把收藏投递到这里。

只监听 127.0.0.1，公网入口走 Cloudflare Tunnel，服务器不开放额外端口。

刻意保持的三个约束：

1. 只写不读不删。没有任何接口能取回图片、列出归档或删除数据，
   因此脚本里那枚 token 泄漏的最坏后果是被人塞垃圾图，
   与 R2 密钥或 Pixiv refresh token 泄漏不是一个量级。
2. 零依赖。用标准库 http.server，不往跑着爬虫的 venv 里加东西。
   不用 multipart：cgi.FieldStorage 在 3.13 已移除，改用 JSON + base64。
3. 落盘原子。先写临时目录再 os.replace 换入，且清单一定后于图片目录出现，
   避免 pipeline 扫到清单时图片还没齐。

接口：

    POST /ingest              入库，Bearer 鉴权
    GET  /exists?source=&source_id=
                              查是否已入库，供脚本打「已收录」角标
    GET  /health              存活探针
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ingest_schema import (  # noqa: E402
    ALLOWED_EXT,
    MAX_PARTS,
    disk_state,
    validate_manifest,
)

MAX_BODY = 150 * 1024 * 1024      # 单请求上限，含 base64 膨胀
MAX_FILE_BYTES = 50 * 1024 * 1024  # 单张原图解码后上限
RATE_LIMIT = 60                    # 每分钟请求数上限（全局）
RATE_WINDOW = 60.0

SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")

_recent: deque[float] = deque()
_rate_lock = threading.Lock()
_write_lock = threading.Lock()

# collection.json 2MB 级，按 mtime 缓存 sources 表，避免每次 /exists 都全量解析
_sources_cache: dict[str, Any] = {"mtime": None, "data": {}}


def rate_ok() -> bool:
    now = time.monotonic()
    with _rate_lock:
        while _recent and now - _recent[0] > RATE_WINDOW:
            _recent.popleft()
        if len(_recent) >= RATE_LIMIT:
            return False
        _recent.append(now)
        return True


def load_sources(root: Path) -> dict[str, Any]:
    path = root / "collection.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _sources_cache["mtime"] == mtime:
        return _sources_cache["data"]
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle).get("sources", {})
    except Exception:
        data = {}
    _sources_cache["mtime"] = mtime
    _sources_cache["data"] = data
    return data


def item_name(source: str, source_id: str) -> str:
    return f"{SAFE_ID.sub('_', source)}-{SAFE_ID.sub('_', source_id)}"[:120]


def already_known(root: Path, source: str, source_id: str) -> str | None:
    """返回 'collection' / 'pending' / None。"""

    if f"{source}:{source_id}" in load_sources(root):
        return "collection"
    name = item_name(source, source_id)
    pending = root / "pending"
    if (pending / f"{name}.json").exists():
        return "pending"
    # Only the persisted source registry confirms an archive commit. A leftover
    # consumed manifest from an interrupted older pipeline is not sufficient.
    return None


def stage_item(root: Path, manifest: dict[str, Any],
               blobs: list[bytes]) -> str:
    """把清单与原图原子地放进 pending/，返回队列项名。"""

    pending = root / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    name = item_name(manifest["source"], manifest["source_id"])

    tmp_dir = pending / f".tmp-{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True)
    try:
        for index, blob in enumerate(blobs):
            (tmp_dir / manifest["files"][index]["file"]).write_bytes(blob)

        target = pending / name
        if target.exists():
            shutil.rmtree(target)
        os.replace(tmp_dir, target)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    # 清单必须后于图片目录出现，否则 pipeline 可能扫到不完整的项
    tmp_manifest = pending / f".tmp-{uuid.uuid4().hex}.json"
    tmp_manifest.write_text(json.dumps(manifest, ensure_ascii=False),
                            encoding="utf-8")
    os.replace(tmp_manifest, pending / f"{name}.json")
    return name


def decode_files(manifest: dict[str, Any]) -> list[bytes]:
    """校验并取出 files[].data 的二进制，同时把 data 字段从清单里剔除。"""

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("files 为空")
    if len(files) > MAX_PARTS:
        raise ValueError(f"分P 过多（{len(files)} > {MAX_PARTS}）")

    blobs: list[bytes] = []
    cleaned: list[dict[str, Any]] = []
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            raise ValueError(f"files[{index}] 格式错误")
        name = str(item.get("file") or "").strip()
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise ValueError(f"files[{index}].file 非法: {name!r}")
        if name.rsplit(".", 1)[-1].lower() not in ALLOWED_EXT:
            raise ValueError(f"files[{index}] 扩展名不支持: {name!r}")
        try:
            blob = base64.b64decode(str(item.get("data") or ""), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"files[{index}].data 不是合法 base64: {exc}") from exc
        if not blob:
            raise ValueError(f"files[{index}].data 为空")
        if len(blob) > MAX_FILE_BYTES:
            raise ValueError(f"files[{index}] 过大: {len(blob)} 字节")
        blobs.append(blob)
        cleaned.append({k: v for k, v in item.items() if k != "data"})

    manifest["files"] = cleaned
    return blobs


class Handler(BaseHTTPRequestHandler):
    server_version = "PixivIngest/1.0"
    root: Path = Path(".")
    token: str = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def reply(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        if not self.token:
            return True
        provided = (self.headers.get("Authorization") or "")
        provided = provided[7:] if provided[:7].lower() == "bearer " else provided
        return bool(provided) and hmac.compare_digest(provided, self.token)

    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        if path == "/health":
            self.reply(200, {"ok": True})
            return
        if path != "/exists":
            self.reply(404, {"error": "not found"})
            return
        if not self.authorized():
            self.reply(401, {"error": "unauthorized"})
            return

        params: dict[str, str] = {}
        for pair in query.split("&"):
            key, _, value = pair.partition("=")
            if key:
                params[key] = unquote_plus(value)

        source = params.get("source", "")
        source_id = params.get("source_id", "")
        if not source or not source_id:
            self.reply(400, {"error": "source 与 source_id 必填"})
            return
        where = already_known(self.root, source, source_id)
        self.reply(200, {"exists": where is not None, "where": where})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/ingest":
            self.reply(404, {"error": "not found"})
            return
        if not self.authorized():
            self.reply(401, {"error": "unauthorized"})
            return
        if not rate_ok():
            self.reply(429, {"error": "rate limited"})
            return

        # 磁盘见底时提前拒收：让脚本当场看到失败并保留角标，
        # 好过收下来堆在队列里、合并时才发现放不下
        disk = disk_state(str(self.root))
        if disk["level"] == "refuse":
            self.reply(507, {"error": "insufficient storage",
                             "free_gib": disk["free_gib"]})
            return

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self.reply(400, {"error": "invalid Content-Length"})
            return
        if length <= 0:
            self.reply(400, {"error": "empty body"})
            return
        if length > MAX_BODY:
            self.reply(413, {"error": f"body too large ({length} > {MAX_BODY})"})
            return

        try:
            manifest = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("body 必须是 JSON 对象")
            blobs = decode_files(manifest)
            reason = validate_manifest(manifest)
            if reason:
                raise ValueError(reason)
        except Exception as exc:
            self.reply(400, {"error": f"{type(exc).__name__}: {exc}"})
            return

        source = str(manifest["source"])
        source_id = str(manifest["source_id"])

        try:
            with _write_lock:
                where = already_known(self.root, source, source_id)
                if where:
                    self.reply(200, {"ok": True, "duplicate": True, "where": where})
                    return
                manifest.setdefault(
                    "collected_at",
                    time.strftime("%Y-%m-%dT%H:%M:%S%z"))
                name = stage_item(self.root, manifest, blobs)
        except Exception as exc:
            self.log_message("stage failed: %s: %s", type(exc).__name__, exc)
            self.reply(500, {"error": "staging failed"})
            return

        self.log_message("queued %s (%d parts)", name, len(blobs))
        self.reply(200, {"ok": True, "duplicate": False, "item": name,
                         "parts": len(blobs)})


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="PixivCollection ingest receiver")
    parser.add_argument("--root", default="/opt/pixiv_collection")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()

    Handler.root = Path(args.root).resolve()
    Handler.token = os.environ.get("INGEST_TOKEN", "").strip()
    if not Handler.token:
        print("WARNING: INGEST_TOKEN 未设置，接口无鉴权", file=sys.stderr)

    (Handler.root / "pending").mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ingest receiver listening on {args.host}:{args.port}, "
          f"root={Handler.root}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
