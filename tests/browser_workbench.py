"""Real-browser workbench checks, run separately from the core Python suite.

uv run --group browser python tests/browser_workbench.py
Use --executable /usr/bin/chromium for a locally installed Chromium on Arch.
Use --scenario examples/constellations/iridium_global_reference.json for atmosphere checks.
"""

import argparse
import csv
import json
import tempfile
from hashlib import sha256
from math import isclose
from pathlib import Path
from threading import Thread

from playwright.sync_api import expect, sync_playwright

from openleo.app import create_server
from openleo.constellation import load_constellation


def check_propagation(page, document, directory):
    if document["schema_version"] == "1":
        expect(page.locator("#propagation-model")).to_have_text("Free space")
        expect(page.locator("#propagation-values")).not_to_be_visible()
        return
    expect(page.locator("#propagation-model")).to_have_text("ITU reference atmosphere")
    expect(page.locator("#propagation-values")).to_be_visible()
    station = int(page.locator("#station-select").input_value())
    satellite = int(page.locator("#satellite-select").input_value())
    link = next(
        link
        for link in document["links"][0]
        if link["station_index"] == station and link["satellite_index"] == satellite
    )
    metrics = (
        ("link-gas-loss", "gaseous_attenuation_db", 1, " dB", 0.000501),
        ("link-apparent-elevation", "apparent_elevation_deg", 1, "°", 0.000501),
        ("link-excess-delay", "atmospheric_excess_delay_s", 1e6, " µs", 0.000501),
        ("link-cn0", "cn0_db_hz", 1, " dBHz", 0.050001),
        ("link-delay", "delay_s", 1e3, " ms", 0.005001),
    )
    for element, field, scale, unit, tolerance in metrics:
        rendered = page.locator("#" + element).inner_text()
        assert rendered.endswith(unit), rendered
        actual = float(rendered.removesuffix(unit).replace(",", ""))
        assert isclose(actual, link[field] * scale, abs_tol=tolerance), (field, rendered)
    rate, unit = page.locator("#link-rate").inner_text().split()
    scale = {"bit/s": 1, "kbit/s": 1e3, "Mbit/s": 1e6, "Gbit/s": 1e9}[unit]
    assert isclose(float(rate.replace(",", "")) * scale, link["rate_bps"], abs_tol=0.005001 * scale)
    assert isclose(link["cn0_db_hz"], link["free_space_cn0_db_hz"] - link["gaseous_attenuation_db"])
    assert isclose(link["delay_s"], link["geometric_delay_s"] + link["atmospheric_excess_delay_s"])
    with page.expect_download() as saved:
        page.locator("#download-links").click()
    csv_path = Path(directory) / "links.csv"
    saved.value.save_as(csv_path)
    with csv_path.open(newline="") as stream:
        selected = next(
            row
            for row in csv.DictReader(stream)
            if row["timestamp_utc"] == document["timestamps_utc"][0]
            and int(row["station_index"]) == station
            and int(row["satellite_index"]) == satellite
        )
    for field in (
        "gaseous_dry_attenuation_db",
        "gaseous_water_attenuation_db",
        "gaseous_attenuation_db",
        "free_space_cn0_db_hz",
        "geometric_delay_s",
        "atmospheric_excess_delay_s",
        "apparent_elevation_deg",
    ):
        assert float(selected[field]) == link[field], field


