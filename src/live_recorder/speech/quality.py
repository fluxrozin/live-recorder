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
# 縦画面のフォント 48 で、およそ1行に収まる字数。
_PHRASE_CHARS = 16
# 語と語のあいだがこれ以上空いたら、別の字幕にする。
_PHRASE_PAUSE = 0.4
# これより短い間隔の英数字同士・カタカナ同士は、1語の続きとして残す。
_PHRASE_JOIN = 0.15

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
    words: tuple[SpeechWord, ...] = ()


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
        collected.append(SpeechWord(raw, start, end))
    return tuple(collected)


def keep_stable(cues: list[SpeechCue], *, hotwords: str | None = None) -> list[SpeechCue]:
    """faster-whisper が温度 0 で残す悪い窓と、無音の定型句を捨てる。"""
    kept: list[SpeechCue] = []
    previous: str | None = None
    for cue in cues:
        text, words = _without_hotword_echo(cue.text.strip(), cue.words, hotwords)
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
                words=words,
            )
        )
    return kept


def split_phrases(cues: list[SpeechCue]) -> list[SpeechCue]:
    """語の時刻で字幕を割る。無音と、1行を超える手前の語境界で切る。英数字とカタカナの断片は残す。"""
    pieces: list[SpeechCue] = []
    for cue in cues:
        pieces.extend(_split_cue(cue))
    return pieces


def strip_hotword_echo(text: str, hotwords: str | None) -> str:
    """単語リストが前文として渡されると、その並びが末尾に書き写される。3語以上の連続だけ外す。"""
    return _without_hotword_echo(text, (), hotwords)[0]


def _without_hotword_echo(
    text: str,
    words: tuple[SpeechWord, ...],
    hotwords: str | None,
) -> tuple[str, tuple[SpeechWord, ...]]:
    catalog = [word for word in (hotwords or "").split() if word]
    if len(catalog) < 3:
        return text.strip(), words
    tokens = text.split()
    removed = set(_removed_token_indexes(tokens, catalog))
    cleaned = " ".join(token for index, token in enumerate(tokens) if index not in removed).strip()
    if not removed or not words:
        return cleaned, words
    groups = _align_words(words, tokens)
    if groups is None:
        return cleaned, ()
    kept = tuple(word for index, group in enumerate(groups) if index not in removed for word in group)
    return cleaned, kept


def _removed_token_indexes(tokens: list[str], catalog: list[str]) -> list[int]:
    alive = list(range(len(tokens)))
    removed: list[int] = []
    while alive:
        live = [tokens[index] for index in alive]
        start, end = _longest_hotword_run(live, catalog)
        if end - start < 3:
            break
        removed.extend(alive[start:end])
        del alive[start:end]
    return removed


def _align_words(words: tuple[SpeechWord, ...], tokens: list[str]) -> list[list[SpeechWord]] | None:
    """空白区切りの本文へ、より細かい語時刻を対応させる。合わなければ None。"""
    groups: list[list[SpeechWord]] = []
    index = 0
    for token in tokens:
        target = _token_key(token)
        group: list[SpeechWord] = []
        if not target:
            while index < len(words) and not _token_key(words[index].text):
                group.append(words[index])
                index += 1
            groups.append(group)
            continue
        built = ""
        while index < len(words) and built != target:
            group.append(words[index])
            piece = _token_key(words[index].text)
            index += 1
            if not piece:
                continue
            built += piece
            if len(built) > len(target):
                return None
        if built != target:
            return None
        groups.append(group)
    if index != len(words):
        return None
    return groups


def _split_cue(cue: SpeechCue) -> list[SpeechCue]:
    words = [word for word in cue.words if word.text and word.end > word.start]
    if not words:
        return [cue]
    groups: list[list[SpeechWord]] = []
    current: list[SpeechWord] = []
    chars = 0
    for atom in _word_atoms(words):
        gap = atom[0].start - current[-1].end if current else 0.0
        atom_chars = sum(len(word.text) for word in atom)
        if current and (gap >= _PHRASE_PAUSE or chars + atom_chars > _PHRASE_CHARS):
            groups.append(current)
            current = []
            chars = 0
        current.extend(atom)
        chars += atom_chars
    if current:
        groups.append(current)
    pieces: list[SpeechCue] = []
    for group in groups:
        text = "".join(word.raw for word in group).strip()
        if not text or group[-1].end <= group[0].start:
            continue
        pieces.append(
            SpeechCue(
                start=group[0].start,
                end=group[-1].end,
                text=text,
                compression_ratio=cue.compression_ratio,
                avg_logprob=cue.avg_logprob,
            )
        )
    return pieces or [cue]


def _word_atoms(words: list[SpeechWord]) -> list[list[SpeechWord]]:
    atoms: list[list[SpeechWord]] = [[words[0]]]
    for word in words[1:]:
        previous = atoms[-1][-1]
        gap = word.start - previous.end
        if _continues_word(previous, word, gap):
            atoms[-1].append(word)
        else:
            atoms.append([word])
    return atoms


def _continues_word(previous: SpeechWord, word: SpeechWord, gap: float) -> bool:
    if gap >= _PHRASE_JOIN or word.raw[:1].isspace() or not previous.text:
        return False
    return _same_run(previous.text[-1], word.text[0])


def _same_run(left: str, right: str) -> bool:
    return (_is_ascii_alnum(left) and _is_ascii_alnum(right)) or (_is_katakana(left) and _is_katakana(right))


def _is_ascii_alnum(char: str) -> bool:
    return char.isascii() and char.isalnum()


def _is_katakana(char: str) -> bool:
    code = ord(char)
    return 0x30A0 <= code <= 0x30FF or 0xFF66 <= code <= 0xFF9D


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
