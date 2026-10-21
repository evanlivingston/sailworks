"""Frozen native-node selection and numerical atlases for shader wind barbs."""

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING, Any, cast

import numpy as np

from gweatherrouting.core.mapviewport import MercatorProjection
from gweatherrouting.rendering.weatherstaging import (
    StagedWind,
    stage_wind,
    time_window,
)
from gweatherrouting.rendering.windpainter import (
    barb_density_parameters,
    mercator_project_flat,
    mercator_project_nodes,
    shoreline_quality,
)

if TYPE_CHECKING:
    from gweatherrouting.qt.gpuweather import WeatherTexturePair
    from gweatherrouting.qt.gpuweatherfields import BarbSelection, GeographicField


# The spatial lattice of a resident field, in view pixels.  A covered camera
# move inside one lattice cell resolves to the same resident field.
FIELD_LATTICE_PX = 64.0


@dataclass(frozen=True)
class BarbView(MercatorProjection):
    width: int
    height: int
    _center_lat: float
    _center_lon: float
    _zoom: float
    density: str
    over_land: bool
    # Opt-in: barbs are laid out on a world grid, so they pan with the map.
    geographic: bool = False

    def get_allocated_width(self):
        return self.width

    def get_allocated_height(self):
        return self.height


@dataclass(frozen=True)
class BarbAtlas:
    """The published payload of one view.

    The historical path publishes the mesh's own endpoint images and
    anchors.  The resident-field path publishes the resident field's endpoint
    images (published once) and the current-view selection (published per
    view); ``field`` and ``selection`` carry them.
    """

    staged: StagedWind
    pair: "WeatherTexturePair | None"
    anchors: tuple
    count: int
    identity: object = None
    field: "GeographicField | None" = None
    selection: "BarbSelection | None" = None

    @property
    def images(self):
        if self.field is not None:
            return self.field.images + cast("BarbSelection", self.selection).images
        return cast("WeatherTexturePair", self.pair).images + self.anchors

    @property
    def nbytes(self):
        return sum(image.sizeInBytes() for image in self.images)


def _native_points(sampler, bounds, cancel):
    """The unique native nodes of every gridded wind source in the bounds."""
    nodes = []
    count = 0
    for index in sampler.indices:
        if cancel():
            return None
        if not any(r.element in ("UGRD", "VGRD") for r in index.records):
            continue
        candidates = index.nodes(bounds)
        count += len(candidates)
        if count > 262144:
            raise ValueError("Wind native-node staging exceeds point budget")
        nodes.append(candidates)
    if not nodes:
        raise ValueError("No gridded wind sources")
    points = np.unique(np.concatenate(nodes), axis=0)
    if not len(points):
        raise ValueError("No native wind nodes in view")
    # Wind columns are longitude-major, then latitude-major on the CPU path.
    return points[np.lexsort((points[:, 0], points[:, 1]))]


def _off_land(chosen, ids, points, view, land_mask, cancel):
    """Drop the chosen symbols that fall on land, if land is not wanted."""
    if view.over_land:
        return chosen
    land_mask.refresh_source(view.get_scale())
    keep = []
    for i in chosen:
        if cancel():
            return None
        keep.append(not land_mask.contains(*points[ids[i]]))
    return chosen[np.asarray(keep, dtype=bool)]


def _view_bounds(view, margin=32.0):
    """The geographic bounds of the view plus the symbol margin."""
    cx, cy = view._lon_to_x(view._center_lon), view._lat_to_y(view._center_lat)
    return (
        (
            view._y_to_lat(cy + view.height / 2 + margin),
            view._x_to_lon(cx - view.width / 2 - margin),
        ),
        (
            view._y_to_lat(cy - view.height / 2 - margin),
            view._x_to_lon(cx + view.width / 2 + margin),
        ),
    )


def field_window(view, lattice=FIELD_LATTICE_PX):
    """The stable spatial window of a resident field.

    The window is the view's bounds quantised *outward* on a pixel lattice,
    so it is a property of the field and not of the exact view: a covered
    camera move inside one lattice cell resolves to the same resident field.
    The quantisation is outward, so the window always contains the view, and
    it is expressed in the view's pixels, so a zoom changes the window and
    therefore the field.
    """
    margin = 32.0
    cx, cy = view._lon_to_x(view._center_lon), view._lat_to_y(view._center_lat)
    west = math.floor((cx - view.width / 2 - margin) / lattice) * lattice
    east = math.ceil((cx + view.width / 2 + margin) / lattice) * lattice
    north = math.floor((cy - view.height / 2 - margin) / lattice) * lattice
    south = math.ceil((cy + view.height / 2 + margin) / lattice) * lattice
    return (
        (view._y_to_lat(south), view._x_to_lon(west)),
        (view._y_to_lat(north), view._x_to_lon(east)),
    )


