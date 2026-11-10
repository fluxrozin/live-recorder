from datetime import datetime, timezone

from live_recorder.comments.dedupe import dedupe_key
from live_recorder.comments.export import (
    csv_safe,
    escape_log_text,
    export_row,
    render_csv,
    render_txt,
)
from live_recorder.comments.offsets import offsets
from live_recorder.comments.segments import (
    ExportSegment,
    assign_segment,
    comment_export_name,
    partition_by_segment,
    segment_offset_ms,
)
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


def test_export_times_follow_display_timezone():
    posted = datetime(2026, 11, 9, 1, 2, 3, tzinfo=timezone.utc)
    received = datetime(2026, 11, 9, 1, 2, 5, tzinfo=timezone.utc)
    row = export_row(
        posted_at=posted,
        received_at=received,
        timezone_name="Asia/Tokyo",
        user_id="u",
        nickname="n",
        message="m",
        platform="tiktok",
        stream_id="s",
        recording_offset_ms=1000,
        stream_offset_ms=2000,
        platform_comment_id="c",
    )
    assert row["posted_at"] == "2026-11-09T10:02:03+09:00"
    assert row["received_at"] == "2026-11-09T10:02:05+09:00"
    assert row["time"] == "2026-11-09T10:02:03+09:00"
    assert "+00:00" not in str(row["posted_at"]) + str(row["received_at"]) + str(row["time"])
    missing = export_row(
        posted_at=None,
        received_at=datetime(2026, 11, 9, 15, 0),
        timezone_name="Asia/Tokyo",
        user_id="u",
        nickname="n",
        message="m",
        platform="tiktok",
        stream_id="s",
        recording_offset_ms=None,
        stream_offset_ms=None,
        platform_comment_id=None,
    )
    assert missing["posted_at"] is None
    assert missing["time"] == missing["received_at"] == "2026-11-10T00:00:00+09:00"


def test_comment_export_uses_recording_stem():
    video = "tiktok_krk__rmn_nostream_20261109_200621_seg01.mp4"
    assert comment_export_name(video, "jsonl") == "tiktok_krk__rmn_nostream_20261109_200621_seg01.jsonl"
    assert comment_export_name(video, "csv") == "tiktok_krk__rmn_nostream_20261109_200621_seg01.csv"
    assert comment_export_name("../comments.mp4", "txt") == "comments.txt"


def test_comments_split_across_segments_and_offsets():
    start = datetime(2026, 11, 9, 11, 6, 21, tzinfo=timezone.utc)
    later = datetime(2026, 11, 9, 11, 20, 0, tzinfo=timezone.utc)
    first = ExportSegment(1, 1, "seg01.mp4", start)
    second = ExportSegment(2, 2, "seg02.mp4", later)
    early = datetime(2026, 11, 9, 11, 6, 0, tzinfo=timezone.utc)
    during_first = datetime(2026, 11, 9, 11, 10, 0, tzinfo=timezone.utc)
    during_second = datetime(2026, 11, 9, 11, 21, 0, tzinfo=timezone.utc)
    assert assign_segment(early, [second, first]).id == 1
    assert assign_segment(during_second, [first, second]).id == 2
    grouped = partition_by_segment(
        [("a", early), ("b", during_first), ("c", during_second)],
        [second, first],
        lambda item: item[1],
    )
    assert [len(items) for _segment, items in grouped] == [2, 1]
    assert segment_offset_ms(during_first, start) == 219_000
