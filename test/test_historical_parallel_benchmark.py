"""Wall-clock sanity check for the bound-run parallel pool.

The workload is a fake worker that sleeps a fixed time per member; no real
solve is measured. Six members at 80 ms each take ~480 ms serially and ~160 ms
with three slots, which is a wide enough margin to assert a 1.8x speedup
without flaking on a loaded machine.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from sailworks.core import historicalexecution
from sailworks.core.historicalexecution import run_bound_members

MARINE_DIRECTORY = "/marine"
MEMBER_SECONDS = 0.08
MEMBERS = ("a", "b", "c", "d", "e", "f")


class SleepingJob:
    def __init__(self, member_id):
        self.calculation_id = f"calc-{member_id}"


class SleepingWorker:
    """Duck-typed worker that spends a fixed wall time inside ``poll``."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.job = None

    def submit(self, job):
        self.job = job

    def poll(self, timeout=0.0):
        time.sleep(self.seconds)
        member = self.job.calculation_id.removeprefix("calc-")
        return SimpleNamespace(state="completed", payload={"member": member}, message="")

    def take_progress(self):
        return None

    def shutdown(self):
        return None


def _executions():
    return [SimpleNamespace(member_id=name, request=object()) for name in MEMBERS]


def _save(execution, result):
    return f"route-{result['member']}", 1.0


def _echo(payload, job):
    return payload


def test_parallel_pool_speeds_up_fake_members(monkeypatch):
    monkeypatch.setattr(
        historicalexecution,
        "build_native_request",
        lambda execution, **kwargs: SleepingJob(execution.member_id),
    )
    lock = threading.Lock()
    saved = []

    def save(execution, result):
        with lock:
            saved.append(result["member"])
        return _save(execution, result)

    started = time.monotonic()
    serial = run_bound_members(
        _executions(),
        worker=SleepingWorker(MEMBER_SECONDS),
        save=save,
        marine_directory=MARINE_DIRECTORY,
        convert=_echo,
    )
    serial_ms = (time.monotonic() - started) * 1000.0

    started = time.monotonic()
    parallel = run_bound_members(
        _executions(),
        worker_factory=lambda: SleepingWorker(MEMBER_SECONDS),
        concurrency=3,
        save=save,
        marine_directory=MARINE_DIRECTORY,
        convert=_echo,
    )
    parallel_ms = (time.monotonic() - started) * 1000.0

    print(
        f"\nbound-run fake workload: serial={serial_ms:.1f} ms "
        f"concurrency=3 {parallel_ms:.1f} ms speedup={serial_ms / parallel_ms:.2f}x"
    )

    assert [item.member_id for item in serial] == list(MEMBERS)
    assert [item.member_id for item in parallel] == list(MEMBERS)
    assert sorted(saved) == sorted(MEMBERS * 2)
    assert serial_ms / parallel_ms >= 1.8
