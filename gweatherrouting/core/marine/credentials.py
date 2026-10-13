"""Copernicus account storage in the operating system credential store."""

import json


class CredentialStoreError(ValueError):
    pass


class SystemCredentialStore:
    SERVICE = "org.gweatherrouting.CopernicusMarine"
    ACCOUNT = "worldwide-currents"

    def __init__(self, backend=None):
        self._backend = backend

    def _keyring(self):
        if self._backend is None:
            import keyring

            self._backend = keyring.get_keyring()
        if self._backend.priority <= 0:
            raise CredentialStoreError("No system credential store is available.")
        return self._backend

    def load(self):
        try:
            value = self._keyring().get_password(self.SERVICE, self.ACCOUNT)
            if value is None:
                return None
            account = json.loads(value)
            username, password = account["username"], account["password"]
            if not isinstance(username, str) or not isinstance(password, str):
                raise ValueError()
            if not username.strip() or not password:
                raise ValueError()
            return username.strip(), password
        except Exception:
            raise CredentialStoreError(
                "Could not read the saved account. Unlock your system credential "
                "store or save the account again."
            ) from None

    def save(self, username, password):
        try:
            self._keyring().set_password(
                self.SERVICE,
                self.ACCOUNT,
                json.dumps(dict(username=username, password=password)),
            )
        except Exception:
            raise CredentialStoreError(
                "Account was not saved. Unlock your system credential store and retry."
            ) from None

    def forget(self):
        try:
            backend = self._keyring()
            if backend.get_password(self.SERVICE, self.ACCOUNT) is not None:
                backend.delete_password(self.SERVICE, self.ACCOUNT)
        except Exception:
            raise CredentialStoreError(
                "Could not remove the saved account. Unlock your system credential "
                "store and retry."
            ) from None
