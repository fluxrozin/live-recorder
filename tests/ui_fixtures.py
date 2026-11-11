"""UI テストとプレビューで共有する、テンプレート用の固定データ。

データベースを使わずに、空データ・大量データ・長文・エラーなどの境界状態を再現する。
"""

from __future__ import annotations

from datetime import timedelta

from starlette.requests import Request

from live_recorder.db.runtime import RuntimeSettings
from live_recorder.recording.integrity import ScanReport
from live_recorder.timeutil import utcnow

SCENARIOS = ("normal", "empty", "stress")
CSRF = "test-csrf-token"

LONG_TITLE = "【長時間配信】" + "とても長い配信タイトルが続きます。" * 8
LONG_COMMENT = "長いコメント本文です。改行を含みます。\n2行目です。\n" + "あ" * 300
LONG_ID = "user_" + "x" * 120
XSS = '<script>alert("xss")</script><img src=x onerror=alert(1)>'


def make_request(path: str = "/", query: str = "", method: str = "GET") -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query.encode(),
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


def base_context(path: str = "/", query_string: str = "", **extra) -> dict:
    now = utcnow()
    context = {
        "request": make_request(path, query_string),
        "query": query_string,
        "csrf": CSRF,
        "flash": None,
        "tz": "Asia/Tokyo",
        "username": "admin",
        "rendered_at": now,
    }
    context.update(extra)
    return context


def _streamer(index: int, now, **overrides) -> dict:
    row = {
        "id": index,
        "platform": "tiktok",
        "user_id": f"streamer_{index}",
        "display_name": f"配信者{index}",
        "profile_url": f"https://www.tiktok.com/@streamer_{index}",
        "monitoring_enabled": True,
        "recording_enabled": True,
        "comments_enabled": True,
        "live_status": "OFFLINE",
        "last_checked_at": now - timedelta(seconds=40),
        "last_live_at": now - timedelta(days=2),
        "last_recording_at": now - timedelta(days=2),
        "last_error": "",
        "recording_state": "NONE",
        "comment_state": "NONE",
    }
    row.update(overrides)
    return row


def streamer_rows(scenario: str) -> list[dict]:
    now = utcnow()
    if scenario == "empty":
        return []
    rows = [
        _streamer(
            1,
            now,
            display_name="さくら",
            live_status="LIVE",
            recording_state="RECORDING",
            comment_state="COLLECTING",
        ),
        _streamer(
            2,
            now,
            platform="twitcasting",
            user_id="caster_2",
            profile_url="https://twitcasting.tv/caster_2",
            display_name="ツイキャス配信者",
        ),
        _streamer(
            3, now, monitoring_enabled=False, live_status="DISABLED", display_name="監視停止中"
        ),
        _streamer(
            4,
            now,
            live_status="ERROR",
            last_error="TikTok の応答がタイムアウトしました (HTTP 504)",
            display_name="エラー発生",
        ),
        _streamer(5, now, last_checked_at=None, display_name="新規登録"),
        _streamer(
            6,
            now,
            live_status="LIVE",
            recording_state="RECONNECTING",
            comment_state="CONNECTING",
            display_name="再接続中",
        ),
    ]
    if scenario == "stress":
        rows.append(
            _streamer(
                7, now, display_name="とても長い表示名" * 6, user_id="a" * 24, live_status="UNKNOWN"
            )
        )
        rows += [_streamer(10 + i, now, display_name=f"大量データ{i}") for i in range(40)]
    return rows


def streamers_context(scenario: str, **extra) -> dict:
    return base_context("/streamers", rows=streamer_rows(scenario), error="", **extra)


