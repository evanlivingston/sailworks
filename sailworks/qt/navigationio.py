"""GPX import and export for the shared track, route and POI collections.

The navigation panel that used to own these operations is gone; the File menu
and the scene tree now call the plain functions here with the window.
"""

import math
from pathlib import Path

import gpxpy
from PySide6.QtWidgets import QFileDialog

from .backgroundtask import BackgroundTask


def gpx_document(tracks=(), routes=(), waypoints=()):
    document = gpxpy.gpx.GPX()
    document.tracks = list(tracks)
    document.routes = list(routes)
    document.waypoints = list(waypoints)
    return document


def distance_nm(lat1, lon1, lat2, lon2):
    # Match the legacy AIS radius and haversine, in nautical miles.
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return 3440.065 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0, 1 - a)))


def _status(window, text):
    bar = getattr(window, "statusBar", None)
    if callable(bar):
        bar().showMessage(text, 5000)


def import_gpx(window, path=None):
    """Pick a GPX file (unless a path is given) and import it in the background."""
    if path is None:
        path, _ = QFileDialog.getOpenFileName(
            window, "Import GPX", "", "GPX files (*.gpx);;All files (*)"
        )
    if not path:
        return None
    return import_path(window, path)


def import_path(window, path):
    task = _task(window)
    if task.busy:
        return None
    _status(window, "Importing GPX…")
    task.start("Importing GPX…", lambda _progress: _read(path))
    return path


def _read(path):
    with open(path) as stream:
        gpx = gpxpy.parse(stream)
    coordinates = [
        (point.latitude, point.longitude)
        for track in gpx.tracks
        for segment in track.segments
        for point in segment.points
    ] + [(point.latitude, point.longitude) for point in gpx.waypoints]
    if any(
        not math.isfinite(lat)
        or not math.isfinite(lon)
        or not -90 <= lat <= 90
        or not -180 <= lon <= 180
        for lat, lon in coordinates
    ):
        raise ValueError("GPX contains invalid coordinates")
    return gpx


def _task(window):
    task = getattr(window, "gpx_import_task", None)
    if task is None:
        task = BackgroundTask(window)
        task.finished.connect(lambda gpx, error: _import_finished(window, gpx, error))
        window.gpx_import_task = task
    return task


def close(window):
    """Release the background import task, if the window created one."""
    task = getattr(window, "gpx_import_task", None)
    if task is not None:
        task.close()


def _import_finished(window, gpx, error):
    if error is not None:
        _status(window, "Could not import GPX: " + str(error))
        return
    core = window.controller.core
    # Same GPX tracks/waypoints semantics as the legacy importer; routes are not imported.
    for source in gpx.tracks:
        source.name = core.trackManager.get_unique_name(source.name or "track")
        core.trackManager.import_from_gpx(gpx_document(tracks=[source]))
    for source in gpx.waypoints:
        source.name = core.poiManager.get_unique_name(source.name or "poi")
        core.poiManager.import_from_gpx(gpx_document(waypoints=[source]))
    if gpx.tracks:
        core.trackManager.set_active(core.trackManager[-1])
    _refresh(window)
    _status(
        window,
        f"Imported {len(gpx.tracks)} tracks and {len(gpx.waypoints)} POIs. "
        "GPX routes are not imported.",
    )


def _refresh(window):
    panel = getattr(window, "scene_panel", None)
    if panel is not None:
        panel.refresh()
    changed = getattr(window, "_navigation_changed", None)
    if callable(changed):
        changed()
    if hasattr(window, "workspace"):
        window.workspace.changed()


def export_all(window):
    core = window.controller.core
    document = gpx_document(
        tracks=[track.to_gpx_object() for track in core.trackManager],
        routes=[route.to_gpx_object() for route in core.routingManager],
        waypoints=[poi.to_gpx_object() for poi in core.poiManager],
    )
    return _save(
        window,
        "Export GPX",
        "navigation.gpx",
        document.to_xml(),
        "GPX files (*.gpx)",
        ".gpx",
    )


def export_track(window):
    track = window.controller.core.trackManager.get_active()
    if track is None:
        return None
    return _save(
        window,
        "Export track",
        track.name + ".gpx",
        gpx_document(tracks=[track.to_gpx_object()]).to_xml(),
        "GPX files (*.gpx)",
        ".gpx",
    )


def export_route(window):
    route = window.controller.core.routingManager.get_active()
    if route is None:
        return None
    return _save(
        window,
        "Export routing",
        route.name + ".gpx",
        gpx_document(routes=[route.to_gpx_object()]).to_xml(),
        "GPX files (*.gpx)",
        ".gpx",
    )


def _save(window, title, name, text, file_filter, suffix):
    path, _ = QFileDialog.getSaveFileName(window, title, name, file_filter)
    if not path:
        return None
    if not Path(path).suffix:
        path += suffix
    try:
        Path(path).write_text(text, encoding="utf-8")
    except OSError as error:
        _status(window, f"Could not export: {error}")
        return None
    _status(window, f"Exported to {path}")
    return path
