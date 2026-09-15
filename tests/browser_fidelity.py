"""Exercise a completed fidelity report and its linked 3D report in Chromium."""

import argparse
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from openleo.fidelity_io import load_fidelity_result


def check_browser(directory, executable=None, screenshot=None):
    root = Path(directory).resolve()
    summary = load_fidelity_result(root)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=executable, headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "console",
                lambda message: errors.append(message.text) if message.type == "error" else None,
            )
            page.goto((root / "index.html").as_uri())
            expect(page.get_by_role("heading", level=1)).to_have_text(summary["name"])
            links = page.get_by_role("link", name="Open 3D child report")
            expect(links).to_have_count(len(summary["runs"]))
            identities = {
                (row["case_id"], row["station_index"]) for row in summary["station_metrics"]
            }
            expect(page.locator("svg")).to_have_count(2 * len(identities))
            for chart in page.locator("svg").all():
                assert chart.locator(".axis-value").count() >= 1
            assert page.get_by_text(
                "Whole-window mean best-link reference rate (bit/s)", exact=True
            ).count() == len(identities)
            assert page.locator("script").count() == 0
            if screenshot:
                page.screenshot(path=screenshot, full_page=True)
            for width in (390, 768):
                page.set_viewport_size({"width": width, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
                expect(links.first).to_be_visible()
            page.set_viewport_size({"width": 1440, "height": 1000})
            expected = root / summary["runs"][0]["bundle_path"] / "explorer.html"
            links.first.click()
            page.wait_for_url(expected.as_uri())
            expect(page.locator("#session-badge")).to_have_text("OFFLINE REPORT")
            assert page.locator("#globe").evaluate("canvas => canvas.width > 200")
            page.locator("#next-sample").click()
            expect(page.locator("#sample-position")).to_contain_text("2 /")
            assert not errors, errors
            print(
                "Fidelity browser checks passed: plots, tables, mobile, source links and 3D drill-down."
            )
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("--executable")
    parser.add_argument("--screenshot")
    args = parser.parse_args()
    check_browser(args.directory, args.executable, args.screenshot)
