"""Google OAuth and token storage for WeatherNext access.

Desktop clients use Google's installed-app loopback flow. Tokens live in the
OS keychain, never in settings, logs or the repository.
"""

from __future__ import annotations

import json
import os
from typing import Any

SCOPES = (
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/devstorage.read_only",
)
KEYRING_SERVICE = "sailworks-weathernext"
KEYRING_USER = "oauth"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"


class WeatherNextAuthError(RuntimeError):
    """WeatherNext credentials are missing, invalid or not configured."""


class WeatherNextAuth:
    """Own the WeatherNext OAuth client and its keyring-backed token."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        *,
        keyring_service: str = KEYRING_SERVICE,
        keyring_user: str = KEYRING_USER,
    ):
        self.client_id = client_id or os.environ.get("WEATHERNEXT_OAUTH_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get(
            "WEATHERNEXT_OAUTH_CLIENT_SECRET", ""
        )
        self.keyring_service = keyring_service
        self.keyring_user = keyring_user

    def _client_config(self) -> dict[str, Any]:
        if not self.client_id:
            raise WeatherNextAuthError(
                "WeatherNext sign-in is not configured: set "
                "WEATHERNEXT_OAUTH_CLIENT_ID or pass a client_id"
            )
        return {
            "installed": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "auth_uri": AUTH_URI,
                "token_uri": TOKEN_URI,
                "redirect_uris": ["http://127.0.0.1"],
            }
        }

    def _store(self, credentials) -> None:
        import keyring

        keyring.set_password(
            self.keyring_service, self.keyring_user, credentials.to_json()
        )

    def _load(self):
        import keyring
        from google.oauth2.credentials import Credentials

        raw = keyring.get_password(self.keyring_service, self.keyring_user)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise WeatherNextAuthError("Stored WeatherNext token is invalid") from exc
        return Credentials.from_authorized_user_info(data, SCOPES)

    def credentials(self):
        """Return refreshed credentials, or None when nobody is signed in."""
        credentials = self._load()
        if credentials is None:
            return None
        if credentials.expired and credentials.refresh_token:
            from google.auth.transport.requests import Request

            credentials.refresh(Request())
            self._store(credentials)
        return credentials

    def sign_in(self, *, open_browser: bool = True, timeout_seconds: int = 300):
        """Run the installed-app loopback flow and store the refresh token."""
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_config(self._client_config(), SCOPES)
        credentials = flow.run_local_server(
            port=0,
            open_browser=open_browser,
            authorization_prompt_message=(
                "Sign in with the Google account approved for WeatherNext."
            ),
            success_message="WeatherNext sign-in complete. You can close this window.",
            timeout_seconds=timeout_seconds,
        )
        self._store(credentials)
        return credentials

    def sign_out(self) -> None:
        """Revoke the stored token and remove it from the keychain."""
        import keyring
        import requests

        credentials = self._load()
        if credentials is not None:
            token = credentials.refresh_token or credentials.token
            if token:
                try:
                    requests.post(REVOKE_URI, params={"token": token}, timeout=10)
                except requests.RequestException:
                    pass
        try:
            keyring.delete_password(self.keyring_service, self.keyring_user)
        except keyring.errors.PasswordDeleteError:
            pass

    def access_token(self) -> str | None:
        credentials = self.credentials()
        return None if credentials is None else credentials.token

    def account_email(self) -> str | None:
        """Best-effort account label for the sign-in status."""
        credentials = self.credentials()
        if credentials is None or not credentials.token:
            return None
        import requests

        try:
            response = requests.get(
                USERINFO_URI,
                headers={"Authorization": f"Bearer {credentials.token}"},
                timeout=10,
            )
            response.raise_for_status()
            return response.json().get("email")
        except requests.RequestException:
            return None

    def gcs_filesystem(self):
        """A gcsfs client authenticated with the stored credentials."""
        credentials = self.credentials()
        if credentials is None:
            raise WeatherNextAuthError("Sign in with Google to read WeatherNext")
        import gcsfs

        return gcsfs.GCSFileSystem(token=credentials)
