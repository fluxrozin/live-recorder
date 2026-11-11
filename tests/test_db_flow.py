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


async def test_monitoring_off_ends_session_and_recording_off_stops_only_recording(tmp_path, monkeypatch):
    await _reset_schema()
    (tmp_path / "rec").mkdir()
    (tmp_path / "cfg").mkdir()
    db = Database(_settings(tmp_path))
    monkeypatch.setattr(Supervisor, "_spawn", lambda *args, **kwargs: None)
    monkeypatch.setattr("live_recorder.monitor.supervisor.disk_free", lambda _path: 10**12)
    monkeypatch.setattr("live_recorder.monitor.supervisor.allow_new_recording", lambda _free, _minimum: True)
    supervisor = Supervisor(_settings(tmp_path), db, TwitCastingClient(client_id="", client_secret="", access_token=""))
    observation = LiveObservation(status=LiveStatus.LIVE, platform_stream_id="room-1", title="公開")
    recording_off = await _streamer(db, "recording_off", recording=True, comments=True)
    await supervisor.apply(recording_off, observation, _plan())
    await supervisor.apply(
        recording_off,
        observation,
        _plan(
            recording_enabled=False,
            has_open_session=True,
            open_stream_id="room-1",
            recording_open=True,
            comments_open=True,
            comments_active=True,
        ),
    )
    monitoring_off = await _streamer(db, "monitoring_off", recording=True, comments=True)
    await supervisor.apply(monitoring_off, observation, _plan())
    await supervisor.apply(
        monitoring_off,
        observation,
        _plan(monitoring_enabled=False, has_open_session=True, open_stream_id="room-1"),
    )
    async with db.session() as session:
        repo = Repository(session)
        kept = await repo.open_session(recording_off)
        assert kept is not None and kept.status == "LIVE"
        stopped = await repo.active_recording(kept.id)
        assert stopped is None
        assert await repo.active_comments(kept.id) is not None
        ended = await repo.open_session(monitoring_off)
        assert ended is None
    await db.dispose()


