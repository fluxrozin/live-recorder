from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from live_recorder import __version__
from live_recorder.cli import verify_password
from live_recorder.comments.export import export_row, iter_jsonl, render_csv, render_txt
from live_recorder.config import Settings, load_settings
from live_recorder.db.models import Streamer, StreamSession
from live_recorder.db.repository import Repository, RepositoryError
from live_recorder.db.runtime import (
    SettingsValidationError,
    ensure_runtime,
    load_runtime,
    save_runtime,
    validate_runtime,
)
from live_recorder.db.session import Database
from live_recorder.domain.enums import Platform
from live_recorder.domain.labels import (
    COMMENT_STATUS_JA,
    LIVE_STATUS_JA,
    PLATFORM_JA,
    RECORDING_STATUS_JA,
    SESSION_STATUS_JA,
)
from live_recorder.platforms.normalize import (
    NormalizeError,
    clean_display_name,
    normalize_tiktok,
    normalize_twitcasting,
    tiktok_profile_url,
    twitcasting_profile_url,
)
from live_recorder.recording.files import PathEscape, resolve_stored
from live_recorder.recording.integrity import scan_storage
from live_recorder.recording.process import command_output
from live_recorder.timeutil import (
    ensure_aware,
    format_local,
    format_offset,
    start_of_local_day,
    utcnow,
)
from live_recorder.web import ui as ui_helpers

BASE_DIR = Path(__file__).resolve().parent
WORKER_OK_SECONDS = 45
WORKER_STALE_SECONDS = 300
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
ui_helpers.register(templates, __version__)
templates.env.filters["localtime"] = lambda value, tz: format_local(value, tz)
templates.env.filters["offset"] = format_offset
templates.env.filters["bytes"] = lambda value: format_bytes(value)
templates.env.filters["live_label"] = lambda value: LIVE_STATUS_JA.get(value or "", value or "—")
templates.env.filters["rec_label"] = lambda value: RECORDING_STATUS_JA.get(value or "NONE", value or "—")
templates.env.filters["com_label"] = lambda value: COMMENT_STATUS_JA.get(value or "NONE", value or "—")
templates.env.filters["session_label"] = lambda value: SESSION_STATUS_JA.get(value or "", value or "—")
templates.env.filters["platform_label"] = lambda value: PLATFORM_JA.get(value or "", value or "—")


