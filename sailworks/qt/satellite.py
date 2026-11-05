"""Satellite basemap frames rendered through the optional MapLibre FFI.

The renderer is deliberately display-only: it produces a still frame for one
camera, and the viewport publishes that frame exactly like a composed chart
image. MapLibre owns tiles, zoom levels, caching and offline packs; Sailworks
owns when a frame is requested and how it is placed.

The MapLibre package is optional and platform-specific, so importing this
module never imports it. ``available()`` reports whether the layer can run and
``SatelliteRenderer`` raises ``SatelliteError`` when it cannot.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import struct
import sys
from dataclasses import dataclass, replace
from threading import Condition, Lock, Thread

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

#: Public-domain CONUS imagery. The default source is display-only and does not
#: grant offline redistribution rights; a configured pack is used instead when
#: the user has one.
DEFAULT_TILE_URL = (
    "https://basemap.nationalmap.gov/arcgis/rest/services/"
    "USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"
)
DEFAULT_ATTRIBUTION = "USGS The National Map"
PACK_SUFFIXES = (".mbtiles", ".pmtiles")


class SatelliteError(RuntimeError):
    """The satellite layer cannot render this request."""


@dataclass(frozen=True)
class SatelliteSource:
    """A raster source MapLibre can draw: online XYZ or a local pack."""

    tiles: tuple[str, ...]
    min_zoom: int = 0
    max_zoom: int = 19
    tile_size: int = 256
    attribution: str = ""
    #: Optional (west, south, east, north) coverage. MapLibre must not request
    #: pack tiles outside these bounds: a missing PMTiles entry is an error,
    #: not a miss, and fails the whole frame.
    bounds: tuple[float, float, float, float] | None = None
    #: Optional source drawn beneath this one. A pack uses the online source
    #: here so areas the pack does not cover still render when connected.
    underlay: "SatelliteSource | None" = None

    @property
    def requires_network(self) -> bool:
        """Whether drawing this source needs a working connection."""
        return any(
            tile.startswith("http://") or tile.startswith("https://")
            for tile in self.tiles
        ) or (self.underlay is not None and self.underlay.requires_network)

    @property
    def offline_capable(self) -> bool:
        """Whether at least part of this source exists without a network."""
        return any(
            not (tile.startswith("http://") or tile.startswith("https://"))
            for tile in self.tiles
        ) or (self.underlay is not None and self.underlay.offline_capable)

    def __post_init__(self):
        if not self.tiles or any(
            not isinstance(tile, str) or not tile for tile in self.tiles
        ):
            raise ValueError("satellite source requires at least one tile URL")
        if not 0 <= self.min_zoom <= self.max_zoom <= 24:
            raise ValueError("satellite source has an invalid zoom range")
        if self.tile_size <= 0:
            raise ValueError("satellite source has an invalid tile size")
        if self.underlay is not None and not isinstance(self.underlay, SatelliteSource):
            raise ValueError("satellite underlay must be a satellite source")
        if self.bounds is not None:
            west, south, east, north = self.bounds
            if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
                raise ValueError("satellite source has invalid bounds")

    def _raster(self) -> dict:
        raster = {
            "type": "raster",
            "tiles": list(self.tiles),
            "tileSize": self.tile_size,
            "minzoom": self.min_zoom,
            "maxzoom": self.max_zoom,
            "attribution": self.attribution,
        }
        if self.bounds is not None:
            raster["bounds"] = list(self.bounds)
        return raster

    def style(self) -> dict:
        """The MapLibre style for this source, underlay first, this layer on top."""
        sources = {}
        layers = []
        if self.underlay is not None:
            sources["satellite-underlay"] = self.underlay._raster()
            layers.append(
                {
                    "id": "satellite-underlay",
                    "type": "raster",
                    "source": "satellite-underlay",
                }
            )
        sources["satellite"] = self._raster()
        layers.append({"id": "satellite", "type": "raster", "source": "satellite"})
        return {"version": 8, "sources": sources, "layers": layers}


def online_source(
    tile_url: str = DEFAULT_TILE_URL,
    attribution: str = DEFAULT_ATTRIBUTION,
    min_zoom: int = 0,
    max_zoom: int = 19,
) -> SatelliteSource:
    """An online XYZ raster source."""
    return SatelliteSource(
        (tile_url,), min_zoom=min_zoom, max_zoom=max_zoom, attribution=attribution
    )


def _pmtiles_bounds(path: str):
    """The (west, south, east, north) from a PMTiles v3 header."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(127)
    except OSError:
        return None
    if len(header) < 127 or header[:7] != b"PMTiles" or header[7] != 3:
        return None
    # v3 header: 102 bytes of counts/offsets, then four int32 in 1e-7 degrees.
    west, south, east, north = (
        value / 1e7 for value in struct.unpack_from("<4i", header, 102)
    )
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        return None
    return (west, south, east, north)


