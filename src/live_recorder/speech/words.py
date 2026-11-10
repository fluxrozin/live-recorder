"""配信者ごとの字幕ヒント。faster-whisper の hotwords に渡す短い語列。"""

from __future__ import annotations

SPEECH_WORDS_LIMIT = 200


class SpeechWordsError(ValueError):
    pass


def normalize_speech_words(raw: str) -> str:
    """空白と改行で区切り、重複を除いた1行に整える。空なら空文字。長すぎるときは拒否する。"""
    words: list[str] = []
    seen: set[str] = set()
    for part in raw.split():
        if part in seen:
            continue
        seen.add(part)
        words.append(part)
    text = " ".join(words)
    if len(text) > SPEECH_WORDS_LIMIT:
        raise SpeechWordsError(f"字幕の単語は{SPEECH_WORDS_LIMIT}文字までです")
    return text


def speech_words_for_form(stored: str | None) -> str:
    """保存済みの語列を、1語1行で編集欄に戻す。"""
    return (stored or "").replace(" ", "\n")
