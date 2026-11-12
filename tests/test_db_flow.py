import asyncio
import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from live_recorder.config import Settings
from live_recorder.db.base import Base
from live_recorder.db.models import RecordingSegment, Streamer
from live_recorder.db.repository import Repository
from live_recorder.db.session import Database
from live_recorder.domain.enums import LiveStatus, RecordingStatus
from live_recorder.monitor.jobs import close_recording_if_idle
from live_recorder.monitor.planner import plan_observation
from live_recorder.monitor.supervisor import Supervisor
from live_recorder.platforms.twitcasting import TwitCastingClient
from live_recorder.platforms.types import CommentDraft, LiveObservation

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


_UNTITLED = "tiktok_@lock_user_20261112_080415_無題_seg01.mp4"


async def _recording(tmp_path, monkeypatch) -> tuple[Database, int, int, int]:
    await _reset_schema()
    (tmp_path / "rec").mkdir()
    (tmp_path / "cfg").mkdir()
    settings = _settings(tmp_path)
    db = Database(settings)
    monkeypatch.setattr(Supervisor, "_spawn", lambda *args, **kwargs: None)
    monkeypatch.setattr("live_recorder.monitor.supervisor.disk_free", lambda _path: 10**12)
    monkeypatch.setattr("live_recorder.monitor.supervisor.allow_new_recording", lambda _free, _minimum: True)
    supervisor = Supervisor(settings, db, TwitCastingClient(client_id="", client_secret="", access_token=""))
    observation = LiveObservation(status=LiveStatus.LIVE, platform_stream_id="room-1", title="公開")
    streamer_id = await _streamer(db, "lock_user", recording=True, comments=True)
    await supervisor.apply(streamer_id, observation, _plan())
    (tmp_path / "rec" / _UNTITLED).write_bytes(b"video")
    async with db.session() as session:
        repo = Repository(session)
        stream = await repo.open_session(streamer_id)
        job = await repo.active_recording(stream.id)
        segment = RecordingSegment(
            recording_job_id=job.id,
            segment_number=1,
            started_at=datetime(2026, 11, 12, tzinfo=timezone.utc),
            finished_at=datetime(2026, 11, 12, 1, tzinfo=timezone.utc),
            file_path=_UNTITLED,
            file_size=5,
            duration_ms=1000,
            validation_status="ok",
            speech_status="pending",
        )
        session.add(segment)
        await session.commit()
        return db, stream.id, job.id, segment.id


async def test_session_lock_lets_comments_in_but_hides_segments_from_speech(tmp_path, monkeypatch):
    db, session_id, job_id, segment_id = await _recording(tmp_path, monkeypatch)
    async with db.session() as holder:
        repo = Repository(holder)
        assert await repo.lock_session(session_id) is not None
        assert [item.id for item in await repo.lock_segments_for_job(job_id)] == [segment_id]
        async with db.session() as writer:
            other = Repository(writer)
            draft = CommentDraft("c1", "u", "n", "hello", None, datetime.now(timezone.utc))
            rows = await other.comment_rows(
                session_id=session_id, recording_started=None, stream_started=None, drafts=[draft]
            )
            assert await asyncio.wait_for(other.insert_comments(rows), 5) == 1
            await asyncio.wait_for(writer.commit(), 5)
        async with db.session() as speech:
            assert await Repository(speech).claim_next_speech() is None
        await holder.commit()
    async with db.session() as speech:
        claimed = await Repository(speech).claim_next_speech()
        assert claimed is not None and claimed.id == segment_id
    await db.dispose()


async def test_closing_retitles_a_pending_segment_and_drops_the_old_name(tmp_path, monkeypatch):
    db, _session_id, job_id, segment_id = await _recording(tmp_path, monkeypatch)
    settings = _settings(tmp_path)
    await close_recording_if_idle(settings, db, job_id, RecordingStatus.COMPLETED)
    renamed = "tiktok_@lock_user_20261112_080415_公開_seg01.mp4"
    assert (tmp_path / "rec" / renamed).read_bytes() == b"video"
    assert not (tmp_path / "rec" / _UNTITLED).exists()
    async with db.session() as session:
        segment = await Repository(session).get_segment(segment_id)
        assert segment.file_path == renamed
        assert segment.speech_status == "pending"
    await db.dispose()


