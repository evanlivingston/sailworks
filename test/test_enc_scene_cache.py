"""Cache, version and cancellation checks for the Stage 1 ENC source scene.

The shared GPKG fixture and geometry helpers live in ``test.enc_scene_fixtures``.
"""

import os

import pytest
from osgeo import ogr, osr

from sailworks.rendering.vectordrawer import encscene, encscenestore
from test.enc_scene_fixtures import (
    _bbox,
    _CancelAfter,
    _multipolygon,
    _polygon,
    _write_cell,
    cell,
    scene,
)


def _build(path, version, cancelled=None):
    datasource = ogr.Open(str(path), 0)
    try:
        return encscene.build_cell_scene(datasource, path, version, cancelled)
    finally:
        datasource = None


def _two_cells(tmp_path):
    first = tmp_path / "a.gpkg"
    second = tmp_path / "b.gpkg"
    _write_cell(first)
    _write_cell(second)
    return (
        encscene.open_cell_scene(first, encscene.cell_version(first)),
        encscene.open_cell_scene(second, encscene.cell_version(second)),
    )


def _bump_mtime(path):
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


@pytest.fixture(autouse=True)
def _fresh_oversized_memo():
    """Keep the process-level oversized memo from leaking between tests."""
    encscene._clear_oversized_memo()
    yield
    encscene._clear_oversized_memo()


def _counting_deep_nbytes(monkeypatch):
    """Record every call into the deep size measurement."""
    real = encscene._deep_nbytes
    calls = []

    def counting(value, seen):
        calls.append(id(value))
        return real(value, seen)

    monkeypatch.setattr(encscene, "_deep_nbytes", counting)
    return calls


