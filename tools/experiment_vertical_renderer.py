"""A0 independent vertical viewport: covered pan and timeline scrub.

The old app is preparation/oracle only: it exports pre-computed chart/weather
resources and numerical fields at one or more forecast times. The candidate
instantiates no TrackMap, MapFrame assembler, or legacy MapScene; it composes
retained image nodes and switches between pre-exported numerical fields as the
forecast time changes. GDAL open entrypoints raise during the candidate run.
"""

import argparse
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event, Thread, get_ident
import time
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
import experiment_app_persistent_weather as previous
import experiment_persistent_weather as model
from native_rendering_probe import RenderRecorder
from architecture_run_protocol import prime_quick
import architecture_run_protocol as run_protocol

startup = previous.startup


def state(view, clock):
    return dict(
        latitude=view._center_lat,
        longitude=view._center_lon,
        zoom=view._zoom,
        forecast_time=clock,
    )


def targets_for_view(initial, view, targets):
    if (initial.width, initial.height, initial._zoom) != (
        view.width,
        view.height,
        view._zoom,
    ):
        raise ValueError("Prepared image viewport/scale escape")
    dx = initial._lon_to_x(initial._center_lon) - view._lon_to_x(view._center_lon)
    dy = initial._lat_to_y(initial._center_lat) - view._lat_to_y(view._center_lat)
    result = []
    for x, y, width, height in targets:
        moved = (x + dx, y + dy, width, height)
        if (
            moved[0] > 1e-7
            or moved[1] > 1e-7
            or moved[0] + width < view.width - 1e-7
            or moved[1] + height < view.height - 1e-7
        ):
            raise ValueError("Prepared image coverage escape")
        result.append(moved)
    return result


def pan(view, dx, dy):
    return replace(
        view,
        _center_lon=view._x_to_lon(view._lon_to_x(view._center_lon) - dx),
        _center_lat=max(
            -85, min(85, view._y_to_lat(view._lat_to_y(view._center_lat) - dy))
        ),
    )


def scrub_trajectory(n_times):
    """Forecast-time index trajectory matching the scrub_return recording pattern.

    The reference replay reads canvas state *before* applying each time change, so
    the first stamp appears twice (initial state, then after the no-op change at
    index 0) and the turnaround stamp appears twice. Total length is 2 * n_times.
    """
    if n_times < 2:
        raise ValueError("Timeline scrub needs at least two time stamps")
    forward = [0] + list(range(n_times))  # [0, 0, 1, 2, ..., n-1]  (n+1 values)
    backward = list(range(n_times - 1, 0, -1))  # [n-1, n-2, ..., 1]  (n-1 values)
    return forward + backward  # 2n values total


