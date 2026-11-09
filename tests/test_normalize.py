from datetime import datetime, timezone

import pytest

from live_recorder.platforms.normalize import (
    NormalizeError,
    normalize_tiktok,
    normalize_twitcasting,
    tiktok_live_url,
)


def test_tiktok_accepts_id_at_and_profile_url():
    assert normalize_tiktok("user.name") == "user.name"
    assert normalize_tiktok("@user.name") == "user.name"
    assert normalize_tiktok("https://www.tiktok.com/@user.name") == "user.name"
    assert normalize_tiktok("https://www.tiktok.com/@user.name/live/") == "user.name"
    assert tiktok_live_url("user.name") == "https://www.tiktok.com/@user.name/live"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "../etc",
        "https://evil.example/@user",
        "https://www.tiktok.com/@user/video/1",
        "user name",
        ".",
        "..",
        "https://user:pass@www.tiktok.com/@user",
        "a",
    ],
)
def test_tiktok_rejects_bad_input(value: str):
    with pytest.raises(NormalizeError):
        normalize_tiktok(value)


def test_twitcasting_profile_and_movie_url():
    assert normalize_twitcasting("screen_id") == "screen_id"
    assert normalize_twitcasting("https://twitcasting.tv/screen_id/movie/10") == "screen_id"


@pytest.mark.parametrize("value", ["", "bad/name", "https://example.com/screen", "..", "has space"])
def test_twitcasting_rejects_bad_input(value: str):
    with pytest.raises(NormalizeError):
        normalize_twitcasting(value)


def test_comment_conversion_uses_only_present_fields():
    from live_recorder.platforms.tiktok import comment_from_event

    class User:
        unique_id = "viewer"
        nickname = "表示"
        id = 5

    class Common:
        msg_id = 99
        create_time = 1_700_000_000

    class Event:
        content = "こんにちは"
        user = User()
        common = Common()

    received = datetime(2026, 11, 9, tzinfo=timezone.utc)
    draft = comment_from_event(Event(), received)
    assert draft is not None
    assert draft.message == "こんにちは"
    assert draft.platform_comment_id == "99"
    assert draft.platform_user_id == "viewer"
    assert draft.posted_at is not None

    class Empty:
        content = "本文だけ"
        user = None
        common = None

    bare = comment_from_event(Empty(), received)
    assert bare is not None
    assert bare.platform_comment_id is None
    assert bare.posted_at is None
    assert bare.platform_user_id is None
