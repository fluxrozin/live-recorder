from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from sqlalchemy import text

from live_recorder.comments.ass import refresh_combined_ass
from live_recorder.config import Settings, load_settings
from live_recorder.db.repository import Repository
from live_recorder.db.runtime import load_runtime
from live_recorder.db.session import Database
from live_recorder.logging import setup_logging
from live_recorder.migrate import upgrade
from live_recorder.recording.files import relative_to_root, resolve_stored
from live_recorder.speech.engine import load_whisper
from live_recorder.speech.queue import (
    clip_error,
    interpret_speech,
    speech_result_path,
    transcribe_video,
)

log = logging.getLogger(__name__)
LOCK_KEY = 824202
HEARTBEAT = Path("/tmp/live-recorder-transcriber-heartbeat")
_POLL_SECONDS = 2


async def _lock(db: Database):
    connection = await db.engine.connect()
    for _ in range(30):
        result = await connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY})
        await connection.commit()
        if result.scalar():
            return connection
        await asyncio.sleep(1)
    await connection.close()
    raise SystemExit("別の文字起こしプロセスが稼働中です")


async def _heartbeat(stop: asyncio.Event) -> None:
    while not stop.is_set():
        HEARTBEAT.touch()
        try:
            await asyncio.wait_for(stop.wait(), 10)
        except TimeoutError:
            continue


async def _claim(db: Database):
    async with db.session() as session:
        runtime = await load_runtime(session)
        repo = Repository(session)
        segment = await repo.claim_next_speech()
        if segment is None:
            await session.commit()
            return None
        previous = segment.speech_srt_path
        segment.speech_status = "running"
        claimed = (
            segment.id,
            segment.file_path,
            runtime.speech_model,
            runtime.speech_language,
            previous,
        )
        await session.commit()
        return claimed


async def _finish(
    db: Database,
    segment_id: int,
    status: str,
    srt_path: str | None,
    error: str | None,
    *,
    keep_path: bool,
) -> None:
    async with db.session() as session:
        await Repository(session).finish_speech(
            segment_id,
            status=status,
            srt_path=srt_path,
            error=error,
            keep_path=keep_path,
        )
        await session.commit()


async def _run(stop: asyncio.Event) -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    os.environ.setdefault("HF_HOME", str(settings.config_dir / "huggingface"))
    upgrade()
    db = Database(settings)
    connection = await _lock(db)
    async with db.session() as session:
        await Repository(session).release_running_speech()
        await session.commit()
    model = None
    loaded_name: str | None = None
    cache = settings.config_dir / "whisper"
    try:
        while not stop.is_set():
            HEARTBEAT.touch()
            claimed = await _claim(db)
            if claimed is None:
                try:
                    await asyncio.wait_for(stop.wait(), _POLL_SECONDS)
                except TimeoutError:
                    continue
                continue
            segment_id, relative, model_name, language, previous_srt = claimed
            video = None
            try:
                video = resolve_stored(settings.recordings_dir, relative)
                if not video.is_file():
                    raise FileNotFoundError(relative)
                if model is None or loaded_name != model_name:
                    model = await asyncio.to_thread(load_whisper, model_name, cache)
                    loaded_name = model_name
                current = model
                status, produced, error = await asyncio.to_thread(
                    interpret_speech,
                    lambda current=current, video=video, language=language: transcribe_video(
                        settings.ffmpeg_path,
                        current,
                        video,
                        language=language,
                        ffprobe=settings.ffprobe_path,
                    ),
                )
            except Exception as exc:
                status, produced, error = "failed", None, clip_error(exc)
            if video is None:
                await _finish(db, segment_id, status, None, error, keep_path=False)
            else:
                status, error = await _publish_speech(
                    db,
                    settings,
                    segment_id,
                    video,
                    status,
                    produced,
                    error,
                    previous_srt,
                )
            if status == "failed":
                log.warning("speech segment %s failed: %s", segment_id, error)
            else:
                log.info("speech segment %s completed", segment_id)
    finally:
        await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
        await connection.commit()
        await connection.close()
        await db.dispose()


async def _publish_speech(
    db: Database,
    settings: Settings,
    segment_id: int,
    video: Path,
    status: str,
    produced: Path | None,
    error: str | None,
    previous_srt: str | None,
) -> tuple[str, str | None]:
    """削除と同時に字幕を書き戻さないよう、配信行をロックしてから公開する。"""
    async with db.session() as session:
        repo = Repository(session)
        found = await repo.segment_with_session(segment_id)
        if found is not None:
            await repo.lock_session(found[1])
            found = await repo.segment_with_session(segment_id)
        segment = found[0] if found is not None else None
        gone = segment is None or segment.file_removed or segment.finished_at is None or not video.is_file()
        if produced is not None and gone:
            produced.unlink(missing_ok=True)
            video.with_suffix(".ass").unlink(missing_ok=True)
            status, produced, error, previous_srt = "failed", None, "録画は削除されました", None
        produced_relative = relative_to_root(settings.recordings_dir, produced) if produced is not None else None
        srt_relative = speech_result_path(status=status, produced=produced_relative, previous=previous_srt)
        await repo.finish_speech(segment_id, status=status, srt_path=srt_relative, error=error, keep_path=False)
        if status == "completed" and segment is not None and not segment.file_removed and video.is_file():
            try:
                await asyncio.to_thread(refresh_combined_ass, video, ffprobe=settings.ffprobe_path)
            except Exception:
                log.exception("combined ass failed for segment %s", segment_id)
        await session.commit()
    return status, error


def main() -> None:
    stop = asyncio.Event()

    async def _main() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        beat = asyncio.create_task(_heartbeat(stop))
        try:
            await _run(stop)
        finally:
            stop.set()
            await beat

    asyncio.run(_main())


if __name__ == "__main__":
    main()
