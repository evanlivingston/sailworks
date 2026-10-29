"""Numerical transport tests; independent of a window or graphics backend."""

from datetime import datetime, timedelta

import numpy as np
import pytest
from PySide6.QtCore import QSize

from gweatherrouting.qt.gpuweather import (
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
