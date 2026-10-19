"""The GPU-resident field store: stable identity, budget, eviction, parity."""

from datetime import timedelta

import numpy as np
import pytest

from gweatherrouting.core.gribsampling import WeatherSampler
from gweatherrouting.qt.gpuweatherfields import (
    FieldIdentity,
    FieldStore,
    ResidentField,
    TEXTURE_BYTES_PER_FLOAT32,
)
from gweatherrouting.qt.gpuweatherjobs import source_key
from gweatherrouting.rendering.weatherbarbs import BarbView, stage_barbs
from gweatherrouting.rendering.weatherstaging import time_window
from test.test_weather_barbs import Land
from test.test_weather_staging import gribs, START  # noqa: F401


def identity(**overrides):
    values = dict(
        source=(("gfs", "2026-10-19", (1, 2, 3, 4)),),
        parameter="wind",
        before=START.timestamp(),
        after=START.timestamp() + 10800,
        window=((-1.0, -1.0), (2.0, 2.0)),
        resolution=(60, 80),
    )
    values.update(overrides)
    return FieldIdentity(**values)


def field(ident, payload=None, nbytes=1024):
    return ResidentField(ident, payload or _Payload((_image(nbytes),)), ())


class _Payload:
    def __init__(self, images):
        self.images = images


