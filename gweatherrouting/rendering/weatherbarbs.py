"""Frozen native-node selection and numerical atlases for shader wind barbs."""

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING

import numpy as np

from gweatherrouting.core.mapviewport import MercatorProjection
from gweatherrouting.rendering.weatherstaging import StagedWind, stage_wind
from gweatherrouting.rendering.windpainter import (
    barb_density_parameters,
    mercator_project_flat,
)

if TYPE_CHECKING:
    from gweatherrouting.qt.gpuweather import WeatherTexturePair


@dataclass(frozen=True)
class BarbView(MercatorProjection):
    width: int
    height: int
    _center_lat: float
    _center_lon: float
    _zoom: float
    density: str
    over_land: bool

    def get_allocated_width(self):
        return self.width

    def get_allocated_height(self):
        return self.height


@dataclass(frozen=True)
class BarbAtlas:
    staged: StagedWind
    pair: "WeatherTexturePair"
    anchors: tuple
    count: int
    identity: object = None

    @property
    def images(self):
        return self.pair.images + self.anchors

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

    When a ``store`` is supplied the staged field is retained under its
    stable identity: source, parameter, resident time window, spatial window
    and resolution.  A field that is still resident is published again by
    reference, so a time change inside the window and a return to a window
    that is still resident cost no re-decode, no re-encode and no upload.
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
        identity = FieldIdentity(
            source=source,
            parameter="wind",
            before=before,
            after=after,
            window=bounds,
            resolution=(rows, cols),
        )
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

        store.store(
            ResidentField(identity, atlas, (atlas_stage.first, atlas_stage.second))
        )
    return atlas
