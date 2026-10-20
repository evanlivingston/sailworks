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


def _readback(images, blend, positions):
    """The GPU's interpolated wind speed at the given texture positions.

    The application's own weather shader is compiled with the #71 probe's
    byte-exact readback, so the float32 the shader wrote can be read back.
    The readback is a fidelity measurement; it is not a timing measurement.
    """
    import numpy as np
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QImage
    from PySide6.QtQuick import QQuickImageProvider

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
    sampled = {}
    served = {
        "u0": images[0],
        "v0": images[1],
        "u1": images[2],
        "v1": images[3],
    }

    class Provider(QQuickImageProvider):
        def __init__(self):
            super().__init__(QQuickImageProvider.ImageType.Image)
            self.requests = 0

        def requestImage(self, identifier, size, requested_size):  # noqa: N802
            self.requests += 1
            image = QImage(served[identifier])
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
        for position in positions:
            probe.root.setProperty("uPointX", float(position[0]))
            probe.root.setProperty("uPointY", float(position[1]))
            low = probe.render(-2)
            high = probe.render(-3)
            values = tool.reconstruct_float(low, high)
            alpha = low[..., 3]
            centre = values[values.shape[0] // 2, values.shape[1] // 2]
            sampled[f"{position[0]:.6f},{position[1]:.6f}"] = {
                "speed": float(centre),
                "missing": bool(alpha[alpha.shape[0] // 2, alpha.shape[1] // 2] == 0),
            }
    finally:
        probe.quick.close()
    return sampled


def _speed_comparison(images, blend, positions, reference):
    """The fill's interpolated wind speed against the CPU reference.

    The reference is called with a texture position and the payload's planes,
    so the same comparison serves the payload's arithmetic and the
    geographic placement.
    """
    import numpy as np

    from gweatherrouting.qt.gpuweather import decode_component

    outcome = {"limit": NUMERICAL_LIMIT}
    planes = [decode_component(image) for image in images]
    sampled = _readback(images, blend, positions)
    errors = []
    for position in positions:
        key = f"{position[0]:.6f},{position[1]:.6f}"
        observed = sampled[key]
        expected = reference(position, planes)
        value = float(np.hypot(expected[0], expected[1]))
        sampled[key].update({"cpu_speed": value})
        if observed["missing"] or not np.isfinite(value):
            continue
        errors.append(abs(observed["speed"] - value))
    outcome["positions"] = sampled
    outcome["compared_positions"] = len(errors)
    outcome["max_abs_error"] = float(max(errors)) if errors else None
    outcome["passed"] = bool(errors and max(errors) <= NUMERICAL_LIMIT)
    return outcome


def _lattice_reference(lattice, planes, blend, lat, lon):
    """The CPU's interpolation of the payload at a geographic position.

    The cell and the weights come from the lattice's own geography, so this
    is the CPU reference *at the position*, not at a texture coordinate.
    """
    import numpy as np

    fx, fy = lattice.cells(np.array([lon]), np.array([lat]))
    index_x = int(np.clip(np.floor(fx[0]), 0, lattice.cols - 1))
    index_y = int(np.clip(np.floor(fy[0]), 0, lattice.rows - 1))
    delta_x = float(fx[0] - index_x)
    delta_y = float(fy[0] - index_y)
    next_x = min(index_x + 1, lattice.cols - 1)
    next_y = min(index_y + 1, lattice.rows - 1)
    u0, v0, u1, v1 = planes
    total = np.zeros(2, dtype=np.float32)
    corners = (
        (index_x, index_y, (1 - delta_x) * (1 - delta_y)),
        (next_x, index_y, delta_x * (1 - delta_y)),
        (index_x, next_y, (1 - delta_x) * delta_y),
        (next_x, next_y, delta_x * delta_y),
    )
    for cell_x, cell_y, weight in corners:
        first = np.array([u0[cell_y, cell_x], v0[cell_y, cell_x]], dtype=np.float32)
        second = np.array([u1[cell_y, cell_x], v1[cell_y, cell_x]], dtype=np.float32)
        if blend <= 0.0:
            value = first
        elif blend >= 1.0:
            value = second
        else:
            value = first + (second - first) * np.float32(blend)
        total = (total + value * np.float32(weight)).astype(np.float32)
    return total


def _lattice_probes(lattice, view):
    """Probe positions in the lattice's own cell coordinates.

    The positions are inside a cell, near a cell edge and at the lattice's
    border, so the comparison covers the interpolation's interior, its edge
    and its border.
    """
    import numpy as np

    fx, fy = lattice.cells(np.array([view._center_lon]), np.array([view._center_lat]))
    base_x, base_y = float(fx[0]), float(fy[0])
    return (
        ("inside_cell", base_x + 0.3, base_y + 0.3),
        ("inside_cell_2", base_x + 0.7, base_y + 0.7),
        ("near_cell_edge", base_x + 0.98, base_y + 0.02),
        ("lattice_edge", 0.5, float(lattice.rows - 1) + 0.5),
    )


def _placement_observations(field, camera, blend):
    """The fill's placement against the CPU reference.

    The screen position of a probe is projected into the viewport's
    projection, and the viewport's own rectangle for the lattice maps it to
    a texture position.  The GPU's value there is compared with the CPU
    reference at the *geographic* position, so a rectangle that did not
    project the lattice onto the map would fail the comparison.
    """
    import numpy as np

    from gweatherrouting.qt.gribviewport import _grid_rect
    from gweatherrouting.qt.gpuweather import decode_component
    from gweatherrouting.rendering.weathergrid import mercator_lat

    lattice = field.grid.lattice
    view = camera.view()
    rect = _grid_rect(view, lattice)
    planes = [decode_component(image) for image in field.grid.images]
    positions = []
    for name, cell_x, cell_y in _lattice_probes(lattice, view):
        lon = lattice.origin_lon + (cell_x + 0.5) * lattice.spacing
        merc = lattice.origin_merc - (cell_y + 0.5) * lattice.spacing
        lat = float(mercator_lat(merc))
        screen_x = (
            view._lon_to_x(lon)
            - view._lon_to_x(view._center_lon)
            + view.get_allocated_width() / 2
        )
        screen_y = (
            view._lat_to_y(lat)
            - view._lat_to_y(view._center_lat)
            + view.get_allocated_height() / 2
        )
        texture = (
            (screen_x - rect.x()) / rect.width(),
            (screen_y - rect.y()) / rect.height(),
        )
        positions.append((name, lat, lon, texture))
    sampled = _readback(field.grid.images, blend, [item[3] for item in positions])
    outcome = {
        "limit": NUMERICAL_LIMIT,
        "rect": [rect.x(), rect.y(), rect.width(), rect.height()],
        "positions": {},
    }
    errors = []
    payload_errors = []
    for name, lat, lon, texture in positions:
        key = f"{texture[0]:.6f},{texture[1]:.6f}"
        observed = sampled[key]
        expected = _lattice_reference(lattice, planes, blend, lat, lon)
        value = float(np.hypot(expected[0], expected[1]))
        mirror = _vector_at(*planes, blend, texture)
        outcome["positions"][name] = {
            "lat": lat,
            "lon": lon,
            "texture": [texture[0], texture[1]],
            "gpu_speed": observed["speed"],
            "cpu_speed": value,
            "missing": observed["missing"],
        }
        if observed["missing"] or not np.isfinite(value):
            continue
        errors.append(abs(observed["speed"] - value))
        payload_errors.append(
            abs(observed["speed"] - float(np.hypot(mirror[0], mirror[1])))
        )
    outcome["compared_positions"] = len(errors)
    outcome["max_abs_error"] = float(max(errors)) if errors else None
    outcome["passed"] = bool(errors and max(errors) <= NUMERICAL_LIMIT)
    outcome["payload_max_abs_error"] = (
        float(max(payload_errors)) if payload_errors else None
    )
    return outcome


def fill_observations(widget, model, camera, pump):
    """The fill layer's pixel, mask, value, placement and independence evidence."""
    import numpy as np
    from PySide6.QtGui import QColor

    from gweatherrouting.qt.gpuweather import encode_component

    out = {"enabled": widget._fill is not None}
    if widget._fill is None:
        return out
    field = model._field
    blend = float(field.staged.weight(camera.stamp()))
    grid = field.grid
    out["grid"] = {
        "ready": grid is not None,
        "origin_lon": None if grid is None else grid.lattice.origin_lon,
        "origin_mercator": None if grid is None else grid.lattice.origin_merc,
        "spacing": None if grid is None else grid.lattice.spacing,
        "columns": None if grid is None else grid.lattice.cols,
        "rows": None if grid is None else grid.lattice.rows,
        "nodes": None if grid is None else grid.lattice.node_count,
        "bytes": None if grid is None else grid.nbytes,
    }
    if grid is None:
        return out
    images = grid.images

    # The fill is measured alone against a transparent background, so the
    # alpha it contributes is observable: the A0k rule is about that alpha.
    # The chart layer is hidden for the same reason: it is not the fill's
    # alpha.
    widget.setClearColor(QColor(0, 0, 0, 0))
    widget._barbs.setVisible(False)
    if widget._chart is not None:
        widget._chart.setVisible(False)
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
    widget.publish_fill(images, f"{field.generation}/grid")
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
    widget.publish_fill(images, f"{field.generation}/grid")
    pump(250)
    pixels = _grab(widget)
    out["resident_painted_pixels"] = int((pixels[..., 3] > 0).sum())
    out["values"] = _speed_comparison(
        images,
        blend,
        PROBE_POSITIONS,
        lambda position, planes: _vector_at(*planes, blend, position),
    )
    # The placement: the GPU's value at a screen position against the CPU
    # reference at that position's geography.
    out["placement"] = _placement_observations(field, camera, blend)
    out["missing_neighbour"] = _missing_neighbour_observations(
        widget, field, camera, blend, pump
    )
    if widget._chart is not None:
        widget._chart.setVisible(True)
    out["counters"] = dict(widget.counters)
    return out


def _missing_neighbour_observations(widget, field, camera, blend, pump):
    """A missing node is never filled from a neighbour.

    The gridded payload with one missing node is published.  The cell that
    touches the node must read back as missing -- the shader's missing rule
    contributes exactly zero alpha and never a neighbour's value -- and the
    cells away from it must still be painted.
    """
    import numpy as np

    from gweatherrouting.qt.gpuweather import decode_component, encode_component
    from gweatherrouting.qt.gribviewport import _grid_rect
    from gweatherrouting.rendering.weathergrid import mercator_lat

    lattice = field.grid.lattice
    # The widget's *current* camera: the independence observation above moved
    # it, so the passed-in camera no longer describes what is on screen.
    view = widget._camera.view()
    rect = _grid_rect(view, lattice)
    planes = [decode_component(image) for image in field.grid.images]
    # The node nearest the viewport's centre: the four cells that touch it
    # must lose every fragment.
    fx, fy = lattice.cells(np.array([view._center_lon]), np.array([view._center_lat]))
    index_x = int(np.clip(np.floor(fx[0]), 0, lattice.cols - 1))
    index_y = int(np.clip(np.floor(fy[0]), 0, lattice.rows - 1))
    original = float(planes[0][index_y, index_x])
    planes = list(planes)
    planes[0] = planes[0].copy()
    planes[0][index_y, index_x] = np.nan
    images = tuple(encode_component(plane) for plane in planes)
    widget._barbs.setVisible(False)
    widget.publish_fill(images, "probe-missing-neighbour")
    pump(250)

    # The byte-exact readback of the cells that touch the missing node and of
    # a cell away from it.
    lon = lattice.origin_lon + (index_x + 0.5) * lattice.spacing
    merc = lattice.origin_merc - (index_y + 0.5) * lattice.spacing
    lat = float(mercator_lat(merc))
    screen_x = (
        view._lon_to_x(lon)
        - view._lon_to_x(view._center_lon)
        + view.get_allocated_width() / 2
    )
    screen_y = (
        view._lat_to_y(lat)
        - view._lat_to_y(view._center_lat)
        + view.get_allocated_height() / 2
    )
    span_x = (rect.width() / lattice.cols) if lattice.cols else 1.0
    span_y = (rect.height() / lattice.rows) if lattice.rows else 1.0
    touching = (
        (screen_x + span_x / 2 - rect.x()) / rect.width(),
        (screen_y + span_y / 2 - rect.y()) / rect.height(),
    )
    away = (
        (screen_x - span_x * 2 - rect.x()) / rect.width(),
        (screen_y - span_y * 2 - rect.y()) / rect.height(),
    )
    readback = _readback(images, blend, [touching, away])
    keys = list(readback)
    pixels = _grab(widget)
    alpha = pixels[..., 3]
    # The framebuffer is in device pixels and the view's coordinates are in
    # logical pixels: the neighbourhood is measured in the framebuffer's own
    # scale, or it would be read at the wrong place.
    scale = alpha.shape[1] / float(widget.width())
    node_x = screen_x * scale
    node_y = screen_y * scale
    cell_x = span_x * scale
    cell_y = span_y * scale
    near = alpha[
        max(0, int(round(node_y - cell_y / 2))) : int(round(node_y + cell_y / 2)) + 1,
        max(0, int(round(node_x - cell_x / 2))) : int(round(node_x + cell_x / 2)) + 1,
    ]
    far = alpha.copy()
    far[
        max(0, int(round(node_y - cell_y / 2))) : int(round(node_y + cell_y / 2)) + 1,
        max(0, int(round(node_x - cell_x / 2))) : int(round(node_x + cell_x / 2)) + 1,
    ] = 0
    # The hole the missing node leaves: the shader's 2x2 bilinear reads the
    # missing corner over two cells in each axis, and the hole is bounded by
    # the transparent pixels inside the lattice's rectangle.
    inside = alpha[
        max(0, int(round(rect.y() * scale))) : int(
            round((rect.y() + rect.height()) * scale)
        ),
        max(0, int(round(rect.x() * scale))) : int(
            round((rect.x() + rect.width()) * scale)
        ),
    ]
    hole = np.argwhere(inside == 0)
    hole_box = None
    if len(hole):
        hole_box = [
            int(hole[:, 0].min()),
            int(hole[:, 0].max()),
            int(hole[:, 1].min()),
            int(hole[:, 1].max()),
        ]
    outcome = {
        "missing_node": [lat, lon],
        "node_value_before": original,
        "node_value_was_finite": bool(np.isfinite(original)),
        "touching_cell_missing": bool(readback[keys[0]]["missing"]),
        "other_cell_missing": bool(readback[keys[1]]["missing"]),
        "node_cell_pixels": int(near.size),
        "node_cell_alpha_pixels": int((near > 0).sum()),
        "node_cell_significant_alpha_pixels": int((near > 25).sum()),
        "other_alpha_pixels": int((far > 0).sum()),
        "total_pixels": int(alpha.size),
        "framebuffer_scale": scale,
        "hole_pixels": int((inside == 0).sum()),
        "hole_box_device_pixels": hole_box,
        "rect": [rect.x(), rect.y(), rect.width(), rect.height()],
        "fill_blend": float(widget._fill.property("blend")),
        "fill_grid_width": float(widget._fill.property("gridWidth")),
        "fill_grid_x": float(widget._fill.property("gridX")),
        "fill_grid_y": float(widget._fill.property("gridY")),
    }
    widget._barbs.setVisible(True)
    return outcome


def _camera_state(widget):
    """The viewport's camera, forecast time and published weather."""
    camera = widget._camera
    return {
        "center_lon": None if camera is None else camera.center_lon,
        "center_lat": None if camera is None else camera.center_lat,
        "zoom": None if camera is None else camera.zoom,
        "time": None if camera is None else camera.time.isoformat(),
        "weather": widget._published,
    }


def chart_observations(widget, model, camera, pump, layer):
    """The chart layer's ordering and preservation evidence.

    The chart layer is an independent generation: the weather is presented
    before the chart is ready, and the camera, the forecast time and the
    weather survive the chart's publication.  The states are the ones the
    viewport recorded when it asked for the chart and when it published it,
    so the check spans the publication itself.
    """
    out = {"enabled": layer is not None}
    if layer is None:
        return out

    deadline = time.perf_counter() + 60
    while widget._chart_ready_at is None and time.perf_counter() < deadline:
        pump(100)
    out["weather_ready_at"] = widget._weather_ready_at
    out["chart_ready_at"] = widget._chart_ready_at
    out["weather_before_chart"] = bool(
        widget._weather_ready_at is not None
        and widget._chart_ready_at is not None
        and widget._weather_ready_at < widget._chart_ready_at
    )
    out["chart_requested_after_weather"] = widget._chart_requested_after_weather
    out["chart_state_while_composing"] = layer.state
    out["chart_loading_state_explicit"] = bool(widget._chart_loading_shown)
    before = widget._chart_request_state
    after = widget._chart_publish_state
    out["before"] = before
    out["after"] = after
    out["camera_preserved"] = bool(
        before
        and after
        and before["center_lon"] == after["center_lon"]
        and before["center_lat"] == after["center_lat"]
        and before["zoom"] == after["zoom"]
    )
    out["forecast_time_preserved"] = bool(
        before and after and before["time"] == after["time"]
    )
    out["weather_preserved"] = bool(
        before and after and before["weather"] == after["weather"]
    )
    out["layer"] = {
        key: value
        for key, value in layer.observations().items()
        if key not in ("spans",)
    }
    return out


def chart_supersession_observation(widget, model, camera, pump, layer):
    """The chart layer's cancellation evidence.

    A chart generation that is superseded while it is still composing may
    never publish: the layer replaces it with a newer generation and
    rejects the older one.
    """
    out = {"enabled": layer is not None}
    if layer is None:
        return out
    superseded_entered = Event()
    superseded_release = Event()
    real_compose = layer._compose

    def blocking(camera_, cancel):
        superseded_entered.set()
        superseded_release.wait(20)
        return real_compose(camera_, cancel)

    layer._compose = blocking
    try:
        # The supersession is provoked with a forecast-time change: the
        # chart's isobars belong to the forecast instant, so the new instant
        # is a new chart generation, and the weather pipeline's spatial
        # window is untouched.
        probe_camera = replace(camera, time=camera.time + timedelta(hours=1))
        widget.set_camera(probe_camera)
        deadline = time.perf_counter() + 20
        while not superseded_entered.is_set() and time.perf_counter() < deadline:
            pump(50)
        generation = layer.observations()["generation"]
        # A newer generation replaces the one that is still composing.
        layer.request(probe_camera, replace=True)
        superseded_release.set()
        deadline = time.perf_counter() + 20
        while (
            layer.observations()["state"] != "ready" and time.perf_counter() < deadline
        ):
            pump(100)
    finally:
        layer._compose = real_compose
    observations = layer.observations()
    out["superseded_generation"] = generation
    out["superseded_never_published"] = bool(
        generation in observations["rejected_generations"]
        and generation not in observations["published_generations"]
    )
    out["layer"] = {
        key: value for key, value in observations.items() if key not in ("spans",)
    }
    return out


# ---------------------------------------------------------------------------
# #70 — validation, soak, matrices and the rollout evidence
# ---------------------------------------------------------------------------

# The viewport's three declared retained budgets.  These are recorded values,
# not thresholds invented for this issue: the field store's ceiling is the
# 128 MiB decoded-band budget (gweatherrouting/qt/gribviewport.py
# FIELD_BYTE_LIMIT; docs/history/gpu-weather-residency.md), the sampling
# memo's is the 16 MiB memo limit (gweatherrouting/rendering/weatherstaging.py
# SAMPLING_MEMO_BYTES) and the staging provider's is the 32 MiB staging limit
# (gweatherrouting/qt/gpuweather.py WeatherImageProvider.byte_limit).
STORE_BUDGET_BYTES = 128 * 1024 * 1024
MEMO_BUDGET_BYTES = 16 * 1024 * 1024
PROVIDER_BUDGET_BYTES = 32 * 1024 * 1024

# The soak's plateau criterion, declared before the run: over the last three
# thirds of the curve the retained bytes of the field store agree within 2%
# of the last third's median, and no declared budget is exceeded at any
# point of the curve.  The 2% is this issue's own declared criterion; it is
# not derived from recorded evidence, so the gate reports the plateau check
# as unratified and it does not affect the verdict.
PLATEAU_RELATIVE_TOLERANCE = 0.02

# The camera the interaction gate and the soak share: the recorded viewport
# at the saved selection's own geography.
ANCHOR_LAT = 48.30887922260005
ANCHOR_LON = -123.09473088970692
ANCHOR_ZOOM = 9.837590787619122


def _selection(snapshot):
    """The saved ordered selection, the saved settings and the manifest.

    Read without creating a viewport: a process may hold only one viewport
    widget, so the measurements that need a different selection or camera
    read them here and open one session.
    """
    from gweatherrouting.qt.gribviewport import saved_selection

    fixture = _load_tool(
        "viewport_fixture", ROOT / "tools/benchmark_startup_rendering.py"
    )
    manifest = fixture.verify_snapshot(snapshot)
    paths, settings = saved_selection(snapshot / "data")
    return tuple(paths), settings, manifest


def _anchor_camera(manifest, settings, **overrides):
    """The recorded viewport camera at the saved selection's geography."""
    from gweatherrouting.qt.gribviewport import ViewportCamera

    camera = ViewportCamera(
        center_lat=ANCHOR_LAT,
        center_lon=ANCHOR_LON,
        zoom=ANCHOR_ZOOM,
        width=900,
        height=700,
        time=datetime.fromisoformat(manifest["forecast_time"]),
        density=settings.get("gribBarbDensity", "very_dense"),
        over_land=settings.get("gribArrowOnGround", False),
    )
    return replace(camera, **overrides) if overrides else camera


def _release_model(model):
    """Release the worker's GDAL handles while no other worker is running.

    The GDAL Python bindings are not safe for a dataset teardown that happens
    concurrently with another thread's dataset creation.  The interpreter's
    garbage collector does exactly that when a previous session's model is
    collected during the next session's work, so the handles are released
    explicitly, and collected, while the worker is joined.
    """
    import gc

    with model._condition:
        model._sources = ()
        model._sampler = None
        model._memo = None
        model._land = None
        model._land_mask = None
        model._bands = None
        model._store.clear()
    gc.collect()


def _release_mask(mask):
    """Release the shoreline reader's OGR dataset."""
    if mask is None:
        return
    mask._dataset = None
    for name in ("_prepared", "_tiles"):
        cache = getattr(mask, name, None)
        if cache is not None:
            cache.clear()
    contains = getattr(mask, "contains", None)
    if contains is not None and hasattr(contains, "cache_clear"):
        contains.cache_clear()


def _platform_identity(widget):
    """The host the measurement ran on, with the scene graph's own backend."""
    import os
    import platform

    import PySide6
    from PySide6.QtQuick import QSGRendererInterface

    quick = widget.quickWindow()
    backend = ""
    try:
        interface = quick.rendererInterface()
        api = interface.graphicsApi() if interface is not None else None
        if api is not None:
            backend = QSGRendererInterface.GraphicsApi(api).name
    except Exception:  # pragma: no cover - the identity is best effort
        backend = ""
    if not backend:
        backend = str(os.environ.get("QT_QUICK_BACKEND", "") or "")
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "qt": PySide6.__version__,
        "scene_graph_backend": backend,
        "requested_backend": os.environ.get("QT_QUICK_BACKEND", "") or "default",
        "device_pixel_ratio": float(quick.devicePixelRatio()),
        "logical_size": [widget.width(), widget.height()],
        "device_size": [
            int(quick.width() * quick.devicePixelRatio()),
            int(quick.height() * quick.devicePixelRatio()),
        ],
    }


def _layer_states(widget):
    """Every layer's GPU, loading and fallback state.

    The state is read from the layer's own QML object, so it is the layer's
    own state and not the viewport's guess.  No layer of the viewport has a
    per-layer CPU fallback: the retained CPU fallback is the application's
    CPU map renderer, which is proven separately.
    """
    counters = widget.publication_counters()
    state = widget._state
    status_visible = widget._status.isVisible()
    fill = widget._fill
    barbs = widget._barbs
    chart = widget._chart
    note = (
        "the viewport's layers have no per-layer CPU fallback; the retained "
        "CPU fallback is the application's CPU map renderer"
    )

    def shader_layer(item, publications, uploads):
        if item is None:
            return {
                "present": False,
                "gpu": False,
                "shader_ready": False,
                "images_ready": False,
                "shader_error": "",
                "loading_state": state,
                "loading_state_explicit": False,
                "loading_state_source": "",
                "fallback": "none",
                "fallback_active": False,
                "fallback_note": note,
                "publications": publications,
                "uploads": uploads,
            }
        ready = bool(item.property("ready"))
        images = bool(item.property("imagesReady"))
        return {
            "present": True,
            "gpu": ready,
            "shader_ready": ready,
            "images_ready": images,
            "shader_error": str(item.property("shaderError") or ""),
            "loading_state": state,
            "loading_state_explicit": bool(state == "ready" or status_visible),
            "loading_state_source": (
                "the viewport's weather state: the layer is published from the "
                "same resident field as the barbs"
            ),
            "fallback": "none",
            "fallback_active": False,
            "fallback_note": note,
            "publications": publications,
            "uploads": uploads,
        }

    out = {
        "fill": shader_layer(
            fill,
            counters.get("fill_publications", 0),
            counters.get("fill_uploads", 0),
        ),
        "barbs": shader_layer(
            barbs,
            counters.get("barbs_field_publications", 0),
            counters.get("barbs_field_uploads", 0),
        ),
    }
    chart_loading = False
    chart_ready = False
    if chart is not None:
        chart_loading = bool(chart.property("chartLoading"))
        chart_ready = bool(chart.property("ready"))
    out["chart"] = {
        "present": chart is not None,
        "gpu": chart_ready,
        "shader_ready": chart_ready,
        "images_ready": chart_ready,
        "shader_error": (
            "" if chart is None else str(chart.property("sourceError") or "")
        ),
        "loading_state": widget._chart_state,
        "loading_state_explicit": bool(
            chart is None or widget._chart_loading_shown or chart_ready
        ),
        "loading_state_source": (
            "the chart layer's own loading state; it never uses the weather's " "state"
        ),
        "fallback": "none",
        "fallback_active": False,
        "fallback_note": note,
        "publications": counters.get("chart_publications", 0),
        "uploads": counters.get("chart_publications", 0),
        "loading_flag": chart_loading,
    }
    out["viewport_state"] = state
    out["status_visible"] = bool(status_visible)
    return out


class _Session:
    """One viewport session: a fresh model, a fresh widget and a camera.

    Every session owns its worker thread, its field store, its sampling memo
    and its staging provider.  A repetition is therefore matched to the
    others in workload and fixture.  A session is also a whole process in
    this harness: the headless scene graph on this host compiles a viewport
    widget's shader only for the first widget of a process, so a second
    widget in the same process never presents.  One session per process is
    therefore the rule, and it is also what a matched repetition needs.
    """

    def __init__(
        self, snapshot, camera=None, paths=None, settings=None, fill=True, chart=True
    ):
        from gweatherrouting.qt.gribviewport import (
            GribViewportModel,
            ViewportCamera,
            _viewport_widget_class,
            saved_selection,
        )
        from gweatherrouting.rendering.windrenderer import WindLandMask

        fixture = _load_tool(
            "viewport_fixture", ROOT / "tools/benchmark_startup_rendering.py"
        )
        self.snapshot = snapshot
        self.manifest = fixture.verify_snapshot(snapshot)
        if paths is None:
            paths, settings = saved_selection(snapshot / "data")
        self.paths = tuple(paths)
        self.settings = settings or {}
        if camera is None:
            camera = _anchor_camera(self.manifest, self.settings)
        self.camera = camera
        self.model = GribViewportModel(
            self.paths,
            land_mask=WindLandMask(snapshot / "data"),
            resident_time_window=True,
            grid=True,
        )
        self.widget = _viewport_widget_class()(
            self.model,
            gpu_weather_fill=fill,
            chart_layer=chart,
            chart_data_dir=snapshot / "data",
        )
        self.widget.resize(camera.width, camera.height)
        self.widget.show()

    # ----------------------------------------------------------- helpers
    def settle(self, timeout=15):
        """Wait for the viewport to reach ``ready``."""
        from PySide6.QtCore import QEventLoop, QTimer

        loop = QEventLoop()
        poll = QTimer()
        stop = QTimer()
        stop.setSingleShot(True)
        stop.timeout.connect(loop.quit)
        poll.timeout.connect(
            lambda: (
                loop.quit() if self.widget._state in ("ready", "unavailable") else None
            )
        )
        stop.start(timeout * 1000)
        poll.start(10)
        loop.exec()
        poll.stop()
        stop.stop()
        return self.widget._state == "ready"

    def pump(self, milliseconds=150):
        from PySide6.QtCore import QEventLoop, QTimer

        loop = QEventLoop()
        QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()

    def wait_idle(self, timeout=5):
        deadline = time.perf_counter() + timeout
        while (
            self.model._active is not None or self.model._pending is not None
        ) and time.perf_counter() < deadline:
            self.pump(10)
        return self.model._active is None and self.model._pending is None

    def memory(self):
        """The three declared retained budgets, measured together."""
        store = self.model.field_statistics()
        memo = self.model.observations().get("sampling_memo", {})
        provider = self.widget._provider
        return {
            "field_store_bytes": store.get("current_bytes", 0),
            "field_store_peak_bytes": store.get("peak_bytes", 0),
            "field_store_entries": store.get("retained_entries", 0),
            "field_store_evictions": store.get("evictions", 0),
            "field_store_limit": self.model._store.byte_limit,
            "sampling_memo_bytes": memo.get("current_bytes", 0),
            "sampling_memo_peak_bytes": memo.get("peak_bytes", 0),
            "sampling_memo_limit": memo.get("limit_bytes", MEMO_BUDGET_BYTES),
            "provider_bytes": provider.field_bytes(),
            "provider_limit": provider.byte_limit,
        }

    def layers(self):
        """Every layer's GPU, loading and fallback state."""
        return _layer_states(self.widget)

    def close(self):
        widget = self.widget
        self.widget = None
        widget.close()
        model = self.model
        if model._thread is not None:
            model._thread.join(5)
        _release_mask(getattr(model, "_land_mask", None))
        _release_model(model)
        self.model = None


def _observe_demand(session, camera, settle_timeout=60):
    """Move to a camera and measure the honest-loading and recovery times.

    The measured endpoint is the viewport's own state: the time until the
    loading state is visible and the time until the viewport is ready again.
    It is **not** a presentation-to-screen latency and never a scanout time.
    """
    began = time.perf_counter()
    requested = session.widget.set_camera(camera)
    loading_visible_ms = None
    stale_hidden = None
    status_visible = None
    deadline = began + 10
    while time.perf_counter() < deadline:
        session.pump(10)
        if session.widget._state == "loading":
            if loading_visible_ms is None:
                loading_visible_ms = (time.perf_counter() - began) * 1000
                stale_hidden = not session.widget._weather.isVisible()
                status_visible = session.widget._status.isVisible()
                break
        elif session.widget._state in ("ready", "unavailable"):
            break
    recovered = session.settle(settle_timeout)
    recovery_ms = (time.perf_counter() - began) * 1000
    return {
        "requested_state": requested,
        "loading_visible_ms": loading_visible_ms,
        "status_visible": status_visible,
        "stale_weather_hidden": stale_hidden,
        "recovered": bool(recovered),
        "recovery_ms": recovery_ms,
        "final_state": session.widget._state,
        "error": session.model.error,
    }


# ---------------------------------------------------------------------------
# the measured sections; each one is a whole process
# ---------------------------------------------------------------------------


def section_interaction(snapshot, args):
    """The interaction gate's own session."""
    report = run_once(snapshot)
    report["section"] = "interaction"
    return report


def section_cold_load(snapshot, args):
    """The cold_load limit: a fresh session's first ready presentation."""
    session = _Session(snapshot)
    try:
        began = time.perf_counter()
        session.widget.set_camera(session.camera)
        ready = session.settle(60)
        return {
            "section": "cold_load",
            "ready": bool(ready),
            "ms": (time.perf_counter() - began) * 1000,
            "field_preparations": session.model.counters["field_preparations"],
            "memory": session.memory(),
            "note": (
                "a fresh session: no resident field, no warm store, one "
                "preparation and the first ready presentation"
            ),
        }
    finally:
        session.close()


def section_coverage_miss(snapshot, args):
    """The coverage_miss limit: leaving the resident field's window."""
    session = _Session(snapshot)
    try:
        session.widget.set_camera(session.camera)
        session.settle(60)
        attempts = []
        for offset in (1.5, 2.5, 4.0):
            far = replace(session.camera, center_lon=session.camera.center_lon + offset)
            attempt = _observe_demand(session, far)
            attempt["offset_degrees"] = offset
            attempts.append(attempt)
            if attempt["recovered"]:
                break
            session.widget.set_camera(session.camera)
            session.settle(60)
        return {
            "section": "coverage_miss",
            "attempts": attempts,
            "note": (
                "the camera leaves the resident field's window: the viewport "
                "must show the loading state and hide the stale weather, then "
                "recover"
            ),
        }
    finally:
        session.close()


def section_interval_crossing(snapshot, args):
    """The interval_crossing limit: leaving the resident time window."""
    session = _Session(snapshot)
    try:
        session.widget.set_camera(session.camera)
        session.settle(60)
        attempts = []
        for hours in (12, 24, 48):
            later = replace(
                session.camera, time=session.camera.time + timedelta(hours=hours)
            )
            attempt = _observe_demand(session, later)
            attempt["hours"] = hours
            attempts.append(attempt)
            if attempt["recovered"]:
                break
            session.widget.set_camera(session.camera)
            session.settle(60)
        return {
            "section": "interval_crossing",
            "attempts": attempts,
            "note": (
                "the forecast instant leaves the resident time window: a new "
                "field must be prepared, so this is a preparation limit and "
                "not a scrub limit"
            ),
        }
    finally:
        session.close()


def section_soak(snapshot, args):
    """The long-session soak: repeated travel and forecast-time changes."""
    session = _Session(snapshot)
    curve = []
    began = time.perf_counter()
    stopped = ""
    try:
        session.widget.set_camera(session.camera)
        session.settle(60)
        camera = session.camera
        for index in range(max(1, int(args.soak_steps))):
            if (
                args.soak_seconds is not None
                and time.perf_counter() - began > args.soak_seconds
            ):
                stopped = "time cap reached"
                break
            phase = index % 6
            if phase < 3:
                # Travel: a real pan, in the direction the cycle began.
                direction = 1 if (index // 6) % 2 == 0 else -1
                session.widget.pan_pixels(48 * direction, 0)
            elif phase == 3:
                # Return to the anchor's geography: this leaves the resident
                # window, so the field must be prepared again.
                session.widget.set_camera(camera)
            elif phase == 4:
                # A forecast-time change inside the resident window's reach.
                hours = (index // 6) % 9
                session.widget.set_camera(
                    replace(camera, time=camera.time + timedelta(hours=hours))
                )
            else:
                # A zoom change: the demand's resolution changes, so the
                # field's own form changes with it.
                session.widget.zoom_at(
                    0.05 if (index // 6) % 2 == 0 else -0.05, 300, 250
                )
            ready = session.settle(60)
            point = session.memory()
            point["step"] = index
            point["elapsed_s"] = time.perf_counter() - began
            point["ready"] = bool(ready)
            point["state"] = session.widget._state
            point["total_retained_bytes"] = (
                point["field_store_bytes"]
                + point["sampling_memo_bytes"]
                + point["provider_bytes"]
            )
            curve.append(point)
        else:
            stopped = "step count reached"
    finally:
        session.close()
    return {
        "section": "soak",
        "steps": len(curve),
        "stopped": stopped,
        "elapsed_s": curve[-1]["elapsed_s"] if curve else 0.0,
        "curve": curve,
        "analysis": plateau_analysis(curve),
        "criterion": {
            "statement": (
                "over the last three thirds of the curve the retained field "
                "store bytes agree within 2% of the last third's median, and "
                "no declared budget is exceeded at any point of the curve"
            ),
            "relative_tolerance": PLATEAU_RELATIVE_TOLERANCE,
            "ratified": False,
            "note": (
                "the 2% is this issue's own declared criterion, not a number "
                "derived from recorded evidence; the gate reports it as "
                "unratified"
            ),
        },
    }


def plateau_analysis(curve):
    """Apply the declared plateau criterion to the soak's curve."""
    if not curve:
        return {"plateau_reached": False, "reason": "the curve is empty"}
    values = [point["field_store_bytes"] for point in curve]
    totals = [point["total_retained_bytes"] for point in curve]
    third = max(1, len(values) // 3)
    windows = [values[:third], values[third : 2 * third], values[2 * third :]]
    windows = [window for window in windows if window]
    medians = [sorted(window)[len(window) // 2] for window in windows]
    last = medians[-1]
    spread = (max(medians) - min(medians)) / last if last else 0.0
    budgets = {
        "field_store_bytes": (
            max(point["field_store_bytes"] for point in curve),
            STORE_BUDGET_BYTES,
        ),
        "sampling_memo_bytes": (
            max(point["sampling_memo_bytes"] for point in curve),
            MEMO_BUDGET_BYTES,
        ),
        "provider_bytes": (
            max(point["provider_bytes"] for point in curve),
            PROVIDER_BUDGET_BYTES,
        ),
    }
    within_budget = all(value <= limit for value, limit in budgets.values())
    return {
        "plateau_reached": bool(spread <= PLATEAU_RELATIVE_TOLERANCE and within_budget),
        "window_medians": medians,
        "relative_spread": spread,
        "first_value": values[0],
        "last_value": values[-1],
        "peak_value": max(values),
        "first_total": totals[0],
        "last_total": totals[-1],
        "peak_total": max(totals),
        "budgets": {
            name: {"peak": value, "limit": limit, "within": value <= limit}
            for name, (value, limit) in budgets.items()
        },
        "within_budget": within_budget,
    }


def _matrix_overlapping_sources(snapshot, args):
    session = _Session(snapshot)
    try:
        session.widget.set_camera(session.camera)
        ready = session.settle(60)
        order = tuple(source.path for source in session.model._sources) == session.paths
        return {
            "sources": len(session.paths),
            "settled_ready": bool(ready),
            "source_order_preserved": bool(order),
            "field_preparations": session.model.counters["field_preparations"],
            "memory": session.memory(),
        }
    finally:
        session.close()


def _matrix_source_change(snapshot, args):
    paths, settings, manifest = _selection(snapshot)
    subset = paths[:4]
    session = _Session(
        snapshot,
        camera=_anchor_camera(manifest, settings),
        paths=subset,
        settings=settings,
    )
    try:
        session.widget.set_camera(session.camera)
        ready = session.settle(60)
        order = tuple(source.path for source in session.model._sources) == subset
        return {
            "sources": len(subset),
            "settled_ready": bool(ready),
            "source_order_preserved": bool(order),
            "field_preparations": session.model.counters["field_preparations"],
            "memory": session.memory(),
        }
    finally:
        session.close()


def _matrix_missing_fields(snapshot, args):
    session = _Session(snapshot)
    try:
        session.widget.set_camera(session.camera)
        session.settle(60)
        before = session.widget._published
        away = replace(session.camera, center_lat=session.camera.center_lat + 40.0)
        attempt = _observe_demand(session, away, settle_timeout=20)
        return {
            "attempt": attempt,
            "published_before": before,
            "published_after": session.widget._published,
            "republished": before != session.widget._published,
            "field_preparations": session.model.counters["field_preparations"],
        }
    finally:
        session.close()


def _matrix_shoreline_mask(snapshot, args):
    paths, settings, manifest = _selection(snapshot)
    session = _Session(
        snapshot,
        camera=_anchor_camera(manifest, settings, over_land=False),
        paths=paths,
        settings=settings,
    )
    try:
        session.widget.set_camera(session.camera)
        ready = session.settle(60)
        return {
            "settled_ready": bool(ready),
            "over_land": False,
            "field_preparations": session.model.counters["field_preparations"],
            "memory": session.memory(),
        }
    finally:
        session.close()


def _platform_resize(snapshot, args):
    session = _Session(snapshot)
    try:
        identity = _platform_identity(session.widget)
        session.widget.set_camera(session.camera)
        ready = session.settle(60)
        before = session.memory()
        session.widget.resize(1180, 820)
        session.pump(400)
        resized_ready = session.settle(60)
        after = session.memory()
        return {
            "settled_ready": bool(ready),
            "resized_ready": bool(resized_ready),
            "logical_size": [session.widget.width(), session.widget.height()],
            "memory_before": before,
            "memory_after": after,
            "identity": identity,
        }
    finally:
        session.close()


def _platform_identity_section(snapshot, args):
    session = _Session(snapshot)
    try:
        session.widget.set_camera(session.camera)
        ready = session.settle(60)
        identity = _platform_identity(session.widget)
        identity["settled_ready"] = bool(ready)
        return identity
    finally:
        session.close()


SECTION_FUNCTIONS = {
    "interaction": section_interaction,
    "cold_load": section_cold_load,
    "coverage_miss": section_coverage_miss,
    "interval_crossing": section_interval_crossing,
    "soak": section_soak,
    "matrix:overlapping_sources": _matrix_overlapping_sources,
    "matrix:source_change": _matrix_source_change,
    "matrix:missing_fields": _matrix_missing_fields,
    "matrix:shoreline_mask": _matrix_shoreline_mask,
    "platform:resize": _platform_resize,
    "platform:identity": _platform_identity_section,
}


def _run_section(section, snapshot, args):
    """Run one section in this process."""
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([sys.argv[0]])
    function = SECTION_FUNCTIONS[section]
    report = function(snapshot, args)
    report.setdefault("section", section)
    return report


def _spawn_section(section, snapshot, output, extra=(), env=None):
    """Run one section in its own process and return its report."""
    import os
    import subprocess

    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--snapshot",
        str(snapshot),
        "--output",
        str(target),
        "--section",
        section,
        *extra,
    ]
    environment = dict(os.environ)
    if env:
        environment.update(env)
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        env=environment,
        capture_output=True,
        text=True,
    )
    if completed.returncode or not target.is_file():
        raise RuntimeError(
            f"the {section!r} section failed: exit {completed.returncode}; "
            f"{completed.stdout[-2000:]}{completed.stderr[-2000:]}"
        )
    return json.loads(target.read_text())


def orchestrate(args):
    """The whole #70 validation run, one native process per measurement.

    The repetitions are matched in workload and fixture and each one is a
    whole process, so a repetition is not contaminated by the process state
    of the repetition before it.  The repetitions are interleaved with the
    other measurements: the soak runs between them, so a drift in the
    machine's state is spread over the repetitions instead of accumulating
    in the last one.
    """
    began = time.perf_counter()
    work = args.output.with_suffix("")
    work.mkdir(parents=True, exist_ok=True)
    sections = [name for name in args.sections.split(",") if name]
    repeats = max(1, int(args.repeats))
    before = repeats // 2
    repetitions = []
    results = {}

    def spawn(section, extra=(), env=None, name=None):
        return _spawn_section(
            section,
            args.snapshot,
            work / f"{name or section.replace(':', '-')}.json",
            extra=extra,
            env=env,
        )

    def run_repetition(index):
        report = spawn("interaction", name=f"repetition-{index}")
        report["repetition_index"] = index
        report["repetition_wall_clock_s"] = time.perf_counter() - began
        repetitions.append(report)

    def run_section(section):
        if section == "soak":
            results["soak"] = spawn(
                section,
                extra=(
                    "--soak-steps",
                    str(args.soak_steps),
                    "--soak-seconds",
                    str(args.soak_seconds if args.soak_seconds else 1800),
                ),
            )
        elif section == "matrix":
            results["matrix"] = _matrix_cells(spawn)
        elif section == "platform":
            results["platform"] = _platform_cells(spawn)
        else:
            results[section] = spawn(section)

    for index in range(before):
        run_repetition(index)
    for section in sections:
        if section == "soak":
            run_section(section)
    for index in range(before, repeats):
        run_repetition(index)
    for section in sections:
        if section != "soak":
            run_section(section)

    merged = dict(repetitions[0])
    merged["repetitions"] = repetitions
    merged["repetition_count"] = len(repetitions)
    merged["repetition_note"] = (
        "each repetition is a whole native process with its own worker "
        "thread, field store, sampling memo and staging provider, on the "
        "same fixture and the same workload; the repetitions are interleaved "
        "with the soak, so a drift in the machine's state is spread over "
        "them"
    )
    merged["limits"] = {
        name: results.get(name, {})
        for name in ("cold_load", "coverage_miss", "interval_crossing")
    }
    merged["soak"] = results.get("soak", {})
    merged["workload_matrix"] = {"cells": results.get("matrix", [])}
    merged["platform_matrix"] = {"cells": results.get("platform", [])}
    merged["validation_duration_s"] = time.perf_counter() - began
    merged["validation_note"] = (
        "every measurement is a whole native process: the headless scene "
        "graph on this host compiles a viewport widget's shader only for the "
        "first widget of a process"
    )
    return merged


def _matrix_cells(spawn):
    """The workload matrix, with an explicit coverage note for every cell."""
    cells = []
    for section, note in (
        (
            "matrix:overlapping_sources",
            "the saved selection of 22 forecasts spans several models and "
            "centres over the same area; the viewport's source order and its "
            "coverage policy are the application's own",
        ),
        (
            "matrix:source_change",
            "a different source set in its own session; the model's source "
            "set is bound when the session is created, so a source change is "
            "a new session, not a step inside one",
        ),
        (
            "matrix:missing_fields",
            "a demand for a region no selected forecast covers; the viewport "
            "must show the loading state and hide the stale weather instead "
            "of painting a neighbour's value. The all-missing payload's "
            "zero-alpha rule is measured in the interaction session's fill "
            "observations",
        ),
        (
            "matrix:shoreline_mask",
            "the over_land=False path: the field must be prepared at the "
            "shoreline resolution tier and the viewport must still serve "
            "current weather",
        ),
    ):
        cells.append(
            {
                "cell": section.split(":", 1)[1],
                "covered": True,
                "note": note,
                "evidence": spawn(section),
            }
        )
    cells.append(
        {
            "cell": "marine-heavy",
            "covered": False,
            "note": (
                "NOT COVERED. The independent viewport has no marine layer: "
                "it renders the wind barbs, the numerical fill and the chart "
                "layer only. A marine-heavy workload exercises the "
                "application's marine pipeline, which this vehicle does not "
                "contain, so no result is claimed for it."
            ),
        }
    )
    cells.append(
        {
            "cell": "both-maps-visible",
            "covered": False,
            "note": (
                "NOT COVERED. The independent viewport is a single standalone "
                "view; the application's two-map layout (the main map and "
                "the log map) is the legacy path, which this vehicle does "
                "not contain, so no result is claimed for it."
            ),
        }
    )
    return cells


def _platform_cells(spawn):
    """The platform matrix, with an explicit coverage note for every cell."""
    cells = []
    resize = spawn("platform:resize")
    identity = resize.get("identity", {})
    cells.append(
        {
            "cell": "resize",
            "covered": True,
            "note": (
                "the widget is resized while it is presented; the viewport "
                "re-derives the demand for the new size and keeps the "
                "weather current"
            ),
            "evidence": resize,
        }
    )
    cells.append(
        {
            "cell": "platform-identity",
            "covered": True,
            "note": (
                "the host the measurement ran on, with the scene graph's own "
                "backend name and the device pixel ratio"
            ),
            "evidence": identity,
        }
    )
    cells.append(
        {
            "cell": "dpr-change",
            "covered": False,
            "note": (
                "NOT COVERED. This host runs at a fixed device pixel ratio of "
                f"{identity.get('device_pixel_ratio')}; a screen change that "
                "alters the ratio cannot be exercised here, and the viewport "
                "records no handling for it. No result is claimed."
            ),
            "evidence": {"device_pixel_ratio": identity.get("device_pixel_ratio")},
        }
    )
    cells.append(
        {
            "cell": "context-loss",
            "covered": False,
            "note": (
                "NOT COVERED. A Metal device loss cannot be induced on this "
                "host, and the viewport records no context-loss handler, so "
                "no result is claimed. The scene graph's own recovery is not "
                "the viewport's."
            ),
            "evidence": {"scene_graph_backend": identity.get("scene_graph_backend")},
        }
    )
    backends = {}
    for name, environment in (
        ("opengl", {"QSG_RHI_BACKEND": "opengl"}),
        ("vulkan", {"QSG_RHI_BACKEND": "vulkan"}),
        ("software", {"QT_QUICK_BACKEND": "software"}),
    ):
        report = spawn("interaction", env=environment, name=f"backend-{name}")
        backends[name] = {
            "requested_backend": report.get("platform", {}).get("requested_backend"),
            "measured_backend": report.get("platform", {}).get("scene_graph_backend"),
            "complete": report.get("complete"),
            "continuous_weather": report.get("continuous_weather"),
            "errors": report.get("errors", []),
            "fill_painted_pixels": report.get("fill", {}).get("painted_pixels"),
            "fill_total_pixels": report.get("fill", {}).get("total_pixels"),
            "fill_values_max_abs_error": report.get("fill", {})
            .get("values", {})
            .get("max_abs_error"),
            "layers_gpu": {
                key: value.get("gpu")
                for key, value in (report.get("layers") or {}).items()
                if isinstance(value, dict)
            },
        }
    cells.append(
        {
            "cell": "non-metal-backend",
            "covered": True,
            "note": (
                "the OpenGL RHI backend, selected with QSG_RHI_BACKEND, in its "
                "own native process: the viewport loads and the layers' "
                "shaders compile, but the framebuffer readback returns an "
                "empty image (0 of the total pixels are painted), so the "
                "numerical fill's pixel and value evidence is NOT available "
                "on that backend with this instrumentation, and the "
                "interaction's continuous-weather check did not hold in this "
                "run. The OpenGL backend does not reproduce the Metal result "
                "and is not claimed to."
            ),
            "evidence": backends["opengl"],
        }
    )
    cells.append(
        {
            "cell": "non-metal-backend-vulkan",
            "covered": True,
            "note": (
                "the Vulkan RHI backend, selected with QSG_RHI_BACKEND, in "
                "its own native process: the viewport's cold load timed out, "
                "so the viewport does not present there. This is a negative "
                "result, not a coverage claim."
            ),
            "evidence": backends["vulkan"],
        }
    )
    cells.append(
        {
            "cell": "software-backend",
            "covered": True,
            "note": (
                "Qt's software renderer, selected with QT_QUICK_BACKEND, in "
                "its own native process: the viewport's cold load timed out, "
                "so the viewport does not present there. This is a negative "
                "result, not a coverage claim."
            ),
            "evidence": backends["software"],
        }
    )
    return cells


def run_once(snapshot):
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
        grid=True,
    )
    # The numerical fill is behind its own flag; the gate harness enables it.
    widget = _viewport_widget_class()(
        model,
        gpu_weather_fill=True,
        chart_layer=True,
        chart_data_dir=snapshot / "data",
    )
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
        # The chart layer: the weather is presented before the chart is
        # ready, and the camera, the forecast time and the weather survive
        # the chart's publication.  The observation follows the interaction
        # phases, so the phases measure the weather pipeline alone.
        report["chart"] = chart_observations(
            widget,
            model,
            camera,
            pump,
            widget._chart_model,
        )
        # The cancellation: a superseded chart generation never publishes.
        report["chart_supersession"] = chart_supersession_observation(
            widget,
            model,
            camera,
            pump,
            widget._chart_model,
        )
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
        # Every layer's GPU, loading and fallback state, read from the
        # layer's own QML object while the viewport is presenting.
        report["layers"] = _layer_states(widget)
        report["platform"] = _platform_identity(widget)
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
        # The layers' own state while the viewport is loading: the loading
        # state must be explicit and the stale weather must be hidden.
        report["layers_while_loading"] = _layer_states(widget)
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
        _release_mask(getattr(model, "_land_mask", None))
        _release_model(model)
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
    parser.add_argument(
        "--section",
        help=(
            "run one measurement section in this process instead of "
            "orchestrating them: " + ", ".join(sorted(SECTION_FUNCTIONS))
        ),
    )
    parser.add_argument(
        "--sections",
        default="",
        help=(
            "the sections to orchestrate, as a comma-separated list: "
            "cold_load, coverage_miss, interval_crossing, soak, matrix, "
            "platform"
        ),
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="matched repetitions of the interaction workload (default 1)",
    )
    parser.add_argument(
        "--soak-steps",
        type=int,
        default=0,
        help="steps of the long-session soak (default 0: no soak)",
    )
    parser.add_argument(
        "--soak-seconds",
        type=float,
        default=None,
        help="cap on the soak's total time (default: no cap)",
    )
    args = parser.parse_args()
    if args.section:
        report = _run_section(args.section, args.snapshot, args)
    else:
        report = orchestrate(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key
                not in (
                    "phases",
                    "repetitions",
                    "soak",
                    "workload_matrix",
                    "platform_matrix",
                    "limits",
                )
            }
        )
    )
    # The gate runner evaluates the completed report, including recorded failures.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
