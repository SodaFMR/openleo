"""One-command, fail-closed reproduction of numerical and native packet evidence.

The frozen benchmark is a sweep spec (``examples/v2/reproduce/release_benchmark.json``) with a
``replay.network`` block. ``run_reproduce`` verifies the published orbit reference, runs the
sweep with native network replays and publishes the evidence, or fails and publishes nothing:

- without a trusted native network backend,
- when the orbit reference does not pass,
- when the spec has no network replay (or asks for a link replay, which needs a packet
  backend that reproduction does not take),
- when any run lacks actual native packet results, or reports unresolved packets or send errors.

Output: ``sweep/`` (the sweep's own layout), ``inputs/`` (the orbit reference and the
configuration), ``orbit-verification.json``, ``release-evidence.json`` and ``manifest.json``,
published atomically.
"""

from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
from typing import Any

from openleo import sweep
from openleo.engine import _assembly as assembly
from openleo.results import manifest_digest, publish_atomically, write_manifest
from openleo.verify.orbit import verify_orbit_reference

EVIDENCE_KIND = "openleo.release-evidence"
EVIDENCE_SCHEMA_VERSION = "2"
MAX_REFERENCE_BYTES = 100_000
NATIVE_EVIDENCE = "actual_ns3"
FAILURE_KEYS = ("unresolved_packets", "send_error_packets")
LIMITATIONS = (
    (
        "Numerical reference agreement and synthetic packet simulation, not observational RF "
        "validation."
    ),
    (
        "No weather, terminal calibration, operator performance or probabilistic uncertainty is "
        f"inferred; every comparison is {sweep.COMPARISON_LABEL}."
    ),
    (
        "numerical_metrics_sha256 is the SHA-256 of sweep/comparisons.csv; the manifests bind the "
        "exact bytes of every artefact."
    ),
)


def require_native_evidence(document: dict[str, Any]) -> int:
    """The number of native replays in a sweep result; ValueError unless every run has them.

    Each run's ``replay`` must be ``{"evidence": "actual_ns3", "replays": [...]}`` with at least
    one replay, each ``summary.aggregate`` reporting no unresolved packets or send errors.
    """
    count = 0
    for entry in document["runs"]:
        replay = entry.get("replay")
        packets = replay.get("replays") if isinstance(replay, dict) else None
        if (
            not isinstance(replay, dict)
            or replay.get("evidence") != NATIVE_EVIDENCE
            or not isinstance(packets, list)
            or not packets
        ):
            raise ValueError(
                f"release evidence requires actual native packet results; run {entry['id']} has none"
            )
        for packet in packets:
            try:
                aggregate = packet["summary"]["aggregate"]
                failed = any(aggregate[key] for key in FAILURE_KEYS)
            except (KeyError, TypeError) as exc:
                raise ValueError(
                    f"run {entry['id']}: a native replay lacks summary.aggregate "
                    f"{list(FAILURE_KEYS)}"
                ) from exc
            if failed:
                raise ValueError(
                    "release evidence contains unresolved packets or source send errors "
                    f"(run {entry['id']})"
                )
        count += len(packets)
    return count


def _reference_bytes(path: Path, reference: dict) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(MAX_REFERENCE_BYTES + 1)
    if len(data) > MAX_REFERENCE_BYTES or sha256(data).hexdigest() != reference["fixture_sha256"]:
        raise ValueError("orbit reference changed during verification")
    return data


def run_reproduce(
    config_path: Path,
    reference_path: Path,
    network_backend: Path | None,
    output_dir: Path,
) -> dict:
    """Verify the orbit reference, run the frozen sweep with native replays, publish evidence.

    Returns the ``release-evidence.json`` document. Nothing is published unless everything
    passes (module docstring); an existing ``output_dir`` raises FileExistsError.
    """
    if network_backend is None:
        raise ValueError("release reproduction requires a trusted native network backend")
    target = Path(output_dir)
    if os.path.lexists(target):
        raise FileExistsError(f"refusing to overwrite existing results at {target}")
    reference = verify_orbit_reference(reference_path)
    if not reference["passed"]:
        raise ValueError("published orbit reference verification failed")
    reference_bytes = _reference_bytes(Path(reference_path), reference)
    backend = Path(network_backend)
    if not backend.is_file():
        raise ValueError(f"network backend {backend} is not a file")
    config = Path(config_path)
    spec, base, runs = sweep.prepare_sweep(config)
    replay = spec["replay"]
    if replay is None or replay["network"] is None:
        raise ValueError("/replay/network: release reproduction requires native network replays")
    if replay["link"] is not None:
        raise ValueError(
            "/replay/link: release reproduction takes no packet backend, so it cannot replay links"
        )
    evidence: dict = {}

    def build(stage: Path) -> None:
        (stage / "sweep").mkdir()
        document = sweep.execute_sweep(spec, base, runs, stage / "sweep", 1, None, backend)
        write_manifest(stage / "sweep")
        packets = require_native_evidence(document)
        (stage / "inputs").mkdir()
        (stage / "inputs" / "orbit-reference.json").write_bytes(reference_bytes)
        (stage / "inputs" / "configuration.json").write_bytes(spec["source_bytes"])
        _write_json(stage / "orbit-verification.json", reference)
        comparisons = (stage / "sweep" / "comparisons.csv").read_bytes()
        evidence.update(
            {
                "kind": EVIDENCE_KIND,
                "schema_version": EVIDENCE_SCHEMA_VERSION,
                "name": spec["name"],
                "software": assembly.software(),
                "sweep": "sweep/sweep.json",
                "sweep_manifest_sha256": manifest_digest(stage / "sweep"),
                "orbit_verification": "orbit-verification.json",
                "orbit_reference_sha256": reference["fixture_sha256"],
                "orbit_reference_passed": reference["passed"],
                "completed_runs": len(document["runs"]),
                "native_packet_replays": packets,
                "numerical_metrics_sha256": sha256(comparisons).hexdigest(),
                "comparison_label": sweep.COMPARISON_LABEL,
                "limitations": list(LIMITATIONS),
            }
        )
        _write_json(stage / "release-evidence.json", evidence)
        write_manifest(stage)

    publish_atomically(target, build)
    return evidence


def _write_json(path: Path, document: dict) -> None:
    text = json.dumps(
        assembly.json_safe(document), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    )
    path.write_bytes((text + "\n").encode("utf-8"))