def check_browser(
    executable=None, screenshot=None, scenario_path="examples/constellations/iridium_global.json"
):
    scenario = load_constellation(scenario_path)
    with create_server(scenario) as server, tempfile.TemporaryDirectory() as directory:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as driver:
                launch = {"headless": True}
                if executable:
                    launch["executable_path"] = executable
                browser = driver.chromium.launch(**launch)
                page = browser.new_page(viewport={"width": 1440, "height": 1100})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                url = f"http://127.0.0.1:{server.server_port}/"
                page.goto(url)
                expect(page.locator("#satellite-count")).to_have_text("80")
                expect(page.locator("#station-count")).to_have_text("4")
                expect(page.locator("#session-badge")).to_have_text("LOCAL SESSION")
                expect(page.locator("#link-status")).to_have_text("RF USABLE")
                assert page.locator("#network-rows tr").count() == 3
                assert page.locator("#globe").evaluate("canvas => canvas.width") > 200
                document = json.loads(page.locator("#experiment-data").text_content())
                if screenshot:
                    Path(screenshot).parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=screenshot, full_page=True)

                check_propagation(page, document, directory)
                page.evaluate("scrollTo(0, 0)")

                page.locator("#next-sample").click()
                expect(page.locator("#sample-position")).to_have_text("2 / 121")
                page.locator("#timeline").evaluate(
                    "input => { input.value = '20'; input.dispatchEvent(new Event('input')); }"
                )
                expect(page.locator("#current-time")).to_contain_text("12:20:00")
                page.locator("#station-select").select_option("2")
                expect(page.locator("#selected-station")).to_have_text("Singapore")
                page.locator("#globe").focus()
                before = page.locator("#camera-position").inner_text()
                page.keyboard.press("ArrowRight")
                assert page.locator("#camera-position").inner_text() != before

                # User edits a location and terminal assumption; views and exports must update.
                page.locator("#scenario-open").click()
                raw = json.loads(page.locator("#scenario-json").input_value())
                changed = {
                    **raw,
                    "name": "Browser experiment",
                    "radio_link": {
                        **raw["radio_link"],
                        "eirp_dbw": -100.0,
                    },
                }
                page.locator("#scenario-json").fill(json.dumps(changed))
                page.locator("#run-scenario").click()
                expect(page.locator("#scenario-dialog")).not_to_be_visible(timeout=30000)
                expect(page.locator("#experiment-name")).to_have_text("Browser experiment")
                expect(page.locator("#usable-count")).to_contain_text("0 RF-usable")
                with page.expect_download() as saved:
                    page.locator("#download-json").click()
                json_path = Path(directory) / "experiment.json"
                saved.value.save_as(json_path)
                exported = json.loads(json_path.read_text())
                assert exported["scenario"]["radio_link"]["eirp_dbw"] == -100.0
                source_json = exported["provenance"]["scenario_canonical_json"]
                assert (
                    sha256(source_json.encode("utf-8")).hexdigest()
                    == exported["provenance"]["scenario_sha256"]
                )
                assert json.loads(source_json) == exported["scenario"]
                assert all(link["rate_bps"] == 0 for frame in exported["links"] for link in frame)

                # Invalid edits must remain an error in the editor, preserving the successful run.
                page.locator("#scenario-open").click()
                page.locator("#scenario-json").fill("{")
                page.locator("#run-scenario").click()
                expect(page.locator("#editor-error")).to_be_visible()
                expect(page.locator("#experiment-name")).to_have_text("Browser experiment")
                page.locator("#scenario-dialog .close-dialog").click()

                with page.expect_download() as saved:
                    page.locator("#download-report").click()
                report = Path(directory) / "explorer.html"
                saved.value.save_as(report)
                assert '"token":""' in report.read_text()

                # The exported report must start and work from disk with all network blocked.
                offline = browser.new_context(viewport={"width": 390, "height": 844})
                offline.set_offline(True)
                offline_page = offline.new_page()
                offline_page.on("pageerror", lambda error: errors.append(str(error)))
                offline_page.goto(report.as_uri())
                expect(offline_page.locator("#session-badge")).to_have_text("OFFLINE REPORT")
                expect(offline_page.locator("#experiment-name")).to_have_text("Browser experiment")
                expect(offline_page.locator("#propagation-model")).to_have_text(
                    "ITU reference atmosphere"
                    if document["schema_version"] == "2"
                    else "Free space"
                )
                offline_page.locator("#next-sample").click()
                expect(offline_page.locator("#sample-position")).to_have_text("2 / 121")
                assert offline_page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth + 1"
                )
                offline_page.locator("#scenario-open").click()
                expect(offline_page.locator("#run-scenario")).to_be_disabled()
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()
    print("Browser checks passed: live edits, exports, offline report, geometry controls, mobile.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable")
    parser.add_argument("--screenshot")
    parser.add_argument(
        "--scenario",
        default="examples/constellations/iridium_global.json",
        help="Iridium global scenario, with free-space or reference-atmosphere propagation",
    )
    args = parser.parse_args()
    check_browser(args.executable, args.screenshot, args.scenario)
