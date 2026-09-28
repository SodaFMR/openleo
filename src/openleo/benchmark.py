"""Frozen, bounded release benchmarks built from aligned experiment bundles."""

from __future__ import annotations

import json
import platform
from collections import defaultdict
from dataclasses import asdict, dataclass, fields, replace
from hashlib import sha256
from html import escape
from importlib.metadata import version
from itertools import combinations, product
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from skyfield.api import load

from openleo.catalog import load_catalog
from openleo.constellation import (
    MAX_LINK_SAMPLES,
    _bounded_grid,
    load_constellation,
    parse_constellation,
    scenario_document,
)
from openleo.experiment import ExperimentConfig, _manifest, _windowed, _write_json, run_experiment
from openleo.experiment_report import _bars, _link, _number, _table
from openleo.fidelity_report import _CSS
from openleo.fidelity_study import _check_output, _read, _unique_object, metric_csv
from openleo.input import _string
from openleo.network_replay import NetworkReplayConfig, _offers, _validate_config
from openleo.orbit import MAX_ORBIT_BYTES
from openleo.packet_replay import _integer, _microseconds
from openleo.packet_replay import _number as _bounded_number
from openleo.workbench import _read as _read_artifact


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    schema_version: str
    name: str
    scenario_path: str
    frequencies_hz: tuple[float, ...]
    sampling_steps_s: tuple[float, ...]
    refinements: tuple[int, ...]
    traffic_loads_bps: tuple[float, ...]
    start_offset_s: float
    duration_s: float
    packet_size_bytes: int
    queue_packets: int
    flows_per_direction: int
    acquisition_delay_s: float
    seed: int

    def __post_init__(self):
        if self.schema_version != "1":
            raise ValueError("schema_version must be '1'")
        _string(self.name, "name")
        _string(self.scenario_path, "scenario_path")
        if (
            Path(self.scenario_path).is_absolute()
            or "\\" in self.scenario_path
            or ":" in self.scenario_path
        ):
            raise ValueError("scenario_path must be a relative local filename")
        limits = {
            "frequencies_hz": 2,
            "sampling_steps_s": 3,
            "refinements": 2,
            "traffic_loads_bps": 2,
        }
        for key, maximum in limits.items():
            values = getattr(self, key)
            if not isinstance(values, tuple) or not 1 <= len(values) <= maximum:
                raise ValueError(f"{key} must contain 1..{maximum} values")
            for value in values:
                if key == "refinements":
                    _integer(value, key, 1, 4)
                    if value not in (1, 2, 4):
                        raise ValueError("refinements must be 1, 2 or 4")
                else:
                    bounds = {
                        "frequencies_hz": (1e9, 200e9),
                        "sampling_steps_s": (0, 3600),
                        "traffic_loads_bps": (1, 1e9),
                    }
                    _bounded_number(value, key, *bounds[key], positive=True)
                    if key == "sampling_steps_s":
                        _microseconds(value, key)
            if len(set(values)) != len(values):
                raise ValueError(f"{key} values must be unique")
        if len(tuple(product(*(getattr(self, key) for key in limits)))) > 24:
            raise ValueError("benchmark matrix exceeds 24 experiments")
        for offered in self.traffic_loads_bps:
            config = self.experiment_config(offered, network=True)
            _, duration_us, _, interval_ns = _validate_config(
                NetworkReplayConfig(
                    **{
                        key: value
                        for key, value in asdict(config).items()
                        if key not in ("packet_mode", "station_name", "norad_id")
                    }
                )
            )
            if (
                sum(
                    count
                    for _, count in _offers(
                        duration_us * 1000, interval_ns, self.flows_per_direction
                    )
                )
                * 2
                > 200_000
            ):
                raise ValueError("benchmark traffic exceeds the 200000 offered packet budget")

    def experiment_config(self, offered: float, *, network: bool) -> ExperimentConfig:
        return ExperimentConfig(
            packet_mode="network" if network else "none",
            start_offset_s=self.start_offset_s,
            duration_s=self.duration_s,
            offered_load_bps=offered,
            packet_size_bytes=self.packet_size_bytes,
            queue_packets=self.queue_packets,
            seed=self.seed,
            flows_per_direction=self.flows_per_direction if network else 1,
            acquisition_delay_s=self.acquisition_delay_s if network else 0,
        )


def load_benchmark_config(path: str | Path) -> BenchmarkConfig:
    """Reject duplicate keys, oversized inputs, unknown fields and unsupported domains."""
    return _parse_config(_read(Path(path)))


