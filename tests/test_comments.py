from datetime import datetime, timezone

from live_recorder.comments.dedupe import dedupe_key
from live_recorder.comments.export import csv_safe, escape_log_text, render_csv, render_txt
from live_recorder.comments.offsets import offsets
from live_recorder.timeutil import parse_platform_time


def test_dedupe_keeps_repeated_text_at_another_time():
    posted = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)
    later = datetime(2026, 11, 9, 1, 1, tzinfo=timezone.utc)
    received = datetime(2026, 11, 9, 1, 0, 2, tzinfo=timezone.utc)
    first = dedupe_key(
        platform_comment_id=None,
        platform_user_id="a",
        message="同じ",
        posted_at=posted,
        received_at=received,
    )
    second = dedupe_key(
        platform_comment_id=None,
        platform_user_id="a",
        message="同じ",
        posted_at=later,
        received_at=received,
    )
    assert first != second


def test_dedupe_uses_platform_id_when_present():
    received = datetime(2026, 11, 9, tzinfo=timezone.utc)
    key = dedupe_key(
        platform_comment_id="abc",
        platform_user_id="a",
        message="本文",
        posted_at=None,
        received_at=received,
    )
    again = dedupe_key(
        platform_comment_id="abc",
        platform_user_id="other",
        message="別の本文",
        posted_at=received,
        received_at=received,
    )
    assert key == again


def test_offsets_allow_negative_and_null():
    start = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)
    before = datetime(2026, 11, 9, 0, 59, tzinfo=timezone.utc)
    received = datetime(2026, 11, 9, 1, 0, 5, tzinfo=timezone.utc)
    stream_offset, recording_offset = offsets(
        posted_at=before,
        received_at=received,
        stream_started_at=start,
        recording_started_at=start,
    )
    assert stream_offset == -60_000
    assert recording_offset == -60_000
    stream_offset, recording_offset = offsets(
        posted_at=None,
        received_at=received,
        stream_started_at=start,
        recording_started_at=None,
    )
    assert stream_offset == 5_000
    assert recording_offset is None


def test_missing_platform_time_is_not_invented():
    assert parse_platform_time(None) is None
    assert parse_platform_time(0) is None
    assert parse_platform_time("nope") is None


def test_csv_formula_and_newline_escape():
    assert csv_safe("=cmd") == "'=cmd"
    assert csv_safe("+1") == "'+1"
    assert csv_safe("-1") == "'-1"
    assert csv_safe("@a") == "'@a"
    assert csv_safe("普通") == "普通"
    text = render_csv(
        [
            {
                "time": "2026-11-09 21:00:00",
                "user_id": "=evil",
                "nickname": "名",
                "message": "こんにちは",
                "platform": "tiktok",
                "stream_id": "1",
                "recording_offset": "00:00:01",
            }
        ]
    )
    assert "'=evil" in text
    assert "時刻" in text
    log = render_txt(
        [{"user_id": "user_a", "message": "一行目\n二行目", "recording_offset_ms": 12_000}]
    )
    assert log == "[00:00:12] user_a: 一行目\\n二行目\n"
    assert "\n二" not in log.split("user_a: ", 1)[1].replace("\\n", "")
    assert escape_log_text("a\nb") == "a\\nb"
