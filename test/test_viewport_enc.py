"""The viewport's ENC chart layer: the application's own ENC, faithfully.

These checks are model-level and headless: they need neither a GPU nor a
window.  The ENC cells are composed by the application's own renderer, and
the synthetic cells below stand in for real exchange sets: the selection,
the coverage clipping and the portrayal are the reference's, unchanged.
"""

import ast
import inspect
import json
import time
from pathlib import Path
from threading import Event

import cairo
from osgeo import ogr

from gweatherrouting.core.enccharts import chart_scale_rank, chart_scale_visible
from gweatherrouting.core.mapviewport import MapViewport
from gweatherrouting.qt import gribviewport, gribviewportchart, viewportenc
from gweatherrouting.qt.gribviewport import ViewportCamera
from gweatherrouting.rendering.encrenderer import ENCOverlayRenderer
from gweatherrouting.rendering.overlayrenderer import RenderJob
from test.map_helpers import MapCanvas

#: The modules the viewport's own code is made of.
PRODUCT_MODULES = (viewportenc, gribviewportchart, gribviewport)

#: The modules the chart worker runs in: they never see the application's
#: global settings or its controllers.
WORKER_MODULES = (viewportenc, gribviewportchart)

#: The application's own settings and controllers: the worker never uses them.
FORBIDDEN_IMPORTS = (
    "gweatherrouting.core.settings",
    "gweatherrouting.core.storage",
    "gweatherrouting.qt.datacontroller",
    "gweatherrouting.qt.datamanager",
    "gweatherrouting.rendering.chartcollection",
)


# --------------------------------------------------------------- helpers
def _camera(**overrides):
    values = dict(
        center_lat=47.6,
        center_lon=-122.4,
        zoom=12.0,
        width=400,
        height=300,
    )
    values.update(overrides)
    return ViewportCamera(**values)


def _settings(data_dir, entries=(), **extra):
    """The user's own settings file, with the user's own chart entries."""
    payload = {"vectorCharts": list(entries), "chartENCVisible": True}
    payload.update(extra)
    (Path(data_dir) / "settings.json").write_text(json.dumps(payload))


def _entry(path, enabled=True):
    return {"path": path, "enabled": enabled, "metadata": {}}


def _enc_job(charts=(), canvas=None):
    """A render job for the application's own ENC renderer."""
    canvas = canvas if canvas is not None else MapCanvas()
    viewport = MapViewport(canvas)
    job = RenderJob((), viewport, tuple(charts), 1, "cm93")
    viewport.render_cancelled = job.cancel.is_set
    return job


def _surface(width=100, height=100):
    return cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)


def _pixel(surface, x, y):
    """The surface's pixel at (x, y), as (red, green, blue, alpha)."""
    surface.flush()
    offset = y * surface.get_stride() + x * 4
    blue, green, red, alpha = bytes(surface.get_data())[offset : offset + 4]
    return (red, green, blue, alpha)


def _rectangle(viewport, left, top, right, bottom):
    """A screen rectangle, as the geographic polygon the reference expects."""
    points = [
        viewport.convert_screen_to_geographic(x, y).get_degrees()
        for x, y in (
            (left, top),
            (right, top),
            (right, bottom),
            (left, bottom),
            (left, top),
        )
    ]
    return ogr.CreateGeometryFromWkt(
        "POLYGON((" + ",".join(f"{lon} {lat}" for lat, lon in points) + "))"
    )


class _FakeChart:
    """A stand-in for an exchange set, drawn by the reference route.

    The coverage is the cell's own; the colour and the label are the
    cell's portrayal.  The clip is the reference's, so a cell never draws
    outside the region it was given.
    """

    def __init__(self, coverage, color, label_box=None, label_color=(1, 1, 1)):
        self._coverage = list(coverage)
        self._color = color
        self._label_box = label_box
        self._label_color = label_color
        self.drawn = 0

    def do_draw(self, viewport, cr):
        self.drawn += 1
        cr.set_source_rgb(*self._color)
        cr.paint()
        if self._label_box is not None:
            cr.set_source_rgb(*self._label_color)
            cr.rectangle(*self._label_box)
            cr.fill()


class _StubRenderer:
    """A stand-in for the reference renderer, with a chosen outcome."""

    def __init__(self, surface=None, regions=(), error=None):
        self._surface = surface
        self._regions = tuple(regions)
        self._error = error
        self.released = False

    def _render(self, job):
        if self._error is not None:
            raise self._error
        return self._surface

    @property
    def regions(self):
        return self._regions

    def release(self):
        self.released = True


