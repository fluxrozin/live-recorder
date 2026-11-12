from __future__ import annotations

import json
import re
import subprocess
import uuid
from pathlib import Path

from live_recorder.comments.export import render_srt
from live_recorder.comments.segments import comment_export_name
from live_recorder.speech.srt import parse_speech_srt, read_speech_cues, speech_srt_path

_REF_W = 1080
_REF_H = 1920
_FALLBACK_SIZE = (_REF_W, _REF_H)
_COMMENT_LINES = 2
_SPEECH_STYLE = "Speech"
_COMMENT_STYLE = "Comment"
_SPEECH_COLOUR = "&H00FFFFFF"
_COMMENT_COLOUR = "&H0000FFFF"

Cue = tuple[int, int, str]
# 次の音声字幕がこの以内に続くときは、文字数で終点を切らない。
_HANDOFF_MS = 400


def combined_ass_path(video: Path) -> Path:
    """MPC-HC が同じ幹の動画と一緒に読むファイル。"""
    return video.with_suffix(".ass")


def unlink_combined_ass(video: Path) -> None:
    extra = combined_ass_path(video)
    if extra.is_file():
        extra.unlink()


def escape_ass(text: str) -> str:
    """制御文字を文字として残し、改行だけを ASS の改行にする。"""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = cleaned.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
    return cleaned.replace("\n", r"\N")


def format_ass_time(ms: int) -> str:
    if ms < 0:
        ms = 0
    centiseconds = (ms + 5) // 10
    hours, remainder = divmod(centiseconds, 100 * 60 * 60)
    minutes, remainder = divmod(remainder, 100 * 60)
    seconds, cents = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{cents:02d}"


def display_size_from_probe(payload: object) -> tuple[int, int] | None:
    """ffprobe の JSON から表示サイズを取る。90 度回転なら幅と高さを入れ替える。"""
    if not isinstance(payload, dict):
        return None
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return None
    for stream in streams:
        if not isinstance(stream, dict) or stream.get("codec_type") not in {None, "video"}:
            continue
        width = _positive(stream.get("width"))
        height = _positive(stream.get("height"))
        if width is None or height is None:
            continue
        if _quarter_turn(stream):
            return height, width
        return width, height
    return None


def probe_display_size(ffprobe: str, video: Path) -> tuple[int, int]:
    if not video.is_file():
        return _FALLBACK_SIZE
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_streams",
                "-of",
                "json",
                str(video),
            ],
            check=False,
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return _FALLBACK_SIZE
    if completed.returncode != 0:
        return _FALLBACK_SIZE
    try:
        payload = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return _FALLBACK_SIZE
    return display_size_from_probe(payload) or _FALLBACK_SIZE


