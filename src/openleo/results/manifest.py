"""``manifest.json``: the SHA-256 of every file in a results directory except itself.

``results.json`` is covered by the manifest, so it cannot also carry the manifest's digest
(the digest would change the file it vouches for). The digest of ``manifest.json`` itself is
the run's single integrity anchor; ``manifest_digest`` computes it for the CLI, reports and
anything that wants to pin a run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

MANIFEST_NAME = "manifest.json"
MANIFEST_KIND = "openleo.manifest"
MANIFEST_SCHEMA_VERSION = "1"


def sha256_file(path: Path) -> str:
    """Streaming SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _files(directory: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        if path.is_symlink():
            raise ValueError(f"results directory contains a symlink: {relative}")
        if path.is_file() and relative != MANIFEST_NAME:
            found[relative] = path
    return found


def build_manifest(directory: Path) -> dict:
    """The manifest document for the files currently under ``directory``."""
    directory = Path(directory)
    return {
        "kind": MANIFEST_KIND,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "algorithm": "sha256",
        "files": {name: sha256_file(path) for name, path in _files(directory).items()},
    }


def write_manifest(directory: Path) -> Path:
    """Write ``manifest.json`` (sorted keys, LF) and return its path."""
    directory = Path(directory)
    text = json.dumps(build_manifest(directory), sort_keys=True, ensure_ascii=False, indent=2)
    path = directory / MANIFEST_NAME
    path.write_bytes((text + "\n").encode("utf-8"))
    return path


def read_manifest(directory: Path) -> dict[str, str]:
    """The ``files`` mapping of a parsed, structurally valid ``manifest.json``."""
    path = Path(directory) / MANIFEST_NAME
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{MANIFEST_NAME} not found in {directory}") from exc
    except ValueError as exc:
        raise ValueError(f"invalid manifest {MANIFEST_NAME}: {exc}") from exc
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("kind") != MANIFEST_KIND
        or manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or not isinstance(files, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())
    ):
        raise ValueError(f"invalid manifest {MANIFEST_NAME}: unrecognised structure")
    return files


def verify_manifest(directory: Path) -> None:
    """Raise ValueError unless the directory holds exactly the files the manifest lists."""
    directory = Path(directory)
    expected = read_manifest(directory)
    actual = _files(directory)
    problems = [f"missing file {name}" for name in sorted(expected.keys() - actual.keys())]
    problems += [f"unlisted file {name}" for name in sorted(actual.keys() - expected.keys())]
    problems += [
        f"sha256 mismatch for {name}"
        for name in sorted(expected.keys() & actual.keys())
        if sha256_file(actual[name]) != expected[name]
    ]
    if problems:
        raise ValueError("manifest verification failed: " + "; ".join(problems))


def manifest_digest(directory: Path) -> str:
    """SHA-256 hex digest of ``manifest.json``: the run's integrity anchor."""
    path = Path(directory) / MANIFEST_NAME
    if not path.is_file():
        raise ValueError(f"{MANIFEST_NAME} not found in {directory}")
    return sha256_file(path)
