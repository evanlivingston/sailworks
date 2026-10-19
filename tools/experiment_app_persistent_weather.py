"""E2d disposable app hooks: persistent packed field, exact current selection."""

import argparse
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from types import MappingProxyType

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
import experiment_native_retained_weather as native
import experiment_persistent_weather as model

startup = native.startup


@dataclass(frozen=True)
class Prepared:
    retained: object
    field: object
    reference: object
    signature: tuple

    @property
    def staged(self):
        return self.retained.staged


@dataclass(frozen=True)
class Current:
    prepared: Prepared
    selection: object
    count: int

    @property
    def staged(self):
        return self.prepared.staged

    @property
    def pair(self):
        return SimpleNamespace(shape=self.selection.values.shape[:2])

    @property
    def nbytes(self):
        return self.selection.values.shape[0] * self.selection.values.shape[1] * 24


class ProviderGroup:
    """Honor MapScene's teardown contract for both independent providers."""

    def __init__(self, *providers):
        self.providers = providers

    def clear(self):
        for provider in self.providers:
            provider.clear()

    @property
    def requests(self):
        return sum(provider.requests for provider in self.providers)


def current_candidates(retained, view):
    """Geographic clipping only; no live index/provider crosses onto the GUI."""
    if not retained.covers(view):
        raise ValueError("Retained coverage escape")
    cx, cy = view._lon_to_x(view._center_lon), view._lat_to_y(view._center_lat)
    south, north = sorted(
        (
            view._y_to_lat(cy + view.height / 2 + 32),
            view._y_to_lat(cy - view.height / 2 - 32),
        )
    )
    west, east = view._x_to_lon(cx - view.width / 2 - 32), view._x_to_lon(
        cx + view.width / 2 + 32
    )
    if east < west or east - west >= 359:
        raise ValueError("Seam/global coverage unsupported in this control")
    points = retained.points
    keep = (
        (points[:, 0] >= south - 1e-7)
        & (points[:, 0] <= north + 1e-7)
        & (points[:, 1] >= west - 1e-7)
        & (points[:, 1] <= east + 1e-7)
    )
    return points[keep]


class NativeLandMask:
    """Immutable exact native-node land decisions, never a live coast/GDAL object."""

    def __init__(self, points, flags, scale):
        points, flags = np.asarray(points), np.asarray(flags)
        if (
            points.ndim != 2
            or points.shape[1] != 2
            or flags.shape != (len(points),)
            or flags.dtype != np.bool_
        ):
            raise ValueError("Invalid native land-mask payload")
        if not np.isfinite(points).all():
            raise ValueError("Nonfinite land-mask coordinates")
        mapping = {
            (float(lat), float(lon)): bool(flag)
            for (lat, lon), flag in zip(points, flags)
        }
        if len(mapping) != len(points):
            raise ValueError("Duplicate native land-mask coordinates")
        self._mapping = MappingProxyType(mapping)
        self._quality = self.quality(scale)

    @staticmethod
    def quality(scale):
        return (
            "c"
            if scale > 5000
            else (
                "l"
                if scale > 500
                else "i" if scale > 200 else "h" if scale >= 15 else "f"
            )
        )

    def refresh_source(self, scale):
        if self.quality(scale) != self._quality:
            raise ValueError("Land-mask scale outside prepared coverage")

    def contains(self, lat, lon):
        try:
            return self._mapping[float(lat), float(lon)]
        except KeyError as exc:
            raise ValueError(
                "Land-mask coordinate outside prepared native coverage"
            ) from exc


def make_selection(prepared, view, stamp, barbs):
    land = getattr(prepared.retained, "land_mask", None)
    if not view.over_land and land is None:
        raise ValueError(
            "Hide-over-land selection needs an exact prepared native land mask"
        )
    return model.select(
        prepared.field,
        prepared.retained,
        stamp,
        view,
        current_candidates(prepared.retained, view),
        land,
        barbs.barb_density_parameters,
        barbs.mercator_project_flat,
    )


