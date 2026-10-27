from __future__ import annotations

import configparser
import json
import os
import socket
import ssl
import stat
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from core import secret_store
from kakarot_overlay import config as overlay_config
from kakarot_overlay.mascot import (
    _avatar_path,
    _configure_application,
    _mascot_window_flags,
)


class _Response:
    def __init__(self, data: bytes):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self.data


class GeminiKeyCheckTests(unittest.TestCase):
    def test_request_uses_header_and_does_not_put_key_in_url(self):
        key = "test-key-should-not-appear-in-url"
        response = _Response(json.dumps({"models": [{"name": "models/test"}]}).encode())
        with patch.object(overlay_config.urllib.request, "urlopen", return_value=response) as open_url:
            result = overlay_config.test_api_key(key)

        request = open_url.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("X-goog-api-key"), key)
        self.assertNotIn(key, request.full_url)
        self.assertEqual(open_url.call_args.kwargs["timeout"], 8)
        self.assertIn("Key verified", result)

    def test_http_errors_are_specific_and_do_not_echo_key(self):
        expected = {
            400: "HTTP 400",
            401: "401 Unauthorized",
            403: "403 Forbidden",
            429: "429",
            500: "HTTP 500",
        }
        for status, fragment in expected.items():
            with self.subTest(status=status):
                failure = urllib.error.HTTPError("https://example.invalid", status, "reason", {}, None)
                with patch.object(overlay_config.urllib.request, "urlopen", side_effect=failure):
                    try:
                        with self.assertRaisesRegex(RuntimeError, fragment) as raised:
                            overlay_config.test_api_key("secret-test-key")
                    finally:
                        failure.close()
                self.assertNotIn("secret-test-key", str(raised.exception))

    def test_dns_timeout_tls_and_generic_network_failures(self):
        cases = (
            (urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure")), "DNS lookup failed"),
            (urllib.error.URLError(TimeoutError("timeout")), "timed out"),
            (urllib.error.URLError(ssl.SSLError("certificate verify failed")), "TLS connection"),
            (urllib.error.URLError(OSError("network unreachable")), "Could not connect"),
        )
        for error, fragment in cases:
            with self.subTest(fragment=fragment):
                with patch.object(overlay_config.urllib.request, "urlopen", side_effect=error):
                    with self.assertRaisesRegex(RuntimeError, fragment):
                        overlay_config.test_api_key("secret-test-key")

    def test_malformed_and_empty_responses_are_reported(self):
        for body, fragment in ((b"not-json", "invalid JSON"), (b'{"models": []}', "no models")):
            with self.subTest(fragment=fragment):
                with patch.object(overlay_config.urllib.request, "urlopen", return_value=_Response(body)):
                    with self.assertRaisesRegex(RuntimeError, fragment):
                        overlay_config.test_api_key("test-key")

    def test_blank_key_fails_before_network(self):
        with patch.object(overlay_config.urllib.request, "urlopen") as open_url:
            with self.assertRaisesRegex(ValueError, "Enter a Gemini API key"):
                overlay_config.test_api_key("  ")
        open_url.assert_not_called()


class CredentialFallbackTests(unittest.TestCase):
    def test_encrypted_fallback_round_trip_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_home = Path(temporary) / "config"
            with patch.dict(os.environ, {"KAKAROT_CONFIG_HOME": str(config_home)}), patch.object(
                secret_store, "_keyring_module", return_value=None
            ):
                backend = secret_store.set_secret("gemini_api_key", "test-secret-value")
                self.assertIn("encrypted local fallback", backend)
                self.assertEqual(secret_store.get_secret("gemini_api_key"), "test-secret-value")

            encrypted = config_home / "secrets.enc"
            key = config_home / "secrets.key"
            self.assertNotIn(b"test-secret-value", encrypted.read_bytes())
            self.assertEqual(stat.S_IMODE(encrypted.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(config_home.stat().st_mode), 0o700)

    def test_keyring_failure_falls_back_without_losing_secret(self):
        class BrokenKeyring:
            @staticmethod
            def set_password(*_args):
                raise RuntimeError("keyring is locked")

            @staticmethod
            def get_password(*_args):
                raise RuntimeError("keyring is locked")

        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"KAKAROT_CONFIG_HOME": temporary}), patch.object(
                secret_store, "_keyring_module", return_value=BrokenKeyring
            ):
                self.assertIn("encrypted local fallback", secret_store.set_secret("gemini_api_key", "key"))
                self.assertEqual(secret_store.get_secret("gemini_api_key"), "key")

    def test_newer_encrypted_fallback_wins_over_stale_keyring_value(self):
        class ReadOnlyKeyring:
            @staticmethod
            def set_password(*_args):
                raise RuntimeError("keyring collection is read-only")

            @staticmethod
            def get_password(service, _name):
                return "stale-key" if service == secret_store.SERVICE_NAME else None

        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"KAKAROT_CONFIG_HOME": temporary}), patch.object(
                secret_store, "_keyring_module", return_value=ReadOnlyKeyring
            ):
                secret_store.set_secret("gemini_api_key", "new-key")
                self.assertEqual(secret_store.get_secret("gemini_api_key"), "new-key")


class MascotIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])
        _configure_application(cls.app)

    def test_qt_identity_and_avatar_icon(self):
        self.assertEqual(self.app.applicationName(), "Kakarot")
        self.assertEqual(self.app.applicationDisplayName(), "Kakarot")
        self.assertEqual(self.app.desktopFileName(), "kakarot-mascot")
        self.assertIsNotNone(_avatar_path())
        self.assertFalse(self.app.windowIcon().isNull())

    def test_floating_flags_are_exact(self):
        flags = _mascot_window_flags()
        self.assertTrue(flags & Qt.WindowType.FramelessWindowHint)
        self.assertTrue(flags & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(flags & Qt.WindowType.Tool)
        self.assertFalse(flags & Qt.WindowType.BypassWindowManagerHint)

    def test_installed_desktop_identity_matches_qt_and_requests_xcb(self):
        desktop_path = Path(__file__).resolve().parents[1] / "kakarot-mascot.desktop"
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(desktop_path)
        entry = parser["Desktop Entry"]
        self.assertEqual(entry["Name"], "Kakarot")
        self.assertEqual(entry["StartupWMClass"], "Kakarot")
        self.assertEqual(entry["Icon"], "io.github.kakarot.Daemon")
        self.assertIn("QT_QPA_PLATFORM=xcb", entry["Exec"])
        self.assertIn("PYTHONPATH=@PREFIX@", entry["Exec"])


if __name__ == "__main__":
    unittest.main()