def _node_key(point):
    """A rounding-tolerant key for a native node.

    The same grid node is enumerated from two different windows, and the
    window's west edge enters the longitude arithmetic, so the two results
    can differ in the last bits.  The key is stable under that difference and
    still separates two nodes that are a hundredth of a millimetre apart.
    """
    return (round(float(point[0]), 9), round(float(point[1]), 9))


class FieldCoverageError(Exception):
    """The resident field does not cover the current view's native nodes."""


def _field_images(staged):
    """The four packed endpoint images of a field, indexed by node id.

    The nodes are laid out in rows of ``FIELD_COLUMNS``; the node id is the
    row-major index.  A missing endpoint stays a NaN, so the shader discards
    it exactly as the historical path discards a missing mesh cell.
    """
    from gweatherrouting.qt.gpuweather import encode_component
    from gweatherrouting.qt.gpuweatherfields import FIELD_COLUMNS

    first = np.asarray(staged.first[0]).reshape(-1, 2)
    second = np.asarray(staged.second[0]).reshape(-1, 2)
    count = len(first)
    rows = max(1, math.ceil(count / FIELD_COLUMNS))
    images = []
    # Match WeatherTexturePair and the scene bindings: U0, V0, U1, V1.
    for endpoint in (first, second):
        for component in range(2):
            plane = np.full((rows, FIELD_COLUMNS), np.nan, dtype=np.float32)
            plane.reshape(-1)[:count] = endpoint[:, component]
            images.append(encode_component(plane))
    return tuple(images)


def _lattice_plan(sampler, window, index):
    """The gridded form's lattice, and the lattice's nodes the field holds.

    The lattice is a regular grid in the viewport's projection, so the GPU's
    2x2 texelFetch bilinear becomes a geographic interpolation.  Its origin is
    quantised on the global grid, so its nodes keep their positions when the
    window moves.

    A lattice node that coincides with one of the field's own nodes takes
    that node's value: the field already holds it, so the gridded form never
    asks the sampler for it twice.  The remaining nodes are sampled in the
    field's own sampling pass, so the gridded form adds no second pass.
    """
    from gweatherrouting.rendering.weathergrid import build_lattice, lattice_spacing

    spacing = lattice_spacing(sampler, window)
    if not spacing:
        return None
    lattice = build_lattice(window, spacing)
    lons, lats = lattice.nodes()
    held = np.full(len(lats), -1, dtype=np.int64)
    for position, key in enumerate(
        _node_key((float(a), float(b))) for a, b in zip(lats, lons)
    ):
        node = index.get(key)
        if node is not None:
            held[position] = node
    return lattice, lons, lats, held


def _gridded_form(plan, staged, offset, field_staged):
    """The gridded form of one field, from the field's own sampling pass.

    The lattice's nodes that the field already holds take the field's own
    value; the rest take the value sampled alongside the field's nodes.  The
    values are therefore the CPU sampler's values at the lattice's node
    positions, a missing node stays missing, and no weather is decoded twice.
    """
    from gweatherrouting.qt.gpuweather import encode_component
    from gweatherrouting.rendering.weathergrid import GriddedField

    lattice, _lons, lats, held = plan
    count = len(lats)
    values = np.full((count, 2), np.nan)
    second = np.full_like(values, np.nan)
    origins = [""] * count
    missing = held < 0
    if missing.any():
        values[missing] = np.asarray(staged.first[0][offset:])[missing]
        second[missing] = np.asarray(staged.second[0][offset:])[missing]
        names = list(staged.sources[offset:])
        for position, name in zip(np.flatnonzero(missing), names):
            origins[position] = name
    kept = np.flatnonzero(~missing)
    if len(kept):
        nodes = held[kept]
        values[kept] = np.asarray(field_staged.first[0]).reshape(-1, 2)[nodes]
        second[kept] = np.asarray(field_staged.second[0]).reshape(-1, 2)[nodes]
        for position, node in zip(kept, nodes):
            origins[position] = field_staged.sources[node]
    rows, cols = lattice.shape
    grid = np.asarray(values).reshape(cols, rows, 2).transpose(1, 0, 2)
    other = np.asarray(second).reshape(cols, rows, 2).transpose(1, 0, 2)
    images = tuple(
        encode_component(plane)
        for plane in (
            grid[:, :, 0],
            grid[:, :, 1],
            other[:, :, 0],
            other[:, :, 1],
        )
    )
    grid_staged = StagedWind(staged.before, staged.after, grid, other, tuple(origins))
    return GriddedField(lattice=lattice, staged=grid_staged, images=images)


