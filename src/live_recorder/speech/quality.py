from __future__ import annotations

import re
from dataclasses import dataclass

_MAX_COMPRESSION_RATIO = 2.4
_MIN_AVG_LOGPROB = -1.0
_MIN_REPEAT_CHARS = 12
_MIN_LOOP_UNIT = 4
_WORD = re.compile(r"[0-9A-Za-z\u3040-\u30ff\u3400-\u9fff]")
_CLAUSE_SPLIT = re.compile(r"[。．.！!？?、,，]+")
_SEPARATORS = re.compile(r"[\s\u3000。．.！!？?、,，・…]+")

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
class SpeechCue:
    start: float
    end: float
    text: str
    compression_ratio: float = 0.0
    avg_logprob: float = 0.0


def keep_stable(cues: list[SpeechCue], *, hotwords: str | None = None) -> list[SpeechCue]:
    """faster-whisper が温度 0 で残す悪い窓と、無音の定型句を捨てる。"""
    kept: list[SpeechCue] = []
    previous: str | None = None
    for cue in cues:
        text = strip_hotword_echo(cue.text.strip(), hotwords)
        if not text or cue.end <= cue.start:
            continue
        if cue.compression_ratio > _MAX_COMPRESSION_RATIO or cue.avg_logprob < _MIN_AVG_LOGPROB:
            continue
        if not _WORD.search(text) or _is_hallucination(text) or _clause_repeats(text):
            continue
        if previous == text and len(text) >= _MIN_REPEAT_CHARS:
            continue
        previous = text
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


def strip_hotword_echo(text: str, hotwords: str | None) -> str:
    """単語リストが前文として渡されると、その並びが末尾に書き写される。3語以上の連続だけ外す。"""
    words = [word for word in (hotwords or "").split() if word]
    if len(words) < 3:
        return text.strip()
    tokens = text.split()
    while tokens:
        start, end = _longest_hotword_run(tokens, words)
        if end - start < 3:
            break
        del tokens[start:end]
    return " ".join(tokens).strip()


def _longest_hotword_run(tokens: list[str], words: list[str]) -> tuple[int, int]:
    best = (0, 0)
    for start, token in enumerate(tokens):
        key = _token_key(token)
        for offset, word in enumerate(words):
            if key != word:
                continue
            end = start
            index = offset
            while end < len(tokens) and index < len(words) and _token_key(tokens[end]) == words[index]:
                end += 1
                index += 1
            if end - start > best[1] - best[0]:
                best = (start, end)
    return best


def _token_key(token: str) -> str:
    return token.strip("。．.！!？?、,，・")


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
