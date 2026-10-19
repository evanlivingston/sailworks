"""Headless retained numerical producer: no chart/window/Core startup required."""

import argparse
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import sys
from threading import Event, Thread
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
import architecture_run_protocol as protocol
from persistent_coverage import PersistentPreparation, atomic_json
from resident_weather_worker import ResidentWeatherWorker


def selected_names(data):
    names = json.loads((Path(data) / "grib-manager.json").read_text())["opened"]
    if not names or any(
        not isinstance(name, str) or Path(name).name != name or name in (".", "..")
        for name in names
    ):
        raise ValueError("Expected managed selected forecast filenames in saved order")
    if len(names) != len(set(names)):
        raise ValueError(
            "Duplicate selected forecast entries require explicit handling"
        )
    return tuple(names)


def prepare_field(resources, view, stamp, snapshot):
    import experiment_vertical_renderer as renderer
    from gweatherrouting.rendering.weatherstaging import stage_wind
    from gweatherrouting.rendering import weatherbarbs as barbs

    points, _ = renderer.previous.native.retained_tool.support.native_points(
        resources.sampler,
        replace(view, width=view.width + 512, height=view.height + 512),
    )
    staged = stage_wind(
        resources.sampler, stamp, points[:, 0][None, :], points[:, 1][None, :]
    )
    if staged is None:
        raise ValueError("Headless numerical staging rejected")
    generation = renderer.startup.digest(
        dict(
            snapshot=snapshot,
            step=0,
            points=renderer.startup.array_content_hash(
                (points, staged.first, staged.second)
            ),
            sources=staged.sources,
            before=staged.before,
            after=staged.after,
        )
    )
    field = renderer.model.Field.create(generation, staged.first, staged.second)
    retained = renderer.previous.native.retained_tool.RetainedWind(
        points, staged, view, 256
    )
    land_flags = None
    if not view.over_land:
        resources.land.refresh_source(view.get_scale())
        land_flags = np.asarray(
            [resources.land.contains(*point) for point in points], dtype=np.bool_
        )
        retained.land_mask = renderer.previous.NativeLandMask(
            points, land_flags, view.get_scale()
        )
    prepared = renderer.previous.Prepared(retained, field, None, ())
    selection = renderer.previous.make_selection(prepared, view, stamp, barbs)
    payload = dict(
        points=points,
        first=staged.first,
        second=staged.second,
        field=field.values,
        selection=selection.values,
    )
    if land_flags is not None:
        payload["land_flags"] = land_flags
    for value in payload.values():
        value.setflags(write=False)
    return (
        dict(
            view=view.__dict__,
            clock=datetime.fromtimestamp(stamp).isoformat(),
            before=staged.before,
            after=staged.after,
            generation=generation,
            node_count=field.node_count,
            candidate_symbols=int(np.count_nonzero(selection.values[..., 0] >= 0)),
        ),
        payload,
    )


