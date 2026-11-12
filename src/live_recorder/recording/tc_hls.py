"""ツイキャスの tc-hls を FFmpeg に渡す前に、冒頭のプリロールを除く。"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin

import httpx

from live_recorder.platforms.twitcasting import (
    TWITCASTING_REFERER,
    TWITCASTING_USER_AGENT,
    validate_hls_url,
)
from live_recorder.recording.commands import ffmpeg_hls_args
from live_recorder.recording.process import ProcessResult, run_process

log = logging.getLogger(__name__)

_PLAYER_HEADERS = {
    "User-Agent": TWITCASTING_USER_AGENT,
    "Referer": TWITCASTING_REFERER,
    "Accept": "*/*",
}
_GLOBAL_EXACT = {"#EXTM3U", "#EXT-X-INDEPENDENT-SEGMENTS"}
_GLOBAL_PREFIXES = (
    "#EXT-X-VERSION:",
    "#EXT-X-TARGETDURATION:",
    "#EXT-X-MEDIA-SEQUENCE:",
    "#EXT-X-PLAYLIST-TYPE:",
    "#EXT-X-START:",
    "#EXT-X-ALLOW-CACHE:",
    "#EXT-X-DISCONTINUITY-SEQUENCE:",
    "#EXT-X-I-FRAMES-ONLY",
)
_MAP_URI = re.compile(r'URI="([^"]*)"', re.IGNORECASE)
_BANDWIDTH = re.compile(r"BANDWIDTH=(\d+)")
# セグメントは数秒。これを超えて出力が増えないときは、不連続で固まったとみて録り直す。
_IDLE_TIMEOUT = 45.0
_READ_TIMEOUT_US = 30_000_000
_REFRESH_SECONDS = 1.0


class TcHlsError(RuntimeError):
    pass


@dataclass(frozen=True)
class FilteredPlaylist:
    text: str
    kept_segments: int
    dropped_preroll: int


def _is_preroll(uri: str) -> bool:
    return "preroll-" in uri


def _is_global(line: str) -> bool:
    return line in _GLOBAL_EXACT or line.startswith(_GLOBAL_PREFIXES)


def _rewrite_tag(line: str, base_url: str) -> str | None:
    match = _MAP_URI.search(line)
    if match is None:
        return line
    absolute = urljoin(base_url, match.group(1))
    if _is_preroll(absolute) or validate_hls_url(absolute) is None:
        return None
    return f"{line[: match.start(1)]}{absolute}{line[match.end(1) :]}"


def filter_preroll_playlist(text: str, base_url: str) -> FilteredPlaylist:
    """プリロールの fMP4 と、その直後の不連続を除いたメディアプレイリストを返す。

    配信開始直後の tc-hls は読み込み画面のセグメントのあとで初期化が変わる。
    その不連続をコピーすると、映像は数秒で止まり FFmpeg は配信終了まで待ち続ける。
    """
    header: list[str] = []
    pending: list[str] = []
    body: list[str] = []
    sequence: int | None = None
    sequence_index: int | None = None
    dropped_prefix = 0
    dropped_preroll = 0
    kept = 0
    started = False

    def emit(uri: str) -> None:
        nonlocal dropped_prefix, dropped_preroll, kept, started
        absolute = urljoin(base_url, uri)
        if _is_preroll(absolute) or validate_hls_url(absolute) is None:
            if _is_preroll(absolute):
                dropped_preroll += 1
            if not started:
                dropped_prefix += 1
            pending.clear()
            return
        for line in pending:
            if not started and line.startswith("#EXT-X-DISCONTINUITY") and not line.startswith(
                "#EXT-X-DISCONTINUITY-SEQUENCE"
            ):
                continue
            rewritten = _rewrite_tag(line, base_url)
            if rewritten is not None:
                body.append(rewritten)
        pending.clear()
        body.append(absolute)
        kept += 1
        started = True

    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-ENDLIST"):
            body.append(line)
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                sequence = int(line.split(":", 1)[1].strip())
            except ValueError:
                sequence = None
            sequence_index = len(header)
            header.append(line)
            continue
        if line.startswith("#") and _is_global(line):
            header.append(line)
            continue
        if line.startswith("#"):
            pending.append(line)
            continue
        emit(line)

    if dropped_prefix:
        updated = dropped_prefix if sequence is None else sequence + dropped_prefix
        tag = f"#EXT-X-MEDIA-SEQUENCE:{updated}"
        if sequence_index is None:
            header.append(tag)
        else:
            header[sequence_index] = tag
    rendered = "\n".join([*header, *body])
    if rendered:
        rendered += "\n"
    return FilteredPlaylist(rendered, kept, dropped_preroll)


def highest_variant(text: str, base_url: str) -> str | None:
    """マスタープレイリストから、許可されたホストで帯域が最大のメディア URL を選ぶ。"""
    best_bandwidth = -1
    best_url: str | None = None
    pending: int | None = None
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            match = _BANDWIDTH.search(line)
            pending = int(match.group(1)) if match else 0
            continue
        if not line or line.startswith("#") or pending is None:
            continue
        absolute = urljoin(base_url, line)
        if validate_hls_url(absolute) and pending >= best_bandwidth:
            best_bandwidth = pending
            best_url = absolute
        pending = None
    return best_url


def _is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/live.m3u8":
            self.send_error(404)
            return
        server = self.server
        if not isinstance(server, _Server):
            self.send_error(500)
            return
        with server.lock:
            payload = server.playlist.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        return


class _Server(ThreadingHTTPServer):
    def __init__(self, playlist: str) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.playlist = playlist
        self.lock = threading.Lock()


class TcHlsMirror:
    """プリロールを除いたプレイリストをローカルに出し、配信中は更新し続ける。"""

    def __init__(
        self,
        playlist_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        wait_seconds: float = 20,
    ) -> None:
        self.upstream = playlist_url
        self.wait_seconds = wait_seconds
        self._client = client
        self._owns_client = client is None
        self._closed = asyncio.Event()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._refresh: asyncio.Task[None] | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20, follow_redirects=True)
            self._owns_client = True
        return self._client

    async def _fetch(self, url: str) -> tuple[str, str]:
        response = await self._http().get(url, headers=_PLAYER_HEADERS)
        final = validate_hls_url(str(response.url))
        if response.status_code != 200 or final is None:
            reason = "playlist host rejected" if final is None else f"playlist http_{response.status_code}"
            raise TcHlsError(reason)
        return final, response.text

    async def _pull(self) -> FilteredPlaylist:
        final, text = await self._fetch(self.upstream)
        self.upstream = final
        if _is_master(text):
            variant = highest_variant(text, final)
            if variant is None:
                raise TcHlsError("tc-hls variant missing")
            final, text = await self._fetch(variant)
            self.upstream = final
        if not text.lstrip("\ufeff \t\r\n").startswith("#EXTM3U"):
            raise TcHlsError("playlist is not hls")
        filtered = filter_preroll_playlist(text, self.upstream)
        if self._server is not None and (filtered.kept_segments > 0 or "#EXT-X-ENDLIST" in filtered.text):
            with self._server.lock:
                self._server.playlist = filtered.text
        return filtered

    def _serve(self, playlist: str) -> str:
        server = _Server(playlist)
        thread = threading.Thread(target=server.serve_forever, name="tc-hls", daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        host, port = server.server_address[:2]
        return f"http://{host}:{port}/live.m3u8"

    async def _refresh_loop(self) -> None:
        while not self._closed.is_set():
            try:
                await asyncio.wait_for(self._closed.wait(), _REFRESH_SECONDS)
                return
            except TimeoutError:
                pass
            try:
                await self._pull()
            except (TcHlsError, httpx.HTTPError):
                log.info("tc-hls playlist refresh failed")

    async def ffmpeg_input(self, stop: asyncio.Event) -> str | None:
        """FFmpeg に渡す URL。プリロールが無ければ配信元をそのまま返す。"""
        deadline = asyncio.get_running_loop().time() + self.wait_seconds
        while True:
            if stop.is_set():
                return None
            filtered = await self._pull()
            if filtered.kept_segments > 0 and filtered.dropped_preroll == 0:
                return self.upstream
            if filtered.kept_segments > 0:
                log.info("removed %s twitcasting preroll segments", filtered.dropped_preroll)
                local = self._serve(filtered.text)
                self._refresh = asyncio.create_task(self._refresh_loop())
                return local
            if asyncio.get_running_loop().time() >= deadline:
                raise TcHlsError("tc-hls has no media")
            try:
                await asyncio.wait_for(stop.wait(), 1)
                return None
            except TimeoutError:
                continue

    async def aclose(self) -> None:
        self._closed.set()
        task = self._refresh
        self._refresh = None
        if task is not None:
            task.cancel()
        server = self._server
        self._server = None
        if server is not None:
            # 配信スレッド側で待っているので、ここで止めてもループは塞がない。
            server.shutdown()
            server.server_close()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(2)
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


async def record_tc_hls(
    *,
    ffmpeg: str,
    playlist_url: str,
    output: Path,
    stop: asyncio.Event,
) -> ProcessResult:
    mirror = TcHlsMirror(playlist_url)
    try:
        source = await mirror.ffmpeg_input(stop)
        if source is None:
            return ProcessResult(None, "", True)
        result = await run_process(
            ffmpeg_hls_args(
                ffmpeg,
                source,
                str(output),
                user_agent=TWITCASTING_USER_AGENT,
                referer=TWITCASTING_REFERER,
                read_timeout_us=_READ_TIMEOUT_US,
            ),
            stop,
            idle_path=output,
            idle_timeout=_IDLE_TIMEOUT,
        )
    finally:
        await mirror.aclose()
    if result.stalled:
        log.info("twitcasting recording stalled")
        stderr = f"{result.stderr}\noutput stalled".strip()
        return ProcessResult(result.returncode, stderr, result.stopped, stalled=True)
    return result