def _image(byte_count):
    from PySide6.QtGui import QImage

    return QImage(byte_count // 4, 1, QImage.Format.Format_RGBA8888)


# ---------------------------------------------------------------- identity
def test_field_identity_is_stable_and_names_every_part_of_the_field():
    first = identity()
    assert first == identity()
    assert first.key() == identity().key()
    assert first.key() != identity(before=first.before + 1).key()
    assert first.key() != identity(after=first.after + 1).key()
    assert first.key() != identity(parameter="pressure").key()
    assert first.key() != identity(window=((-2.0, -2.0), (2.0, 2.0))).key()
    assert first.key() != identity(resolution=(60, 81)).key()
    assert first.key() != identity(source=(("gfs", "other"),)).key()


def test_resident_time_window_matches_the_staging_semantics(gribs):
    source = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    sampler = WeatherSampler([source])
    # The model run start is a selection event: it is served by a singleton
    # window, so no interpolation crosses the model-run switch.
    assert time_window(sampler, START.timestamp()) == (
        START.timestamp(),
        START.timestamp(),
    )
    before, after = time_window(sampler, START.timestamp() + 60)
    assert before == START.timestamp() and after > before
    # An instant before the first event has no window to bracket it.
    assert time_window(sampler, START.timestamp() - 60) == (
        START.timestamp() - 60,
        START.timestamp() - 60,
    )


# ------------------------------------------------------------------ budget
def test_budget_is_accounted_in_bytes_and_evicts_the_oldest_field():
    store = FieldStore(2048)
    first, second = identity(), identity(before=1.0, after=2.0)
    assert store.store(field(first, nbytes=1024)) is True
    assert store.store(field(second, nbytes=1024)) is True
    assert store.current_bytes == 2048
    third = identity(before=3.0, after=4.0)
    assert store.store(field(third, nbytes=1024)) is True
    # The least recently used field made room for the new one.
    assert store.acquire(first) is None
    assert store.current_bytes == 2048
    assert store.statistics()["evictions"] == 1


def test_the_current_field_is_protected_from_eviction():
    store = FieldStore(2048)
    first, second = identity(), identity(before=1.0, after=2.0)
    store.store(field(first, nbytes=1024))
    store.store(field(second, nbytes=1024))
    # The second field is the protected one: it survives the next eviction.
    third = identity(before=3.0, after=4.0)
    store.store(field(third, nbytes=1024))
    assert store.acquire(second) is not None
    assert store.acquire(first) is None


def test_a_field_larger_than_the_budget_is_published_but_not_retained():
    store = FieldStore(64)
    assert store.store(field(identity(), nbytes=1024)) is False
    assert store.current_bytes == 0
    assert store.acquire(identity()) is None


def test_release_and_clear_drop_resident_fields():
    store = FieldStore(4096)
    store.store(field(identity()))
    assert store.release(identity()) is True
    assert store.release(identity()) is False
    assert store.current_bytes == 0
    store.store(field(identity()))
    store.clear()
    assert store.current_bytes == 0
    assert store.statistics()["released"] == 2


def test_rgba8888_expansion_is_accounted_in_the_resident_cost():
    assert TEXTURE_BYTES_PER_FLOAT32 == 8
    rows, cols = 4, 5
    cells = rows * cols
    images = tuple(_image(cells * 8) for _ in range(6))
    resident = ResidentField(identity(), _Payload(images), ())
    # Six float32 per mesh cell become eight bytes of texture each.
    assert resident.texture_bytes == cells * 6 * 8
    assert resident.nbytes == resident.texture_bytes


class _Payload:
    def __init__(self, images):
        self.images = images


def _image(byte_count):
    from PySide6.QtGui import QImage

    return QImage(byte_count // 4, 1, QImage.Format.Format_RGBA8888)


# --------------------------------------------------------------- residency
def _stage(sampler, view, stamp, store=None, source=None):
    return stage_barbs(
        sampler,
        stamp,
        view,
        Land(),
        store=store,
        source=source,
    )


def test_a_time_change_inside_the_resident_window_reuses_the_field(gribs):
    source = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    sampler = WeatherSampler([source])
    view = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    store = FieldStore(1 << 20)
    signature = (source_key(source),)
    first = _stage(sampler, view, START.timestamp() + 3600, store, signature)
    # A later instant inside the same resident time window.
    second = _stage(sampler, view, START.timestamp() + 7200, store, signature)
    assert second is first
    assert store.statistics()["acquire_hits"] == 1
    assert store.statistics()["acquire_misses"] == 1


def test_returning_to_a_resident_window_reuses_the_field(gribs):
    source = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    sampler = WeatherSampler([source])
    view = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    store = FieldStore(1 << 20)
    signature = (source_key(source),)
    first = _stage(sampler, view, START.timestamp() + 3600, store, signature)
    _stage(sampler, view, START.timestamp() + 7200, store, signature)
    again = _stage(sampler, view, START.timestamp() + 3600, store, signature)
    assert again is first


def test_a_new_time_window_stages_a_new_field(gribs):
    source = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    sampler = WeatherSampler([source])
    view = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    store = FieldStore(1 << 20)
    signature = (source_key(source),)
    first = _stage(sampler, view, START.timestamp() + 3600, store, signature)
    # The model run start is an event: it is served by its own singleton
    # field, never blended with the window that follows it.
    second = _stage(sampler, view, START.timestamp(), store, signature)
    assert second is not first
    assert second.identity.before == second.identity.after
    assert store.statistics()["acquire_misses"] == 2


def test_a_covered_pan_outside_the_resident_window_stages_a_new_field(gribs):
    source = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    sampler = WeatherSampler([source])
    store = FieldStore(1 << 20)
    signature = (source_key(source),)
    here = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    there = BarbView(640, 480, 0.6, 0.6, 9, "standard", False)
    stamp = START.timestamp() + 3600
    first = _stage(sampler, here, stamp, store, signature)
    panned = _stage(sampler, there, stamp, store, signature)
    assert panned is not first
    # The panned field is a different field, never the stale one.
    assert panned.identity.window != first.identity.window
    # Pan back: the original field is still resident.
    assert _stage(sampler, here, stamp, store, signature) is first


def test_the_resident_field_is_numerically_identical_to_a_fresh_stage(gribs):
    source = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    sampler = WeatherSampler([source])
    view = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    stamp = START.timestamp() + 3600
    store = FieldStore(1 << 20)
    resident = _stage(sampler, view, stamp, store, (source_key(source),))
    fresh = _stage(sampler, view, stamp)
    np.testing.assert_array_equal(
        resident.staged.first.view(np.uint64), fresh.staged.first.view(np.uint64)
    )
    np.testing.assert_array_equal(
        resident.staged.second.view(np.uint64), fresh.staged.second.view(np.uint64)
    )
    assert resident.count == fresh.count
    # The transport bytes are unchanged, so the missing-data contract holds.
    for stored, expected in zip(resident.pair.images, fresh.pair.images):
        assert stored.constBits().tobytes() == expected.constBits().tobytes()
    for stored, expected in zip(resident.anchors, fresh.anchors):
        assert stored.constBits().tobytes() == expected.constBits().tobytes()


def test_the_default_path_keeps_the_historical_publication(gribs):
    source = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    sampler = WeatherSampler([source])
    view = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    stamp = START.timestamp() + 3600
    atlas = _stage(sampler, view, stamp)
    assert atlas.identity is None
    from gweatherrouting.qt.gpuweather import WeatherImageProvider

    provider = WeatherImageProvider()
    first = provider.publish(atlas)
    second = provider.publish(atlas)
    assert first != second
    assert all(url.startswith("image://weather/") for url in first)


def test_a_named_publication_keeps_its_urls(gribs):
    source = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    sampler = WeatherSampler([source])
    view = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    store = FieldStore(1 << 20)
    signature = (source_key(source),)
    atlas = _stage(sampler, view, START.timestamp() + 3600, store, signature)
    from gweatherrouting.qt.gpuweather import WeatherImageProvider

    provider = WeatherImageProvider()
    first = provider.publish(atlas, atlas.identity.key())
    again = provider.publish(atlas, atlas.identity.key())
    assert first == again
