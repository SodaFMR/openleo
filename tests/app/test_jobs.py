"""The app's job queue: ids, one worker, phases, caching and failures."""

import json
import threading
import time
from hashlib import sha256
from pathlib import Path

import pytest

from openleo.app import JobQueue
from openleo.app.jobs import QueueFull, job_id


def _wait(queue, identifier, states=("done", "failed"), timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = queue.status(identifier)
        if status["state"] in states:
            return status
        time.sleep(0.005)
    raise AssertionError(f"{identifier} stayed {queue.status(identifier)}")


@pytest.fixture
def queues():
    made = []

    def make(run_fn):
        queue = JobQueue(run_fn)
        made.append(queue)
        return queue

    yield make
    for queue in made:
        queue.close()


def test_id_is_sha256_of_canonical_kind_and_payload():
    payload = {"b": [1, 2.5], "a": "é"}
    canonical = json.dumps(
        {"kind": "run", "payload": payload},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    assert job_id("run", payload) == sha256(canonical.encode("utf-8")).hexdigest()
    assert job_id("run", payload) != job_id("sweep", payload)
    assert job_id("run", {"a": "é", "b": [1, 2.5]}) == job_id("run", payload)


def test_submit_rejects_unknown_kinds_and_non_finite_payloads(queues):
    queue = queues(lambda kind, payload: Path("x"))
    with pytest.raises(ValueError, match="kind"):
        queue.submit("study", {})
    with pytest.raises(ValueError):
        queue.submit("run", {"x": float("nan")})
    assert queue.count() == 0


def test_one_worker_runs_jobs_in_order_with_phases(queues):
    release = threading.Event()
    started, order = threading.Event(), []

    def run_fn(kind, payload):
        order.append((kind, payload["n"], threading.current_thread().name))
        queue.report("geometry")
        started.set()
        assert release.wait(10)
        queue.report("tables")
        return Path(f"/results/{payload['n']}")

    queue = queues(run_fn)
    first = queue.submit("run", {"n": 1})
    second = queue.submit("sweep", {"n": 2})
    assert started.wait(10)
    assert queue.status(first) == {
        "id": first,
        "state": "running",
        "phase": "geometry",
        "error": None,
    }
    assert queue.status(second) == {"id": second, "state": "queued", "phase": None, "error": None}
    release.set()
    assert _wait(queue, first) == {"id": first, "state": "done", "phase": "tables", "error": None}
    assert _wait(queue, second)["state"] == "done"
    assert [entry[:2] for entry in order] == [("run", 1), ("sweep", 2)]
    assert len({entry[2] for entry in order}) == 1
    assert queue.result(first) == ("run", Path("/results/1"))
    assert queue.result(second) == ("sweep", Path("/results/2"))
    assert queue.count() == 2


def test_report_outside_a_job_is_ignored(queues):
    queue = queues(lambda kind, payload: Path("x"))
    queue.report("geometry")


def test_failure_never_overwrites_a_cached_success(queues):
    outcomes = iter([Path("/ok"), ValueError("bad catalog"), ValueError("second failure")])

    def run_fn(kind, payload):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    queue = queues(run_fn)
    first = queue.submit("run", {"n": 1})
    assert _wait(queue, first)["state"] == "done"
    assert queue.submit("run", {"n": 1}) == first
    assert queue.status(first)["state"] == "done"
    assert queue.result(first) == ("run", Path("/ok"))
    failing = queue.submit("run", {"n": 2})
    assert _wait(queue, failing) == {
        "id": failing,
        "state": "failed",
        "phase": None,
        "error": "bad catalog",
    }
    assert queue.result(failing) is None
    assert queue.status(first)["state"] == "done"
    assert queue.submit("run", {"n": 2}) == failing
    assert _wait(queue, failing)["error"] == "second failure"
    assert queue.result(first) == ("run", Path("/ok"))


def test_failed_job_can_be_retried_to_success(queues):
    attempts = []

    def run_fn(kind, payload):
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError("transient")
        return Path("/ok")

    queue = queues(run_fn)
    identifier = queue.submit("run", {})
    assert _wait(queue, identifier)["state"] == "failed"
    assert queue.submit("run", {}) == identifier
    assert _wait(queue, identifier) == {
        "id": identifier,
        "state": "done",
        "phase": None,
        "error": None,
    }


def test_unexpected_errors_are_reported_generically(queues, capsys):
    def run_fn(kind, payload):
        raise KeyError("/secret/path")

    queue = queues(run_fn)
    identifier = queue.submit("run", {})
    status = _wait(queue, identifier)
    assert status["state"] == "failed"
    assert "secret" not in status["error"]
    err = capsys.readouterr().err
    assert "KeyError" in err and "secret" not in err


def test_resubmitting_a_pending_job_does_not_queue_it_twice(queues):
    release = threading.Event()
    calls = []

    def run_fn(kind, payload):
        calls.append(payload)
        assert release.wait(10)
        return Path("/ok")

    queue = queues(run_fn)
    first = queue.submit("run", {"n": 1})
    second = queue.submit("run", {"n": 2})
    assert queue.submit("run", {"n": 2}) == second
    assert queue.submit("run", {"n": 1}) == first
    release.set()
    _wait(queue, second)
    assert calls == [{"n": 1}, {"n": 2}]


def test_pending_jobs_are_bounded(queues):
    release = threading.Event()
    queue = queues(lambda kind, payload: release.wait(10) and Path("/ok"))
    queue.max_pending = 2
    queue.submit("run", {"n": 1})
    queue.submit("run", {"n": 2})
    with pytest.raises(QueueFull):
        queue.submit("run", {"n": 3})
    release.set()


def test_unknown_ids(queues):
    queue = queues(lambda kind, payload: Path("/ok"))
    with pytest.raises(KeyError):
        queue.status("0" * 64)
    assert queue.result("0" * 64) is None


def test_close_stops_the_worker_and_refuses_new_jobs(queues):
    queue = queues(lambda kind, payload: Path("/ok"))
    queue.close()
    queue.close()
    assert not queue.worker.is_alive()
    with pytest.raises(RuntimeError):
        queue.submit("run", {})