def worker(config):
    def blocked(*args, **kwargs):
        raise RuntimeError("Network disabled in independent numerical service")

    socket.socket.connect = socket.socket.connect_ex = blocked
    from PySide6.QtCore import QCoreApplication
    from gweatherrouting.core.grib import Grib
    from gweatherrouting.core.routingjob import freeze_wind_sources
    from gweatherrouting.qt import gpuweatherjobs as jobs
    from gweatherrouting.rendering.weatherbarbs import BarbView

    root = Path(config["directory"])
    names = selected_names(config["data"])
    if QCoreApplication.instance() is not None:
        raise ValueError("Numerical service unexpectedly has a Qt application")

    def factory():
        parsed = []
        resources = None
        try:
            for name in names:
                path = Path(config["data"]) / "grib" / name
                if not path.is_file():
                    raise ValueError(f"Selected forecast unavailable: {name}")
                parsed.append(Grib.parse(str(path)))
            frozen = freeze_wind_sources(parsed)
            signature = tuple(jobs.source_key(source) for source in frozen)
            resources = jobs._WeatherResources(128 * 1024 * 1024)
            resources._open(frozen, signature, Event())
            if len(resources.providers) != len(names):
                raise ValueError("Headless service lost a selected source")
            return resources
        except Exception:
            if resources is not None:
                resources.close()
            raise
        finally:
            for source in parsed:
                source.close()

    owner = ResidentWeatherWorker(factory)
    completed = []
    try:
        atomic_json(
            root / "ready.json",
            dict(
                pid=os.getpid(),
                sources=len(names),
                source_order_sha256=protocol.file_hash(
                    Path(config["data"]) / "grib-manager.json"
                ),
            ),
        )
        last = 0
        while not (root / "stop.json").exists():
            path = root / "request.json"
            request = json.loads(path.read_text()) if path.exists() else None
            if request and request["generation"] > last:
                last = request["generation"]
                began = time.perf_counter()
                response = dict(generation=last, error=None)
                try:
                    view = BarbView(**request["view"])
                    metadata, payload = owner.call(
                        lambda resources: prepare_field(
                            resources, view, request["stamp"], config["snapshot_sha256"]
                        )
                    )
                    directory = root / f"generation-{last}"
                    directory.mkdir(exist_ok=False)
                    np.savez(directory / "field.npz", **payload)
                    metadata["field_sha256"] = protocol.file_hash(
                        directory / "field.npz"
                    )
                    atomic_json(directory / "field.json", metadata)
                    response.update(directory=str(directory), metadata=metadata)
                except Exception as exc:
                    response["error"] = str(exc)
                response["prepare_seconds"] = time.perf_counter() - began
                completed.append(response)
                atomic_json(root / f"response-{last}.json", response)
            time.sleep(0.005)
    finally:
        owner.close()
    if (
        QCoreApplication.instance() is not None
        or "gweatherrouting.qt.window" in sys.modules
    ):
        raise ValueError(
            "Headless service acquired a GUI application/window dependency"
        )
    return dict(
        protocol="independent-numerical-service-a0i-v1",
        sources=len(names),
        no_qt_application=True,
        no_legacy_window=True,
        owner=owner.owner,
        worker_events=owner.events,
        completed=completed,
    )


def check_reference(directory, reference):
    import experiment_vertical_renderer as renderer
    from gweatherrouting.rendering.weatherbarbs import BarbView
    from gweatherrouting.rendering import weatherbarbs as barbs

    metadata = json.loads((directory / "field.json").read_text())
    if protocol.file_hash(directory / "field.npz") != metadata["field_sha256"]:
        raise ValueError("Independent field payload changed")
    rm, arrays, _ = renderer.read_bundle(reference)
    fm = rm["field_meta"][0]
    if metadata["view"] != rm["view"] or metadata["clock"] != rm["clocks"][0]:
        raise ValueError("Headless/reference view or clock differs")
    for key in ("before", "after", "generation", "node_count"):
        if metadata[key] != fm[key]:
            raise ValueError(f"Headless/reference provenance differs: {key}")
    with np.load(directory / "field.npz", allow_pickle=False) as payload:
        for name in ("points", "first", "second", "field"):
            expected = arrays[fm["file"]][name]
            if payload[name].dtype != expected.dtype or not np.array_equal(
                payload[name], expected, equal_nan=True
            ):
                raise ValueError(f"Headless/reference array differs: {name}")
        if "land_flags" in arrays[fm["file"]]:
            if (
                "land_flags" not in payload
                or payload["land_flags"].dtype != np.bool_
                or not np.array_equal(
                    payload["land_flags"], arrays[fm["file"]]["land_flags"]
                )
            ):
                raise ValueError("Headless/reference native land decisions differ")
        prepared = renderer.prepared_fields(rm, arrays)[0][2]
        initial = BarbView(**metadata["view"])
        selection = renderer.previous.make_selection(
            prepared,
            initial,
            datetime.fromisoformat(metadata["clock"]).timestamp(),
            barbs,
        )
        if not np.array_equal(payload["selection"], selection.values, equal_nan=True):
            raise ValueError("Headless/reference selected nodes or anchors differ")
    return dict(
        exact_arrays=True,
        exact_selected_nodes_anchors=True,
        candidate_symbols=metadata["candidate_symbols"],
        field_generation=metadata["generation"],
    )