class _StubCatalog:
    """A stand-in for the catalog: the sources are given, not read."""

    def __init__(self, manifest, sources=(), disabled=()):
        self._manifest = manifest
        self._sources = tuple(sources)
        self._disabled = tuple(disabled)

    def refresh(self):
        return self

    @property
    def manifest(self):
        return self._manifest

    @property
    def sources(self):
        return self._sources

    @property
    def disabled(self):
        return self._disabled


def _stub_catalog(sources=(), disabled=(), class_enabled=True):
    entries = tuple((source.path, True) for source in sources)
    entries += tuple((path, False) for path in disabled)
    manifest = viewportenc.Manifest(
        entries=entries,
        class_enabled=class_enabled,
        generation=tuple((path, enabled) for path, enabled in entries),
    )
    return _StubCatalog(manifest, sources, disabled)


class _FakeImage:
    """A stand-in for the published image, with the image's own size."""

    def __init__(self, width, height):
        self._width = width
        self._height = height

    def width(self):
        return self._width

    def height(self):
        return self._height


def _chart_metadata(camera, factor=1):
    viewport = MapViewport(
        gribviewportchart.ChartCamera(camera, factor), 256, lambda: None
    )
    return {
        "viewport": viewport,
        "time": camera.stamp(),
        "factor": factor,
    }


def _chart_layer(tmp_path, **kwargs):
    return gribviewportchart.ViewportChartLayer(paths=(), data_dir=tmp_path, **kwargs)


