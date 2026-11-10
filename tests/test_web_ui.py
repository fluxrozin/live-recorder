"""UI の回帰テスト。データベースを使わず、テンプレートを固定データで描画して検証する。"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest

import ui_fixtures as fx
from live_recorder.config import Settings
from live_recorder.web import ui
from live_recorder.web.app import _fallback_runtime, create_app, settings_tab, templates

CASES = [(name, scenario) for name in fx.PAGES for scenario in fx.SCENARIOS]


def render(name: str, context: dict) -> str:
    return templates.get_template(name).render(context)


def render_page(name: str, scenario: str) -> str:
    return render(name, fx.PAGES[name](scenario))


class FormCollector(HTMLParser):
    """フォームごとに method / action / hx-* 属性と、含まれる入力要素を集める。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self.stack: list[dict] = []
        self.tags: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        attributes = {key: (value or "") for key, value in attrs}
        self.tags.append((tag, attributes))
        if tag == "form":
            form = {"attrs": attributes, "fields": [], "buttons": []}
            self.forms.append(form)
            self.stack.append(form)
        elif self.stack and tag in {"input", "select", "textarea"}:
            self.stack[-1]["fields"].append(attributes)
        elif self.stack and tag == "button":
            self.stack[-1]["buttons"].append(attributes)

    def handle_endtag(self, tag):
        if tag == "form" and self.stack:
            self.stack.pop()


def parse(html: str) -> FormCollector:
    collector = FormCollector()
    collector.feed(html)
    return collector


def field_names(form: dict) -> set[str]:
    return {field.get("name", "") for field in form["fields"]}


@pytest.mark.parametrize(("name", "scenario"), CASES)
def test_every_page_renders(name, scenario):
    html = render_page(name, scenario)
    assert "<html" in html and "</html>" in html
    assert "None" not in re.sub(r"<[^>]+>", "", html), "None がそのまま表示されている"


@pytest.mark.parametrize(("name", "scenario"), CASES)
def test_post_forms_carry_csrf(name, scenario):
    collector = parse(render_page(name, scenario))
    for form in collector.forms:
        if form["attrs"].get("method", "get").lower() != "post":
            continue
        tokens = [f for f in form["fields"] if f.get("name") == "csrf"]
        assert tokens and tokens[0].get("value") == fx.CSRF, form["attrs"]


@pytest.mark.parametrize(("name", "scenario"), CASES)
def test_no_inline_script_style_or_handlers(name, scenario):
    """CSP（script-src / style-src 'self'）に違反するインライン記述がないこと。"""
    collector = parse(render_page(name, scenario))
    for tag, attrs in collector.tags:
        assert "style" not in attrs, (tag, attrs)
        assert not [key for key in attrs if key.startswith("on")], (tag, attrs)
        assert not [key for key in attrs if key.startswith("hx-on")], (tag, attrs)
        if tag == "script":
            assert attrs.get("src"), "インラインスクリプトは CSP でブロックされる"
        if tag in {"link", "script", "img"}:
            url = attrs.get("href") or attrs.get("src") or ""
            assert not url.startswith(("http:", "https:", "//")), f"外部リソース {url}"


@pytest.mark.parametrize("name", [n for n in fx.PAGES if n != "login.html"])
def test_shell_has_navigation_and_logout(name):
    html = render_page(name, "normal")
    collector = parse(html)
    for href in ("/", "/streamers", "/recordings", "/comments", "/settings", "/system"):
        assert f'href="{href}"' in html
    assert any(f["attrs"].get("action") == "/logout" for f in collector.forms)
    assert 'aria-current="page"' in html
    assert 'hx-headers=\'{"X-CSRF-Token": "test-csrf-token"}\'' in html


def test_login_page_has_no_shell():
    html = render_page("login.html", "stress")
    assert 'id="sidebar"' not in html
    assert "create-admin" in html
    assert "ユーザー名またはパスワードが違います" in html
    form = next(f for f in parse(html).forms if f["attrs"].get("action") == "/login")
    assert {"csrf", "username", "password"} <= field_names(form)


