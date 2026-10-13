"""接收端与合并端共用的约定：pending 清单格式，以及磁盘水位阈值。

单独成文件是为了让 receiver.py 能零依赖引入：接收端是公网可达的服务，
不应该因为 pixivpy3 / PIL / loguru 出问题就起不来，而 ingest.py 需要它们。
两边共用同一份定义，格式与阈值不会漂移。
"""

from __future__ import annotations

import shutil
import re
from typing import Any

KNOWN_SOURCES = {"pixiv", "twitter", "bilibili", "mihuashi", "local"}
ALLOWED_EXT = {"jpg", "jpeg", "png", "gif", "webp", "avif"}
MAX_PARTS = 50


def merge_bookmark_accounts(*groups: Any) -> list[str]:
    """Only opaque local labels are allowed; never infer a login from artists."""
    return sorted({label for group in groups if isinstance(group, list)
                   for label in group if isinstance(label, str)
                   and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,31}', label)})


def merge_bookmark_contexts(*groups: Any) -> list[dict[str, str]]:
    """Keep distinct direct/quote associations, excluding malformed metadata."""
    contexts = {}
    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict) or item.get('kind') not in ('direct', 'quote'):
                continue
            source_id = str(item.get('source_id') or '')
            url = str(item.get('source_url') or '')
            if (not re.fullmatch(r'[1-9][0-9]{0,29}', source_id)
                    or not re.fullmatch(r'https://x\.com/(?:[A-Za-z0-9_]{1,50}/status|i/web/status)/' + source_id, url)):
                continue
            contexts[(item['kind'], source_id)] = {
                'kind': item['kind'], 'source_id': source_id, 'source_url': url}
    return [contexts[key] for key in sorted(contexts)]

# 磁盘水位。原图归档是只增不减的，装满会让爬虫和入库一起失败，
# 所以宁可提前拒收：拒掉的投递可以重投，写坏的归档不好收拾。
DISK_REFUSE_BYTES = 2 * 1024 ** 3   # 低于此值直接拒绝新入库
DISK_WARN_BYTES = 5 * 1024 ** 3     # 低于此值仍然收，但在报告里标出来


def disk_state(path: str) -> dict[str, Any]:
    """返回 {free, total, level}，level 取 ok / warn / refuse。"""

    usage = shutil.disk_usage(path)
    if usage.free < DISK_REFUSE_BYTES:
        level = "refuse"
    elif usage.free < DISK_WARN_BYTES:
        level = "warn"
    else:
        level = "ok"
    return {
        "free": usage.free,
        "total": usage.total,
        "free_gib": round(usage.free / 1024 ** 3, 2),
        "level": level,
    }


def validate_manifest(manifest: dict[str, Any]) -> str:
    """返回空串表示通过，否则返回不通过的原因。"""

    source = str(manifest.get("source") or "").strip()
    if source not in KNOWN_SOURCES:
        return f"未知 source: {source!r}"
    if source == "pixiv":
        return "pixiv 走既有爬虫路径，不接受 pending 入库"
    if not str(manifest.get("source_id") or "").strip():
        return "source_id 为空"

    author = manifest.get("author")
    if not isinstance(author, dict):
        return "缺少 author"
    if not str(author.get("source_uid") or "").strip():
        return "author.source_uid 为空"

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        return "files 为空"
    if len(files) > MAX_PARTS:
        return f"分P 过多（{len(files)} > {MAX_PARTS}）"
    for index, item in enumerate(files):
        if not isinstance(item, dict) or not str(item.get("file") or "").strip():
            return f"files[{index}] 缺少 file"
        name = str(item["file"])
        if "/" in name or "\\" in name or name.startswith("."):
            return f"files[{index}].file 非法: {name!r}"
        if name.rsplit(".", 1)[-1].lower() not in ALLOWED_EXT:
            return f"files[{index}] 扩展名不支持: {name!r}"
    return ""