def _parse_config(source: bytes) -> BenchmarkConfig:
    try:
        raw = json.loads(source.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(raw, dict) or set(raw) != {
            field.name for field in fields(BenchmarkConfig)
        }:
            raise ValueError("benchmark configuration requires the exact declared fields")
        lists = ("frequencies_hz", "sampling_steps_s", "refinements", "traffic_loads_bps")
        if any(not isinstance(raw[key], list) for key in lists):
            raise ValueError("benchmark matrix dimensions must be lists")
        return BenchmarkConfig(
            **{key: tuple(value) if key in lists else value for key, value in raw.items()}
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, OverflowError) as exc:
        raise ValueError("invalid UTF-8 benchmark configuration") from exc


def _configured(scenario, frequency, step, refinement):
    selected = replace(
        scenario,
        radio_link=replace(scenario.radio_link, carrier_frequency_hz=frequency),
        time_window=replace(scenario.time_window, step_s=step),
        propagation=replace(scenario.propagation, refinement=refinement),
    )
    return parse_constellation(
        scenario_document(selected), selected.source_path, selected.source_sha256
    )


def _preflight(config, scenario):
    if scenario.propagation is None or scenario.propagation.hydrometeors is None:
        raise ValueError("benchmark requires declared gases and hydrometeors")
    satellite_count = len(load_catalog(scenario.orbit, load.timescale(builtin=True)))
    for frequency, step, refinement in product(
        config.frequencies_hz, config.sampling_steps_s, config.refinements
    ):
        selected = _configured(scenario, frequency, step, refinement)
        selected = _windowed(
            selected, config.experiment_config(config.traffic_loads_bps[0], network=False)
        )
        _, count = _bounded_grid(selected)
        if count * satellite_count * len(scenario.stations) > MAX_LINK_SAMPLES:
            raise ValueError("benchmark exceeds the constellation link sample budget")


_METRIC_UNITS = {
    "visible_sample_fraction": "1",
    "usable_sample_fraction": "1",
    "best_link_integrated_bits": "bit",
    "fixed_baseline_integrated_bits": "bit",
    "handover_count": "count",
    "visible_duration_s": "s",
    "usable_duration_s": "s",
    "rf_outage_duration_s": "s",
    "out_of_view_duration_s": "s",
    "visible_time_fraction": "1",
    "usable_time_fraction": "1",
    "mean_best_rate_bps": "bit/s",
    "connected_duration_s": "s",
    "disconnected_duration_s": "s",
    "connected_time_fraction": "1",
    "mean_bottleneck_bps": "bit/s",
    "integrated_bottleneck_bits": "bit",
    "route_changes": "count",
    "connected_sample_fraction": "1",
    "goodput_bps": "bit/s",
    "mean_delay_s": "s",
    "minimum_delay_s": "s",
    "maximum_delay_s": "s",
    "max_delay_s": "s",
}


def _scalar_rows(common, row):
    return [
        {**common, "metric": key, "value": value, "unit": _METRIC_UNITS.get(key, "packet")}
        for key, value in row.items()
        if key in _METRIC_UNITS or key.endswith("_packets")
    ]


def _metrics(run):
    common = {key: run[key] for key in ("frequency_hz", "step_s", "refinement", "offered_load_bps")}
    common = {**common, "run_id": run["id"], "direction": None, "flow": None}
    rows = []
    for case in run["summary"]["cases"]:
        base = {**common, "propagation": case["id"]}
        for row in case["station_metrics"]:
            rows.extend(
                _scalar_rows(
                    {**base, "kind": "station", "name": row["name"], "routing_model": None}, row
                )
            )
        for row in case["route_metrics"]:
            rows.extend(
                _scalar_rows(
                    {
                        **base,
                        "kind": "route",
                        "name": "endpoint route",
                        "routing_model": row["routing_model"],
                    },
                    row,
                )
            )
        for packet in case["packets"]:
            for flow in packet["summary"]["flow_metrics"]:
                rows.extend(
                    _scalar_rows(
                        {
                            **base,
                            "kind": "packet",
                            "name": "endpoint flow",
                            "routing_model": packet["routing_model"],
                            "direction": flow["direction"],
                            "flow": flow["flow"],
                        },
                        flow,
                    )
                )
    return rows


def _comparisons(metrics):
    """Signed deterministic effects, matched on every other matrix and entity dimension."""
    identity = (
        "frequency_hz",
        "step_s",
        "refinement",
        "offered_load_bps",
        "propagation",
        "kind",
        "name",
        "routing_model",
        "direction",
        "flow",
        "metric",
        "unit",
    )
    rows = []
    for axis in (
        "frequency_hz",
        "step_s",
        "refinement",
        "offered_load_bps",
        "propagation",
        "routing_model",
    ):
        groups = defaultdict(list)
        for row in metrics:
            if row["value"] is None or row[axis] is None:
                continue
            if axis == "routing_model" and row["kind"] != "packet":
                continue
            groups[tuple(row[key] for key in identity if key != axis)].append(row)
        for group in groups.values():
            order = {
                "free_space": 0,
                "reference": 1,
                "hydrometeors": 2,
                "fixed_capacity": 0,
                "minimum_delay": 1,
                "maximum_rate": 2,
            }
            group = sorted(group, key=lambda row: order.get(row[axis], row[axis]))
            for baseline, actual in combinations(group, 2):
                if baseline[axis] == actual[axis]:
                    continue
                delta = actual["value"] - baseline["value"]
                rows.append(
                    {
                        **{key: actual[key] for key in identity if key != axis},
                        "axis": axis,
                        "baseline_parameter": baseline[axis],
                        "actual_parameter": actual[axis],
                        "baseline_run_id": baseline["run_id"],
                        "actual_run_id": actual["run_id"],
                        "baseline_value": baseline["value"],
                        "actual_value": actual["value"],
                        "absolute_effect": delta,
                        "relative_effect": delta / baseline["value"] if baseline["value"] else None,
                        "relative_effect_unit": "1",
                        "case_count": 2,
                    }
                )
    return rows


def _uncertainty_inventory():
    records = (
        (
            "orbital_inputs",
            "m",
            "fitted GP elements",
            "Orbit and geometry propagated from archived GP elements",
            "No orbital covariance or independent tracking accuracy; fitted elements are not truth",
        ),
        (
            "station_coordinates",
            "deg, m",
            "declared unsurveyed coordinates",
            "Not quantified; declared coordinates held fixed.",
            "Survey, antenna location and geoid uncertainty are unknown",
        ),
        (
            "rf_and_noise",
            "dBW, dBi, K",
            "declared fixed terminal values",
            "Same EIRP, gain and fixed noise at each frequency",
            "Calibration, hardware variability and atmospheric emission are unknown",
        ),
        (
            "rain_and_cloud",
            "mm/h, kg/m²",
            "declared uniform sensitivity inputs",
            "Controlled gases/rain/cloud ablations",
            "No measured weather, exceedance model or spatial correlation",
        ),
        (
            "gas_grid_and_sampling",
            "s, bit/s",
            "deterministic numerical sensitivity",
            "Aligned time-step and gas-layer refinement deltas",
            "Convergence is not a statistical uncertainty bound or proof of absolute accuracy",
        ),
        (
            "mac_phy_interference",
            "bit/s",
            "model discrepancy",
            "Not quantified",
            "Satellite MAC/PHY, interference, atmospheric emission and operator scheduling are absent",
        ),
        (
            "packet_control",
            "s, packet, bit/s",
            "declared simulation assumptions",
            "Actual ns-3 receive events when enabled",
            "Synthetic reciprocal P2P, UDP, finite queues, left-held routes and acquisition; no calibrated traffic distribution",
        ),
    )
    return [
        {
            "source": key,
            "unit": unit,
            "evidence_class": evidence,
            "what_quantified": quantified,
            "unknowns": unknowns,
        }
        for key, unit, evidence, quantified, unknowns in records
    ]


def _report(summary):
    runs = summary["runs"]
    aliases = {run["id"]: f"R{index}" for index, run in enumerate(runs, 1)}
    links = "".join(
        f'<li><a href="{_link(run["bundle_path"] + "/index.html")}">{escape(run["id"])}</a></li>'
        for run in runs
    )
    chosen = {
        "mean_best_rate_bps",
        "rf_outage_duration_s",
        "mean_bottleneck_bps",
        "goodput_bps",
        "mean_delay_s",
    }
    records = [
        [
            row["run_id"],
            row["propagation"],
            row["kind"],
            row["name"],
            row["routing_model"] or "—",
            row["direction"] if row["direction"] is not None else "—",
            row["flow"] if row["flow"] is not None else "—",
            row["metric"],
            _number(row["value"]),
            row["unit"],
        ]
        for row in summary["metrics"]
        if row["metric"] in chosen
    ]
    chart_rows = [
        row
        for row in summary["metrics"]
        if row["metric"] == "mean_best_rate_bps" and row["propagation"] == "hydrometeors"
    ]
    chart = (
        '<div class="charts">'
        + "".join(
            _bars(
                f"Mean best-link reference rate · {name} · declared hydrometeors",
                [
                    (aliases[row["run_id"]], row["value"])
                    for row in chart_rows
                    if row["name"] == name
                ],
                "bit/s",
            )
            for name in dict.fromkeys(row["name"] for row in chart_rows)
        )
        + "</div>"
    )
    effects = [
        [
            row["baseline_run_id"],
            row["actual_run_id"],
            row["axis"],
            row["baseline_parameter"],
            row["actual_parameter"],
            row["kind"],
            row["name"],
            row.get("propagation", "varied"),
            row.get("routing_model", "varied") or "—",
            row.get("direction") if row.get("direction") is not None else "—",
            row.get("flow") if row.get("flow") is not None else "—",
            row["metric"],
            _number(row["absolute_effect"]),
            row["unit"],
            _number(row["relative_effect"]),
            row["case_count"],
        ]
        for row in summary["comparisons"]
        if row["metric"] in chosen
    ]
    limits = "".join(f"<li>{escape(text)}</li>" for text in summary["limitations"])
    inventory = [
        [row[key] for key in ("source", "unit", "evidence_class", "what_quantified", "unknowns")]
        for row in summary["uncertainty_inventory"]
    ]
    name = escape(summary["name"])
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "script-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
        f"<title>{name} · OpenLEO benchmark</title><style>{_CSS}</style></head><body>"
        f"<header><h1>{name}</h1><p>{len(runs)} completed experiments; packet evidence: {escape(summary['packet_evidence'])}</p></header><main>"
        '<p class="notice">Model-derived results, not measured weather or service performance. '
        "Wall runtime is diagnostic and excluded from numerical reproducibility.</p>"
        '<section class="panel"><h2>Frozen matrix</h2>'
        + _table(
            [
                "Alias",
                "Run",
                "Frequency (Hz)",
                "Step (s)",
                "Gas refinement",
                "Offered load per flow (bit/s)",
                "Runtime (s)",
            ],
            [
                [
                    aliases[run["id"]],
                    run["id"],
                    _number(run["frequency_hz"]),
                    _number(run["step_s"]),
                    run["refinement"],
                    _number(run["offered_load_bps"]),
                    _number(run["elapsed_s"]),
                ]
                for run in runs
            ],
        )
        + '</section><section class="panel"><h2>Station, route and packet metrics</h2>'
        + chart
        + _table(
            [
                "Run",
                "Propagation",
                "Kind",
                "Name",
                "Route",
                "Direction",
                "Flow",
                "Metric",
                "Value",
                "Unit",
            ],
            records,
        )
        + '</section><section class="panel"><h2>Aligned deterministic effects</h2>'
        + "<p>Effect = actual − baseline; relative effect = effect / baseline (dimensionless). "
        "A zero baseline has no relative effect. All other parameters and flow identities are matched. "
        "Zero effects remain; no hypothesis tests, confidence intervals or probability claims.</p>"
        + _table(
            [
                "Baseline run",
                "Actual run",
                "Axis",
                "Baseline",
                "Actual",
                "Kind",
                "Name",
                "Propagation",
                "Route",
                "Direction",
                "Flow",
                "Metric",
                "Effect",
                "Unit",
                "Relative (1)",
                "Cases",
            ],
            effects,
        )
        + '</section><section class="panel"><h2>Uncertainty inventory</h2>'
        + _table(["Source", "Units", "Evidence class", "Quantified", "Unknowns"], inventory)
        + '</section><section class="panel"><h2>Evidence bundles</h2><ul>'
        + links
        + '</ul><p><a href="benchmark.json">Numerical summary</a> · <a href="metrics.csv">Metrics CSV</a> · '
        '<a href="manifest.json">Artifact fingerprints</a></p></section><section class="panel"><h2>Limits</h2><ul>'
        + limits
        + "</ul></section></main></body></html>\n"
    )


