"""Screenshots of the running app (make serve) for the report: python scripts/screenshots.py [out_dir] [paths...]

Uses the installed Google Chrome through Playwright (pip install playwright; no browser download needed) and logs in
with PILOT_PASSCODE / ADMIN_PASSCODE.
"""

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("LL_BASE_URL", "http://127.0.0.1:8000")
PAGES = [
    ("landing", "/", False),
    ("company_pcyo", "/app/company/PCYO", True),
    ("company_pnbk", "/app/company/PNBK", True),
    ("signals", "/app/signals", True),
    ("universe", "/app/universe", True),
    ("backtest", "/app/backtest", True),
    ("how_it_works", "/how-it-works", False),
    ("warroom_list", "/warroom", True),
    ("costs", "/app/costs", True),
    ("safety", "/app/safety", True),
]


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "out/screenshots")
    only = set(sys.argv[2:])
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page = ctx.new_page()
        page.goto(f"{BASE}/login")
        page.fill("input[name=name]", "Andrew")
        page.fill("input[name=passcode]", os.environ.get("ADMIN_PASSCODE", "ledger-admin"))
        page.press("input[name=passcode]", "Enter")
        page.wait_for_load_state("networkidle")
        for name, path, _auth in PAGES:
            if only and name not in only and path not in only:
                continue
            page.goto(f"{BASE}{path}")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(out / f"{name}.png"), full_page=True)
            print(out / f"{name}.png")
        browser.close()


if __name__ == "__main__":
    main()