def test_version_key_is_file_identity(cell):
    version = encscene.cell_version(cell)
    assert len(version) == 4 and all(isinstance(part, int) for part in version)
    scene = encscene.open_cell_scene(cell, version)
    assert scene.version == version
    stat = cell.stat()
    os.utime(cell, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert encscene.cell_version(cell) != version


def test_cache_retains_working_set_or_reports_eviction(tmp_path):
    first, second = _two_cells(tmp_path)

    retaining = encscene.SceneCache(max_bytes=first.nbytes + second.nbytes)
    assert retaining.put(first) and retaining.put(second)
    assert retaining.get(first.path, first.version) is first
    assert retaining.get(second.path, second.version) is second
    assert retaining.evictions == 0
    assert retaining.resident_bytes == first.nbytes + second.nbytes

    bounded = encscene.SceneCache(max_bytes=max(first.nbytes, second.nbytes))
    assert bounded.put(first)
    assert bounded.get(first.path, first.version) is first
    assert bounded.put(second)
    assert bounded.evictions == 1
    assert bounded.resident_bytes == second.nbytes
    assert bounded.get(first.path, first.version) is None  # recorded revisit miss
    assert bounded.misses == 1
    assert bounded.get(second.path, second.version) is second


def test_oversized_scene_falls_back_without_eviction(tmp_path):
    first, _ = _two_cells(tmp_path)
    cache = encscene.SceneCache(max_bytes=first.nbytes - 1)
    assert cache.put(first) is False
    assert cache.oversized == 1
    assert cache.evictions == 0
    assert cache.resident_bytes == 0
    assert cache.get(first.path, first.version) is None


def test_version_replacement_drops_the_previous_scene(tmp_path):
    first, _ = _two_cells(tmp_path)
    cache = encscene.SceneCache(max_bytes=10_000_000)
    assert cache.put(first)
    _bump_mtime(first.path)
    replacement_version = encscene.cell_version(first.path)
    replacement = encscene.open_cell_scene(first.path, replacement_version)
    assert cache.put(replacement)
    assert cache.get(first.path, first.version) is None
    assert cache.get(first.path, replacement_version) is replacement
    assert cache.resident_bytes == replacement.nbytes
    assert cache.is_oversized(first.path, first.version) is False


def test_cancelled_build_never_publishes_partial_scene(cell):
    version = encscene.cell_version(cell)
    cache = encscene.SceneCache(max_bytes=10_000_000)
    seen = []

    def loader(cancelled):
        seen.append(cancelled)
        return _build(cell, version, cancelled)

    # The callback fires inside build_cell_scene, not at the entry checks.
    result = cache.get_or_build(cell, version, loader, _CancelAfter(6))
    assert result is None
    assert seen and seen[0] is not None
    assert cache.resident_bytes == 0
    assert cache.get(cell, version) is None


def test_cancelled_after_loader_is_never_published(cell):
    version = encscene.cell_version(cell)
    cache = encscene.SceneCache(max_bytes=10_000_000)
    state = {"cancelled": False}

    def cancelled():
        return state["cancelled"]

    def loader(_cancelled):
        state["cancelled"] = True
        return _build(cell, version)

    assert cache.get_or_build(cell, version, loader, cancelled) is None
    assert cache.resident_bytes == 0
    assert cache.get(cell, version) is None


def test_build_clears_inherited_spatial_and_attribute_filters(cell):
    datasource = ogr.Open(str(cell), 0)
    try:
        layer = datasource.GetLayerByName("DEPARE")
        layer.SetSpatialFilter(_bbox(4.5, 4.5, 5.5, 5.5))
        layer.SetAttributeFilter("DRVAL1 = 5.0")
        scene = encscene.build_cell_scene(datasource, cell, encscene.cell_version(cell))
    finally:
        datasource = None
    assert len(scene.records) == 11
    assert sum(record.source_class == "DEPARE" for record in scene.records) == 3


def test_open_rejects_caller_version_that_does_not_match(cell):
    version = encscene.cell_version(cell)
    stale = (version[0], version[1], version[2], version[3] - 1)
    with pytest.raises(ValueError):
        encscene.open_cell_scene(cell, stale)


def test_open_rejects_cell_changed_during_read(cell, monkeypatch):
    version = encscene.cell_version(cell)
    real = encscene.cell_version
    calls = []

    def changing(path):
        calls.append(path)
        if len(calls) > 1:
            return (version[0], version[1], version[2], version[3] + 1)
        return real(path)

    monkeypatch.setattr(encscene, "cell_version", changing)
    with pytest.raises(ValueError):
        encscene.open_cell_scene(cell, version)


def test_cache_rejects_late_stale_version(tmp_path):
    path = tmp_path / "a.gpkg"
    _write_cell(path)
    old_version = encscene.cell_version(path)
    old = encscene.open_cell_scene(path, old_version)
    _bump_mtime(path)
    new_version = encscene.cell_version(path)
    new = encscene.open_cell_scene(path, new_version)
    cache = encscene.SceneCache(max_bytes=old.nbytes + new.nbytes)
    assert cache.put(new)
    assert cache.put(old) is False
    assert cache.stale == 1
    assert cache.get(path, new_version) is new
    assert cache.get(path, old_version) is None


def test_oversized_version_is_marked_and_not_rebuilt(tmp_path):
    path = tmp_path / "a.gpkg"
    _write_cell(path)
    version = encscene.cell_version(path)
    scene = encscene.open_cell_scene(path, version)
    cache = encscene.SceneCache(max_bytes=scene.nbytes - 1)
    calls = []

    def loader(cancelled):
        calls.append(cancelled)
        return encscene.open_cell_scene(path, encscene.cell_version(path), cancelled)

    assert cache.get_or_build(path, version, loader) is None
    assert cache.get_or_build(path, version, loader) is None
    assert len(calls) == 1
    assert cache.oversized == 1
    assert cache.is_oversized(path, version)

    _bump_mtime(path)
    new_version = encscene.cell_version(path)
    assert cache.is_oversized(path, new_version) is False
    assert cache.get_or_build(path, new_version, loader) is None
    assert len(calls) == 2
    assert cache.is_oversized(path, version) is False
    assert cache.is_oversized(path, new_version)


def test_repeat_oversized_open_skips_the_measurement(tmp_path, monkeypatch):
    path = tmp_path / "a.gpkg"
    _write_cell(path)
    version = encscene.cell_version(path)
    scene = encscene.open_cell_scene(path, version)
    cap = scene.nbytes - 1
    loads = []

    def loader(cancelled):
        loads.append(cancelled)
        return encscene.open_cell_scene(path, encscene.cell_version(path), cancelled)

    measured = _counting_deep_nbytes(monkeypatch)
    first = encscene.SceneCache(cap)
    assert first.get_or_build(path, version, loader) is None
    assert loads == [None]
    assert measured
    assert first.is_oversized(path, version)

    # The repeat composition builds a fresh cache; the verdict must survive it.
    second = encscene.SceneCache(cap)
    loads.clear()
    measured.clear()
    assert second.get_or_build(path, version, loader) is None
    assert loads == []
    assert measured == []
    assert second.is_oversized(path, version)


def test_touching_an_oversized_cell_forces_a_fresh_measurement(tmp_path, monkeypatch):
    path = tmp_path / "a.gpkg"
    _write_cell(path)
    version = encscene.cell_version(path)
    scene = encscene.open_cell_scene(path, version)
    cap = scene.nbytes - 1
    loads = []

    def loader(cancelled):
        loads.append(cancelled)
        return encscene.open_cell_scene(path, encscene.cell_version(path), cancelled)

    assert encscene.SceneCache(cap).get_or_build(path, version, loader) is None
    loads.clear()

    cache = encscene.SceneCache(cap)
    assert cache.get_or_build(path, version, loader) is None
    assert loads == []

    measured = _counting_deep_nbytes(monkeypatch)
    _bump_mtime(path)
    new_version = encscene.cell_version(path)
    assert new_version != version
    assert cache.get_or_build(path, new_version, loader) is None
    assert len(loads) == 1
    assert measured
    assert cache.is_oversized(path, new_version)


def test_acceptable_cell_is_never_served_from_the_oversized_memo(tmp_path, monkeypatch):
    path = tmp_path / "a.gpkg"
    _write_cell(path)
    version = encscene.cell_version(path)
    scene = encscene.open_cell_scene(path, version)
    assert encscene.SceneCache(scene.nbytes - 1).put(scene) is False
    loads = []

    def loader(cancelled):
        loads.append(cancelled)
        return encscene.open_cell_scene(path, encscene.cell_version(path), cancelled)

    measured = _counting_deep_nbytes(monkeypatch)
    accepting = encscene.SceneCache(scene.nbytes)
    built = accepting.get_or_build(path, version, loader)
    assert built is not None
    assert loads == [None]
    assert measured
    assert accepting.get(path, version) is built


def _write_lean_cell(path):
    """A valid one-feature cell: far fewer records than ``_write_cell``."""
    datasource = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    layer = datasource.CreateLayer("DEPARE", srs, ogr.wkbMultiPolygon)
    layer.CreateField(ogr.FieldDefn("DRVAL1", ogr.OFTReal))
    layer.CreateField(ogr.FieldDefn("DRVAL2", ogr.OFTReal))
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry(_multipolygon(_polygon([(0, 0), (1, 0), (1, 1), (0, 1)])))
    feature.SetField("DRVAL1", 5.0)
    feature.SetField("DRVAL2", 6.0)
    assert layer.CreateFeature(feature) == ogr.OGRERR_NONE
    datasource = None


def test_oversized_memo_keys_off_the_verified_version(tmp_path, monkeypatch):
    """A write landing after the put's version check must not poison the memo.

    The put verified *version* against the scene it measured, then the file
    became the lean cell below.  Keying the memo off a fresh stat here would
    record the lean file's identity against the big count, and a later cache
    with room for the lean cell would refuse it until the file was touched.
    """
    path = tmp_path / "a.gpkg"
    _write_cell(path)
    verified = encscene.cell_version(path)
    big = encscene.open_cell_scene(path, verified)

    path.unlink()
    _write_lean_cell(path)
    lean_version = encscene.cell_version(path)
    lean = encscene.open_cell_scene(path, lean_version)
    assert lean_version != verified
    # The cap fits the lean cell but not the bytes the big scene measured.
    cap = big.nbytes // 2
    assert lean.nbytes < cap < big.nbytes

    # The put validated `verified`, as it did before the write landed; a fresh
    # stat now returns the lean file's identity.
    with monkeypatch.context() as patch:
        patch.setattr(encscene, "cell_version", lambda path: verified)
        assert encscene.SceneCache(cap).put(big) is False

    # The verdict names the measured file: its count against the verified
    # identity, not against the file that replaced it.
    identity = (os.path.realpath(str(path)), verified[2], verified[3])
    assert encscene._oversized_memo[identity] == big.nbytes
    assert encscene._known_oversized_nbytes(path) is None

    # A cell that fits the cap is built, not refused by the stale verdict.
    loads = []

    def loader(cancelled):
        loads.append(cancelled)
        return encscene.open_cell_scene(path, encscene.cell_version(path), cancelled)

    accepting = encscene.SceneCache(cap)
    built = accepting.get_or_build(path, lean_version, loader)
    assert loads == [None]
    assert built is not None
    assert accepting.get(path, lean_version) is built
    assert accepting.is_oversized(path, lean_version) is False
    assert accepting.resident_bytes == built.nbytes


def test_memoized_refusal_is_the_same_refusal(tmp_path):
    path = tmp_path / "a.gpkg"
    _write_cell(path)
    version = encscene.cell_version(path)
    scene = encscene.open_cell_scene(path, version)
    cap = scene.nbytes - 1

    def loader(cancelled):
        return encscene.open_cell_scene(path, encscene.cell_version(path), cancelled)

    measured = encscene.SceneCache(cap)
    assert measured.get_or_build(path, version, loader) is None
    memoized = encscene.SceneCache(cap)
    assert memoized.get_or_build(path, version, loader) is None
    for cache in (measured, memoized):
        assert cache.is_oversized(path, version)
        assert cache.get(path, version) is None
        assert cache.resident_bytes == 0
        assert cache.oversized == 1


def test_oversized_memo_is_bounded(tmp_path):
    for index in range(encscene._OVERSIZED_MEMO_MAX + 4):
        path = tmp_path / f"cell{index}.gpkg"
        path.write_bytes(b"x")
        encscene._remember_oversized(path, 10, encscene.cell_version(path))
    assert len(encscene._oversized_memo) == encscene._OVERSIZED_MEMO_MAX


# ------------------------------------------------------- the persisted store
def _store(tmp_path, max_bytes=10_000_000, schema=None):
    return encscenestore.SceneStore(tmp_path / "store", max_bytes, schema)


def test_a_stored_scene_round_trips_through_the_disk(tmp_path, scene):
    """The file holds the whole scene, not an approximation of it."""
    store = _store(tmp_path)
    assert store.put(scene) is True

    decoded = store.get(scene.path, scene.version)
    assert decoded is not None
    assert (store.hits, store.misses, store.errors) == (1, 0, 0)
    assert decoded == scene

    bounding = _bbox(4.0, 4.0, 6.0, 6.0)
    original_ids = tuple(record.fid for record in scene.query(bounding))
    assert original_ids
    assert tuple(record.fid for record in decoded.query(bounding)) == original_ids
    assert store.path_for(scene.path, scene.version).stat().st_size == (
        store.size_bytes()
    )


def test_a_scene_of_another_schema_is_never_loaded(tmp_path, scene):
    """A code change that alters the scene's meaning invalidates the file."""
    store = _store(tmp_path)
    target = store.path_for(scene.path, scene.version)
    assert encscenestore.scene_key(
        scene.path, scene.version, "an older build"
    ) != encscenestore.scene_key(scene.path, scene.version)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encscenestore.encode_scene(scene, "an older build"))
    assert store.get(scene.path, scene.version) is None
    assert (store.hits, store.misses, store.errors) == (0, 1, 1)
    assert not target.exists()


