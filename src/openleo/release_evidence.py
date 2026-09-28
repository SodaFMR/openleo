"""One-command, fail-closed reproduction of numerical and native packet evidence."""

from hashlib import sha256
from html import escape
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

from openleo.benchmark import run_benchmark
from openleo.experiment import _manifest, _write_json
from openleo.fidelity_report import _CSS
from openleo.fidelity_study import _check_output
from openleo.orbit_verification import verify_orbit_reference
from openleo.workbench import _read


def reproduce_release(benchmark_path, reference_path, output_dir, *, network_backend) -> dict:
    """Require the compiled backend and passing reference before publishing evidence."""
    if network_backend is None:
        raise ValueError("release reproduction requires a trusted native network backend")
    target = Path(output_dir)
    _check_output(target)
    reference = verify_orbit_reference(reference_path)
    if not reference["passed"]:
        raise ValueError("published orbit reference verification failed")
    reference_bytes = _read(Path(reference_path), 100_000)
    if sha256(reference_bytes).hexdigest() != reference["fixture_sha256"]:
        raise ValueError("orbit reference changed during verification")
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=f".{target.name}-", dir=target.parent) as temporary:
        stage = Path(temporary) / "result"
        stage.mkdir()
        benchmark = run_benchmark(
            benchmark_path, stage / "benchmark", network_backend=network_backend
        )
        packets = [
            packet
            for run in benchmark["runs"]
            for case in run["summary"]["cases"]
            for packet in case["packets"]
        ]
        if benchmark["packet_evidence"] != "actual_ns3" or not packets:
            raise ValueError("release evidence requires actual native packet results")
        if any(
            packet["summary"]["aggregate"][key]
            for packet in packets
            for key in ("unresolved_packets", "send_error_packets")
        ):
            raise ValueError("release evidence contains unresolved packets or source send errors")
        (stage / "inputs").mkdir()
        (stage / "inputs" / "orbit-reference.json").write_bytes(reference_bytes)
        _write_json(stage / "orbit-verification.json", reference)
        summary = {
            "schema_version": "1",
            "kind": "openleo.release-evidence",
            "software": {"openleo-link": version("openleo-link")},
            "benchmark": "benchmark/benchmark.json",
            "orbit_verification": "orbit-verification.json",
            "orbit_reference_sha256": reference["fixture_sha256"],
            "orbit_reference_passed": reference["passed"],
            "completed_experiments": len(benchmark["runs"]),
            "native_packet_replays": len(packets),
            "numerical_metrics_sha256": benchmark["numerical_metrics_sha256"],
            "limitations": [
                "Numerical reference agreement and synthetic packet simulation, not observational RF validation.",
                "No weather, terminal calibration, operator performance or probabilistic uncertainty is inferred.",
                "Wall runtimes are diagnostics; numerical_metrics_sha256 excludes them.",
            ],
        }
        _write_json(stage / "release-evidence.json", summary)
        limits = "".join(f"<li>{escape(value)}</li>" for value in summary["limitations"])
        (stage / "index.html").write_text(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            "script-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
            f"<title>OpenLEO release evidence</title><style>{_CSS}</style></head><body>"
            "<header><h1>OpenLEO reproducible research evidence</h1></header><main>"
            f'<section class="panel"><p>{len(benchmark["runs"])} completed experiments · '
            f"{len(packets)} native multi-hop replays · {len(reference['epochs'])} published orbit states checked.</p>"
            '<p><a href="benchmark/index.html">Open benchmark figures, results and 3D experiments</a></p>'
            '<p><a href="orbit-verification.json">Published-reference verification</a> · '
            '<a href="release-evidence.json">Release summary</a> · <a href="manifest.json">All artifact fingerprints</a></p>'
            f"<ul>{limits}</ul></section></main></body></html>\n",
            encoding="utf-8",
            newline="\n",
        )
        _write_json(
            stage / "manifest.json", {**_manifest(stage), "kind": "openleo.release-evidence-bundle"}
        )
        _check_output(target)
        if target.exists():
            target.rmdir()
        stage.replace(target)
    return summary
