"""The app's job queue: one worker thread runs submitted runs and sweeps one at a time.

A job's id is the SHA-256 of the canonical JSON (sorted keys, compact, UTF-8, no NaN) of
``{"kind": kind, "payload": payload}``, so the same normalised request always gets the same id.

States: ``queued`` -> ``running`` -> ``done`` or ``failed``. A ``done`` job is a cache entry:
submitting its id again returns it without running anything, so a later failure can never
replace a result that was produced. ``evict`` turns a ``done`` job into ``evicted`` (its
result was deleted to bound disk use). Submitting a ``failed`` or ``evicted`` id queues it
again. While a job runs, ``report(phase)`` (called by ``run_fn`` from the worker thread) sets
its phase; once ``close`` has been called it raises ``JobCancelled`` instead, so the running
job stops at its next phase.

``run_fn(kind, payload)`` returns the directory holding the job's result. A ``ValueError``
fails the job with its message, which callers must keep free of private paths; any other
exception fails it with a generic message, and only the exception type is logged.
"""

from __future__ import annotations

import queue
import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from openleo.engine._assembly import canonical_sha256

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

KINDS = ("run", "sweep")
MAX_PENDING = 32
UNEXPECTED_ERROR = "The job failed unexpectedly; the server log names the error type."
CANCELLED_ERROR = "The job was cancelled: the server is shutting down."


class QueueFull(Exception):
    """Too many jobs are queued or running; retry when one has finished."""


class JobCancelled(Exception):
    """Raised by ``JobQueue.report`` in the running job once the queue is closing."""


def job_id(kind: str, payload: dict) -> str:
    """SHA-256 of the canonical JSON of ``{"kind": kind, "payload": payload}``."""
    return canonical_sha256({"kind": kind, "payload": payload})


@dataclass
class _Job:
    id: str
    kind: str
    payload: dict | None
    state: str = "queued"
    phase: str | None = None
    error: str | None = None
    path: Path | None = None


class JobQueue:
    """Jobs by id, run one at a time on a single daemon worker thread."""

    def __init__(self, run_fn: Callable[[str, dict], Path]) -> None:
        self._run_fn = run_fn
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()
        self._pending: queue.Queue[_Job | None] = queue.Queue()
        self._current: _Job | None = None
        self._closed = False
        self._cancel = threading.Event()
        self.max_pending = MAX_PENDING
        self.worker = threading.Thread(target=self._work, name="openleo-jobs", daemon=True)
        self.worker.start()

    def submit(self, kind: Literal["run", "sweep"], payload: dict) -> str:
        """Queue a job unless its id is done or pending; return the id."""
        if kind not in KINDS:
            raise ValueError(f"job kind must be one of {list(KINDS)}")
        identifier = job_id(kind, payload)
        with self._lock:
            if self._closed:
                raise RuntimeError("the job queue is closed")
            job = self._jobs.get(identifier)
            if job is not None and job.state not in ("failed", "evicted"):
                return identifier
            pending = sum(j.state in ("queued", "running") for j in self._jobs.values())
            if pending >= self.max_pending:
                raise QueueFull(f"{pending} jobs are already queued or running")
            job = _Job(identifier, kind, payload)
            self._jobs[identifier] = job
            self._pending.put(job)
        return identifier

    def status(self, identifier: str) -> dict[str, Any]:
        """``{"id", "state", "phase", "error"}``; KeyError for an unknown id."""
        with self._lock:
            job = self._jobs[identifier]
            return {"id": job.id, "state": job.state, "phase": job.phase, "error": job.error}

    def result(self, identifier: str) -> tuple[str, Path] | None:
        """``(kind, result directory)`` of a done job, else None."""
        with self._lock:
            job = self._jobs.get(identifier)
            if job is None or job.state != "done":
                return None
            return job.kind, job.path

    def kind(self, identifier: str) -> str | None:
        with self._lock:
            job = self._jobs.get(identifier)
            return None if job is None else job.kind

    def count(self) -> int:
        """Jobs known to the queue, in any state."""
        with self._lock:
            return len(self._jobs)

    def report(self, phase: str) -> None:
        """Set the phase of the running job (ignored when none is running).

        Raises ``JobCancelled`` in the running job once ``close`` has been called.
        """
        with self._lock:
            if self._current is None:
                return
            if self._cancel.is_set():
                raise JobCancelled(CANCELLED_ERROR)
            self._current.phase = phase

    def evict(self, identifier: str) -> None:
        """Mark a done job ``evicted``: its result is gone (unknown or unfinished ids: no-op)."""
        with self._lock:
            job = self._jobs.get(identifier)
            if job is not None and job.state == "done":
                job.state, job.path = "evicted", None

    def close(self) -> None:
        """Refuse new jobs, cancel the running one at its next phase and wait for the worker.

        Queued jobs stay ``queued`` and never run. The join has no timeout: when ``close``
        returns, nothing writes results any more.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cancel.set()
        self._pending.put(None)
        self.worker.join()

    def _work(self) -> None:
        while True:
            job = self._pending.get()
            if job is None:
                return
            with self._lock:
                if self._closed:
                    return
                job.state, job.phase, job.error = "running", None, None
                self._current = job
            try:
                path = self._run_fn(job.kind, job.payload)
            except JobCancelled:
                outcome = {"state": "failed", "error": CANCELLED_ERROR}
            except ValueError as exc:
                outcome = {"state": "failed", "error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - never expose internal paths or values
                print(f"OpenLEO {job.kind} job failed: {type(exc).__name__}.", file=sys.stderr)
                outcome = {"state": "failed", "error": UNEXPECTED_ERROR}
            else:
                outcome = {"state": "done", "path": path}
            with self._lock:
                job.state = outcome["state"]
                job.error = outcome.get("error")
                job.path = outcome.get("path")
                job.payload = None  # a failed job is queued again with a fresh payload
                self._current = None
