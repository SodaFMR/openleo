"""Real scientific editor/comparison checks: uv run --group browser python tests/browser_experiment.py."""

import argparse
import csv
import io
import json
import tempfile
import zipfile
from hashlib import sha256
from pathlib import Path
from threading import Thread

from playwright.sync_api import expect, sync_playwright

from openleo.app import create_server
from openleo.constellation import parse_constellation


def check_browser(executable=None):
    path = Path("examples/constellations/iridium_global_reference.json").resolve()
    raw = json.loads(path.read_text())
    raw = {**raw, "time_window": {**raw["time_window"], "stop_utc": "2026-09-10T12:02:00Z"}}
    scenario = parse_constellation(raw, path, sha256(path.read_bytes()).hexdigest())
    with create_server(scenario) as server, tempfile.TemporaryDirectory() as directory:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as driver:
                browser = driver.chromium.launch(
                    headless=True, **({"executable_path": executable} if executable else {})
                )
                page = browser.new_page(viewport={"width": 1440, "height": 1100})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                url = f"http://127.0.0.1:{server.server_port}"
                page.goto(url)
                page.locator("#scenario-open").click()
                expect(page.locator("#edit-station-name")).to_be_visible()

                def draft():
                    return json.loads(page.locator("#scenario-json").input_value())

                def change(selector, value):
                    page.locator(selector).fill(str(value))
                    page.locator(selector).press("Tab")

                change("#edit-station-name", "Madrid renamed")
                assert draft()["network"]["source_station"] == "Madrid renamed"
                assert draft()["propagation"]["station_heights_amsl_m"]["Madrid renamed"] == 650
                page.locator("#add-station").click()
                assert len(draft()["stations"]) == 5
                name = draft()["stations"][-1]["name"]
                assert draft()["propagation"]["station_heights_amsl_m"][name] == 0
                page.locator("#remove-station").click()
                assert len(draft()["stations"]) == 4
                assert name not in draft()["propagation"]["station_heights_amsl_m"]
                page.locator("#reset-scenario").click()
                page.locator("#editor-station").select_option("0")
                page.locator("#edit-hydrometeors").check()
                layers = draft()["propagation"]["hydrometeors"]["stations"]
                assert set(layers) == {station["name"] for station in draft()["stations"]}
                assert all(
                    layer["rain_rate_mm_h"] == layer["liquid_water_kg_m2"] == 0
                    for layer in layers.values()
                )
                change("#edit-cloud", 0.2)
                assert (
                    draft()["propagation"]["hydrometeors"]["stations"]["Tromso"][
                        "liquid_water_kg_m2"
                    ]
                    == 0
                )
                change("#edit-rain-top", 5000)
                change("#edit-rain", 2)
                change("#edit-height", 800)
                assert draft()["propagation"]["station_heights_amsl_m"]["Madrid"] == 650
                change("#edit-rain-top", 20001)
                assert not page.locator("#edit-rain-top").evaluate("input => input.checkValidity()")
                assert (
                    draft()["propagation"]["hydrometeors"]["stations"]["Madrid"][
                        "rain_top_height_amsl_m"
                    ]
                    == 5000
                )
                change("#edit-rain-top", 5000)
                page.locator("#run-scenario").click()
                expect(page.locator("#scenario-dialog")).not_to_be_visible(timeout=60000)
                document = json.loads(page.locator("#experiment-data").text_content())
                assert document["schema_version"] == "3"
                expect(page.locator("#hydrometeor-values")).to_be_visible()
                station = int(page.locator("#station-select").input_value())
                satellite = int(page.locator("#satellite-select").input_value())
                link = next(
                    item
                    for item in document["links"][0]
                    if item["station_index"] == station and item["satellite_index"] == satellite
                )
                for element, field in [
                    ("link-cloud-loss", "cloud_attenuation_db"),
                    ("link-rain-loss", "rain_attenuation_db"),
                    ("link-total-loss", "total_atmospheric_attenuation_db"),
                ]:
                    actual = float(
                        page.locator("#" + element)
                        .inner_text()
                        .removesuffix(" dB")
                        .replace(",", "")
                    )
                    assert abs(actual - link[field]) < 0.000501
                for chart in ("rate", "elevation", "doppler"):
                    assert page.locator(f"#{chart}-chart").evaluate("canvas => canvas.width") > 200
                    expect(page.locator(f"#{chart}-chart-value")).not_to_have_text("—")
                with page.expect_download() as downloaded:
                    page.locator("#download-links").click()
                csv_path = Path(directory) / "links.csv"
                downloaded.value.save_as(csv_path)
                rows = list(csv.DictReader(io.StringIO(csv_path.read_text())))
                row = next(
                    row
                    for row in rows
                    if int(row["station_index"]) == station
                    and int(row["satellite_index"]) == satellite
                )
                assert float(row["cloud_attenuation_db"]) == link["cloud_attenuation_db"]
                assert float(row["rain_path_length_m"]) == link["rain_path_length_m"]

                # An uncomputed editor change must not affect the comparison source.
                page.locator("#scenario-open").click()
                changed = {**document["scenario"], "name": "Uncomputed draft"}
                page.locator("#scenario-json").fill(json.dumps(changed))
                page.locator("#scenario-dialog .close-dialog").click()
                page.locator("#comparison-open").click()
                expect(page.locator("#comparison-scenario")).to_have_text(
                    document["scenario"]["name"]
                )
                assert page.locator("#packet-mode option[value=link]").evaluate(
                    "option => option.disabled"
                )
                assert page.locator("#packet-mode option[value=network]").evaluate(
                    "option => option.disabled"
                )
                with page.expect_response(url + "/api/study", timeout=60000) as response:
                    page.locator("#run-comparison").click()
                assert response.value.ok, response.value.text()
                result = response.value.json()
                expect(page.locator("#comparison-results")).to_be_visible()
                assert (
                    page.locator("#comparison-report").get_attribute("href") == result["report_url"]
                )
                assert (
                    page.locator("#comparison-archive").get_attribute("href")
                    == result["archive_url"]
                )
                assert (
                    json.loads(page.locator("#comparison-data").text_content()) == result["summary"]
                )
                assert (
                    len(result["summary"]["cases"])
                    == page.locator("#comparison-case-list article").count()
                )
                report = page.request.get(url + result["report_url"])
                assert report.ok
                prefix = result["report_url"].removesuffix("index.html")
                numerical = page.request.get(url + prefix + "experiment-study.json")
                assert numerical.ok and numerical.json() == result["summary"]
                for case in result["summary"]["cases"]:
                    assert case["bundle_path"] + "/explorer.html" in report.text()
                    case_document = page.request.get(
                        url + prefix + case["bundle_path"] + "/experiment.json"
                    )
                    assert case_document.ok
                    assert (
                        case_document.json()["provenance"]["scenario_sha256"]
                        == case["scenario_sha256"]
                    )
                archive = page.request.get(url + result["archive_url"])
                assert archive.ok
                with zipfile.ZipFile(io.BytesIO(archive.body())) as zipped:
                    summaries = [
                        json.loads(zipped.read(name))
                        for name in zipped.namelist()
                        if name.endswith("experiment-study.json")
                    ]
                    assert result["summary"] in summaries, zipped.namelist()
                prior = page.locator("#comparison-results").inner_text()
                page.route("**/api/study", lambda route: route.abort("failed"))
                page.locator("#run-comparison").click()
                expect(page.locator("#comparison-error")).to_be_visible()
                assert page.locator("#comparison-results").inner_text() == prior
                assert json.loads(page.locator("#experiment-data").text_content()) == document
                for width in (390, 768, 1440):
                    page.set_viewport_size({"width": width, "height": 1000})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    assert page.locator("#comparison-dialog").evaluate(
                        "dialog => dialog.scrollWidth <= dialog.clientWidth + 1"
                    )
                page.locator("#comparison-dialog .close-dialog").click()
                with page.expect_download() as downloaded:
                    page.locator("#download-report").click()
                offline_path = Path(directory) / "explorer.html"
                downloaded.value.save_as(offline_path)
                offline = browser.new_context(viewport={"width": 390, "height": 844})
                offline.set_offline(True)
                offline_page = offline.new_page()
                requests = []
                offline_page.on("request", lambda request: requests.append(request.url))
                offline_page.goto(offline_path.as_uri())
                offline_page.locator("#comparison-open").click()
                expect(offline_page.locator("#run-comparison")).to_be_disabled()
                expect(offline_page.locator("#comparison-results")).to_be_visible()
                expect(offline_page.locator("#comparison-report")).not_to_be_visible()
                offline_page.wait_for_timeout(100)
                assert not any("/api/" in request for request in requests), requests
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()
    print(
        "Scientific browser checks passed: guided maps, schema 3, actual study/archive, error preservation, offline, 390/768/1440."
    )