def test_toggle_forms_keep_htmx_contract():
    html = render("streamer_row.html", fx.base_context(row=fx.streamer_rows("normal")[0]))
    toggles = [f for f in parse(html).forms if f["attrs"].get("hx-post")]
    assert len(toggles) == 3
    fields = set()
    for form in toggles:
        attrs = form["attrs"]
        assert attrs["hx-post"] == "/streamers/1/toggle"
        assert attrs["action"] == "/streamers/1/toggle"
        assert attrs["method"] == "post"
        assert attrs["hx-target"] == "closest tr"
        assert attrs["hx-swap"] == "outerHTML"
        field = next(f for f in form["fields"] if f.get("name") == "field")
        fields.add(field["value"])
        button = form["buttons"][0]
        assert button["role"] == "switch"
        assert button["aria-checked"] in {"true", "false"}
        assert button.get("aria-label")
    assert fields == {"monitoring_enabled", "recording_enabled", "comments_enabled"}
    assert html.lstrip().startswith('<tr id="streamer-1"')


def test_toggle_reflects_state():
    row = fx.streamer_rows("normal")[2]  # 監視停止
    html = render("streamer_row.html", fx.base_context(row=row))
    assert re.search(r'aria-checked="false"[^>]*aria-label="監視', html)
    assert "監視停止" in html


def test_delete_dialogs_post_confirmation():
    html = render_page("recording_detail.html", "normal")
    forms = {
        f["attrs"]["action"]: f for f in parse(html).forms if f["attrs"].get("method") == "post"
    }
    assert "/recordings/101/delete-files" in forms
    delete_forms = [
        f for f in parse(html).forms if f["attrs"].get("action") == "/recordings/101/delete"
    ]
    assert len(delete_forms) == 2
    with_files = [f for f in delete_forms if "delete_files" in field_names(f)]
    assert len(with_files) == 1
    assert (
        next(x for x in with_files[0]["fields"] if x.get("name") == "delete_files")["value"]
        == "yes"
    )
    for form in [forms["/recordings/101/delete-files"], *delete_forms]:
        confirm = next(x for x in form["fields"] if x.get("name") == "confirm")
        assert confirm["value"] == "yes" and "required" in confirm


def test_streamer_delete_dialog_and_edit_form():
    html = render_page("streamer_form.html", "normal")
    forms = {
        f["attrs"]["action"]: f for f in parse(html).forms if f["attrs"].get("method") == "post"
    }
    assert {
        "csrf",
        "display_name",
        "monitoring_enabled",
        "recording_enabled",
        "comments_enabled",
    } <= field_names(forms["/streamers/1/edit"])
    assert "confirm" in field_names(forms["/streamers/1/delete"])
    listing = render_page("streamers.html", "normal")
    add = next(f for f in parse(listing).forms if f["attrs"].get("action") == "/streamers")
    assert {
        "csrf",
        "platform",
        "account",
        "display_name",
        "monitoring_enabled",
        "recording_enabled",
        "comments_enabled",
    } <= field_names(add)


def test_add_form_keeps_submitted_values_on_error():
    context = fx.streamers_context("normal")
    context["error"] = "形式が不正です"
    context["add_form"] = {
        "platform": "twitcasting",
        "account": "bad<id>",
        "display_name": "表示",
        "monitoring_enabled": False,
        "recording_enabled": True,
        "comments_enabled": False,
    }
    html = render("streamers.html", context)
    assert "data-open-on-load" in html
    assert 'value="bad&lt;id&gt;"' in html
    assert re.search(r'value="twitcasting"[^>]*checked', html)
    form = next(f for f in parse(html).forms if f["attrs"].get("action") == "/streamers")
    checked = {f["name"] for f in form["fields"] if f.get("type") == "checkbox" and "checked" in f}
    assert checked == {"recording_enabled"}