def stage_field(  # noqa: C901 - native/gridded forms share one sampling pass
    sampler,
    stamp,
    window,
    identity,
    cancel=lambda: False,
    *,
    view=None,
    land_mask=None,
    geometry=None,
    memo=None,
    grid=False,
):
    """Stage the geographic numerical field of one spatial window.

    The field is the native nodes of the window and the endpoint values of
    the wind parameter at those nodes.  It does not depend on the exact
    view, so a covered camera move reuses it without decoding weather or
    sampling it again.

    With ``grid`` the field also carries its *gridded form*: the same values
    on a regular lattice in the viewport's projection, which is what the GPU's
    fill interpolates.  The lattice's nodes are sampled in the field's own
    pass, and the lattice nodes that coincide with the field's own nodes
    reuse the field's value, so the gridded form adds no second pass over
    the weather and the CPU sampler stays the reference authority.

    A supplied ``memo`` (a :class:`~gweatherrouting.rendering.weatherstaging.
    NodeSampleMemo`) lets a re-centred window reuse the per-node endpoints and
    land decisions of the window it overlaps.  The memo is a pure function of
    the same inputs, so the staged field is bit-identical to a cold staging.
    """
    from gweatherrouting.qt.gpuweatherfields import GeographicField, NativeGridGeometry

    reuse = (
        geometry is not None
        and geometry.identity.window == window
        and (
            view is None
            or view.over_land
            or (
                geometry.land_flags is not None
                and geometry.land_quality == shoreline_quality(view.get_scale())
            )
        )
    )
    points = geometry.points if reuse else _native_points(sampler, window, cancel)
    if points is None:
        return None
    index = (
        geometry.index
        if reuse
        else {_node_key(point): position for position, point in enumerate(points)}
    )
    # The gridded form: a regular lattice in the viewport's projection.  Its
    # nodes join the field's own nodes in ONE sampling pass, so the gridded
    # form adds no second pass over the weather; the nodes the field already
    # holds are reused instead of sampled again.
    plan = _lattice_plan(sampler, window, index) if grid else None
    query_lats = points[:, 0]
    query_lons = points[:, 1]
    if plan is not None:
        _lattice, lons, lats, held = plan
        missing = held < 0
        query_lats = np.concatenate((query_lats, lats[missing]))
        query_lons = np.concatenate((query_lons, lons[missing]))
    staged = stage_wind(
        sampler,
        stamp,
        query_lats[None, :],
        query_lons[None, :],
        cancel,
        memo=memo,
        memo_namespace=(identity.source, identity.before, identity.after),
        node_key=_node_key,
    )
    if staged is None:
        return None
    count = len(points)
    field_staged = StagedWind(
        staged.before,
        staged.after,
        staged.first[:, :count],
        staged.second[:, :count],
        staged.sources[:count],
    )
    if any(
        np.any(np.linalg.norm(a, axis=-1) > 250)
        for a in (field_staged.first, field_staged.second)
    ):
        raise ValueError("Wind exceeds bounded GPU symbol range")
    land_flags = geometry.land_flags if reuse else None
    if view is not None and not view.over_land and land_flags is None:
        quality = shoreline_quality(view.get_scale())
        land_mask.refresh_source(view.get_scale())
        flags = []
        for point in points:
            if cancel():
                return None
            key = _node_key(point)
            cached = memo.get_land(key, quality) if memo is not None else None
            if cached is not None:
                flags.append(cached)
                continue
            over_land = land_mask.contains(*point)
            flags.append(over_land)
            if memo is not None:
                memo.put_land(key, quality, over_land)
        land_flags = np.asarray(flags, dtype=bool)
        land_flags.setflags(write=False)
    points.setflags(write=False)
    # Multiple selected forecasts often share a grid (different times/runs).
    # Their candidate nodes are identical. Deduplicate only enumeration;
    # stage_wind above still evaluates every source under the existing policy.
    grids: dict[tuple, Any] = {}
    for grid in sampler.indices:
        if any(record.element in ("UGRD", "VGRD") for record in grid.records):
            geometry = NativeGridGeometry.from_index(grid)
            grids.setdefault(geometry.key(), geometry)
    grid_field = (
        _gridded_form(plan, staged, count, field_staged) if plan is not None else None
    )
    return GeographicField(
        identity=identity,
        points=points,
        staged=field_staged,
        index=index,
        images=_field_images(field_staged),
        generation=identity.key(),
        grids=tuple(grids.values()),
        land_flags=land_flags,
        land_zoom=None if view is None else view._zoom,
        land_quality=(
            None
            if view is None or view.over_land
            else shoreline_quality(view.get_scale())
        ),
        grid=grid_field,
    )


