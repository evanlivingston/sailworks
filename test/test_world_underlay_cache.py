# -*- coding: utf-8 -*-
"""The disk cache behind the startup world underlay."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage

from sailworks.qt import gribviewport, worldunderlaycache
from sailworks.qt.basecharts import BaseSource
from sailworks.qt import gribviewportchart


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _image(width=8, height=8):
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#336699"))
    image.setPixel(1, 1, 0xFF102030)
    image.setPixel(width - 1, height - 1, 0xFFAABBCC)
    return image


def _source(path):
    return (BaseSource("simple", str(path), False, (), (), 7),)


@pytest.fixture
def shapefile(tmp_path):
    path = tmp_path / "coast.shp"
    path.write_bytes(b"coastline")
    return path


def test_save_then_load_is_pixel_identical(app, tmp_path):
    image = _image()
    path = tmp_path / "world.cache"

    assert worldunderlaycache.save(str(path), image) is True
    loaded = worldunderlaycache.load(str(path))

    assert loaded is not None
    assert loaded.size() == image.size()
    assert loaded.format() == image.format()
    assert loaded.devicePixelRatio() == image.devicePixelRatio()
    assert loaded == image
    assert not list(tmp_path.glob(".world-*"))


def test_second_lookup_hits_without_a_render(app, tmp_path, monkeypatch):
    source = shapefile
    sources = _source(source)
    cache_dir = tmp_path / "cache"
    image = _image()

    assert worldunderlaycache.load_for(sources, "day", 2, str(cache_dir)) is None
    assert worldunderlaycache.save_for(sources, "day", 2, image, str(cache_dir))
    hit = worldunderlaycache.load_for(sources, "day", 2, str(cache_dir))

    assert hit is not None and hit == image
    # The same inputs resolve to the same file, with no render taking part.
    assert worldunderlaycache.path_for(
        sources, "day", 2, str(cache_dir)
    ) == worldunderlaycache.path_for(sources, "day", 2, str(cache_dir))


def test_palette_factor_size_mtime_and_version_change_the_key(shapefile, monkeypatch):
    sources = _source(shapefile)
    base = worldunderlaycache.fingerprint(sources, "day", 1)

    assert worldunderlaycache.fingerprint(sources, "night", 1) != base
    assert worldunderlaycache.fingerprint(sources, "day", 2) != base

    shapefile.write_bytes(b"coastline plus more")
    bigger = worldunderlaycache.fingerprint(sources, "day", 1)
    assert bigger != base

    stat = shapefile.stat()
    os.utime(shapefile, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert worldunderlaycache.fingerprint(sources, "day", 1) != bigger

    monkeypatch.setattr(worldunderlaycache, "CACHE_FORMAT_VERSION", 2)
    assert worldunderlaycache.fingerprint(sources, "day", 1) != bigger


def test_missing_and_corrupt_entries_return_none(app, tmp_path, shapefile):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    sources = _source(shapefile)
    missing = worldunderlaycache.path_for(sources, "day", 1, str(cache_dir))
    assert worldunderlaycache.load(missing) is None

    image = _image()
    good = cache_dir / "good.cache"
    assert worldunderlaycache.save(str(good), image)
    raw = good.read_bytes()

    corrupt = {
        "empty": b"",
        "garbage": b"not a world cache",
        "magic only": worldunderlaycache._MAGIC,
        "header only": worldunderlaycache._MAGIC
        + worldunderlaycache._HEADER.pack(
            int(QImage.Format.Format_ARGB32_Premultiplied.value), 1.0
        ),
        "truncated png": raw[: len(raw) // 2],
        "bad format": worldunderlaycache._MAGIC
        + worldunderlaycache._HEADER.pack(9999, 1.0)
        + raw[len(worldunderlaycache._MAGIC) + worldunderlaycache._HEADER.size :],
    }
    for name, payload in corrupt.items():
        path = tmp_path / f"{name}.cache"
        path.write_bytes(payload)
        assert worldunderlaycache.load(str(path)) is None, name


def test_old_caches_are_pruned(app, tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    images = [
        cache_dir / f"world-{index:064x}.cache"
        for index in range(worldunderlaycache.MAX_CACHE_FILES + 2)
    ]
    base = 1_000_000_000
    for offset, path in enumerate(images):
        path.write_bytes(b"stale")
        stamp = base + offset
        os.utime(path, ns=(stamp, stamp))

    fresh = worldunderlaycache.path_for((), "day", 1, str(cache_dir))
    assert worldunderlaycache.save(fresh, _image()) is True

    remaining = {path.name for path in cache_dir.glob("world-*.cache")}
    assert os.path.basename(fresh) in remaining
    assert len(remaining) <= worldunderlaycache.MAX_CACHE_FILES
    # The two oldest, created first, are the ones dropped.
    assert images[0].name not in remaining
    assert images[1].name not in remaining


def test_save_failure_is_swallowed(app, tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("replace refused")

    monkeypatch.setattr(worldunderlaycache.os, "replace", boom)
    assert worldunderlaycache.save(str(tmp_path / "world.cache"), _image()) is False
    assert not list(tmp_path.glob(".world-*"))


def test_unwritable_directory_is_swallowed(app, tmp_path):
    if os.name != "posix" or os.geteuid() == 0:
        pytest.skip("permissions are not enforced for this user")
    directory = tmp_path / "readonly"
    directory.mkdir()
    directory.chmod(0o500)
    try:
        assert (
            worldunderlaycache.save(str(directory / "world.cache"), _image()) is False
        )
        assert worldunderlaycache.load(str(directory / "world.cache")) is None
    finally:
        directory.chmod(0o700)


def test_startup_miss_renders_on_hit_skips(app, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    source = data_dir / "coast.shp"
    source.write_bytes(b"coastline")
    cache_dir = tmp_path / "cache"
    sources = _source(source)
    monkeypatch.setattr(gribviewportchart, "base_sources", lambda _dir: sources)

    rendered = []
    emitted = []
    image = _image(16, 16)

    def render(data_dir, palette, factor):
        rendered.append((data_dir, palette, factor))
        return image

    def emit(result, error):
        emitted.append((result, error))

    def inline(worker):
        worker()

    # Miss: the renderer runs once and the raster is stored.
    assert (
        gribviewport.start_world_underlay(
            str(data_dir), "day", 2, render, emit, str(cache_dir), inline
        )
        is False
    )
    assert len(rendered) == 1
    assert emitted[-1] == (image, "")
    assert os.path.isfile(
        worldunderlaycache.path_for(sources, "day", 2, str(cache_dir))
    )

    # Hit: no second render, same raster, no error.
    emitted.clear()
    assert (
        gribviewport.start_world_underlay(
            str(data_dir), "day", 2, render, emit, str(cache_dir), inline
        )
        is True
    )
    assert len(rendered) == 1
    assert len(emitted) == 1
    assert emitted[0][1] == ""
    assert emitted[0][0] == image

    # A palette change invalidates the entry and renders again.
    emitted.clear()
    assert (
        gribviewport.start_world_underlay(
            str(data_dir), "night", 2, render, emit, str(cache_dir), inline
        )
        is False
    )
    assert len(rendered) == 2


def test_startup_failure_writes_no_cache(app, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    source = data_dir / "coast.shp"
    source.write_bytes(b"coastline")
    cache_dir = tmp_path / "cache"
    sources = _source(source)
    monkeypatch.setattr(gribviewportchart, "base_sources", lambda _dir: sources)

    emitted = []

    def render(data_dir, palette, factor):
        raise RuntimeError("no coastline data available")

    def emit(result, error):
        emitted.append((result, error))

    assert (
        gribviewport.start_world_underlay(
            str(data_dir),
            "day",
            1,
            render,
            emit,
            str(cache_dir),
            lambda worker: worker(),
        )
        is False
    )
    assert emitted == [(None, "no coastline data available")]
    path = worldunderlaycache.path_for(sources, "day", 1, str(cache_dir))
    assert not os.path.exists(path)