def test_a_truncated_file_is_a_miss_and_is_removed(tmp_path, scene):
    """A half-written file is a miss that is deleted, never a crash."""
    store = _store(tmp_path)
    assert store.put(scene)
    target = store.path_for(scene.path, scene.version)
    target.write_bytes(target.read_bytes()[:-16])

    assert store.get(scene.path, scene.version) is None
    assert (store.hits, store.misses, store.errors) == (0, 1, 1)
    assert not target.exists()


def test_a_garbage_file_is_a_miss_and_is_removed(tmp_path, scene):
    """Bytes that are not a scene are deleted rather than retried forever."""
    store = _store(tmp_path)
    target = store.path_for(scene.path, scene.version)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"not a scene at all")

    assert store.get(scene.path, scene.version) is None
    assert (store.hits, store.misses, store.errors) == (0, 1, 1)
    assert not target.exists()


def test_a_file_stored_for_another_cell_is_a_miss(tmp_path):
    """A decodable scene of another cell or version never satisfies a request."""
    first, second = _two_cells(tmp_path)
    store = _store(tmp_path)
    target = store.path_for(first.path, first.version)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encscenestore.encode_scene(second))

    assert store.get(first.path, first.version) is None
    assert (store.misses, store.errors) == (1, 1)
    assert not target.exists()

    version = first.version
    stale = (version[0], version[1], version[2], version[3] + 1)
    target = store.path_for(first.path, stale)
    target.write_bytes(encscenestore.encode_scene(first))

    assert store.get(first.path, stale) is None
    assert (store.misses, store.errors) == (2, 2)
    assert not target.exists()