def coverage_miss_trajectory(n_steps=24, step_px=48):
    """Relative logical-pixel pan steps, outward then exactly back to origin."""
    if n_steps < 2 or n_steps % 2 or step_px <= 0:
        raise ValueError(
            "Coverage-miss trajectory needs even steps and positive stride"
        )
    return [(-step_px, 0)] * (n_steps // 2) + [(step_px, 0)] * (n_steps // 2)


def coverage_for_view(initial, view, targets, retained):
    """Image enclosure and numerical coverage are independent requirements."""
    try:
        moved = targets_for_view(initial, view, targets)
    except ValueError as exc:
        return None, str(exc)
    if not retained.covers(view):
        return None, "Prepared numerical field coverage escape"
    return moved, None


def partial_image_coverage(initial, view, targets):
    """Return translated images and their common covered screen rectangle."""
    if (initial.width, initial.height, initial._zoom) != (
        view.width,
        view.height,
        view._zoom,
    ):
        raise ValueError("Resident preview viewport/scale escape")
    dx = initial._lon_to_x(initial._center_lon) - view._lon_to_x(view._center_lon)
    dy = initial._lat_to_y(initial._center_lat) - view._lat_to_y(view._center_lat)
    moved = [(x + dx, y + dy, w, h) for x, y, w, h in targets]
    left = max([0] + [x for x, y, w, h in moved])
    top = max([0] + [y for x, y, w, h in moved])
    right = min([view.width] + [x + w for x, y, w, h in moved])
    bottom = min([view.height] + [y + h for x, y, w, h in moved])
    return moved, (left, top, max(0, right - left), max(0, bottom - top))


def selection_availability(count, reference_count, first, second, allow_empty=False):
    if count != reference_count:
        raise ValueError(
            f"Selection count differs: candidate={count}, reference={reference_count}"
        )
    if count:
        return "ready"
    if not allow_empty:
        raise ValueError(
            "Reference selection is empty; explicit empty-view support required"
        )
    if any(
        np.isfinite(values[..., :2]).all(axis=-1).any() for values in (first, second)
    ):
        raise ValueError("Empty selection contains drawable endpoint vectors")
    return "reference-empty"


def empty_layer_can_suppress(independent_count, frame_count, finite_endpoint_counts):
    return independent_count == frame_count == 0 and list(finite_endpoint_counts) == [
        0,
        0,
    ]


def export_resources(
    canvas,
    app,
    directory,
    snapshot,
    stamps=None,
    allow_empty_reference=False,
    numerical_worker=None,
    validate_numeric=True,
    capture_oracle=True,
):
    from gweatherrouting.qt import gpuweatherjobs as jobs, gpuweather as transport
    from gweatherrouting.core.routingjob import freeze_wind_sources
    from gweatherrouting.rendering import weatherbarbs as barbs
    from gweatherrouting.rendering.weatherstaging import stage_wind

    initial = barbs.BarbView(
        canvas.width(),
        canvas.height(),
        canvas._center_lat,
        canvas._center_lon,
        canvas._zoom,
        canvas.controller.settings.gribBarbDensity,
        bool(canvas.controller.settings.gribArrowOnGround),
    )
    if initial.density == "all":
        raise ValueError("A0 requires a saved thinned scene")

    if stamps is None:
        stamps = [canvas.time_control.time.timestamp()]

    sources = tuple(canvas.controller.core.grib_manager.gribs)
    signature = tuple(jobs.source_key(s) for s in sources)
    frozen_sources = freeze_wind_sources(sources)
    result, errors = [], []
    preparation_timings = {}

    def prepare(resources=None):
        owns_resources = resources is None
        if owns_resources:
            resources = jobs._WeatherResources(128 * 1024 * 1024)
        try:
            phase_began = time.perf_counter()
            if owns_resources:
                resources._open(frozen_sources, signature, Event())
            preparation_timings["private_source_open_ms"] = (
                time.perf_counter() - phase_began
            ) * 1000
            phase_began = time.perf_counter()
            points, _ = previous.native.retained_tool.support.native_points(
                resources.sampler,
                replace(
                    initial, width=initial.width + 512, height=initial.height + 512
                ),
            )
            preparation_timings["native_point_union_ms"] = (
                time.perf_counter() - phase_began
            ) * 1000
            land_flags = None
            if not initial.over_land:
                resources.land.refresh_source(initial.get_scale())
                land_flags = np.asarray(
                    [resources.land.contains(*point) for point in points],
                    dtype=np.bool_,
                )
                land_flags.setflags(write=False)

            all_checks = []
            field_meta = []

            for si, stamp in enumerate(stamps):
                phase_began = time.perf_counter()
                staged = stage_wind(
                    resources.sampler,
                    stamp,
                    points[:, 0][None, :],
                    points[:, 1][None, :],
                )
                if staged is None:
                    raise ValueError(
                        f"Independent field preparation rejected at time step {si}"
                    )
                retained = previous.native.retained_tool.RetainedWind(
                    points, staged, initial, 256
                )
                if land_flags is not None:
                    retained.land_mask = previous.NativeLandMask(
                        points, land_flags, initial.get_scale()
                    )
                generation = startup.digest(
                    dict(
                        snapshot=snapshot,
                        step=si,
                        points=startup.array_content_hash(
                            (points, staged.first, staged.second)
                        ),
                        sources=staged.sources,
                        before=staged.before,
                        after=staged.after,
                    )
                )
                field = model.Field.create(generation, staged.first, staged.second)
                prepared = previous.Prepared(retained, field, None, ())
                preparation_timings["stage_and_field_ms"] = (
                    preparation_timings.get("stage_and_field_ms", 0)
                    + (time.perf_counter() - phase_began) * 1000
                )

                checks = []
                phase_began = time.perf_counter()
                view = initial
                for revision in range(25 if validate_numeric else 0):
                    if revision:
                        view = pan(view, -8 if revision <= 12 else 8, 0)
                    reference = barbs.stage_barbs(
                        resources.sampler, stamp, view, resources.land
                    )
                    candidates, _ = previous.native.retained_tool.support.native_points(
                        resources.sampler, view
                    )
                    if not np.array_equal(
                        previous.current_candidates(retained, view), candidates
                    ):
                        raise ValueError(
                            f"Independent candidate membership differs at time step {si}"
                        )
                    with previous.native.unavailable_numerics(resources.sampler):
                        selection = previous.make_selection(
                            prepared, view, stamp, barbs
                        )
                    decoded = [
                        transport.decode_component(image) for image in reference.images
                    ]
                    first_uv = np.stack(
                        (decoded[0], decoded[1], decoded[4], decoded[5]), axis=-1
                    )
                    second_uv = np.stack(
                        (decoded[2], decoded[3], decoded[4], decoded[5]), axis=-1
                    )
                    if not np.array_equal(
                        model.reconstruct(field, selection, 0),
                        first_uv,
                        equal_nan=True,
                    ) or not np.array_equal(
                        model.reconstruct(field, selection, 1),
                        second_uv,
                        equal_nan=True,
                    ):
                        raise ValueError(
                            f"Independent selection changed endpoints/anchors at time step {si}"
                        )
                    count = int(np.count_nonzero(selection.values[..., 0] >= 0))
                    try:
                        availability = selection_availability(
                            count,
                            reference.count,
                            first_uv,
                            second_uv,
                            allow_empty=allow_empty_reference,
                        )
                    except ValueError as exc:
                        raise ValueError(
                            f"{exc}; step={si}, revision={revision}, view={view.__dict__}"
                        ) from exc
                    checks.append(
                        dict(
                            revision=revision,
                            symbols=count,
                            exact=True,
                            step=si,
                            availability=availability,
                        )
                    )

                if not validate_numeric:
                    selection = previous.make_selection(prepared, initial, stamp, barbs)
                    count = int(np.count_nonzero(selection.values[..., 0] >= 0))
                    checks.append(
                        dict(
                            revision=0,
                            symbols=count,
                            exact=False,
                            step=si,
                            availability=(
                                "candidate-ready" if count else "candidate-empty"
                            ),
                        )
                    )

                preparation_timings["selection_oracle_ms"] = (
                    preparation_timings.get("selection_oracle_ms", 0)
                    + (time.perf_counter() - phase_began) * 1000
                )
                phase_began = time.perf_counter()
                np.savez(
                    directory / f"field_{si:03d}.npz",
                    points=points,
                    first=staged.first,
                    second=staged.second,
                    field=field.values,
                    **({"land_flags": land_flags} if land_flags is not None else {}),
                )
                preparation_timings["numeric_file_write_ms"] = (
                    preparation_timings.get("numeric_file_write_ms", 0)
                    + (time.perf_counter() - phase_began) * 1000
                )

                field_meta.append(
                    dict(
                        step=si,
                        before=staged.before,
                        after=staged.after,
                        generation=generation,
                        node_count=field.node_count,
                        file=f"field_{si:03d}.npz",
                    )
                )
                all_checks.append(checks)

            result.append((points, field_meta, all_checks))
        except Exception as exc:
            errors.append(exc)
        finally:
            phase_began = time.perf_counter()
            if owns_resources:
                resources.close()
            preparation_timings["private_resource_close_ms"] = (
                time.perf_counter() - phase_began
            ) * 1000

    if numerical_worker is None:
        thread = Thread(target=prepare, name="vertical-private-preparation")
        thread.start()
        thread.join(30)
        if thread.is_alive():
            raise RuntimeError("Bounded vertical preparation timed out")
    else:
        numerical_worker.call(prepare)
    if errors:
        raise errors[0]

    points, field_meta, all_checks = result[0]

    frame = canvas._gpu.scene.frame
    empty_initial = all_checks[0][0]["symbols"] == 0
    if (
        (frame.numerical_wind is None and not empty_initial)
        or frame.wind
        or frame.raster_wind is not None
    ):
        raise ValueError("Reference initial frame is not numerical-only")
    image_metadata = []
    for name, image in (
        ("background", frame.base.image),
        ("navigation", frame.navigation.image),
    ):
        np.save(
            directory / (name + ".npy"),
            np.frombuffer(image.constBits(), dtype=np.uint8),
        )
        image_metadata.append(
            dict(
                width=image.width(),
                height=image.height(),
                stride=image.bytesPerLine(),
                format=image.format().value,
            )
        )
    oracle = canvas._gpu.widget.grabFramebuffer() if capture_oracle else None
    if capture_oracle and (
        oracle.isNull() or not oracle.save(str(directory / "oracle.png"))
    ):
        raise ValueError("Unable to capture reference composition")
    empty_diagnostic = None
    if capture_oracle and empty_initial and allow_empty_reference:
        numerical = frame.numerical_wind
        atlas = numerical[0] if numerical is not None else None
        finite_counts = []
        if atlas is not None:
            decoded = [transport.decode_component(image) for image in atlas.pair.images]
            finite_counts = [
                int((np.isfinite(decoded[i]) & np.isfinite(decoded[i + 1])).sum())
                for i in (0, 2)
            ]
        empty_diagnostic = dict(
            reference_frame_symbols=atlas.count if atlas is not None else None,
            finite_endpoint_vectors=finite_counts,
            independent_selection_symbols=all_checks[0][0]["symbols"],
        )
        if atlas is not None and empty_layer_can_suppress(
            all_checks[0][0]["symbols"], atlas.count, finite_counts
        ):
            weather = canvas._gpu.scene.weather
            was_visible = weather.isVisible()
            weather.setVisible(False)
            without_empty = canvas._gpu.widget.grabFramebuffer()
            weather.setVisible(was_visible)
            without_empty.save(str(directory / "oracle-empty-layer-suppressed.png"))
            first_rgba = oracle.convertToFormat(oracle.Format.Format_RGBA8888)
            second_rgba = without_empty.convertToFormat(oracle.Format.Format_RGBA8888)
            a = np.frombuffer(first_rgba.constBits(), dtype=np.uint8).reshape(-1, 4)
            b = np.frombuffer(second_rgba.constBits(), dtype=np.uint8).reshape(-1, 4)
            empty_diagnostic["pixels_changed_by_empty_layer"] = int(
                np.any(a != b, axis=-1).sum()
            )
            # A proven-empty layer must contribute no pixels. Preserve the
            # uncorrected GPU readback, then use the same reference scene with
            # only that empty layer hidden. This is an explicit oracle correction,
            # not permission to drop real symbols or copy a bad GPU portrayal.
            oracle.save(str(directory / "oracle-original-empty-gpu.png"))
            oracle = without_empty
            oracle.save(str(directory / "oracle.png"))
            empty_diagnostic["oracle_semantics"] = (
                "reference-with-proven-empty-symbol-layer-hidden"
            )
        (directory / "empty-layer-diagnostic.json").write_text(
            json.dumps(empty_diagnostic, indent=2) + "\n"
        )

    metadata = dict(
        snapshot_sha256=snapshot,
        view=initial.__dict__,
        clocks=[datetime.fromtimestamp(s).isoformat() for s in stamps],
        field_meta=field_meta,
        reference_checks=all_checks,
        targets=[list(frame.base.target), list(frame.navigation.target)],
        dpr=frame.dpr,
        opacity=frame.opacity,
        sea_color=list(frame.sea_color),
        image_metadata=image_metadata,
        preparation_thread_closed=True,
        background_includes_cpu_weather=True,
        timed_preparation_available=False,
        empty_reference_supported=allow_empty_reference,
        empty_layer_diagnostic=empty_diagnostic,
        private_preparation_timings_ms=preparation_timings,
        oracle_pending=not capture_oracle,
        numerical_validation_pending=not validate_numeric,
    )
    for name in (
        ("background.npy", "navigation.npy", "oracle.png")
        if capture_oracle
        else ("background.npy", "navigation.npy")
    ):
        metadata.setdefault("files", {})[name] = startup.protocol.file_hash(
            directory / name
        )
    for name in (
        "oracle-original-empty-gpu.png",
        "oracle-empty-layer-suppressed.png",
        "empty-layer-diagnostic.json",
    ):
        if (directory / name).exists():
            metadata["files"][name] = startup.protocol.file_hash(directory / name)
    for fm in field_meta:
        metadata["files"][fm["file"]] = startup.protocol.file_hash(
            directory / fm["file"]
        )
    (directory / "resources.json").write_text(json.dumps(metadata, indent=2) + "\n")

    total_checks = sum(len(c) for c in all_checks)
    return dict(
        exported=True,
        exact_reference_selections=total_checks if validate_numeric else 0,
        time_steps=len(stamps),
        reference_empty_checks=sum(
            c["availability"] == "reference-empty"
            for checks in all_checks
            for c in checks
        ),
        private_preparation_timings_ms=preparation_timings,
        oracle_pending=not capture_oracle,
    )


def apply_preparation_view(canvas, requested_view):
    """Apply after native layout has settled; never stretch a mismatched view."""
    actual = dict(width=canvas.width(), height=canvas.height(), _zoom=canvas._zoom)
    expected = {key: requested_view[key] for key in actual}
    if actual != expected:
        raise ValueError(
            f"Preparation viewport differs: actual={actual}, requested={expected}"
        )
    canvas._center_lat = requested_view["_center_lat"]
    canvas._center_lon = requested_view["_center_lon"]
    canvas._invalidate_base()
    canvas.invalidate_navigation()
    canvas.update()
    return dict(actual=actual, requested=requested_view)


def tick_gap_summary(ticks):
    # The shared summary preserves input numeric types. Pass Python floats so
    # threshold counts remain ordinary ints and the evidence is JSON-safe.
    return startup.protocol.summary((np.diff(ticks) * 1000).tolist())


def export_worker(config, directory):
    import socket

    def no_network(*_args, **_kwargs):
        raise RuntimeError("Network disabled in vertical reference export")

    socket.socket.connect = socket.socket.connect_ex = no_network

    if config.get("service_directory"):
        from persistent_coverage import serve_requests

        def service(canvas, app, settle, grab):
            if config.get("data_policy", "legacy") == "legacy":
                return serve_requests(
                    canvas,
                    app,
                    settle,
                    grab,
                    config,
                    lambda *args, **kwargs: export_resources(
                        *args, **kwargs, allow_empty_reference=True
                    ),
                    apply_preparation_view,
                )
            from resident_weather_worker import ResidentWeatherWorker
            from gweatherrouting.qt import gpuweatherjobs as jobs
            from gweatherrouting.core.routingjob import freeze_wind_sources

            sources = tuple(canvas.controller.core.grib_manager.gribs)
            signature = tuple(jobs.source_key(source) for source in sources)
            frozen = freeze_wind_sources(sources)

            def factory():
                resources = jobs._WeatherResources(128 * 1024 * 1024)
                try:
                    resources._open(frozen, signature, Event())
                except Exception:
                    resources.close()
                    raise
                return resources

            worker = ResidentWeatherWorker(
                factory, retain=config["data_policy"] == "resident"
            )
            try:
                result = serve_requests(
                    canvas,
                    app,
                    settle,
                    grab,
                    config,
                    lambda *args, **kwargs: export_resources(
                        *args,
                        **kwargs,
                        allow_empty_reference=True,
                        numerical_worker=worker,
                        validate_numeric=False,
                        capture_oracle=False,
                    ),
                    apply_preparation_view,
                    validation_export=lambda *args, **kwargs: export_resources(
                        *args,
                        **kwargs,
                        allow_empty_reference=True,
                        numerical_worker=worker,
                    ),
                    request_frame=lambda: canvas._gpu._present(),
                )
            finally:
                worker.close()
            result["data_worker"] = dict(
                policy=config["data_policy"],
                owner=worker.owner,
                producer_gui_thread=get_ident(),
                events=worker.events,
            )
            return result

        config["_preparation_service"] = service

    requested_view = config.get("prepare_view")
    if requested_view is not None:

        def move_after_settle(canvas):
            # Persist actual and requested geometry before any failure/teardown.
            diagnostic = dict(
                actual=dict(
                    width=canvas.width(), height=canvas.height(), _zoom=canvas._zoom
                ),
                requested=requested_view,
            )
            (directory / "viewport-diagnostic.json").write_text(
                json.dumps(diagnostic, indent=2) + "\n"
            )
            apply_preparation_view(canvas, requested_view)

        config["_after_startup_settled"] = move_after_settle

    def before(canvas, app):
        stamps = config.get("scrub_stamps")
        fidelity = export_resources(
            canvas, app, directory, config["snapshot_sha256"], stamps=stamps
        )
        fidelity["host"] = dict(
            qt_platform=app.platformName(),
            dpr=canvas.devicePixelRatioF(),
            backend=canvas._gpu.widget.quickWindow()
            .rendererInterface()
            .graphicsApi()
            .name,
            samples=canvas._gpu.widget.format().samples(),
            alpha_bits=canvas._gpu.widget.format().alphaBufferSize(),
        )
        if config.get("prime_ms"):
            fidelity["priming"] = prime_quick(canvas._gpu.widget, config["prime_ms"])
        return fidelity

    config["_before_native_probe"] = before
    return startup.worker(config)


def read_bundle(directory, expected=None, byte_limit=64 * 1024 * 1024):
    """Decode immutable preparation output off GUI thread with an explicit cap."""
    metadata = json.loads((directory / "resources.json").read_text())
    if expected:
        for key, value in expected.items():
            if metadata[key] != value:
                raise ValueError(f"Prepared identity differs: {key}")
    files = metadata["files"]
    required = (
        {"background.npy", "navigation.npy"}
        | (set() if metadata.get("oracle_pending") else {"oracle.png"})
        | {fm["file"] for fm in metadata["field_meta"]}
    )
    if not required.issubset(files):
        raise ValueError("Prepared bundle lacks checksums")
    if any(Path(name).name != name for name in files):
        raise ValueError("Invalid bundle filename")
    if sum((directory / name).stat().st_size for name in files) > byte_limit:
        raise ValueError("Prepared bundle exceeds payload budget")
    for name, digest in files.items():
        if startup.protocol.file_hash(directory / name) != digest:
            raise ValueError("Prepared resource checksum changed")
    arrays = {}
    for fm in metadata["field_meta"]:
        with np.load(directory / fm["file"], allow_pickle=False) as payload:
            arrays[fm["file"]] = {name: payload[name].copy() for name in payload.files}
            for array in arrays[fm["file"]].values():
                array.setflags(write=False)
    for name in ("background", "navigation"):
        arrays[name] = np.load(directory / (name + ".npy"), allow_pickle=False)
        arrays[name].setflags(write=False)
    if (
        sum(
            a.nbytes
            for data in arrays.values()
            for a in (data.values() if isinstance(data, dict) else (data,))
        )
        > byte_limit
    ):
        raise ValueError("Decoded bundle exceeds payload budget")
    return metadata, arrays, directory


def prepared_fields(metadata, arrays):
    from gweatherrouting.rendering.weatherbarbs import BarbView
    from gweatherrouting.rendering.weatherstaging import StagedWind

    view = BarbView(**metadata["view"])
    result = []
    for fm in metadata["field_meta"]:
        payload = arrays[fm["file"]]
        staged = StagedWind(
            fm["before"], fm["after"], payload["first"], payload["second"], ()
        )
        retained = previous.native.retained_tool.RetainedWind(
            payload["points"], staged, view, 256
        )
        if "land_flags" in payload:
            retained.land_mask = previous.NativeLandMask(
                payload["points"], payload["land_flags"], view.get_scale()
            )
        field = model.Field(fm["generation"], payload["field"], fm["node_count"])
        result.append(
            (staged, retained, previous.Prepared(retained, field, None, ()), field)
        )
    return result


def prepare_gui_bundle(bundle):
    """Create CPU-only immutable images/field lookup before GUI publication."""
    from PySide6.QtGui import QImage

    metadata, arrays, directory = bundle
    arrays["_gui_prepared"] = prepared_fields(metadata, arrays)
    images = []
    for name, info in zip(("background", "navigation"), metadata["image_metadata"]):
        raw = arrays[name]
        images.append(
            QImage(
                raw.data,
                info["width"],
                info["height"],
                info["stride"],
                QImage.Format(info["format"]),
            ).copy()
        )
    arrays["_gui_images"] = tuple(images)
    return bundle


def assert_same_prepared_assets(candidate, reference):
    cm, ca, _ = candidate
    rm, ra, _ = reference
    for key in (
        "snapshot_sha256",
        "view",
        "clocks",
        "targets",
        "dpr",
        "opacity",
        "sea_color",
        "image_metadata",
        "field_meta",
    ):
        if cm[key] != rm[key]:
            raise ValueError(f"Prepared asset identity changed: {key}")
    for fm in cm["field_meta"]:
        names = ["points", "first", "second", "field"]
        if "land_flags" in ca[fm["file"]] or "land_flags" in ra[fm["file"]]:
            if "land_flags" not in ca[fm["file"]] or "land_flags" not in ra[fm["file"]]:
                raise ValueError("Prepared native land-mask payload differs")
            names.append("land_flags")
        for name in names:
            left, right = ca[fm["file"]][name], ra[fm["file"]][name]
            if left.dtype != right.dtype or not np.array_equal(
                left, right, equal_nan=True
            ):
                raise ValueError(f"Prepared field differs: {fm['file']}/{name}")
    for name in ("background", "navigation"):
        if ca[name].dtype != ra[name].dtype or not np.array_equal(ca[name], ra[name]):
            raise ValueError(f"Prepared image differs: {name}")


def run_candidate(
    directory,
    shader_directory,
    prime_ms=0,
    scenario="scrub",
    coverage_step_px=48,
    preparation=None,
):
    """Independent QQuickWidget: no TrackMap, MapFrame, or old MapScene instance."""
    from PySide6.QtCore import QEventLoop, QPointF, QTimer, Qt, QUrl
    from PySide6.QtGui import QColor, QImage, QSurfaceFormat, QWheelEvent
    from PySide6.QtQml import QQmlComponent
    from PySide6.QtQuickWidgets import QQuickWidget
    from PySide6.QtWidgets import QApplication, QWidget, QLabel
    from gweatherrouting.qt.gpuweather import WeatherImageProvider, encode_component
    from gweatherrouting.qt.gpuscene import ImageItem
    from gweatherrouting.rendering.weatherbarbs import BarbView
    from gweatherrouting.rendering.weatherstaging import StagedWind

    metadata, initial_arrays, _ = read_bundle(directory)
    initial = BarbView(**metadata["view"])

    # Load all pre-exported numerical fields, one per forecast time step.
    clocks = [datetime.fromisoformat(c).timestamp() for c in metadata["clocks"]]
    prepared_sets = prepared_fields(metadata, initial_arrays)

    from gweatherrouting.rendering import weatherbarbs as barbs

    # Missing-data/source preparation is OFFLINE for this first vertical slice.
    # Installing an explicit failure guard prevents a hidden warm GDAL cache
    # from making the independent input/render path appear preparation-free.
    from osgeo import gdal

    old_open, old_open_ex = gdal.Open, gdal.OpenEx

    def blocked(*_args, **_kwargs):
        raise AssertionError("Preparation/GDAL accessed by independent viewport")

    gdal.Open = gdal.OpenEx = blocked
    app = QApplication([])
    recorder, errors, selection_ms = RenderRecorder(), [], []
    counts = dict(texture_uploads=0, texture_upload_bytes=0, vertex_upload_bytes=0)

    if scenario in (
        "coverage_miss",
        "coverage_load",
        "coverage_stream",
        "coverage_perf",
    ):
        # Pan trajectory: leave coverage, then return. 24 inputs total.
        pan_trajectory = coverage_miss_trajectory(24, coverage_step_px)
        if len(pan_trajectory) != 24:
            raise ValueError(
                f"Coverage-miss trajectory has {len(pan_trajectory)} steps, expected 24"
            )
    else:
        n_times = len(prepared_sets)
        trajectory = scrub_trajectory(n_times)
        if len(trajectory) != 24:
            raise ValueError(
                f"Timeline scrub trajectory has {len(trajectory)} steps, expected 24"
            )

    class Viewport(QWidget):
        def __init__(self, prepared_sets, clocks, scenario, coverage_step_px):
            super().__init__()
            self.view, self.revision = initial, None
            self.resource_view, self.metadata = initial, metadata
            self.prepared_sets = prepared_sets
            self.clocks = clocks
            self.scenario = scenario
            self.coverage_step_px = coverage_step_px
            if scenario in (
                "coverage_miss",
                "coverage_load",
                "coverage_stream",
                "coverage_perf",
            ):
                self.pan_trajectory = coverage_miss_trajectory(24, coverage_step_px)
                self.pan_index = 0
                self.time_index = 0
            else:
                self.trajectory = trajectory
                self.time_index = trajectory[0]
            self.coverage_miss = False
            self.coverage_miss_steps = []
            self.resize(initial.width, initial.height)
            self.wind_symbols = 0
            self.quick = QQuickWidget(self)
            format_ = QSurfaceFormat()
            format_.setSamples(4)
            format_.setAlphaBufferSize(8)
            self.quick.setFormat(format_)
            self.quick.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
            self.quick.setGeometry(0, 0, initial.width, initial.height)
            self.quick.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.quick.setClearColor(QColor.fromRgbF(*metadata["sea_color"]))
            self.wind_status = QLabel(self)
            self.wind_status.setGeometry(8, 8, initial.width - 16, 48)
            self.wind_status.setWordWrap(True)
            self.wind_status.setStyleSheet(
                "background: #202830; color: white; padding: 6px;"
            )
            self.wind_status.setText("No wind symbols selected for this view/time")
            self.wind_status.hide()
            self.component = QQmlComponent(self.quick.engine())
            self.component.setData(b"import QtQuick\nItem { clip: true }", QUrl())
            self.root = self.component.create()
            self.quick.setContent(QUrl(), self.component, self.root)
            self.background = ImageItem(self.root, self.quick, counts)
            self.navigation = ImageItem(self.root, self.quick, counts)
            self.navigation.setZ(10000)
            self.images = []
            for name, info in zip(
                ("background", "navigation"), metadata["image_metadata"]
            ):
                raw = initial_arrays[name]
                self.images.append(
                    QImage(
                        raw.data,
                        info["width"],
                        info["height"],
                        info["stride"],
                        QImage.Format(info["format"]),
                    ).copy()
                )
            self.field_provider, self.selection_provider = (
                WeatherImageProvider(),
                WeatherImageProvider(),
            )
            self.quick.engine().addImageProvider("verticalfield", self.field_provider)
            self.quick.engine().addImageProvider(
                "verticalselection", self.selection_provider
            )
            self.weather_component = QQmlComponent(
                self.quick.engine(),
                QUrl.fromLocalFile(str(shader_directory / "WeatherBarbs.qml")),
            )
            self.weather = self.weather_component.create()
            if self.weather is None:
                raise RuntimeError(str(self.weather_component.errors()))
            self.weather.setParent(self.root)
            self.weather.setParentItem(self.root)
            self.weather.setZ(100)
            self.weather.setWidth(initial.width)
            self.weather.setHeight(initial.height)
            self.weather.setOpacity(metadata["opacity"])
            self.unavailable_component = QQmlComponent(self.quick.engine())
            self.unavailable_component.setData(
                b"""import QtQuick
Rectangle {
    property string statusText: "Map/weather unavailable here - preparation disabled in prototype"
    color: "#202830"
    Text {
        anchors.centerIn: parent
        text: parent.statusText
        color: "white"
        wrapMode: Text.WordWrap
        width: parent.width - 40
        horizontalAlignment: Text.AlignHCenter
    }
}""",
                QUrl(),
            )
            self.unavailable = self.unavailable_component.create()
            if self.unavailable is None:
                raise RuntimeError(str(self.unavailable_component.errors()))
            self.unavailable.setParent(self.root)
            self.unavailable.setParentItem(self.root)
            self.unavailable.setWidth(initial.width)
            self.unavailable.setHeight(initial.height)
            self.unavailable.setZ(20000)
            self.unavailable.setVisible(False)
            self.mask_component = QQmlComponent(self.quick.engine())
            self.mask_component.setData(
                b'import QtQuick\nRectangle { color: "#202830" }', QUrl()
            )
            self.masks = []
            for _ in range(4):
                mask = self.mask_component.create()
                mask.setParent(self.root)
                mask.setParentItem(self.root)
                mask.setZ(15000)
                mask.setVisible(False)
                self.masks.append(mask)
            self.update_view(record=False)

        def update_view(self, record=True):
            self.wind_status.hide()
            for mask in self.masks:
                mask.setVisible(False)
            self.unavailable.setHeight(initial.height)
            began = time.perf_counter()
            stamp = self.clocks[self.time_index]
            staged, retained, prepared, field = self.prepared_sets[self.time_index]

            # Coverage check: if the panned view is outside the retained field's
            # padded coverage, show a loading state instead of stale weather data.
            # Never silently render stale or clipped weather.
            targets, reason = coverage_for_view(
                self.resource_view, self.view, self.metadata["targets"], retained
            )
            self.coverage_miss = reason is not None
            for item in (self.background, self.navigation, self.weather):
                item.setVisible(not self.coverage_miss)
            self.unavailable.setVisible(self.coverage_miss)
            if self.coverage_miss:
                self.coverage_miss_steps.append(
                    dict(
                        revision=self.revision,
                        reason=reason,
                        view=dict(
                            latitude=self.view._center_lat,
                            longitude=self.view._center_lon,
                        ),
                    )
                )
                # Retain resources, but hide ALL old content, including CPU
                # weather baked into the background. Zero vectors are real
                # calm wind, not a missing-data representation.
                iso_clock = datetime.fromtimestamp(stamp).isoformat()
                if record:
                    recorder.submit(
                        len(recorder.submissions),
                        self.revision,
                        time.perf_counter(),
                        state(self.view, iso_clock),
                        dict(
                            numerical_weather=dict(
                                active=False,
                                symbols=0,
                                mixed=False,
                                coverage_miss=True,
                            ),
                            field_image_requests=self.field_provider.requests,
                            selection_image_requests=self.selection_provider.requests,
                            time_step=self.time_index,
                            coverage_miss=True,
                            availability="unavailable",
                            preparation_pending=False,
                            stale_layers_visible=False,
                        ),
                    )
                self.quick.update()
                return

            # Covered: render normally with exact-time numerical selection.
            selection = previous.make_selection(prepared, self.view, stamp, barbs)
            self.wind_symbols = int(np.count_nonzero(selection.values[..., 0] >= 0))
            if self.wind_symbols == 0:
                self.weather.setVisible(False)
                self.wind_status.show()
                self.wind_status.raise_()
            selection_ms.append((time.perf_counter() - began) * 1000)

            # Publish field images (u0, v0, u1, v1) for this forecast time.
            values = field.values
            images = tuple(encode_component(values[..., i]) for i in range(4))
            pair = SimpleNamespace(
                images=images, nbytes=sum(i.sizeInBytes() for i in images)
            )
            for name, url in zip(
                ("u0Source", "v0Source", "u1Source", "v1Source"),
                self.field_provider.publish(pair),
            ):
                self.weather.setProperty(
                    name,
                    QUrl(url.replace("image://weather/", "image://verticalfield/")),
                )

            # Publish selection images (nodeId, anchorX, anchorY) for this view/time.
            sel_values = selection.values
            sel_images = tuple(encode_component(sel_values[..., i]) for i in (0, 1, 2))
            sel_pair = SimpleNamespace(
                images=sel_images,
                nbytes=sum(i.sizeInBytes() for i in sel_images),
            )
            for name, url in zip(
                ("nodeIdSource", "anchorXSource", "anchorYSource"),
                self.selection_provider.publish(sel_pair),
            ):
                self.weather.setProperty(
                    name,
                    QUrl(url.replace("image://weather/", "image://verticalselection/")),
                )
            self.weather.setProperty("rows", sel_values.shape[0])
            self.weather.setProperty("columns", sel_values.shape[1])
            self.weather.setProperty("blend", staged.weight(stamp))
            for item, image, target in zip(
                (self.background, self.navigation), self.images, targets
            ):
                item.present(
                    SimpleNamespace(image=image, target=target, smooth=False),
                    self.metadata["dpr"],
                )
            iso_clock = datetime.fromtimestamp(stamp).isoformat()
            if not self.weather.property("ready") and record:
                raise ValueError(
                    "Independent selection not ready; no stale success allowed"
                )
            if record:
                recorder.submit(
                    len(recorder.submissions),
                    self.revision,
                    time.perf_counter(),
                    state(self.view, iso_clock),
                    dict(
                        numerical_weather=dict(
                            active=bool(self.weather.property("ready"))
                            and self.wind_symbols > 0,
                            symbols=int(np.count_nonzero(sel_values[..., 0] >= 0)),
                            mixed=False,
                            coverage_miss=False,
                        ),
                        field_image_requests=self.field_provider.requests,
                        selection_image_requests=self.selection_provider.requests,
                        time_step=self.time_index,
                        coverage_miss=False,
                        availability=(
                            "ready" if self.wind_symbols else "reference-empty"
                        ),
                    ),
                )
            self.quick.update()

        def wheelEvent(self, event):
            # Time scrub is driven by the dispatch loop, not pointer input.
            event.accept()

        def install_bundle(self, bundle):
            began = time.perf_counter()
            meta, arrays, _directory = bundle
            self.resource_view = BarbView(**meta["view"])
            self.metadata = meta
            self.prepared_sets = arrays.get("_gui_prepared") or prepared_fields(
                meta, arrays
            )
            fields_done = time.perf_counter()
            self.clocks = [
                datetime.fromisoformat(c).timestamp() for c in meta["clocks"]
            ]
            self.time_index = 0
            if "_gui_images" in arrays:
                self.images = list(arrays["_gui_images"])
            else:
                self.images = []
                for name, info in zip(
                    ("background", "navigation"), meta["image_metadata"]
                ):
                    raw = arrays[name]
                    self.images.append(
                        QImage(
                            raw.data,
                            info["width"],
                            info["height"],
                            info["stride"],
                            QImage.Format(info["format"]),
                        ).copy()
                    )
            self.update_view(record=False)
            self.last_publication_timing_ms = dict(
                prepared_fields_ms=(fields_done - began) * 1000,
                images_and_selection_bind_ms=(time.perf_counter() - fields_done) * 1000,
                total_ms=(time.perf_counter() - began) * 1000,
            )

        def resident_preview(self):
            targets, covered = partial_image_coverage(
                self.resource_view, self.view, self.metadata["targets"]
            )
            left, top, width, height = covered
            if width <= 0 or height <= 0:
                return covered
            for item, image, target in zip(
                (self.background, self.navigation), self.images, targets
            ):
                item.present(
                    SimpleNamespace(image=image, target=target, smooth=False),
                    self.metadata["dpr"],
                )
                item.setVisible(True)
            self.weather.setVisible(False)
            self.unavailable.setHeight(64)
            self.unavailable.setProperty(
                "statusText",
                "Loading: resident map/fill preview only; wind barbs unavailable. Shaded area has no prepared coverage.",
            )
            rectangles = [
                (0, 0, initial.width, top),
                (0, top + height, initial.width, initial.height - top - height),
                (0, top, left, height),
                (left + width, top, initial.width - left - width, height),
            ]
            for mask, (x, y, w, h) in zip(self.masks, rectangles):
                mask.setX(x)
                mask.setY(y)
                mask.setWidth(max(0, w))
                mask.setHeight(max(0, h))
                mask.setVisible(w > 0 and h > 0)
            self.quick.update()
            return covered

    def loading_probe(viewport):
        from coverage_scheduler import CoverageScheduler

        work = preparation["directory"]
        work.mkdir(parents=True, exist_ok=False)
        target = pan(initial, -576, 0)
        expected = {
            key: metadata[key]
            for key in ("snapshot_sha256", "dpr", "opacity", "sea_color")
        }
        expected.update(view=target.__dict__, clocks=[metadata["clocks"][0]])
        release = Event()
        process_statuses = []

        def prepare(request, cancel):
            while not release.wait(0.01):
                if cancel.is_set():
                    raise RuntimeError("Preparation cancelled before launch")
            if cancel.is_set():
                raise RuntimeError("Preparation cancelled")
            view_file = work / "requested-view.json"
            view_file.write_text(json.dumps(target.__dict__))
            output = work / "prepared-result.json"
            resources = work / "resources"
            env = run_protocol.environment()
            env["PYTHONPATH"] = os.pathsep.join(
                (str(preparation["app_root"]), str(ROOT))
            )
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--app-root",
                str(preparation["app_root"]),
                "--snapshot",
                str(preparation["snapshot"]),
                "--resources",
                str(resources),
                "--output",
                str(output),
                "--scenario",
                "stationary",
                "--scrub-steps",
                "1",
                "--prepare-view",
                str(view_file),
            ]
            status = run_protocol.supervise(
                command, ROOT, env, work / "preparation", timeout=90, cancel=cancel
            )
            process_statuses.append(status)
            run_protocol.require_clean(status)
            return read_bundle(resources, expected=expected)

        scheduler = CoverageScheduler(prepare)
        viewport.view = target
        viewport.update_view(record=False)
        if not viewport.coverage_miss:
            scheduler.close()
            raise ValueError("Destination did not miss coverage")
        viewport.unavailable.setProperty(
            "statusText", "Loading map/weather for requested area..."
        )
        loading_image = viewport.quick.grabFramebuffer()
        if (
            loading_image.isNull()
            or loading_image.pixelColor(0, 0).name() != "#202830"
            or any(
                item.isVisible()
                for item in (viewport.background, viewport.navigation, viewport.weather)
            )
        ):
            scheduler.close()
            raise ValueError("Loading state exposed stale layers")
        request = scheduler.request(
            (
                metadata["snapshot_sha256"],
                metadata["clocks"][0],
                target._center_lat,
                target._center_lon,
            )
        )
        loop, pulse, watchdog = QEventLoop(), QTimer(), QTimer()
        pulse.setTimerType(Qt.TimerType.PreciseTimer)
        pulse.setInterval(16)
        watchdog.setSingleShot(True)
        ticks, camera_states, failures = [], [], []
        loaded, began = [], time.perf_counter()

        def fail(message):
            failures.append(message)
            for item in (viewport.background, viewport.navigation, viewport.weather):
                item.setVisible(False)
            viewport.unavailable.setVisible(True)
            viewport.unavailable.setProperty(
                "statusText", "Map/weather preparation failed"
            )
            loop.quit()

        def verify_loaded(bundle):
            try:
                observed = viewport.quick.grabFramebuffer().convertToFormat(
                    QImage.Format.Format_RGBA8888
                )
                oracle = QImage(str(bundle[2] / "oracle.png")).convertToFormat(
                    QImage.Format.Format_RGBA8888
                )
                if (
                    oracle.isNull()
                    or observed.size() != oracle.size()
                    or bytes(observed.constBits()) != bytes(oracle.constBits())
                ):
                    observed.save(str(work / "post-load-diagnostic.png"))
                    raise ValueError(
                        "New-area complete composition differs from reference"
                    )
                if viewport.coverage_miss or viewport.unavailable.isVisible():
                    raise ValueError("New-area resources did not become ready")
                loaded.append(
                    dict(
                        post_load_complete_pixels_exact=True,
                        generation=request.generation,
                        prepared_view=bundle[0]["view"],
                    )
                )
                loop.quit()
            except Exception as exc:
                fail(str(exc))

        def tick():
            try:
                ticks.append(time.perf_counter() - began)
                if len(ticks) <= 32:
                    # Real camera state changes while preparation is held.
                    viewport.view = pan(target, -8 if len(ticks) % 2 else 0, 0)
                    viewport.update_view(record=False)
                    camera_states.append(state(viewport.view, metadata["clocks"][0]))
                    if not viewport.coverage_miss:
                        raise ValueError("Stalled preparation exposed stale content")
                if len(ticks) == 32:
                    release.set()
                result = scheduler.take()
                if result:
                    completed, bundle, error = result
                    if error:
                        raise RuntimeError(error)
                    if completed != request or viewport.view != target:
                        raise ValueError("Obsolete generation/view reached publication")
                    viewport.install_bundle(bundle)
                    QTimer.singleShot(100, lambda: verify_loaded(bundle))
            except Exception as exc:
                fail(str(exc))

        pulse.timeout.connect(tick)
        watchdog.timeout.connect(lambda: fail("Bounded preparation probe timed out"))
        try:
            pulse.start()
            watchdog.start(115000)
            loop.exec()
        finally:
            pulse.stop()
            watchdog.stop()
            scheduler.close()
        (work / "loading-diagnostic.json").write_text(
            json.dumps(
                dict(
                    failures=failures,
                    loaded=loaded,
                    gui_tick_seconds=ticks,
                    held_preparation_camera_states=camera_states,
                    preparation_statuses=process_statuses,
                    scheduler_events=scheduler.events,
                ),
                indent=2,
            )
            + "\n"
        )
        if failures or not loaded:
            raise RuntimeError("; ".join(failures) or "No new-area publication")
        return dict(
            protocol="independent-coverage-load-a0e-v1",
            snapshot_sha256=metadata["snapshot_sha256"],
            new_area=loaded[0],
            preparation_statuses=process_statuses,
            scheduler_events=scheduler.events,
            gui_ticks=len(ticks),
            gui_tick_seconds=ticks,
            held_preparation_camera_states=camera_states,
            held_preparation_camera_inputs=len(camera_states),
            gui_tick_gap_ms=tick_gap_summary(ticks),
            preparation_available_during_replay=True,
            presentation_measured=False,
            end_to_end_speedup_proven=False,
            payload_limit_bytes=64 * 1024 * 1024,
            loading_pixels_verified=True,
            host=dict(
                qt_platform=app.platformName(),
                dpr=viewport.devicePixelRatioF(),
                backend=viewport.quick.quickWindow()
                .rendererInterface()
                .graphicsApi()
                .name,
            ),
        )

    def persistent_probe(viewport):
        from coverage_scheduler import CoverageScheduler
        from persistent_coverage import PersistentPreparation, atomic_json

        work = preparation["directory"]
        work.mkdir(parents=True, exist_ok=False)
        service = PersistentPreparation(
            work / "producer",
            preparation["app_root"],
            preparation["snapshot"],
            data_policy=preparation.get("data_policy", "legacy"),
        )
        performance_mode = scenario == "coverage_perf"
        readback_state = dict(active=False, outside=0, forbidden=0)
        original_grab = viewport.quick.grabFramebuffer

        def guarded_grab():
            if readback_state["active"]:
                readback_state["forbidden"] += 1
                raise AssertionError(
                    "Explicit readback attempted inside measured input window"
                )
            readback_state["outside"] += 1
            return original_grab()

        if performance_mode:
            viewport.quick.grabFramebuffer = guarded_grab
        release = Event()
        responses, diagnostics = [], []
        freshness = None

        def prepare(request, cancel):
            while not release.wait(0.01):
                if cancel.is_set():
                    raise RuntimeError("Preparation request superseded during hold")
            view = json.loads(request.identity[0])
            directory, response = service.prepare(request, view, clocks[0], cancel)
            expected = {
                key: metadata[key]
                for key in ("snapshot_sha256", "dpr", "opacity", "sea_color")
            }
            expected.update(view=view, clocks=[metadata["clocks"][0]])
            bundle = read_bundle(directory, expected)
            if performance_mode:
                prepare_gui_bundle(bundle)
            return bundle, response

        scheduler = CoverageScheduler(prepare)
        try:
            if performance_mode:
                # Producer initialization includes legacy correctness grabs. It
                # must finish BEFORE either matched measured input window.
                deadline = time.perf_counter() + 65
                while not service.observe_ready():
                    app.processEvents()
                    if time.perf_counter() > deadline:
                        raise TimeoutError(
                            "Producer startup timed out before measurement"
                        )
                    time.sleep(0.005)
            for leg in range(2):
                target = pan(viewport.resource_view, -576, 0)
                viewport.view = target
                viewport.update_view(record=False)
                if not viewport.coverage_miss:
                    raise ValueError("Persistent probe must leave resident coverage")
                coverage = viewport.resident_preview()
                app.processEvents()
                first = viewport.quick.grabFramebuffer().convertToFormat(
                    QImage.Format.Format_RGBA8888
                )
                viewport.view = pan(target, -8, 0)
                viewport.update_view(record=False)
                viewport.resident_preview()
                app.processEvents()
                second = viewport.quick.grabFramebuffer().convertToFormat(
                    QImage.Format.Format_RGBA8888
                )
                dpr = metadata["dpr"]
                x, y, w, h = [round(v * dpr) for v in (32, 120, 200, 400)]
                before = first.copy(x + round(8 * dpr), y, w, h)
                after = second.copy(x, y, w, h)
                content_colors = len(
                    np.unique(
                        np.frombuffer(after.constBits(), dtype=np.uint8).reshape(-1, 4),
                        axis=0,
                    )
                )
                boundary_x = round((coverage[2] - 32) * dpr)
                boundary_width = round(64 * dpr)
                boundary_before = first.copy(
                    boundary_x + round(8 * dpr), y, boundary_width, h
                )
                boundary_after = second.copy(boundary_x, y, boundary_width, h)
                boundary_unmoved = first.copy(boundary_x, y, boundary_width, h)
                if (
                    coverage[2] < 248
                    or before.isNull()
                    or after.isNull()
                    or bytes(before.constBits()) != bytes(after.constBits())
                    or (leg == 0 and content_colors < 4)
                    or bytes(boundary_before.constBits())
                    != bytes(boundary_after.constBits())
                    or bytes(boundary_unmoved.constBits())
                    == bytes(boundary_after.constBits())
                    or second.pixelColor(second.width() - 2, round(400 * dpr)).name()
                    != "#202830"
                    or viewport.weather.isVisible()
                ):
                    second.save(str(work / f"preview-failed-{leg}.png"))
                    raise ValueError("Resident preview movement/coverage mask differs")
                viewport.view = target
                viewport.update_view(record=False)
                viewport.resident_preview()
                release.clear()
                request = scheduler.request(
                    (json.dumps(target.__dict__, sort_keys=True),)
                )
                loop, pulse, watchdog = QEventLoop(), QTimer(), QTimer()
                pulse.setTimerType(Qt.TimerType.PreciseTimer)
                pulse.setInterval(16)
                watchdog.setSingleShot(True)
                ticks, inputs, failures, accepted = [], [], [], []
                callback_ms, render_signals = [], []
                began = time.perf_counter()
                publishing = False

                def after_rendering():
                    render_signals.append(time.perf_counter() - began)

                def fail(message):
                    failures.append(message)
                    for item in (
                        viewport.background,
                        viewport.navigation,
                        viewport.weather,
                    ):
                        item.setVisible(False)
                    viewport.unavailable.setHeight(initial.height)
                    viewport.unavailable.setVisible(True)
                    viewport.unavailable.setProperty(
                        "statusText", "Map/weather preparation failed"
                    )
                    loop.quit()

                def verify(bundle, response):
                    try:
                        if performance_mode:
                            if viewport.coverage_miss or not viewport.weather.property(
                                "ready"
                            ):
                                raise ValueError("Provisional publication not ready")
                            accepted.append(
                                dict(
                                    response=response,
                                    validation_pending=True,
                                    reference_symbols=None,
                                    candidate_symbols=viewport.wind_symbols,
                                    gui_publication_ms=viewport.last_publication_timing_ms,
                                    request_to_provisional_seconds=time.perf_counter()
                                    - began,
                                    preview_pixels_move_exact=True,
                                    missing_coverage_masked=True,
                                )
                            )
                            loop.quit()
                            return
                        actual = viewport.quick.grabFramebuffer().convertToFormat(
                            QImage.Format.Format_RGBA8888
                        )
                        oracle = QImage(str(bundle[2] / "oracle.png")).convertToFormat(
                            QImage.Format.Format_RGBA8888
                        )
                        if (
                            oracle.isNull()
                            or actual.size() != oracle.size()
                            or bytes(actual.constBits()) != bytes(oracle.constBits())
                            or viewport.coverage_miss
                            or viewport.unavailable.isVisible()
                        ):
                            actual.save(str(work / f"post-load-failed-{leg}.png"))
                            raise ValueError(
                                "Persistent destination composition differs from reference"
                            )
                        reference_symbols = bundle[0]["reference_checks"][0][0][
                            "symbols"
                        ]
                        if viewport.wind_symbols != reference_symbols:
                            raise ValueError(
                                "Published wind count differs from reference"
                            )
                        if reference_symbols == 0:
                            if (
                                not viewport.wind_status.isVisible()
                                or viewport.weather.isVisible()
                            ):
                                raise ValueError(
                                    "Reference-empty wind state was not explicitly labeled"
                                )
                            viewport.grab().save(
                                str(work / f"reference-empty-status-{leg}.png")
                            )
                        accepted.append(
                            dict(
                                response=response,
                                post_load_pixels_exact=True,
                                preview_pixels_move_exact=True,
                                preview_content_colors=content_colors,
                                geographic_detail_motion_demonstrated=content_colors
                                >= 4,
                                coverage_boundary_moves_exact=True,
                                missing_coverage_masked=True,
                                reference_symbols=reference_symbols,
                                availability=(
                                    "ready" if reference_symbols else "reference-empty"
                                ),
                                empty_status_visible=viewport.wind_status.isVisible(),
                                gui_publication_ms=viewport.last_publication_timing_ms,
                                oracle_semantics=(
                                    bundle[0].get("empty_layer_diagnostic") or {}
                                ).get(
                                    "oracle_semantics", "unchanged-reference-gpu-scene"
                                ),
                                request_to_verified_seconds=time.perf_counter() - began,
                            )
                        )
                        loop.quit()
                    except Exception as exc:
                        fail(str(exc))

                def tick():
                    nonlocal publishing
                    callback_began = time.perf_counter()
                    try:
                        ticks.append(time.perf_counter() - began)
                        if not publishing and (
                            not performance_mode or len(ticks) <= 32
                        ):
                            viewport.view = pan(target, -8 if len(ticks) % 2 else 0, 0)
                            viewport.update_view(record=False)
                            viewport.resident_preview()
                            inputs.append(
                                dict(
                                    seconds=ticks[-1],
                                    state=state(viewport.view, metadata["clocks"][0]),
                                    availability="partial-preview",
                                    numerical_wind_ready=False,
                                )
                            )
                        if len(ticks) == 32:
                            release.set()
                        result = scheduler.take()
                        if result:
                            completed, value, error = result
                            if error:
                                raise RuntimeError(error)
                            if completed != request:
                                raise ValueError(
                                    "Obsolete persistent generation reached publication"
                                )
                            bundle, response = value
                            viewport.view = target
                            viewport.install_bundle(bundle)
                            publishing = True
                            QTimer.singleShot(100, lambda: verify(bundle, response))
                    except Exception as exc:
                        fail(str(exc))
                    finally:
                        callback_ms.append(
                            (time.perf_counter() - callback_began) * 1000
                        )

                pulse.timeout.connect(tick)
                watchdog.timeout.connect(
                    lambda: fail("Persistent coverage request timed out")
                )
                try:
                    if performance_mode:
                        readback_state["active"] = True
                        viewport.quick.quickWindow().afterRendering.connect(
                            after_rendering, Qt.ConnectionType.DirectConnection
                        )
                    pulse.start()
                    watchdog.start(85000)
                    loop.exec()
                finally:
                    pulse.stop()
                    watchdog.stop()
                    if performance_mode:
                        viewport.quick.quickWindow().afterRendering.disconnect(
                            after_rendering
                        )
                        readback_state["active"] = False
                diagnostic = dict(
                    leg=leg,
                    ticks=ticks,
                    inputs=inputs,
                    failures=failures,
                    accepted=accepted,
                    gui_tick_gap_ms=tick_gap_summary(ticks),
                    gui_callback_ms=startup.protocol.summary(callback_ms),
                    after_rendering_seconds=render_signals,
                )
                diagnostics.append(diagnostic)
                atomic_json(work / "loading-diagnostic.json", diagnostics)
                if failures or not accepted:
                    raise RuntimeError(
                        "; ".join(failures) or "No persistent publication"
                    )
                responses.append(accepted[0])
            # Native A->B->A: wait until old A is actively preparing, replace
            # its pending successor twice, and bind only the newest A generation.
            target_a = pan(initial, -576, 0)
            target_b = pan(initial, -1440, 0)
            release.set()
            old_a = scheduler.request((json.dumps(target_a.__dict__, sort_keys=True),))
            latest_a = old_a
            intermediate_b = None
            replaced = False
            loop, pulse, watchdog = QEventLoop(), QTimer(), QTimer()
            pulse.setInterval(16)
            pulse.setTimerType(Qt.TimerType.PreciseTimer)
            watchdog.setSingleShot(True)
            published, failures, transitions, freshness_ticks = [], [], [], []
            freshness_render_signals = []
            began = time.perf_counter()

            def freshness_rendered():
                freshness_render_signals.append(time.perf_counter() - began)

            def show_requested(view, generation):
                viewport.view = view
                viewport.update_view(record=False)
                viewport.resident_preview()
                transitions.append(
                    dict(
                        generation=generation,
                        seconds=time.perf_counter() - began,
                        view=view.__dict__,
                    )
                )

            show_requested(target_a, old_a.generation)

            def freshness_fail(message):
                failures.append(message)
                loop.quit()

            def verify_latest(bundle, response):
                try:
                    if performance_mode:
                        discarded = any(
                            event in ("discarded", "discarded-ready")
                            and generation == old_a.generation
                            for event, generation in scheduler.events
                        )
                        if (
                            not discarded
                            or response["generation"] != latest_a.generation
                            or viewport.view != target_a
                            or viewport.coverage_miss
                            or response["pid"] != responses[0]["response"]["pid"]
                            or (
                                service.directory
                                / f"response-{intermediate_b.generation}.json"
                            ).exists()
                        ):
                            raise ValueError(
                                "Provisional A-B-A generation/coverage gate failed"
                            )
                        published.append(
                            dict(
                                generation=latest_a.generation,
                                pid=response["pid"],
                                validation_pending=True,
                                candidate_symbols=viewport.wind_symbols,
                                gui_publication_ms=viewport.last_publication_timing_ms,
                            )
                        )
                        loop.quit()
                        return
                    actual = viewport.quick.grabFramebuffer().convertToFormat(
                        QImage.Format.Format_RGBA8888
                    )
                    oracle = QImage(str(bundle[2] / "oracle.png")).convertToFormat(
                        QImage.Format.Format_RGBA8888
                    )
                    discarded = any(
                        event in ("discarded", "discarded-ready")
                        and generation == old_a.generation
                        for event, generation in scheduler.events
                    )
                    if (
                        actual.size() != oracle.size()
                        or oracle.isNull()
                        or bytes(actual.constBits()) != bytes(oracle.constBits())
                        or viewport.view != target_a
                        or viewport.coverage_miss
                        or not discarded
                        or response["generation"] != latest_a.generation
                        or response["pid"] != responses[0]["response"]["pid"]
                        or (
                            service.directory
                            / f"response-{intermediate_b.generation}.json"
                        ).exists()
                    ):
                        raise ValueError(
                            "A-B-A publication/freshness or pixel gate failed"
                        )
                    published.append(
                        dict(
                            generation=latest_a.generation,
                            pid=response["pid"],
                            complete_pixels_exact=True,
                            reference_symbols=viewport.wind_symbols,
                        )
                    )
                    loop.quit()
                except Exception as exc:
                    freshness_fail(str(exc))

            def freshness_tick():
                nonlocal latest_a, intermediate_b, replaced
                try:
                    freshness_ticks.append(time.perf_counter() - began)
                    marker = service.directory / "active.json"
                    active = json.loads(marker.read_text()) if marker.exists() else None
                    if (
                        not replaced
                        and active
                        and active["generation"] == old_a.generation
                    ):
                        intermediate_b = scheduler.request(
                            (json.dumps(target_b.__dict__, sort_keys=True),)
                        )
                        show_requested(target_b, intermediate_b.generation)
                        latest_a = scheduler.request(
                            (json.dumps(target_a.__dict__, sort_keys=True),)
                        )
                        show_requested(target_a, latest_a.generation)
                        replaced = True
                    if replaced:
                        result = scheduler.take()
                        if result:
                            request, value, error = result
                            if error:
                                raise RuntimeError(error)
                            if request != latest_a:
                                raise ValueError(
                                    "Obsolete completion attempted native publication"
                                )
                            bundle, response = value
                            if response["generation"] != latest_a.generation:
                                raise ValueError(
                                    "Producer generation differs from requested generation"
                                )
                            viewport.install_bundle(bundle)
                            QTimer.singleShot(
                                100, lambda: verify_latest(bundle, response)
                            )
                except Exception as exc:
                    freshness_fail(str(exc))

            pulse.timeout.connect(freshness_tick)
            watchdog.timeout.connect(
                lambda: freshness_fail("A-B-A freshness probe timed out")
            )
            try:
                if performance_mode:
                    readback_state["active"] = True
                    viewport.quick.quickWindow().afterRendering.connect(
                        freshness_rendered, Qt.ConnectionType.DirectConnection
                    )
                pulse.start()
                watchdog.start(40000)
                loop.exec()
            finally:
                pulse.stop()
                watchdog.stop()
                if performance_mode:
                    viewport.quick.quickWindow().afterRendering.disconnect(
                        freshness_rendered
                    )
                    readback_state["active"] = False
            freshness = dict(
                old_a=old_a.generation,
                intermediate_b=intermediate_b.generation if intermediate_b else None,
                latest_a=latest_a.generation,
                published=published,
                failures=failures,
                transitions=transitions,
                gui_tick_seconds=freshness_ticks,
                gui_tick_gap_ms=tick_gap_summary(freshness_ticks),
                scheduler_events=list(scheduler.events),
                after_rendering_seconds=freshness_render_signals,
            )
            atomic_json(work / "freshness-diagnostic.json", freshness)
            if failures or not published:
                raise RuntimeError("; ".join(failures) or "A-B-A did not publish")
            if performance_mode:
                # All measured input/heartbeat loops are stopped. Oracle work,
                # GPU readbacks and later reconstruction are deliberately outside.
                validation_began = time.perf_counter()
                validation = service.validate(
                    [1, 2, old_a.generation, latest_a.generation]
                )
                post_checks = []
                for record in validation["completed"]:
                    generation = record["generation"]
                    timed_directory = service.directory / f"generation-{generation}"
                    candidate = read_bundle(timed_directory)
                    reference = read_bundle(Path(record["directory"]))
                    cm, ca, _ = candidate
                    rm, ra, rd = reference
                    assert_same_prepared_assets(candidate, reference)
                    viewport.view = BarbView(**cm["view"])
                    viewport.install_bundle(prepare_gui_bundle(candidate))
                    app.processEvents()
                    actual = viewport.quick.grabFramebuffer().convertToFormat(
                        QImage.Format.Format_RGBA8888
                    )
                    oracle = QImage(str(rd / "oracle.png")).convertToFormat(
                        QImage.Format.Format_RGBA8888
                    )
                    if (
                        oracle.isNull()
                        or actual.size() != oracle.size()
                        or bytes(actual.constBits()) != bytes(oracle.constBits())
                        or viewport.wind_symbols
                        != rm["reference_checks"][0][0]["symbols"]
                    ):
                        raise ValueError(
                            f"Deferred scene/count mismatch: generation {generation}"
                        )
                    if (
                        viewport.wind_symbols == 0
                        and not viewport.wind_status.isVisible()
                    ):
                        raise ValueError("Deferred reference-empty badge missing")
                    post_checks.append(
                        dict(
                            generation=generation,
                            unchanged_assets=True,
                            scene_pixels_exact=True,
                            fidelity=record["fidelity"],
                            oracle_semantics=(
                                rm.get("empty_layer_diagnostic") or {}
                            ).get("oracle_semantics", "unchanged-reference-gpu-scene"),
                        )
                    )
                validation = dict(
                    records=post_checks, seconds=time.perf_counter() - validation_began
                )
            else:
                validation = None
        finally:
            readback_state["active"] = False
            if performance_mode:
                viewport.quick.grabFramebuffer = original_grab
            service.close()
            scheduler.close()
        run_protocol.require_clean(service.status)
        service_result = json.loads(
            (service.directory / "service-result.json").read_text()
        )
        if len({item["response"]["pid"] for item in responses}) != 1:
            raise ValueError("Preparation producer was restarted between requests")
        return dict(
            protocol=(
                "independent-persistent-coverage-a0h-v1"
                if performance_mode
                else "independent-persistent-coverage-a0g-v4-detailed-attribution"
            ),
            snapshot_sha256=metadata["snapshot_sha256"],
            producer_status=service.status,
            producer_ready_seconds=service.ready_seconds,
            requests=responses,
            diagnostics=diagnostics,
            scheduler_events=scheduler.events,
            freshness=freshness,
            data_policy=preparation.get("data_policy", "legacy"),
            post_window_validation=validation,
            measured_window_has_oracle_or_readback=False if performance_mode else True,
            explicit_readback_guard=readback_state if performance_mode else None,
            data_worker=service_result.get("data_worker"),
            initial_view=initial.__dict__,
            clocks=metadata["clocks"],
            host=dict(
                qt_platform=app.platformName(),
                dpr=viewport.devicePixelRatioF(),
                backend=viewport.quick.quickWindow()
                .rendererInterface()
                .graphicsApi()
                .name,
            ),
            presentation_measured=False,
            paired_speedup_proven=False,
            loading_has_visible_partial_map=True,
            loading_has_current_complete_weather=False,
        )

    viewport = None
    try:
        viewport = Viewport(prepared_sets, clocks, scenario, coverage_step_px)
        viewport.show()
        deadline = time.perf_counter() + 15
        while not viewport.weather.property("ready"):
            app.processEvents()
            viewport.quick.update()
            if time.perf_counter() > deadline:
                raise RuntimeError("Independent shader did not become ready")
            time.sleep(0.001)
        app.processEvents()
        observed = viewport.quick.grabFramebuffer()
        expected = QImage(str(directory / "oracle.png"))
        actual = observed.convertToFormat(QImage.Format.Format_RGBA8888)
        oracle = expected.convertToFormat(QImage.Format.Format_RGBA8888)
        pixels = np.frombuffer(actual.constBits(), dtype=np.uint8).reshape(
            actual.height(), actual.width(), 4
        )
        if len(np.unique(pixels.reshape(-1, 4), axis=0)) < 16:
            raise ValueError("Independent composition is blank/degenerate")
        if actual.size() != oracle.size() or bytes(actual.constBits()) != bytes(
            oracle.constBits()
        ):
            actual.save(str(directory / "candidate-composition-diagnostic.png"))
            diagnostic = dict(
                actual_size=[actual.width(), actual.height()],
                oracle_size=[oracle.width(), oracle.height()],
                fidelity_passed=False,
            )
            if actual.size() == oracle.size():
                expected_pixels = np.frombuffer(
                    oracle.constBits(), dtype=np.uint8
                ).reshape(pixels.shape)
                changed = np.any(pixels != expected_pixels, axis=-1)
                yy, xx = np.where(changed)
                diagnostic.update(
                    changed_pixels=int(changed.sum()),
                    max_channel_error=int(
                        np.max(np.abs(pixels.astype(int) - expected_pixels.astype(int)))
                    ),
                    bbox=(
                        [int(xx.min()), int(yy.min()), int(xx.max()), int(yy.max())]
                        if len(xx)
                        else None
                    ),
                )
            (directory / "composition-diagnostic.json").write_text(
                json.dumps(diagnostic, indent=2) + "\n"
            )
            raise ValueError("Initial complete composition differs from reference")
        if viewport.devicePixelRatioF() != metadata["dpr"]:
            raise ValueError("Independent host DPR differs")
        if scenario == "coverage_load":
            return loading_probe(viewport)
        if scenario in ("coverage_stream", "coverage_perf"):
            return persistent_probe(viewport)
        coverage_diagnostic = None
        if scenario == "coverage_miss":
            # Correctness readbacks only, never input-phase timing endpoints.
            viewport.view = pan(initial, -12 * coverage_step_px, 0)
            viewport.update_view(record=False)
            app.processEvents()
            missing = viewport.quick.grabFramebuffer().convertToFormat(
                QImage.Format.Format_RGBA8888
            )
            hidden = all(
                not item.isVisible()
                for item in (viewport.background, viewport.navigation, viewport.weather)
            )
            corners = [
                missing.pixelColor(x, y).name()
                for x, y in (
                    (0, 0),
                    (missing.width() - 1, missing.height() - 1),
                )
            ]
            missing_pixels = np.frombuffer(missing.constBits(), dtype=np.uint8).reshape(
                -1, 4
            )
            if (
                not viewport.coverage_miss
                or not hidden
                or not viewport.unavailable.isVisible()
                or corners != ["#202830", "#202830"]
                or len(np.unique(missing_pixels, axis=0)) < 2
            ):
                raise ValueError("Unavailable composition failed visibility/pixel gate")
            viewport.view = initial
            viewport.update_view(record=False)
            app.processEvents()
            recovered = viewport.quick.grabFramebuffer().convertToFormat(
                QImage.Format.Format_RGBA8888
            )
            if (
                viewport.coverage_miss
                or viewport.unavailable.isVisible()
                or recovered.size() != oracle.size()
                or bytes(recovered.constBits()) != bytes(oracle.constBits())
            ):
                raise ValueError("Returned resident composition differs from reference")
            coverage_diagnostic = dict(
                unavailable_pixels_verified=True,
                stale_layers_hidden=True,
                resident_return_pixels_exact=True,
                new_area_post_load_verified=False,
            )
            viewport.coverage_miss_steps.clear()
        priming = prime_quick(viewport.quick, prime_ms) if prime_ms else None
        host = dict(
            qt_platform=app.platformName(),
            dpr=viewport.devicePixelRatioF(),
            backend=viewport.quick.quickWindow().rendererInterface().graphicsApi().name,
            samples=viewport.quick.format().samples(),
            alpha_bits=viewport.quick.format().alphaBufferSize(),
        )
        quick = viewport.quick.quickWindow()
        quick.beforeSynchronizing.connect(
            recorder.synchronize, Qt.ConnectionType.DirectConnection
        )

        def rendered():
            recorder.rendered(time.perf_counter())

        quick.afterRendering.connect(rendered, Qt.ConnectionType.DirectConnection)
        loop = QEventLoop()
        timer, drain = QTimer(), QTimer()
        timer.setSingleShot(True)
        timer.setTimerType(Qt.TimerType.PreciseTimer)
        drain.setSingleShot(True)
        drain.timeout.connect(loop.quit)
        began, index = time.perf_counter(), 0
        selection_ms.clear()
        counters_before = dict(counts)

        def dispatch():
            nonlocal index
            try:
                viewport.revision = recorder.dispatch(
                    began + index * 0.016667,
                    time.perf_counter(),
                    state(
                        viewport.view,
                        datetime.fromtimestamp(
                            clocks[
                                0 if scenario == "coverage_miss" else trajectory[index]
                            ]
                        ).isoformat(),
                    ),
                )
                if scenario == "coverage_miss":
                    # Coverage miss: pan the camera, hold forecast time fixed.
                    dx, dy = viewport.pan_trajectory[viewport.pan_index]
                    viewport.view = pan(viewport.view, dx, dy)
                    viewport.pan_index += 1
                else:
                    # Time scrub: change forecast time, do not pan the camera.
                    viewport.time_index = trajectory[index]
                viewport.update_view()
                if errors:
                    raise RuntimeError(errors[-1])
                index += 1
                if index == 24:
                    drain.start(2000)
                else:
                    timer.start(
                        max(
                            0,
                            round(
                                (began + index * 0.016667 - time.perf_counter()) * 1000
                            ),
                        )
                    )
            except Exception as exc:
                errors.append(str(exc))
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
        if errors:
            raise RuntimeError("; ".join(errors))
        if scenario == "coverage_miss" and (
            not viewport.coverage_miss_steps or viewport.coverage_miss
        ):
            raise ValueError("Coverage replay must miss and return to ready")
        if scenario == "coverage_miss":
            # End-of-replay fidelity is separate from callback timing.
            returned = viewport.quick.grabFramebuffer().convertToFormat(
                QImage.Format.Format_RGBA8888
            )
            if returned.size() != oracle.size() or bytes(returned.constBits()) != bytes(
                oracle.constBits()
            ):
                raise ValueError(
                    "Post-replay resident composition differs from reference"
                )
            coverage_diagnostic["post_replay_return_pixels_exact"] = True
        probe = recorder.result()
        probe["gpu_counter_delta"] = {
            key: value - counters_before[key] for key, value in counts.items()
        }
        return dict(
            protocol=(
                "independent-vertical-coverage-miss-a0-v1"
                if scenario == "coverage_miss"
                else "independent-vertical-scrub-a0-v1"
            ),
            snapshot_sha256=metadata["snapshot_sha256"],
            native_render_probe=probe,
            static_complete_pixels_exact=True,
            nonblank_guard_passed=True,
            independent_host=True,
            legacy_frame_assembly_calls=0,
            preparation_available_during_replay=False,
            gui_selection_ms=startup.protocol.summary(selection_ms),
            dpr=viewport.devicePixelRatioF(),
            field_image_requests=viewport.field_provider.requests,
            selection_image_requests=viewport.selection_provider.requests,
            background_includes_cpu_weather=True,
            cold_miss_scheduler_tested=False,
            presentation_measured=False,
            end_to_end_speedup_proven=False,
            priming=priming,
            host=host,
            scenario=scenario,
            coverage_miss_steps=viewport.coverage_miss_steps,
            coverage_miss_count=len(viewport.coverage_miss_steps),
            coverage_diagnostic=coverage_diagnostic,
            time_steps=len(prepared_sets),
            scrub_trajectory=trajectory if scenario != "coverage_miss" else None,
        )
    finally:
        if viewport is not None:
            viewport.hide()
            viewport.quick.setSource(QUrl())
            viewport.field_provider.clear()
            viewport.selection_provider.clear()
            viewport.close()
            viewport.deleteLater()
            app.processEvents()
        gdal.Open, gdal.OpenEx = old_open, old_open_ex


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--candidate", action="store_true")
    parser.add_argument("--prime-ms", type=int, default=0)
    parser.add_argument("--prepare-view", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--preparation-directory", type=Path)
    parser.add_argument("--service-directory", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--data-policy", choices=("legacy", "reopen", "resident"), default="legacy"
    )
    parser.add_argument(
        "--scrub-interval-seconds",
        type=int,
        default=1,
        help="Seconds between forecast-time steps for timeline scrub export",
    )
    parser.add_argument(
        "--scrub-steps",
        type=int,
        default=12,
        help="Number of forecast-time steps (forward) for timeline scrub export; total inputs = 2 * steps",
    )
    parser.add_argument(
        "--scenario",
        type=str,
        default="scrub_return",
        help="Replay scenario: scrub_return (time scrub) or coverage_miss (pan beyond coverage)",
    )
    parser.add_argument(
        "--coverage-step-px",
        type=int,
        default=48,
        help="Screen pixels per pan step for coverage-miss scenario",
    )
    args = parser.parse_args()
    if args.worker:
        config = json.loads(args.worker.read_text())
        result = export_worker(config, args.resources)
    elif args.candidate:
        if args.scenario in (
            "coverage_load",
            "coverage_stream",
            "coverage_perf",
        ) and not all((args.snapshot, args.preparation_directory)):
            parser.error(
                "coverage_load requires --snapshot and --preparation-directory"
            )
        with tempfile.TemporaryDirectory(prefix="gwr-vertical-shaders-") as temporary:
            previous.shader_sources(args.app_root.resolve(), Path(temporary))
            result = run_candidate(
                args.resources,
                Path(temporary),
                args.prime_ms,
                scenario=args.scenario,
                coverage_step_px=args.coverage_step_px,
                preparation=(
                    dict(
                        directory=args.preparation_directory,
                        snapshot=args.snapshot.resolve(),
                        app_root=args.app_root.resolve(),
                        data_policy=args.data_policy,
                    )
                    if args.scenario
                    in ("coverage_load", "coverage_stream", "coverage_perf")
                    else None
                ),
            )
    else:
        if not args.snapshot:
            parser.error("--snapshot required for export/reference")
        if args.resources.exists():
            raise ValueError("Refusing to overwrite resource export")
        args.resources.mkdir(parents=True)
        manifest = startup.verify_snapshot(args.snapshot)
        with tempfile.TemporaryDirectory(prefix="gwr-vertical-reference-") as temporary:
            root = Path(temporary)
            startup.prepare_sandbox(args.snapshot, root)
            initial_stamp = manifest.get("forecast_time")
            if initial_stamp is None:
                raise ValueError("Snapshot manifest missing forecast_time")
            if isinstance(initial_stamp, str):
                initial_stamp = datetime.fromisoformat(initial_stamp).timestamp()
            stamps = [
                initial_stamp + k * args.scrub_interval_seconds
                for k in range(args.scrub_steps)
            ]
            config = dict(
                snapshot_sha256=manifest["content_sha256"],
                forecast_time=manifest["forecast_time"],
                scenario=args.scenario,
                frames=1 if args.prepare_view else 24,
                repeats=1,
                warmup_frames=2,
                interval_ms=16.667,
                trackpad_step_px=8,
                scale=1,
                native_drain_timeout_s=2,
                measurement_mode="native-render",
                settle_timeout_s=60,
                map_renderer="gpu",
                prime_ms=args.prime_ms,
                scrub_stamps=stamps,
                prepare_view=(
                    json.loads(args.prepare_view.read_text())
                    if args.prepare_view
                    else None
                ),
                service_directory=(
                    str(args.service_directory.resolve())
                    if args.service_directory
                    else None
                ),
                data_policy=args.data_policy,
            )
            configuration = root / "config.json"
            configuration.write_text(json.dumps(config))
            output = root / "result.json"
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
                    str(configuration),
                    "--app-root",
                    str(args.app_root.resolve()),
                    "--resources",
                    str(args.resources.resolve()),
                    "--output",
                    str(output),
                ],
                cwd=args.app_root,
                env=env,
                timeout=110,
                check=True,
            )
            result = json.loads(output.read_text())
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
