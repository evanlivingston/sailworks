"""B1 embedded Qt render-signal probe; never a scanout/GPU-completion timer.

The recorder is Qt-independent so attribution can be tested deterministically.
Qt adapters timestamp direct render callbacks, not their queued GUI delivery.
"""

from threading import Lock, get_ident
import time

PROTOCOL = "embedded-after-rendering-v1"
SCENARIOS = ("stationary", "trackpad_pan", "reverse_pan", "scrub_return")


class RenderRecorder:
    def __init__(self):
        self.lock = Lock()
        self.inputs = []
        self.submissions = []
        self.callbacks = []
        self.latest = None
        self.synchronized = None

    def dispatch(self, intended, actual, state):
        with self.lock:
            revision = len(self.inputs)
            self.inputs.append(
                dict(
                    revision=revision,
                    intended=intended,
                    dispatched=actual,
                    dispatch_late_ms=max(0, actual - intended) * 1000,
                    state=state,
                )
            )
            return revision

    def submit(self, sequence, revision, stamp, state, content=None):
        with self.lock:
            record = dict(
                sequence=sequence,
                revision=revision,
                submitted=stamp,
                state=state,
                content=content,
                thread=get_ident(),
            )
            self.submissions.append(record)
            self.latest = record

    def synchronize(self):
        with self.lock:
            self.synchronized = self.latest

    def rendered(self, stamp):
        with self.lock:
            self.callbacks.append(
                dict(
                    submission=self.synchronized,
                    after_rendering=stamp,
                    thread=get_ident(),
                )
            )

    def result(self):
        with self.lock:
            first = {}
            for callback in self.callbacks:
                submission = callback["submission"]
                if submission is not None and submission["revision"] is not None:
                    revision = submission["revision"]
                    if (
                        callback["after_rendering"]
                        >= self.inputs[revision]["dispatched"]
                    ):
                        first[revision] = min(
                            first.get(revision, float("inf")),
                            callback["after_rendering"],
                        )
            newest = max(first, default=-1)
            inputs = []
            for record in self.inputs:
                revision = record["revision"]
                observed = first.get(revision)
                inputs.append(
                    dict(
                        **record,
                        status=(
                            "observed"
                            if observed is not None
                            else (
                                "superseded_unobserved"
                                if revision < newest
                                else "timeout_unobserved"
                            )
                        ),
                        input_to_after_rendering_ms=(
                            (observed - record["dispatched"]) * 1000
                            if observed is not None
                            else None
                        ),
                        intended_to_after_rendering_ms=(
                            (observed - record["intended"]) * 1000
                            if observed is not None
                            else None
                        ),
                    )
                )
            return dict(
                protocol=PROTOCOL,
                endpoint="QQuickWindow.afterRendering (embedded offscreen scene)",
                presentation_latency_measured=False,
                gpu_completion_measured=False,
                usable_data_coverage_measured=False,
                inputs=inputs,
                submissions=list(self.submissions),
                callbacks=list(self.callbacks),
            )


def view_state(canvas):
    return dict(
        latitude=canvas._center_lat,
        longitude=canvas._center_lon,
        zoom=canvas._zoom,
        forecast_time=canvas.time_control.time.isoformat(),
    )


def replay(canvas, config):
    """Deadline-paced input without waiting for each render or forcing paints.

    GUI dispatch can still be late when the GUI is blocked; the intended times
    remain fixed. All inputs, including unobserved revisions, remain in output.
    """
    from datetime import timedelta
    from PySide6.QtCore import QEventLoop, QPoint, QPointF, QTimer, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication

    gpu = canvas._gpu
    if gpu is None or not gpu.active or not gpu.initialized:
        raise RuntimeError("Native render probe requires initialized GPU presenter")
    if config["scenario"] not in SCENARIOS:
        raise ValueError(f"Native render probe scenarios: {SCENARIOS}")
    recorder = RenderRecorder()
    scene = gpu.scene
    original_present = scene.present
    quick = gpu.widget.quickWindow()
    current_revision = None
    frame_metadata = config.get("_frame_metadata")

    def present(frame):
        previous = scene.frame
        original_present(frame)
        if scene.frame is not previous:
            content = dict(
                base_image_key=frame.base.image.cacheKey(),
                navigation_image_key=frame.navigation.image.cacheKey(),
                raster_wind_key=(
                    frame.raster_wind.image.cacheKey() if frame.raster_wind else None
                ),
                wind_batches=[
                    dict(key=batch.key, vertices=len(batch.vertices))
                    for batch in frame.wind
                ],
            )
            if frame_metadata is not None:
                content["numerical_weather"] = frame_metadata(canvas)
            recorder.submit(
                frame.sequence,
                current_revision,
                time.perf_counter(),
                view_state(canvas),
                content,
            )

    def rendered():
        recorder.rendered(time.perf_counter())

    scene.present = present
    quick.beforeSynchronizing.connect(
        recorder.synchronize, Qt.ConnectionType.DirectConnection
    )
    quick.afterRendering.connect(rendered, Qt.ConnectionType.DirectConnection)
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    drain = QTimer()
    drain.setSingleShot(True)
    drain.timeout.connect(loop.quit)
    began = time.perf_counter()
    interval = config["interval_ms"] / 1000
    initial_time = canvas.time_control.time
    counters_before = dict(scene.counters)
    errors = []
    index = 0

    def dispatch():
        nonlocal index, current_revision
        try:
            if not gpu.active:
                raise RuntimeError("GPU fallback during native render probe")
            current_revision = recorder.dispatch(
                began + index * interval, time.perf_counter(), view_state(canvas)
            )
            scenario = config["scenario"]
            if scenario in ("trackpad_pan", "reverse_pan"):
                canvas._trackpad_scroll_pans = True
                point = QPointF(canvas.width() / 2, canvas.height() / 2)
                direction = (
                    -1
                    if scenario == "reverse_pan" and index >= config["frames"] // 2
                    else 1
                )
                event = QWheelEvent(
                    point,
                    point,
                    QPoint(-config.get("trackpad_step_px", 16) * direction, 0),
                    QPoint(),
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier,
                    (
                        Qt.ScrollPhase.ScrollBegin
                        if index == 0
                        else Qt.ScrollPhase.ScrollUpdate
                    ),
                    False,
                )
                QApplication.sendEvent(canvas, event)
            elif scenario == "scrub_return":
                canvas.time_control.set_time(
                    initial_time
                    + timedelta(
                        seconds=(
                            index
                            if index < config["frames"] // 2
                            else config["frames"] - 1 - index
                        )
                    )
                )
            canvas.update()
            index += 1
            if index == config["frames"]:
                drain.start(round(config["native_drain_timeout_s"] * 1000))
            else:
                timer.start(
                    max(
                        0,
                        round((began + index * interval - time.perf_counter()) * 1000),
                    )
                )
        except Exception as exc:
            errors.append(exc)
            loop.quit()

    timer.timeout.connect(dispatch)
    try:
        timer.start(0)
        loop.exec()
    finally:
        timer.stop()
        drain.stop()
        quick.beforeSynchronizing.disconnect(recorder.synchronize)
        quick.afterRendering.disconnect(rendered)
        scene.present = original_present
    if errors:
        raise errors[0]
    if not gpu.active:
        raise RuntimeError("GPU fallback during native render probe")
    result = recorder.result()
    result["gpu_counter_delta"] = {
        key: value - counters_before[key] for key, value in scene.counters.items()
    }
    return result