async def test_ended_session_is_not_returned_to_reconnecting(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from live_recorder.db.models import RecordingJob, RecordingSegment

    await _reset_schema()
    (tmp_path / "rec").mkdir()
    (tmp_path / "cfg").mkdir()
    db = Database(_settings(tmp_path))
    monkeypatch.setattr(Supervisor, "_spawn", lambda *args, **kwargs: None)
    monkeypatch.setattr("live_recorder.monitor.supervisor.disk_free", lambda _path: 10**12)
    monkeypatch.setattr("live_recorder.monitor.supervisor.allow_new_recording", lambda _free, _minimum: True)
    supervisor = Supervisor(_settings(tmp_path), db, TwitCastingClient(client_id="", client_secret="", access_token=""))
    observation = LiveObservation(status=LiveStatus.LIVE, platform_stream_id="room-1", title="公開")
    streamer_id = await _streamer(db, "ended_user", recording=True, comments=False)
    await supervisor.apply(streamer_id, observation, _plan(comments_enabled=False))
    started = datetime(2020, 1, 1, tzinfo=timezone.utc)
    async with db.session() as session:
        repo = Repository(session)
        stream = await repo.open_session(streamer_id)
        assert stream is not None
        stream.status = "ENDED"
        job = await repo.active_recording(stream.id)
        assert job is not None
        job.status = "RECORDING"
        job.started_at = started - timedelta(hours=5)
        session.add(
            RecordingSegment(
                recording_job_id=job.id,
                segment_number=1,
                started_at=started,
                finished_at=started + timedelta(hours=1),
                file_path="clip.mp4",
                file_size=100,
                duration_ms=3_600_000,
                validation_status="ok",
            )
        )
        await session.commit()
        job_id = job.id
    await supervisor.recover()
    async with db.session() as session:
        job = await session.get(RecordingJob, job_id)
        assert job is not None
        assert job.status == "PARTIAL"
    await db.dispose()


async def test_restart_gap_uses_segment_duration(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from live_recorder.db.models import RecordingSegment

    await _reset_schema()
    (tmp_path / "rec").mkdir()
    (tmp_path / "cfg").mkdir()
    db = Database(_settings(tmp_path))
    monkeypatch.setattr(Supervisor, "_spawn", lambda *args, **kwargs: None)
    monkeypatch.setattr("live_recorder.monitor.supervisor.disk_free", lambda _path: 10**12)
    monkeypatch.setattr("live_recorder.monitor.supervisor.allow_new_recording", lambda _free, _minimum: True)
    supervisor = Supervisor(_settings(tmp_path), db, TwitCastingClient(client_id="", client_secret="", access_token=""))
    observation = LiveObservation(status=LiveStatus.LIVE, platform_stream_id="room-1", title="公開")
    streamer_id = await _streamer(db, "gap_user", recording=True, comments=False)
    await supervisor.apply(streamer_id, observation, _plan(comments_enabled=False))
    started = datetime(2020, 1, 1, tzinfo=timezone.utc)
    async with db.session() as session:
        repo = Repository(session)
        stream = await repo.open_session(streamer_id)
        assert stream is not None
        job = await repo.active_recording(stream.id)
        assert job is not None
        job.status = "RECORDING"
        job.started_at = started - timedelta(hours=5)
        session.add(
            RecordingSegment(
                recording_job_id=job.id,
                segment_number=1,
                started_at=started,
                finished_at=started + timedelta(hours=1),
                file_path="clip.mp4",
                file_size=100,
                duration_ms=3_600_000,
                validation_status="ok",
            )
        )
        await session.commit()
        session_id = stream.id
        job_started = job.started_at
    await supervisor.recover()
    async with db.session() as session:
        gaps = [gap for gap in await Repository(session).gaps_for_session(session_id) if gap.job_type == "recording"]
        assert len(gaps) == 1
        assert gaps[0].started_at == started + timedelta(hours=1)
        assert gaps[0].started_at != job_started
        assert gaps[0].ended_at is None
    await db.dispose()


async def test_comment_insert_failure_still_updates_the_job(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from sqlalchemy import text

    from live_recorder.monitor.jobs import _flush_comments
    from live_recorder.platforms.types import CommentDraft

    await _reset_schema()
    (tmp_path / "rec").mkdir()
    (tmp_path / "cfg").mkdir()
    db = Database(_settings(tmp_path))
    monkeypatch.setattr(Supervisor, "_spawn", lambda *args, **kwargs: None)
    monkeypatch.setattr("live_recorder.monitor.supervisor.disk_free", lambda _path: 10**12)
    monkeypatch.setattr("live_recorder.monitor.supervisor.allow_new_recording", lambda _free, _minimum: True)
    supervisor = Supervisor(_settings(tmp_path), db, TwitCastingClient(client_id="", client_secret="", access_token=""))
    observation = LiveObservation(status=LiveStatus.LIVE, platform_stream_id="room-1", title="公開")
    streamer_id = await _streamer(db, "comment_user", recording=False, comments=True)
    await supervisor.apply(streamer_id, observation, _plan(recording_enabled=False))

    async def fail_insert(self, rows):
        await self.session.execute(text("SELECT 1/0"))
        return 0

    monkeypatch.setattr(Repository, "insert_comments", fail_insert)
    async with db.session() as session:
        stream = await Repository(session).open_session(streamer_id)
        assert stream is not None
        job = await Repository(session).active_comments(stream.id)
        assert job is not None
        session_id = stream.id
        job_id = job.id
    await _flush_comments(
        db,
        session_id,
        job_id,
        [
            CommentDraft(
                platform_comment_id="1",
                platform_user_id="viewer",
                nickname="視聴者",
                message="こんにちは",
                posted_at=None,
                received_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
        ],
        0,
    )
    async with db.session() as session:
        job = await Repository(session).get_comment_job(job_id)
        assert job is not None
        assert job.received_count == 1
        assert job.stored_count == 0
        assert job.error_count == 1
    await db.dispose()


async def test_comment_retention_keeps_speech_and_video(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from live_recorder.db.models import RecordingSegment

    await _reset_schema()
    (tmp_path / "rec").mkdir()
    (tmp_path / "cfg").mkdir()
    db = Database(_settings(tmp_path))
    monkeypatch.setattr(Supervisor, "_spawn", lambda *args, **kwargs: None)
    monkeypatch.setattr("live_recorder.monitor.supervisor.disk_free", lambda _path: 10**12)
    monkeypatch.setattr("live_recorder.monitor.supervisor.allow_new_recording", lambda _free, _minimum: True)
    settings = _settings(tmp_path)
    supervisor = Supervisor(settings, db, TwitCastingClient(client_id="", client_secret="", access_token=""))
    observation = LiveObservation(status=LiveStatus.LIVE, platform_stream_id="room-1", title="公開")
    streamer_id = await _streamer(db, "retain_user", recording=True, comments=True)
    await supervisor.apply(streamer_id, observation, _plan())
    video = tmp_path / "rec" / "clip.mp4"
    speech = tmp_path / "rec" / "clip_Speech.srt"
    comments = tmp_path / "rec" / "clip_Comments.srt"
    video.write_bytes(b"video")
    speech.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
    comments.write_text("1\n00:00:00,000 --> 00:00:01,000\nold\n", encoding="utf-8")
    async with db.session() as session:
        repo = Repository(session)
        stream = await repo.open_session(streamer_id)
        assert stream is not None
        job = await repo.active_recording(stream.id)
        assert job is not None
        session.add(
            RecordingSegment(
                recording_job_id=job.id,
                segment_number=1,
                started_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                finished_at=datetime(2020, 1, 1, 1, tzinfo=timezone.utc),
                file_path="clip.mp4",
                file_size=5,
                duration_ms=3_600_000,
                validation_status="ok",
            )
        )
        await session.commit()
        await supervisor._refresh_comment_retention(repo, stream.id, "Asia/Tokyo")
        await session.commit()
    assert video.read_bytes() == b"video"
    assert speech.is_file()
    assert not comments.is_file()
    await db.dispose()
