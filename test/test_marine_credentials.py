"""Saved Copernicus account restoration and credential-store failures."""

import json
import tempfile
import unittest
from unittest.mock import Mock

from gweatherrouting.core.marine.credentials import (
    CredentialStoreError,
    SystemCredentialStore,
)
from gweatherrouting.core.marine.manager import MarineManager
from gweatherrouting.core.marine.spatialproviders import CopernicusProvider


class MemoryKeyring:
    priority = 1

    def __init__(self):
        self.items = {}

    def get_password(self, service, account):
        return self.items.get((service, account))

    def set_password(self, service, account, value):
        self.items[service, account] = value

    def delete_password(self, service, account):
        del self.items[service, account]


class TestMarineCredentials(unittest.TestCase):
    def setUp(self):
        self.backend = MemoryKeyring()
        self.store = SystemCredentialStore(self.backend)

    def test_account_restored_in_new_manager_without_secrets_in_data(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = MarineManager(directory, credential_store=self.store)
            provider = manager.coverage_planner.worldwide
            self.assertIsNone(provider.credentials)
            provider.configure(" sailor ", "example-secret")
            restarted = MarineManager(
                directory, credential_store=SystemCredentialStore(self.backend)
            )
            self.assertEqual(
                restarted.coverage_planner.worldwide.credentials,
                ("sailor", "example-secret"),
            )
            self.assertTrue(restarted.coverage_planner.worldwide.credentials_saved)
            from pathlib import Path

            for path in Path(directory).rglob("*"):
                if path.is_file():
                    self.assertNotIn(b"example-secret", path.read_bytes())
            restarted.coverage_planner.worldwide.forget_account()
            self.assertIsNone(provider.credential_store.load())
            self.assertIsNone(restarted.coverage_planner.worldwide.credentials)
            self.assertFalse(restarted.coverage_planner.worldwide.credentials_saved)
            self.store.forget()  # Removing an absent account is harmless.

    def test_save_failure_keeps_previous_account_and_sanitizes_error(self):
        provider = CopernicusProvider(self.store)
        provider.configure("sailor", "old-secret")
        self.backend.set_password = Mock(side_effect=RuntimeError("example-secret"))
        with self.assertRaises(CredentialStoreError) as raised:
            provider.configure("replacement", "example-secret")
        self.assertNotIn("example-secret", str(raised.exception))
        self.assertEqual(provider.credentials, ("sailor", "old-secret"))
        self.assertEqual(self.store.load(), ("sailor", "old-secret"))

    def test_locked_store_does_not_prevent_startup(self):
        self.backend.get_password = Mock(side_effect=RuntimeError("private detail"))
        provider = CopernicusProvider(self.store)
        self.assertIsNone(provider.credentials)
        self.assertFalse(provider.credentials_saved)
        self.assertIn("Could not read", provider.credential_error)
        self.assertNotIn("private detail", provider.credential_error)

    def test_delete_failure_does_not_claim_account_removed(self):
        provider = CopernicusProvider(self.store)
        provider.configure("sailor", "example-secret")
        self.backend.delete_password = Mock(side_effect=RuntimeError())
        with self.assertRaises(CredentialStoreError):
            provider.forget_account()
        self.assertTrue(provider.credentials_saved)
        self.assertEqual(provider.credentials, ("sailor", "example-secret"))

    def test_invalid_saved_accounts_and_unavailable_backend(self):
        for value in (
            "invalid json",
            "{}",
            json.dumps(dict(username=42, password="x")),
            json.dumps(dict(username="sailor", password="")),
        ):
            self.backend.set_password(self.store.SERVICE, self.store.ACCOUNT, value)
            with self.assertRaises(CredentialStoreError):
                self.store.load()
        self.backend.priority = 0
        with self.assertRaises(CredentialStoreError):
            self.store.save("sailor", "secret")

    def test_invalid_input_is_not_saved(self):
        provider = CopernicusProvider(self.store)
        for username, password in ((" ", "secret"), ("sailor", "")):
            with self.assertRaises(ValueError):
                provider.configure(username, password)
        self.assertEqual(self.backend.items, {})
