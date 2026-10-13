"""多来源入库：把 pending/ 队列合并进 collection.json。

设计要点见 PIPELINE.md「多来源入库」一节。核心约束：

1. 合成 id。X 的 tweet id 与 B 站动态 id 都是 snowflake（~1.9e18），
   超过 JS 的 Number.MAX_SAFE_INTEGER（9.007e15），前端直接精度丢失；
   而 collection.export() 的排序键还会把它乘以 1000。因此平台原始 id
   只作为字符串存进 source_id，内部另发一个从 9e8 起的递增 id。
   Pixiv 现役 illust id 约 1.5e8，作者 id 上限约 1e8，9e8 留足了余量。

2. 幂等。collection.json 顶层新增 sources 注册表，键为 "{source}:{source_id}"。
   重复收藏、取消再收藏、多设备同时触发都只会入库一次。

3. 不碰既有爬取路径。本模块只在 pipeline 的 crawl 与 validate 之间运行，
   产出的本地文件由既有的 validate_archive 校验、upload_to_r2 增量上传。

pending/ 布局：

    pending/<item>.json        入库清单
    pending/<item>/<n>.<ext>   清单里引用的原图
    pending/consumed/<item>/   合并成功后归档至此
    pending/failed/<item>/     合并失败移到此处，保留现场
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from collection import (
    PREVIEW_QUALITY,
    PREVIEW_SIZE,
    THUMBNAIL_QUALITY,
    THUMBNAIL_SIZE,
    get_dominant_color,
)
from ingest_schema import (  # noqa: F401  供既有调用方继续从本模块引入
    ALLOWED_EXT,
    KNOWN_SOURCES,
    MAX_PARTS,
    disk_state,
    merge_bookmark_contexts,
    validate_manifest,
)

# 合成 id 起点，需高于 Pixiv 现役 id 且远低于 JS 安全整数上限
LOCAL_ID_BASE = 900_000_000
LOCAL_AUTHOR_ID_BASE = 900_000_000


def timestamp() -> int:
    return int(time.time())


# --------------------------------------------------------------------------
# collection.json 结构扩展
# --------------------------------------------------------------------------


def ensure_registry(collection: dict[str, Any]) -> None:
    """就地补齐多来源所需的顶层结构，对既有 Pixiv 数据无副作用。"""

    collection.setdefault("sources", {})
    counters = collection.setdefault("counters", {})
    counters.setdefault("next_image_id", LOCAL_ID_BASE)
    counters.setdefault("next_author_id", LOCAL_AUTHOR_ID_BASE)

    # 兜底：万一历史数据里已存在 >= BASE 的 id，把游标顶到其后，避免撞号
    for key, base in (("next_image_id", LOCAL_ID_BASE),
                      ("next_author_id", LOCAL_AUTHOR_ID_BASE)):
        table = collection.get("images" if key == "next_image_id" else "authors", {})
        used = [int(k) for k in table if k.lstrip("-").isdigit() and int(k) >= base]
        if used:
            counters[key] = max(counters[key], max(used) + 1)


def alloc_id(collection: dict[str, Any], counter: str) -> int:
    value = int(collection["counters"][counter])
    collection["counters"][counter] = value + 1
    return value


def resolve_author(collection: dict[str, Any], source: str,
                   author: dict[str, Any]) -> int:
    """按 (source, source_uid) 找到或创建作者，返回内部 author_id。"""

    source_uid = str(author.get("source_uid") or "").strip()
    if not source_uid:
        raise ValueError("author.source_uid 不能为空")

    key = f"author:{source}:{source_uid}"
    registry = collection["sources"]
    if key in registry:
        author_id = int(registry[key])
    else:
        author_id = alloc_id(collection, "next_author_id")
        registry[key] = author_id

    name = str(author.get("name") or "").strip() or source_uid
    account = str(author.get("account") or "").strip() or source_uid

    entry = {
        "id": author_id,
        "name": name,
        "account": account,
        "source": source,
        "source_uid": source_uid,
    }
    url = str(author.get("url") or "").strip()
    if url:
        entry["url"] = url

    collection["authors"][str(author_id)] = {
        "update": timestamp(),
        "data": entry,
    }
    return author_id


def register_tags(collection: dict[str, Any], tags: list[str]) -> list[str]:
    """把标签写进 tags 表，返回去重后的标签名列表。"""

    names: list[str] = []
    for raw in tags:
        name = str(raw).strip()
        if not name or name in names:
            continue
        names.append(name)
        if name not in collection["tags"]:
            collection["tags"][name] = {
                "update": timestamp(),
                "data": {"name": name, "translated_name": None},
            }
    return names


# --------------------------------------------------------------------------
# 图片处理（与 collection.py 的私有方法保持同参数，避免两套观感）
# --------------------------------------------------------------------------


def _flatten(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA"):
        background = Image.new(img.mode[:-1], img.size, "white")
        background.paste(img, img.split()[-1])
        return background
    if img.mode == "P":
        return img.convert("RGB")
    return img


def derive_variants(paths: dict[str, str], filename: str, stem: str) -> None:
    """由原图生成 preview / thumbnail，与既有 Pixiv 图产出规格一致。"""

    for kind, size, quality in (
        ("preview", PREVIEW_SIZE, PREVIEW_QUALITY),
        ("thumbnail", THUMBNAIL_SIZE, THUMBNAIL_QUALITY),
    ):
        with Image.open(paths["original"] + filename) as img:
            img.thumbnail(size)
            _flatten(img).save(paths[kind] + stem + ".webp", "WEBP",
                               quality=quality)


def probe_image(path: str) -> tuple[tuple[int, int], str]:
    with Image.open(path) as img:
        size = img.size
        color = get_dominant_color(img)
    return size, color


# --------------------------------------------------------------------------
# 合并（清单校验见 ingest_schema.validate_manifest）
# --------------------------------------------------------------------------


def merge_item(collection: dict[str, Any], paths: dict[str, str],
               manifest: dict[str, Any], item_dir: Path) -> dict[str, Any]:
    """把一个清单合并进 collection，返回结果摘要。"""

    source = str(manifest["source"]).strip()
    source_id = str(manifest["source_id"]).strip()
    key = f"{source}:{source_id}"

    if key in collection["sources"]:
        image_id = collection['sources'][key]
        data = collection['images'][str(image_id)]['data']
        contexts = merge_bookmark_contexts(data.get('bookmark_contexts'), manifest.get('bookmark_contexts'))
        if contexts:
            data['bookmark_contexts'] = contexts
        return {"status": "duplicate", "key": key,
                "image_id": collection["sources"][key]}

    files = manifest["files"]
    for index, item in enumerate(files):
        if not (item_dir / str(item["file"])).is_file():
            raise FileNotFoundError(f"缺少原图 files[{index}]: {item['file']}")

    author_id = resolve_author(collection, source, manifest["author"])
    image_id = alloc_id(collection, "next_image_id")
    tags = register_tags(collection, manifest.get("tags") or [])

    now_iso = datetime.now().astimezone().isoformat()
    image_data = {
        "id": image_id,
        "author_id": author_id,
        "title": str(manifest.get("title") or "").strip() or "未命名",
        "caption": str(manifest.get("caption") or ""),
        "tags": tags,
        "created_at": str(manifest.get("created_at") or now_iso),
        "sanity_level": int(manifest.get("sanity_level") or 2),
        "x_restrict": 1 if manifest.get("x_restrict") else 0,
        "bookmark": int(manifest.get("bookmark") or 0),
        "view": int(manifest.get("view") or 0),
        "source": source,
        "source_id": source_id,
        "collected_at": str(manifest.get("collected_at") or now_iso),
    }
    source_url = str(manifest.get("source_url") or "").strip()
    if source_url:
        image_data["source_url"] = source_url
    contexts = merge_bookmark_contexts(manifest.get('bookmark_contexts'))
    if contexts:
        image_data['bookmark_contexts'] = contexts

    written: list[str] = []
    registered: list[str] = []
    try:
        for part, item in enumerate(files):
            src = item_dir / str(item["file"])
            ext = src.name.rsplit(".", 1)[-1].lower()
            if ext == "jpeg":
                ext = "jpg"
            stem = f"{image_id}_p{part}"
            filename = f"{stem}.{ext}"

            shutil.copy2(src, paths["original"] + filename)
            written.append(paths["original"] + filename)

            size, dominant_color = probe_image(paths["original"] + filename)
            derive_variants(paths, filename, stem)
            written.append(paths["preview"] + stem + ".webp")
            written.append(paths["thumbnail"] + stem + ".webp")

            file_data = {
                "id": image_id,
                "part": part,
                "size": list(size),
                "ext": ext,
                "dominant_color": dominant_color,
                "filesize": os.path.getsize(paths["original"] + filename),
            }
            origin_url = str(item.get("origin_url") or "").strip()
            if origin_url:
                file_data["origin_url"] = origin_url

            collection["files"][filename] = {
                "update": timestamp(),
                "data": file_data,
            }
            registered.append(filename)
    except Exception:
        # 任一分P 失败就回滚已落地的文件与条目，不留半个作品
        for path in written:
            if os.path.exists(path):
                os.remove(path)
        for filename in registered:
            collection["files"].pop(filename, None)
        raise

    collection["images"][str(image_id)] = {
        "update": timestamp(),
        "data": image_data,
    }
    collection["sources"][key] = image_id

    return {"status": "merged", "key": key, "image_id": image_id,
            "author_id": author_id, "parts": len(files)}


def merge_pending(root: Path, paths: dict[str, str],
                  collection: dict[str, Any]) -> dict[str, Any]:
    """扫描 pending/ 并合并，返回报告。collection 会被就地修改。"""

    pending = root / "pending"
    disk = disk_state(str(root))
    report: dict[str, Any] = {
        "created_at": datetime.now().astimezone().isoformat(),
        "scanned": 0, "merged": 0, "duplicate": 0, "failed": 0,
        "disk": disk,
        "items": [],
    }
    if not pending.is_dir():
        return report

    # 磁盘见底时不动 pending：投递还在队列里，扩容后再跑一次即可，
    # 比写到一半塞满磁盘、连爬虫一起拖垮要好收拾得多
    if disk["level"] == "refuse":
        report["skipped"] = f"磁盘剩余 {disk['free_gib']}GiB，低于阈值，本次跳过入库"
        print(report["skipped"], file=sys.stderr)
        return report
    if disk["level"] == "warn":
        print(f"警告：磁盘剩余 {disk['free_gib']}GiB，接近阈值", file=sys.stderr)

    ensure_registry(collection)
    failed = pending / "failed"

    for manifest_path in sorted(pending.glob("*.json")):
        item = manifest_path.stem
        item_dir = pending / item
        report["scanned"] += 1
        entry: dict[str, Any] = {"item": item}

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            reason = validate_manifest(manifest)
            if reason:
                raise ValueError(reason)
            key = f"{manifest['source']}:{str(manifest['source_id']).strip()}"
            if not item_dir.is_dir() and key not in collection["sources"]:
                raise FileNotFoundError(f"缺少原图目录 {item_dir.name}/")

            result = merge_item(collection, paths, manifest, item_dir)
            entry.update(result)

            if result["status"] == "duplicate":
                report["duplicate"] += 1
            else:
                report["merged"] += 1

            # Keep the queue until BOTH archive JSON files have been committed.
            # Retry sees a duplicate if collection.json committed before a crash.
        except Exception as exc:  # 单项失败不影响其余
            report["failed"] += 1
            entry.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            failed.mkdir(parents=True, exist_ok=True)
            try:
                if manifest_path.exists():
                    shutil.move(str(manifest_path), str(failed / manifest_path.name))
                if item_dir.is_dir():
                    target = failed / item
                    if target.exists():
                        shutil.rmtree(target)
                    shutil.move(str(item_dir), str(target))
            except Exception:
                pass

        report["items"].append(entry)

    return report


def commit_pending(root: Path, report: dict[str, Any]) -> None:
    """Acknowledge only items present in both durable metadata files.

    Move originals first and the manifest last. On interruption the registry
    allows retry even when the queue's image directory has already moved.
    Existing consumed material is preserved under a unique name, never deleted.
    """
    collection = json.loads((root / 'collection.json').read_text('utf-8'))
    exported = json.loads((root / 'images.json').read_text('utf-8'))
    exported_parts = {}
    for image in exported:
        exported_parts.setdefault(int(image['id']), set()).add(int(image['part']))
    pending = root / 'pending'
    consumed = pending / 'consumed'
    for entry in report['items']:
        if entry.get('status') not in ('merged', 'duplicate'):
            continue
        image_id = entry['image_id']
        item = entry['item']
        manifest = pending / (item + '.json')
        expected = len(json.loads(manifest.read_text('utf-8'))['files'])
        if (collection.get('sources', {}).get(entry['key']) != image_id
                or exported_parts.get(image_id) != set(range(expected))):
            raise RuntimeError('Pending item has not been durably exported')
        consumed.mkdir(parents=True, exist_ok=True)
        item_dir = pending / item
        if item_dir.is_dir():
            destination = consumed / item
            if destination.exists():
                destination = consumed / (item + '.' + uuid.uuid4().hex)
            item_dir.rename(destination)
        if manifest.exists():
            destination = consumed / manifest.name
            if destination.exists():
                destination = consumed / (item + '.' + uuid.uuid4().hex + '.json')
            manifest.rename(destination)
