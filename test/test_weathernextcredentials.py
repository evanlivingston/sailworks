from types import SimpleNamespace

import pytest

from sailworks.core.weathernextcredentials import (
    API_DISABLED_HINT,
    QUOTA_PROJECT_HINT,
    WeatherNextCredentials,
    WeatherNextCredentialsError,
    describe_access_error,
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


def test_gcs_filesystem_passes_the_token(monkeypatch):
    captured = {}

    class _Filesystem:
        def __init__(self, token):
            captured["token"] = token

    monkeypatch.setattr("gcsfs.GCSFileSystem", _Filesystem)
    WeatherNextCredentials("/tmp/key.json").gcs_filesystem()
    assert captured["token"] == "/tmp/key.json"
    WeatherNextCredentials("").gcs_filesystem()
    assert captured["token"] == "google_default"


@pytest.mark.parametrize("message", ["without a quota project", "USER_PROJECT_DENIED"])
def test_describe_quota_error(message):
    assert describe_access_error(UserWarning(message)) == QUOTA_PROJECT_HINT


@pytest.mark.parametrize(
    "message", ["SERVICE_DISABLED", "has not been used in project"]
)
def test_describe_disabled_api(message):
    assert describe_access_error(Exception(message)) == API_DISABLED_HINT


def test_describe_unknown_error_is_compact():
    result = describe_access_error(Exception("long\n" * 100))
    assert "\n" not in result
    assert len(result) <= 300
    assert result.endswith("…")


def test_describe_non_exception_and_broken_attributes():
    assert describe_access_error(None) == "None"

    class Broken:
        def __str__(self):
            raise ValueError("broken")

        @property
        def reason(self):
            raise ValueError("broken")

    assert describe_access_error(Broken()) == "Unknown error"


def test_describe_response_content():
    error = SimpleNamespace(response=SimpleNamespace(content=b"SERVICE_DISABLED"))
    assert describe_access_error(error) == API_DISABLED_HINT
