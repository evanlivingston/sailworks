"""Bounded regional downloads and optional authenticated worldwide subsets."""

import hashlib
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import List
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import numpy as np
import requests
from osgeo import gdal

from .chs import DownloadCancelledError
from .fields import Area, CurrentField
from .models import canonical_json, utc_time

CIOPS_URL = "https://dd.weather.gc.ca/today/model_ciops/"
DATAMART_URL = "https://dd.weather.gc.ca/"
CIOPS_DOMAINS = {
    "salish-sea": (
        "500m",
        "SalishSea",
        "0.008x0.005",
        500,
        Area(-126.2, 47, -121.11, 50.9915),
    ),
    "west": ("2km", "West", "0.03x0.02", 2000, Area(-140, 44.3, -122.03, 59.98)),
}
SMOC = "cmems_mod_glo_phy_anfc_merged-uv_PT1H-i"


class DownloadClient:
    CACHE_LIMIT = 256 * 1024 * 1024

    def __init__(self, directory, cancel, session=None, cache_directory=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.cancel = cancel
        self.session = session or requests.Session()
        self.total = 0
        self.sources = []
        self.cache_directory = Path(cache_directory or directory)
        self.cache_directory.mkdir(parents=True, exist_ok=True)
        self._verified = {}

    @staticmethod
    def _immutable(url):
        # Directory listings and `today` discovery must always be refreshed.
        # Component filenames pin a run and lead hour. Revalidate on a new client
        # so a provider correction to the same run is still observed.
        return bool(re.search(r"/\d{8}T\d{2}Z_MSC_CIOPS-[^/?]+\.nc$", url))

    def _cached(self, key):
        path = self.cache_directory / key
        metadata = path.with_suffix(".json")
        try:
            saved = json.loads(metadata.read_text())
            source = saved["source"]
            if (
                path.stat().st_size != source["bytes"]
                or source["bytes"] > 8 * 1024 * 1024
            ):
                return None
            payload = path.read_bytes()
            if (
                len(payload) != source["bytes"]
                or source["bytes"] > 8 * 1024 * 1024
                or hashlib.sha256(payload).hexdigest() != source["sha256"]
            ):
                return None
            path.touch()
            # Keep each calculation's bytes stable if another client refreshes
            # or evicts the shared entry while this provider is reading it.
            local = self.directory / key
            if local != path:
                local.write_bytes(payload)
            return local, saved
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _store_cache(self, key, path, saved):
        from .manager import atomic_json

        target = self.cache_directory / key
        if path != target:
            with tempfile.NamedTemporaryFile(
                dir=self.cache_directory, delete=False
            ) as handle:
                temporary = Path(handle.name)
                try:
                    handle.write(path.read_bytes())
                    handle.close()
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
        atomic_json(target.with_suffix(".json"), saved)
        entries = []
        for entry in self.cache_directory.iterdir():
            if re.fullmatch(r"[0-9a-f]{64}", entry.name):
                try:
                    stat = entry.stat()
                    entries.append((stat.st_mtime, stat.st_size, entry))
                except FileNotFoundError:
                    pass
        size = sum(e[1] for e in entries)
        for _, count, entry in sorted(entries):
            if size <= self.CACHE_LIMIT:
                break
            if entry == target:
                continue
            entry.unlink(missing_ok=True)
            entry.with_suffix(".json").unlink(missing_ok=True)
            size -= count
        return path

    def _write_response(self, response, path):
        digest = hashlib.sha256()
        size = 0
        with path.open("wb") as handle:
            for chunk in response.iter_content(65536):
                if self.cancel.is_set():
                    raise DownloadCancelledError()
                size += len(chunk)
                self.total += len(chunk)
                if size > 8 * 1024 * 1024 or self.total > 256 * 1024 * 1024:
                    raise ValueError("Spatial download exceeds its size limit")
                digest.update(chunk)
                handle.write(chunk)
        return dict(
            url=response.url,
            sha256=digest.hexdigest(),
            bytes=size,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
        )

    def get(self, url):
        if self.cancel.is_set():
            raise DownloadCancelledError()
        key = hashlib.sha256(url.encode()).hexdigest()
        immutable = self._immutable(url)
        cached = None
        headers = {}
        if immutable:
            if url in self._verified:
                path, source = self._verified[url]
                if path.exists():
                    self.sources.append(source)
                    return path
            cached = self._cached(key)
            if cached is not None:
                _, saved = cached
                if saved.get("etag"):
                    headers["If-None-Match"] = saved["etag"]
                elif saved.get("last_modified"):
                    headers["If-Modified-Since"] = saved["last_modified"]
        path = self.directory / key
        with self.session.get(
            url, stream=True, timeout=(5, 30), headers=headers
        ) as response:
            if response.status_code == 304 and cached is not None and headers:
                path, saved = cached
                self.sources.append(saved["source"])
                self._verified[url] = (path, saved["source"])
                return path
            response.raise_for_status()
            source = self._write_response(response, path)
            self.sources.append(source)
            saved = dict(
                source=source,
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )
        if immutable:
            path = self._store_cache(key, path, saved)
            self._verified[url] = (path, source)
        return path


def read_component(path, name, area, geographic_axes=False):  # noqa: C901
    """Read explicit CF coordinates, never a raster driver's default transform."""
    dataset = gdal.OpenEx(str(path), gdal.OF_MULTIDIM_RASTER)
    if dataset is None:
        raise ValueError("Unable to open current NetCDF")
    root = dataset.GetRootGroup()
    variable = root.OpenMDArray(name)
    if variable is None or variable.GetUnit() not in ("m s-1", "m/s", "m s**-1"):
        raise ValueError("Missing velocity variable or unsupported units: " + name)
    variable_attrs = {a.GetName(): a.Read() for a in variable.GetAttributes()}
    if geographic_axes:
        if variable_attrs.get("grid_mapping") != "latitude_longitude":
            raise ValueError("CIOPS velocity is not on the supported geographic axes")
        reference = root.OpenMDArray("latitude_longitude")
        if reference is not None:
            mapping_attrs = {a.GetName(): a.Read() for a in reference.GetAttributes()}
            if (
                mapping_attrs.get("grid_mapping_name", "latitude_longitude")
                != "latitude_longitude"
            ):
                raise ValueError("Unsupported rotated current grid")
    lat = root.OpenMDArray("latitude")
    lon = root.OpenMDArray("longitude")
    clock = root.OpenMDArray("time")
    if any(a is None for a in (lat, lon, clock)):
        raise ValueError("Missing geographic current coordinates")
    latitudes = np.asarray(lat.ReadAsArray(), dtype=float)
    longitudes = (np.asarray(lon.ReadAsArray(), dtype=float) + 180) % 360 - 180
    units = clock.GetUnit()
    scales = {"seconds": 1, "hours": 3600, "days": 86400}
    scale, separator, epoch = units.partition(" since ")
    calendar = {a.GetName(): a.Read() for a in clock.GetAttributes()}.get("calendar")
    if (
        not separator
        or scale not in scales
        or (
            calendar
            and calendar not in ("gregorian", "standard", "proleptic_gregorian")
        )
    ):
        raise ValueError("Unsupported current forecast clock")
    epoch = datetime.fromisoformat(epoch.replace("Z", "+00:00"))
    if epoch.tzinfo is None:
        epoch = epoch.replace(tzinfo=timezone.utc)
    times = (
        np.asarray(clock.ReadAsArray(), dtype=float) * scales[scale] + epoch.timestamp()
    )
    values = np.asarray(variable.ReadAsArray(), dtype=float)
    dimensions = [d.GetName() for d in variable.GetDimensions()]
    if (
        dimensions == ["time", "depth", "latitude", "longitude"]
        and values.shape[1] == 1
    ):
        values = values[:, 0]
        dimensions.remove("depth")
    if dimensions != ["time", "latitude", "longitude"] or values.shape != (
        len(times),
        len(latitudes),
        len(longitudes),
    ):
        raise ValueError("Unsupported current dimension order")
    nodata = variable.GetNoDataValueAsDouble()
    invalid = ~np.isfinite(values) | (np.abs(values) > 20)
    if nodata is not None:
        invalid |= values == nodata
    for key, compare in (("valid_min", np.less), ("valid_max", np.greater)):
        if key in variable_attrs:
            invalid |= compare(values, float(variable_attrs[key]))
    values[invalid] = np.nan
    yi, xi = np.argsort(latitudes), np.argsort(longitudes)
    latitudes, longitudes = latitudes[yi], longitudes[xi]
    values = values[:, yi][:, :, xi]
    if latitudes.ndim != 1 or longitudes.ndim != 1:
        raise ValueError("Curvilinear coordinates require a separate adapter")
    # Keep one surrounding cell on each side so interpolation covers the request.
    y0 = max(0, int(np.searchsorted(latitudes, area.south)) - 1)
    y1 = min(
        len(latitudes), int(np.searchsorted(latitudes, area.north, side="right")) + 1
    )
    x0 = max(0, int(np.searchsorted(longitudes, area.west)) - 1)
    x1 = min(
        len(longitudes), int(np.searchsorted(longitudes, area.east, side="right")) + 1
    )
    attrs = {a.GetName(): a.Read() for a in root.GetAttributes()}
    return times, latitudes[y0:y1], longitudes[x0:x1], values[:, y0:y1, x0:x1], attrs


class CIOPSProvider:
    @staticmethod
    def _listing(client, url):
        try:
            return client.get(url).read_text()
        except requests.HTTPError as error:
            if error.response is not None and error.response.status_code == 404:
                return ""
            raise

    def _discover(self, domain, client, start=None, end=None):
        resolution, label, spacing, _, _ = CIOPS_DOMAINS[domain]
        # `today` can be empty at UTC rollover and can change during a download.
        # Retained daily directories give each request a stable run location.
        root = self._listing(client, DATAMART_URL)
        dates = sorted(set(re.findall(r'href="(\d{8})/"', root)), reverse=True)[:3]
        bases = [DATAMART_URL + date + "/WXO-DD/model_ciops/" for date in dates]
        bases.append(CIOPS_URL)
        for parent in bases:
            base = parent + domain + "/" + resolution + "/"
            found = self._run_at(base, label, spacing, client, start, end)
            if found:
                return base, *found
        raise ValueError("No complete CIOPS run advertised for " + domain)

    def _run_at(self, base, label, spacing, client, start=None, end=None):
        listing = self._listing(client, base)
        cycles = sorted(
            set(re.findall(r'href="((?:00|06|12|18)/)"', listing)), reverse=True
        )
        for cycle in cycles:
            listing = self._listing(client, urljoin(base, cycle + "000/"))
            names = re.findall(
                r'href="(\d{8}T\d{2}Z_MSC_CIOPS-'
                + label
                + r"_SeaWaterVelocityX_DBS-0\.5m_LatLon"
                + re.escape(spacing)
                + r'_PT000H\.nc)"',
                listing,
            )
            if (
                len(set(names)) == 1
                and names[0].replace("VelocityX", "VelocityY") in listing
            ):
                template = names[0]
                run = datetime.strptime(template[:11], "%Y%m%dT%H").replace(
                    tzinfo=timezone.utc
                )
                if start is not None and run > utc_time(start):
                    continue
                if end is not None and run + timedelta(hours=48) < utc_time(end):
                    continue
                return cycle, template
        return None

    def download(self, domain, area, start, end, client, progress=None):  # noqa: C901
        _, label, _, metres, _ = CIOPS_DOMAINS[domain]
        base, cycle, template = self._discover(domain, client, start, end)
        run = datetime.strptime(template[:11], "%Y%m%dT%H").replace(tzinfo=timezone.utc)
        first = max(0, math.floor((utc_time(start) - run).total_seconds() / 3600))
        last = min(48, math.ceil((utc_time(end) - run).total_seconds() / 3600))
        if first >= last:
            raise ValueError(
                "CIOPS "
                + domain
                + " covers "
                + run.isoformat()
                + " to "
                + (run + timedelta(hours=48)).isoformat()
                + "; choose a window within it"
            )
        east, north = [], []
        times: List[float] = []
        latitudes = longitudes = np.array([])
        attrs = {}
        source_start = len(client.sources)
        for hour in range(first, last + 1):
            pair = []
            for component, name in (("X", "vozocrtx"), ("Y", "vomecrty")):
                filename = template.replace(
                    "VelocityX", "Velocity" + component
                ).replace("PT000H", "PT%03dH" % hour)
                path = client.get(urljoin(base, cycle + "%03d/" % hour + filename))
                pair.append(read_component(path, name, area, geographic_axes=True))
            a, b = pair
            if any(not np.array_equal(a[i], b[i]) for i in range(3)) or len(a[0]) != 1:
                raise ValueError("CIOPS X/Y coordinates or clocks disagree")
            if abs(a[0][0] - (run + timedelta(hours=hour)).timestamp()) > 1:
                raise ValueError("CIOPS filename and forecast clock disagree")
            if times and (
                not np.array_equal(latitudes, a[1])
                or not np.array_equal(longitudes, a[2])
            ):
                raise ValueError("CIOPS grids changed within one download")
            times.append(a[0][0])
            latitudes, longitudes, attrs = a[1], a[2], a[4]
            east.append(a[3][0])
            north.append(b[3][0])
            if progress:
                progress("CIOPS " + label + ": hour %d of %d" % (hour, last))
        metadata = dict(
            schema_version=1,
            provider="eccc-ciops",
            model="CIOPS-" + label,
            resolution_m=metres,
            run=run.isoformat(),
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            depth_m=0.5,
            composition="total ocean current including tides",
            vector_reference="true_east_north",
            vector_basis="CF geographic latitude_longitude grid X/Y axes",
            routing_qualified=False,
            requested_area=area.to_json(),
            sources=client.sources[source_start:],
            original_metadata=attrs,
            attribution="Environment and Climate Change Canada",
            licence_url="https://eccc-msc.github.io/open-data/licence/readme_en/",
        )
        return CurrentField(
            canonical_json(metadata),
            np.array(times),
            latitudes,
            longitudes,
            np.array(east),
            np.array(north),
        )


class CopernicusProvider:
    """SMOC circulation plus tides; exclude the separately supplied Stokes drift."""

    def __init__(self, credential_store=None):
        self.credentials = None
        self.credential_store = credential_store
        self.credentials_saved = False
        self.credential_error = ""
        if credential_store is not None:
            from .credentials import CredentialStoreError

            try:
                self.credentials = credential_store.load()
                self.credentials_saved = self.credentials is not None
            except CredentialStoreError as error:
                self.credential_error = str(error)

    def configure(self, username, password):
        if not username.strip() or not password:
            raise ValueError("Enter your Copernicus username and password")
        if self.credential_store is not None:
            self.credential_store.save(username.strip(), password)
            self.credentials_saved = True
        self.credentials = (username.strip(), password)
        self.credential_error = ""

    def forget_account(self):
        if self.credential_store is not None:
            self.credential_store.forget()
        self.credentials = None
        self.credentials_saved = False
        self.credential_error = ""

    @staticmethod
    def installed():
        return importlib.util.find_spec("copernicusmarine") is not None

    def download(self, area, start, end, directory, cancel, progress=None, client=None):
        if cancel.is_set():
            raise DownloadCancelledError()
        if client is None and not self.installed():
            raise ValueError(
                "Worldwide downloads need the marine-global extra and a Copernicus login"
            )
        hours = math.ceil((utc_time(end) - utc_time(start)).total_seconds() / 3600) + 2
        estimated = (
            (math.ceil((area.east - area.west) * 12) + 3)
            * (math.ceil((area.north - area.south) * 12) + 3)
            * hours
            * 4
            * 8
        )
        if estimated > 128 * 1024 * 1024:
            raise ValueError(
                "Choose a smaller area or time window (worldwide subset limit: 128 MiB)"
            )
        arguments = dict(
            dataset_id=SMOC,
            variables=["uo", "vo", "utide", "vtide"],
            minimum_longitude=area.west,
            maximum_longitude=area.east,
            minimum_latitude=area.south,
            maximum_latitude=area.north,
            start_datetime=utc_time(start).isoformat(),
            end_datetime=utc_time(end).isoformat(),
            coordinates_selection_method="outside",
            output_directory=str(directory),
            output_filename="smoc.nc",
            file_format="netcdf",
            overwrite=True,
            disable_progress_bar=True,
        )
        if self.credentials:
            arguments.update(username=self.credentials[0], password=self.credentials[1])
        if client is None:
            path = self._subset(arguments, cancel)
        else:
            response = client.subset(**arguments)
            reported = getattr(response, "file_path", None)
            path = (
                Path(reported)
                if isinstance(reported, (str, Path))
                else Path(directory) / "smoc.nc"
            )
        if cancel.is_set():
            raise DownloadCancelledError()
        self._check_subset_path(path, directory)
        components = [
            read_component(path, name, area) for name in arguments["variables"]
        ]
        a = components[0]
        if any(
            not np.array_equal(a[i], b[i]) for b in components[1:] for i in range(3)
        ):
            raise ValueError("Copernicus component coordinates disagree")
        metadata = dict(
            schema_version=1,
            provider="copernicus-marine",
            model="SMOC circulation + tides",
            resolution_m=9000,
            run=None,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            depth_m=0,
            composition="ocean circulation plus FES2014 tides; excludes Stokes drift",
            vector_reference="true_east_north",
            routing_qualified=False,
            requested_area=area.to_json(),
            request={
                k: v
                for k, v in arguments.items()
                if not k.startswith("output_") and k not in ("username", "password")
            },
            sources=[
                dict(
                    dataset_id=SMOC,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    bytes=path.stat().st_size,
                )
            ],
            original_metadata=a[4],
            attribution="E.U. Copernicus Marine Service information",
            licence_url="https://marine.copernicus.eu/user-corner/service-commitments-and-licence",
        )
        return CurrentField(
            canonical_json(metadata),
            a[0],
            a[1],
            a[2],
            a[3] + components[2][3],
            components[1][3] + components[3][3],
        )

    @staticmethod
    def _check_subset_path(path, directory):
        if path.resolve().parent != Path(directory).resolve():
            raise ValueError(
                "Copernicus returned a file outside the download directory"
            )
        if not path.is_file():
            raise ValueError(
                "Copernicus returned no NetCDF file. Check login and forecast availability."
            )
        if path.stat().st_size > 128 * 1024 * 1024:
            raise ValueError(
                "Copernicus subset exceeds 128 MiB; choose a smaller area or time window"
            )

    @staticmethod
    def _subset(arguments, cancel):
        # Isolate the SDK so cancellation and time limits can stop its network work.
        result_path = Path(arguments["output_directory"]) / "subset-result.json"
        result_path.unlink(missing_ok=True)
        # Report only exception types, never SDK exception text/URLs that could
        # contain account details. A successful process exit alone is insufficient.
        code = """import json,sys,pathlib
args=json.load(sys.stdin)
report=args.pop('_result_file')
try:
    import copernicusmarine
    result=copernicusmarine.subset(**args)
    data=({'error_type':'DownloadAborted'} if result is None
          else {'file_path':str(result.file_path)})
except BaseException as error:
    data={'error_type':type(error).__name__}
pathlib.Path(report).write_text(json.dumps(data))
"""
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert process.stdin is not None
            process.stdin.write(
                json.dumps(dict(arguments, _result_file=str(result_path))).encode()
            )
            process.stdin.close()
            deadline = time.monotonic() + 180
            while process.poll() is None:
                if cancel.wait(0.1):
                    raise DownloadCancelledError()
                if time.monotonic() > deadline:
                    raise ValueError("Worldwide download exceeded three minutes")
            if process.returncode:
                raise ValueError(
                    "Worldwide download failed. Check Copernicus login and forecast dates."
                )
            if not result_path.is_file():
                raise ValueError(
                    "Copernicus did not complete a download. Check login and forecast availability."
                )
            result = json.loads(result_path.read_text())
            if result.get("error_type") == "DownloadAborted":
                raise ValueError(
                    "Copernicus stopped before downloading a file. "
                    "Configure Worldwide account… and retry."
                )
            if "error_type" in result:
                raise ValueError(
                    "Copernicus download failed ("
                    + result["error_type"]
                    + "). Check Copernicus login and forecast availability."
                )
            return Path(result["file_path"])
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
