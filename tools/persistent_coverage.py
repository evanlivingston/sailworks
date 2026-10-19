"""Experiment-only persistent offline preparation service and bounded client."""

import json
import os
from pathlib import Path
import sys
from threading import Event, Thread
import time

import architecture_run_protocol as protocol

ROOT = Path(__file__).resolve().parents[1]


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def serve_requests(
    canvas,
    app,
    settle,
    grab,
    config,
    export,
    apply_view,
    validation_export=None,
    request_frame=None,
):
    """Runs in the isolated preparation app, never the interactive GUI process."""
    root = Path(config["service_directory"])
    pid, last, completed = os.getpid(), 0, []
    atomic_json(
        root / "ready.json", dict(pid=pid, snapshot_sha256=config["snapshot_sha256"])
    )
    while not (root / "stop.json").exists():
        app.processEvents()
        validation = root / "validate.json"
        if (
            validation_export
            and validation.exists()
            and not (root / "validation-result.json").exists()
        ):
            wanted = json.loads(validation.read_text())["generations"]
            checked = []
            for generation in wanted:
                result = dict(generation=generation, error=None, pid=pid)
                try:
                    original = next(
                        record
                        for record in completed
                        if record["generation"] == generation
                    )
                    destination = root / f"validation-{generation}"
                    destination.mkdir(exist_ok=False)
                    apply_view(canvas, original["request_view"])
                    grab()
                    settle()
                    grab()
                    result["fidelity"] = validation_export(
                        canvas,
                        app,
                        destination,
                        config["snapshot_sha256"],
                        stamps=[original["request_stamp"]],
                    )
                    result["directory"] = str(destination)
                except Exception as exc:
                    result["error"] = str(exc)
                checked.append(result)
            atomic_json(root / "validation-result.json", dict(completed=checked))
        mailbox = root / "request.json"
        request = json.loads(mailbox.read_text()) if mailbox.exists() else None
        if request and request["generation"] > last:
            last = request["generation"]
            atomic_json(root / "active.json", dict(generation=last, pid=pid))
            began = time.perf_counter()
            response = dict(generation=last, pid=pid, error=None)
            response.update(
                request_view=request["view"], request_stamp=request["stamp"]
            )
            phases = {}
            mark = began

            def phase(name):
                nonlocal mark
                now = time.perf_counter()
                phases[name] = now - mark
                mark = now

            try:
                destination = root / f"generation-{last}"
                destination.mkdir(exist_ok=False)
                apply_view(canvas, request["view"])
                phase("apply_view_seconds")
                (request_frame or grab)()
                phase(
                    "request_no_readback_seconds"
                    if request_frame
                    else "request_and_first_readback_seconds"
                )
                settle()
                phase("overlay_settle_seconds")
                (request_frame or grab)()
                phase(
                    "settled_no_readback_seconds"
                    if request_frame
                    else "settled_readback_seconds"
                )
                response["fidelity"] = export(
                    canvas,
                    app,
                    destination,
                    config["snapshot_sha256"],
                    stamps=[request["stamp"]],
                )
                if validation_export and (
                    not response["fidelity"].get("oracle_pending")
                    or response["fidelity"]["exact_reference_selections"] != 0
                ):
                    raise ValueError(
                        "Timed preparation unexpectedly invoked oracle/readback"
                    )
                phase("export_and_numerical_oracle_seconds")
                response["directory"] = str(destination)
            except Exception as exc:
                response["error"] = str(exc)
            response["prepare_seconds"] = time.perf_counter() - began
            response["phase_seconds"] = phases
            completed.append(response)
            atomic_json(root / f"response-{last}.json", response)
        time.sleep(0.005)
    return dict(protocol="persistent-coverage-service-v1", pid=pid, completed=completed)


class PersistentPreparation:
    def __init__(self, directory, app_root, snapshot, data_policy="legacy"):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.cancel = Event()
        self.status = None
        self.closed = False
        self.began = time.perf_counter()
        self.ready_seconds = None
        env = protocol.environment()
        env["PYTHONPATH"] = os.pathsep.join((str(app_root), str(ROOT)))
        command = [
            sys.executable,
            str(ROOT / "tools/experiment_vertical_renderer.py"),
            "--app-root",
            str(app_root),
            "--snapshot",
            str(snapshot),
            "--resources",
            str(self.directory / "initial"),
            "--output",
            str(self.directory / "service-result.json"),
            "--scenario",
            "stationary",
            "--scrub-steps",
            "1",
            "--service-directory",
            str(self.directory),
            "--data-policy",
            data_policy,
        ]

        def run():
            self.status = protocol.supervise(
                command,
                ROOT,
                env,
                self.directory / "service",
                timeout=90,
                cancel=self.cancel,
            )

        self.thread = Thread(target=run, name="persistent-preparation-supervisor")
        self.thread.start()

    def prepare(self, request, view, stamp, cancel):
        ready = self.directory / "ready.json"
        while not ready.exists():
            self._check_alive()
            if cancel.wait(0.01):
                raise RuntimeError(
                    "Preparation request superseded before service ready"
                )
        if self.ready_seconds is None:
            self.ready_seconds = time.perf_counter() - self.began
        identity = json.loads(ready.read_text())
        atomic_json(
            self.directory / "request.json",
            dict(generation=request.generation, view=view, stamp=stamp),
        )
        result = self.directory / f"response-{request.generation}.json"
        # An active export is not interrupted. One latest pending request is kept
        # by CoverageScheduler; the superseded result is discarded before bind.
        while not result.exists():
            self._check_alive()
            time.sleep(0.01)
        response = json.loads(result.read_text())
        if (
            response["pid"] != identity["pid"]
            or response["generation"] != request.generation
        ):
            raise ValueError("Preparation response identity differs")
        if cancel.is_set():
            raise RuntimeError("Completed preparation request superseded")
        if response["error"]:
            raise RuntimeError(response["error"])
        return Path(response["directory"]), response

    def observe_ready(self):
        ready = self.directory / "ready.json"
        self._check_alive()
        if not ready.exists():
            return False
        if self.ready_seconds is None:
            self.ready_seconds = time.perf_counter() - self.began
        return True

    def _check_alive(self):
        if self.closed or not self.thread.is_alive():
            raise RuntimeError(
                "Persistent preparation service stopped; see service status/log"
            )

    def validate(self, generations):
        atomic_json(
            self.directory / "validate.json", dict(generations=list(generations))
        )
        result = self.directory / "validation-result.json"
        while not result.exists():
            self._check_alive()
            time.sleep(0.01)
        value = json.loads(result.read_text())
        if any(record["error"] for record in value["completed"]):
            raise ValueError(f"Post-window validation rejected: {value}")
        return value

    def close(self):
        if self.closed:
            return
        self.closed = True
        atomic_json(self.directory / "stop.json", dict(stop=True))
        self.thread.join(5)
        if self.thread.is_alive():
            self.cancel.set()
            self.thread.join(7)
        if self.thread.is_alive():
            raise RuntimeError("Persistent preparation process failed to stop")
