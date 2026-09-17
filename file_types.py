"""
文件类型检测（是的，非常高端！）
"""
from __future__ import annotations
import gzip
import json
import zlib


IMAGE_TYPES = {"png", "jpg", "gif", "bmp", "svg", "tgs"}


def detect_file_type(content: bytes, fallback_name: str = "") -> str:
    """返回文件类型"""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if content.startswith(b"BM"):
        return "bmp"
    try:
        text_head = content[:4096].decode("utf-8-sig").lstrip().lower()
        if "<svg" in text_head and text_head.find("<svg") < 512:
            return "svg"
    except UnicodeDecodeError:
        pass

    if content.startswith(b"\x1f\x8b") and len(content) <= 10 * 1024 * 1024:
        try:
            payload = json.loads(gzip.decompress(content).decode("utf-8"))
            if isinstance(payload, dict) and (
                "tgs" in payload or ("v" in payload and "layers" in payload)
            ):
                return "tgs"
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, EOFError, zlib.error):
            pass

    suffix = fallback_name.rsplit(".", 1)[-1].lower() if "." in fallback_name else ""
    aliases = {"jpeg": "jpg", "svgz": "svg"}
    return aliases.get(suffix, suffix) if suffix in IMAGE_TYPES | {"jpeg", "svgz"} else "unknown"


def is_sticker_type(content: bytes, fallback_name: str = "") -> bool:
    return detect_file_type(content, fallback_name) in IMAGE_TYPES
