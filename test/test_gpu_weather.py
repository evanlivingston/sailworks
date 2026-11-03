"""Numerical transport tests; independent of a window or graphics backend."""

from datetime import datetime, timedelta

import numpy as np
import pytest
from PySide6.QtCore import QSize

from sailworks.qt.gpuweather import (
    WeatherImageProvider,
    WeatherTexturePair,
    decode_component,
    encode_component,
)

START = datetime(2026, 1, 1)


def pair(values, end=3):
    return WeatherTexturePair.from_arrays(
        START, START + timedelta(hours=end), *(values for _ in range(4))
    )


def test_transport_preserves_float32_bits_and_opaque_alpha():
    bits = np.array(
        [
            [0, 0x80000000, 0x7FC00001, 0x7F800000],
            [1, 0xFF800000, 0xC1A12345, 0x3EAAAAAB],
        ],
        dtype=np.uint32,
    )
    image = encode_component(bits.view(np.float32))
    np.testing.assert_array_equal(decode_component(image).view(np.uint32), bits)
    pixels = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(2, 8, 4)
    assert np.all(pixels[:, :, 3] == 255)


def test_image_owns_its_storage_and_accepts_noncontiguous_input():
    original = np.arange(24, dtype=np.float64).reshape(4, 6)[:, ::2]
    expected = original.astype(np.float32)
    image = encode_component(original)
    original[:] = -1
    np.testing.assert_array_equal(decode_component(image), expected)


@pytest.mark.parametrize("values", [[], [1, 2], np.empty((0, 3)), np.zeros((2, 2, 2))])
def test_rejects_invalid_shapes(values):
    with pytest.raises(ValueError):
        encode_component(values)


def test_continuous_weight_uses_actual_irregular_endpoints():
    snapshot = pair([[1]], end=3)
    for seconds in (0, 1, 60, 3599, 5400, 10799, 10800):
        assert snapshot.weight(START + timedelta(seconds=seconds)) == seconds / 10800
    with pytest.raises(ValueError):
        snapshot.weight(START - timedelta(microseconds=1))
    with pytest.raises(ValueError):
        snapshot.weight(START + timedelta(hours=4))
    assert pair([[1]], end=0).weight(START) == 0


def test_rejects_reversed_time_or_mismatched_grids():
    with pytest.raises(ValueError):
        pair([[1]], end=-1)
    with pytest.raises(ValueError):
        WeatherTexturePair.from_arrays(START, START, [[1]], [[1, 2]], [[1]], [[1]])


def test_provider_is_bounded_and_generation_safe():
    snapshot = pair([[3, -4]])
    provider = WeatherImageProvider(byte_limit=snapshot.nbytes)
    urls = provider.publish(snapshot)
    size = QSize()
    identifier = urls[0].removeprefix("image://weather/")
    image = provider.requestImage(identifier, size, QSize())
    assert size == QSize(4, 1)
    np.testing.assert_array_equal(decode_component(image), [[3, -4]])
    next_urls = provider.publish(pair([[5, 6]]))
    assert next_urls != urls
    assert provider.requestImage(identifier, size, QSize()).isNull()
    # Previously delivered images survive replacement and clear.
    provider.clear()
    np.testing.assert_array_equal(decode_component(image), [[3, -4]])
    with pytest.raises(ValueError):
        WeatherImageProvider(byte_limit=1).publish(snapshot)


def test_provider_keeps_displayed_selection_during_native_swap():
    provider = WeatherImageProvider()
    first = (encode_component([[1]]),) * 3
    second = (encode_component([[2]]),) * 3
    displayed = provider.publish_selection(first, "first")
    provider.pin_selection(displayed)
    pending = provider.publish_selection(second, "second")
    old_id = displayed[0].removeprefix("image://weather/")
    new_id = pending[0].removeprefix("image://weather/")
    assert not provider.requestImage(old_id, None, QSize()).isNull()
    assert not provider.requestImage(new_id, None, QSize()).isNull()
    provider.pin_selection(pending)
    assert provider.requestImage(old_id, None, QSize()).isNull()
    assert not provider.requestImage(new_id, None, QSize()).isNull()


