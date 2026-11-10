"""Web 画面の結合テスト（PostgreSQL 必須）。実際のルートを通して画面と操作を確認する。"""

import asyncio
import os
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from live_recorder.cli import hash_password
from live_recorder.db.models import (
    Comment,
    CommentCollectionJob,
    RecordingJob,
    RecordingSegment,
    Streamer,
    StreamSession,
)
from live_recorder.db.repository import Repository
from live_recorder.db.session import Database
from live_recorder.timeutil import utcnow
from live_recorder.web.app import create_app
from test_db_flow import _reset_schema, _settings

pytestmark = pytest.mark.skipif(
    not os.environ.get("LIVE_RECORDER_TEST_DATABASE_URL"),
    reason="LIVE_RECORDER_TEST_DATABASE_URL が無いためデータベース試験を省略",
)

XSS = "<script>alert(1)</script>"


async def _seed(settings) -> dict:
    await _reset_schema()
    db = Database(settings)
    now = utcnow()
    async with db.session() as session:
        repo = Repository(session)
        await repo.create_user("admin", hash_password("password-123"))
        live = Streamer(
            platform="tiktok",
            platform_user_id="live_user",
            display_name="配信中の人",
            profile_url="https://www.tiktok.com/@live_user",
            live_status="LIVE",
            last_checked_at=now,
        )
        await repo.add_streamer(live)
        stream = StreamSession(
            streamer_id=live.id,
            title="テスト配信",
            detected_at=now - timedelta(minutes=5),
            status="LIVE",
        )
        session.add(stream)
        await session.flush()
        job = RecordingJob(
            stream_session_id=stream.id,
            status="RECORDING",
            recorder_engine="streamlink",
            started_at=now,
        )
        session.add(job)
        session.add(
            CommentCollectionJob(
                stream_session_id=stream.id, status="COLLECTING", received_count=1, stored_count=1
            )
        )
        await session.flush()
        stored = settings.recordings_dir / "tiktok" / "live_user" / "seg1.mp4"
        stored.parent.mkdir(parents=True)
        stored.write_bytes(b"0" * 1024)
        session.add(
            RecordingSegment(
                recording_job_id=job.id,
                segment_number=1,
                started_at=now,
                file_path="tiktok/live_user/seg1.mp4",
                file_size=1024,
            )
        )
        session.add(
            RecordingSegment(
                recording_job_id=job.id,
                segment_number=2,
                started_at=now,
                file_path="tiktok/live_user/missing.mp4",
            )
        )
        session.add(
            Comment(
                stream_session_id=stream.id,
                platform_user_id="viewer",
                nickname="視聴者",
                message="古いコメント",
                received_at=now - timedelta(minutes=10),
                dedupe_key="k0",
            )
        )
        session.add(
            Comment(
                stream_session_id=stream.id,
                platform_user_id="viewer",
                nickname="視聴者",
                message=XSS,
                received_at=now,
                dedupe_key="k1",
            )
        )
        await session.commit()
        ids = {"streamer": live.id, "session": stream.id}
    await db.dispose()
    return ids


@pytest.fixture
def client(tmp_path):
    (tmp_path / "rec").mkdir()
    (tmp_path / "cfg").mkdir()
    settings = _settings(tmp_path)
    ids = asyncio.run(_seed(settings))
    with TestClient(create_app(settings)) as test_client:
        test_client.ids = ids
        yield test_client


def _csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def _login(client) -> str:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"csrf": _csrf(page.text), "username": "admin", "password": "password-123"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return _csrf(client.get("/").text)


def test_healthz_reports_active_recording(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "recording": True}


def test_pages_require_login(client):
    for path in (
        "/",
        "/streamers",
        "/recordings",
        "/comments",
        "/settings",
        "/system",
        "/partials/active-sessions",
    ):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login", path