def run_benchmark(config_path, output_dir, *, network_backend=None) -> dict:
    """Run a validated fixed matrix and atomically publish complete offline evidence."""
    path, target = Path(config_path), Path(output_dir)
    source_bytes = _read(path)
    config = _parse_config(source_bytes)
    scenario = load_constellation(path.parent / config.scenario_path)
    _preflight(config, scenario)
    _check_output(target)
    orbit_bytes = _read_artifact(scenario.orbit.path, MAX_ORBIT_BYTES)
    if sha256(orbit_bytes).hexdigest() != scenario.orbit.provenance.sha256:
        raise ValueError("source orbit checksum mismatch")
    network = network_backend is not None
    loads = config.traffic_loads_bps if network else (config.traffic_loads_bps[0],)
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=f".{target.name}-", dir=target.parent) as temporary:
        stage = Path(temporary) / "result"
        (stage / "inputs").mkdir(parents=True)
        raw = scenario_document(scenario)
        _write_json(
            stage / "inputs" / "scenario.json",
            {**raw, "orbit": {**raw["orbit"], "path": "orbit.csv"}},
        )
        (stage / "inputs" / "orbit.csv").write_bytes(orbit_bytes)
        _write_json(
            stage / "inputs" / "benchmark.json",
            {**asdict(config), "scenario_path": "scenario.json"},
        )
        portable = load_constellation(stage / "inputs" / "scenario.json")
        runs = []
        matrix = product(config.frequencies_hz, config.sampling_steps_s, config.refinements, loads)
        for frequency, step, refinement, offered in matrix:
            parameters = {
                "frequency_hz": frequency,
                "step_s": step,
                "refinement": refinement,
                "offered_load_bps": offered if network else None,
            }
            identifier = (
                "case-" + sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()[:16]
            )
            relative = "cases/" + identifier
            selected = _configured(portable, frequency, step, refinement)
            started = perf_counter()
            result = run_experiment(
                selected,
                config.experiment_config(offered, network=network),
                stage / relative,
                network_backend=network_backend,
            )
            runs.append(
                {
                    "id": identifier,
                    **parameters,
                    "bundle_path": relative,
                    "summary": result,
                    "elapsed_s": perf_counter() - started,
                }
            )
        metrics = [row for run in runs for row in _metrics(run)]
        summary = {
            "schema_version": "1",
            "kind": "openleo.benchmark",
            "name": config.name,
            "configuration": json.loads(json.dumps(asdict(config))),
            "source_fingerprints": {
                "configuration_sha256": sha256(source_bytes).hexdigest(),
                "scenario_sha256": scenario.source_sha256,
                "orbit_sha256": scenario.orbit.provenance.sha256,
            },
            "software": {
                name: version(name) for name in ("openleo-link", "skyfield", "sgp4", "numpy")
            },
            "runtime_environment": {
                "python": platform.python_version(),
                "implementation": platform.python_implementation(),
                "system": platform.system(),
                "machine": platform.machine(),
            },
            "packet_evidence": "actual_ns3" if network else "missing",
            "runs": runs,
            "metrics": metrics,
            "comparisons": _comparisons(metrics),
            "numerical_metrics_sha256": sha256(metric_csv(metrics).encode()).hexdigest(),
            "uncertainty_inventory": _uncertainty_inventory(),
            "limitations": [
                "Climate and hydrometeor values and station coordinates are declared assumptions, not measurements.",
                "Frequency changes retain the same gains, EIRP, bandwidth and fixed noise temperature; no frequency-specific hardware calibration.",
                "Numerical effects are deterministic sensitivities; no confidence intervals, probability distributions or statistical superiority claims.",
                "Reference link rates and snapshot path bottlenecks are not delivered packet goodput.",
                "Wall runtime and runtime environment are non-deterministic diagnostics excluded from numerical_metrics_sha256; bundle manifests bind exact artifact bytes.",
                "Packet replay uses synthetic P2P links with finite queues, acquisition gating and left-held geometry; MAC/PHY/interference are absent.",
                *(
                    [
                        "Packet results are missing: no ns-3 backend was configured. Traffic-load cases were collapsed; network-only flow and acquisition settings were not applied."
                    ]
                    if not network
                    else []
                ),
            ],
        }
        _write_json(stage / "benchmark.json", summary)
        (stage / "metrics.csv").write_text(metric_csv(metrics), encoding="utf-8", newline="\n")
        (stage / "index.html").write_text(_report(summary), encoding="utf-8", newline="\n")
        manifest = {**_manifest(stage), "kind": "openleo.benchmark-bundle"}
        _write_json(stage / "manifest.json", manifest)
        _check_output(target)
        if target.exists():
            target.rmdir()
        stage.replace(target)
    return summary
