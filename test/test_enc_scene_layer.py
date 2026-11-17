"""Deterministic checks for the opt-in GPU ENC scene layer.

The layer is still unwired, so these tests exercise its pure helpers, its
module-level assembly against the shared synthetic GPKG cell, and its
lifecycle with a stubbed worker composition.  Nothing here creates a Qt
object; the module is expected to stay importable without PySide6.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from sailworks.qt import encscenelayer
from sailworks.rendering import encplan, encview
from sailworks.rendering.vectordrawer import encscene
from test.enc_scene_fixtures import _CancelAfter, cell

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 15.0


class _FakePoint:
    """The ``get_degrees() -> (lat, lon)`` shape ``encplan._bounding`` reads."""

    def __init__(self, lat, lon):
        self._lat = lat
        self._lon = lon

    def get_degrees(self):
        return (self._lat, self._lon)


class _FakeViewport:
    """A viewport-shaped object: linear projection and a lon/lat bbox.

    The helpers keep ``MapViewport``'s conventions: ``_lon_to_x`` grows
    eastward and ``_lat_to_y`` grows southward (Mercator world pixels), the
    camera sits at the bbox centre, and the allocated size is the item's
    logical size.
    """

    def __init__(
        self, west=-1.0, south=-1.0, east=12.0, north=12.0, width=100, height=100
    ):
        self._west = west
        self._south = south
        self._east = east
        self._north = north
        self._width = float(width)
        self._height = float(height)
        self._center_lon = (west + east) / 2.0
        self._center_lat = (south + north) / 2.0

    def _lon_to_x(self, lon):
        return (np.asarray(lon, dtype=np.float64) - self._west) * 10.0

    def _lat_to_y(self, lat):
        return (self._north - np.asarray(lat, dtype=np.float64)) * 10.0

    def get_allocated_width(self):
        return self._width

    def get_allocated_height(self):
        return self._height

    def convert_geographic_to_screen_batch(self, lats, lons):
        """``MapViewport``'s batch screen projection, over the fake's linear map."""
        xs = self._lon_to_x(lons) - self._lon_to_x(self._center_lon) + self._width / 2.0
        ys = (
            self._lat_to_y(lats) - self._lat_to_y(self._center_lat) + self._height / 2.0
        )
        return xs, ys

    def get_bbox(self):
        return (
            _FakePoint(self._north, self._west),
            _FakePoint(self._south, self._east),
        )


class _Camera:
    """The camera surface the layer reads: attributes plus ``stamp()``."""

    def __init__(self, lon=5.5, lat=5.5, zoom=10.0, width=800, height=600, stamp=1.0):
        self.center_lon = lon
        self.center_lat = lat
        self.zoom = zoom
        self.width = width
        self.height = height
        self._stamp = stamp

    def stamp(self):
        return self._stamp


def _plan(path, count=1):
    version = encscene.cell_version(path)
    return tuple(
        encplan.CellPlan(
            rank_key=(0, 0),
            path=str(path),
            version=version,
            clip=None,
            coverage_known=False,
        )
        for _ in range(count)
    )


def _cache():
    return encscene.SceneCache(encscenelayer.SCENE_CACHE_BYTES)


def _assembled(path):
    scene = encscenelayer.assemble_scene(_plan(path), _FakeViewport(), _cache(), None)
    assert scene is not None
    assert not scene.is_empty
    return scene


def _metadata(camera, dpr=1):
    return {"camera": camera, "time": camera.stamp(), "dpr": dpr, "palette": None}


class _StubCatalog:
    """The catalog surface the layer reads, supplied by its caller."""

    def __init__(self, sources=(), class_enabled=True):
        self.sources = tuple(sources)
        self.refreshes = 0
        self._class_enabled = class_enabled

    @property
    def manifest(self):
        return SimpleNamespace(class_enabled=self._class_enabled)

    def refresh(self):
        self.refreshes += 1


def _layer(tmp_path, catalog=None, **kwargs):
    catalog = _StubCatalog() if catalog is None else catalog
    kwargs.setdefault("catalog_factory", lambda data_dir, palette: catalog)
    return encscenelayer.EncSceneLayer(tmp_path / "enc", None, **kwargs)


# --------------------------------------------------------------- pure helpers
@pytest.mark.parametrize(
    ("flags", "expected"),
    (
        ((False, False, False), (False, False, False)),
        ((True, False, False), (True, False, False)),
        # The raster ENC layer has always implied the chart layer, so the
        # chart flag is on even when it was not passed explicitly.
        ((False, True, False), (True, True, False)),
        ((False, False, True), (True, False, True)),
        ((True, True, True), (True, False, True)),
        ((1, 1, 0), (True, True, False)),
    ),
)
def test_resolve_enc_modes(flags, expected):
    assert encscenelayer.resolve_enc_modes(*flags) == expected


def test_scene_layer_supersedes_the_raster_enc_source():
    chart, enc_chart, scene = encscenelayer.resolve_enc_modes(
        chart_layer=False, enc_chart_layer=True, enc_scene_layer=True
    )
    assert (chart, enc_chart, scene) == (True, False, True)


def test_projection_for_maps_degrees_to_logical_pixels():
    project = encscenelayer.projection_for(
        _FakeViewport(west=0.0, south=0.0, east=10.0, north=10.0)
    )
    # Mesh positions are (lon, lat).  The 100x100 item is centred on (5, 5)
    # and its bbox spans it exactly, so the north-west corner is (0, 0) and
    # the south-east corner is the bottom-right pixel.
    assert project(np.array([[0.0, 10.0], [5.0, 5.0]])).tolist() == [
        [0.0, 0.0],
        [50.0, 50.0],
    ]
    assert project([(10.0, 0.0)]).tolist() == [[100.0, 100.0]]

    empty = project(np.empty((0, 2), dtype=np.float64))
    assert empty.shape == (0, 2)
    assert empty.dtype == np.float64


def test_published_projection_maps_the_camera_centre_to_the_item_centre(tmp_path):
    """The published projection is the item's own frame, not the padded plan.

    A 710x743 item has its camera centre at (355, 371.5) logical pixels.
    Publishing the padded planning viewport's projection put that same point
    at MARGIN (256) further right and down, which drew the whole chart off by
    256 logical pixels.
    """
    from sailworks.core.mapviewport import MapViewport
    from sailworks.rendering.overlayrenderer import MARGIN
    from sailworks.qt.gribviewportchart import ChartCamera

    camera = _Camera(lon=5.5, lat=5.5, zoom=10.0, width=710, height=743)
    layer = _layer(tmp_path)
    try:
        scene, metadata = layer._compose(camera, threading.Event())

        assert scene is None
        centre = metadata["projection"](np.array([[5.5, 5.5]], dtype=np.float64))
        assert np.allclose(centre, [[355.0, 371.5]], atol=1.0)

        # The same camera through the padded planning viewport projects to the
        # item centre plus the margin, which is the offset this dispatch fixes.
        padded = encscenelayer.projection_for(
            MapViewport(ChartCamera(camera, 1), MARGIN, lambda: None)
        )
        padded_centre = padded(np.array([[5.5, 5.5]], dtype=np.float64))
        assert np.allclose(padded_centre, centre + MARGIN, atol=1.0)
    finally:
        layer._release()


def test_compose_reuses_the_projection_for_an_unchanged_camera(tmp_path):
    """A re-composition at the same camera must not invalidate geometry.

    ``EncMeshItem.set_projection`` compares identity, and the projection is
    baked into the vertex buffer rather than applied as a uniform, so a fresh
    closure per composition forces a full re-upload of an unchanged view.
    """
    layer = _layer(tmp_path)
    camera = _Camera(width=710, height=743)
    try:
        _, first = layer._compose(camera, threading.Event())
        _, second = layer._compose(camera, threading.Event())
        _, moved = layer._compose(
            _Camera(lon=5.6, width=710, height=743), threading.Event()
        )

        assert second["projection"] is first["projection"]
        assert moved["projection"] is not first["projection"]
    finally:
        layer._release()


# ------------------------------------------------------------------ assembly
def test_assemble_scene_keeps_plan_order_and_vertex_offsets(cell):
    scene = encscenelayer.assemble_scene(
        _plan(cell, 2), _FakeViewport(), _cache(), None
    )
    assert scene is not None
    assert [span.path for span in scene.cells] == [str(cell), str(cell)]

    first, second = scene.cells
    assert first.first_vertex == 0
    assert first.vertex_count > 0
    assert second.first_vertex == first.vertex_count
    assert second.first_part == first.part_count
    assert len(scene.parts) == first.part_count + second.part_count
    assert scene.vertex_count == first.vertex_count + second.vertex_count
    assert all(part.first_vertex >= 0 for part in scene.parts)
    assert scene.skipped == ()
    # The fixture's point classes are supported features the area/line slice
    # cannot draw: they are reported, attributed and never silently dropped.
    assert {entry.path for entry in scene.unsupported} == {str(cell)}
    assert all(entry.reason for entry in scene.unsupported)
    assert scene.is_complete is False
    assert scene.accounted_bytes > 0


def test_assemble_scene_reports_skipped_cells_explicitly(cell):
    scene = encscenelayer.assemble_scene(
        _plan(cell), _FakeViewport(), encscene.SceneCache(1), None
    )
    assert scene is not None
    assert scene.is_empty
    assert [(skip.path, skip.reason) for skip in scene.skipped] == [
        (str(cell), "oversized")
    ]

    state, report = encscenelayer.scene_report(scene)
    assert state == encscenelayer.STATE_UNAVAILABLE
    assert report["reason"] == "no ENC geometry for this view"
    assert report["skipped"] == ((str(cell), "oversized"),)
    assert report["complete"] is False
    assert report["incomplete"] == encscenelayer.INCOMPLETE_LABELS


def test_assemble_scene_cancellation_returns_no_partial_scene(cell):
    assert (
        encscenelayer.assemble_scene(
            _plan(cell), _FakeViewport(), _cache(), None, cancelled=lambda: True
        )
        is None
    )
    assert (
        encscenelayer.assemble_scene(
            _plan(cell, 2),
            _FakeViewport(),
            _cache(),
            None,
            cancelled=_CancelAfter(2),
        )
        is None
    )


def test_scene_report_never_claims_a_complete_chart():
    state, report = encscenelayer.scene_report(None)
    assert state == encscenelayer.STATE_UNAVAILABLE
    assert report["complete"] is False
    assert report["incomplete"] == encscenelayer.INCOMPLETE_LABELS

    partial = encview.ViewScene(
        positions=np.zeros((3, 2), dtype=np.float64),
        parts=(),
        cells=(),
        unsupported=(object(),),
        skipped=(),
        accounted_bytes=0,
    )
    state, report = encscenelayer.scene_report(partial)
    assert state == encscenelayer.STATE_READY
    assert report["unsupported"] == 1
    assert report["cells"] == 0
    assert report["complete"] is False
    assert report["incomplete"] == encscenelayer.INCOMPLETE_LABELS


# ---------------------------------------------------------------- injection
def test_compose_reads_the_catalog_supplied_by_the_caller(tmp_path):
    catalog = _StubCatalog()
    layer = _layer(tmp_path, catalog=catalog)
    try:
        scene, metadata = layer._compose(_Camera(), threading.Event())

        assert scene is None
        assert metadata["reason"] == "no readable ENC cell is enabled"
        assert catalog.refreshes == 1
        assert layer._catalog is catalog
    finally:
        layer._release()


def test_compose_honours_the_injected_catalog_class_flag(tmp_path):
    catalog = _StubCatalog(class_enabled=False)
    layer = _layer(tmp_path, catalog=catalog)
    try:
        scene, metadata = layer._compose(_Camera(), threading.Event())

        assert scene is None
        assert metadata["reason"] == "ENC charts are disabled"
        assert catalog.refreshes == 1
    finally:
        layer._release()


def test_layer_without_a_catalog_source_reports_that_explicitly(tmp_path):
    layer = encscenelayer.EncSceneLayer(tmp_path / "enc", None)
    with pytest.raises(RuntimeError, match="no catalog source"):
        layer._compose(_Camera(), threading.Event())


# ----------------------------------------------------------------- lifecycle
def test_lifecycle_idle_loading_ready_then_reuse(tmp_path, cell):
    states = []
    published = []
    ready = threading.Event()

    def on_state(state):
        states.append(state)
        if state == encscenelayer.STATE_READY:
            ready.set()

    layer = _layer(tmp_path, on_state=on_state, on_publish=published.append)
    scene = _assembled(cell)
    camera = _Camera()
    calls = []
    gate = threading.Event()

    def compose(requested, cancel):
        calls.append(requested)
        gate.wait(TIMEOUT)
        return scene, _metadata(requested)

    layer._compose = compose
    try:
        assert layer.state == encscenelayer.STATE_IDLE
        generation = layer.request(camera)
        assert layer.state == encscenelayer.STATE_LOADING
        assert states == [encscenelayer.STATE_LOADING]
        gate.set()
        assert ready.wait(TIMEOUT)
        assert layer.state == encscenelayer.STATE_READY
        assert layer.error == ""
        assert layer.incomplete == encscenelayer.INCOMPLETE_LABELS
        assert calls == [camera]

        stored_generation, stored_scene, metadata = layer.published
        assert stored_generation == generation
        assert stored_scene is scene
        assert metadata["complete"] is False
        assert metadata["incomplete"] == encscenelayer.INCOMPLETE_LABELS
        assert metadata["cells"] == 1
        assert metadata["timings"]["assembly_ms"] >= 0.0
        assert published and published[0] is layer.published
        assert layer.observations()["generation"] == generation
        assert layer.counters["publications"] == 1
        assert states == [
            encscenelayer.STATE_LOADING,
            encscenelayer.STATE_READY,
        ]

        # The same camera reuses the published scene without new work.
        assert layer.request(camera) == generation
        assert calls == [camera]
        assert layer.counters["reuses"] == 1
    finally:
        gate.set()
        layer.close()
        layer._thread.join(TIMEOUT)


def test_observations_expose_residency_counters_before_any_compose(tmp_path):
    layer = encscenelayer.EncSceneLayer(tmp_path / "enc", None)
    try:
        observations = layer.observations()
        for key in (
            "mesh_triangulations",
            "mesh_hits",
            "mesh_misses",
            "scene_hits",
            "scene_misses",
            "store_hits",
            "store_misses",
        ):
            assert observations[key] == 0
        assert observations["counters"]["requests"] == 0
        assert observations["state"] == encscenelayer.STATE_IDLE
    finally:
        layer.close()


def test_cache_counters_snapshot_reads_each_worker_owned_cache():
    class _Cache:
        def __init__(self, hits, misses, triangulations=0):
            self.hits = hits
            self.misses = misses
            self.triangulations = triangulations

    counters = encscenelayer._cache_counters(
        _Cache(7, 2, 3), _Cache(4, 1), _Cache(5, 6)
    )
    assert counters == {
        "mesh_triangulations": 3,
        "mesh_hits": 7,
        "mesh_misses": 2,
        "scene_hits": 4,
        "scene_misses": 1,
        "store_hits": 5,
        "store_misses": 6,
    }


def test_unavailable_reports_the_composition_reason(tmp_path):
    failed = threading.Event()

    def on_state(state):
        if state == encscenelayer.STATE_UNAVAILABLE:
            failed.set()

    layer = _layer(tmp_path, on_state=on_state)
    camera = _Camera()
    layer._compose = lambda requested, cancel: (
        None,
        {"reason": "ENC charts are disabled", **_metadata(requested)},
    )
    try:
        layer.request(camera)
        assert failed.wait(TIMEOUT)
        assert layer.state == encscenelayer.STATE_UNAVAILABLE
        assert layer.error == "ENC charts are disabled"
        assert layer.published is None
        assert layer.incomplete == ""
        assert layer.counters["failures"] == 1
    finally:
        layer.close()
        layer._thread.join(TIMEOUT)


def test_exception_is_an_explicit_unavailable_state(tmp_path):
    failed = threading.Event()

    def on_state(state):
        if state == encscenelayer.STATE_UNAVAILABLE:
            failed.set()

    def compose(requested, cancel):
        raise RuntimeError("cannot open ENC cell")

    layer = _layer(tmp_path, on_state=on_state)
    layer._compose = compose
    try:
        layer.request(_Camera())
        assert failed.wait(TIMEOUT)
        assert layer.state == encscenelayer.STATE_UNAVAILABLE
        assert layer.error == "cannot open ENC cell"
        assert layer.published is None
    finally:
        layer.close()
        layer._thread.join(TIMEOUT)


def test_close_cancels_without_publishing_a_partial_scene(tmp_path):
    started = threading.Event()
    release = threading.Event()
    layer = _layer(tmp_path)
    camera = _Camera()

    def compose(requested, cancel):
        started.set()
        release.wait(TIMEOUT)
        return None, {"reason": "cancelled", **_metadata(requested)}

    layer._compose = compose
    try:
        layer.request(camera)
        assert started.wait(TIMEOUT)
        layer.close()
        release.set()
        layer._thread.join(TIMEOUT)
        assert not layer._thread.is_alive()
        assert layer.published is None
        assert layer.state != encscenelayer.STATE_READY
        assert layer.incomplete == ""
        assert layer.counters["cancelled"] >= 1
    finally:
        release.set()
        layer.close()


def test_close_releases_worker_owned_residency(tmp_path):
    failed = threading.Event()

    def on_state(state):
        if state == encscenelayer.STATE_UNAVAILABLE:
            failed.set()

    layer = _layer(tmp_path, on_state=on_state)
    layer._compose = lambda requested, cancel: (
        None,
        {"reason": "no readable ENC cell is enabled", **_metadata(requested)},
    )
    layer.request(_Camera())
    assert failed.wait(TIMEOUT)

    layer._catalog = object()
    layer._scene_cache = object()
    layer.close()
    layer._thread.join(TIMEOUT)
    assert not layer._thread.is_alive()
    assert layer._catalog is None
    assert layer._scene_cache is None


def test_changed_pixel_ratio_withdraws_the_published_scene(tmp_path, cell):
    ready = threading.Event()

    def on_state(state):
        if state == encscenelayer.STATE_READY:
            ready.set()

    layer = _layer(tmp_path, on_state=on_state)
    scene = _assembled(cell)
    layer._compose = lambda requested, cancel: (scene, _metadata(requested))
    try:
        layer.request(_Camera())
        assert ready.wait(TIMEOUT)
        assert layer.published is not None

        assert layer.set_device_pixel_ratio(2) is True
        assert layer.published is None
        assert layer.incomplete == ""
        assert layer.state == encscenelayer.STATE_IDLE
        assert layer.set_device_pixel_ratio(2) is False
    finally:
        layer.close()
        layer._thread.join(TIMEOUT)


def test_request_after_close_is_refused(tmp_path):
    layer = _layer(tmp_path)
    layer.close()
    assert layer.request(_Camera()) is None
    assert layer.state == encscenelayer.STATE_IDLE


# ------------------------------------------------- progressive publication
def test_partial_publication_is_loading_and_explicitly_incomplete(tmp_path, cell):
    published = []
    layer = _layer(tmp_path, on_publish=published.append)
    scene = _assembled(cell)
    try:
        with layer._condition:
            generation = layer._generation
        accepted = layer._publish_partial(generation, scene, 1, 2, threading.Event())
        assert accepted is True

        # Progress so far, never the current view: the layer stays loading and
        # says which cells it holds.
        assert layer.state == encscenelayer.STATE_LOADING
        assert "partial cells" in layer.incomplete
        assert encscenelayer.INCOMPLETE_LABELS in layer.incomplete
        assert layer._progress == (1, 2)

        stored_generation, stored_scene, metadata = layer.published
        assert stored_generation == generation
        assert stored_scene is scene
        assert metadata["complete"] is False
        assert metadata["partial"] is True
        assert metadata["progress"] == (1, 2)
        assert metadata["incomplete"] == layer.incomplete
        assert published and published[0] is layer.published

        observations = layer.observations()
        assert observations["progress"] == (1, 2)
        assert observations["partial_publications"] == 1
        assert observations["counters"]["partial_publications"] == 1
    finally:
        layer.close()


def test_partial_scene_is_never_reusable(tmp_path, cell):
    layer = _layer(tmp_path)
    scene = _assembled(cell)
    camera = _Camera()
    try:
        with layer._condition:
            generation = layer._generation
        layer._publish_partial(
            generation, scene, 1, 2, threading.Event(), _metadata(camera)
        )
        # It describes this camera, but it is not the finished view.
        assert layer._reusable(camera) is False
        # A final publication makes it reusable; the assembly state is what
        # distinguishes the two.
        with layer._condition:
            layer._state = encscenelayer.STATE_READY
        assert layer._reusable(camera) is True
    finally:
        layer.close()


def test_final_publication_is_ready_with_every_cell_drawn(tmp_path, cell):
    ready = threading.Event()

    def on_state(state):
        if state == encscenelayer.STATE_READY:
            ready.set()

    layer = _layer(tmp_path, on_state=on_state)
    scene = _assembled(cell)
    accepted = []

    def compose(requested, cancel):
        with layer._condition:
            generation = layer._generation
        accepted.append(layer._publish_partial(generation, scene, 1, 2, cancel))
        return scene, _metadata(requested)

    layer._compose = compose
    try:
        layer.request(_Camera())
        assert ready.wait(TIMEOUT)

        assert accepted == [True]
        assert layer.state == encscenelayer.STATE_READY
        assert layer.incomplete == encscenelayer.INCOMPLETE_LABELS
        assert "partial cells" not in layer.incomplete
        assert layer._progress == (1, 1)
        assert layer.counters["partial_publications"] == 1
        assert layer.counters["publications"] == 1
        _generation, _scene, metadata = layer.published
        assert "partial" not in metadata
        assert metadata["complete"] is False
    finally:
        layer.close()
        layer._thread.join(TIMEOUT)


def test_superseded_generation_withdraws_the_partial_scene(tmp_path, cell):
    partial_ready = threading.Event()
    release = threading.Event()
    hold = threading.Event()
    withdrawn = threading.Event()
    published = []

    def on_publish(payload):
        published.append(payload)
        # A withdrawal names the generation it withdraws; the scene slot is
        # empty and there is no unqualified ``None``.
        if payload is not None and payload[1] is None:
            withdrawn.set()

    layer = _layer(tmp_path, on_publish=on_publish)
    scene = _assembled(cell)
    camera = _Camera()
    other = _Camera(lon=6.0)

    def compose(requested, cancel):
        with layer._condition:
            generation = layer._generation
        if requested is camera:
            layer._publish_partial(generation, scene, 1, 2, cancel)
            partial_ready.set()
            release.wait(TIMEOUT)
            return scene, _metadata(requested)
        hold.wait(TIMEOUT)
        return None, {"reason": "held", **_metadata(requested)}

    layer._compose = compose
    try:
        first = layer.request(camera)
        assert partial_ready.wait(TIMEOUT)
        assert layer.published is not None
        assert layer.published[0] == first

        # A newer request supersedes the partial: the GUI must not keep it.
        assert layer.request(other) != first
        release.set()
        assert withdrawn.wait(TIMEOUT)
        assert layer.published is None
        # The withdrawal is qualified: it speaks only for the generation whose
        # partial it takes down.
        assert published[-1] == (first, None, None)
        assert layer.incomplete == ""
        assert layer._progress == (0, 0)
        assert layer.state == encscenelayer.STATE_LOADING
    finally:
        release.set()
        hold.set()
        layer.close()
        layer._thread.join(TIMEOUT)


# ---------------------------------------------------------------- Qt-freeness
def test_module_imports_without_qt():
    assert not hasattr(encscenelayer, "QQuickItem")
    assert not any(
        name in vars(encscenelayer) for name in ("QtCore", "QtGui", "QtQuick")
    )

    code = "\n".join(
        (
            "import sys",
            "import sailworks.qt.encscenelayer",
            "print('PySide6' in sys.modules)",
            "print('PySide2' in sys.modules)",
            "print('sailworks.qt.encgpu' in sys.modules)",
        )
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["False", "False", "False"], completed.stdout


def test_mesh_scale_maps_every_view_into_one_reference_frame():
    import numpy as np

    from sailworks.qt import encscenelayer

    points = np.array([[-123.0, 48.3], [-122.5, 48.6]])
    reference = points * (2.0**encscenelayer.encview.MESH_REFERENCE_ZOOM)
    # Every camera zoom, put through projection_for + mesh_scale_for, must land
    # on the same frame: that equality is what makes a zoom a cache hit.
    for zoom in (8.0, 11.5, 14.25, encscenelayer.encview.MESH_REFERENCE_ZOOM):
        current = points * (2.0**zoom)
        framed = current * encscenelayer.mesh_scale_for(zoom)
        np.testing.assert_allclose(framed, reference)


# ------------------------------------- ordered identity and explicit states
def test_cell_identity_records_the_plan_fields(cell):
    identity = encscenelayer.cell_identity(_plan(cell)[0])

    assert identity["path"] == str(cell)
    assert identity["name"] == cell.name
    assert identity["rank_key"] == (0, 0)
    assert identity["version"] == encscene.cell_version(cell)
    assert identity["coverage_known"] is False


def test_cell_states_name_every_planned_cell():
    planned = tuple(
        {"path": f"/cells/{name}.000", "name": f"{name}.000"} for name in "ABCD"
    )
    states = encscenelayer.cell_states(
        planned,
        (
            ("/cells/B.000", "unavailable"),
            ("/cells/C.000", "oversized"),
            ("/cells/D.000", "error: OSError: unreadable"),
        ),
    )

    assert [(state["name"], state["state"]) for state in states] == [
        ("A.000", "ready"),
        ("B.000", "unavailable"),
        ("C.000", "oversized"),
        ("D.000", "error"),
    ]
    assert states[0]["reason"] == ""
    assert states[2]["reason"] == "oversized"


def test_cell_states_mark_unresolved_cells_loading():
    planned = tuple(
        {"path": f"/cells/{name}.000", "name": f"{name}.000"} for name in "AB"
    )

    states = encscenelayer.cell_states(planned, (), done=1)

    assert [state["state"] for state in states] == ["ready", "loading"]


def test_partial_publication_publishes_the_ordered_plan(tmp_path, cell):
    published = []
    layer = _layer(tmp_path, on_publish=published.append)
    scene = _assembled(cell)
    camera = _Camera()
    try:
        with layer._condition:
            generation = layer._generation
        identity = encscenelayer.cell_identity(_plan(cell)[0])
        metadata = _metadata(camera)
        metadata["cells_order"] = (identity, identity)
        assert layer._publish_partial(
            generation, scene, 1, 2, threading.Event(), metadata
        )

        assert layer.cells_order == (identity, identity)
        assert [(state["state"], state["reason"]) for state in layer.cell_states] == [
            ("ready", ""),
            ("loading", ""),
        ]
        assert layer.observations()["cells_order"] == layer.cells_order
        assert layer.observations()["cell_states"] == layer.cell_states
        assert published and published[0] is layer.published
    finally:
        layer.close()


def test_changed_palette_withdraws_the_published_scene(tmp_path, cell):
    ready = threading.Event()
    states = []

    def on_state(state):
        states.append(state)
        if state == encscenelayer.STATE_READY:
            ready.set()

    layer = _layer(tmp_path, on_state=on_state)
    scene = _assembled(cell)
    calls = []

    def compose(requested, cancel):
        calls.append(requested)
        return scene, _metadata(requested)

    layer._compose = compose
    camera = _Camera()
    try:
        layer.request(camera)
        assert ready.wait(TIMEOUT)
        assert layer.state == encscenelayer.STATE_READY

        assert layer.set_palette("night") is True
        # Nothing published describes the new palette, so READY would be a
        # stale claim; the next demand composes at the new palette.
        assert layer.state == encscenelayer.STATE_IDLE
        assert layer.published is None
        assert layer.incomplete == ""
        assert layer._progress == (0, 0)
        assert states[-1] == encscenelayer.STATE_IDLE
        assert layer.set_palette("night") is False

        ready.clear()
        layer.request(camera)
        assert ready.wait(TIMEOUT)
        assert len(calls) == 2
        assert layer.state == encscenelayer.STATE_READY
    finally:
        layer.close()
        layer._thread.join(TIMEOUT)


def test_assemble_scene_forwards_the_timings_mapping(monkeypatch):
    """The composition's accumulator reaches build_view_scene unchanged."""
    seen = {}

    def fake_build(plan, scene_cache, **kwargs):
        seen.update(kwargs)
        return "scene"

    monkeypatch.setattr(encscenelayer.encview, "build_view_scene", fake_build)
    monkeypatch.setattr(encscenelayer, "projection_for", lambda viewport: None)
    timings = {}
    assert (
        encscenelayer.assemble_scene([], object(), None, None, timings=timings)
        == "scene"
    )
    assert seen["timings"] is timings


def test_stored_loader_threads_timings_into_store_and_ogr(monkeypatch):
    """The store and a fresh OGR read write into the caller's accumulator."""
    seen = {}

    class _Store:
        def load_or_build(self, path, version, loader, timings=None):
            seen["store_timings"] = timings
            return loader()

    def fake_open(path, version, cancelled=None, timings=None):
        seen["ogr_timings"] = timings
        return "scene"

    monkeypatch.setattr(encscenelayer.encscene, "open_cell_scene", fake_open)
    timings = {}
    loader = encscenelayer._stored_loader(_Store(), timings)
    assert loader("cell.000", (1, 2, 3, 4)) == "scene"
    assert seen["store_timings"] is timings
    assert seen["ogr_timings"] is timings
