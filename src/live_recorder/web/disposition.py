"""Content-Disposition。HTTP ヘッダは latin-1 だけ通す。"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote


def attachment_disposition(filename: str) -> str:
    """表示名は filename* に載せ、filename は ASCII の代替名にする。"""
    name = Path(filename).name or "download"
    fallback = _ascii_filename(name)
    encoded = quote(name, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def _ascii_filename(name: str) -> str:
    chars: list[str] = []
    for ch in name:
        if ch.isascii() and ch.isprintable() and ch not in '"\\':
            chars.append(ch)
        else:
            chars.append("_")
    text = "".join(chars).strip(" .")
    return text or "download"
