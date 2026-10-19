"""Read-only metadata for Web Mercator PNG raster MBTiles archives."""

import math
import sqlite3
import struct
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_TILE_BYTES = 8 * 1024 * 1024


@contextmanager
def open_archive(path):
    connection = sqlite3.connect(
        Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def png_size(data):
    if len(data) > MAX_TILE_BYTES or len(data) < 24 or not data.startswith(PNG_MAGIC):
        raise ValueError(
            "Only PNG raster MBTiles are supported (not JPEG or vector/PBF tiles)"
        )
    width, height = struct.unpack(">II", data[16:24])
    if width != height or not 1 <= width <= 1024:
        raise ValueError(
            "MBTiles PNG tiles must be square and no larger than 1024 pixels"
        )
    return width


@dataclass(frozen=True)
class MBTilesInfo:
    path: str
    name: str
    bounds: tuple
    zooms: tuple
    scheme: str = "tms"
    attribution: str = ""
    mtime_ns: int = 0
    size: int = 0

    def metadata(self):
        return asdict(self)

    @classmethod
    def from_metadata(cls, path, metadata):
        values = dict(metadata)
        values.update(
            path=str(Path(path).resolve()),
            bounds=tuple(values["bounds"]),
            zooms=tuple(values["zooms"]),
        )
        return cls(**values)

    def contains(self, lat, lon):
        west, south, east, north = self.bounds
        longitude = west <= lon <= east if west <= east else lon >= west or lon <= east
        return south <= lat <= north and longitude


def _latitude(y, zoom):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / 2**zoom))))


def read_info(path):
    """Inspect headers/indexes only; never load an archive's full tile set."""
    path = Path(path).resolve()
    try:
        with open_archive(path) as db:
            metadata = dict(db.execute("SELECT name, value FROM metadata"))
            plan = db.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM tiles WHERE zoom_level=? LIMIT 1",
                (0,),
            ).fetchall()
            if not any("SEARCH" in row[3].upper() for row in plan):
                raise ValueError(
                    "MBTiles requires a tile index starting with zoom_level for on-demand display"
                )
            format_ = metadata.get("format", "png").lower()
            if format_ != "png":
                raise ValueError(
                    "Only PNG raster MBTiles are supported (not JPEG or vector/PBF tiles)"
                )
            scheme = metadata.get("scheme", "tms").lower()
            if scheme not in ("tms", "xyz"):
                raise ValueError(f"Unsupported MBTiles tile scheme: {scheme}")
            # At most 31 indexed existence probes, including sparse pyramids.
            zooms = tuple(
                z
                for z in range(31)
                if db.execute(
                    "SELECT 1 FROM tiles WHERE zoom_level=? LIMIT 1", (z,)
                ).fetchone()
            )
            if not zooms:
                raise ValueError("This MBTiles archive contains no tiles")
            sample = db.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()[0]
            png_size(sample)
            if metadata.get("bounds"):
                bounds = tuple(float(v.strip()) for v in metadata["bounds"].split(","))
            else:
                z = zooms[0]
                left, right, low, high = db.execute(
                    "SELECT min(tile_column), max(tile_column),"
                    " min(tile_row), max(tile_row) FROM tiles WHERE zoom_level=?",
                    (z,),
                ).fetchone()
                top, bottom = (
                    (2**z - 1 - high, 2**z - low)
                    if scheme == "tms"
                    else (low, high + 1)
                )
                bounds = (
                    left / 2**z * 360 - 180,
                    _latitude(bottom, z),
                    (right + 1) / 2**z * 360 - 180,
                    _latitude(top, z),
                )
            if len(bounds) != 4 or not all(math.isfinite(v) for v in bounds):
                raise ValueError("Invalid MBTiles coverage bounds")
            west, south, east, north = bounds
            if not (
                -180 <= west <= 180
                and -180 <= east <= 180
                and -90 <= south < north <= 90
            ):
                raise ValueError("Invalid MBTiles coverage bounds")
            stat = path.stat()
            return MBTilesInfo(
                str(path),
                metadata.get("name") or path.stem,
                bounds,
                zooms,
                scheme,
                metadata.get("attribution", ""),
                stat.st_mtime_ns,
                stat.st_size,
            )
    except sqlite3.Error as error:
        raise ValueError(f"Cannot read MBTiles archive: {error}") from error
