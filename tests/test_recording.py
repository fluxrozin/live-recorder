from pathlib import Path

import pytest

from live_recorder.recording.commands import pick_tiktok_pull_url, streamlink_tiktok_args
from live_recorder.recording.files import PathEscape, build_segment_path, resolve_stored, safe_token
from live_recorder.recording.integrity import scan_storage
from live_recorder.recording.policy import ACCESS_DENIED, INTERRUPTED, next_action


def test_hls_url_stays_on_twitcasting():
    from live_recorder.platforms.twitcasting import validate_hls_url

    assert validate_hls_url("https://twitcasting.tv/user/metastream.m3u8/?video=1")
    assert validate_hls_url("https://edge.twitcasting.tv/live.m3u8")
    assert validate_hls_url("http://twitcasting.tv/user/metastream.m3u8") is None
    assert validate_hls_url("https://evil.example/live.m3u8") is None


def test_filename_strips_traversal(tmp_path: Path):
    path = build_segment_path(
        root=tmp_path,
        platform="tiktok",
        user_id="../etc",
        when_stamp="20261109_210000",
        day="2026-11-09",
        stream_id="room/1",
        segment_number=2,
    )
    assert path.is_relative_to(tmp_path)
    assert ".." not in path.parts
    assert path.name.startswith("tiktok_")
    assert "_seg02.ts" in path.name
    assert safe_token("../../x") == "x"


def test_resolve_rejects_escape(tmp_path: Path):
    with pytest.raises(PathEscape):
        resolve_stored(tmp_path, "../secret.mp4")


def test_streamlink_command_uses_normalized_url():
    args = streamlink_tiktok_args("streamlink", "@user.name", "/tmp/out.ts", "best")
    assert args[0] == "streamlink"
    assert "https://www.tiktok.com/@user.name/live" in args
    assert "--no-config" in args


def test_access_denied_does_not_switch_engine():
    assert next_action(kind=ACCESS_DENIED, bytes_written=0, attempt=0, max_attempts=5, engine_index=0, engine_count=3) == "stop"


def test_extract_failure_switches_then_reconnects_after_data():
    assert next_action(kind=INTERRUPTED, bytes_written=0, attempt=0, max_attempts=5, engine_index=0, engine_count=3) == "switch"
    assert next_action(kind=INTERRUPTED, bytes_written=100_000, attempt=1, max_attempts=5, engine_index=1, engine_count=3) == "same"
    assert next_action(kind=INTERRUPTED, bytes_written=100_000, attempt=5, max_attempts=5, engine_index=1, engine_count=3) == "stop"


def test_pick_only_https_rendition_that_exists():
    room = {
        "stream_url": {
            "live_core_sdk_data": {
                "pull_data": {
                    "stream_data": {
                        "data": {
                            "hd": {"main": {"flv": "https://cdn.example/live.flv"}},
                        }
                    }
                }
            }
        }
    }
    assert pick_tiktok_pull_url(room, "best") == "https://cdn.example/live.flv"
    room["stream_url"]["live_core_sdk_data"]["pull_data"]["stream_data"]["data"]["ld"] = {
        "main": {"flv": "http://cdn.example/insecure.flv"}
    }
    with pytest.raises(KeyError):
        pick_tiktok_pull_url({"stream_url": {"live_core_sdk_data": {"pull_data": {"stream_data": {"data": {}}}}}}, "medium")


def test_scan_finds_missing_unregistered_and_temp(tmp_path: Path):
    kept = tmp_path / "tiktok" / "a"
    kept.mkdir(parents=True)
    registered = kept / "clip.mp4"
    registered.write_bytes(b"1234")
    (kept / "extra.mp4").write_bytes(b"abcd")
    (kept / "partial.part").write_bytes(b"x")
    report = scan_storage(
        tmp_path,
        [
            {"file_path": "tiktok/a/clip.mp4", "file_size": 4, "validation": "ok", "finished": True, "file_removed": False},
            {"file_path": "tiktok/a/missing.mp4", "file_size": 4, "validation": "ok", "finished": True, "file_removed": False},
            {"file_path": "tiktok/a/clip.mp4", "file_size": 9, "validation": "invalid", "finished": True, "file_removed": False},
        ],
    )
    assert "tiktok/a/missing.mp4" in report.missing
    assert any(item.endswith("extra.mp4") for item in report.unregistered)
    assert any(item.endswith(".part") for item in report.temporary)
    assert any(item.endswith("clip.mp4") for item in report.suspect)
