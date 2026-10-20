"""Native interaction gate for the standalone viewport (not presentation FPS)."""

import argparse
from dataclasses import replace
from datetime import datetime, timedelta
import json
import importlib.util
from pathlib import Path
import shutil
import sys
from threading import Event
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The recorded #71 probe positions: the same places the float-transport probe
# reads the application's weather shader at.
PROBE_POSITIONS = ((0.07, 0.09), (0.5, 0.5), (0.93, 0.91), (0.31, 0.62), (0.72, 0.28))

# The application's unchanged numerical gate.
NUMERICAL_LIMIT = 2e-5


def _load_tool(name, path):
    """Load a repository tool by path, as the runner loads its registry."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _grab(widget):
    """The scene graph's framebuffer as an RGBA array."""
    import numpy as np

    image = widget.grabFramebuffer()
    if image.isNull():
        raise RuntimeError("framebuffer readback failed")
    return (
        np.frombuffer(image.constBits(), np.uint8)
        .reshape(image.height(), image.bytesPerLine())[:, : image.width() * 4]
        .reshape(image.height(), image.width(), 4)
        .copy()
    )


def _vector_at(u0, v0, u1, v1, blend, position):
    """The CPU mirror of the shader's per-corner arithmetic.

    The shader blends the two time endpoints per corner and only then
    accumulates the spatial weights, and it derives the wind speed from the
    interpolated U and V.  The mirror keeps that order: the speed is never
    interpolated.
    """
    import numpy as np

    rows, cols = u0.shape
    point_x = np.float32(position[0]) * np.float32(cols) - np.float32(0.5)
    point_y = np.float32(position[1]) * np.float32(rows) - np.float32(0.5)
    point_x = min(max(point_x, np.float32(0.0)), np.float32(cols - 1))
    point_y = min(max(point_y, np.float32(0.0)), np.float32(rows - 1))
    base_x = int(np.floor(point_x))
    base_y = int(np.floor(point_y))
    delta_x = np.float32(point_x - base_x)
    delta_y = np.float32(point_y - base_y)
    total = np.zeros(2, dtype=np.float32)
    for step_y in (0, 1):
        for step_x in (0, 1):
            weight = np.float32(
                (delta_x if step_x else np.float32(1.0) - delta_x)
                * (delta_y if step_y else np.float32(1.0) - delta_y)
            )
            if weight <= np.float32(1e-12):
                continue
            cell_y = min(base_y + step_y, rows - 1)
            cell_x = min(base_x + step_x, cols - 1)
            first = np.array([u0[cell_y, cell_x], v0[cell_y, cell_x]], dtype=np.float32)
            second = np.array(
                [u1[cell_y, cell_x], v1[cell_y, cell_x]], dtype=np.float32
            )
            if blend <= 0.0:
                value = first
            elif blend >= 1.0:
                value = second
            else:
                value = first + (second - first) * np.float32(blend)
            total = (total + value * weight).astype(np.float32)
    return total