def run(args):
    import experiment_vertical_renderer as renderer

    args.directory.mkdir(parents=True, exist_ok=False)
    began = time.perf_counter()
    try:
        manifest = renderer.startup.verify_snapshot(args.snapshot)
    except (ValueError, OSError) as exc:
        atomic_json(
            args.directory / "preflight-error.json",
            dict(error=str(exc), workers_launched=False, snapshot_accepted=False),
        )
        raise
    sandbox = args.directory / "sandbox"
    data = renderer.startup.prepare_sandbox(args.snapshot, sandbox)
    preflight_seconds = time.perf_counter() - began
    directory = args.directory / "numerical"
    directory.mkdir()
    config = dict(
        data=str(data),
        directory=str(directory),
        snapshot_sha256=manifest["content_sha256"],
    )
    atomic_json(args.directory / "config.json", config)
    env = protocol.environment()
    env.update(
        HOME=str(sandbox / "home"),
        XDG_DATA_HOME=str(sandbox / "xdg-data"),
        XDG_CONFIG_HOME=str(sandbox / "xdg-config"),
        XDG_CACHE_HOME=str(sandbox / "xdg-cache"),
        PYTHONPATH=os.pathsep.join((str(args.app_root.resolve()), str(ROOT))),
    )
    status = []
    launch = time.perf_counter()
    chart = PersistentPreparation(
        args.directory / "chart", args.app_root.resolve(), args.snapshot.resolve()
    )
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        str(args.directory / "config.json"),
        "--output",
        str(directory / "service-result.json"),
    ]
    thread = Thread(
        target=lambda: status.append(
            protocol.supervise(command, ROOT, env, directory / "process", timeout=90)
        )
    )
    thread.start()
    ready_seconds = first_field_seconds = None
    chart_ready_at_first_field = None
    rows = []

    def wait(path):
        deadline = launch + 80
        while not path.exists():
            if not thread.is_alive() or time.perf_counter() > deadline:
                raise RuntimeError(
                    "Independent service stopped/timed out; see process logs"
                )
            time.sleep(0.005)
        return json.loads(path.read_text())

    try:
        ready = wait(directory / "ready.json")
        ready_seconds = time.perf_counter() - launch
        for generation, reference in enumerate(args.reference, 1):
            rm = json.loads((reference / "resources.json").read_text())
            if rm["snapshot_sha256"] != manifest["content_sha256"]:
                raise ValueError("Reference fixture differs")
            atomic_json(
                directory / "request.json",
                dict(
                    generation=generation,
                    view=rm["view"],
                    stamp=datetime.fromisoformat(rm["clocks"][0]).timestamp(),
                ),
            )
            response = wait(directory / f"response-{generation}.json")
            if response["error"]:
                raise ValueError(response["error"])
            if first_field_seconds is None:
                first_field_seconds = time.perf_counter() - launch
                chart_ready_at_first_field = (chart.directory / "ready.json").exists()
            response["reference_check"] = check_reference(
                Path(response["directory"]), reference
            )
            rows.append(response)
        while not chart.observe_ready():
            if time.perf_counter() - launch > 80:
                raise TimeoutError("Concurrent chart startup timed out")
            time.sleep(0.005)
    finally:
        atomic_json(directory / "stop.json", dict(stop=True))
        thread.join(10)
        chart.close()
    if thread.is_alive():
        raise RuntimeError("Independent numerical service did not stop")
    protocol.require_clean(status[0])
    protocol.require_clean(chart.status)
    service = json.loads((directory / "service-result.json").read_text())
    if (
        chart_ready_at_first_field
        or len({e["thread"] for e in service["worker_events"]}) != 1
    ):
        raise ValueError(
            "Numerical/chart readiness independence or ownership not demonstrated"
        )
    result = dict(
        protocol="independent-numerical-startup-a0i-v1",
        snapshot_sha256=manifest["content_sha256"],
        preflight_seconds=preflight_seconds,
        numerical_ready_seconds=ready_seconds,
        first_field_seconds=first_field_seconds,
        chart_ready_seconds=chart.ready_seconds,
        chart_ready_at_first_field=chart_ready_at_first_field,
        numerical_status=status[0],
        chart_status=chart.status,
        numerical_service=service,
        requests=rows,
        presentation_measured=False,
        early_numerical_display_integrated=False,
        full_map_startup_solved=False,
    )
    atomic_json(args.output, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--app-root", type=Path)
    parser.add_argument("--reference", type=Path, nargs="+")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.worker:
        atomic_json(args.output, worker(json.loads(args.worker.read_text())))
    else:
        if not all((args.snapshot, args.app_root, args.reference, args.directory)):
            parser.error("snapshot, app-root, reference and directory required")
        sys.path.insert(0, str(args.app_root.resolve()))
        run(args)


if __name__ == "__main__":
    main()
