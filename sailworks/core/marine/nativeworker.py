"""Bounded native routing worker process.

One worker slot isolates the native engine from the caller. A new submission
supersedes the running generation, so a superseded calculation can never
publish a result. Cancellation terminates the process, and an abnormal exit is
reported as a failure rather than as "no feasible route".

Progress is lifecycle-only: the vendored bridge exposes no intra-solve progress
callback, so this module does not invent one.
"""

from __future__ import annotations

import multiprocessing
from dataclasses import dataclass
from typing import Any, Dict, Optional

STATES = frozenset({"queued", "running", "completed", "cancelled", "failed"})


@dataclass
class NativeJobStatus:
    generation: int
    state: str
    payload: Optional[Dict[str, Any]] = None
    message: str = ""
    exit_code: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generation": self.generation,
            "state": self.state,
            "payload": self.payload,
            "message": self.message,
            "exit_code": self.exit_code,
        }


def _worker_main(connection, job, engine: str) -> None:
    """Child-process entry point; import provider handles only after spawn."""
    try:
        if job is None:
            import sailworks_native_routing as bridge

            payload = bridge.synthetic_route(engine)
        else:
            from .nativeexecution import run_native_request

            payload = run_native_request(job)
        connection.send({"kind": "result", "payload": payload})
    except BaseException as exc:  # pragma: no cover - child process boundary
        connection.send({"kind": "failed", "message": str(exc)})
    finally:
        connection.close()


class NativeRoutingWorker:
    """Run one native calculation at a time in a spawned process."""

    def __init__(self, *, max_workers: int = 1, context=None):
        if max_workers != 1:
            raise ValueError("only one bounded native worker is supported")
        self.max_workers = max_workers
        self._context = context or multiprocessing.get_context("spawn")
        self._process = None
        self._connection = None
        self._generation = 0
        self._active_generation: Optional[int] = None
        self._superseded_generation: Optional[int] = None
        self._status: Optional[NativeJobStatus] = None
        self._status_consumed = False

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.is_alive()

    @property
    def superseded_generation(self) -> Optional[int]:
        """Generation replaced by the most recent submit, if any."""
        return self._superseded_generation

    @property
    def status(self) -> Optional[NativeJobStatus]:
        return self._status

    def submit(self, job) -> int:
        """Run a selected request; no fixture fallback on missing inputs."""
        from .nativerequest import NativeRequest

        if not isinstance(job, NativeRequest):
            raise TypeError("submit requires a frozen NativeRequest")
        return self._start(job, job.engine)

    def submit_synthetic(self, *, engine: str = "main") -> int:
        """Explicit diagnostic fixture only; never a selected user request."""
        return self._start(None, engine)

    def _start(self, job, engine: str) -> int:
        """Start a bounded process, superseding any running generation."""
        self._supersede()
        self._generation += 1
        generation = self._generation
        parent, child = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=_worker_main, args=(child, job, engine), daemon=True
        )
        try:
            process.start()
        except BaseException:
            parent.close()
            child.close()
            self._status = None
            self._status_consumed = False
            self._active_generation = None
            raise
        child.close()
        self._process = process
        self._connection = parent
        self._active_generation = generation
        self._status = NativeJobStatus(generation, "running")
        self._status_consumed = False
        return generation

    def poll(self, timeout: float = 0.0) -> Optional[NativeJobStatus]:
        """Return the terminal status once, or None while still running.

        A terminal status stays inspectable through ``.status``. It is delivered
        exactly once: by this call, by ``cancel()``, or by whichever runs first.
        A caller that polls after ``cancel()`` still observes the cancellation.
        """
        if self._status is not None and self._status.state != "running":
            if self._status_consumed:
                return None
            self._status_consumed = True
            return self._status
        if self._active_generation is None:
            return None
        generation = self._active_generation
        if not self._connection.poll(timeout):
            if self._process is not None and not self._process.is_alive():
                return self._publish(
                    NativeJobStatus(
                        generation,
                        "failed",
                        message="worker exited without publishing a result",
                        exit_code=self._process.exitcode,
                    )
                )
            return None
        try:
            message = self._connection.recv()
        except EOFError:
            return self._publish(
                NativeJobStatus(
                    generation,
                    "failed",
                    message="worker exited without publishing a result",
                    exit_code=None if self._process is None else self._process.exitcode,
                )
            )
        self._release()
        if message.get("kind") == "result":
            return self._publish(
                NativeJobStatus(generation, "completed", payload=message["payload"])
            )
        return self._publish(
            NativeJobStatus(
                generation,
                "failed",
                message=str(message.get("message", "worker failed")),
            )
        )

    def cancel(self) -> Optional[NativeJobStatus]:
        """Terminate the running worker; never publishes a completion."""
        if self._active_generation is None:
            return None
        generation = self._active_generation
        self._terminate()
        # The caller gets the status here, and a poll-only caller still gets it
        # once from poll(); neither can turn it into a completion.
        return self._publish(
            NativeJobStatus(generation, "cancelled", message="cancelled by caller"),
            consumed=False,
        )

    def shutdown(self) -> None:
        if self._active_generation is not None:
            self._terminate()
        self._active_generation = None
        self._status = None
        self._status_consumed = True

    def _supersede(self) -> None:
        if self._active_generation is None:
            return
        self._superseded_generation = self._active_generation
        self._terminate()

    def _terminate(self) -> None:
        if self._process is not None:
            if self._process.is_alive():
                self._process.terminate()
            self._process.join(timeout=5)
            if self._process.is_alive():  # pragma: no cover - defensive
                self._process.kill()
                self._process.join(timeout=5)
        self._release()

    def _release(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except OSError:  # pragma: no cover - already closed
                pass
        self._connection = None
        self._process = None
        self._active_generation = None

    def _publish(
        self, status: NativeJobStatus, *, consumed: bool = True
    ) -> NativeJobStatus:
        self._release()
        self._status = status
        self._status_consumed = consumed
        return status