def render_ass(
    speech: list[Cue],
    comments: list[Cue],
    *,
    width: int,
    height: int,
) -> str:
    """音声字幕は下段、コメントはそのすぐ上。同時のコメントは上へ積む。"""
    play_w = width if width > 0 else _REF_W
    play_h = height if height > 0 else _REF_H
    body_size = _scale(48, play_h, _REF_H)
    top = _scale(80, play_h, _REF_H)
    margin_v = _scale(40, play_h, _REF_H)
    line_height = max(1, round(body_size * 1.8), round(play_h * 0.055))
    comment_base = margin_v + line_height
    lane_count = max(1, (play_h - top - comment_base) // line_height)
    events = _speech_events(speech)
    events.extend(
        _comment_events(comments, base=comment_base, line_height=line_height, lanes=lane_count)
    )
    events.sort(key=lambda item: (item[0], item[1], item[2]))
    lines = [
        "[Script Info]",
        "Title: Live Recorder",
        "ScriptType: v4.00+",
        f"PlayResX: {play_w}",
        f"PlayResY: {play_h}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        _style(
            _SPEECH_STYLE,
            body_size,
            _SPEECH_COLOUR,
            outline=_scale(2, play_h, _REF_H),
            shadow=0,
            alignment=2,
            margin_l=_scale(40, play_w, _REF_W),
            margin_r=_scale(40, play_w, _REF_W),
            margin_v=_scale(40, play_h, _REF_H),
        ),
        _style(
            _COMMENT_STYLE,
            body_size,
            _COMMENT_COLOUR,
            outline=_scale(2, play_h, _REF_H),
            shadow=0,
            alignment=2,
            margin_l=_scale(40, play_w, _REF_W),
            margin_r=_scale(40, play_w, _REF_W),
            margin_v=_scale(40, play_h, _REF_H),
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    lines.extend(text for _start, _layer, _order, text in events)
    lines.append("")
    return "\n".join(lines)


_BY_VIDEO_NAME = object()


def refresh_combined_ass(
    video: Path,
    *,
    ffprobe: str = "ffprobe",
    play_size: tuple[int, int] | None = None,
    speech_srt: Path | None | object = _BY_VIDEO_NAME,
) -> Path | None:
    """隣の SRT から同名の ASS を作り直す。両方空なら既存ファイルを消して None を返す。

    speech_srt を渡すと、動画名から探さずにその音声字幕を使う。None なら音声字幕なし。
    """
    source = speech_srt_path(video) if speech_srt is _BY_VIDEO_NAME else speech_srt
    speech = read_speech_cues(source) if isinstance(source, Path) else []
    comments = comment_cues(video)
    target = combined_ass_path(video)
    if not speech and not comments:
        target.unlink(missing_ok=True)
        return None
    width, height = play_size or probe_display_size(ffprobe, video)
    body = render_ass(speech, comments, width=width, height=height)
    temporary = target.with_name(f".ass-{uuid.uuid4().hex[:12]}.tmp")
    try:
        temporary.write_text(body, encoding="utf-8-sig")
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


def comment_cues(video: Path) -> list[Cue]:
    """コメント SRT を読む。無いときは、以前の JSONL を同じ時刻で字幕にする。"""
    srt = video.with_name(comment_export_name(video.name))
    if srt.is_file():
        return read_speech_cues(srt)
    return _legacy_jsonl_cues(video.with_suffix(".jsonl"))


def _legacy_jsonl_cues(path: Path) -> list[Cue]:
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict[str, object]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    if not rows:
        return []
    return parse_speech_srt(render_srt(rows))


def _speech_events(cues: list[Cue]) -> list[tuple[int, int, int, str]]:
    prepared: list[tuple[int, int, int, str]] = []
    for index, (start, end, text) in enumerate(cues):
        shown = text.strip()
        if not shown or end <= start:
            continue
        plain = _plain_text(shown)
        if not plain:
            continue
        prepared.append((index, start, end, plain))
    prepared.sort(key=lambda item: (item[1], item[0]))
    events: list[tuple[int, int, int, str]] = []
    for position, (index, start, end, plain) in enumerate(prepared):
        nxt = prepared[position + 1][1] if position + 1 < len(prepared) else None
        follows = nxt is not None and nxt <= end + _HANDOFF_MS
        visible_end = end if follows else _speech_visible_end(start, end, plain)
        events.append((start, 0, index, _dialogue(0, start, visible_end, _SPEECH_STYLE, r"{\an2}" + escape_ass(plain))))
    return events


def _comment_events(
    cues: list[Cue],
    *,
    base: int,
    line_height: int,
    lanes: int,
) -> list[tuple[int, int, int, str]]:
    ordered = sorted(enumerate(cues), key=lambda item: (item[1][0], item[0]))
    free = [0] * lanes
    events: list[tuple[int, int, int, str]] = []
    for index, (start, end, text) in ordered:
        shown = _clip_lines(text)
        plain = _plain_text(shown)
        if not plain or end <= start:
            continue
        span = min(_line_count(plain), lanes)
        lane = _first_open_lane(free, start, span)
        for slot in range(lane, lane + span):
            free[slot] = end
        placed = escape_ass(plain)
        lift = base + lane * line_height
        events.append(
            (start, 1, index, _dialogue(1, start, end, _COMMENT_STYLE, r"{\an2}" + placed, margin_v=lift))
        )
    return events


def _first_open_lane(free: list[int], start: int, span: int) -> int:
    chosen = 0
    earliest = None
    for lane in range(0, len(free) - span + 1):
        ready = max(free[lane : lane + span])
        if ready <= start:
            return lane
        if earliest is None or ready < earliest:
            earliest = ready
            chosen = lane
    return chosen


def _speech_visible_end(start: int, end: int, text: str) -> int:
    """Whisper の区間が無音まで伸びても、短い文を画面に残さない。"""
    chars = len("".join(text.split()))
    limit = max(2_500, min(10_000, chars * 400))
    return min(end, start + limit)


def _plain_text(text: str) -> str:
    """絵文字は字間を崩すので、字幕の本文からは除く。"""
    return _EMOJI.sub("", text).strip()


_EMOJI = re.compile(
    "["
    "\U0001f1e6-\U0001f1ff"
    "\U0001f300-\U0001faff"
    "\U00002600-\U000027bf"
    "\U0001f000-\U0001f0ff"
    "\u200d"
    "\ufe0f"
    "]+"
)


def _clip_lines(text: str) -> str:
    lines = [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    kept = [line for line in lines if line][:_COMMENT_LINES]
    return "\n".join(kept)


def _line_count(text: str) -> int:
    return max(1, text.count("\n") + 1)


def _dialogue(layer: int, start: int, end: int, style: str, text: str, margin_v: int = 0) -> str:
    start_cs = max(0, (start + 5) // 10)
    end_cs = max(start_cs + 1, (end + 5) // 10)
    return (
        f"Dialogue: {layer},{_clock(start_cs)},{_clock(end_cs)},{style},,0,0,{margin_v},,{text}"
    )


def _clock(centiseconds: int) -> str:
    hours, remainder = divmod(centiseconds, 100 * 60 * 60)
    minutes, remainder = divmod(remainder, 100 * 60)
    seconds, cents = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{cents:02d}"


def _style(
    name: str,
    size: int,
    primary: str,
    *,
    outline: int,
    shadow: int,
    alignment: int,
    margin_l: int,
    margin_r: int,
    margin_v: int,
) -> str:
    return (
        f"Style: {name},,{size},{primary},&H000000FF,&H00000000,&H90000000,"
        f"0,0,0,0,100,100,0,0,1,{outline},{shadow},{alignment},{margin_l},{margin_r},{margin_v},1"
    )


def _scale(value: int, play: int, ref: int) -> int:
    if value <= 0:
        return 0
    return max(1, round(value * play / ref))


def _positive(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _quarter_turn(stream: dict) -> bool:
    raw_values: list[object] = []
    tags = stream.get("tags")
    if isinstance(tags, dict) and tags.get("rotate") is not None:
        raw_values.append(tags.get("rotate"))
    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data:
            if isinstance(item, dict) and item.get("rotation") is not None:
                raw_values.append(item.get("rotation"))
    for raw in raw_values:
        try:
            degrees = abs(int(float(raw))) % 360
        except (TypeError, ValueError):
            continue
        if degrees in {90, 270}:
            return True
    return False
