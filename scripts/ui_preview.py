"""データベースなしで UI を確認するためのプレビューサーバー（開発専用）。

    uv run python scripts/ui_preview.py [port]

各ページに ?scenario=normal|empty|stress を付けると、空データ・大量データ・長文・エラーの状態を表示する。
本番と同じ Content-Security-Policy を付けるので、CSP 違反もブラウザで確認できる。
"""

from __future__ import annotations

import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import ui_fixtures as fixtures  # noqa: E402
from live_recorder.web.app import BASE_DIR, templates  # noqa: E402

app = FastAPI(docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'"
    )
    return response


def _scenario(request: Request) -> str:
    value = request.query_params.get("scenario", "normal")
    return value if value in fixtures.SCENARIOS else "normal"


def _query(request: Request) -> str:
    return "&".join(
        f"{k}={v}" for k, v in request.query_params.multi_items() if k not in {"scenario", "flash"}
    )


def _render(request: Request, name: str, context: dict, status_code: int = 200) -> HTMLResponse:
    context["request"] = fixtures.make_request(request.url.path, str(request.url.query))
    flash = request.query_params.get("flash")
    if flash:
        context["flash"] = {"message": "プレビュー用の通知です（" + flash + "）", "tone": flash}
    return templates.TemplateResponse(request, name, context, status_code=status_code)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return _render(request, "dashboard.html", fixtures.dashboard_context(_scenario(request)))


@app.get("/partials/active-sessions", response_class=HTMLResponse)
async def active(request: Request):
    return _render(
        request, "partials/active_sessions.html", fixtures.dashboard_context(_scenario(request))
    )


@app.get("/streamers", response_class=HTMLResponse)
async def streamers(request: Request):
    context = fixtures.streamers_context(_scenario(request))
    if request.query_params.get("add_error"):
        context["error"] = "TikTok ユーザーIDの形式が不正です"
        context["add_form"] = {
            "platform": "tiktok",
            "account": "bad id!",
            "display_name": "",
            "monitoring_enabled": True,
            "recording_enabled": False,
            "comments_enabled": True,
        }
    context["order"] = "custom" if request.query_params.get("order") == "custom" else "default"
    if context["order"] == "default":
        context["rows"] = sorted(
            context["rows"], key=lambda row: (row["platform"], row["display_name"], row["id"])
        )
    return _render(request, "streamers.html", context)


@app.post("/streamers/order")
async def streamer_order(request: Request):
    form = await request.form()
    mode = "custom" if str(form.get("order") or "") == "custom" else "default"
    return RedirectResponse(f"/streamers?order={mode}", status_code=303)


@app.post("/streamers/reorder")
async def reorder():
    return Response(status_code=204)


@app.post("/streamers/{streamer_id}/toggle", response_class=HTMLResponse)
async def toggle(request: Request, streamer_id: int):
    form = await request.form()
    if _scenario(request) == "stress" or streamer_id == 4:
        return HTMLResponse("error", status_code=500)
    row = next(item for item in fixtures.streamer_rows("stress") if item["id"] == streamer_id)
    field = str(form.get("field"))
    row[field] = not row[field]
    referer = request.headers.get("referer") or ""
    order = "custom" if "order=custom" in referer else "default"
    return _render(request, "streamer_row.html", fixtures.base_context(row=row, order=order))


@app.get("/streamers/{streamer_id}/edit", response_class=HTMLResponse)
async def streamer_edit(request: Request, streamer_id: int):
    return _render(
        request, "streamer_form.html", fixtures.streamer_form_context(_scenario(request))
    )


@app.get("/recordings", response_class=HTMLResponse)
async def recordings(request: Request):
    return _render(
        request, "recordings.html", fixtures.recordings_context(_scenario(request), _query(request))
    )


@app.get("/recordings/{session_id}", response_class=HTMLResponse)
async def recording_detail(request: Request, session_id: int):
    return _render(
        request, "recording_detail.html", fixtures.recording_detail_context(_scenario(request))
    )


@app.get("/comments", response_class=HTMLResponse)
async def comments(request: Request):
    return _render(
        request, "comments.html", fixtures.comments_context(_scenario(request), _query(request))
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    error = "監視間隔は30秒以上にしてください" if request.query_params.get("error") else ""
    return _render(
        request, "settings.html", fixtures.settings_context(_scenario(request), error=error)
    )


@app.get("/system", response_class=HTMLResponse)
async def system(request: Request):
    report = bool(request.query_params.get("report"))
    return _render(
        request, "system.html", fixtures.system_context(_scenario(request), with_report=report)
    )


@app.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    return _render(request, "login.html", fixtures.login_context(_scenario(request)))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
