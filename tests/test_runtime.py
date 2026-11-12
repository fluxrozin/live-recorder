import asyncio
import os
from pathlib import Path

import pytest

from live_recorder.recording.finalize import (
    allow_new_recording,
    container_name,
    finalize_recording,
    remux_args,
)
from live_recorder.recording.process import run_process


def test_low_disk_blocks_new_recording():
    assert not allow_new_recording(100, 200)
    assert allow_new_recording(200, 200)


async def test_process_stop_does_not_use_a_shell():
    stop = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.2)
        stop.set()

    stopper = asyncio.create_task(stop_soon())
    result = await run_process(["python", "-c", "import time; time.sleep(30)"], stop, kill_after=2)
    await stopper
    assert result.stopped
    assert result.returncode is not None


async def test_recording_and_comment_tasks_are_independent():
    done = asyncio.Event()

    async def recording() -> str:
        await asyncio.sleep(0.05)
        done.set()
        return "kept"

    async def comments() -> None:
        raise RuntimeError("comment failed")

    rec_task = asyncio.create_task(recording())
    comment_task = asyncio.create_task(comments())
    with pytest.raises(RuntimeError):
        await comment_task
    assert await rec_task == "kept"
    assert done.is_set()


async def test_finalize_remuxes_local_mpegts(tmp_path: Path):
    ffmpeg = "ffmpeg"
    source = tmp_path / "sample.ts"
    proc = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x120:rate=10:duration=1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-f",
        "mpegts",
        str(source),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode != 0 or not source.exists():
        pytest.skip("ffmpeg test source is unavailable")
    final = await finalize_recording(ffmpeg, "ffprobe", source)
    assert final.validation == "ok"
    assert final.path.suffix == ".mp4"
    assert final.container == "mp4"
    assert final.size > 0
    assert final.duration_ms and final.duration_ms > 0


def test_container_name_collapses_ffprobe_labels():
    assert container_name("mov,mp4,m4a,3gp,3g2,mj2") == "mp4"
    assert container_name("flv") == "flv"
    assert container_name("mpegts") == "mpegts"
    assert container_name(None) is None


def test_hevc_remux_uses_hvc1_tag(tmp_path: Path):
    source = tmp_path / "seg.ts"
    target = tmp_path / "seg.mp4"
    hevc = remux_args("ffmpeg", source, target, "hevc")
    h264 = remux_args("ffmpeg", source, target, "h264")
    assert hevc[hevc.index("-tag:v") + 1] == "hvc1"
    assert "-tag:v" not in h264
    assert hevc[-1] == str(target)


def test_remux_partial_is_not_an_existing_recording(tmp_path: Path):
    from live_recorder.recording.finalize import remux_partial_path

    source = tmp_path / "clip.ts"
    existing = tmp_path / "clip.mp4"
    alternate = tmp_path / "clip_remux.mp4"
    existing.write_bytes(b"keep")
    alternate.write_bytes(b"also")
    partial = remux_partial_path(source)
    assert partial.parent == source.parent
    assert partial.name.startswith(".remux-") and partial.name.endswith(".mp4.partial")
    assert len(partial.name.encode()) <= 255
    assert partial != existing
    assert partial != alternate
    long_source = tmp_path / ("a" * 249 + ".ts")
    assert len(long_source.name.encode()) == 252
    long_partial = remux_partial_path(long_source)
    assert len(long_partial.name.encode()) <= 255
    long_partial.unlink(missing_ok=True)
    assert existing.read_bytes() == b"keep"
    assert alternate.read_bytes() == b"also"


