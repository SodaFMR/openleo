"""Offline comparison figures generated only from completed experiment summaries."""

from html import escape
from math import isfinite
from pathlib import PurePosixPath

from openleo.fidelity_report import _CSS


def _number(value):
    if value is None:
        return "—"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError("report values must be finite numbers")
    return f"{value:.6g}"


def _link(path):
    if not isinstance(path, str) or "\\" in path:
        raise ValueError("report links must be portable relative paths")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or any(part in ("..", ".") for part in path.split("/")) or ":" in path:
        raise ValueError("report links must remain inside the bundle")
    return escape(path, quote=True)


def _table(headings, rows):
    return (
        '<div class="table-wrap"><table><thead><tr>'
        + "".join(f'<th scope="col">{escape(heading)}</th>' for heading in headings)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in row) + "</tr>"
            for row in rows
        )
        + "</tbody></table></div>"
    )


def _bars(title, values, unit):
    maximum = max((value for _, value in values), default=0)
    maximum = maximum or 1
    height = 55 + len(values) * 35
    bars = []
    for index, (label, value) in enumerate(values):
        _number(value)
        y = 30 + index * 35
        width = max(0, value / maximum * 285)
        bars.append(
            f'<text x="8" y="{y + 14}" font-size="12">{escape(label)}</text>'
            f'<rect x="145" y="{y}" width="{width:.6g}" height="20" fill="#2563a6"/>'
            f'<text x="445" y="{y + 14}" font-size="12">{_number(value)}</text>'
        )
    return (
        f"<figure><figcaption>{escape(title)} ({escape(unit)})</figcaption>"
        f'<svg role="img" aria-label="{escape(title, quote=True)}" viewBox="0 0 560 {height}">'
        f'<line x1="145" y1="20" x2="145" y2="{height - 20}" stroke="#506070"/>'
        + "".join(bars)
        + "</svg></figure>"
    )


def _packet_section(cases):
    records = []
    downloads = []
    for case in cases:
        for packet in case["packets"]:
            summary = packet["summary"]
            flows = summary.get("flow_metrics", summary.get("directions", []))
            for row in flows:
                records.append(
                    [
                        case["id"],
                        packet["routing_model"] or "selected link",
                        row["direction"],
                        row.get("flow", 0),
                        row["offered_packets"],
                        row["received_in_window_packets"],
                        _number(row["goodput_bps"]),
                        _number(row["mean_delay_s"]),
                        row["received_after_window_packets"],
                        row["end_of_window_drop_packets"],
                    ]
                )
            path = _link(packet["bundle_path"] + "/" + packet["summary_file"])
            downloads.append(
                f'<li><a href="{path}">{escape(case["id"])} · '
                f"{escape(packet['routing_model'] or 'selected link')} packet evidence</a></li>"
            )
            downloads.append(
                f'<li><a href="{_link(packet["bundle_path"] + "/packets.csv")}">'
                f"{escape(case['id'])} · {escape(packet['routing_model'] or 'selected link')} packet CSV</a></li>"
            )
            if packet["mode"] == "network":
                downloads.append(
                    f'<li><a href="{_link(packet["bundle_path"] + "/hops.csv")}">'
                    f"{escape(case['id'])} · {escape(packet['routing_model'])} hop CSV</a></li>"
                )
    if not records:
        return "<p>No packet backend was requested. Reference rates are not delivered goodput.</p>"
    return (
        "<p>Simulated within-window UDP payload goodput and one-way delivered-packet delay; "
        "not measured performance. After-window deliveries and censored queues are separate.</p>"
        + _table(
            [
                "Case",
                "Route",
                "Direction",
                "Flow",
                "Offered",
                "Received in window",
                "Goodput (bit/s)",
                "Mean delay (s)",
                "Received after window",
                "Window-censored queue",
            ],
            records,
        )
        + "<ul>"
        + "".join(downloads)
        + "</ul>"
    )


def render_experiment_report(summary: dict) -> str:
    """Render the successful experiment, preserving units and evidence boundaries."""
    if summary.get("kind") != "openleo.experiment" or summary.get("schema_version") != "1":
        raise ValueError("unsupported experiment summary")
    cases = summary["cases"]
    sections = []
    for station in cases[0]["station_metrics"]:
        index = station["station_index"]
        rows = [(case["id"], case["station_metrics"][index]) for case in cases]
        sections.append(
            '<section class="panel"><h2>' + escape(station["name"]) + "</h2>"
            '<div class="charts">'
            + _bars(
                "Mean best-link reference rate",
                [(name, row["mean_best_rate_bps"]) for name, row in rows],
                "bit/s",
            )
            + _bars(
                "Visible RF-outage duration",
                [(name, row["rf_outage_duration_s"]) for name, row in rows],
                "s",
            )
            + "</div></section>"
        )
    routes = [
        [
            case["id"],
            row["routing_model"],
            _number(row["mean_bottleneck_bps"]),
            _number(row["connected_duration_s"]),
            row["route_changes"],
        ]
        for case in cases
        for row in case["route_metrics"]
    ]
    links = "".join(
        f'<li><a href="{_link(case["bundle_path"] + "/explorer.html")}">'
        f"{escape(case['label'])} · open 3D experiment</a></li>"
        for case in cases
    )
    limits = "".join(f"<li>{escape(text)}</li>" for text in summary["limitations"])
    name = escape(summary["name"])
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "script-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
        f"<title>{name} · OpenLEO comparison</title><style>{_CSS}</style></head><body>"
        f"<header><h1>{name}</h1><p>Aligned propagation and packet experiments · "
        f"{_number(summary['configuration']['duration_s'])} s comparison window</p></header><main>"
        '<p class="notice">Declared environmental and terminal assumptions; model-derived results, '
        "not measured weather or commercial service performance.</p>"
        + "".join(sections)
        + '<section class="panel"><h2>Snapshot routing</h2>'
        + _table(
            [
                "Case",
                "Routing model",
                "Mean bottleneck reference rate (bit/s)",
                "Connected duration (s)",
                "Route changes",
            ],
            routes,
        )
        + '</section><section class="panel"><h2>Packet delivery</h2>'
        + _packet_section(cases)
        + '</section><section class="panel"><h2>Reproduce and inspect</h2><ul>'
        + links
        + '</ul><p><a href="experiment-study.json">Full numerical summary</a> · '
        '<a href="stations.csv">Station CSV</a> · <a href="routes.csv">Route CSV</a> · '
        '<a href="manifest.json">Artifact fingerprints</a></p>'
        "<p>The inputs folder contains the archived orbit, scenario and experiment settings.</p>"
        '</section><section class="panel"><h2>Interpretation and limits</h2><ul>'
        + limits
        + "</ul></section></main></body></html>\n"
    )
