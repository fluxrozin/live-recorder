"""確定した録画の「無題」を、セッションのタイトルへ付け直す。"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from live_recorder.comments.segments import comment_export_name
from live_recorder.recording.files import untitled_replacement
from live_recorder.speech.srt import speech_export_name

log = logging.getLogger(__name__)
_LEGACY_SUFFIXES = (".jsonl", ".csv", ".txt")


@dataclass
class StagedRetitle:
    """新しい名前へリンクした状態。DB が確定するまで元の名前も残す。"""

    source: Path
    dest: Path
    links: list[tuple[Path, Path, str]] = field(default_factory=list)
    release_sources: bool = True


def stage_retitle(path: Path, title: str | None) -> StagedRetitle | None:
    """新しい名前をハードリンクする。移さないとき、または対象外のときは None。"""
    if not path.is_file():
        return None
    try:
        dest = untitled_replacement(path, title)
    except OSError:
        log.exception("retitle name failed for %s", path)
        return None
    if dest is None:
        return None
    links: list[tuple[Path, Path, str]] = []
    try:
        for src, dst in _sidecar_pairs(path, dest):
            kind = _place(src, dst)
            if kind is not None:
                links.append((src, dst, kind))
        kind = _place(path, dest)
        if kind is None:
            _drop_new(links)
            return None
        links.append((path, dest, kind))
    except OSError:
        log.exception("retitle failed for %s", path)
        _drop_new(links)
        return None
    return StagedRetitle(path, dest, links)


def publish_retitle(staged: StagedRetitle) -> None:
    """DB が新しいパスを指したあと、古い名前を消す。文字起こし中の複製は残す。"""
    if not staged.release_sources:
        return
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
    """実際にリンクしたファイル名だけを、新しいパスへ合わせる。"""
    if relative is None:
        return None
    mapping = {src.name: dst.name for src, dst, _kind in staged.links}
    renamed = mapping.get(Path(relative).name)
    if renamed is None:
        return relative
    return Path(relative).with_name(renamed).as_posix()


def _place(src: Path, dst: Path) -> str | None:
    """同じ内容の新しい名前を作る。別のファイルが既にあるときは None。"""
    if not src.is_file() or src == dst:
        return None
    if dst.exists():
        try:
            if src.samefile(dst):
                return "link"
        except OSError:
            return None
        return None
    try:
        os.link(src, dst)
        return "link"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


def _drop_new(links: list[tuple[Path, Path, str]]) -> None:
    for src, dst, _kind in reversed(links):
        try:
            if not dst.is_file() or src == dst:
                continue
            if src.is_file():
                try:
                    if src.samefile(dst):
                        dst.unlink()
                        continue
                except OSError:
                    pass
            if src.is_file():
                dst.unlink()
        except OSError:
            log.exception("retitle rollback failed for %s", dst)


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
