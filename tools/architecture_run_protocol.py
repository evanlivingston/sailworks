"""A0 whole-path priming and supervising-process contracts, not scanout timing."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import signal
import time


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prime_quick(widget, milliseconds):
    """Equal event-loop priming, with no grabs or per-input render waits."""
    from PySide6.QtCore import QEventLoop, QTimer, Qt
    from PySide6.QtGui import QRhiRenderTarget
    from PySide6.QtQuick import QSGRendererInterface
    import shiboken6

    if milliseconds < 1000:
        raise ValueError("Architecture priming must last at least one second")
    loop, pulse, stop = QEventLoop(), QTimer(), QTimer()
    pulse.setTimerType(Qt.TimerType.PreciseTimer)
    pulse.setInterval(16)
    stop.setSingleShot(True)
    stop.setTimerType(Qt.TimerType.PreciseTimer)
    stop.timeout.connect(loop.quit)
    began, ticks, callbacks = time.perf_counter(), [], []
    quick = widget.quickWindow()
    targets = []

    def rendered():
        callbacks.append(time.perf_counter() - began)
        if not targets:
            pointer = quick.rendererInterface().getResource(
                quick, QSGRendererInterface.Resource.RhiRedirectRenderTarget
            )
            if pointer:
                target = shiboken6.wrapInstance(pointer, QRhiRenderTarget)
                size = target.pixelSize()
                targets.append(
                    dict(
                        samples=target.sampleCount(),
                        width=size.width(),
                        height=size.height(),
                        dpr=target.devicePixelRatio(),
                    )
                )

    def update():
        ticks.append(time.perf_counter() - began)
        widget.update()

    quick.afterRendering.connect(rendered, Qt.ConnectionType.DirectConnection)
    pulse.timeout.connect(update)
    try:
        pulse.start()
        stop.start(milliseconds)
        loop.exec()
    finally:
        pulse.stop()
        stop.stop()
        quick.afterRendering.disconnect(rendered)
    return dict(
        protocol="equal-native-event-loop-priming-v1",
        requested_ms=milliseconds,
        elapsed_ms=(time.perf_counter() - began) * 1000,
        gui_tick_seconds=ticks,
        render_callback_seconds=callbacks,
        readback=False,
        effective_render_target=targets[0] if targets else None,
    )


def supervise(command, cwd, environment, output_prefix, timeout=110, cancel=None):
    """Persist process outcome independent of whether Qt wrote a replay report."""
    prefix = Path(output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    paths = {
        suffix: prefix.with_suffix("." + suffix)
        for suffix in ("stdout.log", "stderr.log", "status.json")
    }
    if any(path.exists() for path in paths.values()):
        raise ValueError("Refusing to overwrite supervised evidence")
    began = time.perf_counter()
    status = dict(
        command=list(map(str, command)),
        cwd=str(cwd),
        timeout_s=timeout,
        timed_out=False,
        exit_code=None,
        exception=None,
        cancelled=False,
        environment={
            key: environment.get(key)
            for key in (
                "PYTHONHASHSEED",
                "QT_SCALE_FACTOR",
                "QT_QPA_PLATFORM",
                "QSG_RHI_BACKEND",
                "PYTHONPATH",
            )
        },
    )
    with (
        paths["stdout.log"].open("w") as stdout,
        paths["stderr.log"].open("w") as stderr,
    ):
        try:
            child = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            try:
                if cancel is None:
                    status["exit_code"] = child.wait(timeout=timeout)
                else:
                    deadline = began + timeout
                    while child.poll() is None:
                        if cancel.is_set():
                            status["cancelled"] = True
                            raise subprocess.TimeoutExpired(command, timeout)
                        remaining = deadline - time.perf_counter()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(command, timeout)
                        try:
                            child.wait(timeout=min(0.05, remaining))
                        except subprocess.TimeoutExpired:
                            pass
                    status["exit_code"] = child.returncode
            except subprocess.TimeoutExpired:
                status["timed_out"] = not status["cancelled"]
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                status["exit_code"] = child.returncode
        except subprocess.TimeoutExpired:
            status["timed_out"] = True
        except OSError as exc:
            status["exception"] = str(exc)
    status.update(
        duration_s=time.perf_counter() - began,
        stdout_sha256=file_hash(paths["stdout.log"]),
        stderr_sha256=file_hash(paths["stderr.log"]),
    )
    paths["status.json"].write_text(json.dumps(status, indent=2) + "\n")
    return status


def require_clean(status):
    if (
        status["exit_code"] != 0
        or status["timed_out"]
        or status["exception"]
        or status.get("cancelled", False)
    ):
        raise ValueError(
            "Native child did not exit cleanly; see persisted process status/logs"
        )


def environment():
    env = dict(os.environ, PYTHONHASHSEED="0", QT_SCALE_FACTOR="1", GWR_GPU_WEATHER="1")
    env.pop("QT_QPA_PLATFORM", None)
    return env
