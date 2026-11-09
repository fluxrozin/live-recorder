"""axe-core による WCAG 2.2 A/AA の自動検査（開発専用）。プレビューサーバーに対して実行する。

uv run python scripts/ui_preview.py &
uv run --with playwright==1.57.0 --with axe-playwright-python python scripts/ui_a11y.py [BASE_URL]
"""

from __future__ import annotations

import sys

from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import sync_playwright

PAGES = [
    "/?scenario=stress",
    "/?scenario=empty",
    "/streamers?scenario=stress",
    "/streamers/1/edit",
    "/recordings?scenario=stress",
    "/recordings/1?scenario=stress",
    "/comments?scenario=stress",
    "/settings?error=1",
    "/system?scenario=stress&report=1",
    "/login?scenario=stress",
]
TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
    axe = Axe()
    failures = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for theme in ("dark", "light"):
            for width in (1440, 390):
                context = browser.new_context(viewport={"width": width, "height": 900})
                context.add_init_script(f"localStorage.setItem('lr-theme','{theme}')")
                page = context.new_page()
                for path in PAGES:
                    page.goto(base + path, wait_until="networkidle")
                    result = axe.run(page, options={"runOnly": {"type": "tag", "values": TAGS}})
                    for violation in result.response["violations"]:
                        failures += 1
                        node = violation["nodes"][0]
                        print(
                            f"{theme} {width} {path}: {violation['id']} ({len(violation['nodes'])}) {node['target']}"
                        )
                context.close()
        browser.close()
    print("違反なし" if not failures else f"{failures} 件の違反")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