def test_comment_body_is_escaped():
    html = render_page("comments.html", "normal")
    assert "<script>alert" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;alert(&#34;xss&#34;)&lt;/script&gt;" in html


def test_streamer_names_are_escaped():
    context = fx.streamers_context("normal")
    context["rows"][0]["display_name"] = "<b>bold</b>"
    html = render("streamers.html", context)
    assert "<b>bold</b>" not in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html


def test_comment_pagination_and_export_keep_filters():
    query = "q=hello&user_id=u1&streamer_id=2&page=3"
    html = render("comments.html", fx.comments_context("stress", query))
    links = re.findall(r'href="(/comments[^"]*)"', html)
    paging = [link for link in links if "page=" in link and "export" not in link]
    assert paging
    for link in paging:
        assert "q=hello" in link and "user_id=u1" in link and "streamer_id=2" in link
    exports = [link for link in links if link.startswith("/comments/export")]
    assert {re.search(r"format=(\w+)", link).group(1) for link in exports} == {"srt"}
    for link in exports:
        assert "q=hello" in link and "user_id=u1" in link and "page=" not in link
    search = next(f for f in parse(html).forms if f["attrs"].get("hx-get") == "/comments")
    assert search["attrs"]["hx-target"] == "#results"
    assert search["attrs"]["hx-select"] == "#results"
    assert search["attrs"]["hx-push-url"] == "true"
    assert {
        "q",
        "user_id",
        "nickname",
        "streamer_id",
        "platform",
        "session_id",
        "start",
        "end",
    } <= field_names(search)


def test_recording_pagination_keeps_filters():
    html = render(
        "recordings.html", fx.recordings_context("stress", "streamer_id=1&status=FAILED&page=2")
    )
    paging = [link for link in re.findall(r'href="(/recordings\?[^"]*)"', html) if "page=" in link]
    assert paging
    for link in paging:
        assert "streamer_id=1" in link and "status=FAILED" in link
    assert re.search(r'value="FAILED"[^>]*selected', html)


def test_segment_download_only_when_file_exists():
    html = render_page("recording_detail.html", "normal")
    assert "最終確認" in html
    assert 'href="/recordings/segments/1/download"' in html
    assert "/segments/2/download" not in html
    assert "/segments/3/download" not in html
    assert "ファイルなし" in html and "削除済み" in html
    for segment_id in (1, 2, 3):
        assert f"segment_id={segment_id}&amp;format=srt" in html


SETTINGS_FIELDS = {
    "monitor_interval_seconds",
    "max_concurrent_recordings",
    "display_timezone",
    "recording_quality",
    "min_free_gib",
    "retention_days",
    "auto_delete_enabled",
    "engine_1",
    "engine_2",
    "engine_3",
    "comments_globally_enabled",
    "comment_retention_days",
    "auto_export_on_complete",
    "comment_poll_seconds",
    "reconnect_max_attempts",
    "log_level",
}


def test_settings_single_form_contains_all_fields():
    html = render_page("settings.html", "normal")
    posts = [f for f in parse(html).forms if f["attrs"].get("action") == "/settings"]
    assert len(posts) == 1, "設定は1つのフォームで保存し、他カテゴリーの値を落とさない"
    names = field_names(posts[0])
    assert SETTINGS_FIELDS | {"csrf"} <= names
    for field in posts[0]["fields"]:
        if field.get("type") == "checkbox":
            assert field.get("value", "on") in {"on", "yes", "1", "true"}


def test_settings_fallback_preserves_submitted_values():
    form = {
        "monitor_interval_seconds": "5",
        "max_concurrent_recordings": "7",
        "display_timezone": "UTC",
        "recording_quality": "720p",
        "min_free_gib": "12",
        "retention_days": "30",
        "auto_delete_enabled": "on",
        "engine_1": "yt-dlp",
        "engine_2": "streamlink",
        "engine_3": "tiktoklive",
        "comments_globally_enabled": "",
        "comment_retention_days": "90",
        "auto_export_on_complete": "on",
        "comment_poll_seconds": "9",
        "reconnect_max_attempts": "4",
        "log_level": "DEBUG",
    }
    runtime = _fallback_runtime(form)
    assert runtime.monitor_interval_seconds == "5"
    assert runtime.max_concurrent_recordings == "7"
    assert runtime.display_timezone == "UTC"
    assert runtime.recording_quality == "720p"
    assert runtime.min_free_bytes == 12 * 1024**3
    assert runtime.auto_delete_enabled is True
    assert runtime.comments_globally_enabled is False
    assert runtime.tiktok_engine_order == ["yt-dlp", "streamlink", "tiktoklive"]
    assert runtime.log_level == "DEBUG"
    html = render(
        "settings.html",
        {**fx.settings_context("normal", error="監視間隔は30秒以上"), "runtime": runtime},
    )
    assert 'value="5"' in html and 'value="UTC"' in html
    assert re.search(r'name="engine_1"[\s\S]*?value="yt-dlp" selected', html)
    assert "監視間隔は30秒以上" in html


def test_settings_tab_accepts_only_known_panels():
    assert settings_tab({"active_tab": "settings-comments"}) == "settings-comments"
    assert settings_tab({"active_tab": "https://evil.example"}) == ""
    assert settings_tab({"active_tab": "settings-general#@evil"}) == ""
    assert settings_tab({}) == ""


def test_settings_restore_open_tab_after_validation_error():
    html = render(
        "settings.html",
        {**fx.settings_context("normal", error="監視間隔は30秒以上"), "active_tab": "settings-tiktok"},
    )
    assert re.search(r'id="tab-tiktok"[^>]*aria-selected="true"', html)
    assert re.search(r'id="tab-general"[^>]*aria-selected="false"', html)
    assert re.search(r'name="active_tab"[^>]*value="settings-tiktok"', html)


def test_environment_values_are_read_only_and_hide_secrets():
    html = render_page("settings.html", "normal")
    assert "/data/recordings" in html
    assert "再起動が必要" in html
    env_section = html[html.index('id="settings-environment"') :]
    env_section = env_section[: env_section.index("</section>")]
    assert "<input" not in env_section and "<select" not in env_section


def test_system_scan_report_shows_all_categories():
    html = render("system.html", fx.system_context("normal", with_report=True))
    for label in (
        "正常",
        "ファイル不足",
        "サイズ不一致",
        "破損の可能性",
        "未登録ファイル",
        "未確定ファイル",
    ):
        assert label in html
    assert "2026/11/unknown_029.mp4" in html
    scan = next(f for f in parse(html).forms if f["attrs"].get("action") == "/system/scan")
    assert "csrf" in field_names(scan)


def test_active_sessions_partial_polls():
    html = render_page("dashboard.html", "normal")
    assert 'id="dashboard-poll"' in html
    assert 'hx-trigger="every 30s, lr:refresh"' in html
    assert 'hx-select="#dashboard-live"' in html
    assert "30秒ごと" in html
    partial = render("partials/active_sessions.html", fx.dashboard_context("normal"))
    assert "<html" not in partial
    assert "さくら" in partial and "data-elapsed-since" in partial


def test_live_pages_refresh_without_settings_or_login():
    expected = {
        "dashboard.html": ("dashboard-poll", "dashboard-live"),
        "streamers.html": ("streamers-poll", "streamers-live"),
        "recordings.html": ("recordings-poll", "recording-results"),
        "comments.html": ("comments-poll", "results"),
        "recording_detail.html": ("recording-poll", "recording-live"),
        "system.html": ("system-poll", "system-live"),
    }
    for name, (poll_id, body_id) in expected.items():
        html = render_page(name, "normal")
        assert f'id="{poll_id}"' in html
        assert f'id="{body_id}"' in html
        if body_id != "results":
            assert f'id="{body_id}" class="c-live"' in html
        assert 'data-poll="30"' in html
        assert 'hx-trigger="every 30s, lr:refresh"' in html
        assert "30秒ごと" in html
    assert "data-poll" not in render_page("settings.html", "normal")
    assert "data-poll" not in render_page("login.html", "normal")
    assert "data-poll" not in render_page("streamer_form.html", "normal")
    system = render("system.html", fx.system_context("normal", with_report=True))

    class _Nest(HTMLParser):
        void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

        def __init__(self):
            super().__init__()
            self.stack: list[str] = []
            self.scan_in_live = False

        def handle_starttag(self, tag, attrs):
            found = dict(attrs).get("id", "")
            if found == "scan" and "system-live" in self.stack:
                self.scan_in_live = True
            if tag not in self.void:
                self.stack.append(found)

        def handle_endtag(self, tag):
            if tag not in self.void and self.stack:
                self.stack.pop()

    nest = _Nest()
    nest.feed(system)
    assert not nest.scan_in_live
    assert "ファイル照合" in system


def test_dashboard_errors_only_when_present():
    assert "直近の警告・エラー" not in render_page("dashboard.html", "normal")
    assert "直近の警告・エラー" in render_page("dashboard.html", "stress")


def test_flash_renders_as_toast_and_inline_alert():
    context = fx.dashboard_context("normal")
    context["flash"] = {"message": "削除できません", "tone": "danger"}
    html = render("dashboard.html", context)
    assert html.count("削除できません") == 2
    context["flash"] = {"message": "保存しました", "tone": "success"}
    assert render("dashboard.html", context).count("保存しました") == 1


def test_status_info_handles_unknown_codes():
    info = ui.status_info("rec", "SOMETHING_NEW")
    assert info["label"] and info["tone"]
    assert ui.status_info("live", "LIVE")["tone"] == "live"
    assert (
        ui.streamer_live_code(
            {"live_status": "OFFLINE", "monitoring_enabled": True, "last_checked_at": None}
        )
        == "CHECKING"
    )


def test_url_helpers_preserve_and_drop_params():
    request = fx.make_request("/comments", "q=a&page=2&user_id=b")
    assert parse_qs(urlsplit(ui.url_with(request, page=3)).query) == {
        "q": ["a"],
        "page": ["3"],
        "user_id": ["b"],
    }
    assert parse_qs(urlsplit(ui.url_with(request, drop=("q",))).query) == {
        "page": ["2"],
        "user_id": ["b"],
    }
    export = ui.export_url(request, "srt")
    assert (
        export.startswith("/comments/export?") and "format=srt" in export and "page" not in export
    )


def test_existing_routes_are_kept(tmp_path):
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        session_secret="x" * 40,
        recordings_dir=tmp_path / "rec",
        config_dir=tmp_path / "cfg",
        web_host="127.0.0.1",
        web_port=8080,
        display_timezone="Asia/Tokyo",
        log_level="INFO",
        cookie_secure=False,
        twitcasting_client_id="",
        twitcasting_client_secret="",
        twitcasting_access_token="",
        sign_api_key="",
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        streamlink_path="streamlink",
        ytdlp_path="yt-dlp",
        shutdown_timeout_seconds=5,
        app_home=tmp_path,
    )
    app = create_app(settings)
    routes = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", None) or ()
    }
    expected = {
        ("GET", "/"),
        ("GET", "/login"),
        ("POST", "/login"),
        ("POST", "/logout"),
        ("GET", "/streamers"),
        ("POST", "/streamers"),
        ("GET", "/streamers/{streamer_id}/edit"),
        ("POST", "/streamers/{streamer_id}/edit"),
        ("POST", "/streamers/{streamer_id}/toggle"),
        ("POST", "/streamers/{streamer_id}/delete"),
        ("GET", "/recordings"),
        ("GET", "/recordings/{session_id}"),
        ("POST", "/recordings/{session_id}/delete"),
        ("GET", "/recordings/segments/{segment_id}/download"),
        ("POST", "/recordings/{session_id}/delete-files"),
        ("GET", "/comments"),
        ("GET", "/comments/export"),
        ("GET", "/settings"),
        ("POST", "/settings"),
        ("GET", "/system"),
        ("POST", "/system/scan"),
        ("GET", "/partials/active-sessions"),
    }
    assert expected <= routes
