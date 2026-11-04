"""WeatherNext credential selection: explicit JSON file or Application Default Credentials."""

from __future__ import annotations

import os
from pathlib import Path

SCOPES = ("https://www.googleapis.com/auth/devstorage.read_only",)
QUOTA_PROJECT_HINT = (
    "Quota project not set. Run: gcloud auth application-default "
    "set-quota-project <PROJECT_ID>, or use a service-account JSON in Settings."
)
API_DISABLED_HINT = (
    "Cloud Storage API is not enabled for this project. Enable it, or set a "
    "quota project: gcloud auth application-default set-quota-project <PROJECT_ID>."
)


def describe_access_error(error) -> str:
    """Return a short actionable message for known Google Cloud access failures."""
    try:
        original = str(error)
    except Exception:
        original = "Unknown error"
    parts = [original]
    for attribute in ("reason", "response"):
        try:
            value = getattr(error, attribute, "")
            parts.append(str(value))
            if attribute == "response":
                parts.append(str(getattr(value, "content", "")))
                parts.append(str(getattr(value, "text", "")))
        except Exception:
            pass
    detail = " ".join(parts).lower()
    if any(
        signal in detail
        for signal in (
            "quota project",
            "user_project_denied",
            "quota_project",
            "requires a quota project",
        )
    ):
        return QUOTA_PROJECT_HINT
    if any(
        signal in detail
        for signal in (
            "service_disabled",
            "api not enabled",
            "has not been enabled",
            "has not been used in project",
        )
    ):
        return API_DISABLED_HINT
    compact = " ".join(original.split())
    return compact if len(compact) <= 300 else compact[:299] + "…"


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
