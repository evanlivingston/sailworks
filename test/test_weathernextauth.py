import json

import pytest

from sailworks.core import weathernextauth
from sailworks.core.weathernextauth import WeatherNextAuth, WeatherNextAuthError


class _Credentials:
    token = "access-token"
    refresh_token = "refresh-token"
    expired = False

    def to_json(self):
        return json.dumps(
            {
                "token": self.token,
                "refresh_token": self.refresh_token,
                "token_uri": weathernextauth.TOKEN_URI,
                "client_id": "client",
                "client_secret": "",
                "scopes": list(weathernextauth.SCOPES),
            }
        )


class _Flow:
    def __init__(self):
        self.calls = []

    def run_local_server(self, **kwargs):
        self.calls.append(kwargs)
        return _Credentials()


def test_sign_in_requires_a_configured_client_id(monkeypatch):
    monkeypatch.delenv("WEATHERNEXT_OAUTH_CLIENT_ID", raising=False)
    with pytest.raises(WeatherNextAuthError, match="not configured"):
        WeatherNextAuth().sign_in()


def test_sign_in_uses_loopback_flow_and_stores_the_token(monkeypatch):
    flow = _Flow()
    stored = {}
    monkeypatch.setattr(
        "google_auth_oauthlib.flow.InstalledAppFlow.from_client_config",
        lambda config, scopes: flow,
    )
    monkeypatch.setattr(
        "keyring.set_password",
        lambda service, user, value: stored.update(
            service=service, user=user, value=value
        ),
    )
    auth = WeatherNextAuth(client_id="client-id")
    credentials = auth.sign_in(open_browser=False, timeout_seconds=30)
    assert isinstance(credentials, _Credentials)
    assert flow.calls[0]["port"] == 0
    assert flow.calls[0]["open_browser"] is False
    assert stored["service"] == weathernextauth.KEYRING_SERVICE
    assert json.loads(stored["value"])["refresh_token"] == "refresh-token"


def test_credentials_loads_the_keyring_token(monkeypatch):
    payload = _Credentials().to_json()
    monkeypatch.setattr("keyring.get_password", lambda service, user: payload)
    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials.from_authorized_user_info",
        lambda data, scopes: _Credentials(),
    )
    credentials = WeatherNextAuth(client_id="client-id").credentials()
    assert credentials.token == "access-token"
    assert credentials.refresh_token == "refresh-token"


def test_no_token_means_not_signed_in(monkeypatch):
    monkeypatch.setattr("keyring.get_password", lambda service, user: None)
    assert WeatherNextAuth(client_id="client-id").credentials() is None


def test_sign_out_revokes_and_deletes_the_token(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "keyring.get_password", lambda service, user: _Credentials().to_json()
    )
    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials.from_authorized_user_info",
        lambda data, scopes: _Credentials(),
    )
    monkeypatch.setattr(
        "requests.post",
        lambda url, **kwargs: calls.append(("post", url, kwargs)),
    )
    monkeypatch.setattr(
        "keyring.delete_password",
        lambda service, user: calls.append(("delete", service, user)),
    )
    WeatherNextAuth(client_id="client-id").sign_out()
    assert calls[0][0] == "post"
    assert calls[0][1] == weathernextauth.REVOKE_URI
    assert calls[-1] == (
        "delete",
        weathernextauth.KEYRING_SERVICE,
        weathernextauth.KEYRING_USER,
    )


def test_gcs_filesystem_requires_sign_in(monkeypatch):
    monkeypatch.setattr("keyring.get_password", lambda service, user: None)
    with pytest.raises(WeatherNextAuthError, match="Sign in"):
        WeatherNextAuth(client_id="client-id").gcs_filesystem()


def test_account_email_uses_the_access_token(monkeypatch):
    monkeypatch.setattr(
        "keyring.get_password", lambda service, user: _Credentials().to_json()
    )
    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials.from_authorized_user_info",
        lambda data, scopes: _Credentials(),
    )

    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"email": "sailor@example.com"}

    monkeypatch.setattr("requests.get", lambda url, **kwargs: _Response())
    assert (
        WeatherNextAuth(client_id="client-id").account_email() == "sailor@example.com"
    )
