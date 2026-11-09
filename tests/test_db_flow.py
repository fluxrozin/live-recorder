import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from live_recorder.config import Settings
from live_recorder.db.base import Base
from live_recorder.db.models import Streamer
from live_recorder.db.repository import Repository
from live_recorder.db.session import Database
from live_recorder.domain.enums import LiveStatus
from live_recorder.monitor.planner import plan_observation
from live_recorder.monitor.supervisor import Supervisor
from live_recorder.platforms.twitcasting import TwitCastingClient
from live_recorder.platforms.types import LiveObservation

pytestmark = pytest.mark.skipif(
    not os.environ.get("LIVE_RECORDER_TEST_DATABASE_URL"),
    reason="LIVE_RECORDER_TEST_DATABASE_URL が無いためデータベース試験を省略",
)


def _settings(tmp_path) -> Settings:
    return Settings(
        database_url=os.environ["LIVE_RECORDER_TEST_DATABASE_URL"],
        session_secret="x" * 40,
        recordings_dir=tmp_path / "rec",
        config_dir=tmp_path / "cfg",
        web_host="127.0.0.1",
        web_port=8080,
        display_timezone="Asia/Tokyo",
        log_level="INFO",
        cookie_secure=False,
        twitcasting_client_id="",
        twitcasting_client_secret="",
        twitcasting_access_token="",
        sign_api_key="",
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        streamlink_path="streamlink",
        ytdlp_path="yt-dlp",
        shutdown_timeout_seconds=5,
        app_home=tmp_path,
    )


async def _reset_schema() -> None:
    engine = create_async_engine(os.environ["LIVE_RECORDER_TEST_DATABASE_URL"])
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()


async def _streamer(db: Database, user_id: str, *, recording: bool, comments: bool) -> int:
    async with db.session() as session:
        row = Streamer(
            platform="tiktok",
            platform_user_id=user_id,
            display_name="公開配信者",
            profile_url=f"https://www.tiktok.com/@{user_id}",
            recording_enabled=recording,
            comments_enabled=comments,
        )
        await Repository(session).add_streamer(row)
        await session.commit()
        return row.id


def _plan(**overrides):
    values = {
        "monitoring_enabled": True,
        "recording_enabled": True,
        "comments_enabled": True,
        "comments_global": True,
        "observation": LiveObservation(status=LiveStatus.LIVE, platform_stream_id="room-1", title="公開"),
        "has_open_session": False,
        "open_stream_id": None,
        "recording_active": False,
        "comments_active": False,
        "recording_slots_left": 3,
        "disk_ok": True,
    }
    values.update(overrides)
    return plan_observation(**values)


async def test_mock_live_creates_independent_jobs(tmp_path, monkeypatch):
    await _reset_schema()
    (tmp_path / "rec").mkdir()
    (tmp_path / "cfg").mkdir()
    db = Database(_settings(tmp_path))
    spawned: list[str] = []

    def remember(self, kind, job_id, runner):
        spawned.append(kind)

    monkeypatch.setattr(Supervisor, "_spawn", remember)
    monkeypatch.setattr("live_recorder.monitor.supervisor.disk_free", lambda _path: 10**12)
    monkeypatch.setattr("live_recorder.monitor.supervisor.allow_new_recording", lambda _free, _minimum: True)
    supervisor = Supervisor(_settings(tmp_path), db, TwitCastingClient(client_id="", client_secret="", access_token=""))
    both = await _streamer(db, "public_user", recording=True, comments=True)
    observation = LiveObservation(status=LiveStatus.LIVE, platform_stream_id="room-1", title="公開")
    await supervisor.apply(both, observation, _plan())
    comments_only_id = await _streamer(db, "comments_user", recording=False, comments=True)
    await supervisor.apply(
        comments_only_id,
        observation,
        _plan(recording_enabled=False),
    )
    restricted_id = await _streamer(db, "restricted_user", recording=True, comments=True)
    restricted = LiveObservation(status=LiveStatus.LIVE, platform_stream_id="room-2", restricted=True)
    await supervisor.apply(restricted_id, restricted, _plan(observation=restricted))

    async with db.session() as session:
        repo = Repository(session)
        both_session = await repo.open_session(both)
        assert both_session is not None
        assert await repo.active_recording(both_session.id) is not None
        assert await repo.active_comments(both_session.id) is not None
        comments_session = await repo.open_session(comments_only_id)
        assert comments_session is not None
        assert await repo.active_recording(comments_session.id) is None
        assert await repo.active_comments(comments_session.id) is not None
        restricted_session = await repo.open_session(restricted_id)
        assert restricted_session is not None
        assert restricted_session.restricted is True
        assert await repo.active_recording(restricted_session.id) is None
        assert await repo.active_comments(restricted_session.id) is None
    assert spawned == ["rec", "com", "com"]
    await db.dispose()