async def test_failed_remux_removes_empty_mp4(tmp_path: Path):
    source = tmp_path / "seg.ts"
    source.write_bytes(b"\x00" * 2048)
    empty = tmp_path / "seg.mp4"
    empty.write_bytes(b"")
    ffprobe = tmp_path / "ffprobe"
    ffprobe.write_text(
        "#!/bin/sh\n"
        "cat <<'EOF'\n"
        '{"streams":[{"codec_type":"video","codec_name":"h264"},'
        '{"codec_type":"audio","codec_name":"aac"}],'
        '"format":{"format_name":"mpegts","duration":"1.5"}}\n'
        "EOF\n",
        encoding="utf-8",
    )
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text(
        "#!/bin/sh\n"
        'target="${@: -1}"\n'
        ': > "$target"\n'
        "exit 1\n",
        encoding="utf-8",
    )
    ffprobe.chmod(0o755)
    ffmpeg.chmod(0o755)
    final = await finalize_recording(str(ffmpeg), str(ffprobe), source)
    assert final.path == source
    assert final.validation == "ok"
    assert final.container == "mpegts"
    assert not empty.exists()


_MEDIA_PROBE = (
    "#!/bin/sh\n"
    "cat <<'EOF'\n"
    '{"streams":[{"codec_type":"video","codec_name":"h264"},'
    '{"codec_type":"audio","codec_name":"aac"}],'
    '"format":{"format_name":"mpegts","duration":"1.5"}}\n'
    "EOF\n"
)


def _tools(tmp_path: Path, ffmpeg_body: str) -> tuple[str, str]:
    ffprobe = tmp_path / "ffprobe"
    ffmpeg = tmp_path / "ffmpeg"
    ffprobe.write_text(_MEDIA_PROBE, encoding="utf-8")
    ffmpeg.write_text(ffmpeg_body, encoding="utf-8")
    ffprobe.chmod(0o755)
    ffmpeg.chmod(0o755)
    return str(ffmpeg), str(ffprobe)


async def test_finalize_keeps_an_older_mp4_and_remuxes_beside_it(tmp_path: Path):
    source = tmp_path / "seg.ts"
    older = tmp_path / "seg.mp4"
    older.write_bytes(b"K" * 2048)
    source.write_bytes(b"\x00" * 2048)
    os.utime(older, (100, 100))
    os.utime(source, (200, 200))
    ffmpeg, ffprobe = _tools(
        tmp_path,
        "#!/bin/sh\n"
        "for target do :; done\n"
        "python3 -c \"import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b'F'*2048)\" \"$target\"\n",
    )
    final = await finalize_recording(ffmpeg, ffprobe, source)
    assert final.validation == "ok"
    assert final.path != older
    assert final.path.name == "seg_2.mp4"
    assert older.read_bytes() == b"K" * 2048
    assert final.path.read_bytes() == b"F" * 2048
    assert final.obsolete == source
    assert source.exists()


async def test_finalize_adopts_only_a_newer_mp4_from_this_segment(tmp_path: Path):
    source = tmp_path / "seg.ts"
    current = tmp_path / "seg.mp4"
    source.write_bytes(b"\x00" * 2048)
    current.write_bytes(b"R" * 2048)
    os.utime(source, (100, 100))
    os.utime(current, (200, 200))
    ffmpeg, ffprobe = _tools(tmp_path, "#!/bin/sh\nexit 9\n")
    final = await finalize_recording(ffmpeg, ffprobe, source)
    assert final.path == current
    assert current.read_bytes() == b"R" * 2048
    assert final.obsolete == source
    assert source.exists()


async def test_finalize_long_name_does_not_need_a_long_partial(tmp_path: Path):
    source = tmp_path / ("a" * 249 + ".ts")
    assert len(source.name.encode()) == 252
    source.write_bytes(b"\x00" * 2048)
    ffmpeg, ffprobe = _tools(
        tmp_path,
        "#!/bin/sh\n"
        "for target do :; done\n"
        "python3 -c \"import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b'F'*2048)\" \"$target\"\n",
    )
    final = await finalize_recording(ffmpeg, ffprobe, source)
    assert final.validation == "ok"
    assert final.path.suffix == ".mp4"
    assert len(final.path.name.encode()) <= 255
    assert final.obsolete == source
    assert source.exists()
    assert not list(tmp_path.glob(".remux-*"))
