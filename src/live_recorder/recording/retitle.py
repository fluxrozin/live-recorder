"""確定した録画の「無題」を、セッションのタイトルへ付け直す。"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from live_recorder.comments.segments import comment_export_name
from live_recorder.recording.files import untitled_replacement
from live_recorder.speech.srt import speech_export_name

log = logging.getLogger(__name__)
_LEGACY_SUFFIXES = (".jsonl", ".csv", ".txt")
_NO_LINK = {errno.EPERM, errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK}


@dataclass
class StagedRetitle:
    """新しい名前を作った状態。DB が確定するまで元の名前も残す。"""

    source: Path
    dest: Path
    links: list[tuple[Path, Path, str]] = field(default_factory=list)


def stage_retitle(
    path: Path,
    title: str | None,
    *,
    reserved_paths: set[Path] | None = None,
) -> StagedRetitle | None:
    """新しい名前を作る。移さないとき、または対象外のときは None。"""
    if not path.is_file():
        return None
    try:
        dest = untitled_replacement(path, title, reserved_paths=reserved_paths)
    except OSError:
        log.exception("retitle name failed for %s", path)
        return None
    if dest is None:
        return None
    staged = StagedRetitle(path, dest)
    try:
        for src, dst in _sidecar_pairs(path, dest):
            _place(src, dst, staged.links)
        if not _place(path, dest, staged.links):
            _drop_new(staged.links)
            return None
    except OSError:
        log.exception("retitle failed for %s", path)
        _drop_new(staged.links)
        return None
    return staged


def publish_retitle(staged: StagedRetitle) -> None:
    """DB が新しいパスを指したあと、古い名前を消す。"""
    for src, dst, kind in staged.links:
        try:
            if not src.is_file() or not dst.is_file() or src == dst:
                continue
            if kind == "link" and src.samefile(dst):
                src.unlink()
            elif kind == "copy" and src.stat().st_size == dst.stat().st_size:
                src.unlink()
        except OSError:
            log.exception("retitle publish failed for %s", src)


def rollback_retitle(staged: StagedRetitle) -> None:
    """DB に古いパスが残るとき、新しい名前だけを消す。"""
    _drop_new(staged.links)


def retitle_closed_file(path: Path, title: str | None) -> Path:
    """その場で付け直す。DB 更新と分けるときは stage_retitle を使う。"""
    staged = stage_retitle(path, title)
    if staged is None:
        return path
    publish_retitle(staged)
    return staged.dest


def rewrite_stored_path(staged: StagedRetitle, relative: str | None) -> str | None:
    """実際に作った新しい名前だけを、保存パスへ反映する。"""
    if relative is None:
        return None
    mapping = {src.name: dst.name for src, dst, _kind in staged.links}
    renamed = mapping.get(Path(relative).name)
    if renamed is None:
        return relative
    return Path(relative).with_name(renamed).as_posix()


def _place(src: Path, dst: Path, links: list[tuple[Path, Path, str]]) -> bool:
    """既存のファイルを上書きせずに新しい名前を作る。作れたものはすぐ links に載せる。"""
    if not src.is_file() or src == dst:
        return False
    try:
        os.link(src, dst)
    except FileExistsError:
        try:
            same = src.samefile(dst)
        except OSError:
            return False
        if same:
            links.append((src, dst, "link"))
        return same
    except OSError as exc:
        if exc.errno not in _NO_LINK:
            raise
        return _copy_into(src, dst, links)
    links.append((src, dst, "link"))
    return True


def _copy_into(src: Path, dst: Path, links: list[tuple[Path, Path, str]]) -> bool:
    """リンクできないときは、名前を排他的に確保してから一時ファイル経由で写す。"""
    try:
        reserved = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    os.close(reserved)
    links.append((src, dst, "copy"))
    temporary = dst.with_name(f".retitle-{uuid.uuid4().hex[:12]}.tmp")
    try:
        shutil.copy2(src, temporary)
        temporary.replace(dst)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _drop_new(links: list[tuple[Path, Path, str]]) -> None:
    for src, dst, kind in reversed(links):
        try:
            if src == dst or not src.is_file():
                continue
            if kind == "link" and dst.is_file() and not src.samefile(dst):
                continue
            dst.unlink(missing_ok=True)
        except OSError:
            log.exception("retitle rollback failed for %s", dst)
    links.clear()


def _sidecar_pairs(old: Path, new: Path) -> list[tuple[Path, Path]]:
    names = [
        (speech_export_name(old.name), speech_export_name(new.name)),
        (comment_export_name(old.name), comment_export_name(new.name)),
        (old.with_suffix(".ass").name, new.with_suffix(".ass").name),
    ]
    names.extend((old.with_suffix(suffix).name, new.with_suffix(suffix).name) for suffix in _LEGACY_SUFFIXES)
    pairs: list[tuple[Path, Path]] = []
    seen: set[str] = set()
    for src_name, dst_name in names:
        if src_name in seen or src_name == dst_name:
            continue
        seen.add(src_name)
        pairs.append((old.with_name(src_name), new.with_name(dst_name)))
    return pairs
