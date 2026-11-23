"""World-chart composition must preserve logical coordinates at native DPR."""

import numpy as np
import pytest

from sailworks.core.mapviewport import MapViewport, MercatorProjection
from sailworks.rendering.canvas import Canvas as DrawCanvas, CanvasImage
from sailworks.rendering.overlayrenderer import paint_viewport


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
    source = CanvasImage.new(viewport.width, viewport.height, factor)
    cr = DrawCanvas.for_image(source)
    cr.set_source_rgb(0, 0, 1)
    cr.paint()
    cr.set_source_rgb(1, 0, 0)
    cr.rectangle(viewport.width / 2 - 20, viewport.height / 2 - 20, 40, 40)
    cr.fill()
    cr.finish()
    canvas._center_lon = canvas._x_to_lon(canvas._lon_to_x(canvas._center_lon) + offset)
    target = CanvasImage.new(180, 160, factor)
    paint_viewport(canvas, DrawCanvas.for_image(target), viewport, source)
    pixels = np.frombuffer(bytes(target.get_data()), np.uint8).reshape(
        160 * factor, -1, 4
    )
    center = pixels[80 * factor, round((90 - offset) * factor)]
    assert tuple(center) == (0, 0, 255, 255)
    assert tuple(pixels[0, 0]) == (255, 0, 0, 255)