def _geographic_cells(points, xx, yy, view, step, margin, cols, rows):
    """Barb cells anchored to the world, so the layout moves with the map.

    Membership is the first node of each world cell, which depends only on
    geography and zoom.  A world cell's mesh slot is the screen cell of its
    centre; world cells form a square lattice of the mesh's own step, so the
    slots are one-to-one.  A cell whose centre falls outside the mesh is
    dropped, which affects only the map's edge.
    """
    world_x, world_y = mercator_project_nodes(points[:, 0], points[:, 1], view._zoom)
    cell_x = np.floor(world_x / step).astype(int)
    cell_y = np.floor(world_y / step).astype(int)
    _, first = np.unique(np.stack((cell_y, cell_x), axis=1), axis=0, return_index=True)
    offset_x = float(np.median(world_x - xx))
    offset_y = float(np.median(world_y - yy))
    slot_x = np.floor(((cell_x + 0.5) * step - offset_x + margin) / step).astype(int)
    slot_y = np.floor(((cell_y + 0.5) * step - offset_y + margin) / step).astype(int)
    valid = (slot_x >= 0) & (slot_x < cols) & (slot_y >= 0) & (slot_y < rows)
    cells = np.where(valid, slot_y * cols + slot_x, -1)
    return cells, np.sort(first[valid[first]])


def stage_selection(sampler, field, stamp, view, land_mask, cancel=lambda: False):
    """The exact current-view selection of a resident field.

    The native nodes, the thinning, the membership and the anchors are
    recomputed for the current view, exactly as the CPU foreground does.  The
    numerical values come from the resident field, so no weather is decoded
    and no sample is taken again.
    """
    from gweatherrouting.qt.gpuweather import encode_component
    from gweatherrouting.qt.gpuweatherfields import BarbSelection

    step, cap = barb_density_parameters(view.density)
    if step <= 0:
        raise ValueError("Unthinned wind uses CPU symbols")
    margin = 32.0
    cols = math.ceil((view.width + 2 * margin) / step) + 1
    rows = math.ceil((view.height + 2 * margin) / step) + 1
    if cols * rows > 8192:
        raise ValueError("Wind symbol mesh exceeds vertex budget")
    if field.grids:
        bounds = _view_bounds(view, margin)
        nodes = [grid.nodes(bounds) for grid in field.grids]
        points = np.unique(np.concatenate(nodes), axis=0)
        points = points[np.lexsort((points[:, 0], points[:, 1]))]
    elif sampler is None:
        raise FieldCoverageError("Resident field lacks native grid metadata")
    else:
        points = _native_points(sampler, _view_bounds(view, margin), cancel)
    if points is None:
        return None
    node_ids = np.array(
        [field.index.get(_node_key(point), -1) for point in points], dtype=np.int64
    )
    if np.any(node_ids < 0):
        raise FieldCoverageError("The resident field does not cover this view")
    first = np.asarray(field.staged.first[0]).reshape(-1, 2)[node_ids]
    second = np.asarray(field.staged.second[0]).reshape(-1, 2)[node_ids]
    weight = field.staged.weight(stamp)
    uv = first + (second - first) * weight
    xx, yy = mercator_project_flat(
        points[:, 0],
        points[:, 1],
        view._center_lat,
        view._center_lon,
        view._zoom,
        view.width,
        view.height,
    )
    visible = (
        (xx >= -margin)
        & (xx <= view.width + margin)
        & (yy >= -margin)
        & (yy <= view.height + margin)
        & np.isfinite(uv).all(axis=1)
        & np.isfinite(xx)
        & np.isfinite(yy)
    )
    ids = np.flatnonzero(visible)
    if getattr(view, "geographic", False):
        cells, chosen = _geographic_cells(
            points[ids], xx[ids], yy[ids], view, step, margin, cols, rows
        )
    else:
        cell_x = np.floor((xx[ids] + margin) / step).astype(int)
        cell_y = np.floor((yy[ids] + margin) / step).astype(int)
        cells = cell_y * cols + cell_x
        _, first_cell = np.unique(cells, return_index=True)
        chosen = np.sort(first_cell)
    chosen = _cap_selection(chosen, cap)
    if not view.over_land and field.land_flags is not None:
        _require_land_quality(field, view)
        chosen = chosen[~field.land_flags[node_ids[ids[chosen]]]]
    elif not view.over_land and sampler is None:
        raise FieldCoverageError("Resident land decisions are unavailable")
    else:
        chosen = _off_land(chosen, ids, points, view, land_mask, cancel)
    if chosen is None:
        return None
    selected = ids[chosen]
    slots = cells[chosen]
    # Empty cells carry node id -1: the shader discards them exactly as it
    # discards a missing mesh cell, and their anchors stay finite and local.
    selection_ids = np.full((rows * cols,), -1.0, dtype=np.float32)
    selection_ids[slots] = node_ids[selected]
    gy, gx = np.mgrid[:rows, :cols]
    ax, ay = (gx * step - margin).astype(np.float32), (gy * step - margin).astype(
        np.float32
    )
    ax.ravel()[slots], ay.ravel()[slots] = xx[selected], yy[selected]
    selection_ids.setflags(write=False)
    return BarbSelection(
        generation=field.generation,
        node_ids=selection_ids.reshape(rows, cols),
        anchors=(encode_component(ax), encode_component(ay)),
        count=len(selected),
        field=field,
        node_image=encode_component(selection_ids.reshape(rows, cols)),
    )


