"""Disk cache for the startup world-underlay raster.

The startup underlay is a fixed zoom-2 world image rendered from the vector
chart sources.  Its inputs change rarely -- only when a source file, the
palette, the device-pixel factor or the cache format itself changes -- so a
warm launch can load the previous raster instead of paying the full render.

The cache is advisory.  Every entry is validated before it is used, a failed
write leaves no trace, and a cache problem never reaches the user: a miss and a
corrupt entry both fall back to rendering.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import struct
import tempfile

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage

logger = logging.getLogger("sailworks")

# Bump when the raster or this module's key derivation changes in a way that
# invalidates every existing entry.
CACHE_FORMAT_VERSION = 1

# The underlay is the fixed zoom-2 world: MercatorProjection.TILE_SIZE * 2**2.
WORLD_ZOOM = 2
WORLD_SIZE = 1024

#: Directory under ``sailworks.core.storage.DATA_DIR``.
CACHE_SUBDIR = os.path.join("cache", "world-underlay")

#: Newest entries kept in the cache directory; older ones are pruned so a
#: changing key cannot grow the directory without bound.
MAX_CACHE_FILES = 3

_FILENAME_PREFIX = "world-"
_FILENAME_SUFFIX = ".cache"
_MAGIC = b"SW-WORLD\x00"
#: Image format and device-pixel ratio, so a reload reproduces the raster.
_HEADER = struct.Struct("<If")


def cache_root(directory=None):
    """The directory that holds world-underlay cache files."""
    if directory is None:
        from sailworks.core.storage import DATA_DIR

        return os.path.join(DATA_DIR, CACHE_SUBDIR)
    return os.fspath(directory)


def _path_state(path):
    try:
        info = os.stat(path)
    except OSError:
        return None
    return [info.st_size, info.st_mtime_ns]


def _directory_state(path):
    """State of a directory source: its members decide the raster too."""
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return []
    entries = []
    for name in names:
        child = os.path.join(path, name)
        state = _path_state(child)
        if state is not None:
            entries.append([name, state])
    return entries


def _source_entry(source):
    path = os.fspath(getattr(source, "path", "") or "")
    entry = [
        str(getattr(source, "kind", "") or ""),
        path,
        _path_state(path) if path else None,
        bool(getattr(source, "downscale", False)),
    ]
    if path and os.path.isdir(path):
        entry.append(_directory_state(path))
    return entry


def fingerprint(sources, palette, factor):
    """A stable digest of everything that determines the rendered raster."""
    entries = sorted(
        (_source_entry(source) for source in sources),
        key=lambda entry: (entry[0], entry[1]),
    )
    payload = {
        "version": CACHE_FORMAT_VERSION,
        "zoom": WORLD_ZOOM,
        "size": WORLD_SIZE,
        "factor": int(factor),
        "palette": str(palette),
        "sources": entries,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def path_for(sources, palette, factor, directory=None):
    """The cache file that holds the raster for these inputs."""
    return os.path.join(
        cache_root(directory),
        _FILENAME_PREFIX + fingerprint(sources, palette, factor) + _FILENAME_SUFFIX,
    )


def _encode(image):
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    try:
        if not image.save(buffer, "PNG"):
            return None
        return (
            _MAGIC
            + _HEADER.pack(int(image.format().value), float(image.devicePixelRatio()))
            + bytes(buffer.data())
        )
    finally:
        buffer.close()


def _prune(directory, keep_path):
    """Delete the oldest ``world-*.cache`` files, keeping the newest few.

    ``keep_path`` is never removed, even if its timestamp looks stale.  Every
    failure is local to one file: it is logged and the remaining entries are
    still considered.
    """
    try:
        names = os.listdir(directory)
    except OSError as exc:
        logger.debug("World underlay cache prune listing failed: %s", exc)
        return
    entries = []
    for name in names:
        if not (name.startswith(_FILENAME_PREFIX) and name.endswith(_FILENAME_SUFFIX)):
            continue
        path = os.path.join(directory, name)
        try:
            entries.append((os.stat(path).st_mtime_ns, path))
        except OSError as exc:
            logger.debug("World underlay cache prune stat failed: %s", exc)
    entries.sort(reverse=True)
    survivors = {path for _, path in entries[:MAX_CACHE_FILES]}
    survivors.add(keep_path)
    for _, path in entries:
        if path in survivors:
            continue
        try:
            os.unlink(path)
        except OSError as exc:
            logger.debug("World underlay cache prune unlink failed: %s", exc)


def save(path, image):
    """Write ``image`` to ``path`` atomically; report whether it was written."""
    if image is None or image.isNull() or image.width() < 1 or image.height() < 1:
        return False
    directory = os.path.dirname(os.path.abspath(path))
    temp_path = None
    try:
        os.makedirs(directory, exist_ok=True)
        data = _encode(image)
        if not data:
            return False
        handle, temp_path = tempfile.mkstemp(
            prefix=".world-", suffix=".part", dir=directory
        )
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temp_path, path)
        temp_path = None
        # Pruning is advisory: a failure here never fails the save.
        try:
            _prune(directory, os.path.abspath(path))
        except Exception as exc:  # noqa: BLE001 - the cache must never raise
            logger.debug("World underlay cache prune failed: %s", exc)
        return True
    except Exception as exc:  # noqa: BLE001 - the cache must never raise
        logger.debug("World underlay cache write failed: %s", exc)
        return False
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def load(path):
    """Return the cached image at ``path``, or ``None``; never raises."""
    try:
        with open(path, "rb") as stream:
            data = stream.read()
        if not data.startswith(_MAGIC):
            return None
        body = data[len(_MAGIC) :]
        if len(body) <= _HEADER.size:
            return None
        image_format, ratio = _HEADER.unpack_from(body, 0)
        payload = body[_HEADER.size :]
        if not payload:
            return None
        image = QImage.fromData(payload)
        if image is None or image.isNull():
            return None
        if image.width() < 1 or image.height() < 1:
            return None
        if image.format() != QImage.Format(image_format):
            image = image.convertedTo(QImage.Format(image_format))
            if image is None or image.isNull():
                return None
        if ratio > 0 and image.devicePixelRatio() != ratio:
            image.setDevicePixelRatio(ratio)
        return image
    except Exception as exc:  # noqa: BLE001 - the cache must never raise
        logger.debug("World underlay cache read failed: %s", exc)
        return None


def load_for(sources, palette, factor, directory=None):
    """Load the cached raster for these inputs, or ``None``."""
    try:
        return load(path_for(sources, palette, factor, directory))
    except Exception as exc:  # noqa: BLE001 - the cache must never raise
        logger.debug("World underlay cache lookup failed: %s", exc)
        return None


def save_for(sources, palette, factor, image, directory=None):
    """Store the raster for these inputs; report whether it was written."""
    try:
        return save(path_for(sources, palette, factor, directory), image)
    except Exception as exc:  # noqa: BLE001 - the cache must never raise
        logger.debug("World underlay cache store failed: %s", exc)
        return False
