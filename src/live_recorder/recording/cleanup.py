"""録画ファイルの物理削除。DB の削除待ちを冪等に消化する。"""

from __future__ import annotations

import logging
from pathlib import Path

from live_recorder.comments.ass import unlink_combined_ass
from live_recorder.comments.segments import comment_sidecar_relatives
from live_recorder.db.repository import Repository
from live_recorder.db.session import Database
from live_recorder.recording.files import PathEscape, resolve_stored
from live_recorder.speech.srt import unlink_speech_sidecar

log = logging.getLogger(__name__)


def unlink_stored(root: Path, relative: str) -> bool:
    """1 ファイルを消す。既に無い・不正パスは完了、権限失敗は未完了。"""
    try:
        path = resolve_stored(root, relative)
    except PathEscape:
        return True
    if not path.is_file():
        return True
    try:
        path.unlink()
    except OSError:
        log.exception("unlink failed for %s", relative)
        return False
    return not path.is_file()


def unlink_recorded_tree(root: Path, relative: str, speech_relative: str | None = None) -> bool:
    """動画と副ファイルを消す。動画が残ったら未完了（キューに残す）。"""
    try:
        path = resolve_stored(root, relative)
    except PathEscape:
        return True
    ok = True
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            log.exception("unlink failed for %s", relative)
            ok = False
    try:
        unlink_speech_sidecar(path)
    except OSError:
        log.exception("speech sidecar unlink failed for %s", relative)
    if speech_relative:
        unlink_stored(root, speech_relative)
    for sidecar in comment_sidecar_relatives(relative):
        unlink_stored(root, sidecar)
    try:
        unlink_combined_ass(path)
    except OSError:
        log.exception("combined ass unlink failed for %s", relative)
    return ok and not path.is_file()


async def flush_pending_deletions(db: Database, root: Path) -> int:
    """削除待ちを消化する。成功または既に無いものだけキューから外す。"""
    async with db.session() as session:
        rows = [
            (item.id, item.file_path, item.speech_srt_path, item.kind)
            for item in await Repository(session).pending_file_deletions()
        ]
    cleared = 0
    for deletion_id, relative, speech, kind in rows:
        if kind == "file":
            done = unlink_stored(root, relative)
        else:
            done = unlink_recorded_tree(root, relative, speech)
        if not done:
            continue
        async with db.session() as session:
            await Repository(session).clear_pending_file_deletion(deletion_id)
            await session.commit()
        cleared += 1
    return cleared


async def queue_removed_segment_files(db: Database) -> int:
    """file_removed なのにキューに無いセグメントを拾い直す。"""
    async with db.session() as session:
        repo = Repository(session)
        queued = await repo.enqueue_removed_segments()
        await session.commit()
    return queued
