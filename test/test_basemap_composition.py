"""World-chart composition must preserve logical coordinates at native DPR."""

import cairo
import numpy as np
import pytest

from gweatherrouting.core.mapviewport import MapViewport, MercatorProjection
from gweatherrouting.rendering.overlayrenderer import paint_viewport


class Canvas(MercatorProjection):
    _center_lat = 48.4
    _center_lon = -123.4
    _zoom = 9.38

    def get_allocated_width(self):
        return 180

    def get_allocated_height(self):
        return 160

    def queue_draw(self):
        pass


@pytest.mark.parametrize("factor", [1, 2])
@pytest.mark.parametrize("offset", [0, 0.5, 32])
def test_worker_surface_preserves_center_at_native_dpr(factor, offset):
    canvas = Canvas()
    viewport = MapViewport(canvas, 256)
    source = cairo.ImageSurface(
        cairo.FORMAT_ARGB32, viewport.width * factor, viewport.height * factor
    )
    source.set_device_scale(factor, factor)
    cr = cairo.Context(source)
    cr.set_source_rgb(0, 0, 1)
    cr.paint()
    cr.set_source_rgb(1, 0, 0)
    cr.rectangle(viewport.width / 2 - 20, viewport.height / 2 - 20, 40, 40)
    cr.fill()
    canvas._center_lon = canvas._x_to_lon(canvas._lon_to_x(canvas._center_lon) + offset)
    target = cairo.ImageSurface(cairo.FORMAT_ARGB32, 180 * factor, 160 * factor)
    target.set_device_scale(factor, factor)
    paint_viewport(canvas, cairo.Context(target), viewport, source)
    target.flush()
    pixels = np.frombuffer(target.get_data(), np.uint8).reshape(160 * factor, -1, 4)
    center = pixels[80 * factor, round((90 - offset) * factor)]
    assert tuple(center) == (0, 0, 255, 255)
    assert tuple(pixels[0, 0]) == (255, 0, 0, 255)
