import asyncio
from pathlib import Path

import pytest

from live_recorder.recording.finalize import allow_new_recording, finalize_recording
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
    assert final.size > 0
    assert final.duration_ms and final.duration_ms > 0