def test_the_least_recently_used_file_is_evicted_under_the_cap(tmp_path):
    """The oldest file goes first and the directory ends up under the cap."""
    first, second = _two_cells(tmp_path)
    store = _store(tmp_path)
    assert store.put(first) and store.put(second)
    first_file = store.path_for(first.path, first.version)
    second_file = store.path_for(second.path, second.version)
    os.utime(first_file, ns=(1_000_000_000, 1_000_000_000))
    os.utime(second_file, ns=(2_000_000_000, 2_000_000_000))

    cap = second_file.stat().st_size
    bounded = encscenestore.SceneStore(store.directory, max_bytes=cap)
    bounded.evict()

    assert not first_file.exists()
    assert second_file.exists()
    assert bounded.evictions == 1
    assert bounded.size_bytes() == cap


def test_an_entry_larger_than_the_cap_is_never_written(tmp_path):
    """A scene that cannot fit the budget is refused, not written and evicted."""
    first, _ = _two_cells(tmp_path)
    store = _store(tmp_path, max_bytes=64)

    assert store.put(first) is False
    assert (store.writes, store.evictions, store.errors) == (0, 0, 1)
    assert not store.path_for(first.path, first.version).exists()
    assert store.size_bytes() == 0
    assert store.get(first.path, first.version) is None
    assert (store.hits, store.misses) == (0, 1)


