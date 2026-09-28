"""Aligned propagation ablations with optional, explicitly configured packet replay."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from datetime import timedelta
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

from openleo.constellation import (
    ConstellationScenario,
    load_constellation,
    parse_constellation,
    scenario_document,
    simulate_constellation,
)
from openleo.fidelity_study import _check_output, derive_metrics, metric_csv
from openleo.network import ROUTE_MODELS, add_network
from openleo.orbit import MAX_ORBIT_BYTES
from openleo.packet_replay import ReplayConfig, _integer, _microseconds, _number, _validate_config
from openleo.workbench import _json, _read, write_workbench


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    packet_mode: str = "none"
    station_name: str | None = None
    norad_id: int | None = None
    start_offset_s: float = 0
    duration_s: float = 60
    offered_load_bps: float = 100_000
    packet_size_bytes: int = 512
    queue_packets: int = 32
    flows_per_direction: int = 1
    acquisition_delay_s: float = 0
    seed: int = 1

    def __post_init__(self):
        if self.packet_mode not in ("none", "link", "network"):
            raise ValueError("packet_mode must be none, link or network")
        if self.station_name is not None and (
            not isinstance(self.station_name, str) or not self.station_name.strip()
        ):
            raise ValueError("station_name must be a nonblank string")
        if self.norad_id is not None:
            _integer(self.norad_id, "norad_id", 1, 2**53 - 1)
        if self.packet_mode == "link" and (self.station_name is None or self.norad_id is None):
            raise ValueError("link replay requires station_name and norad_id")
        _validate_config(
            ReplayConfig(
                self.station_name or "model-only",
                self.norad_id or 1,
                self.start_offset_s,
                self.duration_s,
                self.offered_load_bps,
                self.packet_size_bytes,
                self.queue_packets,
                self.seed,
            )
        )
        _integer(self.flows_per_direction, "flows_per_direction", 1, 4)
        delay = _number(self.acquisition_delay_s, "acquisition_delay_s", 0, 60)
        _microseconds(delay, "acquisition_delay_s")
        if self.packet_mode != "network" and (
            self.flows_per_direction != 1 or self.acquisition_delay_s != 0
        ):
            raise ValueError("flows_per_direction and acquisition_delay_s require network replay")


def parse_experiment_config(raw: dict) -> ExperimentConfig:
    """Accept configuration values only, never executable or filesystem paths."""
    allowed = {field.name for field in fields(ExperimentConfig)}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError("invalid experiment settings or unknown fields")
    try:
        return ExperimentConfig(**raw)
    except (TypeError, OverflowError) as exc:
        raise ValueError("invalid experiment settings") from exc


def _windowed(scenario: ConstellationScenario, config: ExperimentConfig) -> ConstellationScenario:
    if not isinstance(scenario, ConstellationScenario):
        raise ValueError("scenario must be a loaded constellation")  # noqa: TRY004
    scenario = parse_constellation(
        scenario_document(scenario), scenario.source_path, scenario.source_sha256
    )
    window = scenario.time_window
    span = (window.stop_utc - window.start_utc).total_seconds()
    if config.start_offset_s > span or config.duration_s > span - config.start_offset_s:
        raise ValueError("experiment window must fit wholly inside the source scenario")
    if config.duration_s < window.step_s:
        raise ValueError("experiment window must be at least one declared sampling interval")
    start = window.start_utc + timedelta(seconds=config.start_offset_s)
    stop = start + timedelta(seconds=config.duration_s)
    if start < window.start_utc or stop > window.stop_utc:
        raise ValueError("experiment window must fit wholly inside the source scenario")
    return replace(scenario, time_window=replace(window, start_utc=start, stop_utc=stop))


def _variants(scenario):
    cases = [("free_space", "Free space", replace(scenario, propagation=None))]
    if scenario.propagation is not None:
        cases.append(
            (
                "reference",
                "Reference gases",
                replace(scenario, propagation=replace(scenario.propagation, hydrometeors=None)),
            )
        )
        if scenario.propagation.hydrometeors is not None:
            cases.append(("hydrometeors", "Declared gases, rain and cloud", scenario))
    return tuple(cases)


def _write_json(path: Path, value) -> None:
    path.write_text(_json(value) + "\n", encoding="utf-8", newline="\n")


def _packets(directory, config, packet_backend, network_backend):
    common = {
        "duration_s": config.duration_s,
        "offered_load_bps": config.offered_load_bps,
        "packet_size_bytes": config.packet_size_bytes,
        "queue_packets": config.queue_packets,
        "seed": config.seed,
    }
    if config.packet_mode == "link":
        from openleo.packet_replay import run_packet_replay

        summary = run_packet_replay(
            directory,
            ReplayConfig(config.station_name, config.norad_id, **common),
            packet_backend,
            directory / "packets" / "link",
        )
        return [
            {
                "mode": "link",
                "routing_model": None,
                "bundle_path": "packets/link",
                "summary_file": "packet-summary.json",
                "summary": summary,
            }
        ]
    if config.packet_mode == "network":
        from openleo.network_replay import NetworkReplayConfig, run_network_replay

        return [
            {
                "mode": "network",
                "routing_model": model,
                "bundle_path": f"packets/{model}",
                "summary_file": "network-summary.json",
                "summary": run_network_replay(
                    directory,
                    NetworkReplayConfig(
                        routing_model=model,
                        **common,
                        flows_per_direction=config.flows_per_direction,
                        acquisition_delay_s=config.acquisition_delay_s,
                    ),
                    network_backend,
                    directory / "packets" / model,
                ),
            }
            for model in ROUTE_MODELS
        ]
    return []


def _case(stage, identifier, label, scenario, config, packet_backend, network_backend):
    relative = f"cases/{identifier}"
    directory = stage / relative
    directory.mkdir(parents=True)
    raw = scenario_document(scenario)
    raw = {**raw, "orbit": {**raw["orbit"], "path": "../../inputs/orbit.csv"}}
    _write_json(directory / "scenario.json", raw)
    normalized = load_constellation(directory / "scenario.json")
    document = add_network(simulate_constellation(normalized))
    write_workbench(document, directory)
    stations, routes = derive_metrics(document)
    packets = _packets(directory, config, packet_backend, network_backend)
    return {
        "id": identifier,
        "label": label,
        "bundle_path": relative,
        "scenario_sha256": normalized.source_sha256,
        "duration_s": config.duration_s,
        "sample_count": len(document["timestamps_utc"]),
        "station_metrics": list(stations),
        "route_metrics": list(routes),
        "packets": [
            {**packet, "bundle_path": f"{relative}/{packet['bundle_path']}"} for packet in packets
        ],
    }


def _comparisons(cases):
    baseline = cases[0]
    rows = []
    for case in cases[1:]:
        for actual, reference in zip(
            case["station_metrics"], baseline["station_metrics"], strict=True
        ):
            rows.append(
                {
                    "baseline_case": "free_space",
                    "case_id": case["id"],
                    "kind": "station",
                    "name": actual["name"],
                    "delta_mean_reference_rate_bps": actual["mean_best_rate_bps"]
                    - reference["mean_best_rate_bps"],
                    "delta_outage_duration_s": actual["rf_outage_duration_s"]
                    - reference["rf_outage_duration_s"],
                }
            )
        for actual, reference in zip(case["route_metrics"], baseline["route_metrics"], strict=True):
            rows.append(
                {
                    "baseline_case": "free_space",
                    "case_id": case["id"],
                    "kind": "route",
                    "name": actual["routing_model"],
                    "delta_mean_reference_rate_bps": actual["mean_bottleneck_bps"]
                    - reference["mean_bottleneck_bps"],
                    "delta_outage_duration_s": reference["connected_duration_s"]
                    - actual["connected_duration_s"],
                }
            )
    return rows


def _manifest(directory):
    paths = sorted(directory.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise ValueError("experiment outputs must not contain symlinks")
    return {
        "schema_version": "1",
        "kind": "openleo.experiment-bundle",
        "files": {
            path.relative_to(directory).as_posix(): sha256(path.read_bytes()).hexdigest()
            for path in paths
            if path.is_file()
        },
    }


def run_experiment(
    scenario: ConstellationScenario,
    config: ExperimentConfig,
    output_dir: str | Path,
    *,
    packet_backend: str | Path | None = None,
    network_backend: str | Path | None = None,
) -> dict:
    """Run aligned, declared variants and publish a portable evidence bundle atomically."""
    from openleo.experiment_report import render_experiment_report

    if not isinstance(config, ExperimentConfig):
        raise ValueError("config must be an ExperimentConfig")  # noqa: TRY004
    config = parse_experiment_config(asdict(config))
    if config.packet_mode == "link" and packet_backend is None:
        raise ValueError("link replay requires a configured trusted packet backend")
    if config.packet_mode == "network" and network_backend is None:
        raise ValueError("network replay requires a configured trusted network backend")
    selected = _windowed(scenario, config)
    target = Path(output_dir)
    _check_output(target)
    catalog = _read(selected.orbit.path, MAX_ORBIT_BYTES)
    if sha256(catalog).hexdigest() != selected.orbit.provenance.sha256:
        raise ValueError("source orbit checksum mismatch")
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=f".{target.name}-", dir=target.parent) as temporary:
        stage = Path(temporary) / "result"
        (stage / "inputs").mkdir(parents=True)
        (stage / "inputs" / "orbit.csv").write_bytes(catalog)
        raw = scenario_document(scenario)
        _write_json(
            stage / "inputs" / "scenario.json",
            {**raw, "orbit": {**raw["orbit"], "path": "orbit.csv"}},
        )
        _write_json(stage / "inputs" / "settings.json", asdict(config))
        cases = [
            _case(stage, identifier, label, variant, config, packet_backend, network_backend)
            for identifier, label, variant in _variants(selected)
        ]
        raw = scenario_document(selected)
        summary = {
            "schema_version": "1",
            "kind": "openleo.experiment",
            "name": scenario.name,
            "configuration": asdict(config),
            "scenario": {**raw, "orbit": {**raw["orbit"], "path": "inputs/orbit.csv"}},
            "provenance": {
                "source_scenario_sha256": scenario.source_sha256,
                "orbit_sha256": selected.orbit.provenance.sha256,
                "software": {
                    name: version(name) for name in ("openleo-link", "skyfield", "sgp4", "numpy")
                },
            },
            "cases": cases,
            "comparisons": _comparisons(cases),
            "limitations": [
                "All variants use the same recomputed UTC window, sampling, station and RF assumptions; propagation changes are explicit.",
                "Declared rain/cloud conditions are controlled model inputs, not local-weather observations or exceedance statistics.",
                "Reference rates and path bottlenecks are not measured throughput; optional UDP goodput comes from actual ns-3 receive events.",
                "Receiver noise temperature stays fixed; atmospheric emission, interference, satellite MAC/PHY and operator hardware are not modeled.",
                "Deterministic deltas are not uncertainty intervals or evidence of universal routing superiority.",
            ],
        }
        _write_json(stage / "experiment-study.json", summary)
        for name, key in (("stations", "station_metrics"), ("routes", "route_metrics")):
            rows = [{"case_id": case["id"], **row} for case in cases for row in case[key]]
            (stage / f"{name}.csv").write_text(metric_csv(rows), encoding="utf-8", newline="\n")
        (stage / "index.html").write_text(
            render_experiment_report(summary), encoding="utf-8", newline="\n"
        )
        _write_json(stage / "manifest.json", _manifest(stage))
        _check_output(target)
        if target.exists():
            target.rmdir()
        stage.replace(target)
    return summary
