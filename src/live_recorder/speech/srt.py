from __future__ import annotations

import re
import uuid
from pathlib import Path

from live_recorder.recording.files import fit_export_name, resolve_stored
from live_recorder.speech.quality import SpeechCue

SPEECH_EXPORT_SUFFIX = "_Speech.srt"


def speech_export_name(video_name: str) -> str:
    """録画ファイルと同じ幹に _Speech.srt を付ける。255 バイトを超えるときは幹を切る。"""
    stem = Path(video_name).name
    stem = Path(stem).stem.replace('"', "").replace("\r", "").replace("\n", "").strip()
    if not stem or stem in {".", ".."} or "/" in stem or "\\" in stem:
        stem = "speech"
    return fit_export_name(stem, SPEECH_EXPORT_SUFFIX, fallback="speech")


def speech_srt_path(video: Path) -> Path:
    return video.with_name(speech_export_name(video.name))


def stored_speech_srt(root: Path, relative: str | None) -> Path | None:
    """DB に記録した音声字幕のパス。記録が無い、または録画フォルダの外なら None。"""
    if not relative:
        return None
    try:
        return resolve_stored(root, relative)
    except ValueError:
        return None


def unlink_speech_sidecar(video: Path) -> None:
    extra = speech_srt_path(video)
    if extra.is_file():
        extra.unlink()


_CUE_TIME = re.compile(
    r"(?P<sh>\d+):(?P<sm>\d+):(?P<ss>\d+),(?P<sms>\d+)\s*-->\s*"
    r"(?P<eh>\d+):(?P<em>\d+):(?P<es>\d+),(?P<ems>\d+)"
)


def _srt_ms(match: re.Match[str], prefix: str) -> int:
    return (
        int(match.group(f"{prefix}h")) * 3_600_000
        + int(match.group(f"{prefix}m")) * 60_000
        + int(match.group(f"{prefix}s")) * 1000
        + int(match.group(f"{prefix}ms"))
    )


def parse_speech_srt(content: str) -> list[tuple[int, int, str]]:
    """SRT を開始・終了ミリ秒と本文の列に戻す。壊れた塊は飛ばす。"""
    cues: list[tuple[int, int, str]] = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        time_index = 1 if lines[0].isdigit() else 0
        if time_index >= len(lines):
            continue
        match = _CUE_TIME.search(lines[time_index])
        if match is None:
            continue
        text = "\n".join(lines[time_index + 1 :]).strip()
        if not text:
            continue
        cues.append((_srt_ms(match, "s"), _srt_ms(match, "e"), text))
    return cues


def read_speech_cues(path: Path) -> list[tuple[int, int, str]]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return parse_speech_srt(content)


def format_srt_time(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    if total_ms < 0:
        total_ms = 0
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_speech_srt(cues: list[SpeechCue], output_path: Path) -> int:
    """キューを原子的に書く。1件も無ければ既存ファイルを消して 0 を返す。"""
    lines: list[str] = []
    cue_index = 1
    for cue in cues:
        text = cue.text.strip()
        if not text:
            continue
        lines.append(str(cue_index))
        lines.append(f"{format_srt_time(cue.start)} --> {format_srt_time(cue.end)}")
        lines.append(text)
        lines.append("")
        cue_index += 1
    if not lines:
        output_path.unlink(missing_ok=True)
        return 0
    temporary_path = output_path.with_name(f".srt-{uuid.uuid4().hex[:12]}.tmp")
    try:
        temporary_path.write_text("\n".join(lines), encoding="utf-8")
        temporary_path.replace(output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return cue_index - 1
