from __future__ import annotations

import json
from typing import Any

from live_recorder.platforms.normalize import tiktok_live_url, twitcasting_profile_url

_TIKTOK_QUALITIES = {
    "best": ("origin", "uhd", "hd", "sd", "ld"),
    "high": ("uhd", "hd", "sd", "ld", "origin"),
    "medium": ("sd", "ld", "hd"),
}
_FORMATS = ("flv", "hls", "cmaf")
_STREAMLINK_QUALITY = {
    "best": "best",
    "high": "720p,hd,best",
    "medium": "480p,sd,worst",
}


def streamlink_tiktok_args(binary: str, user_id: str, output: str, quality: str) -> list[str]:
    return [
        binary,
        "--no-config",
        "--loglevel",
        "warning",
        "--force",
        "--retry-max",
        "1",
        "-o",
        output,
        tiktok_live_url(user_id),
        _STREAMLINK_QUALITY.get(quality, "best"),
    ]


def streamlink_twitcasting_args(binary: str, user_id: str, output: str, quality: str) -> list[str]:
    return [
        binary,
        "--no-config",
        "--loglevel",
        "warning",
        "--force",
        "--stream-types",
        "hls",
        "--retry-max",
        "1",
        "-o",
        output,
        twitcasting_profile_url(user_id),
        _STREAMLINK_QUALITY.get(quality, "best"),
    ]


def ytdlp_args(binary: str, url: str, output: str) -> list[str]:
    return [
        binary,
        "--ignore-config",
        "--no-playlist",
        "--no-progress",
        "--no-write-info-json",
        "--no-part",
        "--hls-use-mpegts",
        "-o",
        output,
        url,
    ]


def ffmpeg_hls_args(
    binary: str,
    url: str,
    output: str,
    *,
    user_agent: str | None = None,
    referer: str | None = None,
) -> list[str]:
    args = [binary, "-hide_banner", "-loglevel", "warning", "-nostdin"]
    if user_agent:
        args.extend(["-user_agent", user_agent])
    if referer:
        args.extend(["-headers", f"Referer: {referer}\r\n"])
    args.extend(["-i", url, "-c", "copy", "-f", "mpegts", output])
    return args


def pick_tiktok_pull_url(room_info: dict[str, Any], quality: str) -> str:
    raw = room_info["stream_url"]["live_core_sdk_data"]["pull_data"]["stream_data"]
    if isinstance(raw, str):
        payload = json.loads(raw)
    elif isinstance(raw, dict):
        payload = raw
    else:
        raise KeyError("stream_data")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise KeyError("data")
    for name in _TIKTOK_QUALITIES.get(quality, _TIKTOK_QUALITIES["best"]):
        rendition = data.get(name)
        if not isinstance(rendition, dict):
            continue
        main = rendition.get("main")
        if not isinstance(main, dict):
            continue
        for fmt in _FORMATS:
            url = main.get(fmt)
            if isinstance(url, str) and url.startswith("https://"):
                return url
    raise KeyError("no published rendition")