def shader_sources(app_root, directory, safe_missing=False):
    import PySide6
    from experiment_float_textures import find_qsb

    shaders = app_root / "gweatherrouting/qt/shaders"
    vertex = (shaders / "barbs.vert").read_text()
    vertex = vertex.replace(
        "layout(binding = 6) uniform sampler2D anchorY;",
        "layout(binding = 6) uniform sampler2D anchorY;\nlayout(binding = 7) uniform sampler2D nodeId;",
    )
    vertex = vertex.replace("textureSize(u0, 0)", "textureSize(nodeId, 0)")
    vertex = vertex.replace(
        "vec2 a = vec2(decodeField(u0, cell), decodeField(v0, cell));\n    vec2 b = vec2(decodeField(u1, cell), decodeField(v1, cell));",
        """int id = int(decodeField(nodeId, cell));
    ivec2 fieldCell = ivec2(max(id, 0) % 64, max(id, 0) / 64);
    vec2 a = vec2(decodeField(u0, fieldCell), decodeField(v0, fieldCell));
    vec2 b = vec2(decodeField(u1, fieldCell), decodeField(v1, fieldCell));
    if (id < 0) { a = vec2(uintBitsToFloat(0x7fc00000u)); b = a; }""",
    )
    if "decodeField(u0, cell)" in vertex or "int id =" not in vertex:
        raise ValueError("Reference barb vertex shader changed")
    qml = (shaders / "WeatherBarbs.qml").read_text()
    qml = qml.replace(
        "property real blend: 0",
        "property alias nodeIdSource: imageId.source\n    property real blend: 0",
    )
    qml = qml.replace(
        "&& imageX.status ===", "&& imageId.status === Image.Ready && imageX.status ==="
    )
    qml = qml.replace(
        "Image { id: imageX;",
        "Image { id: imageId; visible: false; smooth: false; cache: false }\n    Image { id: imageX;",
    )
    qml = qml.replace(
        "property variant anchorX:",
        "property variant nodeId: imageId\n        property variant anchorX:",
    )
    fragment = (shaders / "barbs.frag").read_text()
    if safe_missing:
        from barb_shader_safety import safe_sources

        vertex, fragment = safe_sources(vertex, fragment)
    (directory / "WeatherBarbs.qml").write_text(qml)
    (directory / "barbs.vert").write_text(vertex)
    (directory / "barbs.frag").write_text(fragment)
    for suffix in ("vert", "frag"):
        subprocess.run(
            [
                str(find_qsb(Path(PySide6.__file__).parent)),
                "--msl",
                "12",
                "-o",
                str(directory / f"barbs.{suffix}.qsb"),
                str(directory / f"barbs.{suffix}"),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )


def install(config, directory):
    from PySide6.QtCore import QUrl
    from PySide6.QtQml import QQmlComponent
    from gweatherrouting.qt import (
        gpuweatherjobs as jobs,
        gpuweather as images,
        gpuscene,
        mapcanvas,
    )
    from gweatherrouting.rendering import weatherbarbs as barbs
    from gweatherrouting.rendering.weatherstaging import stage_wind

    events, stagers = [], []
    originals = []

    def hook(cls, name, value):
        old = getattr(cls, name)
        originals.append((cls, name, old))
        setattr(cls, name, value)
        return old

    def event(kind, **kwargs):
        events.append(dict(kind=kind, at=time.perf_counter(), **kwargs))

    old_init = jobs.WeatherStager.__init__

    def init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        stagers.append(self)

    hook(jobs.WeatherStager, "__init__", init)

    def prepare(self, key, stamp, sources, cancel):
        if self.signature != key[1]:
            self._open(sources, key[1], cancel)
        if cancel.is_set() or self.sampler is None:
            return None
        view = key[0]
        wider = replace(view, width=view.width + 512, height=view.height + 512)
        points, _ = native.retained_tool.support.native_points(self.sampler, wider)
        staged = stage_wind(
            self.sampler,
            stamp,
            points[:, 0][None, :],
            points[:, 1][None, :],
            cancel.is_set,
        )
        if staged is None or cancel.is_set():
            return None
        retained = native.retained_tool.RetainedWind(points, staged, view, 256)
        generation = startup.digest(
            dict(
                values=startup.array_content_hash(
                    (points, staged.first, staged.second)
                ),
                signature=repr(key[1]),
                before=staged.before,
                after=staged.after,
            )
        )
        field = model.Field.create(generation, staged.first, staged.second)
        reference = barbs.stage_barbs(
            self.sampler, stamp, view, self.land, cancel.is_set
        )
        if reference is None:
            return None
        # Verify membership for a fixed interior pan/reversal set, not only the
        # initial view. No runtime oracle/GDAL is called by GUI selection.
        for dx in (0, 8, 16, 32, 64, 128, -16, -64):
            check_view = replace(
                view, _center_lon=view._x_to_lon(view._lon_to_x(view._center_lon) + dx)
            )
            expected, _ = native.retained_tool.support.native_points(
                self.sampler, check_view
            )
            if not np.array_equal(current_candidates(retained, check_view), expected):
                raise ValueError("Retained geographic membership changed")
        if any(jobs.source_key(s)[-1] != k[-1] for s, k in zip(sources, key[1])):
            raise ValueError("Forecast changed during field preparation")
        event("field_build", nodes=len(points))
        return Prepared(retained, field, reference, key[1])

    original_request = jobs.WeatherStager.request

    def request(self, canvas):
        if getattr(self, "_control_cpu_only", False):
            return None
        candidate = self.frame[1] if self.frame is not None else None
        if not isinstance(candidate, Prepared):
            result = original_request(self, canvas)
            candidate = result[0] if result is not None else None
        if not isinstance(candidate, Prepared):
            return None
        settings = canvas.controller.settings
        signature = tuple(
            jobs.source_key(s) for s in canvas.controller.core.grib_manager.gribs
        )
        view = barbs.BarbView(
            canvas.width(),
            canvas.height(),
            canvas._center_lat,
            canvas._center_lon,
            canvas._zoom,
            getattr(settings, "gribBarbDensity", "standard"),
            bool(settings.gribArrowOnGround),
        )
        stamp = canvas.time_control.time.timestamp()
        if (
            self.closed
            or not canvas.wind_visible
            or settings.gribWindStyle != "barbs"
            or signature != candidate.signature
            or not candidate.retained.covers(view)
            or not candidate.staged.contains(stamp)
            or not view.over_land
            or view.density == "all"
        ):
            self.invalidate()
            event("fast_path_rejected")
            return None
        selection_key = (candidate.field.generation, view, stamp)
        cached = getattr(self, "_control_current", None)
        if cached is not None and cached[0] == selection_key:
            return cached[1]
        began = time.perf_counter()
        selection = make_selection(candidate, view, stamp, barbs)
        event("gui_selection", duration_ms=(time.perf_counter() - began) * 1000)
        atlas = Current(
            candidate, selection, int(np.count_nonzero(selection.values[..., 0] >= 0))
        )
        result = atlas, candidate.staged.weight(stamp)
        self._control_current = selection_key, result
        return result

    def present_weather(self, numerical, frame):
        if numerical is None:
            if self.weather is not None:
                self.weather.setVisible(False)
            return
        atlas, blend = numerical
        if self.weather is None:
            self.field_provider, self.selection_provider = (
                images.WeatherImageProvider(),
                images.WeatherImageProvider(),
            )
            self.widget.engine().addImageProvider(
                "persistentfield", self.field_provider
            )
            self.widget.engine().addImageProvider(
                "persistentselection", self.selection_provider
            )
            self.weather_provider = ProviderGroup(
                self.field_provider, self.selection_provider
            )
            self.weather_component = QQmlComponent(
                self.widget.engine(),
                QUrl.fromLocalFile(str(directory / "WeatherBarbs.qml")),
            )
            self.weather = self.weather_component.create()
            if self.weather is None:
                raise RuntimeError(str(self.weather_component.errors()))
            self.weather.setParent(self.root)
            self.weather.setParentItem(self.root)
            self.weather.setZ(100)
            self.weather.readyChanged.connect(self.request)
            self.weather.shaderErrorChanged.connect(
                lambda: (
                    self.fail(self.weather.property("shaderError"))
                    if self.weather.property("shaderError")
                    else None
                )
            )
            self._field_generation = None
        if self._field_generation != atlas.prepared.field.generation:
            values = atlas.prepared.field.values
            pair = SimpleNamespace(
                images=tuple(images.encode_component(values[..., i]) for i in range(4))
            )
            pair.nbytes = sum(i.sizeInBytes() for i in pair.images)
            urls = self.field_provider.publish(pair)
            for name, url in zip(
                ("u0Source", "v0Source", "u1Source", "v1Source"), urls
            ):
                self.weather.setProperty(
                    name,
                    QUrl(url.replace("image://weather/", "image://persistentfield/")),
                )
            self._field_generation = atlas.prepared.field.generation
            self.counters["texture_uploads"] += 4
            self.counters["texture_upload_bytes"] += pair.nbytes
            event("field_images", bytes=pair.nbytes)
        if atlas is not self.weather_atlas:
            selection = atlas.selection.values
            pair = SimpleNamespace(
                images=tuple(
                    images.encode_component(selection[..., i]) for i in (0, 1, 2)
                )
            )
            pair.nbytes = sum(i.sizeInBytes() for i in pair.images)
            urls = self.selection_provider.publish(pair)
            for name, url in zip(
                ("nodeIdSource", "anchorXSource", "anchorYSource"), urls
            ):
                self.weather.setProperty(
                    name,
                    QUrl(
                        url.replace("image://weather/", "image://persistentselection/")
                    ),
                )
            self.weather.setProperty("rows", selection.shape[0])
            self.weather.setProperty("columns", selection.shape[1])
            self.weather_atlas = atlas
            self.counters["texture_uploads"] += 3
            self.counters["texture_upload_bytes"] += pair.nbytes
            event("selection_images", bytes=pair.nbytes)
        self.weather.setWidth(frame.width)
        self.weather.setHeight(frame.height)
        self.weather.setOpacity(frame.opacity)
        self.weather.setProperty("blend", blend)
        self.weather.setVisible(True)

    original_frame = mapcanvas.TrackMap._gpu_frame

    def frame(self):
        value = original_frame(self)
        if value.numerical_wind is not None:
            scene = self._gpu.scene
            scene.present_weather(value.numerical_wind, value)
            if scene.weather.property("ready"):
                value = replace(value, wind=(), raster_wind=None)
            else:
                self._gpu.weather._control_cpu_only = True
                try:
                    value = original_frame(self)
                finally:
                    self._gpu.weather._control_cpu_only = False
        if value.numerical_wind is not None and (
            value.wind or value.raster_wind is not None
        ):
            raise AssertionError("Mixed CPU/numerical symbol frame")
        return value

    if config["control"] == "retained":
        hook(jobs._WeatherResources, "prepare", prepare)
        hook(jobs.WeatherStager, "request", request)
        hook(gpuscene.MapScene, "present_weather", present_weather)
        hook(mapcanvas.TrackMap, "_gpu_frame", frame)

    def metadata(canvas):
        scene = canvas._gpu.scene
        value = scene.frame
        atlas = value.numerical_wind[0] if value and value.numerical_wind else None
        item = scene.weather
        ready = (
            item is not None
            and bool(item.property("ready"))
            and item.isVisible()
            and item.opacity() > 0
        )
        cpu = value is not None and bool(value.wind or value.raster_wind is not None)
        return dict(
            active=bool(atlas and ready and not cpu),
            symbols=atlas.count if atlas else 0,
            mixed=bool(atlas and cpu),
            shader_ready=bool(ready),
            field_image_requests=getattr(
                getattr(scene, "field_provider", None), "requests", 0
            ),
            selection_image_requests=getattr(
                getattr(scene, "selection_provider", None), "requests", 0
            ),
        )

    config["_frame_metadata"] = metadata

    def static_fidelity(canvas, app):
        if config["control"] != "retained":
            return None
        scene = canvas._gpu.scene
        if scene.frame.numerical_wind is None:
            raise ValueError("No numerical frame for static glyph fidelity")
        atlas, blend = scene.frame.numerical_wind
        reference = atlas.prepared.reference
        restored = [
            (item, item.isVisible())
            for item in scene.root.childItems()
            if item is not scene.weather
        ]
        for item, _visible in restored:
            item.setVisible(False)
        provider = images.WeatherImageProvider()
        scene.widget.engine().addImageProvider("glyphoracle", provider)
        component = QQmlComponent(
            scene.widget.engine(),
            QUrl.fromLocalFile(
                str(
                    Path(config["app_root"])
                    / "gweatherrouting/qt/shaders/WeatherBarbs.qml"
                )
            ),
        )
        oracle = component.create()
        if oracle is None:
            raise RuntimeError(str(component.errors()))
        oracle.setParent(scene.root)
        oracle.setParentItem(scene.root)
        oracle.setWidth(scene.frame.width)
        oracle.setHeight(scene.frame.height)
        oracle.setOpacity(scene.frame.opacity)
        oracle.setVisible(False)
        for name, url in zip(
            (
                "u0Source",
                "v0Source",
                "u1Source",
                "v1Source",
                "anchorXSource",
                "anchorYSource",
            ),
            provider.publish(reference),
        ):
            oracle.setProperty(
                name, QUrl(url.replace("image://weather/", "image://glyphoracle/"))
            )
        oracle.setProperty("rows", reference.pair.shape[0])
        oracle.setProperty("columns", reference.pair.shape[1])
        oracle.setProperty("blend", blend)
        deadline = time.perf_counter() + 15
        try:
            while not oracle.property("ready"):
                oracle.setVisible(True)
                scene.weather.setVisible(False)
                scene.widget.update()
                app.processEvents()
                if time.perf_counter() > deadline:
                    raise RuntimeError("Reference glyph shader did not become ready")
                time.sleep(0.001)
            oracle.setVisible(False)
            scene.weather.setVisible(True)
            scene.widget.update()
            app.processEvents()
            actual = scene.widget.grabFramebuffer()
            oracle.setVisible(True)
            scene.weather.setVisible(False)
            scene.widget.update()
            app.processEvents()
            expected = scene.widget.grabFramebuffer()
            if actual.size() != expected.size() or bytes(actual.constBits()) != bytes(
                expected.constBits()
            ):
                raise ValueError("Initial glyph pixels differ from reference")
            event("static_glyph_match")
            return dict(
                exact_glyph_pixels=True,
                width=actual.width(),
                height=actual.height(),
                readback_outside_timed_replay=True,
            )
        finally:
            oracle.setVisible(False)
            oracle.deleteLater()
            scene.weather.setVisible(True)
            for item, visible in restored:
                item.setVisible(visible)
            scene.widget.update()
            app.processEvents()

    def diagnosed_fidelity(canvas, app):
        try:
            return static_fidelity(canvas, app)
        except Exception:
            # Preserve the initiating error even if host shutdown also raises.
            import traceback

            traceback.print_exc()
            raise

    config["_before_native_probe"] = diagnosed_fidelity

    def cleanup():
        try:
            for stager in stagers:
                stager.close()
                if stager._thread is not None:
                    stager._thread.join(5)
                    if stager._thread.is_alive():
                        raise RuntimeError("Control worker did not shut down")
        finally:
            for cls, name, old in reversed(originals):
                setattr(cls, name, old)

    return events, cleanup


def worker(config):
    import socket

    def no_network(*_args, **_kwargs):
        raise RuntimeError("Network disabled in app persistent-field control")

    socket.socket.connect = socket.socket.connect_ex = no_network
    with tempfile.TemporaryDirectory(prefix="gwr-app-persistent-shaders-") as temporary:
        directory = Path(temporary)
        shader_sources(Path(config["app_root"]), directory)
        events, cleanup = install(config, directory)
        try:
            result = startup.worker(config)
            result["retained_control_events"] = events
        finally:
            cleanup()
    return result


def compare(reference, candidate):
    for key in ("protocol", "workload", "harness_identity", "app_commit"):
        if reference[key] != candidate[key]:
            raise ValueError(f"Incompatible {key}")
    if reference["control"] != "reference" or candidate["control"] != "retained":
        raise ValueError("Expected reference and retained controls")
    a, b = reference["run"], candidate["run"]
    for key in ("startup", "actual_viewport", "environment"):
        if a[key] != b[key]:
            raise ValueError(f"Incompatible run {key}")
    old, new = a["native_render_probe"]["inputs"], b["native_render_probe"]["inputs"]
    expected = reference["workload"]["frames"]
    if any(
        len(stream) != expected
        or [i["revision"] for i in stream] != list(range(expected))
        for stream in (old, new)
    ):
        raise ValueError("Incomplete input stream")
    if any(x["state"] != y["state"] for x, y in zip(old, new)):
        raise ValueError("Input trajectory changed")
    if not b.get("native_fidelity", {}).get("exact_glyph_pixels"):
        raise ValueError("Missing static glyph fidelity")
    interval = reference["workload"]["interval_ms"]
    baseline, retained = native.observations(a, interval), native.observations(
        b, interval
    )
    if retained["field_builds_during_input"]:
        raise ValueError("Field rebuilt during input")
    probe = b["native_render_probe"]
    start, end = new[0]["dispatched"], new[-1]["dispatched"] + interval / 1000
    submissions = [s for s in probe["submissions"] if start <= s["submitted"] <= end]
    states = [s["content"]["numerical_weather"] for s in submissions]
    if any(s["mixed"] for s in states):
        raise ValueError("Mixed symbol mode in candidate")
    events = [e for e in b["retained_control_events"] if start <= e["at"] <= end]
    if any(e["kind"] in ("field_images", "fast_path_rejected") for e in events):
        raise ValueError("Field republished or fast path rejected during input")
    return dict(
        protocol="app-persistent-field-e2d-comparison-v1",
        trajectory_match=True,
        reference=baseline,
        retained=retained,
        static_glyph_pixels_exact=True,
        mixed_candidate_submissions=0,
        input_phase_candidate_submissions=len(states),
        input_phase_numerical_submissions=sum(s["active"] for s in states),
        field_image_requests=sorted(set(s["field_image_requests"] for s in states)),
        gui_selection_ms=startup.protocol.summary(
            [e["duration_ms"] for e in events if e["kind"] == "gui_selection"]
        ),
        end_to_end_speedup_proven=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--app-root", type=Path)
    parser.add_argument("--control", choices=("reference", "retained"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--compare", nargs=2, type=Path)
    args = parser.parse_args()
    if args.worker:
        result = worker(json.loads(args.worker.read_text()))
    elif args.compare:
        result = compare(*(json.loads(path.read_text()) for path in args.compare))
    else:
        if not args.snapshot or not args.app_root or not args.control:
            parser.error("--snapshot, --app-root and --control required")
        manifest = startup.verify_snapshot(args.snapshot)
        workload = dict(
            snapshot_sha256=manifest["content_sha256"],
            forecast_time=manifest["forecast_time"],
            scenario="reverse_pan",
            frames=24,
            repeats=1,
            warmup_frames=2,
            interval_ms=16.667,
            trackpad_step_px=8,
            scale=1,
            native_drain_timeout_s=2,
            measurement_mode="native-render",
            settle_timeout_s=60,
        )
        with tempfile.TemporaryDirectory(prefix="gwr-app-persistent-") as temporary:
            root = Path(temporary)
            startup.prepare_sandbox(args.snapshot, root)
            config = dict(
                workload,
                app_root=str(args.app_root.resolve()),
                control=args.control,
                map_renderer="gpu",
            )
            path, output = root / "config.json", root / "result.json"
            path.write_text(json.dumps(config))
            env = dict(
                os.environ,
                HOME=str(root / "home"),
                XDG_DATA_HOME=str(root / "xdg-data"),
                XDG_CONFIG_HOME=str(root / "xdg-config"),
                XDG_CACHE_HOME=str(root / "xdg-cache"),
                PYTHONPATH=os.pathsep.join((str(args.app_root.resolve()), str(ROOT))),
                GWR_GPU_WEATHER="1",
                PYTHONHASHSEED="0",
                QT_SCALE_FACTOR="1",
            )
            env.pop("QT_QPA_PLATFORM", None)
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    str(path),
                    "--output",
                    str(output),
                ],
                cwd=args.app_root,
                env=env,
                check=True,
                timeout=110,
            )
            run = json.loads(output.read_text())
        result = dict(
            protocol="app-persistent-field-e2d-v1",
            control=args.control,
            workload=workload,
            run=run,
            harness_identity={
                name: startup.protocol.file_hash(Path(__file__).with_name(name))
                for name in (
                    "experiment_app_persistent_weather.py",
                    "experiment_persistent_weather.py",
                    "benchmark_startup_rendering.py",
                    "native_rendering_probe.py",
                )
            },
            app_commit=subprocess.check_output(
                ["git", "-C", str(args.app_root), "rev-parse", "HEAD"], text=True
            ).strip(),
        )
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
