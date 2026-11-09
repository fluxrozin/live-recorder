from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass

from live_recorder.redact import redact


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stderr: str
    stopped: bool


async def _read_limited(stream: asyncio.StreamReader | None, limit: int = 200_000) -> str:
    if stream is None:
        return ""
    chunks: list[bytes] = []
    size = 0
    while True:
        line = await stream.readline()
        if not line:
            break
        if size < limit:
            chunks.append(line)
            size += len(line)
    return redact(b"".join(chunks).decode("utf-8", errors="replace"))


async def _shutdown(proc: asyncio.subprocess.Process, kill_after: float) -> None:
    if proc.returncode is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        await asyncio.wait_for(proc.wait(), kill_after)
        return
    except TimeoutError:
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except TimeoutError:
        proc.kill()
        await proc.wait()


async def run_process(args: list[str], stop: asyncio.Event, *, kill_after: float = 12) -> ProcessResult:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_task = asyncio.create_task(_read_limited(proc.stderr))
    try:
        while proc.returncode is None:
            if stop.is_set():
                await _shutdown(proc, kill_after)
                break
            try:
                await asyncio.wait_for(proc.wait(), 0.5)
            except TimeoutError:
                continue
        if proc.returncode is None:
            await proc.wait()
    finally:
        stderr = await stderr_task
    return ProcessResult(returncode=proc.returncode, stderr=stderr, stopped=stop.is_set())


async def command_output(args: list[str], timeout: float = 8) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        return ""
    text = (out or b"").decode("utf-8", errors="replace").strip()
    return text.splitlines()[0][:200] if text else ""
