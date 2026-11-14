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

from functools import lru_cache
from pathlib import Path

# isort:skip_file
from . import geometry as _geometry  # noqa: F401  Install before routing is imported.

from . import core  # noqa: F401

__version__ = "0.3.6"


@lru_cache(maxsize=1)
def build_label() -> str:
    """Name of the source tree holding this package, to tell builds apart."""
    try:
        return Path(__file__).resolve().parent.parent.name
    except Exception:
        return ""