def _mbtiles_bounds(path: str):
    """The ``bounds`` metadata entry of an MBTiles archive."""
    try:
        database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = database.execute(
                "SELECT value FROM metadata WHERE name = 'bounds'"
            ).fetchone()
        finally:
            database.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        west, south, east, north = (float(part) for part in row[0].split(","))
    except (TypeError, ValueError):
        return None
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        return None
    return (west, south, east, north)


def pack_source(path: str) -> SatelliteSource:
    """A local MBTiles or PMTiles archive as a raster source."""
    resolved = os.path.abspath(os.path.expanduser(path))
    suffix = os.path.splitext(resolved)[1].lower()
    if suffix not in PACK_SUFFIXES:
        raise ValueError(f"unsupported satellite pack: {path}")
    if not os.path.isfile(resolved):
        raise FileNotFoundError(resolved)
    # MBTiles is read directly; PMTiles needs the explicit file URL prefix.
    url = f"mbtiles://{resolved}"
    bounds = _mbtiles_bounds(resolved)
    if suffix == ".pmtiles":
        url = f"pmtiles://file://{resolved}"
        bounds = _pmtiles_bounds(resolved)
    return SatelliteSource(
        (url,),
        min_zoom=0,
        max_zoom=19,
        attribution="Offline imagery pack",
        bounds=bounds,
    )


def available() -> bool:
    """Whether this interpreter can import a MapLibre runtime."""
    if sys.version_info < (3, 12):
        return False
    if importlib.util.find_spec("maplibre_native_ffi") is None:
        return False
    try:
        import maplibre_native_ffi  # noqa: F401
    except Exception:  # noqa: BLE001 - a broken install disables the layer
        return False
    return True


def configured_source(pack_path: str = "", data_dir: str = "") -> SatelliteSource:
    """A pack when one is configured or discovered, otherwise online imagery.

    A pack is hybrid: the online source sits beneath it, so areas outside the
    pack's coverage still render while connected. Offline, the pack alone is
    drawn. A configured path wins; otherwise the ``satellite`` folder under the
    data directory is scanned, PMTiles before MBTiles, so dropping a file there
    is enough to go offline.
    """
    pack = None
    if pack_path:
        pack = pack_source(pack_path)
    elif data_dir:
        folder = os.path.join(os.path.expanduser(data_dir), "satellite")
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            names = []
        for suffix in (".pmtiles", ".mbtiles"):
            for name in names:
                if name.lower().endswith(suffix):
                    pack = pack_source(os.path.join(folder, name))
                    break
            if pack is not None:
                break
    if pack is None:
        return online_source()
    online = online_source()
    attribution = pack.attribution
    if online.attribution:
        attribution = f"{attribution} · {online.attribution}"
    return replace(pack, underlay=online, attribution=attribution)


