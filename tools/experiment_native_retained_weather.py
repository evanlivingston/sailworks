"""E2b disposable native-app control; never edits the supplied app checkout."""

import argparse
from contextlib import contextmanager
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from threading import Lock
import time
from types import FunctionType

ROOT = Path(__file__).resolve().parents[1]


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


retained_tool = load_file(
    "native_retained_support",
    Path(__file__).with_name("experiment_retained_weather.py"),
)
startup = retained_tool.startup


@contextmanager
def unavailable_numerics(sampler):
    old_side = sampler._side
    old_arrays = [(index, index.array) for index in sampler.indices]

    def unavailable(*_args, **_kwargs):
        raise AssertionError(
            "Retained native selection attempted numerical preparation"
        )

    sampler._side = unavailable
    for index, _ in old_arrays:
        index.array = unavailable
    try:
        yield
    finally:
        sampler._side = old_side
        for index, original in old_arrays:
            index.array = original


def worker(config):
    def no_network(*_args, **_kwargs):
        raise RuntimeError("Network disabled in native retained-field control")

    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    from gweatherrouting.qt import gpuweatherjobs as jobs
    from gweatherrouting.rendering import weatherbarbs as barbs
    from gweatherrouting.rendering.weatherstaging import stage_wind

    production_tool = load_file(
        "native_production_telemetry",
        Path(config["app_root"]) / "tools/benchmark_startup_rendering.py",
    )
    events, instances, lock = [], [], Lock()

    def event(kind, **values):
        with lock:
            events.append(dict(kind=kind, at=time.perf_counter(), **values))

    resource_type, stager_type = jobs._WeatherResources, jobs.WeatherStager
    old_prepare, old_release, old_init = (
        resource_type.prepare,
        resource_type.release,
        stager_type.__init__,
    )

    def remember_stager(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        instances.append(self)

    def release(self):
        self._retained_control_field = None
        old_release(self)

    def prepare(self, key, stamp, sources, cancel):
        if self.signature != key[1]:
            self._open(sources, key[1], cancel)
        if cancel.is_set() or self.sampler is None:
            return None
        view = key[0]
        field = getattr(self, "_retained_control_field", None)
        if field is None or not field.covers(view) or not field.staged.contains(stamp):
            wider = replace(view, width=view.width + 512, height=view.height + 512)
            points, _count = retained_tool.support.native_points(self.sampler, wider)
            staged = stage_wind(
                self.sampler,
                stamp,
                points[:, 0][None, :],
                points[:, 1][None, :],
                cancel.is_set,
            )
            if staged is None:
                return None
            field = retained_tool.RetainedWind(points, staged, view, 256)
            field.verified = False
            self._retained_control_field = field
            event("field_build", nodes=len(points))
        # Dependency injection into a COPY of the function globals avoids
        # patching a shared module while another map/worker might use it.
        selector = FunctionType(
            barbs.stage_barbs.__code__,
            dict(barbs.stage_barbs.__globals__, stage_wind=field.sample),
            argdefs=barbs.stage_barbs.__defaults__,
            closure=barbs.stage_barbs.__closure__,
        )
        with unavailable_numerics(self.sampler):
            atlas = selector(self.sampler, stamp, view, self.land, cancel.is_set)
        if atlas is None:
            return None
        if not field.verified:
            reference = old_prepare(self, key, stamp, sources, cancel)
            if reference is None:
                return None
            if retained_tool.atlas_identity(reference) != retained_tool.atlas_identity(
                atlas
            ):
                raise AssertionError(
                    "Retained native control changed initial atlas bytes"
                )
            field.verified = True
            event("verified_field")
        if any(
            jobs.source_key(source)[-1] != item[-1]
            for source, item in zip(sources, key[1])
        ):
            raise ValueError("Forecast changed during retained control")
        event("retained_selection", symbols=atlas.count)
        return atlas

    def metadata(canvas):
        result = production_tool.numerical_weather_status(canvas)
        item = getattr(getattr(canvas._gpu, "scene", None), "weather", None)
        result["opacity"] = item.opacity() if item is not None else 0
        result["active"] = result["active"] and result["opacity"] > 0
        return result

    stager_type.__init__ = remember_stager
    if config["control"] == "retained":
        resource_type.prepare, resource_type.release = prepare, release
    config["_frame_metadata"] = metadata
    try:
        result = startup.worker(config)
    finally:
        # Validation cleanup after all timed work, not production GUI shutdown.
        try:
            for stager in instances:
                stager.close()
                if stager._thread is not None:
                    stager._thread.join(5)
                    if stager._thread.is_alive():
                        raise RuntimeError(
                            "Numerical worker did not close in control cleanup"
                        )
        finally:
            resource_type.prepare, resource_type.release = old_prepare, old_release
            stager_type.__init__ = old_init
    with lock:
        result["retained_control_events"] = list(events)
    return result


def observations(run, interval_ms):
    probe = run["native_render_probe"]
    inputs = probe["inputs"]
    if not inputs:
        raise ValueError("Missing input observations")
    start, end = inputs[0]["dispatched"], inputs[-1]["dispatched"] + interval_ms / 1000
    callbacks = [c for c in probe["callbacks"] if start <= c["after_rendering"] <= end]
    numerical, observed = {}, {}
    for callback in callbacks:
        submission = callback["submission"]
        if submission is None or submission["revision"] is None:
            continue
        revision = submission["revision"]
        if not 0 <= revision < len(inputs):
            raise ValueError("Invalid callback input revision")
        if callback["after_rendering"] < inputs[revision]["dispatched"]:
            continue
        observed.setdefault(revision, callback["after_rendering"])
        if submission["content"].get("numerical_weather", {}).get("active"):
            numerical.setdefault(revision, callback["after_rendering"])
    build_in_input = sum(
        e["kind"] == "field_build" and start <= e["at"] <= end
        for e in run.get("retained_control_events", [])
    )
    return dict(
        inputs=len(inputs),
        any_rendered_revisions=len(observed),
        numerical_rendered_revisions=len(numerical),
        input_phase_callbacks=len(callbacks),
        total_callbacks_including_drain=len(probe["callbacks"]),
        dispatch_lateness=startup.protocol.summary(
            [i["dispatch_late_ms"] for i in inputs]
        ),
        first_any_render_ms=startup.protocol.summary(
            [(stamp - inputs[i]["dispatched"]) * 1000 for i, stamp in observed.items()]
        ),
        first_numerical_render_ms=startup.protocol.summary(
            [(stamp - inputs[i]["dispatched"]) * 1000 for i, stamp in numerical.items()]
        ),
        field_builds_during_input=build_in_input,
        gpu_counter_delta_including_drain=probe["gpu_counter_delta"],
        presentation_measured=False,
    )


def compare(reference, candidate):
    if any(
        report.get("protocol") != "native-retained-field-e2b-v1"
        for report in (reference, candidate)
    ):
        raise ValueError("Incompatible native retained-field protocol")
    for key in ("workload", "environment", "app_identity", "harness_identity"):
        if reference[key] != candidate[key]:
            raise ValueError(f"Incompatible {key}")
    if reference["control"] != "reference" or candidate["control"] != "retained":
        raise ValueError("Expected reference and retained controls")
    a, b = reference["run"], candidate["run"]
    if a["startup"] != b["startup"] or a["actual_viewport"] != b["actual_viewport"]:
        raise ValueError("Startup/viewport changed")
    old = a["native_render_probe"]["inputs"]
    new = b["native_render_probe"]["inputs"]
    expected = reference["workload"]["frames"]
    if any(
        len(inputs) != expected
        or [i["revision"] for i in inputs] != list(range(expected))
        for inputs in (old, new)
    ):
        raise ValueError("Incomplete or duplicated input stream")
    if len(old) != len(new) or any(x["state"] != y["state"] for x, y in zip(old, new)):
        raise ValueError("Per-input camera/time trajectory changed")
    interval = reference["workload"]["interval_ms"]
    baseline, retained = observations(a, interval), observations(b, interval)
    if retained["field_builds_during_input"]:
        raise ValueError("Retained numerical preparation was not warm during input")
    if not any(
        e["kind"] == "verified_field" for e in b.get("retained_control_events", [])
    ):
        raise ValueError("No reference-verified retained field")
    return dict(
        protocol="native-retained-field-e2b-comparison-v1",
        trajectory_match=True,
        reference=baseline,
        retained=retained,
        end_to_end_speedup_proven=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--app-root", type=Path)
    parser.add_argument("--control", choices=("reference", "retained"))
    parser.add_argument("--compare", nargs=2, type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        result = worker(json.loads(args.worker.read_text()))
    elif args.compare:
        result = compare(*(json.loads(p.read_text()) for p in args.compare))
    else:
        if not args.snapshot or not args.app_root or not args.control:
            parser.error("--snapshot, --app-root and --control are required")
        manifest = startup.verify_snapshot(args.snapshot)
        app_root = args.app_root.resolve()
        files = (
            "gweatherrouting/qt/gpuweatherjobs.py",
            "gweatherrouting/qt/gpuscene.py",
            "gweatherrouting/qt/mapcanvas.py",
            "gweatherrouting/rendering/weathercache.py",
            "gweatherrouting/rendering/weatherstaging.py",
            "gweatherrouting/rendering/weatherbarbs.py",
            "tools/benchmark_startup_rendering.py",
        )
        app_identity = {
            name: startup.protocol.file_hash(app_root / name) for name in files
        }
        app_identity["commit"] = subprocess.check_output(
            ["git", "-C", str(app_root), "rev-parse", "HEAD"], text=True
        ).strip()
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
        with tempfile.TemporaryDirectory(prefix="gwr-native-retained-") as temporary:
            root = Path(temporary)
            startup.prepare_sandbox(args.snapshot, root)
            config = dict(
                workload,
                app_root=str(app_root),
                control=args.control,
                map_renderer="gpu",
            )
            configuration, output = root / "config.json", root / "result.json"
            configuration.write_text(json.dumps(config))
            env = dict(
                os.environ,
                HOME=str(root / "home"),
                XDG_DATA_HOME=str(root / "xdg-data"),
                XDG_CONFIG_HOME=str(root / "xdg-config"),
                XDG_CACHE_HOME=str(root / "xdg-cache"),
                PYTHONPATH=os.pathsep.join((str(app_root), str(ROOT))),
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
                    str(configuration),
                    "--output",
                    str(output),
                ],
                cwd=app_root,
                env=env,
                check=True,
                timeout=110,
            )
            run = json.loads(output.read_text())
        environment = run.pop("environment")
        result = dict(
            protocol="native-retained-field-e2b-v1",
            control=args.control,
            workload=workload,
            environment=environment,
            app_identity=app_identity,
            harness_identity={
                name: startup.protocol.file_hash(Path(__file__).with_name(name))
                for name in (
                    "experiment_native_retained_weather.py",
                    "experiment_retained_weather.py",
                    "native_rendering_probe.py",
                    "benchmark_startup_rendering.py",
                )
            },
            run=run,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