def _wait_for_publication(layer, published, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not published and time.monotonic() < deadline:
        time.sleep(0.01)
    assert published, "the chart layer never published"


# -------------------------------------------------------------- manifest
def test_the_manifest_reads_the_installed_cells_and_the_users_own_state(tmp_path):
    """The installed cells and their enabled state are the user's own."""
    _settings(
        tmp_path,
        [
            _entry("/charts/US5OAKFF.000"),
            _entry("/charts/US5OAKFG.000", enabled=False),
            _entry("/charts/coastline.geojson"),
        ],
        chartPalette="dark",
    )
    manifest = viewportenc.read_manifest(tmp_path)
    assert [path for path, _ in manifest.enc_entries] == [
        "/charts/US5OAKFF.000",
        "/charts/US5OAKFG.000",
    ]
    assert [path for path, _ in manifest.enabled] == ["/charts/US5OAKFF.000"]
    assert [path for path, _ in manifest.disabled] == ["/charts/US5OAKFG.000"]
    assert manifest.class_enabled is True
    assert manifest.palette == "dark"
    assert manifest.error == ""


def test_the_manifest_never_reads_a_missing_settings_file_as_no_charts(tmp_path):
    """A missing settings file is reported, never read as an empty library."""
    manifest = viewportenc.read_manifest(tmp_path)
    assert manifest.entries == ()
    assert manifest.error


def test_a_broken_settings_file_is_reported_not_swallowed(tmp_path):
    (tmp_path / "settings.json").write_text("{not json")
    manifest = viewportenc.read_manifest(tmp_path)
    assert manifest.entries == ()
    assert "unreadable" in manifest.error


def test_the_enc_switch_is_the_applications_own_switch(tmp_path):
    """The class switch is the application's own key, with its own meaning."""
    from gweatherrouting.rendering.chartclasses import CLASS_SETTINGS

    assert viewportenc.CLASS_KEY == CLASS_SETTINGS["enc"]
    _settings(tmp_path, [], chartENCVisible=False)
    assert viewportenc.read_manifest(tmp_path).class_enabled is False


def test_the_manifest_generation_follows_the_files_own_identity(tmp_path):
    """A replaced or switched off cell invalidates what described it."""
    chart = tmp_path / "US5TEST.000"
    chart.write_bytes(b"cell")
    _settings(tmp_path, [_entry(str(chart))])
    first = viewportenc.read_manifest(tmp_path)
    assert first.generation == viewportenc.read_manifest(tmp_path).generation
    chart.write_bytes(b"a replaced cell")
    replaced = viewportenc.read_manifest(tmp_path)
    assert replaced.generation != first.generation
    _settings(tmp_path, [_entry(str(chart), enabled=False)])
    disabled = viewportenc.read_manifest(tmp_path)
    assert disabled.generation != replaced.generation


def test_the_class_switch_is_never_stale(tmp_path):
    """The user's own switch is read again, never kept from the last read."""
    _settings(tmp_path, [_entry("/charts/US5TEST.000")], chartENCVisible=True)
    catalog = viewportenc.EncCatalog(tmp_path).refresh()
    assert catalog.manifest.class_enabled is True
    _settings(tmp_path, [_entry("/charts/US5TEST.000")], chartENCVisible=False)
    catalog.refresh()
    assert catalog.manifest.class_enabled is False


def test_an_unreadable_cell_is_reported_and_never_pruned(tmp_path):
    """A cell that cannot be read keeps its place and reports why."""
    broken = tmp_path / "US5BROKE.000"
    broken.write_bytes(b"not a chart")
    _settings(tmp_path, [_entry(str(broken))])
    catalog = viewportenc.EncCatalog(tmp_path).refresh()
    assert [source.name for source in catalog.sources] == ["US5BROKE"]
    assert catalog.sources[0].error
    assert catalog.sources[0].native_scale == 0


# ------------------------------------------------------- the descriptors
def test_the_source_descriptor_is_the_one_the_reference_renderer_expects():
    """The worker hands the reference the descriptors it already reads."""
    source = viewportenc.EncSource(
        "/charts/US5TEST.000", 45000, ((1.0, 2.0, 3.0, 4.0),)
    )
    assert source.descriptor == (
        "/charts/US5TEST.000",
        45000,
        ((1.0, 2.0, 3.0, 4.0),),
    )
    assert source.name == "US5TEST"
    assert viewportenc.enc_cell_name("/charts/US5TEST.000") == "US5TEST"


def test_the_cell_closest_to_the_display_scale_is_drawn_first():
    """The ranking is the application's own rule, unchanged."""
    canvas = MapCanvas()
    scale = canvas.get_scale()
    native = int(scale * 3779.527559)
    job = _enc_job((("overview", native * 4, ()), ("harbor", native, ())), canvas)
    ranked = viewportenc.ViewportEncRenderer._ranked(job)
    assert [chart[0] for chart in ranked] == ["harbor", "overview"]
    assert chart_scale_rank(native, scale) < chart_scale_rank(native * 4, scale)


def test_equal_scales_are_ordered_by_path():
    """Two cells at the same scale are ordered the way the app orders them."""
    canvas = MapCanvas()
    native = int(canvas.get_scale() * 3779.527559)
    job = _enc_job((("US5B", native, ()), ("US5A", native, ())), canvas)
    ranked = viewportenc.ViewportEncRenderer._ranked(job)
    assert [chart[0] for chart in ranked] == ["US5A", "US5B"]


def test_a_cell_outside_the_visibility_rule_is_never_selected():
    """The scale visibility filter is the application's own, unchanged."""
    canvas = MapCanvas()
    scale = canvas.get_scale()
    visible = int(scale * 3779.527559)
    hidden = int(visible / 32)
    assert chart_scale_visible(visible, scale)
    assert not chart_scale_visible(hidden, scale)
    renderer = viewportenc.ViewportEncRenderer()
    assert renderer._regions_of(_enc_job((("harbor", visible, ()),), canvas))
    assert renderer._regions_of(_enc_job((("harbor", hidden, ()),), canvas)) == ()


# ------------------------------------------------- the reference's pixels
def _render_pair(job, factory, monkeypatch):
    """The reference renderer's pixels and the viewport's own pixels.

    The same synthetic cells are drawn twice: once by the application's
    reference renderer and once by the viewport's, through the same route.
    The pixels must be identical.
    """
    charts = factory()

    def worker_chart(self, path, native_scale, palette):
        return charts[path]

    monkeypatch.setattr(ENCOverlayRenderer, "_worker_chart", worker_chart)
    reference = ENCOverlayRenderer()._render(job)
    charts = factory()
    surface = viewportenc.ViewportEncRenderer()._render(job)
    return reference, surface


def test_the_viewport_pixels_are_the_reference_pixels(monkeypatch):
    """The viewport draws the reference's pixels, not its own."""
    canvas = MapCanvas()
    viewport = MapViewport(canvas)
    native = int(canvas.get_scale() * 3779.527559)
    full = _rectangle(viewport, -10, -10, 410, 310)
    hole = _rectangle(viewport, 160, 110, 240, 190)

    def factory():
        return {
            "coast": _FakeChart([full.Difference(hole)], (1, 0, 0)),
            "harbor": _FakeChart([full], (0, 0, 1)),
        }

    job = _enc_job((("coast", native, ()), ("harbor", native // 8, ())), canvas)
    reference, surface = _render_pair(job, factory, monkeypatch)
    assert bytes(surface.get_data()) == bytes(reference.get_data())


def test_the_best_scale_wins_the_overlap_and_the_other_cell_fills_the_gap(
    monkeypatch,
):
    """The overlap belongs to the closer scale; the rest to the other cell."""
    canvas = MapCanvas()
    viewport = MapViewport(canvas)
    native = int(canvas.get_scale() * 3779.527559)

    def factory():
        return {
            "coast": _FakeChart([_rectangle(viewport, 0, 0, 280, 300)], (1, 0, 0)),
            "harbor": _FakeChart([_rectangle(viewport, 120, 0, 400, 300)], (0, 0, 1)),
        }

    job = _enc_job((("harbor", native // 8, ()), ("coast", native, ())), canvas)
    _reference, surface = _render_pair(job, factory, monkeypatch)
    assert _pixel(surface, 200, 150) == (255, 0, 0, 255)
    assert _pixel(surface, 350, 150) == (0, 0, 255, 255)


def test_a_coverage_hole_is_filled_by_the_fallback_and_drawn_only_once(
    monkeypatch,
):
    """A no-coverage panel is the other cell's, never both cells'."""
    canvas = MapCanvas()
    viewport = MapViewport(canvas)
    native = int(canvas.get_scale() * 3779.527559)
    full = _rectangle(viewport, -10, -10, 410, 310)
    region = full.Difference(_rectangle(viewport, 160, 110, 240, 190))

    def factory():
        return {
            "coast": _FakeChart([region], (1, 0, 0)),
            "harbor": _FakeChart([full], (0, 0, 1)),
        }

    job = _enc_job((("coast", native, ()), ("harbor", native // 8, ())), canvas)
    _reference, surface = _render_pair(job, factory, monkeypatch)
    assert _pixel(surface, 100, 150) == (255, 0, 0, 255)
    assert _pixel(surface, 200, 150) == (0, 0, 255, 255)


def test_a_fully_covered_cell_is_never_drawn(monkeypatch):
    """A cell whose region is empty contributes nothing to the picture."""
    canvas = MapCanvas()
    viewport = MapViewport(canvas)
    native = int(canvas.get_scale() * 3779.527559)
    full = _rectangle(viewport, -10, -10, 410, 310)
    charts = {}

    def factory():
        charts.clear()
        charts.update(
            {
                "coast": _FakeChart([full], (1, 0, 0)),
                "harbor": _FakeChart([full], (0, 0, 1)),
            }
        )
        return charts

    job = _enc_job((("coast", native, ()), ("harbor", native // 8, ())), canvas)
    _render_pair(job, factory, monkeypatch)
    assert charts["coast"].drawn == 1
    assert charts["harbor"].drawn == 0


def test_the_labels_of_a_cell_are_retained_outside_the_better_cell(monkeypatch):
    """A label survives where its cell is drawn and is clipped where not."""
    canvas = MapCanvas()
    viewport = MapViewport(canvas)
    native = int(canvas.get_scale() * 3779.527559)

    def factory():
        return {
            "coast": _FakeChart(
                [_rectangle(viewport, 0, 0, 200, 300)],
                (1, 0, 0),
                label_box=(20, 140, 40, 160),
            ),
            "harbor": _FakeChart(
                [_rectangle(viewport, 0, 0, 400, 300)],
                (0, 0, 1),
                label_box=(300, 140, 320, 160),
            ),
        }

    job = _enc_job((("coast", native, ()), ("harbor", native // 8, ())), canvas)
    _reference, surface = _render_pair(job, factory, monkeypatch)
    # The label of the cell that owns the ground is drawn.
    assert _pixel(surface, 30, 150) == (255, 255, 255, 255)
    # The label of the other cell is drawn where that cell is drawn.
    assert _pixel(surface, 310, 150) == (255, 255, 255, 255)
    # The better cell owns the overlap, and the other cell's label is not
    # drawn there.
    assert _pixel(surface, 100, 150) == (255, 0, 0, 255)


def test_the_viewport_reports_the_cells_it_selected_and_their_coverage(
    monkeypatch,
):
    """The reported identity is the composition that was drawn."""
    canvas = MapCanvas()
    viewport = MapViewport(canvas)
    native = int(canvas.get_scale() * 3779.527559)

    def factory():
        return {
            "coast": _FakeChart([_rectangle(viewport, 0, 0, 200, 300)], (1, 0, 0)),
            "harbor": _FakeChart([_rectangle(viewport, 0, 0, 400, 300)], (0, 0, 1)),
        }

    charts = factory()

    def worker_chart(self, path, native_scale, palette):
        return charts[path]

    monkeypatch.setattr(ENCOverlayRenderer, "_worker_chart", worker_chart)
    renderer = viewportenc.ViewportEncRenderer()
    renderer._render(
        _enc_job((("coast", native, ()), ("harbor", native // 8, ())), canvas)
    )
    regions = renderer.regions
    assert [path for path, _region in regions] == ["coast", "harbor"]
    coast, harbor = regions[0][1], regions[1][1]
    # The two regions are disjoint and together they cover the view.
    assert coast.Intersection(harbor).GetArea() < 1e-9
    assert coast.Union(harbor).GetArea() > 0


def test_the_coverage_identity_is_the_region_that_was_drawn():
    """The identity of a cell's region is geometric, never a pixel claim."""
    region = ogr.CreateGeometryFromWkt("POLYGON((0 0,1 0,1 1,0 1,0 0))")
    identity = viewportenc.region_identity("/charts/US5TEST.000", region)
    assert identity["name"] == "US5TEST"
    assert identity["envelope"] == (0.0, 1.0, 0.0, 1.0)
    assert identity["area"] == 1.0
    assert len(identity["wkt_sha256"]) == 64


# ------------------------------------------------------------ the states
def _enc_source(sources=(), disabled=(), class_enabled=True, renderer=None):
    source = viewportenc.ViewportEncSource("/unused", "cm93")
    source._catalog = _stub_catalog(sources, disabled, class_enabled)
    if renderer is not None:
        source._renderer_instance = lambda: renderer
    return source


def test_the_enc_class_switch_disables_the_source():
    """The user's own switch turns the source off, and says so."""
    source = _enc_source(class_enabled=False)
    surface = _surface()
    result, metadata = source.compose(_enc_job(), surface)
    assert result is surface
    assert metadata["state"] == viewportenc.STATE_DISABLED
    assert metadata["reason"] == viewportenc.REASON_DISABLED
    assert metadata["enabled"] is False
    assert source.state == viewportenc.STATE_DISABLED


def test_no_enabled_cell_is_not_a_coverage_miss():
    """Nothing enabled is a disabled source, not a coverage miss."""
    source = _enc_source(disabled=("US5TEST",))
    _result, metadata = source.compose(_enc_job(), _surface())
    assert metadata["state"] == viewportenc.STATE_DISABLED
    assert metadata["reason"] == viewportenc.REASON_NO_CELLS
    assert metadata["enabled"] is False
    assert metadata["disabled_ids"] == ("US5TEST",)


def test_a_cell_that_does_not_cover_the_view_is_an_explicit_miss():
    """A coverage miss is reported, never presented as an empty chart."""
    cell = viewportenc.EncSource("/charts/US5TEST.000", 45000, ((-1, -1, 1, 1),))
    renderer = _StubRenderer(surface=_surface())
    source = _enc_source(sources=(cell,), renderer=renderer)
    surface = _surface()
    result, metadata = source.compose(_enc_job(), surface)
    assert result is surface
    assert metadata["state"] == viewportenc.STATE_COVERAGE_MISS
    assert metadata["reason"] == viewportenc.REASON_NO_COVERAGE
    assert metadata["selected"] == ()
    assert metadata["source_ids"] == ("US5TEST",)


def test_a_cell_that_cannot_be_drawn_is_never_a_chart():
    """A broken cell leaves the chart alone and says why."""
    cell = viewportenc.EncSource("/charts/US5TEST.000", 45000, ())
    renderer = _StubRenderer(error=ValueError("cannot draw"))
    source = _enc_source(sources=(cell,), renderer=renderer)
    surface = _surface()
    result, metadata = source.compose(_enc_job(), surface)
    assert result is surface
    assert metadata["state"] == viewportenc.STATE_UNAVAILABLE
    assert metadata["reason"] == viewportenc.REASON_UNREADABLE
    assert "cannot draw" in metadata["error"]
    assert _pixel(result, 50, 50) == (0, 0, 0, 0)


def test_a_cancelled_composition_is_never_a_chart():
    """A cancelled composition reports itself and draws nothing."""
    cell = viewportenc.EncSource("/charts/US5TEST.000", 45000, ())
    renderer = _StubRenderer(surface=None)
    source = _enc_source(sources=(cell,), renderer=renderer)
    surface = _surface()
    result, metadata = source.compose(_enc_job(), surface)
    assert result is surface
    assert metadata["state"] == viewportenc.STATE_LOADING
    assert metadata["reason"] == viewportenc.REASON_CANCELLED
    assert _pixel(result, 50, 50) == (0, 0, 0, 0)


def test_a_ready_composition_reports_its_cells_and_its_coverage():
    """A drawn composition reports what it drew, in the worker's own terms."""
    cell = viewportenc.EncSource("/charts/US5TEST.000", 45000, ())
    region = ogr.CreateGeometryFromWkt("POLYGON((0 0,1 0,1 1,0 1,0 0))")
    painted = _surface()
    context = cairo.Context(painted)
    context.set_source_rgb(0, 0, 1)
    context.paint()
    renderer = _StubRenderer(surface=painted, regions=((cell.path, region),))
    source = _enc_source(sources=(cell,), renderer=renderer)
    surface = _surface()
    result, metadata = source.compose(_enc_job(), surface)
    assert result is surface
    assert metadata["state"] == viewportenc.STATE_READY
    assert metadata["enabled"] is True
    assert metadata["selected"] == ("US5TEST",)
    assert metadata["coverage"][0]["name"] == "US5TEST"
    assert metadata["factor"] == 1
    assert metadata["bytes"] > 0
    # The ENC is painted onto the chart surface, in the application's order.
    assert _pixel(result, 50, 50) == (0, 0, 255, 255)


def test_an_unreadable_installed_cell_is_reported_by_the_catalog(tmp_path):
    """A cell that cannot be opened is named, never dropped in silence."""
    broken = tmp_path / "US5BROKE.000"
    broken.write_bytes(b"not a chart")
    _settings(tmp_path, [_entry(str(broken))])
    source = viewportenc.ViewportEncSource(tmp_path, "cm93")
    _result, metadata = source.compose(_enc_job(), _surface())
    assert metadata["state"] == viewportenc.STATE_UNAVAILABLE
    assert metadata["failed"]
    assert metadata["failed"][0][0] == "US5BROKE"


def test_an_unreadable_manifest_is_never_an_empty_chart(tmp_path):
    """A manifest that cannot be read is unavailable, never "no cells"."""
    (tmp_path / "settings.json").write_text("{not json")
    source = viewportenc.ViewportEncSource(tmp_path, "cm93")
    _result, metadata = source.compose(_enc_job(), _surface())
    assert metadata["state"] == viewportenc.STATE_UNAVAILABLE
    assert metadata["reason"] == viewportenc.REASON_UNREADABLE
    assert "unreadable" in metadata["error"]


def test_a_missing_manifest_is_a_disabled_source_not_a_failure(tmp_path):
    """A user with no settings file has no cells, and that is not a failure."""
    source = viewportenc.ViewportEncSource(tmp_path, "cm93")
    _result, metadata = source.compose(_enc_job(), _surface())
    assert metadata["state"] == viewportenc.STATE_DISABLED
    assert metadata["reason"] == viewportenc.REASON_NO_CELLS
    assert metadata["error"] == ""


# ----------------------------------------------------------------- layer
def test_the_enc_layer_is_off_by_default(tmp_path):
    """The experimental layer is off until it is asked for."""
    layer = _chart_layer(tmp_path)
    try:
        assert layer._enc_enabled is False
        assert layer.enc_state == "disabled"
        assert layer.enc_metadata == {}
        assert layer.enc_status() == ""
        assert layer.observations()["enc"]["enabled"] is False
    finally:
        layer.close()


def test_the_viewport_never_enables_the_enc_layer_by_itself():
    """The widget and the command line keep the layer behind a flag."""
    widget = gribviewport._viewport_widget_class()
    defaults = {
        name: argument.default
        for name, argument in inspect.signature(widget.__init__).parameters.items()
        if argument.default is not inspect.Parameter.empty
    }
    assert defaults["enc_chart_layer"] is False
    assert defaults["chart_layer"] is False
    tree = ast.parse(inspect.getsource(gribviewport))
    flags = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "add_argument"
        ):
            for keyword in node.keywords:
                if (
                    keyword.arg == "action"
                    and getattr(keyword.value, "value", "") == "store_true"
                ):
                    flags.append(node.args[0].value)
    assert "--enc-chart-layer" in flags


def test_the_chart_palette_is_the_users_own(tmp_path):
    """The palette is the user's own, and an unknown name falls back."""
    from gweatherrouting.rendering.style import Style

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"chartPalette": "dark"}))
    assert gribviewport.saved_chart_palette(tmp_path) == "dark"
    settings.write_text(json.dumps({"chartPalette": "not-a-palette"}))
    assert gribviewport.saved_chart_palette(tmp_path) == "cm93"
    assert gribviewport.saved_chart_palette(tmp_path / "missing") == "cm93"
    assert "dark" in Style.chart_palettes


def test_the_composition_is_made_at_the_gui_device_pixel_ratio():
    """The camera presents the GUI's own ratio to the chart code."""
    camera = _camera()
    assert gribviewportchart.ChartCamera(camera, 1).get_scale_factor() == 1
    assert gribviewportchart.ChartCamera(camera, 2).get_scale_factor() == 2


def test_a_ratio_change_withdraws_the_published_composition(tmp_path):
    """A composition made at another ratio is not current, and is not reused."""
    published = []
    layer = _chart_layer(tmp_path, on_publish=published.append)
    layer._compose = lambda camera, cancel: (
        _FakeImage(10, 10),
        _chart_metadata(camera),
    )
    camera = _camera()
    layer.request(camera)
    _wait_for_publication(layer, published)
    assert layer.observations()["payload_bytes"] == 10 * 10 * 4
    generation = layer.observations()["generation"]
    assert layer.set_device_pixel_ratio(2) is True
    assert layer._published is None
    assert layer.observations()["payload_bytes"] == 0
    layer.request(camera)
    assert layer.observations()["generation"] != generation
    assert layer.counters["reuses"] == 0


def test_a_zoom_change_never_reuses_the_previous_scale(tmp_path):
    """A different scale is a new generation, never a resampled frame."""
    published = []
    layer = _chart_layer(tmp_path, on_publish=published.append)
    layer._compose = lambda camera, cancel: (
        _FakeImage(10, 10),
        _chart_metadata(camera),
    )
    camera = _camera()
    layer.request(camera)
    _wait_for_publication(layer, published)
    generation = layer.observations()["generation"]
    layer.request(_camera(zoom=camera.zoom + 0.5))
    assert layer.observations()["generation"] != generation
    assert layer.counters["reuses"] == 0


def test_a_pan_at_the_same_scale_is_retained(tmp_path):
    """A pan at the same scale and ratio keeps the composition."""
    published = []
    layer = _chart_layer(tmp_path, on_publish=published.append)
    layer._compose = lambda camera, cancel: (
        _FakeImage(10, 10),
        _chart_metadata(camera),
    )
    camera = _camera()
    layer.request(camera)
    _wait_for_publication(layer, published)
    generation = layer.observations()["generation"]
    layer.request(_camera(center_lon=camera.center_lon + 0.0001))
    assert layer.observations()["generation"] == generation
    assert layer.counters["reuses"] == 1


def test_the_enc_state_and_metadata_are_published_with_the_chart(tmp_path):
    """The ENC's own report travels with the composition it belongs to."""
    published = []
    layer = _chart_layer(tmp_path, on_publish=published.append, enc_enabled=True)

    def compose(camera, cancel):
        metadata = _chart_metadata(camera)
        metadata["enc"] = {
            "enabled": True,
            "state": "ready",
            "reason": "",
            "source_ids": ("US5TEST",),
            "selected": ("US5TEST",),
            "coverage": (),
            "failed": (),
            "factor": 1,
            "generation": "abc",
            "bytes": 128,
        }
        return _FakeImage(10, 10), metadata

    layer._compose = compose
    layer.request(_camera())
    _wait_for_publication(layer, published)
    assert layer.enc_state == "ready"
    assert layer.enc_metadata["selected"] == ("US5TEST",)
    assert layer.observations()["enc"]["state"] == "ready"
    assert layer.observations()["enc"]["metadata"]["source_ids"] == ("US5TEST",)
    assert layer.counters["enc_compositions"] == 1
    assert layer.counters["enc_cells"] == 1


def test_a_superseded_enc_generation_never_publishes(tmp_path):
    """A generation that is replaced while composing never publishes."""
    published = []
    layer = _chart_layer(tmp_path, on_publish=published.append, enc_enabled=True)
    entered, release = Event(), Event()

    def compose(camera, cancel):
        entered.set()
        release.wait(5)
        return _FakeImage(10, 10), _chart_metadata(camera)

    layer._compose = compose
    camera = _camera()
    first = layer.request(camera)
    assert entered.wait(5)
    second = layer.request(camera, replace=True)
    release.set()
    _wait_for_publication(layer, published)
    observations = layer.observations()
    assert first in observations["rejected_generations"]
    assert first not in observations["published_generations"]
    assert observations["published_generations"] == (second,)
    assert published[0][0] == second


def test_a_closed_layer_never_publishes(tmp_path):
    """A closed layer publishes nothing, not even a finished composition."""
    published = []
    layer = _chart_layer(tmp_path, on_publish=published.append, enc_enabled=True)
    entered, release = Event(), Event()

    def compose(camera, cancel):
        entered.set()
        release.wait(5)
        return _FakeImage(10, 10), _chart_metadata(camera)

    layer._compose = compose
    layer.request(_camera())
    assert entered.wait(5)
    layer.close()
    release.set()
    if layer._thread is not None:
        layer._thread.join(5)
    assert published == []


def test_a_failed_composition_hides_the_previous_enc(tmp_path):
    """A failure never leaves the previous ENC presented as if it were current."""
    published = []
    layer = _chart_layer(tmp_path, on_publish=published.append, enc_enabled=True)
    layer._compose = lambda camera, cancel: (
        _FakeImage(10, 10),
        _chart_metadata(camera),
    )
    layer.request(_camera())
    _wait_for_publication(layer, published)
    assert layer._published is not None

    def failing(camera, cancel):
        raise ValueError("the chart could not be composed")

    layer._compose = failing
    layer.request(_camera(), replace=True)
    deadline = time.monotonic() + 5
    while layer.enc_state != "unavailable" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert layer.enc_state == "unavailable"
    assert layer._published is None
    assert layer.observations()["payload_bytes"] == 0
    assert layer.enc_status() == "ENC unavailable"


def test_the_retained_payload_is_bounded_and_accounted(tmp_path):
    """The published payload is accounted for, and the history is bounded."""
    published = []
    layer = _chart_layer(tmp_path, on_publish=published.append, enc_enabled=True)
    layer._compose = lambda camera, cancel: (
        _FakeImage(64, 32),
        _chart_metadata(camera),
    )
    layer.request(_camera())
    _wait_for_publication(layer, published)
    assert layer.observations()["payload_bytes"] == 64 * 32 * 4
    # The diagnostic history is evidence, never a store.
    limit = gribviewportchart.HISTORY_LIMIT
    layer._outcomes = {index: "published" for index in range(limit + 20)}
    layer._published_generations = set(layer._outcomes)
    layer._rejected_generations = set(layer._outcomes)
    layer._trim_history()
    assert len(layer._outcomes) == limit
    assert len(layer._published_generations) == limit
    assert len(layer._rejected_generations) == limit


def test_a_source_replacement_withdraws_the_published_composition(tmp_path):
    """A replaced or switched off cell invalidates what was published."""
    published = []
    layer = _chart_layer(tmp_path, on_publish=published.append, enc_enabled=True)
    layer._compose = lambda camera, cancel: (
        _FakeImage(10, 10),
        _chart_metadata(camera),
    )
    layer.request(_camera())
    _wait_for_publication(layer, published)
    assert layer._published is not None
    layer.invalidate_sources()
    assert layer._published is None
    assert layer.observations()["payload_bytes"] == 0


def test_a_changed_manifest_is_never_a_retained_composition(tmp_path):
    """The user's own manifest decides: a changed one is composed again."""
    _settings(tmp_path, [_entry("/charts/US5TEST.000")])
    published = []
    layer = _chart_layer(tmp_path, on_publish=published.append, enc_enabled=True)
    layer._compose = lambda camera, cancel: (
        _FakeImage(10, 10),
        _chart_metadata(camera),
    )
    camera = _camera()
    layer.request(camera)
    _wait_for_publication(layer, published)
    generation = layer.observations()["generation"]
    # The cell is switched off: the manifest is a different file.
    _settings(tmp_path, [])
    layer.request(camera)
    assert layer.observations()["generation"] != generation
    assert layer.counters["reuses"] == 0


def test_the_worker_never_uses_the_applications_global_settings():
    """The worker reads the user's files, never the application's settings."""
    for module in WORKER_MODULES:
        tree = ast.parse(inspect.getsource(module))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        for name in imported:
            assert not name.startswith(FORBIDDEN_IMPORTS), (module.__name__, name)


def test_the_viewport_never_imports_the_tools_package():
    """The product never reaches into the experiment tooling."""
    for module in PRODUCT_MODULES:
        tree = ast.parse(inspect.getsource(module))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert not any(name.startswith("tools") for name in imported), module.__name__


def test_the_application_never_imports_the_enc_source():
    """The ENC source is the viewport's own; the application never uses it."""
    package = Path(gribviewport.__file__).parent
    for path in sorted(package.glob("*.py")):
        if path.name in ("gribviewport.py", "gribviewportchart.py", "viewportenc.py"):
            continue
        assert "viewportenc" not in path.read_text(), path.name
