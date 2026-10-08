"""缩略图生成与磁盘缓存（按 路径+修改时间 做 key，按需生成，不改动原图）。"""
from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageOps

from .config import THUMB_DIR, THUMB_SIZE


def _key(path: str, mtime: float, size: int) -> Path:
    raw = f"{Path(path).resolve()}|{int(mtime)}|{size}".encode("utf-8", "ignore")
    digest = hashlib.sha1(raw).hexdigest()[:20]
    shard = THUMB_DIR / digest[:2]
    shard.mkdir(parents=True, exist_ok=True)
    return shard / f"{digest}.jpg"


def ensure_thumb(path: str, mtime: float = 0, size: int = 0) -> Path | None:
    """返回缩略图文件路径；失败返回 None。"""
    target = _key(path, mtime, size)
    if target.exists() and target.stat().st_size > 0:
        return target
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA", "P", "1"):
                img = img.convert("RGB")
            elif img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
            tmp = target.with_suffix(".tmp")
            img.save(tmp, "JPEG", quality=80, optimize=True)
            tmp.replace(target)
        return target
    except Exception:
        return None