# ------------------------------------------------------------ pinning
def _third_cell(tmp_path, name):
    path = tmp_path / name
    _write_cell(path)
    return encscene.open_cell_scene(path, encscene.cell_version(path))


def test_a_pinned_scene_survives_an_eviction(tmp_path):
    first, second = _two_cells(tmp_path)
    third = _third_cell(tmp_path, "c.gpkg")
    # The premise: equal-sized cells, so the cap is exactly two of them.
    assert len({first.nbytes, second.nbytes, third.nbytes}) == 1

    cache = encscene.SceneCache(max_bytes=2 * first.nbytes)
    assert cache.put(first) and cache.put(second)
    pinned = {encscene.SceneCache.key(first.path, first.version)}

    # The pinned scene is the one the plan still needs, so the other goes.
    assert cache.put(third, pin=pinned)
    assert cache.evictions == 1
    assert cache.pinned_evictions_avoided == 1
    assert cache.get(first.path, first.version) is first
    assert cache.get(second.path, second.version) is None
    assert cache.resident_bytes == first.nbytes + third.nbytes


def test_the_byte_cap_evicts_the_oldest_pinned_scene(tmp_path):
    first, second = _two_cells(tmp_path)
    third = _third_cell(tmp_path, "d.gpkg")
    assert len({first.nbytes, second.nbytes, third.nbytes}) == 1

    cache = encscene.SceneCache(max_bytes=2 * first.nbytes)
    assert cache.put(first) and cache.put(second)
    pinned = {
        encscene.SceneCache.key(first.path, first.version),
        encscene.SceneCache.key(second.path, second.version),
    }

    # Every resident scene is pinned: the cap still wins, so the oldest pinned
    # entry is dropped and the cache stays inside its budget.
    assert cache.put(third, pin=pinned)
    assert cache.evictions == 1
    assert cache.pinned_evictions_avoided == 0
    assert cache.get(first.path, first.version) is None
    assert cache.get(second.path, second.version) is second
    assert cache.resident_bytes == second.nbytes + third.nbytes


def test_scene_store_timings_separate_read_from_build(tmp_path):
    """A store miss reports both the read and the fresh build, and no decode."""
    from sailworks.rendering.vectordrawer import encscenestore

    store = encscenestore.SceneStore(tmp_path / "store")
    timings = {}
    assert (
        store.load_or_build("cell.000", (1, 2, 3, 4), lambda: None, timings=timings)
        is None
    )
    assert timings["store_get"] >= 0.0
    assert timings["store_build"] >= 0.0
    assert "decode_scene" not in timings
    assert encscenestore.decode_scene.__defaults__ is not None