def format_bytes(size: object) -> str:
    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.db = Database(settings)
        app.state.login_failures = {}
        async with app.state.db.session() as session:
            await ensure_runtime(session)
            await session.commit()
        yield
        await app.state.db.dispose()

    app = FastAPI(title="ライブ配信録画", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="lr_session",
        max_age=60 * 60 * 12,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'"
        return response

    @app.get("/healthz")
    async def healthz():
        try:
            async with app.state.db.session() as session:
                from sqlalchemy import text

                await session.execute(text("SELECT 1"))
            return {"status": "ok"}
        except Exception:
            return Response('{"status":"error"}', status_code=503, media_type="application/json")

    def csrf(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(32)
            request.session["csrf"] = token
        return str(token)

    def check_csrf(request: Request, form: dict) -> None:
        provided = str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
        expected = str(request.session.get("csrf") or "")
        if not expected or not provided or not secrets.compare_digest(expected, provided):
            raise HTTPException(status_code=403, detail="CSRF")

    async def runtime_tz(request: Request) -> str:
        async with request.app.state.db.session() as session:
            runtime = await load_runtime(session)
            return runtime.display_timezone

    async def render(request: Request, name: str, **extra):
        status_code = int(extra.pop("status_code", 200))
        tz = extra.pop("tz", None)
        if tz is None:
            try:
                tz = await runtime_tz(request)
            except Exception:
                tz = request.app.state.settings.display_timezone
        context = {
            "csrf": csrf(request),
            "flash": _flash_payload(request.session.pop("flash", None)),
            "tz": tz,
            "username": request.session.get("username"),
            "rendered_at": utcnow(),
            **extra,
        }
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    async def require_user(request: Request) -> RedirectResponse | None:
        uid = request.session.get("uid")
        if not uid:
            return RedirectResponse("/login", status_code=303)
        async with request.app.state.db.session() as session:
            user = await Repository(session).user_by_id(int(uid))
        if user is None or not user.is_active:
            request.session.clear()
            return RedirectResponse("/login", status_code=303)
        return None

    def flash(request: Request, message: str, tone: str = "success") -> None:
        request.session["flash"] = {"message": message, "tone": tone}

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        if request.session.get("uid"):
            return RedirectResponse("/", status_code=303)
        return await render(request, "login.html", error="")

    @app.post("/login")
    async def login_submit(request: Request):
        form = dict(await request.form())
        check_csrf(request, form)
        client = request.client.host if request.client else "local"
        failures: list[float] = request.app.state.login_failures.setdefault(client, [])
        now = utcnow().timestamp()
        failures[:] = [item for item in failures if now - item < 300]
        if len(failures) >= 5:
            return await render(request, "login.html", error="しばらく待ってから再度ログインしてください", status_code=429)
        username = str(form.get("username") or "").strip()
        password = str(form.get("password") or "")
        async with request.app.state.db.session() as session:
            user = await Repository(session).user_by_name(username)
        if user is None or not user.is_active or not verify_password(password, user.password_hash):
            failures.append(now)
            return await render(request, "login.html", error="ユーザー名またはパスワードが違います", status_code=401)
        request.session["uid"] = user.id
        request.session["username"] = user.username
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        form = dict(await request.form())
        check_csrf(request, form)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        blocked = await require_user(request)
        if blocked:
            return blocked
        async with request.app.state.db.session() as session:
            repo = Repository(session)
            runtime = await load_runtime(session)
            stats = await repo.dashboard(start_of_local_day(runtime.display_timezone))
            recent = await repo.recent_sessions(8)
            errors = await repo.recent_errors(8)
            by_streamer = await repo.bytes_by_streamer()
            cards = []
            for item in recent:
                job = await repo.latest_recording(item.id)
                cards.append(
                    {
                        "id": item.id,
                        "name": item.streamer.display_name,
                        "platform": item.streamer.platform,
                        "title": item.title or "—",
                        "detected_at": item.detected_at,
                        "status": job.status if job else "NONE",
                    }
                )
            since = utcnow() - timedelta(hours=24)
            error_rows = [
                {"created_at": event.created_at, "message": event.message, "severity": event.severity}
                for event in errors
                if ensure_aware(event.created_at) >= since
            ]
            active = await _active_sessions(session)
            worker = _worker_health(await repo.latest_heartbeat())
            last_checked = await _last_checked(repo)
            monitored = len(await repo.monitored_streamers())
        free, total = _disk_usage(request.app.state.settings.recordings_dir)
        return await render(
            request,
            "dashboard.html",
            tz=runtime.display_timezone,
            stats=stats,
            cards=cards,
            errors=error_rows,
            free=free,
            total=total,
            used=(total - free) if free is not None and total is not None else None,
            min_free=runtime.min_free_bytes,
            by_streamer=by_streamer,
            active=active,
            worker=worker,
            last_checked=last_checked,
            monitored=monitored,
            warning="",
        )

    @app.get("/partials/active-sessions", response_class=HTMLResponse)
    async def active_sessions_partial(request: Request):
        blocked = await require_user(request)
        if blocked:
            return blocked
        async with request.app.state.db.session() as session:
            runtime = await load_runtime(session)
            active = await _active_sessions(session)
            worker = _worker_health(await Repository(session).latest_heartbeat())
        return await render(
            request,
            "partials/active_sessions.html",
            tz=runtime.display_timezone,
            active=active,
            worker=worker,
        )

    @app.get("/streamers", response_class=HTMLResponse)
    async def streamers_page(request: Request):
        blocked = await require_user(request)
        if blocked:
            return blocked
        rows = await _streamer_rows(request)
        return await render(request, "streamers.html", rows=rows, error="")

    @app.post("/streamers")
    async def streamers_create(request: Request):
        blocked = await require_user(request)
        if blocked:
            return blocked
        form = dict(await request.form())
        check_csrf(request, form)
        submitted = _submitted_streamer(form)
        try:
            platform, user_id, profile = _parse_account(str(form.get("platform") or ""), str(form.get("account") or ""))
        except NormalizeError as exc:
            rows = await _streamer_rows(request)
            return await render(
                request, "streamers.html", rows=rows, error=str(exc), add_form=submitted, status_code=400
            )
        async with request.app.state.db.session() as session:
            repo = Repository(session)
            if await repo.find_streamer(platform, user_id):
                rows = await _streamer_rows(request)
                return await render(
                    request,
                    "streamers.html",
                    rows=rows,
                    error="同じ配信者は登録済みです",
                    add_form=submitted,
                    status_code=400,
                )
            streamer = Streamer(
                platform=platform,
                platform_user_id=user_id,
                display_name=clean_display_name(str(form.get("display_name") or ""), user_id),
                profile_url=profile,
                monitoring_enabled=_checked(form, "monitoring_enabled"),
                recording_enabled=_checked(form, "recording_enabled"),
                comments_enabled=_checked(form, "comments_enabled"),
                live_status="OFFLINE",
            )
            await repo.add_streamer(streamer)
            await repo.add_event("streamer_added", f"{platform}:{user_id} を登録")
            await session.commit()
        flash(request, "配信者を追加しました")
        return RedirectResponse("/streamers", status_code=303)

    @app.get("/streamers/{streamer_id}/edit", response_class=HTMLResponse)
    async def streamer_edit(request: Request, streamer_id: int):
        blocked = await require_user(request)
        if blocked:
            return blocked
        async with request.app.state.db.session() as session:
            streamer = await Repository(session).get_streamer(streamer_id)
            if streamer is None:
                raise HTTPException(404)
            row = _streamer_dict(streamer)
        return await render(request, "streamer_form.html", row=row, error="")

    @app.post("/streamers/{streamer_id}/edit")
    async def streamer_update(request: Request, streamer_id: int):
        blocked = await require_user(request)
        if blocked:
            return blocked
        form = dict(await request.form())
        check_csrf(request, form)
        async with request.app.state.db.session() as session:
            streamer = await Repository(session).get_streamer(streamer_id)
            if streamer is None:
                raise HTTPException(404)
            streamer.display_name = clean_display_name(str(form.get("display_name") or ""), streamer.platform_user_id)
            streamer.monitoring_enabled = _checked(form, "monitoring_enabled")
            streamer.recording_enabled = _checked(form, "recording_enabled")
            streamer.comments_enabled = _checked(form, "comments_enabled")
            if not streamer.monitoring_enabled:
                streamer.live_status = "DISABLED"
            await session.commit()
        flash(request, "配信者を更新しました")
        return RedirectResponse("/streamers", status_code=303)

    @app.post("/streamers/{streamer_id}/toggle", response_class=HTMLResponse)
    async def streamer_toggle(request: Request, streamer_id: int):
        blocked = await require_user(request)
        if blocked:
            return blocked
        form = dict(await request.form())
        check_csrf(request, form)
        field = str(form.get("field") or "")
        if field not in {"monitoring_enabled", "recording_enabled", "comments_enabled"}:
            raise HTTPException(400)
        async with request.app.state.db.session() as session:
            streamer = await Repository(session).get_streamer(streamer_id)
            if streamer is None:
                raise HTTPException(404)
            setattr(streamer, field, not getattr(streamer, field))
            if field == "monitoring_enabled" and not streamer.monitoring_enabled:
                streamer.live_status = "DISABLED"
            elif field == "monitoring_enabled" and streamer.live_status == "DISABLED":
                streamer.live_status = "OFFLINE"
            await session.commit()
            row = await _one_streamer_row(session, streamer)
        if request.headers.get("hx-request"):
            return await render(request, "streamer_row.html", row=row)
        return RedirectResponse("/streamers", status_code=303)

    @app.post("/streamers/{streamer_id}/delete")
    async def streamer_delete(request: Request, streamer_id: int):
        blocked = await require_user(request)
        if blocked:
            return blocked
        form = dict(await request.form())
        check_csrf(request, form)
        if form.get("confirm") != "yes":
            flash(request, "削除確認が必要です", "warning")
            return RedirectResponse("/streamers", status_code=303)
        async with request.app.state.db.session() as session:
            try:
                await Repository(session).delete_streamer(streamer_id)
            except RepositoryError as exc:
                flash(request, str(exc), "danger")
                return RedirectResponse("/streamers", status_code=303)
            await session.commit()
        flash(request, "配信者を削除しました")
        return RedirectResponse("/streamers", status_code=303)

    @app.get("/recordings", response_class=HTMLResponse)
    async def recordings_page(request: Request):
        blocked = await require_user(request)
        if blocked:
            return blocked
        tz = await runtime_tz(request)
        filters = _recording_filters(request, tz)
        async with request.app.state.db.session() as session:
            repo = Repository(session)
            rows, total = await repo.search_sessions(page=filters["page"], page_size=20, **filters["query"])
            streamers = await repo.list_streamers()
            cards = []
            for item in rows:
                segments = await repo.segments_for_session(item.id)
                job = await repo.latest_recording(item.id)
                visible = [segment for segment in segments if not segment.file_removed]
                cards.append(
                    {
                        "id": item.id,
                        "name": item.streamer.display_name,
                        "platform": item.streamer.platform,
                        "title": item.title or "—",
                        "started": visible[0].started_at if visible else item.detected_at,
                        "finished": visible[-1].finished_at if visible else item.ended_detected_at,
                        "duration_ms": sum(segment.duration_ms or 0 for segment in visible) or None,
                        "files": len(visible),
                        "size": sum(segment.file_size or 0 for segment in visible),
                        "comments": await repo.comment_count(item.id),
                        "status": job.status if job else "NONE",
                    }
                )
            people = [{"id": person.id, "name": person.display_name, "platform": person.platform} for person in streamers]
        return await render(
            request,
            "recordings.html",
            tz=tz,
            cards=cards,
            total=total,
            page=filters["page"],
            pages=max(1, (total + 19) // 20),
            filters=filters["raw"],
            streamers=people,
        )

    @app.get("/recordings/{session_id}", response_class=HTMLResponse)
    async def recording_detail(request: Request, session_id: int):
        blocked = await require_user(request)
        if blocked:
            return blocked
        async with request.app.state.db.session() as session:
            repo = Repository(session)
            item = await repo.get_session(session_id)
            if item is None:
                raise HTTPException(404)
            segments = await repo.segments_for_session(session_id)
            job = await repo.latest_recording(session_id)
            comment_job = await repo.latest_comment_job(session_id)
            gaps = await repo.gaps_for_session(session_id)
            comments_total = await repo.comment_count(session_id)
            root = request.app.state.settings.recordings_dir
            detail = {
                "id": item.id,
                "name": item.streamer.display_name,
                "platform": item.streamer.platform,
                "user_id": item.streamer.platform_user_id,
                "title": item.title or "—",
                "stream_id": item.platform_stream_id or "—",
                "status": item.status,
                "detected_at": item.detected_at,
                "platform_started_at": item.platform_started_at,
                "ended_detected_at": item.ended_detected_at,
                "recording_status": job.status if job else "NONE",
                "engine": job.recorder_engine if job else "—",
                "recording_error": job.last_error if job else "",
                "comment_status": comment_job.status if comment_job else "NONE",
                "comment_received": comment_job.received_count if comment_job else 0,
                "comment_stored": comment_job.stored_count if comment_job else 0,
                "comment_errors": comment_job.error_count if comment_job else 0,
                "comment_reconnects": comment_job.reconnect_count if comment_job else 0,
                "comment_error": comment_job.last_error if comment_job else "",
                "comment_last_received_at": comment_job.last_received_at if comment_job else None,
                "comments_total": comments_total,
                "recording_started_at": job.started_at if job else None,
                "recording_finished_at": job.finished_at if job else None,
                "recording_retries": job.retry_count if job else 0,
                "segments": [
                    {
                        "id": segment.id,
                        "number": segment.segment_number,
                        "started_at": segment.started_at,
                        "finished_at": segment.finished_at,
                        "size": segment.file_size,
                        "duration_ms": segment.duration_ms,
                        "container": segment.container_format or "—",
                        "validation": segment.validation_status,
                        "removed": segment.file_removed,
                        "exists": _stored_file_exists(root, segment.file_path) if not segment.file_removed else False,
                        "name": Path(segment.file_path).name,
                    }
                    for segment in segments
                ],
                "gaps": [
                    {"job_type": gap.job_type, "started_at": gap.started_at, "ended_at": gap.ended_at, "reason": gap.reason}
                    for gap in gaps
                ],
            }
        return await render(request, "recording_detail.html", detail=detail)

    @app.get("/recordings/segments/{segment_id}/download")
    async def download_segment(request: Request, segment_id: int):
        blocked = await require_user(request)
        if blocked:
            return blocked
        async with request.app.state.db.session() as session:
            segment = await Repository(session).get_segment(segment_id)
            if segment is None or segment.file_removed:
                raise HTTPException(404)
            relative = segment.file_path
            filename = Path(relative).name
        try:
            path = resolve_stored(request.app.state.settings.recordings_dir, relative)
        except PathEscape as exc:
            raise HTTPException(404) from exc
        if not path.is_file():
            raise HTTPException(404)

        def chunks():
            with path.open("rb") as handle:
                while True:
                    block = handle.read(1024 * 1024)
                    if not block:
                        break
                    yield block

        return StreamingResponse(
            chunks(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(path.stat().st_size),
                "Accept-Ranges": "none",
                "Cache-Control": "no-store",
            },
        )

    @app.post("/recordings/{session_id}/delete-files")
    async def delete_files(request: Request, session_id: int):
        blocked = await require_user(request)
        if blocked:
            return blocked
        form = dict(await request.form())
        check_csrf(request, form)
        if form.get("confirm") != "yes":
            flash(request, "ファイル削除の確認が必要です", "warning")
            return RedirectResponse(f"/recordings/{session_id}", status_code=303)
        async with request.app.state.db.session() as session:
            repo = Repository(session)
            for segment in await repo.segments_for_session(session_id):
                if segment.file_removed:
                    continue
                try:
                    path = resolve_stored(request.app.state.settings.recordings_dir, segment.file_path)
                except PathEscape:
                    continue
                if path.is_file():
                    path.unlink()
                segment.file_removed = True
            await repo.add_event("file_deleted", "録画ファイルを削除しました", session_id=session_id)
            await session.commit()
        flash(request, "録画ファイルを削除しました。履歴は残しています")
        return RedirectResponse(f"/recordings/{session_id}", status_code=303)

    @app.post("/recordings/{session_id}/delete")
    async def delete_history(request: Request, session_id: int):
        blocked = await require_user(request)
        if blocked:
            return blocked
        form = dict(await request.form())
        check_csrf(request, form)
        if form.get("confirm") != "yes":
            flash(request, "履歴削除の確認が必要です", "warning")
            return RedirectResponse(f"/recordings/{session_id}", status_code=303)
        delete_files_too = form.get("delete_files") == "yes"
        async with request.app.state.db.session() as session:
            repo = Repository(session)
            item = await repo.get_session(session_id)
            if item is None:
                raise HTTPException(404)
            segments = await repo.segments_for_session(session_id)
            comment_job = await repo.latest_comment_job(session_id)
            export_path = comment_job.export_path if comment_job else None
            if delete_files_too:
                for segment in segments:
                    try:
                        path = resolve_stored(request.app.state.settings.recordings_dir, segment.file_path)
                    except PathEscape:
                        continue
                    if path.is_file():
                        path.unlink()
            if export_path:
                try:
                    path = resolve_stored(request.app.state.settings.recordings_dir, export_path)
                    if path.is_file():
                        path.unlink()
                except PathEscape:
                    pass
            await repo.delete_session(session_id)
            await session.commit()
        flash(request, "履歴を削除しました")
        return RedirectResponse("/recordings", status_code=303)

    @app.get("/comments", response_class=HTMLResponse)
    async def comments_page(request: Request):
        blocked = await require_user(request)
        if blocked:
            return blocked
        tz = await runtime_tz(request)
        filters = _comment_filters(request, tz)
        async with request.app.state.db.session() as session:
            repo = Repository(session)
            rows, total = await repo.search_comments(page=filters["page"], page_size=50, **filters["query"])
            people = await repo.list_streamers()
            comments = [
                {
                    "when": comment.posted_at or comment.received_at,
                    "streamer": comment.session.streamer.display_name if comment.session and comment.session.streamer else "—",
                    "platform": comment.session.streamer.platform if comment.session and comment.session.streamer else "",
                    "user_id": comment.platform_user_id or "—",
                    "nickname": comment.nickname or "—",
                    "message": comment.message,
                    "offset": comment.recording_offset_ms,
                    "session_id": comment.stream_session_id,
                }
                for comment in rows
            ]
            streamers = [{"id": person.id, "name": person.display_name} for person in people]
        return await render(
            request,
            "comments.html",
            tz=tz,
            comments=comments,
            total=total,
            page=filters["page"],
            pages=max(1, (total + 49) // 50),
            filters=filters["raw"],
            streamers=streamers,
            query=request.url.query,
        )

    @app.get("/comments/export")
    async def comments_export(request: Request):
        blocked = await require_user(request)
        if blocked:
            return blocked
        tz = await runtime_tz(request)
        filters = _comment_filters(request, tz)
        kind = str(request.query_params.get("format") or "jsonl")
        if kind not in {"jsonl", "csv", "txt"}:
            raise HTTPException(400)
        async with request.app.state.db.session() as session:
            repo = Repository(session)
            found = await repo.export_comments(**filters["query"])
            rows = []
            for comment in found:
                stream = comment.session
                rows.append(
                    export_row(
                        posted_at=comment.posted_at,
                        received_at=comment.received_at,
                        timezone_name=tz,
                        user_id=comment.platform_user_id,
                        nickname=comment.nickname,
                        message=comment.message,
                        platform=stream.streamer.platform if stream and stream.streamer else "",
                        stream_id=stream.platform_stream_id if stream else None,
                        recording_offset_ms=comment.recording_offset_ms,
                        stream_offset_ms=comment.stream_offset_ms,
                        platform_comment_id=comment.platform_comment_id,
                    )
                )
        if kind == "csv":
            body = "\ufeff" + render_csv(rows)
            media = "text/csv; charset=utf-8"
            filename = "comments.csv"
        elif kind == "txt":
            body = render_txt(rows)
            media = "text/plain; charset=utf-8"
            filename = "comments.txt"
        else:
            body = "".join(iter_jsonl(rows))
            media = "application/x-ndjson; charset=utf-8"
            filename = "comments.jsonl"
        return Response(
            content=body.encode("utf-8"),
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        blocked = await require_user(request)
        if blocked:
            return blocked
        async with request.app.state.db.session() as session:
            runtime = await load_runtime(session)
        env = request.app.state.settings
        return await render(
            request,
            "settings.html",
            runtime=runtime,
            error="",
            env={
                "recordings_dir": str(env.recordings_dir),
                "web_port": env.web_port,
                "twitcasting": env.twitcasting_configured,
                "sign_key": bool(env.sign_api_key),
                "cookie_secure": env.cookie_secure,
            },
        )

    @app.post("/settings")
    async def settings_save(request: Request):
        blocked = await require_user(request)
        if blocked:
            return blocked
        form = dict(await request.form())
        check_csrf(request, form)
        order = [str(form.get(key) or "") for key in ("engine_1", "engine_2", "engine_3")]
        order = [item for item in order if item]
        try:
            runtime = validate_runtime(
                {
                    "monitor_interval_seconds": form.get("monitor_interval_seconds"),
                    "max_concurrent_recordings": form.get("max_concurrent_recordings"),
                    "recording_quality": form.get("recording_quality"),
                    "min_free_bytes": int(float(str(form.get("min_free_gib") or "0")) * 1024**3),
                    "retention_days": form.get("retention_days"),
                    "auto_delete_enabled": _checked(form, "auto_delete_enabled"),
                    "tiktok_engine_order": order,
                    "comments_globally_enabled": _checked(form, "comments_globally_enabled"),
                    "log_level": form.get("log_level"),
                    "display_timezone": form.get("display_timezone"),
                    "comment_retention_days": form.get("comment_retention_days"),
                    "auto_export_on_complete": _checked(form, "auto_export_on_complete"),
                    "reconnect_max_attempts": form.get("reconnect_max_attempts"),
                    "comment_poll_seconds": form.get("comment_poll_seconds"),
                }
            )
        except (SettingsValidationError, ValueError) as exc:
            env = request.app.state.settings
            return await render(
                request,
                "settings.html",
                runtime=_fallback_runtime(form),
                error=str(exc),
                env={
                    "recordings_dir": str(env.recordings_dir),
                    "web_port": env.web_port,
                    "twitcasting": env.twitcasting_configured,
                    "sign_key": bool(env.sign_api_key),
                    "cookie_secure": env.cookie_secure,
                },
                status_code=400,
            )
        async with request.app.state.db.session() as session:
            await save_runtime(session, runtime)
            await Repository(session).add_event("settings_updated", "即時反映の設定を更新しました")
            await session.commit()
        flash(request, "設定を保存しました。保存先と秘密情報の変更は環境変数を更新して再起動してください")
        return RedirectResponse("/settings", status_code=303)

    @app.get("/system", response_class=HTMLResponse)
    async def system_page(request: Request):
        blocked = await require_user(request)
        if blocked:
            return blocked
        payload = await _system_payload(request, None)
        return await render(request, "system.html", **payload)

    @app.post("/system/scan")
    async def system_scan(request: Request):
        blocked = await require_user(request)
        if blocked:
            return blocked
        form = dict(await request.form())
        check_csrf(request, form)
        async with request.app.state.db.session() as session:
            segments = await Repository(session).stored_files()
            payload_segments = [
                {
                    "file_path": segment.file_path,
                    "file_size": segment.file_size,
                    "validation": segment.validation_status,
                    "finished": segment.finished_at is not None,
                    "file_removed": segment.file_removed,
                }
                for segment in segments
            ]
        report = scan_storage(request.app.state.settings.recordings_dir, payload_segments)
        payload = await _system_payload(request, report)
        problems = set(report.missing) | set(report.size_mismatch) | set(report.suspect)
        checked = [item for item in payload_segments if not item["file_removed"]]
        payload["scan"] = {
            "checked": len(checked),
            "ok": sum(1 for item in checked if item["file_path"] not in problems),
        }
        return await render(request, "system.html", **payload)

    return app


def _checked(form: dict, name: str) -> bool:
    return str(form.get(name) or "").lower() in {"1", "true", "on", "yes"}


def _flash_payload(value: object) -> dict | None:
    if not value:
        return None
    if isinstance(value, dict):
        return {"message": str(value.get("message") or ""), "tone": str(value.get("tone") or "success")}
    return {"message": str(value), "tone": "success"}


def _submitted_streamer(form: dict) -> dict:
    return {
        "platform": str(form.get("platform") or ""),
        "account": str(form.get("account") or ""),
        "display_name": str(form.get("display_name") or ""),
        "monitoring_enabled": _checked(form, "monitoring_enabled"),
        "recording_enabled": _checked(form, "recording_enabled"),
        "comments_enabled": _checked(form, "comments_enabled"),
    }


def _stored_file_exists(root: Path, relative: str) -> bool:
    try:
        return resolve_stored(root, relative).is_file()
    except (PathEscape, OSError):
        return False


def _disk_usage(root: Path) -> tuple[int | None, int | None]:
    from live_recorder.recording.finalize import disk_free, disk_total

    try:
        return disk_free(root), disk_total(root)
    except OSError:
        return None, None


def _worker_health(heartbeat) -> dict:
    """ハートビートの鮮度からワーカー状態を 正常 / 警告 / 異常 / 不明 に分類する。"""
    if heartbeat is None:
        return {"state": "unknown", "age": None, "status": None, "last_seen_at": None, "label": "未検出"}
    age = int((utcnow() - ensure_aware(heartbeat.last_seen_at)).total_seconds())
    if age <= WORKER_OK_SECONDS and heartbeat.status == "running":
        state, label = "ok", "稼働中"
    elif age <= WORKER_STALE_SECONDS:
        state, label = "warning", "応答が遅れています" if heartbeat.status == "running" else str(heartbeat.status)
    else:
        state, label = "error", "停止または無応答"
    return {"state": state, "age": age, "status": heartbeat.status, "last_seen_at": heartbeat.last_seen_at, "label": label}


async def _last_checked(repo: Repository) -> datetime | None:
    times = [item.last_checked_at for item in await repo.monitored_streamers() if item.last_checked_at]
    return max(times) if times else None


async def _active_sessions(session) -> list[dict]:
    """配信中または終了処理中のセッションと、その録画・コメント収集ジョブの状態。"""
    repo = Repository(session)
    query = repo.session_query().where(StreamSession.status.in_(("LIVE", "ENDING")))
    rows = []
    for item in await session.scalars(query):
        recording = await repo.latest_recording(item.id)
        comments = await repo.latest_comment_job(item.id)
        rows.append(
            {
                "id": item.id,
                "name": item.streamer.display_name,
                "platform": item.streamer.platform,
                "user_id": item.streamer.platform_user_id,
                "title": item.title or "",
                "session_status": item.status,
                "detected_at": item.detected_at,
                "recording_status": recording.status if recording else "NONE",
                "recording_started_at": recording.started_at if recording else None,
                "recording_error": recording.last_error if recording else "",
                "comment_status": comments.status if comments else "NONE",
                "comment_received": comments.received_count if comments else 0,
                "comment_last_received_at": comments.last_received_at if comments else None,
            }
        )
    return rows


def _parse_account(platform: str, account: str) -> tuple[str, str, str]:
    if platform == Platform.TIKTOK:
        user_id = normalize_tiktok(account)
        return platform, user_id, tiktok_profile_url(user_id)
    if platform == Platform.TWITCASTING:
        user_id = normalize_twitcasting(account)
        return platform, user_id, twitcasting_profile_url(user_id)
    raise NormalizeError("プラットフォームを選択してください")


def _streamer_dict(streamer: Streamer) -> dict:
    return {
        "id": streamer.id,
        "platform": streamer.platform,
        "user_id": streamer.platform_user_id,
        "display_name": streamer.display_name,
        "profile_url": streamer.profile_url,
        "monitoring_enabled": streamer.monitoring_enabled,
        "recording_enabled": streamer.recording_enabled,
        "comments_enabled": streamer.comments_enabled,
        "live_status": streamer.live_status,
        "last_checked_at": streamer.last_checked_at,
        "last_live_at": streamer.last_live_at,
        "last_recording_at": streamer.last_recording_at,
        "last_error": streamer.last_error or "",
        "recording_state": "NONE",
        "comment_state": "NONE",
    }


async def _one_streamer_row(session, streamer: Streamer) -> dict:
    repo = Repository(session)
    row = _streamer_dict(streamer)
    open_session = await repo.open_session(streamer.id)
    if open_session is not None:
        recording = await repo.active_recording(open_session.id)
        comments = await repo.active_comments(open_session.id)
        row["recording_state"] = recording.status if recording else "NONE"
        row["comment_state"] = comments.status if comments else "NONE"
    return row


async def _streamer_rows(request: Request) -> list[dict]:
    async with request.app.state.db.session() as session:
        repo = Repository(session)
        rows = []
        for streamer in await repo.list_streamers():
            rows.append(await _one_streamer_row(session, streamer))
        return rows


def _parse_day(value: str, tz_name: str, *, end: bool) -> datetime | None:
    if not value:
        return None
    try:
        day = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    local = day.replace(tzinfo=ZoneInfo(tz_name))
    if end:
        local = local + timedelta(days=1)
    return local.astimezone(timezone.utc)


def _recording_filters(request: Request, tz: str) -> dict:
    params = request.query_params
    page = _page(params.get("page"))
    streamer_raw = params.get("streamer_id") or ""
    query = {
        "streamer_id": int(streamer_raw) if streamer_raw.isdigit() else None,
        "platform": params.get("platform") or None,
        "status": params.get("status") or None,
        "start": _parse_day(params.get("start") or "", tz, end=False),
        "end": _parse_day(params.get("end") or "", tz, end=True),
    }
    return {"page": page, "query": query, "raw": {key: params.get(key) or "" for key in ("streamer_id", "platform", "status", "start", "end")}}


def _comment_filters(request: Request, tz: str) -> dict:
    params = request.query_params
    streamer_raw = params.get("streamer_id") or ""
    session_raw = params.get("session_id") or ""
    query = {
        "text": params.get("q") or None,
        "user_id": params.get("user_id") or None,
        "nickname": params.get("nickname") or None,
        "streamer_id": int(streamer_raw) if streamer_raw.isdigit() else None,
        "platform": params.get("platform") or None,
        "session_id": int(session_raw) if session_raw.isdigit() else None,
        "start": _parse_day(params.get("start") or "", tz, end=False),
        "end": _parse_day(params.get("end") or "", tz, end=True),
    }
    return {
        "page": _page(params.get("page")),
        "query": query,
        "raw": {key: params.get(key) or "" for key in ("q", "user_id", "nickname", "streamer_id", "platform", "session_id", "start", "end")},
    }


def _page(value: str | None) -> int:
    if value and value.isdigit():
        return max(1, int(value))
    return 1


def _fallback_runtime(form: dict):
    """検証エラーで再表示するとき、送信された値をそのままフォームへ戻す（既定値で上書きしない）。"""
    from types import SimpleNamespace

    from live_recorder.db.runtime import RuntimeSettings

    defaults = RuntimeSettings()
    try:
        min_free_bytes = int(float(str(form.get("min_free_gib") or "0")) * 1024**3)
    except ValueError:
        min_free_bytes = defaults.min_free_bytes
    order = [str(form.get(key) or "") for key in ("engine_1", "engine_2", "engine_3")]
    return SimpleNamespace(
        monitor_interval_seconds=form.get("monitor_interval_seconds", defaults.monitor_interval_seconds),
        max_concurrent_recordings=form.get("max_concurrent_recordings", defaults.max_concurrent_recordings),
        recording_quality=str(form.get("recording_quality") or defaults.recording_quality),
        min_free_bytes=min_free_bytes,
        retention_days=form.get("retention_days", defaults.retention_days),
        auto_delete_enabled=_checked(form, "auto_delete_enabled"),
        tiktok_engine_order=order,
        comments_globally_enabled=_checked(form, "comments_globally_enabled"),
        log_level=str(form.get("log_level") or defaults.log_level),
        display_timezone=str(form.get("display_timezone") or ""),
        comment_retention_days=form.get("comment_retention_days", defaults.comment_retention_days),
        auto_export_on_complete=_checked(form, "auto_export_on_complete"),
        reconnect_max_attempts=form.get("reconnect_max_attempts", defaults.reconnect_max_attempts),
        comment_poll_seconds=form.get("comment_poll_seconds", defaults.comment_poll_seconds),
    )


async def _system_payload(request: Request, report) -> dict:
    settings: Settings = request.app.state.settings
    async with request.app.state.db.session() as session:
        repo = Repository(session)
        runtime = await load_runtime(session)
        heartbeat = await repo.latest_heartbeat()
        engines = await repo.list_engines()
        events = await repo.recent_events(50)
        checked = None
        streamers = await repo.monitored_streamers()
        worker = _worker_health(heartbeat)
        if streamers:
            times = [item.last_checked_at for item in streamers if item.last_checked_at]
            checked = max(times) if times else None
        engine_rows = [
            {
                "name": engine.name,
                "version": engine.version or "—",
                "available": engine.available,
                "last_success_at": engine.last_success_at,
                "last_failure_at": engine.last_failure_at,
                "failure_reason": engine.failure_reason or "",
                "retry_count": engine.retry_count,
            }
            for engine in engines
        ]
        event_rows = [{"created_at": event.created_at, "severity": event.severity, "message": event.message} for event in events]
    versions = {
        "app": __version__,
        "streamlink": await command_output([settings.streamlink_path, "--version"]),
        "yt_dlp": await command_output([settings.ytdlp_path, "--version"]),
        "ffmpeg": await command_output([settings.ffmpeg_path, "-version"]),
        "ffprobe": await command_output([settings.ffprobe_path, "-version"]),
    }
    try:
        import TikTokLive

        versions["tiktoklive"] = str(getattr(TikTokLive, "__version__", "installed"))
    except Exception:
        versions["tiktoklive"] = ""
    return {
        "tz": runtime.display_timezone,
        "versions": versions,
        "engines": engine_rows,
        "events": event_rows,
        "worker": worker,
        "worker_ok": worker["state"] == "ok",
        "worker_age": worker["age"],
        "db_ok": True,
        "last_checked": checked,
        "monitor_interval": runtime.monitor_interval_seconds,
        "monitored": len(streamers),
        "report": report,
        "scan": None,
        "sign_key": bool(settings.sign_api_key),
        "twitcasting": settings.twitcasting_configured,
    }
