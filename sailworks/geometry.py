"""Routing geometry backend selection.

`weatherrouting.utils` performs geodesic operations through `latlon`, which
constructs a fresh `pyproj.Geod` for every call. Routing calls those functions
millions of times, so the hot path can be served by the small native kernel in
``native/geometry`` instead. The Python implementation stays installed as the
fallback and as the equivalence reference.

The backend is selected once when the package is imported. Set
``SAILWORKS_NATIVE_GEOMETRY=0`` (or false/no/off) to force the Python path.
"""

import os
from types import MappingProxyType

from weatherrouting import utils as _routing_utils

_FUNCTIONS = (
    "point_distance",
    "lossodromic",
    "ortodromic",
    "routage_point_distance",
)

# Keep the unpatched Python implementations for fallback and tests.
REFERENCE = MappingProxyType(
    {name: getattr(_routing_utils, name) for name in _FUNCTIONS}
)

try:
    from . import _geometry_native
except ImportError:  # The extension is optional; a source checkout may omit Rust.
    _geometry_native = None

NATIVE_AVAILABLE = _geometry_native is not None
_backend = "python"


def _native_requested():
    value = os.environ.get("SAILWORKS_NATIVE_GEOMETRY", "1")
    return value.strip().lower() not in {"0", "false", "no", "off"}


def set_backend(backend=None):
    """Install one backend into `weatherrouting.utils` and return its name.

    ``backend`` defaults to native when the extension is importable and not
    disabled by the environment, otherwise Python.
    """
    global _backend

    if backend is None:
        backend = "native" if NATIVE_AVAILABLE and _native_requested() else "python"
    if backend == "native":
        if not NATIVE_AVAILABLE:
            raise RuntimeError("the native routing geometry extension is not available")
        source = _geometry_native
        functions = {name: getattr(source, name) for name in _FUNCTIONS}
    elif backend == "python":
        functions = REFERENCE
    else:
        raise ValueError("backend must be 'native' or 'python'")

    for name in _FUNCTIONS:
        setattr(_routing_utils, name, functions[name])
    _backend = backend
    return backend


def active_backend():
    """Return the backend currently installed in `weatherrouting.utils`."""
    return _backend


def install():
    """Select and install the backend for this process."""
    return set_backend()


install()
