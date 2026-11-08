"""Tests for the W2K-1 REST client against a local fake device."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from sailworks.core.w2kapi import (
    W2KAuthError,
    W2KClient,
    W2KFile,
    W2KRequestError,
    base_url,
    safe_component,
)


class FakeW2K(BaseHTTPRequestHandler):
    server_version = "FakeW2K/1.0"

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        expected = f"Bearer {self.server.token}"
        if self.headers.get("Authorization") != expected:
            self._json(401, {"error": "unauthorized"})
            return False
        return True

    def do_POST(self):
        if self.path != "/api/login":
            return self._json(404, {})
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if body.get("user") != "admin" or body.get("password") != "secret":
            return self._json(401, {"error": "bad credentials"})
        self.server.logins += 1
        self.server.token = f"token-{self.server.logins}"
        return self._json(200, {"token": self.server.token})

    def do_GET(self):
        if not self._authorized():
            return
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/paths":
            if not self.server.offer_paths:
                return self._json(404, {})
            return self._json(
                200, {"paths": [{"name": "EBL logs", "path": "/mnt/sd/ebl"}]}
            )
        if parsed.path == "/api/info":
            return self._json(
                200,
                {
                    "statusColumns": [
                        {
                            "name": "Device",
                            "statusDetails": [
                                {"key": "Serial Number", "value": "W2K-12345"},
                                {"key": "Firmware Version", "value": "1.398"},
                            ],
                        }
                    ]
                },
            )
        if parsed.path == "/api/data_logs":
            if query.get("method") == ["fileList"]:
                return self._json(
                    200,
                    {
                        "dataFiles": [
                            {
                                "file_name": "000000_080.ebl",
                                "file_size": 120,
                                "file_time": 1700000000,
                            }
                        ]
                    },
                )
            return self._json(
                200,
                {
                    "dataFolders": [
                        {"name": "EBL000000", "size": 120, "isInProgress": False}
                    ]
                },
            )
        if parsed.path == "/api/download":
            remote = (query.get("file_name") or [""])[0]
            self.server.downloads.append(remote)
            if not remote.endswith("/EBL000000/000000_080.ebl"):
                return self._json(404, {})
            body = b"EBL" * 40
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return self._json(404, {})


class W2KClientTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeW2K)
        self.server.logins = 0
        self.server.token = ""
        self.server.offer_paths = True
        self.server.downloads = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host = f"127.0.0.1:{self.server.server_address[1]}"
        self.client = W2KClient(host, password="secret")

    def tearDown(self):
        self.client.close()
        self.server.shutdown()
        self.server.server_close()

    def test_base_url_accepts_addresses_and_urls(self):
        self.assertEqual(base_url("192.168.4.1"), "http://192.168.4.1")
        self.assertEqual(base_url(" http://w2k/ "), "http://w2k")
        with self.assertRaises(ValueError):
            base_url("")

    def test_ebl_root_chooses_base_from_paths_payload(self):
        from sailworks.core.w2kapi import _find_ebl_root

        payload = {
            "paths": [
                "/sdcard/W2K-1/ebl_data_logs",
                "/sdcard/W2K-1/ebl_data_logs/EBL000000",
                "/sdcard/W2K-1/ebl_data_logs/EBL000000/000000_080.ebl",
            ]
        }
        self.assertEqual(_find_ebl_root(payload), "/sdcard/W2K-1/ebl_data_logs")
        self.assertIsNone(_find_ebl_root({"paths": ["/tmp/other"]}))

    def test_safe_component_rejects_names_that_escape_staging(self):
        self.assertEqual(safe_component("EBL000000"), "EBL000000")
        for value in ("", ".", "..", "../x", "/absolute", "a\\b", "a\x00b"):
            with self.assertRaises(W2KRequestError):
                safe_component(value)

    def test_info_and_login(self):
        device = self.client.info()
        self.assertEqual(device.serial_number, "W2K-12345")
        self.assertEqual(device.firmware, "1.398")
        self.assertEqual(self.server.logins, 1)

    def test_folders_and_files(self):
        folders = self.client.folders()
        self.assertEqual([folder.name for folder in folders], ["EBL000000"])
        self.assertFalse(folders[0].in_progress)

        files = self.client.files(folders[0])
        self.assertEqual(files[0].key, "EBL000000/000000_080.ebl")
        self.assertEqual(files[0].size, 120)

    def test_download_writes_file_and_reports_progress(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "EBL000000" / "000000_080.ebl"
            seen = []
            written = self.client.download(
                W2KFile(
                    folder="EBL000000",
                    name="000000_080.ebl",
                    size=120,
                    time=1700000000,
                ),
                target,
                progress=lambda done, total: seen.append((done, total)),
            )
            self.assertEqual(written, 120)
            self.assertEqual(target.read_bytes(), b"EBL" * 40)
            self.assertEqual(seen[-1], (120, 120))
            self.assertEqual(
                self.server.downloads[-1],
                "/mnt/sd/ebl/EBL000000/000000_080.ebl",
            )
            self.assertEqual(list(target.parent.glob(".*.part")), [])

    def test_download_falls_back_to_published_path_without_paths(self):
        import tempfile
        from pathlib import Path

        self.server.offer_paths = False
        client = W2KClient(
            f"127.0.0.1:{self.server.server_address[1]}", password="secret"
        )
        with tempfile.TemporaryDirectory() as directory:
            client.download(
                W2KFile(
                    folder="EBL000000",
                    name="000000_080.ebl",
                    size=120,
                    time=1700000000,
                ),
                Path(directory) / "log.ebl",
            )
        client.close()
        self.assertEqual(
            self.server.downloads[-1],
            "/sdcard/W2K-1/ebl_data_logs/EBL000000/000000_080.ebl",
        )

    def test_bad_password(self):
        client = W2KClient(
            f"127.0.0.1:{self.server.server_address[1]}", password="nope"
        )
        with self.assertRaises(W2KAuthError):
            client.info()
        client.close()

    def test_relogin_once_when_token_expires(self):
        self.client.info()
        self.assertEqual(self.server.logins, 1)
        self.server.token = "expired"
        device = self.client.info()
        self.assertEqual(device.serial_number, "W2K-12345")
        self.assertEqual(self.server.logins, 2)

    def test_unknown_route_raises_request_error(self):
        with self.assertRaises(W2KRequestError):
            self.client._get("/api/missing")


if __name__ == "__main__":
    unittest.main()