def _cap_selection(chosen, cap):
    """Apply the existing evenly-spaced symbol cap, when configured."""
    if cap is not None and len(chosen) > cap:
        return chosen[np.linspace(0, len(chosen) - 1, cap, dtype=int)]
    return chosen


def _require_land_quality(field, view):
    """Reject retained land decisions from a different shoreline tier."""
    if field.land_quality != shoreline_quality(view.get_scale()):
        raise FieldCoverageError("Resident land mask has a different scale")


def stage_barbs(
    sampler,
    stamp,
    view,
    land_mask,
    cancel=lambda: False,
    *,
    store=None,
    source=None,
):
    """Select the same native nodes/cells/count budget as the CPU foreground.

    Only standard density modes with a finite cell size are supported. No
    resampled screen-grid approximation is substituted for native samples.

    This remains the existing application's exact-view atlas path. A supplied
    ``store`` caches that atlas, not a reusable geographic field. The independent
    viewport uses ``stage_field`` and ``select_resident`` instead.
    """
    from gweatherrouting.qt.gpuweather import encode_component
    from gweatherrouting.rendering.weatherstaging import StagedWind, time_window

    step, cap = barb_density_parameters(view.density)
    if step <= 0:
        raise ValueError("Unthinned wind uses CPU symbols")
    margin = 32.0
    cols = math.ceil((view.width + 2 * margin) / step) + 1
    rows = math.ceil((view.height + 2 * margin) / step) + 1
    if cols * rows > 8192:
        raise ValueError("Wind symbol mesh exceeds vertex budget")
    cx, cy = view._lon_to_x(view._center_lon), view._lat_to_y(view._center_lat)
    bounds = (
        (
            view._y_to_lat(cy + view.height / 2 + margin),
            view._x_to_lon(cx - view.width / 2 - margin),
        ),
        (
            view._y_to_lat(cy - view.height / 2 - margin),
            view._x_to_lon(cx + view.width / 2 + margin),
        ),
    )
    identity = None
    if store is not None:
        from gweatherrouting.qt.gpuweatherfields import FieldIdentity

        before, after = time_window(sampler, stamp)
        identity = FieldIdentity(source, "wind", before, after, bounds, (rows, cols))
        resident = store.acquire(identity)
        if resident is not None:
            return resident
    points = _native_points(sampler, bounds, cancel)
    if points is None:
        return None
    staged = stage_wind(
        sampler, stamp, points[:, 0][None, :], points[:, 1][None, :], cancel
    )
    if staged is None:
        return None
    if any(
        np.any(np.linalg.norm(a, axis=-1) > 250) for a in (staged.first, staged.second)
    ):
        raise ValueError("Wind exceeds bounded GPU symbol range")
    weight = staged.weight(stamp)
    uv = staged.first[0] + (staged.second[0] - staged.first[0]) * weight
    xx, yy = mercator_project_flat(
        points[:, 0],
        points[:, 1],
        view._center_lat,
        view._center_lon,
        view._zoom,
        view.width,
        view.height,
    )
    visible = (
        (xx >= -margin)
        & (xx <= view.width + margin)
        & (yy >= -margin)
        & (yy <= view.height + margin)
        & np.isfinite(uv).all(axis=1)
        & np.isfinite(xx)
        & np.isfinite(yy)
    )
    ids = np.flatnonzero(visible)
    cell_x = np.floor((xx[ids] + margin) / step).astype(int)
    cell_y = np.floor((yy[ids] + margin) / step).astype(int)
    cells = cell_y * cols + cell_x
    _, first = np.unique(cells, return_index=True)
    chosen = np.sort(first)
    if cap is not None and len(chosen) > cap:
        chosen = chosen[np.linspace(0, len(chosen) - 1, cap, dtype=int)]
    chosen = _off_land(chosen, ids, points, view, land_mask, cancel)
    if chosen is None:
        return None
    selected = ids[chosen]
    slots = cells[chosen]
    a = np.full((rows * cols, 2), np.nan, dtype=np.float32)
    b = np.full_like(a, np.nan)
    a[slots] = staged.first[0, selected]
    b[slots] = staged.second[0, selected]
    # Empty slots get local anchors (not offscreen) so connector triangles
    # cannot span the whole viewport. Their NaN vectors discard the symbols.
    gy, gx = np.mgrid[:rows, :cols]
    ax, ay = (gx * step - margin).astype(np.float32), (gy * step - margin).astype(
        np.float32
    )
    ax.ravel()[slots], ay.ravel()[slots] = xx[selected], yy[selected]
    a, b = a.reshape(rows, cols, 2), b.reshape(rows, cols, 2)
    a.setflags(write=False)
    b.setflags(write=False)
    atlas_stage = StagedWind(staged.before, staged.after, a, b, ())
    if cancel():
        return None
    atlas = BarbAtlas(
        atlas_stage,
        atlas_stage.texture_pair(),
        (encode_component(ax), encode_component(ay)),
        len(selected),
        identity,
    )
    if store is not None:
        from gweatherrouting.qt.gpuweatherfields import ResidentField

        store.store(ResidentField(identity, atlas, (a, b)))
    return atlas


