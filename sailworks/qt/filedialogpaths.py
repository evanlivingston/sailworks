# -*- coding: utf-8 -*-
"""Remember the folder each file dialog last used.

Keys are plain strings, one per workflow:

- ``"w2k"``: copied W2K-1 log folder
- ``"logs"``: instrument logs and log folders
- ``"polars"``: polar files
- ``"races"``: local race JSON
- ``"vector-charts"``: vector chart files and folders
- ``"raster-charts"``: raster chart folders
- ``"mbtiles"``: MBTiles charts
- ``"routing-results"``: native routing result files
- ``"gpx"``: GPX imports and exports
- ``"credentials"``: WeatherNext service-account credentials

The remembered value is only ever a directory; a selected file contributes its
parent so the next dialog opens where the user was working.
"""

import os


def _directories(settings):
    """Return the remembered mapping for ``settings`` without raising."""
    if settings is None:
        return {}
    try:
        value = getattr(settings, "lastUsedDirectories", None)
    except Exception:
        return {}
    if isinstance(value, dict):
        return value
    try:
        value = settings.get("lastUsedDirectories")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def remembered_dir(settings, key):
    """Return the stored directory for ``key`` when it still exists."""
    value = _directories(settings).get(key)
    if not isinstance(value, str) or not value:
        return ""
    try:
        path = os.path.expanduser(value)
    except (TypeError, ValueError):
        return ""
    return path if os.path.isdir(path) else ""


def remember_dir(settings, key, selected):
    """Store the directory of ``selected`` under ``key`` when it exists."""
    if settings is None or not selected:
        return
    try:
        selected = os.path.expanduser(os.fspath(selected))
    except (TypeError, ValueError):
        return
    directory = selected if os.path.isdir(selected) else os.path.dirname(selected)
    if not directory or not os.path.isdir(directory):
        return
    directories = dict(_directories(settings))
    directories[key] = directory
    try:
        settings.lastUsedDirectories = directories
    except AttributeError:
        try:
            settings["lastUsedDirectories"] = directories
        except (OSError, TypeError):
            pass
    except (OSError, TypeError):
        pass
