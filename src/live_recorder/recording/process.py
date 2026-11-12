from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass
from pathlib import Path

from live_recorder.redact import redact


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stderr: str
    stopped: bool
    stalled: bool = False


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


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


async def run_process(
    args: list[str],
    stop: asyncio.Event,
    *,
    kill_after: float = 12,
    idle_path: Path | None = None,
    idle_timeout: float | None = None,
) -> ProcessResult:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_task = asyncio.create_task(_read_limited(proc.stderr))
    cancelled = False
    stopped = stop.is_set()
    stalled = False
    last_size = -1
    last_change = asyncio.get_running_loop().time()
    try:
        while proc.returncode is None:
            if stop.is_set():
                stopped = True
                await _shutdown(proc, kill_after)
                break
            if idle_path is not None and idle_timeout is not None:
                size = _file_size(idle_path)
                now = asyncio.get_running_loop().time()
                if size != last_size:
                    last_size = size
                    last_change = now
                elif now - last_change >= idle_timeout:
                    # プレイリストの再読み込みが成功したままだと、FFmpeg は
                    # 映像を書かずに待ち続ける。止めて録り直せるようにする。
                    stalled = True
                    await _shutdown(proc, kill_after)
                    break
            try:
                await asyncio.wait_for(proc.wait(), 0.5)
            except TimeoutError:
                continue
        if proc.returncode is None:
            await proc.wait()
    except asyncio.CancelledError:
        cancelled = True
        await _shutdown_after_cancel(proc, stderr_task)
        raise
    if not cancelled:
        try:
            stderr = await asyncio.wait_for(stderr_task, 5)
        except TimeoutError:
            stderr_task.cancel()
            stderr = ""
    else:
        stderr = ""
    return ProcessResult(
        returncode=proc.returncode,
        stderr=stderr,
        stopped=stopped or stop.is_set(),
        stalled=stalled,
    )


async def _shutdown_after_cancel(proc: asyncio.subprocess.Process, stderr_task: asyncio.Task[str]) -> None:
    """キャンセル中でも子プロセスを止める。Python 3.12 では待機の前にキャンセルを一旦外す。"""
    task = asyncio.current_task()
    if task is not None:
        while task.uncancel():
            pass
    try:
        await _shutdown(proc, 5)
    except Exception:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if not stderr_task.done():
        stderr_task.cancel()
    try:
        await asyncio.wait_for(stderr_task, 2)
    except (asyncio.CancelledError, TimeoutError, Exception):
        pass


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
