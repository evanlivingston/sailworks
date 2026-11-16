# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
"""
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

For detail about GNU see <http://www.gnu.org/licenses/>.
"""

# isort:skip_file
from importlib import import_module

# Re-exports that pull a heavy module are resolved on first use (PEP 562) so
# importing ``sailworks.core`` does not pay for them at import time.  The hook
# is defined before the imports below because ``.core`` itself imports from
# this package while it is still initialising.
_LAZY_EXPORTS = {"PolarManager": ".polarmanager"}


def __getattr__(name):
    """Import one deferred re-export on first access."""
    module = _LAZY_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    """Keep deferred re-exports visible to attribute discovery."""
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


from .grib import Grib  # noqa: F401
from .gribmanager import GribManager  # noqa: F401
from .timecontrol import TimeControl  # noqa: F401
from .datasource import DataSource  # noqa: F401
from .datasource import NMEADataPacket  # noqa: F401
from .datasource import DataPacket  # noqa: F401
from .datasource import AISDataPacket  # noqa: F401
from .aismanager import AISManager, AISTarget  # noqa: F401
from .serialdatasource import SerialDataSource  # noqa: F401
from .networkdatasource import NetworkDataSource  # noqa: F401
from .connectionmanager import ConnectionManager  # noqa: F401
from .core import Core  # noqa: F401
from .utils import EventDispatcher  # noqa: F401
