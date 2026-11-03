"""WeatherNext credential selection: explicit JSON file or Application Default Credentials."""

from __future__ import annotations

import os
from pathlib import Path

SCOPES = ("https://www.googleapis.com/auth/devstorage.read_only",)


class WeatherNextCredentialsError(RuntimeError):
    """WeatherNext credentials are missing or invalid."""


class WeatherNextCredentials:
    """Resolve the credentials used by WeatherNext Zarr access."""

    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")

    @property
    def using_adc(self) -> bool:
        return not bool(self.path)

    @property
    def token(self) -> str:
        """The gcsfs token/path; safe to serialize into zarr storage options."""
        return self.path or "google_default"

    @property
    def description(self) -> str:
        if self.using_adc:
            return "Application Default Credentials"
        return f"Credentials file: {self.path}"

    def credentials(self):
        """Return google-auth credentials for the configured source."""
        if self.path:
            file = Path(self.path).expanduser()
            if not file.is_file():
                raise WeatherNextCredentialsError(
                    f"WeatherNext credentials file does not exist: {file}"
                )
            from google.oauth2.service_account import Credentials

            return Credentials.from_service_account_file(str(file), scopes=SCOPES)
        try:
            import google.auth
        except ImportError as exc:
            raise WeatherNextCredentialsError(
                "WeatherNext needs gcsfs and google-auth; install the weathernext extra"
            ) from exc
        try:
            credentials, _project = google.auth.default(scopes=SCOPES)
        except Exception as exc:
            raise WeatherNextCredentialsError(
                "WeatherNext credentials are not configured: choose a service-account "
                "JSON or set GOOGLE_APPLICATION_CREDENTIALS"
            ) from exc
        return credentials

    def gcs_filesystem(self):
        """A gcsfs client for the configured credentials."""
        import gcsfs

        return gcsfs.GCSFileSystem(token=self.token)