def test_provider_retains_the_staged_selection_until_the_front_commits():
    provider = WeatherImageProvider()
    front = (encode_component([[1]]),) * 3
    pending = (encode_component([[2]]),) * 3
    displayed = provider.publish_selection(front, "front")
    provider.pin_selection(displayed)
    staged = provider.publish_selection(pending, "pending")
    provider.retain_selection(staged)
    # A newer publication replaces the front's own images, but the pending
    # selection the back layer binds must stay servable until the swap.
    provider.publish_selection((encode_component([[3]]),) * 3, "later")
    for url in displayed + staged:
        assert not requested(provider, url).isNull()
    # The commit pins the pending selection and releases the retained front.
    provider.pin_selection(staged)
    for url in displayed:
        assert requested(provider, url).isNull()
    for url in staged:
        assert not requested(provider, url).isNull()


def test_retained_selection_keeps_two_pending_frames_until_pinned():
    provider = WeatherImageProvider()
    front = provider.publish_selection((encode_component([[0]]),) * 3, "front")
    provider.pin_selection(front)
    first = provider.publish_selection((encode_component([[1]]),) * 3, "first")
    provider.retain_selection(first)
    second = provider.publish_selection((encode_component([[2]]),) * 3, "second")
    provider.retain_selection(second)
    # The back layer may still be loading either pending frame, so the next
    # publication keeps the previous and current pending beside the front.
    third = provider.publish_selection((encode_component([[3]]),) * 3, "third")
    for url in front + first + second:
        assert not requested(provider, url).isNull()
    # Once the back layer binds the newest pending, the next replacement drops
    # the oldest pending and keeps the pinned front plus the two newest.
    provider.retain_selection(third)
    provider.publish_selection((encode_component([[4]]),) * 3, "fourth")
    for url in first:
        assert requested(provider, url).isNull()
    for url in front + second + third:
        assert not requested(provider, url).isNull()
    # The commit pins the pending frame and releases everything it superseded.
    provider.pin_selection(third)
    for url in front + second:
        assert requested(provider, url).isNull()
    for url in third:
        assert not requested(provider, url).isNull()


def test_two_fields_near_budget_document_front_eviction():
    front = WeatherTexturePair.from_arrays(
        START,
        START + timedelta(hours=3),
        *([[value, value + 1]] for value in range(1, 5)),
    )
    pending = WeatherTexturePair.from_arrays(
        START,
        START + timedelta(hours=3),
        *([[value, value + 1]] for value in range(5, 9)),
    )
    for limit, front_retained in (
        (front.nbytes + pending.nbytes, True),
        (front.nbytes + pending.nbytes - 1, False),
    ):
        provider = WeatherImageProvider(byte_limit=limit)
        front_urls = provider.publish_field(front.images, "front")
        pending_urls = provider.publish_field(pending.images, "pending")
        assert provider.field_bytes() <= limit
        for url in front_urls:
            image = provider.requestImage(
                url.removeprefix("image://weather/"), None, QSize()
            )
            assert (not image.isNull()) is front_retained
        for url in pending_urls:
            assert not provider.requestImage(
                url.removeprefix("image://weather/"), None, QSize()
            ).isNull()


def requested(provider, url):
    return provider.requestImage(url.removeprefix("image://weather/"), None, QSize())


@pytest.mark.parametrize("extra", [0, -1])
def test_reserved_fields_budget_edge_preserves_bytes(extra):
    front = tuple(encode_component([[value]]) for value in (1, 2))
    pending = tuple(encode_component([[value]]) for value in (3, 4))
    limit = sum(image.sizeInBytes() for image in front + pending) + extra
    provider = WeatherImageProvider(byte_limit=limit)
    front_urls = provider.reserve_field("map", front, "front")
    provider.promote_field("map")
    if extra < 0:
        with pytest.raises(ValueError):
            provider.reserve_field("map", pending, "pending")
    else:
        pending_urls = provider.reserve_field("map", pending, "pending")
        for url, image in zip(pending_urls, pending):
            assert (
                requested(provider, url).constBits().tobytes()
                == image.constBits().tobytes()
            )
    for url, image in zip(front_urls, front):
        assert (
            requested(provider, url).constBits().tobytes()
            == image.constBits().tobytes()
        )
    assert provider.field_bytes() <= limit