def test_pages_render_with_real_data(client):
    _login(client)
    session_id = client.ids["session"]
    for path in (
        "/",
        "/streamers",
        "/recordings",
        f"/recordings/{session_id}",
        "/comments",
        "/settings",
        "/system",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "script-src 'self'" in response.headers["content-security-policy"]
    dashboard = client.get("/").text
    assert "配信中の人" in dashboard and "テスト配信" in dashboard
    partial = client.get("/partials/active-sessions").text
    assert "<html" not in partial and "配信中の人" in partial
    detail = client.get(f"/recordings/{session_id}").text
    assert detail.count("/download") == 1, "保存されているセグメントだけダウンロードできる"
    index = client.get("/comments").text
    assert "録画を選ぶと" in index and f"/comments?session_id={session_id}" in index
    comments = client.get(f"/comments?session_id={session_id}").text
    assert XSS not in comments and "&lt;script&gt;" in comments
    assert comments.index("&lt;script&gt;") < comments.index("古いコメント")
    assert client.get("/comments/export?format=srt").status_code == 400
    exported = client.get(f"/comments/export?session_id={session_id}&format=srt")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/x-subrip")
    assert "-->" in exported.text
    assert exported.text.index("古いコメント") < exported.text.index("<script>")
    assert client.get("/comments/export?format=jsonl").status_code == 400


def test_recording_history_delete_keeps_database_constraints(client):
    token = _login(client)
    session_id = client.ids["session"]
    removed = client.post(
        f"/recordings/{session_id}/delete",
        data={"csrf": token, "confirm": "yes"},
        follow_redirects=False,
    )
    assert removed.status_code == 303
    assert removed.headers["location"] == "/recordings"
    assert client.get(f"/recordings/{session_id}").status_code == 404
    assert "テスト配信" not in client.get("/recordings").text


def test_streamer_lifecycle(client):
    token = _login(client)
    bad = client.post(
        "/streamers",
        data={
            "csrf": token,
            "platform": "tiktok",
            "account": "bad id!!",
            "display_name": "残る名前",
        },
    )
    assert bad.status_code == 400
    assert "data-open-on-load" in bad.text and 'value="残る名前"' in bad.text
    created = client.post(
        "/streamers",
        data={
            "csrf": token,
            "platform": "tiktok",
            "account": "@ui_dummy",
            "display_name": "ダミー",
            "recording_enabled": "on",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    listing = client.get("/streamers").text
    streamer_id = int(
        re.search(r'id="streamer-(\d+)"[^>]*>(?:(?!</tr>)[\s\S])*?ui_dummy', listing).group(1)
    )
    row = client.post(
        f"/streamers/{streamer_id}/toggle",
        data={"field": "recording_enabled"},
        headers={"HX-Request": "true", "X-CSRF-Token": token},
    )
    assert row.status_code == 200
    assert row.text.lstrip().startswith(f'<tr id="streamer-{streamer_id}"')
    assert re.search(r'aria-checked="false"[^>]*aria-label="録画', row.text)
    forbidden = client.post(f"/streamers/{streamer_id}/toggle", data={"field": "recording_enabled"})
    assert forbidden.status_code == 403
    edited = client.post(
        f"/streamers/{streamer_id}/edit",
        data={"csrf": token, "display_name": "変更後", "monitoring_enabled": "on"},
        follow_redirects=False,
    )
    assert edited.status_code == 303
    assert "変更後" in client.get(f"/streamers/{streamer_id}/edit").text
    kept = client.post(
        f"/streamers/{streamer_id}/delete", data={"csrf": token}, follow_redirects=True
    )
    assert "変更後" in kept.text, "確認なしでは削除しない"
    deleted = client.post(
        f"/streamers/{streamer_id}/delete",
        data={"csrf": token, "confirm": "yes"},
        follow_redirects=True,
    )
    assert "ui_dummy" not in deleted.text
    blocked = client.post(
        f"/streamers/{client.ids['streamer']}/delete",
        data={"csrf": token, "confirm": "yes"},
        follow_redirects=True,
    )
    assert "録画履歴がある配信者は削除できません" in blocked.text


def test_settings_roundtrip_and_validation_keep_values(client):
    token = _login(client)
    page = client.get("/settings").text

    def value(name):
        return re.search(rf'name="{name}"[^>]*value="([^"]*)"', page).group(1)

    def selected(name):
        block = re.search(rf'name="{name}"[\s\S]*?</select>', page).group(0)
        return re.search(r'value="([^"]*)" selected', block).group(1)

    form = {
        "csrf": token,
        "monitor_interval_seconds": value("monitor_interval_seconds"),
        "max_concurrent_recordings": value("max_concurrent_recordings"),
        "display_timezone": value("display_timezone"),
        "recording_quality": selected("recording_quality"),
        "min_free_gib": value("min_free_gib"),
        "retention_days": value("retention_days"),
        "engine_1": selected("engine_1"),
        "engine_2": selected("engine_2"),
        "engine_3": selected("engine_3"),
        "comment_retention_days": value("comment_retention_days"),
        "comment_poll_seconds": value("comment_poll_seconds"),
        "reconnect_max_attempts": value("reconnect_max_attempts"),
        "log_level": selected("log_level"),
        "comments_globally_enabled": "on",
        "speech_subtitles_enabled": "on",
        "speech_model": selected("speech_model"),
        "speech_language": value("speech_language"),
    }
    saved = client.post(
        "/settings", data={**form, "active_tab": "settings-comments"}, follow_redirects=False
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == "/settings#settings-comments"
    shown = client.get("/settings")
    assert shown.status_code == 200 and "設定を保存しました" in shown.text
    rejected = client.post(
        "/settings", data={**form, "active_tab": "https://evil.example"}, follow_redirects=False
    )
    assert rejected.headers["location"] == "/settings"
    invalid = client.post(
        "/settings",
        data={
            **form,
            "active_tab": "settings-recording",
            "monitor_interval_seconds": "1",
            "display_timezone": "UTC",
        },
    )
    assert invalid.status_code == 400
    assert re.search(r'name="display_timezone"[^>]*value="UTC"', invalid.text)
    assert re.search(r'name="monitor_interval_seconds"[^>]*value="1"', invalid.text)
    assert re.search(r'id="tab-recording"[^>]*aria-selected="true"', invalid.text)
    assert re.search(r'id="tab-general"[^>]*aria-selected="false"', invalid.text)
    assert re.search(r'name="active_tab"[^>]*value="settings-recording"', invalid.text)
    tampered = client.post(
        "/settings",
        data={**form, "active_tab": '"><script>', "monitor_interval_seconds": "1"},
    )
    assert tampered.status_code == 400
    assert '"><script>' not in tampered.text
    assert re.search(r'id="tab-general"[^>]*aria-selected="true"', tampered.text)


def test_comment_partial_and_export_keep_filters(client):
    _login(client)
    session_id = client.ids["session"]
    response = client.get(f"/comments?session_id={session_id}&q=script", headers={"HX-Request": "true"})
    assert response.status_code == 200 and 'id="results"' in response.text
    assert "q=script" in response.text and "format=srt" in response.text
    export = client.get(f"/comments/export?session_id={session_id}&q=script&format=srt")
    assert export.status_code == 200 and "attachment" in export.headers["content-disposition"]


def test_system_scan(client):
    token = _login(client)
    response = client.post("/system/scan", data={"csrf": token})
    assert response.status_code == 200
    assert "ファイル不足" in response.text and "missing.mp4" in response.text
