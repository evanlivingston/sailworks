"""Native interaction gate for the standalone viewport (not presentation FPS)."""

import argparse
from dataclasses import replace
from datetime import datetime, timedelta
import json
import importlib.util
from pathlib import Path
import sys
from threading import Event
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(snapshot):
    from PySide6.QtCore import QEvent, QEventLoop, QPointF, Qt, QTimer
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    # Load by path: an installed package named tools can shadow this repo's
    # namespace directory. This is also how gate_runner loads its registry.
    spec = importlib.util.spec_from_file_location(
        "viewport_fixture", ROOT / "tools/benchmark_startup_rendering.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    from gweatherrouting.qt.gribviewport import (
        GribViewportModel,
        PreparedResource,
        ViewportCamera,
        _covers,
        _generation_key,
        _viewport_widget_class,
        saved_selection,
    )
    from gweatherrouting.rendering.windrenderer import WindLandMask

    manifest = fixture.verify_snapshot(snapshot)
    paths, settings = saved_selection(snapshot / "data")
    report = {
        "snapshot_sha256": manifest["content_sha256"],
        "endpoint": "afterRendering proxy; not presentation FPS or GPU uploads",
        "visual_fidelity": "failed/deferred by user; not evaluated",
        "phases": [],
        "errors": [],
    }
    app = QApplication.instance() or QApplication([sys.argv[0]])
    model = GribViewportModel(paths, land_mask=WindLandMask(snapshot / "data"))
    widget = _viewport_widget_class()(model)
    widget.resize(900, 700)
    widget.show()
    camera = ViewportCamera(
        center_lat=48.30887922260005,
        center_lon=-123.09473088970692,
        zoom=9.837590787619122,
        width=900,
        height=700,
        time=datetime.fromisoformat(manifest["forecast_time"]),
        density=settings.get("gribBarbDensity", "very_dense"),
        over_land=settings.get("gribArrowOnGround", False),
    )

    def settle(timeout=15):
        loop = QEventLoop()
        poll = QTimer()
        stop = QTimer()
        stop.setSingleShot(True)
        stop.timeout.connect(loop.quit)
        poll.timeout.connect(
            lambda: loop.quit() if widget._state in ("ready", "unavailable") else None
        )
        stop.start(timeout * 1000)
        poll.start(10)
        loop.exec()
        poll.stop()
        stop.stop()
        return widget._state == "ready"

    def check_current():
        if widget._state == "ready":
            assert _covers(
                model._field, widget._camera
            ), "Stale field presented as current"
            assert widget._published == model._field.generation
        else:
            assert not widget._weather.isVisible(), "Stale weather visible on miss"

    def wait_idle(timeout=5):
        deadline = time.perf_counter() + timeout
        while (
            model._active is not None or model._pending is not None
        ) and time.perf_counter() < deadline:
            loop = QEventLoop()
            QTimer.singleShot(10, loop.quit)
            loop.exec()
        return model._active is None and model._pending is None

    def mouse(kind, x, button, buttons):
        point = QPointF(x, 350)
        QApplication.sendEvent(
            widget,
            QMouseEvent(
                kind,
                point,
                QPointF(widget.mapToGlobal(point.toPoint())),
                button,
                buttons,
                Qt.NoModifier,
            ),
        )

    def phase(name, count, action):
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        drain = QTimer()
        drain.setSingleShot(True)
        drain.timeout.connect(loop.quit)
        phase_report = {"name": name, "inputs": [], "render_observed_revisions": 0}
        seen = set()
        state = {"revision": -1}
        before = widget.publication_counters()
        preparations = model.counters["field_preparations"]
        origin = time.perf_counter()

        def rendered():
            seen.add(state["revision"])

        def dispatch():
            revision = state["revision"] + 1
            state["revision"] = revision
            began = time.perf_counter()
            try:
                action(revision)
                check_current()
                phase_report["inputs"].append(
                    {
                        "dispatch_late_ms": (began - origin - revision / 30) * 1000,
                        "handler_ms": (time.perf_counter() - began) * 1000,
                        "state": widget._state,
                    }
                )
            except Exception as exc:
                report["errors"].append(f"{name}: {exc}")
                loop.quit()
                return
            if revision + 1 == count:
                drain.start(100)
            else:
                timer.start(
                    max(
                        0,
                        round(
                            (origin + (revision + 1) / 30 - time.perf_counter()) * 1000
                        ),
                    )
                )

        widget.quickWindow().afterRendering.connect(rendered)
        timer.timeout.connect(dispatch)
        timer.start(0)
        loop.exec()
        widget.quickWindow().afterRendering.disconnect(rendered)
        phase_report["render_observed_revisions"] = len(seen - {-1})
        phase_report["settled_ready"] = settle()
        check_current()
        phase_report["field_preparations"] = (
            model.counters["field_preparations"] - preparations
        )
        phase_report["publication_delta"] = {
            key: value - before[key]
            for key, value in widget.publication_counters().items()
        }
        report["phases"].append(phase_report)
        assert phase_report["settled_ready"], f"{name} did not settle: {model.error}"

    try:
        began = time.perf_counter()
        widget.set_camera(camera)
        assert settle(), model.error or "Cold load timed out"
        report["cold_ready_seconds"] = time.perf_counter() - began
        report["selected_sources"] = len(paths)
        report["source_order_preserved"] = (
            tuple(source.path for source in model._sources) == paths
        )
        # Larger real mouse motion, crossing multiple field windows, then return.
        mouse(QEvent.MouseButtonPress, 450, Qt.LeftButton, Qt.LeftButton)
        phase(
            "sustained-pan",
            120,
            lambda i: mouse(
                QEvent.MouseMove,
                450 - (i + 1) * 12,
                Qt.NoButton,
                Qt.LeftButton,
            ),
        )
        mouse(QEvent.MouseButtonRelease, -990, Qt.LeftButton, Qt.NoButton)
        widget.set_camera(camera)
        assert settle(), "Return failed"
        phase(
            "anchored-zoom",
            24,
            lambda i: widget.zoom_at(0.05 if i < 12 else -0.05, 300, 250),
        )
        widget.set_camera(camera)
        assert settle(), "Zoom return failed"
        phase(
            "time-crossings",
            18,
            lambda i: widget.set_time(camera.time + timedelta(hours=i % 9)),
        )
        widget.set_camera(camera)
        assert settle(), "Time return failed"
        # Structural observation: a preparation completed after its request was
        # cancelled must be rejected, never published.  The cancellation is
        # explicit: a time change alone no longer cancels active work.
        real_prepare = model._prepare
        superseded_entered = Event()
        held = model._field

        def superseded(camera_, cancel):
            superseded_entered.set()
            cancel.wait(5)
            return held

        published_before = set(model.observations()["published_demands"])
        rejected_before = set(model.observations()["rejected_demands"])
        stale_before = model.counters["stale_rejected"]
        model._prepare = superseded
        widget.set_camera(replace(camera, time=camera.time + timedelta(days=90)))
        superseded_entered.wait(5)
        with model._condition:
            if model._active is not None:
                model._active[1].set()
        widget.set_camera(replace(camera, time=camera.time + timedelta(days=91)))
        deadline = time.perf_counter() + 5
        while (
            model.counters["stale_rejected"] == stale_before
            and time.perf_counter() < deadline
        ):
            wait = QEventLoop()
            QTimer.singleShot(10, wait.quit)
            wait.exec()
        model._prepare = real_prepare
        with model._condition:
            for request in (model._pending, model._active):
                if request is not None:
                    request[1].set()
            model._pending = None
        widget.set_camera(camera)
        assert settle(), "Supersession probe did not return to ready"
        assert wait_idle(), "Supersession probe left work in flight"
        superseded = model.observations()
        rejected_now = set(superseded["rejected_demands"]) - rejected_before
        published_now = set(superseded["published_demands"]) | published_before
        report["superseded_rejected_observed"] = len(rejected_now)
        report["superseded_never_published"] = bool(rejected_now) and not (
            rejected_now & published_now
        )
        # Structural observation: a prepared resource whose generation no
        # longer matches its field may not be presented.
        assert wait_idle(), "Supersession probe left work in flight"
        with model._condition:
            resource = next((item for item in model._slots if item is not None), None)
            if resource is None:
                resource = PreparedResource(
                    field=model._field,
                    generation=_generation_key(model._field),
                    demand=model._request_key(camera),
                    slot=0,
                    span=(0.0, 0.0),
                )
            saved_slots = list(model._slots)
            saved_front = model._front_index
            saved_field = model._field
            saved_state = model._state
            saved_store = model._store
            stale = replace(resource, generation=("stale-generation",), slot=0)
            model._slots = [stale, None]
            model._front_index = 0
            model._field = None
            model._store = type(saved_store)(saved_store.byte_limit)
        generation_before = model.counters["generation_rejected"]
        model._prepare = lambda camera_, cancel: None
        mismatch_state = widget.set_camera(camera)
        with model._condition:
            model._slots = saved_slots
            model._front_index = saved_front
            model._field = saved_field
            model._state = saved_state
            model._store = saved_store
            if model._pending is not None:
                model._pending[1].set()
                model._pending = None
        model._prepare = real_prepare
        widget.set_camera(camera)
        assert settle(), "Generation probe did not return to ready"
        report["generation_mismatch_rejected"] = (
            mismatch_state == "loading"
            and model.counters["generation_rejected"] > generation_before
            and "stale-generation" not in widget.observations()["published_generations"]
        )
        assert wait_idle(), "Generation probe left work in flight"
        observations = model.observations()
        report["sampling_memo"] = observations.get("sampling_memo", {})
        presentation = [
            span
            for span in widget.observations()["presentation_spans"]
            if span[2] == "ready"
        ]
        preparation = observations["preparation_spans"]
        report["preparation_span_count"] = len(preparation)
        report["presentation_span_count"] = len(presentation)
        report["prepared_while_presenting"] = sum(
            1
            for began, ended in preparation
            if any(
                began < stop and start < ended for start, stop, _state in presentation
            )
        )
        report["slot_handoffs"] = observations["slot_handoffs"]
        report["superseded_retained_observed"] = observations["superseded_retained"]
        report["time_lookaheads_observed"] = observations["time_lookaheads"]
        # Explicit cold miss and pending close; loading must not disable camera input.
        entered = Event()

        def blocked(camera, cancel):
            entered.set()
            cancel.wait(10)
            return None

        model._prepare = blocked
        assert (
            widget.set_camera(replace(camera, time=camera.time + timedelta(days=90)))
            == "loading"
        )
        wait = QEventLoop()
        QTimer.singleShot(100, wait.quit)
        wait.exec()
        old_lon = widget._camera.center_lon
        widget.pan_pixels(5, 0)
        report["stalled_camera_responsive"] = (
            entered.is_set() and old_lon != widget._camera.center_lon
        )
        report["honest_loading"] = (
            widget._state == "loading"
            and widget._status.isVisible()
            and not widget._weather.isVisible()
        )
        stats = model.field_statistics()
        report["retained_field_bytes"] = stats["current_bytes"]
        report["retained_field_entries"] = stats["retained_entries"]
        report["retained_field_evictions"] = stats["evictions"]
        report["retained_field_limit"] = model._store.byte_limit
        report["retained_provider_bytes"] = widget._provider.field_bytes()
        report["retained_provider_limit"] = widget._provider.byte_limit
        report["retained_budgets_respected"] = (
            stats["current_bytes"] <= model._store.byte_limit
            and widget._provider.field_bytes() <= widget._provider.byte_limit
        )
        report["counters"] = dict(model.counters)
    except Exception as exc:
        report["errors"].append(str(exc))
    finally:
        began = time.perf_counter()
        widget.close()
        report["close_ms"] = (time.perf_counter() - began) * 1000
        if model._thread is not None:
            model._thread.join(5)
        report["worker_stopped"] = model._thread is None or not model._thread.is_alive()
    report["complete"] = not report["errors"] and len(report["phases"]) == 3
    report["continuous_weather"] = report["complete"] and all(
        phase["inputs"] and all(event["state"] == "ready" for event in phase["inputs"])
        for phase in report["phases"]
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.snapshot)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "phases"}))
    # The gate runner evaluates the completed report, including recorded failures.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
