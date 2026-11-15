"""Cache, version and cancellation checks for the Stage 1 ENC source scene.

The shared GPKG fixture and geometry helpers live in ``test.enc_scene_fixtures``.
"""

import os

import pytest
from osgeo import ogr

from sailworks.rendering.vectordrawer import encscene
from test.enc_scene_fixtures import (
    _bbox,
    _CancelAfter,
    _write_cell,
    cell,
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
