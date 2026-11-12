"""録画ファイルの物理削除。DB の削除待ちを冪等に消化する。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from live_recorder.comments.ass import refresh_combined_ass
from live_recorder.comments.segments import comment_sidecar_relatives
from live_recorder.config import Settings
from live_recorder.db.repository import Repository
from live_recorder.db.session import Database
from live_recorder.recording.files import PathEscape, resolve_stored
from live_recorder.speech.srt import speech_srt_path, stored_speech_srt

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileGeneration:
    """パスが指すファイル世代。再利用された同名ファイルとは一致しない。"""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_path(cls, path: Path) -> FileGeneration | None:
        try:
            stat = path.stat()
        except (FileNotFoundError, OSError):
            return None
        if not path.is_file():
            return None
        return cls(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _stored_generation(root: Path, relative: str) -> tuple[Path | None, FileGeneration | None]:
    try:
        path = resolve_stored(root, relative)
    except PathEscape:
        return None, None
    return path, FileGeneration.from_path(path)


def unlink_stored(root: Path, relative: str, expected: FileGeneration | None = None) -> bool:
    """期待した世代だけを消す。別世代なら古い要求は完了として実体を守る。"""
    path, current = _stored_generation(root, relative)
    if path is None or current is None:
        return True
    if expected is not None and current != expected:
        return True
    if expected is None:
        # 0007 由来の世代情報がない要求は、安全のため現在の実体を削除しない。
        return True
    try:
        path.unlink()
    except OSError:
        log.exception("unlink failed for %s", relative)
        return False
    return not path.is_file()


def _segment_file_relatives(relative: str, speech_relative: str | None) -> list[str]:
    video = Path(relative)
    values = [relative, video.with_name(speech_srt_path(video).name).as_posix()]
    if speech_relative:
        values.append(speech_relative)
    values.extend(comment_sidecar_relatives(relative))
    return list(dict.fromkeys(values))


async def queue_file_deletion(
    repo: Repository,
    root: Path,
    relative: str,
    *,
    segment_id: int | None = None,
) -> bool:
    """現在存在するファイル世代を削除待ちへ入れる。"""
    _path, generation = _stored_generation(root, relative)
    if generation is None:
        return False
    await repo.enqueue_file_deletion(
        relative,
        kind="file",
        recording_segment_id=segment_id,
        expected_device=generation.device,
        expected_inode=generation.inode,
        expected_size=generation.size,
        expected_mtime_ns=generation.mtime_ns,
        expected_ctime_ns=generation.ctime_ns,
    )
    return True


async def queue_segment_deletion(repo: Repository, root: Path, segment) -> int:
    """動画と、その時点で存在する副ファイルを世代付きでキューへ入れる。"""
    if await repo.path_has_live_segment(segment.file_path, excluding_id=segment.id):
        segment.file_purge_done = True
        return 0
    queued = 0
    for relative in _segment_file_relatives(segment.file_path, segment.speech_srt_path):
        queued += int(await queue_file_deletion(repo, root, relative, segment_id=segment.id))
    segment.file_purge_done = queued == 0
    return queued


def _generation_from_row(row) -> FileGeneration | None:
    values = (
        row.expected_device,
        row.expected_inode,
        row.expected_size,
        row.expected_mtime_ns,
        row.expected_ctime_ns,
    )
    if any(value is None for value in values):
        return None
    return FileGeneration(*(int(value) for value in values))


async def flush_pending_deletions(db: Database, root: Path) -> int:
    """削除要求が記録した世代だけを消し、パス再利用後の実体は守る。"""
    async with db.session() as session:
        deletion_ids = [
            item.id
            for item in await Repository(session).pending_file_deletions()
            if item.kind in {"file", "video"}
        ]
    cleared = 0
    for deletion_id in deletion_ids:
        async with db.session() as session:
            repo = Repository(session)
            await repo.lock_recording_paths()
            item = await repo.get_pending_file_deletion(deletion_id)
            if item is None or item.kind not in {"file", "video"}:
                continue
            if not unlink_stored(root, item.file_path, _generation_from_row(item)):
                continue
            segment_id = item.recording_segment_id
            await repo.clear_pending_file_deletion(deletion_id)
            await session.commit()
        # セグメント行は別トランザクションで更新する。パスロックを持ったまま
        # 録画確定側のセグメントロックを待たず、逆順デッドロックを避ける。
        if segment_id is not None:
            async with db.session() as session:
                repo = Repository(session)
                await repo.finish_segment_purge_if_ready(segment_id)
                await session.commit()
        cleared += 1
    return cleared


async def queue_removed_segment_files(db: Database, root: Path) -> int:
    """未完了の削除済みセグメントを、現在存在する世代だけキューへ戻す。"""
    async with db.session() as session:
        repo = Repository(session)
        await repo.lock_recording_paths()
        segments = await repo.segments_needing_purge()
        queued = 0
        for segment in segments:
            queued += await queue_segment_deletion(repo, root, segment)
        await session.commit()
    return queued


async def queue_ass_refresh(
    repo: Repository,
    video_relative: str,
    speech_relative: str | None,
) -> None:
    """ASS 再生成を、削除要求とは別の永続操作として記録する。"""
    await repo.enqueue_file_deletion(
        video_relative,
        speech_relative,
        kind="refresh_ass",
    )


async def flush_pending_ass_refreshes(db: Database, settings: Settings) -> int:
    """失敗していた ASS 再生成を再試行する。成功した操作だけキューから外す。"""
    async with db.session() as session:
        operation_ids = [
            item.id for item in await Repository(session).pending_file_deletions(kind="refresh_ass")
        ]
    cleared = 0
    for operation_id in operation_ids:
        async with db.session() as session:
            repo = Repository(session)
            await repo.lock_recording_paths()
            item = await repo.get_pending_file_deletion(operation_id)
            if item is None or item.kind != "refresh_ass":
                continue
            try:
                video = resolve_stored(settings.recordings_dir, item.file_path)
            except PathEscape:
                video = None
            if video is not None and video.is_file():
                try:
                    await asyncio.to_thread(
                        refresh_combined_ass,
                        video,
                        ffprobe=settings.ffprobe_path,
                        speech_srt=stored_speech_srt(
                            settings.recordings_dir, item.speech_srt_path
                        ),
                    )
                except Exception:
                    log.exception("pending combined ass refresh failed for %s", item.file_path)
                    continue
            await repo.clear_pending_file_deletion(operation_id)
            await session.commit()
        cleared += 1
    return cleared
