"""The settled-view NOAA demand the Qt viewport offers its data controller."""

from sailworks.core.mapviewport import MapViewport


def auto_chart_view(camera):
    """The view a settled camera asks NOAA coverage for.

    Returns ``((south, west, north, east), meters_per_pixel)``, or ``None``
    when the camera cannot describe a view.  The projection is the same
    ``MapViewport`` the chart composition renders through, so the download
    gate and the renderer agree on what is on screen.
    """
    if camera is None:
        return None
    try:
        from sailworks.qt.gribviewportchart import ChartCamera

        viewport = MapViewport(ChartCamera(camera), 0, lambda: None)
        top_left, bottom_right = viewport.get_bbox()
        north, west = top_left.get_degrees()
        south, east = bottom_right.get_degrees()
        meters_per_pixel = viewport.get_scale()
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return None
    if south >= north or meters_per_pixel <= 0:
        return None
    return (south, west, north, east), meters_per_pixel


def auto_chart_ready_request(widget):
    """Ask a stationary viewport for fresh frames after a chart is installed.

    The camera has not moved, so neither the scene nor the raster would ask
    again on its own: the demand is already covered.  Installing a chart
    changes what can be drawn, so both layers mint a new generation.
    """
    request = getattr(widget, "_request_chart", None)
    if request is None:
        return False
    widget._chart_force_replace = True
    widget._chart_scene_replace = True
    request(cause="auto-chart")
    return True
