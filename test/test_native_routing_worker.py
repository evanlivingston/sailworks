"""Lifecycle tests for the bounded native routing worker."""

from __future__ import annotations

import time

import pytest

from sailworks.core.marine.nativeworker import NativeRoutingWorker


def _wait(worker, timeout=90.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = worker.poll(0.1)
        if status is not None:
            return status
    pytest.fail("native worker did not publish a status in time")


def test_worker_returns_an_explicit_synthetic_diagnostic():
    worker = NativeRoutingWorker()
    try:
        generation = worker.submit_synthetic(engine="main")
        assert worker.running
        status = _wait(worker)

        assert status.generation == generation
        assert status.state == "completed"
        payload = status.payload
        assert payload["accepted"] is True
        assert payload["status"].startswith("complete")
        assert payload["layers"]
        assert payload["validation"]["passed"] is True
    finally:
        worker.shutdown()


@pytest.mark.parametrize(
    ("mode", "layer_count"),
    [("standard", 0), ("original", 30), ("professional", 5)],
)
def test_worker_delivers_qualified_mode(mode, layer_count):
    worker = NativeRoutingWorker()
    try:
        generation = worker.submit_synthetic(engine=mode)
        status = _wait(worker)
        assert status.generation == generation
        assert status.state == "completed"
        assert status.payload["status"] == "complete"
        assert status.payload["accepted"] is True
        assert status.payload["validation"]["passed"] is True
        assert status.payload["independent_validation"]["passed"] is True
        assert status.payload["layer_count"] == layer_count
    finally:
        worker.shutdown()


def test_superseded_generation_never_publishes():
    worker = NativeRoutingWorker()
    try:
        first = worker.submit_synthetic(engine="main")
        second = worker.submit_synthetic(engine="main")
        assert second == first + 1

        status = _wait(worker)
        assert status.generation == second
        assert status.state == "completed"
        # The superseded generation is never observable as a completion.
        assert worker.status.generation == second
    finally:
        worker.shutdown()


def test_cancel_publishes_no_accepted_completion():
    worker = NativeRoutingWorker()
    try:
        worker.submit_synthetic(engine="main")
        status = worker.cancel()

        assert status.state == "cancelled"
        assert status.payload is None
        assert worker.poll(0) is None  # cancel already returned the status
        assert worker.poll(0) is None
        assert not worker.running
    finally:
        worker.shutdown()


def test_aborted_worker_is_a_failure_not_a_route():
    worker = NativeRoutingWorker()
    try:
        worker.submit_synthetic(engine="main")
        deadline = time.monotonic() + 30
        while not worker.running and time.monotonic() < deadline:
            time.sleep(0.05)
        worker._process.terminate()  # simulate an abnormal exit

        status = _wait(worker)
        assert status.state == "failed"
        assert status.payload is None
        assert "without publishing" in status.message
        assert status.exit_code not in (0, None)
    finally:
        worker.shutdown()


def test_shutdown_releases_the_process():
    worker = NativeRoutingWorker()
    worker.submit_synthetic(engine="main")
    assert worker.running
    worker.shutdown()
    assert not worker.running
    assert worker.status is None