def test_reservation_cancel_promote_and_delayed_request():
    images = [encode_component([[value]]) for value in (1, 2, 3)]
    provider = WeatherImageProvider(byte_limit=2 * images[0].sizeInBytes())
    front = provider.reserve_field("map", images[:1], "front")
    provider.promote_field("map")
    pending = provider.reserve_field("map", images[1:2], "pending")
    provider.cancel_field("map")
    replacement = provider.reserve_field("map", images[2:], "replacement")
    assert requested(provider, pending[0]).isNull()
    assert not requested(provider, front[0]).isNull()
    provider.promote_field("map")
    # A delayed loader request still succeeds for the newly displayed field.
    assert (
        requested(provider, replacement[0]).constBits().tobytes()
        == images[2].constBits().tobytes()
    )
    provider.release_fields("map")


def test_reservations_are_independent_and_count_distinct_images():
    first, second, third = (encode_component([[value]]) for value in (1, 2, 3))
    provider = WeatherImageProvider(byte_limit=2 * first.sizeInBytes())
    one = provider.reserve_field("one", (first, first), "one")
    provider.promote_field("one")
    two = provider.reserve_field("two", (second,), "two")
    provider.promote_field("two")
    assert provider.field_bytes() == 2 * first.sizeInBytes()
    with pytest.raises(ValueError):
        provider.reserve_field("one", (third,), "three")
    assert not requested(provider, one[0]).isNull()
    assert not requested(provider, two[0]).isNull()
    provider.release_fields("two")
    provider.reserve_field("one", (third,), "three")
    assert requested(provider, two[0]).isNull()
    assert not requested(provider, one[0]).isNull()


def test_barb_node_field_remains_servable_during_fill_reservation():
    nodes = tuple(encode_component([[value]]) for value in (1, 2, 3, 4))
    grid = tuple(encode_component([[value]]) for value in (5, 6, 7, 8))
    size = sum(image.sizeInBytes() for image in nodes + grid)
    provider = WeatherImageProvider(byte_limit=size)
    node_urls = provider.reserve_field("barbs", nodes, "nodes")
    provider.promote_field("barbs")
    grid_urls = provider.reserve_field("fill", grid, "grid")
    assert all(not requested(provider, url).isNull() for url in node_urls + grid_urls)
    assert provider.field_bytes() == size
    provider.promote_field("fill")
    assert all(not requested(provider, url).isNull() for url in node_urls + grid_urls)

    limited = WeatherImageProvider(byte_limit=size - 1)
    old_nodes = limited.reserve_field("barbs", nodes, "nodes")
    limited.promote_field("barbs")
    with pytest.raises(ValueError):
        limited.reserve_field("fill", grid, "grid")
    assert all(not requested(limited, url).isNull() for url in old_nodes)
    assert limited.field_bytes() <= limited.byte_limit


def test_none_owner_pending_reservation_cannot_be_evicted_by_lookahead():
    images = tuple(encode_component([[value]]) for value in (1, 2, 3))
    provider = WeatherImageProvider(byte_limit=2 * images[0].sizeInBytes())
    front = provider.reserve_field(None, images[:1], "front")
    provider.promote_field(None)
    pending = provider.reserve_field(None, images[1:2], "pending")
    with pytest.raises(ValueError):
        provider.publish_field(images[2:], "lookahead")
    assert not requested(provider, front[0]).isNull()
    assert not requested(provider, pending[0]).isNull()
    assert "lookahead" not in provider._field_urls
    assert provider.field_bytes() <= provider.byte_limit
