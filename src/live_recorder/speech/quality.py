from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_MAX_COMPRESSION_RATIO = 2.4
_MIN_AVG_LOGPROB = -1.0
_MIN_REPEAT_CHARS = 12
_REPEAT_WINDOW = 30.0
_MIN_LOOP_UNIT = 4
_CLAUSE_SPLIT = re.compile(r"[。．.！!？?、,，]+")
_SEPARATORS = re.compile(r"[\s\u3000。．.！!？?、,，・…]+")
# これ以上空いた語は、同じ発話の塊に入れない。
_SPOKEN_GAP = 1.0
# 境界の丸めで区間の外に出た語は、内側にあるものとして扱う。
_SEGMENT_EDGE = 0.05

# 無音で Whisper が出す定型句。本文全体がこの句だけのとき捨てる。
_HALLUCINATIONS = frozenset(
    {
        "ご視聴ありがとうございました",
        "ご視聴ありがとうございます",
        "チャンネル登録よろしくお願いします",
        "thanksforwatching",
    }
)


@dataclass(frozen=True)
class SpeechWord:
    raw: str
    start: float
    end: float

    @property
    def text(self) -> str:
        return self.raw.strip()


@dataclass(frozen=True)
class SpeechCue:
    start: float
    end: float
    text: str
    compression_ratio: float = 0.0
    avg_logprob: float = 0.0


def _has_lexical(text: str) -> bool:
    """文字または数字が一つでもあれば、その言語の発話として残す。"""
    return any(unicodedata.category(ch)[0] in {"L", "N"} for ch in text)


def keep_stable(cues: list[SpeechCue]) -> list[SpeechCue]:
    """faster-whisper が温度 0 で残す悪い窓と、無音の定型句を捨てる。"""
    kept: list[SpeechCue] = []
    previous: str | None = None
    previous_end = 0.0
    for cue in cues:
        text = cue.text.strip()
        if not text or cue.end <= cue.start:
            continue
        if cue.compression_ratio > _MAX_COMPRESSION_RATIO or cue.avg_logprob < _MIN_AVG_LOGPROB:
            continue
        if not _has_lexical(text) or _is_hallucination(text) or _clause_repeats(text):
            continue
        if (
            previous == text
            and len(text) >= _MIN_REPEAT_CHARS
            and cue.start - previous_end < _REPEAT_WINDOW
        ):
            continue
        previous = text
        previous_end = cue.end
        kept.append(
            SpeechCue(
                start=cue.start,
                end=cue.end,
                text=text,
                compression_ratio=cue.compression_ratio,
                avg_logprob=cue.avg_logprob,
            )
        )
    return kept


def collect_speech_words(raw_words) -> tuple[SpeechWord, ...]:
    """faster-whisper の語時刻を、空の語を除いて取り出す。"""
    collected: list[SpeechWord] = []
    for word in raw_words or ():
        raw = str(getattr(word, "word", "") or "")
        if not raw.strip():
            continue
        try:
            start = float(word.start)
            end = float(word.end)
        except (TypeError, ValueError, AttributeError):
            continue
        if end <= start:
            continue
        collected.append(SpeechWord(raw, start, end))
    return tuple(collected)


def refine_cue_times(cues: list[SpeechCue], words: tuple[SpeechWord, ...]) -> list[SpeechCue]:
    """本文の先頭と末尾に合う一塊の語へ、開始と終了だけを寄せる。文章は変えない。"""
    if not words:
        return cues
    ordered = tuple(sorted(words, key=lambda word: (word.start, word.end)))
    return [_refine_cue(cue, ordered) for cue in cues]


def _refine_cue(cue: SpeechCue, words: tuple[SpeechWord, ...]) -> SpeechCue:
    inside = [
        word
        for word in words
        if word.text and word.start >= cue.start - _SEGMENT_EDGE and word.end <= cue.end + _SEGMENT_EDGE
    ]
    span = _aligned_span(cue.text, inside)
    if span is None:
        return cue
    start, end = span
    if start < cue.start - _SEGMENT_EDGE or end > cue.end + _SEGMENT_EDGE or end <= start:
        return cue
    return SpeechCue(
        start=start,
        end=end,
        text=cue.text,
        compression_ratio=cue.compression_ratio,
        avg_logprob=cue.avg_logprob,
    )


def _aligned_span(text: str, words: list[SpeechWord]) -> tuple[float, float] | None:
    compact = _normalize(text)
    if not compact:
        return None
    for run in _dense_runs(words):
        covered = _cover(compact, run)
        if covered is not None:
            return covered
    return None


def _dense_runs(words: list[SpeechWord]) -> list[list[SpeechWord]]:
    if not words:
        return []
    runs: list[list[SpeechWord]] = [[words[0]]]
    for word in words[1:]:
        if word.start - runs[-1][-1].end >= _SPOKEN_GAP:
            runs.append([word])
        else:
            runs[-1].append(word)
    return runs


def _cover(compact: str, run: list[SpeechWord]) -> tuple[float, float] | None:
    """正規化した本文の先頭から末尾までを、この塊が順に覆うときだけ時刻を返す。"""
    pos = 0
    used: list[SpeechWord] = []
    for word in run:
        token = _normalize(word.text)
        if not token:
            continue
        if pos == len(compact):
            break
        if compact.startswith(token, pos):
            used.append(word)
            pos += len(token)
            continue
        if not used:
            continue
        return None
    if not used or pos != len(compact):
        return None
    return used[0].start, used[-1].end


def _is_hallucination(text: str) -> bool:
    return _normalize(text) in _HALLUCINATIONS


def _normalize(text: str) -> str:
    return _SEPARATORS.sub("", text).casefold()


def _clause_repeats(text: str) -> bool:
    parts = [part.strip() for part in _CLAUSE_SPLIT.split(text) if part.strip()]
    if len(parts) >= 3 and all(part == parts[0] for part in parts):
        return True
    compact = _SEPARATORS.sub("", text)
    if len(compact) < _MIN_LOOP_UNIT * 3:
        return False
    limit = len(compact) // 3
    for size in range(_MIN_LOOP_UNIT, limit + 1):
        if len(compact) % size:
            continue
        unit = compact[:size]
        if unit * (len(compact) // size) == compact:
            return True
    return False
