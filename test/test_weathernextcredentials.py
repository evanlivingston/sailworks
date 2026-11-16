from types import SimpleNamespace

import pytest

from sailworks.core.weathernextcredentials import (
    API_DISABLED_HINT,
    QUOTA_PROJECT_HINT,
    SCOPES,
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


def test_adc_loads_the_gcloud_file_without_shelling_out(monkeypatch, tmp_path):
    adc = tmp_path / "application_default_credentials.json"
    adc.write_text("{}")
    marker = object()
    seen = {}

    def load(filename, scopes=None):
        seen["filename"] = filename
        seen["scopes"] = scopes
        return marker, None

    monkeypatch.setattr(
        "sailworks.core.weathernextcredentials.gcloud_adc_file", lambda: adc
    )
    monkeypatch.setattr("google.auth.load_credentials_from_file", load)

    def no_default(*args, **kwargs):
        raise AssertionError("google.auth.default forks the gcloud CLI")

    monkeypatch.setattr("google.auth.default", no_default)
    assert WeatherNextCredentials("").credentials() is marker
    assert seen["filename"] == str(adc)
    assert seen["scopes"] == SCOPES


def test_adc_without_a_gcloud_file_uses_google_auth_default(monkeypatch, tmp_path):
    marker = object()
    monkeypatch.setattr(
        "sailworks.core.weathernextcredentials.gcloud_adc_file",
        lambda: tmp_path / "missing.json",
    )
    monkeypatch.setattr("google.auth.default", lambda scopes: (marker, "project"))
    assert WeatherNextCredentials("").credentials() is marker


def test_gcs_filesystem_passes_resolved_credentials(monkeypatch, tmp_path):
    captured = {}

    class _Filesystem:
        def __init__(self, token):
            captured["token"] = token

    monkeypatch.setattr("gcsfs.GCSFileSystem", _Filesystem)
    path = tmp_path / "key.json"
    path.write_text("{}")
    marker = object()
    monkeypatch.setattr(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        lambda filename, scopes: marker,
    )
    WeatherNextCredentials(str(path)).gcs_filesystem()
    assert captured["token"] is marker

    adc_marker = object()
    monkeypatch.setattr(WeatherNextCredentials, "credentials", lambda self: adc_marker)
    WeatherNextCredentials("").gcs_filesystem()
    assert captured["token"] is adc_marker


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
    assert result == " ".join(["long"] * 100)


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
