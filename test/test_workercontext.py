"""Worker pools must not fork the threaded application process."""

from __future__ import annotations

import multiprocessing
import sys
import threading

import pytest

from sailworks.core.workercontext import worker_context


def _square(value):
    return value * value


def test_posix_prefers_forkserver():
    method = worker_context().get_start_method()
    if sys.platform == "win32":
        assert method == "spawn"
    else:
        assert method == "forkserver"


def test_the_context_runs_a_pool():
    context = worker_context()
    with context.Pool(2) as pool:
        assert pool.map(_square, [1, 2, 3]) == [1, 4, 9]


def test_the_context_never_forks_the_caller():
    """A fork start method would copy the calling process and its threads."""
    if sys.platform == "win32":
        pytest.skip("no fork start method on Windows")
    assert worker_context().get_start_method() != "fork"


def test_a_worker_survives_a_threaded_parent():
    """The parent has live threads; the child must still start and answer."""
    stop = threading.Event()

    def spin():
        while not stop.is_set():
            pass

    thread = threading.Thread(target=spin, daemon=True)
    thread.start()
    try:
        with worker_context().Pool(1) as pool:
            assert pool.map(_square, [4]) == [16]
    finally:
        stop.set()
        thread.join(timeout=2)
