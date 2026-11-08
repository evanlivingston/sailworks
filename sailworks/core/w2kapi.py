"""Client for the Actisense W2K-1 local REST API.

The W2K-1 serves a small HTTP API on its access point (``192.168.4.1`` by
default) for device information and EBL log download. This is separate from
the ActisenseCloud API: it is unauthenticated until ``/api/login`` returns a
bearer token, and it has no live-data endpoints.
"""

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import requests

DEFAULT_HOST = "192.168.4.1"
DEFAULT_USER = "admin"
CONNECT_TIMEOUT = 5
# W2K-1 stores EBL segments under this SD-card path. The device's
# /api/download expects the full path, not a folder-relative name.
EBL_BASE_PATH = "/sdcard/W2K-1/ebl_data_logs"


class W2KError(Exception):
    """Base error for W2K-1 communication."""


class W2KAuthError(W2KError):
    """The device rejected the credentials or returned no token."""


class W2KRequestError(W2KError):
    """The device returned an unexpected response."""


@dataclass(frozen=True)
class W2KDevice:
    serial_number: str
    firmware: str = ""
    raw: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class W2KFolder:
    name: str
    size: int
    in_progress: bool
    raw: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class W2KFile:
    folder: str
    name: str
    size: int
    time: int

    @property
    def key(self):
        return f"{self.folder}/{self.name}"


def base_url(host):
    """Return ``http://host`` for an address or URL typed by the user."""
    value = str(host or "").strip()
    if not value:
        raise ValueError("Enter the W2K-1 address")
    if "://" not in value:
        value = "http://" + value
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("Enter a valid W2K-1 address")
    return f"{parts.scheme}://{parts.netloc}"


def safe_component(value, kind="name"):
    """Reject device-supplied names that could escape a staging directory."""
    text = str(value or "")
    if (
        not text
        or text in (".", "..")
        or "/" in text
        or "\\" in text
        or "\x00" in text
        or Path(text).is_absolute()
    ):
        raise W2KRequestError(f"Unsafe {kind} from device: {text!r}")
    return text


def _details(data):
    details = {}
    for column in data.get("statusColumns", []):
        for detail in column.get("statusDetails", []):
            key = detail.get("key")
            if key:
                details[str(key)] = detail.get("value")
    return details


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _find_ebl_root(data):
    """Pick the EBL directory from the device's ``/paths`` payload."""
    candidates = [
        value.rstrip("/")
        for value in _strings(data)
        if value.startswith("/")
        and "ebl" in value.lower()
        and not value.lower().endswith(".ebl")
    ]
    if not candidates:
        return None
    marked = [value for value in candidates if "ebl_data_logs" in value.lower()]
    return min(marked or candidates, key=len)


class W2KClient:
    """Minimal read-only client for one W2K-1."""

    def __init__(
        self,
        host=DEFAULT_HOST,
        password="",
        user=DEFAULT_USER,
        timeout=30,
        base_path=None,
    ):
        self.host = host
        self.base = base_url(host)
        self.user = user
        self.password = password
        self.timeout = (CONNECT_TIMEOUT, timeout)
        self.base_path = base_path.rstrip("/") if base_path else None
        self._ebl_root = None
        self.session = requests.Session()
        self._token = None

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def login(self):
        try:
            response = self.session.post(
                f"{self.base}/api/login",
                json={"user": self.user, "password": self.password},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise W2KError(f"Cannot reach {self.base}: {exc}") from exc
        if response.status_code in (401, 403):
            raise W2KAuthError("The device rejected the admin password")
        if not response.ok:
            raise W2KRequestError(
                f"Login failed: HTTP {response.status_code} {response.text[:200]}"
            )
        token = (response.json() or {}).get("token")
        if not token:
            raise W2KAuthError("The device did not return a login token")
        self._token = token
        self.session.headers["Authorization"] = f"Bearer {token}"
        return token

    def _get(self, path, params=None, stream=False, retry=True):
        if self._token is None:
            self.login()
        try:
            response = self.session.get(
                self.base + path,
                params=params,
                timeout=self.timeout,
                stream=stream,
            )
        except requests.RequestException as exc:
            raise W2KError(f"Cannot reach {self.base}: {exc}") from exc
        if response.status_code == 401 and retry:
            response.close()
            self.login()
            return self._get(path, params=params, stream=stream, retry=False)
        if not response.ok:
            response.close()
            raise W2KRequestError(f"GET {path} failed: HTTP {response.status_code}")
        return response

    def info(self):
        data = self._get("/api/info").json() or {}
        details = _details(data)
        serial = str(details.get("Serial Number") or "").strip()
        if serial:
            safe_component(serial, "serial number")
        firmware = str(
            details.get("Firmware Version") or details.get("Software Version") or ""
        ).strip()
        return W2KDevice(serial_number=serial, firmware=firmware, raw=data)

    def ebl_root(self):
        """Return the device directory that holds EBL session folders.

        ``/paths`` reports the live filesystem layout. If it is absent or
        unhelpful, fall back to the published W2K-1 location.
        """
        if self.base_path:
            return self.base_path
        if self._ebl_root is None:
            root = None
            try:
                root = _find_ebl_root(self._get("/paths").json() or {})
            except (W2KError, ValueError):
                root = None
            self._ebl_root = (root or EBL_BASE_PATH).rstrip("/")
        return self._ebl_root

    def folders(self):
        data = self._get("/api/data_logs").json() or {}
        folders = []
        for item in data.get("dataFolders", []):
            folders.append(
                W2KFolder(
                    name=safe_component(item.get("name"), "folder name"),
                    size=int(item.get("size") or 0),
                    in_progress=bool(
                        item.get("isInProgress", item.get("isExpanded", False))
                    ),
                    raw=item,
                )
            )
        return folders

    def files(self, folder):
        name = folder.name if isinstance(folder, W2KFolder) else str(folder)
        data = (
            self._get(
                "/api/data_logs",
                params={"method": "fileList", "folder": name},
            ).json()
            or {}
        )
        return [
            W2KFile(
                folder=name,
                name=safe_component(item.get("file_name"), "file name"),
                size=int(item.get("file_size") or 0),
                time=int(item.get("file_time") or 0),
            )
            for item in data.get("dataFiles", [])
        ]

    def download(self, file, destination, progress=None):
        """Stream one EBL file to ``destination``; return bytes written.

        ``file`` is a :class:`W2KFile` (the SD path is built from the configured
        base path) or a string holding the absolute path on the device.
        """
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.part")
        if isinstance(file, str):
            remote = file
        else:
            safe_component(file.folder, "folder name")
            safe_component(file.name, "file name")
            remote = f"{self.ebl_root()}/{file.key}"
        if ".." in Path(remote).parts:
            raise W2KRequestError(f"Unsafe download path: {remote!r}")
        response = self._get(
            "/api/download",
            params={"file_name": remote},
            stream=True,
        )
        total = int(response.headers.get("Content-Length") or 0)
        written = 0
        try:
            with temporary.open("wb") as stream:
                for chunk in response.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    stream.write(chunk)
                    written += len(chunk)
                    if progress is not None:
                        progress(written, total)
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            response.close()
        if progress is not None and not written:
            progress(0, total)
        return written
