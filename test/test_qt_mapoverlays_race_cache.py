os.environ.setdefault("QT_QPA_PLATFORM", "offscreen") if False else None
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

from PySide6.QtGui import QImage

from gweatherrouting.qt.mapoverlays import RetainedOverlayMixin


class _Host(RetainedOverlayMixin):
    def __init__(self):
        self.controller = SimpleNamespace(
            core=SimpleNamespace(trackManager=[], poiManager=[])
        )
        self.navigation = SimpleNamespace(draw_race_tracks=lambda gpsmap, cr: None)
        self.time_control = SimpleNamespace(time=SimpleNamespace(timestamp=lambda: 0))

    def _view_pixel_size(self):
        return (10, 10, 1.0)

    def get_allocated_width(self):
        return 10

    def get_allocated_height(self):
        return 10

    def get_scale_factor(self):
        return 1

    def _collection_snapshot(self, manager):
        return tuple((getattr(item, "name", str(item)),) for item in manager)


def test_race_geometry_key_ignores_marker_positions():
    host = _Host()
    key_before = host._race_geometry_key()
    host.race_boat_positions = ((48.0, -123.0),)
    host.race_positions = ((48.1, -123.1),)
    key_after = host._race_geometry_key()
    assert key_before == key_after


def test_race_geometry_key_invalidates_on_track_change():
    host = _Host()
    key_before = host._race_geometry_key()
    host.controller.core.trackManager.append("track-a")
    key_after = host._race_geometry_key()
    assert key_before != key_after


def test_race_geometry_image_uses_draw_race_tracks():
    host = _Host()
    calls = []

    def draw(gpsmap, cr):
        calls.append((gpsmap is host, cr is not None))

    host.navigation.draw_race_tracks = draw
    image = host._race_geometry_image()
    assert calls and calls[0][0] is True
    assert isinstance(image, QImage)
    assert not image.isNull()


def test_race_geometry_cache_aliases_share_storage():
    host = _Host()
    sentinel = ("key", 1, 2, 3, None)
    host._race_tracks_cache = sentinel
    assert host._race_geometry_cache is sentinel
    assert host._race_cache is sentinel
    assert host._static_race_cache is sentinel
    host.invalidate_race_tracks()
    assert host._race_geometry_cache is None
    assert host._race_cache is None


def test_race_geometry_image_caches_and_reuses():
    host = _Host()
    calls = []

    def draw(gpsmap, cr):
        calls.append(1)

    host.navigation.draw_race_tracks = draw
    first = host._race_geometry_image()
    second = host._race_geometry_image()
    assert first is second
    assert len(calls) == 1


def test_race_geometry_image_invalidates_on_track_change():
    host = _Host()
    calls = []

    def draw(gpsmap, cr):
        calls.append(1)

    host.navigation.draw_race_tracks = draw
    host._race_geometry_image()
    host.controller.core.trackManager.append("track-a")
    host._race_geometry_image()
    assert len(calls) == 2


def test_race_geometry_image_invalidates_on_dimension_change():
    host = _Host()
    calls = []

    def draw(gpsmap, cr):
        calls.append(1)

    host.navigation.draw_race_tracks = draw
    host._race_geometry_image()
    host._view_pixel_size = lambda: (11, 10, 1.0)
    host._race_geometry_image()
    assert len(calls) == 2


def test_race_geometry_image_invalidates_on_view_change():
    host = _Host()
    calls = []

    def draw(gpsmap, cr):
        calls.append(1)

    host.navigation.draw_race_tracks = draw
    host._center_lat = 48.0
    host._center_lon = -123.0
    host._zoom = 6.0
    host._race_geometry_image()
    host._zoom = 7.0
    host._race_geometry_image()
    assert len(calls) == 2


def test_navigation_image_reuses_cached_race_geometry_on_marker_change():
    host = _Host()
    calls = []

    def draw(gpsmap, cr):
        calls.append(1)

    host.navigation = type("Navigation", (), {})()
    host.navigation.draw_race_tracks = draw
    host.navigation.draw_routes = lambda gpsmap, cr, ts: None
    host.navigation.draw_navigation = lambda gpsmap, cr: None
    host._center_lat = 48.0
    host._center_lon = -123.0
    host._zoom = 6.0

    host._navigation_image()
    host.race_boat_positions = ((48.0, -123.0),)
    host._navigation_image()

    assert len(calls) == 1


def test_navigation_image_invalidates_cached_race_geometry_on_view_change():
    host = _Host()
    calls = []

    def draw(gpsmap, cr):
        calls.append(1)

    host.navigation = type("Navigation", (), {})()
    host.navigation.draw_race_tracks = draw
    host.navigation.draw_routes = lambda gpsmap, cr, ts: None
    host.navigation.draw_navigation = lambda gpsmap, cr: None
    host._center_lat = 48.0
    host._center_lon = -123.0
    host._zoom = 6.0

    host._navigation_image()
    host._zoom = 7.0
    host._navigation_image()

    assert len(calls) == 2


