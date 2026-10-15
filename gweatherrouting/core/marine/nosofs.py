"""NOAA CO-OPS Operational Forecast Systems via OPeNDAP hyperslabs.

Full NOS NetCDF files run 56-73MB per hour, so this provider never
downloads whole files. Static grids come down once per run; hourly
surface currents come down as index-window hyperslabs around the
requested area. Staggered ROMS components are averaged to rho points,
rotated to true east/north, and resampled onto a regular grid, which
is the only shape downstream code consumes.
"""

import logging
import calendar
import math
import re
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timedelta, timezone

import numpy as np
from netCDF4 import Dataset, num2date

from .chs import DownloadCancelledError
from .fields import Area, CurrentField
from .models import canonical_json, utc_time
from .roms import (
    destagger_to_rho,
    output_grid,
    prep_window,
    regular_field,
    rotate_to_east_north,
)

logger = logging.getLogger("gweatherrouting")

THREDDS = "https://opendap.co-ops.nos.noaa.gov/thredds/"
DODS = THREDDS + "dodsC/"

NOSOFS_DOMAINS = {
    # WCOFS: ROMS, ~4km, 24N-54N, hourly surface fields to ~72h.
    "wcofs": ("WCOFS", 4000, Area(-140, 22, -110, 55)),
}

_attribution = "U.S. NOAA Center for Operational Oceanographic Products"


def _catalog(url):
    import requests

    response = requests.get(url, timeout=(5, 30))
    response.raise_for_status()
    root = ElementTree.fromstring(response.content)
    paths = set()
    refs = set()
    for node in root.iter():
        if node.tag.endswith("dataset") and node.get("urlPath"):
            paths.add(node.get("urlPath"))
        if node.tag.endswith("catalogRef") and node.get(
            "{http://www.w3.org/1999/xlink}href"
        ):
            refs.add(node.get("{http://www.w3.org/1999/xlink}href"))
    return paths, refs


def _ref_name(ref):
    return ref.replace("/catalog.xml", "").rstrip("/").split("/")[-1]


