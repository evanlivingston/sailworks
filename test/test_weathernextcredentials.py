from types import SimpleNamespace

import pytest

from sailworks.core.weathernextcredentials import (
    WeatherNextCredentials,
    WeatherNextCredentialsError,
)


def test_default_is_application_default_credentials():
    credentials = WeatherNextCredentials("")
    assert credentials.using_adc is True
    assert credentials.description == "Application Default Credentials"


def test_missing_service_account_file_fails_closed(tmp_path):
    credentials = WeatherNextCredentials(str(tmp_path / "missing.json"))
    with pytest.raises(WeatherNextCredentialsError, match="does not exist"):
        credentials.credentials()


def test_service_account_file_uses_scoped_credentials(monkeypatch, tmp_path):
    path = tmp_path / "key.json"
    path.write_text("{}")
    marker = object()
    monkeypatch.setattr(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        lambda filename, scopes: marker,
    )
    assert WeatherNextCredentials(str(path)).credentials() is marker


def test_adc_uses_google_auth_default(monkeypatch):
    marker = object()
    monkeypatch.setattr("google.auth.default", lambda scopes: (marker, "project"))
    assert WeatherNextCredentials("").credentials() is marker


def test_gcs_filesystem_passes_credentials(monkeypatch):
    marker = object()
    captured = {}

    class _Filesystem:
        def __init__(self, token):
            captured["token"] = token

    monkeypatch.setattr(
        "sailworks.core.weathernextcredentials.WeatherNextCredentials.credentials",
        lambda self: marker,
    )
    monkeypatch.setattr("gcsfs.GCSFileSystem", _Filesystem)
    WeatherNextCredentials("").gcs_filesystem()
    assert captured["token"] is marker