class SatelliteRenderer:
    """Render still frames for one camera. Call from a single worker thread.

    The MapLibre runtime, map and texture live for the lifetime of the
    renderer and are recreated only when the frame size changes. A cache
    directory keeps fetched online tiles across frames and runs.
    """

    def __init__(self, source: SatelliteSource, cache_dir: str = ""):
        self._source = source
        self._cache_dir = cache_dir
        self._lock = Lock()
        self._runtime = None
        self._map = None
        self._device = None
        self._session = None
        self._size = None
        self._style = None

    def render(
        self,
        center_lat: float,
        center_lon: float,
        zoom: float,
        width: int,
        height: int,
        timeout: float = 12.0,
    ) -> QImage:
        if width < 1 or height < 1:
            raise SatelliteError("satellite frame has no size")
        if not -90.0 <= center_lat <= 90.0:
            raise SatelliteError("satellite frame has an invalid latitude")
        with self._lock:
            try:
                module = self._acquire(width, height)
                self._map.update_camera(
                    module.CameraUpdate(
                        module.CameraUpdateMode.JUMP,
                        module.CameraOptions(
                            center=module.LatLng(float(center_lat), float(center_lon)),
                            zoom=float(zoom),
                            bearing=0.0,
                            pitch=0.0,
                        ),
                    )
                ).result(timeout=timeout)
                self._map.request_still_image().result(timeout=timeout)
                readback = self._session.read_texture().result(timeout=timeout)
                data = bytes(readback.data)
            except TimeoutError as exc:
                raise SatelliteError("satellite tiles timed out") from exc
            except Exception as exc:  # noqa: BLE001 - FFI failures are display state
                raise SatelliteError(str(exc)) from exc
            info = readback.info
            image = QImage(
                data,
                info.width,
                info.height,
                info.stride,
                QImage.Format.Format_RGBA8888,
            )
            # QImage does not own the FFI buffer; copy before it is released.
            return image.copy()

    def close(self) -> None:
        with self._lock:
            self._release_graphics()
            if self._runtime is not None:
                try:
                    self._runtime.close()
                except Exception:  # noqa: BLE001 - best effort teardown
                    pass
            self._runtime = None

    def _acquire(self, width: int, height: int):
        import maplibre_native_ffi as module

        self._ffi = module
        if self._runtime is None:
            options = None
            if self._cache_dir:
                os.makedirs(self._cache_dir, exist_ok=True)
                options = module.RuntimeOptions(
                    cache_path=os.path.join(self._cache_dir, "ambient.db")
                )
            self._runtime = module.RuntimeHandle.create(options)
            if self._cache_dir:
                try:
                    self._runtime.set_maximum_ambient_cache_size(
                        256 * 1024 * 1024
                    ).result(timeout=5.0)
                except Exception:  # noqa: BLE001 - cache budget is best effort
                    pass
        if self._size != (width, height):
            self._release_graphics()
            created = self._runtime.create_map(
                module.MapOptions(
                    module.LogicalExtent(width, height, 1.0),
                    module.MapMode.STATIC,
                )
            )
            created.ready.result()
            self._map = created.map
            self._device = module.GraphicsDeviceHandle.create()
            attached = self._map.attach_graphics_device_texture(
                self._device,
                module.GraphicsDeviceTextureDescriptor(
                    module.LogicalExtent(width, height, 1.0)
                ),
                module.RenderSessionAttachOptions(module.RenderDriverKind.CORE_WORKER),
            )
            self._session = attached.session
            self._size = (width, height)
            self._style = None
        style = self._source.style()
        if style != self._style:
            self._map.set_style_json(style).result()
            self._style = style
        return module

    def _release_graphics(self) -> None:
        for name, close in (
            ("_session", lambda handle: handle.close()),
            ("_map", lambda handle: handle.close()),
            ("_device", lambda handle: handle.close()),
        ):
            handle = getattr(self, name)
            setattr(self, name, None)
            if handle is not None:
                try:
                    close(handle)
                except Exception:  # noqa: BLE001 - best effort teardown
                    pass
        self._size = None
        self._style = None


class SatelliteWorker(QObject):
    """Render frames on one dedicated thread, newest request wins.

    MapLibre owns a graphics device and a render thread, so every request for
    one source stays on the thread that created it. A burst of camera changes
    collapses to the latest frame; stale frames are still emitted and the
    viewport drops them by generation.
    """

    rendered = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, source: SatelliteSource, cache_dir: str = ""):
        super().__init__()
        self._source = source
        self._cache_dir = cache_dir
        self._condition = Condition()
        self._job = None
        self._stop = False
        self._thread = Thread(target=self._run, name="satellite", daemon=True)
        self._thread.start()

    def submit(self, generation: int, camera, scale: float = 1.0) -> None:
        with self._condition:
            self._job = (generation, camera, scale)
            self._condition.notify()

    def stop(self) -> None:
        with self._condition:
            self._stop = True
            self._condition.notify()

    def _run(self) -> None:
        renderer = None
        try:
            while True:
                with self._condition:
                    while self._job is None and not self._stop:
                        self._condition.wait()
                    if self._stop:
                        return
                    generation, camera, scale = self._job
                    self._job = None
                if renderer is None:
                    renderer = SatelliteRenderer(self._source, self._cache_dir)
                width = max(1, round(camera.width * scale))
                height = max(1, round(camera.height * scale))
                try:
                    image = renderer.render(
                        camera.center_lat,
                        camera.center_lon,
                        camera.zoom,
                        width,
                        height,
                    )
                except SatelliteError as exc:
                    self.failed.emit(generation, str(exc))
                else:
                    self.rendered.emit(generation, image)
        finally:
            if renderer is not None:
                renderer.close()
