from __future__ import annotations

import re

_TOOL_VERSION = re.compile(r"^(?:ffmpeg|ffprobe)\s+version\s+(\S+)", re.IGNORECASE)


def tool_version(text: str) -> str:
    """Keep the version token from `ffmpeg -version` or `ffprobe -version`."""
    line = text.strip().splitlines()[0] if text and text.strip() else ""
    if not line:
        return ""
    match = _TOOL_VERSION.match(line)
    return match.group(1) if match else line


def distribution_version(name: str) -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:
        return ""
    try:
        return version(name) or ""
    except PackageNotFoundError:
        return ""


def version_from_tiktoklive(module: object) -> str:
    raw = getattr(module, "__version__", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    nested = getattr(raw, "__version__", None)
    if isinstance(nested, str) and nested.strip():
        return nested.strip()
    return "installed"


def tiktoklive_status() -> tuple[str, bool]:
    """Return (version, installed). Version is empty when the package is missing."""
    found = distribution_version("TikTokLive")
    if found:
        return found, True
    try:
        import TikTokLive
    except Exception:
        return "", False
    return version_from_tiktoklive(TikTokLive), True
