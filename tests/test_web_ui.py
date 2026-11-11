"""UI の回帰テスト。データベースを使わず、テンプレートを固定データで描画して検証する。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
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
    for href in ("/", "/streamers", "/recordings", "/comments", "/speech", "/settings", "/system"):
        assert f'href="{href}"' in html
    assert any(f["attrs"].get("action") == "/logout" for f in collector.forms)
    assert 'aria-current="page"' in html
    assert 'hx-headers=\'{"X-CSRF-Token": "test-csrf-token"}\'' in html


def test_recording_mark_follows_active_recordings():
    idle = render_page("dashboard.html", "normal")
    assert 'id="favicon"' in idle
    assert 'data-idle="' in idle and "favicon-recording.svg" in idle
    assert 'id="app-mark"' in idle and 'data-recording="off"' in idle
    assert 'aria-label="待機中"' in idle
    assert "is-recording" not in idle
    assert 'id="recording-state-poll"' in idle
    context = fx.dashboard_context("normal")
    context["recording_active"] = True
    live = render("dashboard.html", context)
    assert 'class="c-logo is-recording"' in live
    assert 'data-recording="on"' in live
    assert 'aria-label="録画中"' in live
    partial = render("partials/recording_state.html", fx.base_context(recording_active=True))
    assert partial.count('hx-swap-oob="outerHTML"') == 2
    assert 'id="app-mark"' in partial and 'id="app-mark-bar"' in partial


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
    page = render_page("streamers.html", "normal")
    assert "監視を停止すると" not in page
    assert "スイッチは即時に保存されます。" in page
    assert 'title="@streamer_1"' in page and ">@streamer_1</span>" in page
    assert 'title="@caster_2"' in page and ">@caster_2</span>" in page
    assert "data-reorder-handle" not in page
    assert 'id="reorder-status"' in page
    assert fields == {"monitoring_enabled", "recording_enabled", "comments_enabled"}
    assert html.lstrip().startswith('<tr id="streamer-1"')
    poll = re.search(r'<div id="streamers-poll"[^>]*>', page)
    assert poll is not None
    assert 'hx-select="#streamers-live"' in poll.group(0)
    assert 'hx-disinherit="hx-select hx-select-oob"' in poll.group(0)
    for form in parse(page).forms:
        if "/toggle" not in form["attrs"].get("hx-post", ""):
            continue
        assert "hx-select" not in form["attrs"]
        assert "hx-select-oob" not in form["attrs"]


def test_streamer_order_toggle_shows_handles_only_for_custom():
    page = render_page("streamers.html", "normal")
    form = next(item for item in parse(page).forms if item["attrs"].get("action") == "/streamers/order")
    assert form["attrs"]["hx-select"] == "#streamers-live"
    assert form["attrs"]["hx-target"] == "#streamers-live"
    pressed = {button["value"]: button["aria-pressed"] for button in form["buttons"]}
    assert pressed == {"default": "true", "custom": "false"}
    assert "data-reorder-handle" not in page
    custom = render("streamers.html", fx.streamers_context("normal", order="custom"))
    assert custom.count("data-reorder-handle") == len(fx.streamer_rows("normal"))
    assert "i-grip-vertical" in custom
    custom_form = next(item for item in parse(custom).forms if item["attrs"].get("action") == "/streamers/order")
    custom_pressed = {button["value"]: button["aria-pressed"] for button in custom_form["buttons"]}
    assert custom_pressed["custom"] == "true" and custom_pressed["default"] == "false"


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
    assert "speech_words" in field_names(forms["/streamers/1/speech-words"])
    assert "さくら" in html
    listing = render_page("streamers.html", "normal")
    assert 'href="/streamers/1/edit#speech-words"' not in listing
    assert listing.count('href="/streamers/1/edit"') == 1
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
    query = "q=hello&user_id=u1&session_id=101&page=3"
    html = render("comments.html", fx.comments_context("stress", query))
    links = re.findall(r'href="(/comments[^"]*)"', html)
    paging = [link for link in links if "page=" in link and "export" not in link]
    assert paging
    for link in paging:
        assert "q=hello" in link and "user_id=u1" in link and "session_id=101" in link
    exports = [link for link in links if link.startswith("/comments/export")]
    assert {re.search(r"format=(\w+)", link).group(1) for link in exports} == {"srt"}
    for link in exports:
        assert "q=hello" in link and "user_id=u1" in link and "session_id=101" in link and "page=" not in link
    search = next(f for f in parse(html).forms if f["attrs"].get("hx-get") == "/comments")
    assert search["attrs"]["hx-target"] == "#results"
    assert search["attrs"]["hx-select"] == "#results"
    assert search["attrs"]["hx-push-url"] == "true"
    names = field_names(search)
    assert {"q", "user_id", "nickname", "session_id", "start", "end"} <= names
    assert "streamer_id" not in names and "platform" not in names


def test_viewers_switch_between_oldest_and_newest():
    comments = render_page("comments.html", "normal")
    assert 'aria-label="時系列"' in comments
    assert 'aria-current="page">新しい順' in comments
    assert "order=asc" in comments and "order=desc" in comments
    speech = render_page("speech_cues.html", "normal")
    assert 'aria-current="page">古い順' in speech
    assert "order=asc" in speech and "order=desc" in speech


def test_comment_index_lists_recordings_before_messages():
    html = render_page("comments_index.html", "normal")
    assert 'href="/comments?session_id=100"' in html
    assert "コメントを見る" in html
    assert "発話" not in html
    assert "コメント本文を検索" not in html
    filtered = render("comments_index.html", fx.comments_index_context("stress", "streamer_id=1"))
    assert 'value="1" selected' in filtered or 'value="1" selected' in filtered.replace("  ", " ")
    assert "streamer_id=1" in filtered


def test_recording_pagination_keeps_filters():
    html = render(
        "recordings.html", fx.recordings_context("stress", "streamer_id=1&status=FAILED&page=2")
    )
    paging = [link for link in re.findall(r'href="(/recordings\?[^"]*)"', html) if "page=" in link]
    assert paging
    for link in paging:
        assert "streamer_id=1" in link and "status=FAILED" in link
    assert re.search(r'value="FAILED"[^>]*selected', html)
    assert "#i-file-video" in html and "録画詳細</a>" in html
    assert "字幕生成中" in html and "字幕待機" in html
    assert "字幕失敗" in html and "字幕完了" in html
    assert 'href="/speech/cues?session_id=201"' in html
    assert 'aria-label="字幕 3 件を表示"' in html


def test_segment_download_only_when_file_exists():
    html = render_page("recording_detail.html", "normal")
    assert "最終確認" in html
    assert html.count("c-status--badge") == 3
    assert 'href="/recordings/segments/1/download"' in html
    assert "/segments/2/download" not in html
    assert "/segments/3/download" not in html
    assert 'href="/recordings/segments/1/speech"' in html
    assert 'href="/recordings/segments/1/ass"' in html
    assert ">統合 (ASS)</a>" in html
    assert "統合字幕のスタイルは Speech と Comment です" in html
    assert "/segments/2/speech" not in html
    assert 'href="/speech?session_id=101"' in html
    assert "音声字幕 (SRT)" in html
    assert 'href="/speech/cues?session_id=101"' in html
    assert "字幕を見る" in html
    assert ">コメント</a>" in html
    assert "<dt>重複</dt><dd" in html and ">19 件</dd>" in html
    assert "受信 / 保存" not in html
    assert 'action="/speech/' not in html
    assert "再生成" not in html
    assert "発話なし" not in html
    assert "モデルを読み込めませんでした" in html
    assert "ファイルなし" in html and "削除済み" in html
    for segment_id in (1, 2, 3):
        assert f"segment_id={segment_id}&amp;format=srt" in html
    assert re.search(
        r'c-table__secondary">〜 <time[^>]*>\d{2}/\d{2} \d{2}:\d{2}</time></span>', html
    )
    assert "〜 記録中" in html


def test_speech_page_shows_queue_download_and_regenerate():
    html = render_page("speech.html", "normal")
    assert ">SRT</a>" in html
    assert "SRT をダウンロード" not in html
    assert 'href="/recordings/segments/1/speech"' in html
    assert 'href="/speech/cues?segment_id=1"' in html
    assert ">字幕を見る</a>" in html
    assert 'class="c-row-actions"' in html
    assert ">本文</a>" not in html
    assert "/recordings/segments/2/speech" not in html
    assert 'action="/speech/1/retry"' in html
    assert 'action="/speech/2/retry"' in html
    assert 'action="/speech/4/retry"' not in html
    assert "モデルを読み込めませんでした" in html
    assert 'action="/speech/retry-failed"' in html
    assert "失敗 1 件を再生成" in html
    assert "処理中" in html


def test_speech_cues_read_like_comments():
    html = render_page("speech_cues.html", "normal")
    assert "字幕本文" in html
    assert "発話 1 です" in html
    assert "&lt;script&gt;alert" in html
    assert "<script>alert" not in html
    assert "00:00:00" in html
    assert 'hx-get="/speech/cues"' in html
    assert 'href="/recordings/segments/1/speech"' in html
    assert 'href="/recordings/101"' in html
    assert "すべてのファイル" in html


def test_speech_page_filter_and_disabled_notice():
    html = render_page("speech.html", "stress")
    assert "自動作成はオフです" in html
    assert 'value="failed" selected' in html
    assert "session_id=101" in html
    assert "失敗 3 件を再生成" in html


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
    "speech_subtitles_enabled",
    "speech_model",
    "speech_language",
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
        "comments_index.html": ("comments-index-poll", "comments-index"),
        "speech_cues.html": ("speech-cues-poll", "results"),
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
    assert "data-poll" not in render_page("login.html", "normal")
    for name in ("settings.html", "streamer_form.html"):
        html = render_page(name, "normal")
        assert 'id="recording-state-poll"' in html
        assert html.count('data-poll="30"') == 1
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


def test_dashboard_metrics_include_speech_in_progress():
    html = render_page("dashboard.html", "normal")
    assert 'class="c-metrics c-metrics--5"' in html
    assert "字幕生成中" in html
    assert "処理中 1 / 待機 3" in html
    assert 'class="c-metric is-active"' in html
    empty = render_page("dashboard.html", "empty")
    assert "処理中 0 / 待機 0" in empty
    assert empty.count('class="c-metric is-active"') == 0


def test_dashboard_errors_only_when_present():
    assert "直近24時間の警告・エラー" not in render_page("dashboard.html", "normal")
    assert "直近24時間の警告・エラー" in render_page("dashboard.html", "stress")


def test_flash_renders_as_toast_and_inline_alert():
    context = fx.dashboard_context("normal")
    context["flash"] = {"message": "削除できません", "tone": "danger"}
    html = render("dashboard.html", context)
    assert html.count("削除できません") == 2
    context["flash"] = {"message": "保存しました", "tone": "success"}
    assert render("dashboard.html", context).count("保存しました") == 1


def test_speech_summary_prefers_work_in_progress():
    assert ui.speech_summary([]) is None
    assert ui.speech_summary([None, "skipped"]) is None
    assert ui.speech_summary(["skipped"]) is None
    assert ui.speech_summary(["completed", "skipped"]) == "completed"
    assert ui.speech_summary(["completed", "failed"]) == "failed"
    assert ui.speech_summary(["failed", "pending"]) == "pending"
    assert ui.speech_summary(["completed", None]) == "pending"
    assert ui.speech_summary(["pending", "running"]) == "running"


def test_short_time_includes_the_date_on_the_same_day():
    now = datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc)
    today = datetime(2026, 11, 10, 3, 15, 9, tzinfo=timezone.utc)
    earlier = datetime(2026, 1, 2, 0, 5, tzinfo=timezone.utc)
    last_year = datetime(2025, 6, 1, 3, 0, tzinfo=timezone.utc)
    assert ui.short_time(today, "Asia/Tokyo", now) == "11/10 12:15"
    assert ui.short_time(earlier, "Asia/Tokyo", now) == "01/02 09:05"
    assert ui.short_time(last_year, "Asia/Tokyo", now) == "2025/06/01 12:00"


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
        ("POST", "/streamers/order"),
        ("POST", "/streamers/reorder"),
        ("POST", "/streamers/{streamer_id}/speech-words"),
        ("POST", "/streamers/{streamer_id}/toggle"),
        ("POST", "/streamers/{streamer_id}/delete"),
        ("GET", "/recordings"),
        ("GET", "/recordings/{session_id}"),
        ("POST", "/recordings/{session_id}/delete"),
        ("GET", "/recordings/segments/{segment_id}/download"),
        ("GET", "/recordings/segments/{segment_id}/speech"),
        ("GET", "/recordings/segments/{segment_id}/ass"),
        ("GET", "/speech"),
        ("GET", "/speech/cues"),
        ("POST", "/speech/retry-failed"),
        ("POST", "/speech/{segment_id}/retry"),
        ("POST", "/recordings/{session_id}/delete-files"),
        ("GET", "/comments"),
        ("GET", "/comments/export"),
        ("GET", "/settings"),
        ("POST", "/settings"),
        ("GET", "/system"),
        ("GET", "/system/scan"),
        ("POST", "/system/scan"),
        ("GET", "/partials/active-sessions"),
        ("GET", "/partials/recording-state"),
    }
    assert expected <= routes
