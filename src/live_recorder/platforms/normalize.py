from __future__ import annotations

import re
from urllib.parse import urlparse

_TIKTOK_ID = re.compile(r"^[A-Za-z0-9._]{2,24}$")
_TWITCASTING_ID = re.compile(r"^[A-Za-z0-9_]{1,50}$")
_TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}


class NormalizeError(ValueError):
    pass


def normalize_tiktok(raw: str) -> str:
    text = raw.strip()
    if not text or any(ord(ch) < 32 for ch in text):
        raise NormalizeError("TikTok のユーザー指定が空です")
    if "\\" in text or text.startswith("/") or ".." in text.split("/"):
        raise NormalizeError("不正な TikTok 指定です")
    if "://" in text or text.startswith("//"):
        parsed = urlparse(text if "://" in text else f"https:{text}")
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise NormalizeError("TikTok の URL が不正です")
        host = (parsed.hostname or "").lower()
        if host not in _TIKTOK_HOSTS:
            raise NormalizeError("TikTok 以外のホストは指定できません")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts or not parts[0].startswith("@"):
            raise NormalizeError("TikTok のプロフィール URL ではありません")
        if len(parts) > 2 or (len(parts) == 2 and parts[1] != "live"):
            raise NormalizeError("プロフィール URL または /live URL だけを指定できます")
        text = parts[0]
    if text.startswith("@"):
        text = text[1:]
    if text in {".", ".."} or ".." in text or not _TIKTOK_ID.fullmatch(text):
        raise NormalizeError("TikTok ユーザーIDの形式が不正です")
    return text


def tiktok_profile_url(user_id: str) -> str:
    return f"https://www.tiktok.com/@{normalize_tiktok(user_id)}"


def tiktok_live_url(user_id: str) -> str:
    return f"https://www.tiktok.com/@{normalize_tiktok(user_id)}/live"


def normalize_twitcasting(raw: str) -> str:
    text = raw.strip()
    if not text or any(ord(ch) < 32 for ch in text):
        raise NormalizeError("ツイキャスのユーザー指定が空です")
    if "\\" in text or ".." in text.split("/"):
        raise NormalizeError("不正なツイキャス指定です")
    if "://" in text or text.startswith("//"):
        parsed = urlparse(text if "://" in text else f"https:{text}")
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise NormalizeError("ツイキャスの URL が不正です")
        host = (parsed.hostname or "").lower()
        if host != "twitcasting.tv":
            raise NormalizeError("ツイキャス以外のホストは指定できません")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise NormalizeError("ツイキャスのユーザーIDがありません")
        text = parts[0]
    if not _TWITCASTING_ID.fullmatch(text):
        raise NormalizeError("ツイキャスのユーザーIDの形式が不正です")
    return text


def twitcasting_profile_url(user_id: str) -> str:
    return f"https://twitcasting.tv/{normalize_twitcasting(user_id)}"


def clean_display_name(value: str | None, fallback: str) -> str:
    if not value:
        return fallback[:100]
    text = "".join(ch for ch in value.strip() if ch.isprintable())
    text = text[:100].strip()
    return text or fallback[:100]