def check_sampling_window(executable=None):
    path = Path("examples/constellations/iridium_global_reference.json").resolve()
    raw = json.loads(path.read_text())
    raw = {
        **raw,
        "time_window": {
            **raw["time_window"],
            "stop_utc": "2026-09-10T12:02:00Z",
            "step_s": 90,
        },
    }
    scenario = parse_constellation(raw, path, sha256(path.read_bytes()).hexdigest())
    with create_server(scenario) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as driver:
                browser = driver.chromium.launch(
                    headless=True, **({"executable_path": executable} if executable else {})
                )
                page = browser.new_page()
                url = f"http://127.0.0.1:{server.server_port}"
                page.goto(url)
                page.locator("#comparison-open").click()
                expect(page.locator("#study-duration")).to_have_value("90")
                expect(page.locator("#study-duration")).to_have_attribute("min", "90")
                expect(page.locator("#study-start")).to_have_attribute("max", "30")
                page.locator("#study-duration").fill("89")
                page.locator("#study-duration").press("Tab")
                assert not page.locator("#study-duration").evaluate(
                    "input => input.checkValidity()"
                )
                page.locator("#study-duration").fill("90")
                page.locator("#study-duration").press("Tab")
                page.locator("#study-start").fill("31")
                page.locator("#study-start").press("Tab")
                expect(page.locator("#study-duration")).to_have_attribute("max", "89")
                expect(page.locator("#run-comparison")).to_be_disabled()
                page.locator("#study-start").fill("30")
                page.locator("#study-start").press("Tab")
                expect(page.locator("#run-comparison")).to_be_enabled()
                with page.expect_response(url + "/api/study", timeout=60000) as response:
                    page.locator("#run-comparison").click()
                assert response.value.ok, response.value.text()
                summary = response.value.json()["summary"]
                assert summary["configuration"]["start_offset_s"] == 30
                assert summary["configuration"]["duration_s"] == 90
                assert summary["scenario"]["time_window"]["step_s"] == 90
                assert all(
                    case["duration_s"] == 90 and case["sample_count"] == 2
                    for case in summary["cases"]
                )
                page.locator("#comparison-dialog .close-dialog").click()
                page.locator("#scenario-open").click()
                coarse = {
                    **raw,
                    "time_window": {
                        **raw["time_window"],
                        "stop_utc": "2026-09-10T14:00:00Z",
                        "step_s": 4000,
                    },
                }
                page.locator("#scenario-json").fill(json.dumps(coarse))
                page.locator("#run-scenario").click()
                expect(page.locator("#scenario-dialog")).not_to_be_visible(timeout=60000)
                requests = []
                page.on("request", lambda request: requests.append(request.url))
                page.locator("#comparison-open").click()
                expect(page.locator("#run-comparison")).to_be_disabled()
                expect(page.locator("#comparison-sampling")).to_contain_text("sampling interval")
                expect(page.locator("#comparison-sampling")).to_contain_text("recompute")
                page.locator("#comparison-form").evaluate(
                    "form => form.dispatchEvent(new Event('submit', {cancelable: true}))"
                )
                assert not any(request.endswith("/api/study") for request in requests), requests
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()
    print(
        "Sampling browser regression passed: step 90 default/minimum, offset bounds, actual aligned study, step 4000 disabled."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable")
    args = parser.parse_args()
    check_sampling_window(args.executable)
    check_browser(args.executable)