class NOSOFSProvider:
    def _discover(self, domain, client, start=None, end=None):
        del client  # discovery is a few small XML listings, never cached blobs
        label = NOSOFS_DOMAINS[domain][0]
        model = "NOAA/" + label + "/MODELS"
        years = sorted(
            (
                _ref_name(ref)
                for ref in _catalog(THREDDS + f"catalog/{model}/catalog.xml")[1]
            ),
            reverse=True,
        )[:2]
        for year in years:
            months = sorted(
                (
                    _ref_name(ref)
                    for ref in _catalog(
                        THREDDS + f"catalog/{model}/{year}/catalog.xml"
                    )[1]
                ),
                reverse=True,
            )[:2]
            for month in months:
                days = sorted(
                    (
                        _ref_name(ref)
                        for ref in _catalog(
                            THREDDS + f"catalog/{model}/{year}/{month}/catalog.xml"
                        )[1]
                    ),
                    reverse=True,
                )[:4]
                for day in days:
                    paths, _ = _catalog(
                        THREDDS + f"catalog/{model}/{year}/{month}/{day}/catalog.xml"
                    )
                    runs = {}
                    for path in paths:
                        match = re.search(
                            r"\.t(\d\d)z\.(\d{8})\.2ds\.f(\d+)\.nc$", path
                        )
                        if match:
                            runs.setdefault(
                                (match.group(2), match.group(1)), []
                            ).append(int(match.group(3)))
                    for date, cycle in sorted(runs, reverse=True):
                        hours = sorted(runs[(date, cycle)])
                        if 1 not in hours:
                            continue
                        run = datetime.strptime(f"{date}T{cycle}", "%Y%m%dT%H").replace(
                            tzinfo=timezone.utc
                        )
                        if start is not None and run > utc_time(start):
                            continue
                        prefix = f"{label.lower()}.t{cycle}z.{date}.2ds.f"
                        base = DODS + f"{model}/{year}/{month}/{day}/"
                        return base, prefix, run, hours
        raise ValueError(f"No complete {label} run advertised by CO-OPS THREDDS")

    def download(self, domain, area, start, end, client, progress=None):
        if client.cancel.is_set():
            raise DownloadCancelledError()
        label, metres, _ = NOSOFS_DOMAINS[domain]
        base, prefix, run, available = self._discover(domain, client, start, end)
        first = max(
            min(available), math.floor((utc_time(start) - run).total_seconds() / 3600)
        )
        last = min(
            max(available), math.ceil((utc_time(end) - run).total_seconds() / 3600)
        )
        if first >= last or first not in available:
            raise ValueError(
                f"{label} run {run.isoformat()} cannot cover the requested window"
            )
        wanted = [h for h in range(first, last + 1) if h in available]
        if len(wanted) < 2:
            raise ValueError(f"{label} run {run.isoformat()} has too few usable hours")

        statics = self._read_statics(base, prefix, available[0], area, client)
        east, north, times = [], [], []
        latitudes, longitudes = statics["lats"], statics["lons"]
        for position, hour in enumerate(wanted):
            if client.cancel.is_set():
                raise DownloadCancelledError()
            url = base + f"{prefix}{hour:03d}.nc"
            values, valid_at = self._read_hour(url, statics, client)
            expect = (run + timedelta(hours=hour)).timestamp()
            if abs(valid_at - expect) > 3600:
                raise ValueError(f"{label} file and forecast clock disagree")
            times.append(valid_at)
            east.append(values[0])
            north.append(values[1])
            if progress:
                progress(f"{label}: hour {position + 1} of {len(wanted)}")
        metadata = dict(
            schema_version=1,
            provider="noaa-coops",
            model=label,
            resolution_m=metres,
            run=run.isoformat(),
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            depth_m=0.0,
            composition="total ocean current including tides",
            vector_reference="true_east_north",
            vector_basis="ROMS staggered components averaged to rho, rotated by angle",
            routing_qualified=False,
            requested_area=area.to_json(),
            sources=statics["sources"],
            attribution=_attribution,
        )
        return CurrentField(
            canonical_json(metadata),
            np.array(times),
            latitudes,
            longitudes,
            np.array(east),
            np.array(north),
        )

    def _read_statics(self, base, prefix, hour, area, client):
        url = base + f"{prefix}{hour:03d}.nc"
        dataset = Dataset(url)
        try:
            lat_full = np.asarray(dataset.variables["lat_rho"][:])
            lon_full = np.asarray(dataset.variables["lon_rho"][:])
            e0, e1, x0, x1 = prep_window(lat_full, lon_full, area)
            lat_rho = np.asarray(dataset.variables["lat_rho"][e0:e1, x0:x1])
            lon_rho = np.asarray(dataset.variables["lon_rho"][e0:e1, x0:x1])
            angle = np.asarray(dataset.variables["angle"][e0:e1, x0:x1])
            mask = np.asarray(dataset.variables["mask_rho"][e0:e1, x0:x1])
            lats, lons = output_grid(area)
            self._record(client, url, lat_rho.nbytes + lon_rho.nbytes)
        finally:
            dataset.close()
        return {
            "window": (e0, e1, x0, x1),
            "lat_rho": lat_rho,
            "lon_rho": lon_rho,
            "angle": angle,
            "mask": mask,
            "lats": lats,
            "lons": lons,
            "sources": list(client.sources[-1:]),
        }

    def _read_hour(self, url, statics, client):
        e0, e1, x0, x1 = statics["window"]
        dataset = Dataset(url)
        try:
            # u sits between rho columns, v between rho rows: widen by one
            # face on each staggered axis so the average covers the window.
            eu0, eu1 = max(e0, 0), e1
            xu0, xu1 = max(x0 - 1, 0), x1
            ev0, ev1 = max(e0 - 1, 0), e1
            xv0, xv1 = max(x0, 0), x1
            u = np.asarray(dataset.variables["u_sur"][0, eu0:eu1, xu0:xu1])
            v = np.asarray(dataset.variables["v_sur"][0, ev0:ev1, xv0:xv1])
            time_var = dataset.variables["ocean_time"]
            valid_at = float(
                calendar.timegm(num2date(time_var[0], time_var.units).timetuple())
            )
            self._record(client, url, u.nbytes + v.nbytes)
        finally:
            dataset.close()
        u_rho, v_rho, _ = destagger_to_rho(u, v)
        # Trim each padded axis back to the rho window: u was widened in
        # xi only, v in eta only.
        shape = statics["lat_rho"].shape
        u_rho = u_rho[:, (x0 - xu0) : (x0 - xu0) + shape[1]]
        v_rho = v_rho[(e0 - ev0) : (e0 - ev0) + shape[0], :]
        east_rho, north_rho = rotate_to_east_north(u_rho, v_rho, statics["angle"])
        grid_lats = np.repeat(statics["lats"], len(statics["lons"]))
        grid_lons = np.tile(statics["lons"], len(statics["lats"]))
        east, north = regular_field(
            statics["lat_rho"],
            statics["lon_rho"],
            east_rho,
            north_rho,
            statics["mask"],
            grid_lats,
            grid_lons,
        )
        rows = len(statics["lats"])
        cols = len(statics["lons"])
        return (east.reshape(rows, cols), north.reshape(rows, cols)), valid_at

    @staticmethod
    def _record(client, url, size):
        import hashlib

        client.sources.append(
            dict(
                url=url,
                sha256=hashlib.sha256(url.encode()).hexdigest(),
                bytes=size,
                retrieved_at=datetime.now(timezone.utc).isoformat(),
            )
        )
