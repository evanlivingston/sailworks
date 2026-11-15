"""Choose how worker processes start, without forking a threaded parent.

Worker pools must not fork the application process, because by the time a
route or a log import runs, the parent holds live network threads (gRPC and
HTTP clients). Forking such a process makes the C libraries skip their
fork handlers and leaves stale descriptors in the child, which shows up as
``fork_posix: Other threads are currently calling into gRPC`` and, more
rarely, as a hung child.

``spawn`` and ``forkserver`` both avoid that: the child never runs application
code before ``exec``. ``forkserver`` additionally forks later children from a
dedicated, thread-free helper, so a run that starts several workers pays the
fork once instead of once per worker.
"""

from __future__ import annotations

import multiprocessing
import sys

#: Start method used on platforms that have no ``forkserver`` (Windows).
_FALLBACK = "spawn"


def worker_method() -> str:
    """The preferred interpreter start method for this platform."""
    if sys.platform == "win32":
        return _FALLBACK
    return "forkserver"


def worker_context():
    """A multiprocessing context that never forks the threaded application.

    Falls back to ``spawn`` where ``forkserver`` is unavailable, which is still
    safe: like ``forkserver``, it never runs application code in a child that
    was forked from this process.
    """
    try:
        return multiprocessing.get_context(worker_method())
    except ValueError:
        return multiprocessing.get_context(_FALLBACK)