async def test_recording_finish_and_monitor_apply_do_not_deadlock(tmp_path, monkeypatch):
    from live_recorder.monitor.jobs import _stamp_last_recording

    db, session_id, _job_id, _segment_id = await _recording(tmp_path, monkeypatch)
    mid = asyncio.Event()
    gate = asyncio.Event()
    results: dict[str, object] = {}

    async def finish_recording():
        try:
            mid.set()
            await gate.wait()
            # 監視が配信行を持ったままでも、streamer だけの更新は通る。
            await asyncio.wait_for(_stamp_last_recording(db, session_id), 5)
            results["finish"] = "ok"
        except Exception as exc:
            results["finish"] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"

    async def apply_tick():
        try:
            await mid.wait()
            async with db.session() as session:
                repo = Repository(session)
                streamer_id = (await repo.get_session(session_id)).streamer_id
                open_session = await repo.open_session(streamer_id)
                if open_session is not None:
                    open_session = await repo.lock_session(open_session.id)
                gate.set()
                await asyncio.sleep(0.1)
                streamer = await repo.get_streamer(streamer_id)
                streamer.last_checked_at = datetime.now(timezone.utc)
                if open_session is not None:
                    open_session.last_seen_live_at = datetime.now(timezone.utc)
                await asyncio.wait_for(session.commit(), 5)
                results["apply"] = "ok"
        except Exception as exc:
            results["apply"] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"

    await asyncio.wait_for(asyncio.gather(finish_recording(), apply_tick()), 10)
    assert results["finish"] == "ok"
    assert results["apply"] == "ok"
    assert "deadlock" not in str(results).lower()
    async with db.session() as session:
        stream = await Repository(session).get_session(session_id)
        assert stream.streamer.last_recording_at is not None
    await db.dispose()


async def test_close_recording_and_history_delete_do_not_deadlock(tmp_path, monkeypatch):
    from live_recorder.recording.finalize import FinalFile

    db, session_id, job_id, segment_id = await _recording(tmp_path, monkeypatch)
    settings = _settings(tmp_path)
    async with db.session() as session:
        segment = await Repository(session).get_segment(segment_id)
        segment.finished_at = None
        segment.validation_status = "pending"
        await session.commit()

    async def slow_finalize(_ffmpeg, _ffprobe, path):
        await asyncio.sleep(0.15)
        return FinalFile(path, "mp4", 1000, path.stat().st_size, "ok", True)

    monkeypatch.setattr("live_recorder.monitor.jobs.finalize_recording", slow_finalize)
    results: dict[str, object] = {}

    async def close_job():
        try:
            await close_recording_if_idle(
                settings, db, job_id, RecordingStatus.PARTIAL, finish=False
            )
            results["close"] = "ok"
        except Exception as exc:
            results["close"] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"

    async def delete_history():
        try:
            await asyncio.sleep(0.05)
            async with db.session() as session:
                repo = Repository(session)
                assert await asyncio.wait_for(repo.lock_session(session_id), 5) is not None
                await asyncio.wait_for(repo.delete_session(session_id), 5)
                await asyncio.wait_for(session.commit(), 5)
                results["delete"] = "ok"
        except Exception as exc:
            results["delete"] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"

    await asyncio.wait_for(asyncio.gather(close_job(), delete_history()), 10)
    assert results["close"] == "ok"
    assert results["delete"] == "ok"
    assert "deadlock" not in str(results).lower()
    # 履歴は消えても、コミット前にファイルを消していないので実体は残る。
    assert any((tmp_path / "rec").glob("*.mp4"))
    async with db.session() as session:
        assert await Repository(session).get_session(session_id) is None
    await db.dispose()


