import pytest

from sailworks.qt.gribviewport import ViewportCamera, _render_world, world_placement


def test_world_placement_centre_and_pan():
    camera = ViewportCamera(zoom=2, width=1024, height=1024)
    x, y, width, height = world_placement(camera)
    assert (x, y, width, height) == pytest.approx((0, 0, 1024, 1024), abs=1e-4)
    east = ViewportCamera(center_lon=45, zoom=2, width=1024, height=1024)
    assert world_placement(east)[0] == pytest.approx(-1024 / 8)


def test_world_placement_wrap_has_adjacent_copy():
    camera = ViewportCamera(center_lon=179, zoom=2, width=1024, height=1024)
    left, _, width, _ = world_placement(camera)
    assert left < 0 < left + width < camera.width
    assert left + 2 * width > camera.width
    wrapped = ViewportCamera(center_lon=539, zoom=2, width=1024, height=1024)
    assert world_placement(wrapped) == pytest.approx(world_placement(camera))


def test_world_render_uses_bundled_chart(tmp_path):
    image = _render_world(tmp_path, "cm93", 1)
    assert (image.width(), image.height()) == (1024, 1024)
    assert not image.isNull()