def test_navigation_image_composites_cached_race_geometry_pixels():
    host = _Host()
    calls = []

    def draw(gpsmap, cr):
        calls.append(1)
        cr.rectangle(0, 0, 3, 3)
        cr.set_source_rgba(1.0, 0.0, 0.0, 1.0)
        cr.fill()

    host.navigation = type("Navigation", (), {})()
    host.navigation.draw_race_tracks = draw
    host.navigation.draw_routes = lambda gpsmap, cr, ts: None
    host.navigation.draw_navigation = lambda gpsmap, cr: None
    host._center_lat = 48.0
    host._center_lon = -123.0
    host._zoom = 6.0

    def has_red(image):
        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                if color.red() > 200 and color.green() < 50 and color.blue() < 50:
                    return True
        return False

    image = host._navigation_image()
    assert has_red(image)

    host.race_boat_positions = ((48.0, -123.0),)
    image_after = host._navigation_image()
    assert len(calls) == 1
    assert has_red(image_after)


# --- Point / visibility / structural mutation invalidation -----------------
# These tests use the real MapHostMixin snapshot and real Track/Collection
# objects (the _Host stub above only tracks names and would hide regressions).

from gweatherrouting.core.geo.collection import Collection
from gweatherrouting.core.geo.track import Track
from gweatherrouting.qt.maphost import MapHostMixin


class _Saver(SimpleNamespace):
    def save(self):
        pass


def _real_host(track_manager):
    host = _Host()
    host.controller.core.trackManager = track_manager
    host._collection_snapshot = MapHostMixin._collection_snapshot
    host._center_lat = 48.0
    host._center_lon = -123.0
    host._zoom = 6.0
    calls = []
    host.navigation.draw_race_tracks = lambda gpsmap, cr: calls.append(1)
    return host, calls


def _track(name="t", points=None, visible=True):
    pts = [(48.0, -123.0, None), (48.1, -123.1, None), (48.2, -123.2, None)]
    return Track(name, pts if points is None else points, visible, collection=_Saver())


def test_track_point_mutations_invalidate_race_geometry_key():
    track = _track()
    host, _ = _real_host([track])
    mutations = [
        lambda t: t.__setitem__(0, (47.0, -122.0, None)),
        lambda t: t.__delitem__(0),
        lambda t: t.move_up(1),
        lambda t: t.move_down(0),
        lambda t: t.add(47.5, -122.5),
        lambda t: t.remove(0),
        lambda t: t.move(0, 47.9, -122.9),
        lambda t: t.duplicate(0),
        lambda t: t.clear(),
    ]
    for mutate in mutations:
        key_before = host._race_geometry_key()
        mutate(track)
        assert host._race_geometry_key() != key_before


def test_track_point_mutation_in_place_list_ops_invalidate_key():
    track = _track()
    host, _ = _real_host([track])
    for op in (
        lambda p: p.append((1.0, 1.0, None)),
        lambda p: p.insert(0, (1.0, 1.0, None)),
        lambda p: p.pop(),
        lambda p: p.reverse(),
        lambda p: p.extend([(2.0, 2.0, None)]),
    ):
        key_before = host._race_geometry_key()
        op(track.points)
        assert host._race_geometry_key() != key_before


def test_track_point_edit_reruns_race_geometry_draw():
    track = _track()
    host, calls = _real_host([track])
    host._race_geometry_image()
    track.points[1] = (47.5, -122.5, None)
    host._race_geometry_image()
    assert len(calls) == 2


def test_track_visibility_toggle_invalidates_race_geometry():
    track = _track(visible=True)
    host, calls = _real_host([track])
    host._race_geometry_image()
    track.visible = False
    host._race_geometry_image()
    track.visible = True
    host._race_geometry_image()
    assert len(calls) == 3


def test_point_reads_do_not_invalidate_race_geometry_key():
    track = _track()
    host, calls = _real_host([track])
    key_before = host._race_geometry_key()
    list(track)
    len(track.points)
    track.points[0]
    track.to_json()
    assert host._race_geometry_key() == key_before
    host._race_geometry_image()
    host._race_geometry_image()
    assert len(calls) == 1


def test_collection_replacement_with_same_shape_invalidates_key():
    # Same name, length, visibility and position: only the collection revision
    # distinguishes the replaced element.
    manager = Collection.__new__(Collection)
    manager.elements = []
    manager.save = lambda: None
    manager.append(_track("same"))
    host, _ = _real_host(manager)
    key_before = host._race_geometry_key()
    manager[0] = _track("same")
    assert host._race_geometry_key() != key_before


def test_collection_structural_edits_bump_revision():
    manager = Collection.__new__(Collection)
    manager.elements = []
    manager.save = lambda: None
    start = manager.revision
    manager.append(_track("a"))
    assert manager.revision > start
    rev = manager.revision
    del manager[0]
    assert manager.revision > rev
    manager.append(_track("b"))
    rev = manager.revision
    manager.clear()
    assert manager.revision > rev


def test_snapshot_is_per_element_not_per_point():
    # The snapshot must not scan points: a large track yields a fixed-size entry.
    big = Track("big", [(float(i), 0.0, None) for i in range(5000)], collection=_Saver())
    snap = MapHostMixin._collection_snapshot([big])
    entry = snap[1][0]
    assert len(entry) == 5
    assert entry[2] == 5000