async def test_comment_retention_and_history_delete_do_not_deadlock(tmp_path, monkeypatch):
    from sqlalchemy import text

    from live_recorder.db.models import Comment, CommentCollectionJob

    db, session_id, _job_id, _segment_id = await _recording(tmp_path, monkeypatch)
    video = tmp_path / "rec" / _UNTITLED
    async with db.session() as session:
        session.add(
            CommentCollectionJob(
                stream_session_id=session_id, status="COMPLETED", received_count=3, stored_count=3
            )
        )
        for index in range(3):
            session.add(
                Comment(
                    stream_session_id=session_id,
                    platform_user_id="v",
                    nickname="n",
                    message=f"m{index}",
                    received_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                    dedupe_key=f"old-{index}",
                )
            )
        await session.commit()
        await session.execute(text("UPDATE comments SET created_at = NOW() - INTERVAL '10 days'"))
        await session.commit()

    results: dict[str, object] = {}

    async def retain():
        try:
            async with db.session() as session:
                repo = Repository(session)
                cutoff = datetime.now(timezone.utc)
                for sid in await repo.sessions_with_comments_before(cutoff):
                    await repo.lock_session(sid)
                removed, _ids = await repo.delete_comments_before(cutoff)
                results["removed"] = removed
                await asyncio.sleep(0.05)
                await asyncio.wait_for(session.commit(), 5)
                results["retain"] = "ok"
        except Exception as exc:
            results["retain"] = type(exc).__name__

    async def delete_history():
        try:
            await asyncio.sleep(0.02)
            async with db.session() as session:
                repo = Repository(session)
                assert await asyncio.wait_for(repo.lock_session(session_id), 5) is not None
                await asyncio.wait_for(repo.delete_session(session_id), 5)
                await asyncio.wait_for(session.commit(), 5)
                results["delete"] = "ok"
        except Exception as exc:
            results["delete"] = type(exc).__name__

    await asyncio.wait_for(asyncio.gather(retain(), delete_history()), 10)
    assert results["retain"] == "ok"
    assert results["delete"] == "ok"
    assert video.is_file()
    async with db.session() as session:
        assert await Repository(session).get_session(session_id) is None
    await db.dispose()


async def test_pending_file_deletion_retries_after_unlink_failure(tmp_path, monkeypatch):
    from live_recorder.recording.cleanup import flush_pending_deletions, queue_segment_deletion

    db, _session_id, _job_id, segment_id = await _recording(tmp_path, monkeypatch)
    settings = _settings(tmp_path)
    video = tmp_path / "rec" / _UNTITLED
    parent = video.parent
    async with db.session() as session:
        repo = Repository(session)
        segment = await repo.get_segment(segment_id)
        repo.mark_segment_removed(segment)
        await queue_segment_deletion(repo, settings.recordings_dir, segment)
        await session.commit()
    parent.chmod(0o555)
    try:
        assert await flush_pending_deletions(db, settings.recordings_dir) == 0
        assert video.is_file()
        async with db.session() as session:
            assert len(await Repository(session).pending_file_deletions()) == 1
    finally:
        parent.chmod(0o755)
    assert await flush_pending_deletions(db, settings.recordings_dir) == 1
    assert not video.exists()
    async with db.session() as session:
        assert await Repository(session).pending_file_deletions() == []
        segment = await Repository(session).get_segment(segment_id)
        assert segment.file_removed is True
        assert segment.file_purge_done is True
    await db.dispose()


async def test_pending_deletion_does_not_remove_reused_live_path(tmp_path, monkeypatch):
    from live_recorder.recording.cleanup import flush_pending_deletions, queue_segment_deletion

    db, _session_id, job_id, segment_id = await _recording(tmp_path, monkeypatch)
    settings = _settings(tmp_path)
    video = tmp_path / "rec" / _UNTITLED
    async with db.session() as session:
        repo = Repository(session)
        segment = await repo.get_segment(segment_id)
        repo.mark_segment_removed(segment)
        await queue_segment_deletion(repo, settings.recordings_dir, segment)
        await session.commit()
    # キューが世代を記録したあと、同名パスが新しい録画へ差し替わる。
    video.unlink()
    video.write_bytes(b"new-capture")
    async with db.session() as session:
        repo = Repository(session)
        session.add(
            RecordingSegment(
                recording_job_id=job_id,
                segment_number=2,
                started_at=datetime(2026, 11, 12, 2, tzinfo=timezone.utc),
                finished_at=datetime(2026, 11, 12, 3, tzinfo=timezone.utc),
                file_path=_UNTITLED,
                file_size=11,
                duration_ms=1000,
                validation_status="ok",
            )
        )
        await session.commit()
    assert await flush_pending_deletions(db, settings.recordings_dir) == 1
    assert video.read_bytes() == b"new-capture"
    async with db.session() as session:
        repo = Repository(session)
        assert await repo.pending_file_deletions() == []
        old = await repo.get_segment(segment_id)
        assert old.file_purge_done is True
    await db.dispose()