def dashboard_context(scenario: str) -> dict:
    now = utcnow()
    empty = scenario == "empty"
    total = 16 * 1024**4
    free = (total // 10) if scenario == "stress" else 7 * 1024**4
    stats = {
        "streamers": 0 if empty else 12,
        "live": 0 if empty else 2,
        "recording": 0 if empty else 2,
        "collecting": 0 if empty else 1,
        "speech_pending": 0 if empty else 3,
        "speech_running": 0 if empty else 1,
        "sessions_today": 0 if empty else 5,
        "comments_today": 0 if empty else 12840,
        "total_bytes": 0 if empty else 2 * 1024**4,
    }
    active = (
        []
        if empty
        else [
            {
                "id": 101,
                "name": "さくら",
                "platform": "tiktok",
                "user_id": "sakura",
                "title": "雑談配信",
                "session_status": "LIVE",
                "detected_at": now - timedelta(minutes=50),
                "recording_status": "RECORDING",
                "recording_started_at": now - timedelta(minutes=49),
                "recording_error": "",
                "comment_status": "COLLECTING",
                "comment_received": 1532,
                "comment_last_received_at": now - timedelta(seconds=3),
            },
            {
                "id": 102,
                "name": "再接続中",
                "platform": "twitcasting",
                "user_id": "caster",
                "title": LONG_TITLE,
                "session_status": "LIVE",
                "detected_at": now - timedelta(hours=3),
                "recording_status": "RECONNECTING",
                "recording_started_at": None,
                "recording_error": "",
                "comment_status": "CONNECTING",
                "comment_received": 0,
                "comment_last_received_at": None,
            },
        ]
    )
    cards = (
        []
        if empty
        else [
            {
                "id": 100 - i,
                "name": f"配信者{i}",
                "platform": "tiktok" if i % 2 else "twitcasting",
                "title": LONG_TITLE if i == 1 else f"配信 {i}",
                "detected_at": now - timedelta(hours=i * 5),
                "status": ["RECORDING", "COMPLETED", "PARTIAL", "FAILED", "CANCELLED", "NONE"][
                    i % 6
                ],
            }
            for i in range(8)
        ]
    )
    errors = (
        []
        if scenario != "stress"
        else [
            {
                "created_at": now - timedelta(minutes=5),
                "severity": "error",
                "message": "録画エンジン streamlink が失敗しました: " + "詳細 " * 50,
            },
            {
                "created_at": now - timedelta(hours=2),
                "severity": "warning",
                "message": "コメント収集を再接続しました",
            },
        ]
    )
    worker = {
        "state": "error" if scenario == "stress" else ("unknown" if empty else "ok"),
        "age": 900 if scenario == "stress" else 5,
        "status": "running",
        "last_seen_at": None
        if empty
        else now - timedelta(seconds=900 if scenario == "stress" else 5),
        "label": "停止または無応答" if scenario == "stress" else ("未検出" if empty else "稼働中"),
    }
    return base_context(
        "/",
        stats=stats,
        cards=cards,
        errors=errors,
        free=free,
        total=total,
        used=total - free,
        min_free=5 * 1024**3,
        by_streamer=[]
        if empty
        else [
            ("tiktok", "さくら", 900 * 1024**3),
            ("twitcasting", "ツイキャス配信者", 300 * 1024**3),
        ],
        active=active,
        worker=worker,
        last_checked=None if empty else now - timedelta(seconds=20),
        monitored=0 if empty else 10,
        warning="",
    )


def recordings_context(scenario: str, query: str = "") -> dict:
    now = utcnow()
    count = {"empty": 0, "normal": 6, "stress": 20}[scenario]
    statuses = ["RECORDING", "COMPLETED", "PARTIAL", "FAILED", "CANCELLED", "NONE"]
    speech = [None, "running", "pending", "failed", "completed", None]
    cards = [
        {
            "id": 200 + i,
            "name": "とても長い表示名" * 4 if i == 2 else f"配信者{i}",
            "platform": "tiktok" if i % 2 else "twitcasting",
            "title": LONG_TITLE if i % 3 == 0 else f"配信タイトル {i}",
            "started": now - timedelta(hours=i * 7),
            "finished": None if i == 0 else now - timedelta(hours=i * 7 - 2),
            "duration_ms": 7_200_000 + i * 1000,
            "files": i % 3,
            "size": (i + 1) * 3 * 1024**3,
            "comments": i * 1234,
            "speech_cues": i * 3,
            "status": statuses[i % len(statuses)],
            "speech_status": speech[i % len(speech)],
        }
        for i in range(count)
    ]
    filters = {"streamer_id": "", "platform": "", "status": "", "start": "", "end": ""}
    for part in query.split("&"):
        if "=" in part:
            key, value = part.split("=", 1)
            if key in filters:
                filters[key] = value
    total = 245 if scenario == "stress" else count
    return base_context(
        "/recordings",
        query,
        cards=cards,
        total=total,
        page=2 if scenario == "stress" else 1,
        pages=max(1, (total + 19) // 20),
        filters=filters,
        streamers=[
            {"id": 1, "name": "さくら", "platform": "tiktok"},
            {"id": 2, "name": "ツイキャス配信者", "platform": "twitcasting"},
        ],
    )


def recording_detail_context(scenario: str) -> dict:
    now = utcnow()
    segments = (
        []
        if scenario == "empty"
        else [
            {
                "id": 1,
                "number": 1,
                "started_at": now - timedelta(hours=3),
                "finished_at": now - timedelta(hours=2),
                "size": 4 * 1024**3,
                "duration_ms": 3_600_000,
                "container": "mp4",
                "validation": "ok",
                "removed": False,
                "exists": True,
                "name": "20261109_120000_sakura_001.mp4",
                "speech_status": "completed",
                "speech_ready": True,
                "speech_empty": False,
                "speech_error": "",
            },
            {
                "id": 2,
                "number": 2,
                "started_at": now - timedelta(hours=2),
                "finished_at": now - timedelta(hours=1),
                "size": 3 * 1024**3,
                "duration_ms": 3_500_000,
                "container": "mp4",
                "validation": "partial",
                "removed": False,
                "exists": False,
                "name": "20261109_130000_sakura_002_" + "x" * 80 + ".mp4",
                "speech_status": "failed",
                "speech_ready": False,
                "speech_empty": False,
                "speech_error": "モデルを読み込めませんでした",
            },
            {
                "id": 3,
                "number": 3,
                "started_at": now - timedelta(hours=1),
                "finished_at": None,
                "size": None,
                "duration_ms": None,
                "container": "—",
                "validation": "pending",
                "removed": True,
                "exists": False,
                "name": "20261109_140000_sakura_003.ts",
                "speech_status": "NONE",
                "speech_ready": False,
                "speech_empty": False,
                "speech_error": "",
            },
        ]
    )
    detail = {
        "id": 101,
        "name": "さくら",
        "platform": "tiktok",
        "user_id": "sakura",
        "title": LONG_TITLE if scenario == "stress" else "雑談配信",
        "stream_id": "7412345678901234567",
        "status": "ENDED",
        "detected_at": now - timedelta(hours=3),
        "platform_started_at": now - timedelta(hours=3, minutes=2),
        "last_seen_live_at": now - timedelta(minutes=32),
        "ended_detected_at": now - timedelta(minutes=30),
        "recording_status": "PARTIAL",
        "engine": "streamlink",
        "recording_error": "ffmpeg exited with code 1: " + "Connection reset by peer " * 10
        if scenario == "stress"
        else "",
        "comment_status": "COMPLETED",
        "comment_received": 15320,
        "comment_stored": 15301,
        "comment_duplicates": 19,
        "comment_errors": 19,
        "comment_reconnects": 2,
        "comment_error": "",
        "comment_last_received_at": now - timedelta(minutes=31),
        "comments_total": 15301,
        "speech_cue_count": 0 if scenario == "empty" else 12,
        "recording_started_at": now - timedelta(hours=3),
        "recording_finished_at": now - timedelta(minutes=30),
        "recording_retries": 1,
        "segments": segments,
        "gaps": []
        if scenario == "empty"
        else [
            {
                "job_type": "recording",
                "started_at": now - timedelta(hours=2),
                "ended_at": now - timedelta(hours=2) + timedelta(seconds=40),
                "reason": "ストリームが切断されました",
            },
            {
                "job_type": "comments",
                "started_at": now - timedelta(hours=1),
                "ended_at": None,
                "reason": "WebSocket 切断",
            },
        ],
    }
    return base_context("/recordings/101", detail=detail)


def comments_context(scenario: str, query: str = "") -> dict:
    now = utcnow()
    count = {"empty": 0, "normal": 8, "stress": 50}[scenario]
    if "session_id=" not in query:
        query = f"session_id=101&{query}" if query else "session_id=101"
    comments = [
        {
            "when": now - timedelta(seconds=i * 13),
            "streamer": "さくら" if i % 2 else "ツイキャス配信者",
            "platform": "tiktok" if i % 2 else "twitcasting",
            "user_id": LONG_ID if i == 1 else f"viewer_{i}",
            "nickname": "とても長いニックネーム" * 5 if i == 2 else f"視聴者{i}",
            "message": XSS if i == 0 else (LONG_COMMENT if i == 3 else f"コメント {i} です"),
            "offset": i * 13_000,
            "session_id": 101,
        }
        for i in range(count)
    ]
    filters = {
        key: ""
        for key in (
            "q",
            "user_id",
            "nickname",
            "streamer_id",
            "platform",
            "session_id",
            "start",
            "end",
        )
    }
    for part in query.split("&"):
        if "=" in part:
            key, value = part.split("=", 1)
            if key in filters:
                filters[key] = value
    filters["session_id"] = filters["session_id"] or "101"
    total = 12_345 if scenario == "stress" else count
    return base_context(
        "/comments",
        query,
        comments=comments,
        total=total,
        page=3 if scenario == "stress" else 1,
        pages=max(1, (total + 49) // 50),
        filters=filters,
        order="desc",
        session={
            "id": int(filters["session_id"]),
            "name": "さくら",
            "platform": "tiktok",
            "title": LONG_TITLE if scenario == "stress" else "雑談配信",
            "streamer_id": 1,
        },
    )


def comments_index_context(scenario: str, query: str = "") -> dict:
    now = utcnow()
    count = {"empty": 0, "normal": 4, "stress": 24}[scenario]
    cards = [
        {
            "id": 100 + i,
            "name": "さくら" if i % 2 else "ツイキャス配信者",
            "platform": "tiktok" if i % 2 else "twitcasting",
            "title": LONG_TITLE if i == 0 else f"配信タイトル {i}",
            "started": now - timedelta(hours=i),
            "comments": 10 + i,
        }
        for i in range(count)
    ]
    filters = {"streamer_id": "", "platform": ""}
    for part in query.split("&"):
        if "=" in part:
            key, value = part.split("=", 1)
            if key in filters:
                filters[key] = value
    total = 80 if scenario == "stress" else count
    return base_context(
        "/comments",
        query,
        cards=cards,
        total=total,
        page=2 if scenario == "stress" else 1,
        pages=max(1, (total + 19) // 20),
        filters=filters,
        streamers=[
            {"id": 1, "name": "さくら", "platform": "tiktok"},
            {"id": 2, "name": "ツイキャス配信者", "platform": "twitcasting"},
        ],
    )


def settings_context(scenario: str, error: str = "") -> dict:
    return base_context(
        "/settings",
        runtime=RuntimeSettings(),
        error=error,
        env={
            "recordings_dir": "/data/recordings",
            "web_port": 8080,
            "twitcasting": scenario != "empty",
            "sign_key": False,
            "cookie_secure": False,
        },
    )


def system_context(scenario: str, with_report: bool = False) -> dict:
    now = utcnow()
    report = None
    scan = None
    if with_report:
        report = ScanReport(
            missing=["2026/11/sakura_001.mp4"],
            size_mismatch=[],
            unregistered=[f"2026/11/unknown_{i:03d}.mp4" for i in range(30)],
            temporary=["2026/11/sakura_004.mp4.part"],
            suspect=["2026/11/" + "long_path_" * 12 + ".mp4"],
        )
        scan = {"checked": 128, "ok": 126}
    worker = {
        "state": "ok",
        "age": 6,
        "status": "running",
        "last_seen_at": now - timedelta(seconds=6),
        "label": "稼働中",
    }
    if scenario == "stress":
        worker = {
            "state": "warning",
            "age": 120,
            "status": "running",
            "last_seen_at": now - timedelta(seconds=120),
            "label": "応答が遅れています",
        }
    elif scenario == "empty":
        worker = {
            "state": "unknown",
            "age": None,
            "status": None,
            "last_seen_at": None,
            "label": "未検出",
        }
    engines = (
        []
        if scenario == "empty"
        else [
            {
                "name": "streamlink",
                "version": "8.6.3",
                "available": True,
                "last_success_at": now - timedelta(hours=1),
                "last_failure_at": None,
                "failure_reason": "",
                "retry_count": 0,
            },
            {
                "name": "yt-dlp",
                "version": "2026.10.26",
                "available": True,
                "last_success_at": None,
                "last_failure_at": now - timedelta(hours=5),
                "failure_reason": "HTTP Error 403: Forbidden " * 6,
                "retry_count": 3,
            },
            {
                "name": "tiktoklive",
                "version": "—",
                "available": False,
                "last_success_at": None,
                "last_failure_at": None,
                "failure_reason": "",
                "retry_count": 0,
            },
        ]
    )
    events = (
        []
        if scenario == "empty"
        else [
            {
                "created_at": now - timedelta(minutes=i * 17),
                "severity": ["info", "warning", "error", "info"][i % 4],
                "message": ("長いメッセージ " * 40)
                if i == 2
                else f"イベント {i}: 配信を検知しました",
            }
            for i in range(12 if scenario == "normal" else 50)
        ]
    )
    return base_context(
        "/system",
        versions={
            "app": "0.1.0",
            "streamlink": "streamlink 8.6.3",
            "yt_dlp": "2026.10.26",
            "ffmpeg": "6.1.1-3ubuntu5",
            "ffprobe": "",
            "tiktoklive": "7.0.1",
        },
        engines=engines,
        events=events,
        worker=worker,
        worker_ok=worker["state"] == "ok",
        worker_age=worker["age"],
        db_ok=True,
        last_checked=None
        if scenario == "empty"
        else now - timedelta(seconds=30 if scenario == "normal" else 900),
        monitor_interval=60,
        monitored=10,
        report=report,
        scan=scan,
        sign_key=False,
        twitcasting=True,
    )


def streamer_form_context(scenario: str) -> dict:
    now = utcnow()
    row = (
        _streamer(4, now, live_status="ERROR", last_error="TikTok の応答がタイムアウトしました")
        if scenario == "stress"
        else _streamer(1, now)
    )
    row["speech_words"] = "とても長い固有名詞" * 8 if scenario == "stress" else "さくら\nApex"
    return base_context("/streamers/1/edit", row=row, error="", words_error="")


def login_context(scenario: str) -> dict:
    return base_context(
        "/login",
        username=None,
        error="ユーザー名またはパスワードが違います" if scenario == "stress" else "",
    )


def speech_cues_context(scenario: str, query: str = "") -> dict:
    filters = {"q": "", "session_id": "", "segment_id": "", "file_name": ""}
    for part in query.split("&"):
        if "=" in part:
            key, value = part.split("=", 1)
            if key in filters:
                filters[key] = value
    if scenario == "empty":
        lines = []
        files = []
    else:
        lines = [
            {
                "start_ms": 0,
                "end_ms": 1500,
                "text": XSS if i == 0 else (LONG_COMMENT if i == 3 else f"発話 {i} です"),
                "name": "さくら",
                "platform": "tiktok",
                "session_id": 101,
                "file_name": "20261109_120000_sakura_001.mp4",
            }
            for i in range({"normal": 4, "stress": 50}[scenario])
        ]
        files = [
            {"id": 1, "name": "20261109_120000_sakura_001.mp4"},
            {"id": 2, "name": "20261109_130000_sakura_002.mp4"},
        ]
    total = len(lines)
    return base_context(
        "/speech/cues",
        query,
        lines=lines,
        total=total,
        page=1,
        pages=max(1, (total + 49) // 50),
        filters=filters,
        files=files,
        order="asc",
        download_url="/recordings/segments/1/speech" if scenario == "normal" else "",
    )


def speech_context(scenario: str) -> dict:
    now = utcnow()
    counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0, "skipped": 0, "none": 0}
    if scenario == "empty":
        return base_context(
            "/speech",
            items=[],
            total=0,
            page=1,
            pages=1,
            filters={"status": "", "session_id": ""},
            counts=counts,
            enabled=True,
        )
    items = [
        {
            "id": 1,
            "session_id": 101,
            "name": "さくら",
            "platform": "tiktok",
            "file_name": "20261109_120000_sakura_001.mp4",
            "finished_at": now,
            "duration_ms": 3_600_000,
            "speech_status": "completed",
            "speech_ready": True,
            "speech_empty": False,
            "speech_error": "",
            "can_requeue": True,
        },
        {
            "id": 2,
            "session_id": 101,
            "name": "さくら",
            "platform": "tiktok",
            "file_name": "20261109_130000_sakura_002.mp4",
            "finished_at": now,
            "duration_ms": 1_800_000,
            "speech_status": "failed",
            "speech_ready": False,
            "speech_empty": False,
            "speech_error": "モデルを読み込めませんでした" if scenario == "normal" else "失敗理由 " * 30,
            "can_requeue": True,
        },
        {
            "id": 4,
            "session_id": 101,
            "name": "さくら",
            "platform": "tiktok",
            "file_name": "20261109_140000_sakura_004.mp4",
            "finished_at": now,
            "duration_ms": 600_000,
            "speech_status": "running",
            "speech_ready": False,
            "speech_empty": False,
            "speech_error": "",
            "can_requeue": False,
        },
    ]
    if scenario == "stress":
        counts = {"pending": 12, "running": 1, "completed": 40, "failed": 3, "skipped": 2, "none": 1}
        filters = {"status": "failed", "session_id": "101"}
        enabled = False
        total = 59
    else:
        counts = {"pending": 1, "running": 1, "completed": 1, "failed": 1, "skipped": 0, "none": 0}
        filters = {"status": "", "session_id": ""}
        enabled = True
        total = len(items)
    return base_context(
        "/speech",
        "status=failed&session_id=101" if scenario == "stress" else "",
        items=items,
        total=total,
        page=2 if scenario == "stress" else 1,
        pages=max(1, (total + 19) // 20),
        filters=filters,
        counts=counts,
        enabled=enabled,
    )


PAGES = {
    "dashboard.html": dashboard_context,
    "streamers.html": streamers_context,
    "streamer_form.html": streamer_form_context,
    "recordings.html": recordings_context,
    "recording_detail.html": recording_detail_context,
    "speech.html": speech_context,
    "speech_cues.html": speech_cues_context,
    "comments.html": comments_context,
    "comments_index.html": comments_index_context,
    "settings.html": settings_context,
    "system.html": system_context,
    "login.html": login_context,
}
