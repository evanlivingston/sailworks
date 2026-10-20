"""The gridded form of a resident field: a geographic lattice for the GPU.

The resident field's payload is a *node list*: the native points of the
window and the wind parameter's endpoint values at those points.  The shader
interpolates a texture with a 2x2 texelFetch bilinear, so over a packed node
list that interpolation is not geographic and the fill paints an unplaced
raster.

The gridded form is the same field on a regular lattice in the viewport's
projection, so the shader's bilinear becomes a geographic interpolation.  The
lattice is regular in longitude and in Mercator-y, both in degrees, so one
cell is a square of the source's own resolution.  Its origin is quantised on
the global grid, so the lattice keeps its position when the field's window
moves and the sampling memo's entries stay valid.

The lattice's values come from the CPU sampler, which stays the reference
authority and the fallback path; the gridded form costs no extra decode.
"""

from dataclasses import dataclass
import math

import numpy as np

# The Mercator limit of the viewport's projection.
MERCATOR_MAX_LAT = 85.051129

# The metres in one degree of arc, the convention the existing
# ``GridIndex.nominal_step_degrees`` uses for a projected grid.
METRES_PER_DEGREE = 111319.0


def mercator_y(lat):
    """The Mercator ordinate of a latitude, in degrees.

    The viewport's projection uses the spherical Mercator; the ordinate
    shares the longitude's scale, so a square cell in this space is a square
    of ground.
    """
    lat = np.clip(np.asarray(lat, dtype=float), -MERCATOR_MAX_LAT, MERCATOR_MAX_LAT)
    rad = np.radians(lat)
    return np.degrees(np.log(np.tan(rad) + 1.0 / np.cos(rad)))


def mercator_lat(y):
    """The latitude of a Mercator ordinate, in degrees."""
    return np.degrees(
        2.0 * np.arctan(np.exp(np.radians(np.asarray(y, dtype=float)))) - math.pi / 2.0
    )


def unwrap_bounds(bounds):
    """The window's bounds as a westward-to-eastward pair."""
    (south, west), (north, east) = bounds
    if east < west:
        east += 360.0
    return (south, west), (north, east)


@dataclass(frozen=True)
class GridLattice:
    """A regular lattice over a geographic window, in the viewport's projection.

    ``origin_lon`` and ``origin_merc`` are the *cell corner* of the lattice's
    north-west cell, quantised on the global grid; the node of a cell is its
    centre.  ``cols`` and ``rows`` are the cell counts.  The rows run from
    north to south, the order a texture's rows have, so the lattice's raster
    is the raster the shader reads.
    """

    origin_lon: float
    origin_merc: float
    spacing: float
    cols: int
    rows: int
    window: tuple

    @property
    def key(self):
        """A stable identity for the lattice's geometry."""
        return (
            round(self.origin_lon, 9),
            round(self.origin_merc, 9),
            round(self.spacing, 12),
            self.cols,
            self.rows,
        )

    @property
    def node_count(self):
        return self.cols * self.rows

    @property
    def shape(self):
        return (self.rows, self.cols)

    def nodes(self):
        """The node positions: the cell centres, column-major."""
        cols = self.origin_lon + (np.arange(self.cols) + 0.5) * self.spacing
        rows = self.origin_merc - (np.arange(self.rows) + 0.5) * self.spacing
        lon, lat = np.meshgrid(cols, mercator_lat(rows), indexing="ij")
        return lon.ravel(), lat.ravel()

    def cells(self, lons, lats):
        """The fractional cell indices of geographic positions.

        A longitude is taken into the lattice's own branch of the global
        grid, so a position across the antimeridian is not split between two
        cells.  The row index counts from the north, the texture's order.
        """
        lon = np.asarray(lons, dtype=float)
        offset = (lon - self.origin_lon + 180.0) % 360.0 - 180.0
        fx = offset / self.spacing - 0.5
        fy = (self.origin_merc - mercator_y(lats)) / self.spacing - 0.5
        return fx, fy

    def bounds(self):
        """The geographic bounds the lattice covers."""
        west = self.origin_lon
        east = self.origin_lon + self.cols * self.spacing
        north = float(mercator_lat(self.origin_merc))
        south = float(mercator_lat(self.origin_merc - self.rows * self.spacing))
        return (south, west), (north, east)


def build_lattice(bounds, spacing):
    """The lattice that covers ``bounds``, quantised on the global grid.

    The lattice is quantised *outward*, so it always covers the window, and
    its origin is a multiple of the spacing, so the lattice does not move
    when the window does.
    """
    (south, west), (north, east) = unwrap_bounds(bounds)
    origin_lon = math.floor(west / spacing) * spacing
    origin_merc = math.ceil(float(mercator_y(north)) / spacing) * spacing
    cols = int(math.ceil((east - origin_lon) / spacing)) + 1
    rows = int(math.ceil((origin_merc - float(mercator_y(south))) / spacing)) + 1
    return GridLattice(
        origin_lon=origin_lon,
        origin_merc=origin_merc,
        spacing=spacing,
        cols=cols,
        rows=rows,
        window=unwrap_bounds(bounds),
    )


def _resolution_metres(index):
    """The source's cell size on its latitude axis, in metres.

    A geographic grid's step is already in degrees of arc; a projected
    grid's step is in the projection's units, which are metres unless the
    projection says otherwise.
    """
    step = abs(float(index.transform[5]))
    if not step:
        return 0.0
    projection = ""
    dataset = getattr(index, "dataset", None)
    if dataset is not None:
        try:
            projection = dataset.GetProjection()
        except (AttributeError, RuntimeError, ValueError):
            projection = ""
    if projection:
        from pyproj import CRS

        try:
            unit = CRS(projection).axis_info[0].unit_name
        except Exception:  # pragma: no cover - a malformed projection
            unit = ""
        if unit and unit.lower().startswith("degree"):
            return step * METRES_PER_DEGREE
        return step
    return step * METRES_PER_DEGREE


def lattice_spacing(sampler, bounds):
    """The lattice's cell, in degrees of the viewport's projection.

    The cell is the finest contributing source's own cell -- its latitude
    axis, in metres -- measured in the viewport's projection at the window's
    centre.  The projection is conformal, so one number serves both axes and
    the cell is square on the ground.  The gridded form therefore carries
    the native resolution and no more.
    """
    (south, _west), (north, _east) = bounds
    centre = 0.5 * (south + north)
    finest = 0.0
    for index in sampler.indices:
        if not any(record.element in ("UGRD", "VGRD") for record in index.records):
            continue
        metres = _resolution_metres(index)
        if metres and (not finest or metres < finest):
            finest = metres
    if not finest:
        return 0.0
    return finest / (METRES_PER_DEGREE * math.cos(math.radians(centre)))


@dataclass(frozen=True)
class GriddedField:
    """The gridded form of one field: its lattice, values and images."""

    lattice: GridLattice
    staged: object
    images: tuple

    @property
    def node_count(self):
        return self.lattice.node_count

    @property
    def value_bytes(self):
        return sum(
            getattr(value, "nbytes", 0)
            for value in (self.staged.first, self.staged.second)
        )

    @property
    def texture_bytes(self):
        return sum(image.sizeInBytes() for image in self.images)

    @property
    def nbytes(self):
        return self.value_bytes + self.texture_bytes