async def test_partial_comment_retention_replaces_stale_delete_and_retries_ass(tmp_path, monkeypatch):
    from live_recorder.comments.segments import comment_export_name
    from live_recorder.db.models import Comment
    from live_recorder.recording.cleanup import (
        flush_pending_ass_refreshes,
        flush_pending_deletions,
    )

    db, session_id, _job_id, _segment_id = await _recording(tmp_path, monkeypatch)
    settings = _settings(tmp_path)
    video = settings.recordings_dir / _UNTITLED
    comment_file = video.with_name(comment_export_name(video.name))
    ass_file = video.with_suffix(".ass")
    comment_file.write_text("期限切れ\n", encoding="utf-8")
    ass_file.write_text("期限切れ\n", encoding="utf-8")
    async with db.session() as session:
        repo = Repository(session)
        job = await repo.latest_comment_job(session_id)
        assert job is not None
        job.export_path = comment_file.relative_to(settings.recordings_dir).as_posix()
        session.add(
            Comment(
                stream_session_id=session_id,
                platform_comment_id="retained",
                platform_user_id="viewer",
                nickname="視聴者",
                message="保持対象",
                received_at=datetime(2026, 11, 12, 0, 0, 0, 500000, tzinfo=timezone.utc),
                dedupe_key="retained",
            )
        )
        await session.commit()

    monkeypatch.setattr("live_recorder.monitor.supervisor.unlink_stored", lambda *_args: False)

    def fail_ass(*_args, **_kwargs):
        raise OSError("directory is read-only")

    monkeypatch.setattr("live_recorder.monitor.supervisor.refresh_combined_ass", fail_ass)
    monkeypatch.setattr("live_recorder.monitor.jobs.refresh_combined_ass", fail_ass)
    supervisor = Supervisor(
        settings,
        db,
        TwitCastingClient(client_id="", client_secret="", access_token=""),
    )
    async with db.session() as session:
        repo = Repository(session)
        await supervisor._refresh_comment_retention(repo, session_id, "Asia/Tokyo")
        await session.commit()

    assert "保持対象" in comment_file.read_text(encoding="utf-8")
    async with db.session() as session:
        pending = await Repository(session).pending_file_deletions()
        assert [(item.file_path, item.kind) for item in pending] == [(_UNTITLED, "refresh_ass")]
    # 古い SRT 削除要求は再生成時に取り消されている。
    assert await flush_pending_deletions(db, settings.recordings_dir) == 0
    assert comment_file.is_file()

    def write_ass(video_path, **_kwargs):
        video_path.with_suffix(".ass").write_text("保持対象\n", encoding="utf-8")

    monkeypatch.setattr("live_recorder.recording.cleanup.refresh_combined_ass", write_ass)
    assert await flush_pending_ass_refreshes(db, settings) == 1
    assert ass_file.read_text(encoding="utf-8") == "保持対象\n"
    async with db.session() as session:
        assert await Repository(session).pending_file_deletions() == []
    await db.dispose()


async def test_reconcile_retitles_follows_db_paths(tmp_path, monkeypatch):
    from live_recorder.monitor.jobs import _reconcile_retitles
    from live_recorder.recording.files import relative_to_root
    from live_recorder.recording.retitle import stage_retitle

    db, _session_id, _job_id, segment_id = await _recording(tmp_path, monkeypatch)
    settings = _settings(tmp_path)
    root = settings.recordings_dir
    source = root / _UNTITLED
    staged = stage_retitle(source, "公開")
    assert staged is not None
    async with db.session() as session:
        segment = await Repository(session).get_segment(segment_id)
        segment.file_path = relative_to_root(root, staged.dest)
        await session.commit()
    pending = [staged]
    await _reconcile_retitles(db, root, pending)
    assert pending == []
    assert staged.dest.is_file()
    assert not source.exists()

    restored = root / _UNTITLED
    restored.write_bytes(b"video")
    staged_again = stage_retitle(restored, "公開")
    assert staged_again is not None
    async with db.session() as session:
        segment = await Repository(session).get_segment(segment_id)
        segment.file_path = _UNTITLED
        await session.commit()
    pending = [staged_again]
    await _reconcile_retitles(db, root, pending)
    assert pending == []
    assert restored.is_file()
    assert not staged_again.dest.exists()
    await db.dispose()
