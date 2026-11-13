"""Bounded native routing worker process.

One worker slot isolates the native engine from the caller. A new submission
supersedes the running generation, so a superseded calculation can never
publish a result. Cancellation is cooperative first: a shared event trips the
engine's cancellation token at its progress checkpoint, and termination is the
bounded fallback. An abnormal exit is reported as a failure rather than as "no
feasible route".

The bridge forwards the vendored engine's own ``RoutingProgressUpdate`` values.
They travel over the same pipe as the result, are throttled in the child, and a
terminal result remains the only completion. Progress from a superseded or
cancelled generation is discarded.
"""

from __future__ import annotations

import multiprocessing
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

STATES = frozenset({"queued", "running", "completed", "cancelled", "failed"})
PROGRESS_INTERVAL_SECONDS = 0.1
# Cooperative stop is checked at the engine's progress checkpoint (at most
# once per second). Give it a few checkpoints before terminating the child.
CANCEL_GRACE_SECONDS = 5.0


def _is_alive(process) -> bool:
    """``is_alive`` with a defensive default for simple test doubles."""
    check = getattr(process, "is_alive", None)
    return True if check is None else bool(check())


class NativeProgressThrottle:
    """Bound forwarded progress to stage changes and a fixed cadence."""

    def __init__(self, interval: float = PROGRESS_INTERVAL_SECONDS):
        self.interval = interval
        self._last_emit: Optional[float] = None
        self._last_stage: Optional[str] = None

    def should_emit(self, update: Dict[str, Any], now: float) -> bool:
        stage = update.get("stage")
        emit = (
            self._last_emit is None
            or stage == "complete"
            or stage != self._last_stage
            or now - self._last_emit >= self.interval
        )
        if emit:
            self._last_emit = now
            self._last_stage = stage
        return emit


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


def _worker_main(
    connection, job, engine: str, generation: int, cancel_event=None
) -> None:
    """Child-process entry point; import provider handles only after spawn."""
    throttle = NativeProgressThrottle()

    def report(update):
        if not throttle.should_emit(update, time.monotonic()):
            return
        try:
            connection.send(
                {
                    "kind": "progress",
                    "generation": generation,
                    "payload": update,
                }
            )
        except (BrokenPipeError, EOFError, OSError):
            # Progress is an observer; a closed pipe must not abort the solve.
            pass

    try:
        if job is None:
            import sailworks_native_routing as bridge

            payload = bridge.synthetic_route(engine, progress=report)
        else:
            from .nativeexecution import run_native_request

            payload = run_native_request(
                job, progress=report, cancel_event=cancel_event
            )
        connection.send(
            {"kind": "result", "generation": generation, "payload": payload}
        )
    except ImportError as exc:
        connection.send(
            {
                "kind": "failed",
                "generation": generation,
                "message": f"Native routing module is not built or importable: {exc}",
            }
        )
    except BaseException as exc:  # pragma: no cover - child process boundary
        connection.send(
            {"kind": "failed", "generation": generation, "message": str(exc)}
        )
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
        self._pending_progress: Optional[Dict[str, Any]] = None
        self._cancel_event = self._context.Event()

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def progress(self) -> Optional[Dict[str, Any]]:
        """Latest undelivered engine progress, or None."""
        return self._pending_progress

    @property
    def running(self) -> bool:
        return self._process is not None and _is_alive(self._process)

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
        self._cancel_event.clear()
        parent, child = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=_worker_main,
            args=(child, job, engine, generation, self._cancel_event),
            daemon=True,
        )
        try:
            process.start()
        except BaseException:
            parent.close()
            child.close()
            self._status = None
            self._status_consumed = False
            self._pending_progress = None
            self._active_generation = None
            raise
        child.close()
        self._process = process
        self._connection = parent
        self._active_generation = generation
        self._status = NativeJobStatus(generation, "running")
        self._status_consumed = False
        self._pending_progress = None
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
        if message.get("kind") == "progress":
            self._accept_progress(message.get("generation"), message.get("payload"))
            # Progress never terminates the job; the next poll can read the
            # result that follows it on the pipe.
            return None
        self._release()
        if message.get("kind") == "result":
            payload = message["payload"]
            state = "completed"
            if isinstance(payload, dict) and payload.get("status") == "cancelled":
                # A cooperatively stopped child never reports a completion.
                state = "cancelled"
            return self._publish(NativeJobStatus(generation, state, payload=payload))
        return self._publish(
            NativeJobStatus(
                generation,
                "failed",
                message=str(message.get("message", "worker failed")),
            )
        )

    def take_progress(self) -> Optional[Dict[str, Any]]:
        """Deliver the latest accepted progress update once."""
        update, self._pending_progress = self._pending_progress, None
        return update

    def _accept_progress(self, generation, payload) -> bool:
        if (
            generation != self._active_generation
            or not isinstance(payload, dict)
            or self._status is None
            or self._status.state != "running"
        ):
            return False
        self._pending_progress = payload
        return True

    def cancel(self) -> Optional[NativeJobStatus]:
        """Stop the running worker; never publishes a completion.

        Cooperative first: trip the shared cancel event, which the bridge
        observes at the engine's progress checkpoint, and wait a bounded time
        for the child to exit. Termination is only the fallback.
        """
        if self._active_generation is None:
            return None
        generation = self._active_generation
        self._request_stop()
        self._await_exit(CANCEL_GRACE_SECONDS)
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
        self._pending_progress = None

    def _supersede(self) -> None:
        if self._active_generation is None:
            return
        self._superseded_generation = self._active_generation
        self._terminate()

    def _request_stop(self) -> None:
        """Trip the event the bridge polls at its engine checkpoints."""
        setter = getattr(self._cancel_event, "set", None)
        if setter is None:
            return
        try:
            setter()
        except (ValueError, OSError):  # pragma: no cover - defensive
            pass

    def _await_exit(self, timeout: float) -> None:
        """Give a cooperative child its bounded grace period to exit."""
        join = getattr(self._process, "join", None)
        if join is None:
            return
        try:
            join(timeout=timeout)
        except (AssertionError, ValueError):  # pragma: no cover - defensive
            pass

    def _terminate(self) -> None:
        self._pending_progress = None
        if self._process is not None:
            if _is_alive(self._process):
                terminate = getattr(self._process, "terminate", None)
                if terminate is not None:
                    terminate()
            join = getattr(self._process, "join", None)
            if join is not None:
                join(timeout=5)
                if _is_alive(self._process):  # pragma: no cover - defensive
                    kill = getattr(self._process, "kill", None)
                    if kill is not None:
                        kill()
                    join(timeout=5)
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
