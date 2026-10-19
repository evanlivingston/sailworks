"""GPU-accelerated weather rendering with instant time scrubbing.

Pre-computes every forecast hour's U/V bands as GPU textures at startup.
Scrubbing the forecast time then becomes a texture lookup with optional
two-texture blending, instead of a ~0.7s CPU resampling pass.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import numpy as np
from osgeo import gdal
from PySide6.QtGui import QImage
from PySide6.QtQuick import QSGTexture, QQuickWindow

logger = logging.getLogger("gweatherrouting")


def _grib_number(value) -> float:
    """GDAL versions expose times both as integers and '123 sec UTC'."""
    return float(str(value).split()[0])


class GPUBandTexture:
    """A single GRIB band uploaded as a GPU texture."""

    def __init__(self, texture: QSGTexture, width: int, height: int):
        self.texture = texture
        self.width = width
        self.height = height


class GPUWeatherCache:
    """Cache of GRIB U/V bands as GPU textures, keyed by forecast hour.

    At startup, every forecast hour's U and V bands are sampled to numpy
    arrays and uploaded as GPU textures. Scrubbing the forecast time then
    becomes a texture lookup (or two-texture blend between adjacent hours)
    instead of a full CPU resampling pass.
    """

    def __init__(self, window: QQuickWindow):
        self.window = window
        # hour -> {"u": GPUBandTexture, "v": GPUBandTexture}
        self.textures: dict[int, dict[str, GPUBandTexture]] = {}
        self._start_time: Optional[datetime] = None
        self._initialized = False

    def initialize(
        self,
        grib_dataset: gdal.Dataset,
        start_time: datetime,
    ) -> None:
        """Upload every forecast hour's U and V bands as GPU textures.

        Runs once at startup (~0.5s per hour). Scrubbing afterward is instant.
        Also builds texture arrays for shader-based interpolation.
        """
        if self._initialized:
            return

        self._start_time = start_time

        # Find wind bands (UGRD/VGRD) grouped by forecast hour
        band_hours: dict[int, dict[str, int]] = {}
        for bidx in range(1, grib_dataset.RasterCount + 1):
            band = grib_dataset.GetRasterBand(bidx)
            metadata = band.GetMetadata()
            element = metadata.get("GRIB_ELEMENT")
            if element not in ("UGRD", "VGRD"):
                continue
            forecast_seconds = _grib_number(metadata.get("GRIB_FORECAST_SECONDS", "0"))
            hour = int(forecast_seconds / 3600)
            band_hours.setdefault(hour, {})[element] = bidx

        self._hours = sorted(band_hours.keys())
        self._hour_to_idx = {h: i for i, h in enumerate(self._hours)}

        # Upload individual hour textures (for fallback/blending)
        for hour in self._hours:
            bands = band_hours[hour]
            if "u" not in bands or "v" not in bands:
                continue
            u_band = grib_dataset.GetRasterBand(bands["u"])
            v_band = grib_dataset.GetRasterBand(bands["v"])
            u_data = np.asarray(u_band.ReadAsArray(), dtype=np.float32)
            v_data = np.asarray(v_band.ReadAsArray(), dtype=np.float32)
            self.textures[hour] = {
                "u": self._upload_texture(u_data),
                "v": self._upload_texture(v_data),
            }

        # Build texture arrays for shader-based interpolation
        self._build_texture_arrays()

        self._initialized = True
        logger.info(
            "GPU weather cache: %d hours uploaded (%d textures)",
            len(self.textures),
            len(self.textures) * 2,
        )

    def _upload_texture(self, data: np.ndarray) -> GPUBandTexture:
        """Upload a float32 array as an RGBA8 GPU texture.

        Normalizes data to [0,1] and encodes into R channel (other channels
        zero). The shader decodes back to [-1, 1] wind components.
        """
        h, w = data.shape
        dmin, dmax = float(np.nanmin(data)), float(np.nanmax(data))
        if dmax > dmin:
            norm = (data - dmin) / (dmax - dmin)
        else:
            norm = np.zeros_like(data)
        norm = np.nan_to_num(norm, nan=0.5)

        # Encode [-1,1] wind component into [0,1] texture value: R channel only
        tex_data = np.zeros((h, w, 4), dtype=np.uint8)
        tex_data[..., 0] = (norm * 255).astype(np.uint8)  # R = wind component
        tex_data[..., 3] = 255  # A = opaque

        image = QImage(tex_data.data, w, h, w * 4, QImage.Format.Format_RGBA8888)
        image = image.copy()  # QImage does not own numpy data

        texture = self.window.createTextureFromImage(image)
        return GPUBandTexture(texture, w, h)

    def get_textures(self, timestamp: datetime) -> Optional[tuple]:
        """Return (u0, v0, u1, v1, blend) for the given timestamp.

        u0/v0 = textures for floor hour, u1/v1 = textures for ceil hour,
        blend = fractional hour (0.0 = exact hour, 0.5 = halfway).
        Returns None if no textures available.
        """
        if not self._initialized or not self.textures:
            return None

        if self._start_time is None:
            return None

        elapsed_hours = (timestamp.timestamp() - self._start_time.timestamp()) / 3600.0
        hour = int(elapsed_hours)
        frac = elapsed_hours - hour

        # Exact hour match
        if hour in self.textures and abs(frac) < 1e-6:
            tex = self.textures[hour]
            return tex["u"], tex["v"], tex["u"], tex["v"], 0.0

        # Blend between hour and hour+1
        if hour in self.textures and (hour + 1) in self.textures:
            return (
                self.textures[hour]["u"],
                self.textures[hour]["v"],
                self.textures[hour + 1]["u"],
                self.textures[hour + 1]["v"],
                frac,
            )

        # Fallback: nearest hour
        nearest = min(self.textures.keys(), key=lambda h: abs(h - hour))
        tex = self.textures[nearest]
        return tex["u"], tex["v"], tex["u"], tex["v"], 0.0

    def _build_texture_arrays(self) -> None:
        """Report that the texture-array path was not completed.

        A full 3D texture array would need a QSGTextureProvider and is more
        complex than the two-texture blend the shader actually uses, so this
        experiment keeps the individual per-hour textures. The method is
        retained so the abandoned approach is visible rather than implied.
        """
        if len(self.textures) < 2:
            return
        logger.debug("Texture array build skipped - using two-texture blend approach")
