"""UI のスクリーンショット取得と、横はみ出し・コンソールエラーの検査（開発専用）。

    uv run --with playwright==1.57.0 python scripts/ui_screenshots.py BASE_URL OUT_DIR [--login USER:PASS] [--pages a,b] [--widths 1440,390] [--themes dark,light]

ページ指定は「名前=パス」をカンマ区切りで渡す。省略時は主要画面を対象にする。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

DEFAULT_PAGES = {
    "dashboard": "/",
    "streamers": "/streamers",
    "recordings": "/recordings",
    "comments": "/comments",
    "settings": "/settings",
    "system": "/system",
}

OVERFLOW_JS = """
() => {
  const doc = document.documentElement;
  const offenders = [];
  if (doc.scrollWidth > doc.clientWidth + 1) {
    for (const el of document.querySelectorAll('body *')) {
      const r = el.getBoundingClientRect();
      if (r.width && (r.right > doc.clientWidth + 1) && getComputedStyle(el).position !== 'fixed') {
        if (!el.closest('.c-table-wrap, .c-tabs, .c-segmented, .c-settings-nav, dialog:not([open]), [hidden]')) {
          offenders.push((el.className && el.className.baseVal === undefined ? el.className : el.tagName) + ' → ' + Math.round(r.right));
        }
      }
    }
  }
  const scrollers = [...document.querySelectorAll('.c-table-wrap, .c-tabs__list, .c-settings-nav')]
    .filter((el) => el.offsetParent && el.scrollWidth > el.clientWidth + 1)
    .map((el) => el.className + ' ' + el.scrollWidth + '/' + el.clientWidth);
  return {scrollWidth: doc.scrollWidth, clientWidth: doc.clientWidth, offenders: offenders.slice(0, 8), scrollers};
}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("out")
    parser.add_argument(
        "--login", help="USER:PASS。省略時は環境変数 LR_LOGIN_USER / LR_LOGIN_PASSWORD があれば使う"
    )
    parser.add_argument("--pages")
    parser.add_argument("--widths", default="1440,390")
    parser.add_argument("--themes", default="dark,light")
    parser.add_argument("--full", action="store_true", help="ページ全体を撮影する")
    args = parser.parse_args()

    pages = DEFAULT_PAGES
    if args.pages:
        pages = dict(item.split("=", 1) for item in args.pages.split(","))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    widths = [int(w) for w in args.widths.split(",")]
    themes = args.themes.split(",")
    problems: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for theme in themes:
            for width in widths:
                context = browser.new_context(
                    viewport={"width": width, "height": 900 if width > 500 else 844},
                    device_scale_factor=1,
                    color_scheme="dark" if theme == "dark" else "light",
                    locale="ja-JP",
                    timezone_id="Asia/Tokyo",
                )
                context.add_init_script(
                    f"try{{localStorage.setItem('lr-theme','{theme}')}}catch(e){{}}"
                )
                page = context.new_page()
                messages: list[str] = []
                page.on(
                    "console",
                    lambda msg: (
                        messages.append(f"{msg.type}: {msg.text}")
                        if msg.type in {"error", "warning"}
                        else None
                    ),
                )
                page.on("pageerror", lambda exc: messages.append(f"pageerror: {exc}"))
                login = args.login or (
                    f"{os.environ['LR_LOGIN_USER']}:{os.environ['LR_LOGIN_PASSWORD']}"
                    if os.environ.get("LR_LOGIN_USER")
                    else None
                )
                if login:
                    user, password = login.split(":", 1)
                    page.goto(args.base + "/login")
                    page.fill("#username", user)
                    page.fill("#password", password)
                    page.click("button[type=submit]")
                    page.wait_for_load_state("networkidle")
                for name, path in pages.items():
                    messages.clear()
                    page.goto(args.base + path, wait_until="networkidle")
                    page.wait_for_timeout(250)
                    overflow = page.evaluate(OVERFLOW_JS)
                    file = out / f"{name}-{width}-{theme}.png"
                    page.screenshot(path=str(file), full_page=args.full)
                    if overflow["scrollWidth"] > overflow["clientWidth"] + 1:
                        problems.append(
                            f"{name} {width} {theme}: overflow {json.dumps(overflow, ensure_ascii=False)}"
                        )
                    for scroller in overflow["scrollers"]:
                        problems.append(f"{name} {width} {theme}: 横スクロール {scroller}")
                    for message in messages:
                        problems.append(f"{name} {width} {theme}: {message}")
                    print(f"saved {file}")
                context.close()
        browser.close()
    print("\n".join(problems) if problems else "no overflow / console problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
