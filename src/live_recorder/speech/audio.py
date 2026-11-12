from __future__ import annotations

import json
import subprocess
from pathlib import Path


def extract_audio(ffmpeg: str, video: Path, wav: Path, *, ffprobe: str | None = None) -> None:
    """映像の再生時刻に合わせた WAV を書く。先頭のずれと途中の欠落は無音として残す。"""
    probe = ffprobe or _ffprobe_path(ffmpeg)
    delay_ms = _leading_delay_ms(*_stream_starts(probe, video))
    filters = "aresample=async=1:osr=16000:ochl=mono"
    if delay_ms > 0:
        filters = f"{filters},adelay={delay_ms}:all=1"
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-dts_delta_threshold",
        "86400",
        "-i",
        str(video),
        "-map",
        "0:a:0",
        "-vn",
        "-af",
        filters,
        "-c:a",
        "pcm_s16le",
        str(wav),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg が見つかりません") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip().replace("\n", " ")[:300]
        raise RuntimeError(detail or f"ffmpeg が終了コード {exc.returncode} で失敗しました") from exc


def _ffprobe_path(ffmpeg: str) -> str:
    return str(Path(ffmpeg).with_name("ffprobe"))


def _stream_starts(ffprobe: str, video: Path) -> tuple[float | None, float | None]:
    """最初の実パケットを基準にする。stream.start_time が 0 のままでも、パケットの時刻を使う。"""
    video_packet = _packet_start(ffprobe, video, "v:0")
    audio_packet = _packet_start(ffprobe, video, "a:0")
    video_start, audio_start = _declared_starts(ffprobe, video)
    return (
        video_packet if video_packet is not None else video_start,
        audio_packet if audio_packet is not None else audio_start,
    )


def _packet_start(ffprobe: str, video: Path, selector: str) -> float | None:
    payload = _probe_json(
        ffprobe,
        [
            "-select_streams",
            selector,
            "-show_entries",
            "packet=pts_time",
            "-read_intervals",
            "0%+#1",
        ],
        video,
    )
    packets = payload.get("packets") if isinstance(payload, dict) else None
    if not packets or not isinstance(packets[0], dict):
        return None
    return _float_time(packets[0].get("pts_time"))


def _declared_starts(ffprobe: str, video: Path) -> tuple[float | None, float | None]:
    payload = _probe_json(ffprobe, ["-show_entries", "stream=codec_type,start_time"], video)
    video_start = None
    audio_start = None
    streams = payload.get("streams") if isinstance(payload, dict) else None
    for stream in streams or []:
        if not isinstance(stream, dict):
            continue
        start = _float_time(stream.get("start_time"))
        if start is None:
            continue
        kind = stream.get("codec_type")
        if kind == "video" and video_start is None:
            video_start = start
        elif kind == "audio" and audio_start is None:
            audio_start = start
    return video_start, audio_start


def _probe_json(ffprobe: str, entries: list[str], video: Path) -> dict:
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", *entries, "-of", "json", str(video)],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError:
        return {}
    if proc.returncode != 0:
        return {}
    try:
        payload = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _float_time(raw: object) -> float | None:
    if raw in (None, "N/A"):
        return None
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _leading_delay_ms(video_start: float | None, audio_start: float | None) -> int:
    """音声が映像より遅れて始まるミリ秒。共通の大きな PTS は引き算で消える。"""
    if video_start is None or audio_start is None:
        return 0
    delta = audio_start - video_start
    if delta <= 0.0005:
        return 0
    return int(round(delta * 1000))
