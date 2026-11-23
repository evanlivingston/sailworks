"""Chart rendering over the Canvas seam, used by the Qt frontend.

Portrayal policy lives in :mod:`sailworks.rendering.scene`. The CPU raster
backend is Qt's ``QPainter``; cairo was removed in #267.
"""