def _speed_comparison(field, blend):
    """The fill's interpolated wind speed against the CPU reference.

    The application's own weather shader is compiled with the #71 probe's
    byte-exact readback, so the float32 the shader wrote can be read back and
    compared with the CPU reference at probe-chosen positions.  The readback
    is a fidelity measurement; it is not a timing measurement.
    """
    import numpy as np
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QImage
    from PySide6.QtQuick import QQuickImageProvider

    from gweatherrouting.qt.gpuweather import decode_component

    tool = _load_tool(
        "float_transport_probe", ROOT / "tools/experiment_float_transport_qsg.py"
    )
    source = (ROOT / "gweatherrouting/qt/shaders/weather.frag").read_text()
    variant = tool.shader_variant(source, float_transport=False)
    # The probe reads the interpolated U; the fill's value is the wind speed,
    # which the shader derives from the interpolated U and V.
    if "floatBitsToUint(uv.x)" not in variant:
        raise RuntimeError("The probe readback mode changed")
    variant = variant.replace("floatBitsToUint(uv.x)", "floatBitsToUint(length(uv))")
    directory = Path(tool.tempfile.mkdtemp(prefix="viewport-fill-"))
    tool.compile_shader(directory, variant, "fillspeed.frag")
    # The probe's default shader name is the application's own; keep the
    # scene's first load valid.
    shutil.copy(
        ROOT / "gweatherrouting/qt/shaders/weather.frag.qsb",
        directory / "weather.frag.qsb",
    )
    probe = tool.Probe(directory)
    outcome = {"limit": NUMERICAL_LIMIT}

    images = {
        "u0": field.images[0],
        "v0": field.images[1],
        "u1": field.images[2],
        "v1": field.images[3],
    }

    class Provider(QQuickImageProvider):
        def __init__(self):
            super().__init__(QQuickImageProvider.ImageType.Image)
            self.requests = 0

        def requestImage(self, identifier, size, requested_size):  # noqa: N802
            self.requests += 1
            image = QImage(images[identifier])
            if size is not None:
                size.setWidth(image.width())
                size.setHeight(image.height())
            return image

    try:
        provider = Provider()
        probe.quick.engine().addImageProvider("fillprobe", provider)
        for name in ("u0Source", "v0Source", "u1Source", "v1Source"):
            probe.root.setProperty(name, QUrl(f"image://fillprobe/{name[0]}{name[1]}"))
        probe.settle(0.3)
        if not probe.set_shader("fillspeed.frag.qsb"):
            raise RuntimeError("The fill's probe shader did not compile")
        probe.root.setProperty("blend", float(blend))
        planes = [decode_component(image) for image in field.images]
        sampled = {}
        errors = []
        for position in PROBE_POSITIONS:
            probe.root.setProperty("uPointX", float(position[0]))
            probe.root.setProperty("uPointY", float(position[1]))
            low = probe.render(-2)
            high = probe.render(-3)
            values = tool.reconstruct_float(low, high)
            alpha = low[..., 3]
            centre = values[values.shape[0] // 2, values.shape[1] // 2]
            reference = _vector_at(*planes, blend, position)
            expected = float(np.hypot(reference[0], reference[1]))
            key = f"{position[0]:.4f},{position[1]:.4f}"
            missing = bool(alpha[alpha.shape[0] // 2, alpha.shape[1] // 2] == 0)
            sampled[key] = {
                "gpu_speed": float(centre),
                "cpu_speed": expected,
                "missing": missing,
            }
            if missing or not np.isfinite(expected):
                continue
            errors.append(abs(float(centre) - expected))
        outcome["positions"] = sampled
        outcome["compared_positions"] = len(errors)
        outcome["max_abs_error"] = float(max(errors)) if errors else None
        outcome["passed"] = bool(errors and max(errors) <= NUMERICAL_LIMIT)
    finally:
        probe.quick.close()
    return outcome


def fill_observations(widget, model, camera, pump):
    """The fill layer's pixel, mask, value and independence evidence."""
    import numpy as np
    from PySide6.QtGui import QColor

    from gweatherrouting.qt.gpuweather import encode_component

    out = {"enabled": widget._fill is not None}
    if widget._fill is None:
        return out
    field = model._field
    blend = float(field.staged.weight(camera.stamp()))

    # The fill is measured alone against a transparent background, so the
    # alpha it contributes is observable: the A0k rule is about that alpha.
    widget.setClearColor(QColor(0, 0, 0, 0))
    widget._barbs.setVisible(False)
    pump(150)
    pixels = _grab(widget)
    out["shader_ready"] = bool(widget._fill.property("ready"))
    out["shader_error"] = str(widget._fill.property("shaderError"))
    out["painted_pixels"] = int((pixels[..., 3] > 0).sum())
    out["total_pixels"] = int(pixels[..., 3].size)

    # The all-missing control: exactly zero alpha, never the field minimum.
    missing = tuple(
        encode_component(np.full((2, 2), np.nan, dtype=np.float32)) for _ in range(4)
    )
    widget.publish_fill(missing, "probe-all-missing")
    pump(250)
    pixels = _grab(widget)
    out["all_missing"] = {
        "alpha_pixels": int((pixels[..., 3] > 0).sum()),
        "significant_alpha_pixels": int((pixels[..., 3] > 25).sum()),
        "total_pixels": int(pixels[..., 3].size),
    }

    # The finite control: painted everywhere.
    finite = tuple(
        encode_component(np.full((2, 2), 5.0, dtype=np.float32)) for _ in range(4)
    )
    widget.publish_fill(finite, "probe-finite-control")
    pump(250)
    pixels = _grab(widget)
    out["finite_control"] = {
        "alpha_pixels": int((pixels[..., 3] > 0).sum()),
        "total_pixels": int(pixels[..., 3].size),
    }

    # Independence.  A fill change must not re-stage the barbs: the barbs'
    # pixels and counters are compared across a fill-only publication.
    widget._fill.setVisible(False)
    widget._barbs.setVisible(True)
    pump(150)
    before = dict(widget.counters)
    barbs_before = _grab(widget)
    widget.publish_fill(finite, "probe-finite-control-again")
    pump(250)
    after = dict(widget.counters)
    barbs_after = _grab(widget)
    out["independence"] = {
        "barbs_pixels_unchanged_by_fill_change": bool(
            np.array_equal(barbs_before, barbs_after)
        ),
        "barbs_publications_by_fill_change": (
            after["barbs_field_publications"]
            - before["barbs_field_publications"]
            + after["barbs_selection_publications"]
            - before["barbs_selection_publications"]
        ),
        "fill_publications_by_fill_change": (
            after["fill_publications"] - before["fill_publications"]
        ),
    }

    # A selection change must not re-stage the fill: a new view publishes a
    # new selection, and the resident field is unchanged.  The fill is put
    # back on the resident field first, so the probe's own publication is not
    # counted as a restaging.
    widget._fill.setVisible(True)
    widget.publish_fill(field.images, field.generation)
    pump(250)
    before = dict(widget.counters)
    widget.set_camera(replace(camera, center_lon=camera.center_lon + 0.01))
    pump(250)
    after = dict(widget.counters)
    out["independence"].update(
        {
            "fill_publications_by_selection_change": (
                after["fill_publications"] - before["fill_publications"]
            ),
            "fill_selection_publications": after["fill_selection_publications"],
            "barbs_selection_publications_by_selection_change": (
                after["barbs_selection_publications"]
                - before["barbs_selection_publications"]
            ),
        }
    )

    # The resident field's own values, through the application's shader.
    widget.publish_fill(field.images, field.generation)
    pump(250)
    pixels = _grab(widget)
    out["resident_painted_pixels"] = int((pixels[..., 3] > 0).sum())
    out["values"] = _speed_comparison(field, blend)
    out["counters"] = dict(widget.counters)
    return out


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
    model = GribViewportModel(
        paths,
        land_mask=WindLandMask(snapshot / "data"),
        resident_time_window=True,
    )
    # The numerical fill is behind its own flag; the gate harness enables it.
    widget = _viewport_widget_class()(model, gpu_weather_fill=True)
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
            assert widget._published == model._field.generation, (
                f"published {widget._published} is not the model field "
                f"{model._field.generation}"
            )
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

    def pump(milliseconds=150):
        loop = QEventLoop()
        QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()

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
        bands_before = model.band_statistics()
        counters_before = dict(model.counters)
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
        bands_after = model.band_statistics()
        phase_report["decoded_band_delta"] = {
            key: bands_after.get(key, 0) - bands_before.get(key, 0)
            for key in ("hits", "misses", "evictions")
        }
        phase_report["counter_delta"] = {
            key: model.counters[key] - counters_before.get(key, 0)
            for key in (
                "covered_reuses",
                "misses",
                "time_window_hits",
                "time_window_fills",
                "time_window_fields",
                "time_window_cancelled",
                "time_window_blocked",
                "time_lookaheads",
                "field_preparations",
            )
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
        # The numerical fill layer: its pixels, its validity mask, its values
        # against the CPU reference, and the independence of the two layers.
        report["fill"] = fill_observations(widget, model, camera, pump)
        assert wait_idle(), "Fill probe left work in flight"
        report["generation_mismatch_rejected"] = (
            mismatch_state == "loading"
            and model.counters["generation_rejected"] > generation_before
            and "stale-generation" not in widget.observations()["published_generations"]
        )
        assert wait_idle(), "Generation probe left work in flight"
        observations = model.observations()
        report["sampling_memo"] = observations.get("sampling_memo", {})
        report["resident_window"] = observations.get("resident_window")
        report["resident_window_fields"] = len(model.window_fields())
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