def _stage_with_field(
    sampler, stamp, view, land_mask, cancel, store, source, grid=False
):
    """Stage the current-view selection from a resident geographic field."""
    from gweatherrouting.qt.gpuweatherfields import FieldIdentity

    before, after = time_window(sampler, stamp)
    window = field_window(view)
    resolution = tuple(
        sorted({index.nominal_step_degrees for index in sampler.indices})
    )
    identity = FieldIdentity(
        source=source,
        parameter="wind",
        before=before,
        after=after,
        window=window,
        resolution=resolution,
    )
    field = store.acquire(identity)
    if field is None or (not view.over_land and field.land_flags is None):
        field = stage_field(
            sampler,
            stamp,
            window,
            identity,
            cancel,
            view=view,
            land_mask=land_mask,
            grid=grid,
        )
        if field is None:
            return None
        store.store(field)
    try:
        selection = stage_selection(sampler, field, stamp, view, land_mask, cancel)
    except FieldCoverageError:
        # The resident field does not cover this view's nodes, which happens
        # only for an unusual window (a view that crosses the longitude
        # seam).  Stage the view's own window instead of failing the
        # numerical path; the values are then the direct path's.
        fallback = stage_field(
            sampler,
            stamp,
            _view_bounds(view),
            identity,
            cancel,
            view=view,
            land_mask=land_mask,
            grid=grid,
        )
        if fallback is None:
            return None
        selection = stage_selection(sampler, fallback, stamp, view, land_mask, cancel)
    if selection is None:
        return None
    return selection_atlas(selection)


def selection_atlas(selection):
    """Wrap current-view portrayal without recreating numerical field images."""
    return BarbAtlas(
        selection.field.staged,
        None,
        selection.anchors,
        selection.count,
        selection.field.identity,
        selection.field,
        selection,
    )


def select_resident(field, stamp, view):
    """GUI-safe covered selection: immutable metadata only, no sampler or I/O."""
    if not field.identity.contains(stamp):
        raise FieldCoverageError("Forecast time is outside the resident field")
    return selection_atlas(stage_selection(None, field, stamp, view, None))
