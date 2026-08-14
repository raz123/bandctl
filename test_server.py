"""Unit tests for the Band Controller HTTP server (v2.2 wave).

Covers the LAN-access settings feature (bind + bearer token), the auth
gate (token required for remote clients in LAN mode and for state-
changing loopback actions when a token is configured; read-only
diagnostics exempt), band validation (M8), guarded parsing (M6),
atomic config writes (M7), and the static-serving contract (SRV-07).

Follows the diag test conventions (unittest.TestCase classes, runnable
under pytest).

The tests never touch the modem: band validation and boot-apply paths
are exercised with stubbed _apply_bands, and requests run against a
socket-free BandHandler built with object.__new__.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))  # module/web

import server  # noqa: E402

UNAUTHORIZED = {"ok": False, "error": "unauthorized"}


class FakeHeaders(dict):
    """Stand-in for http.client.HTTPMessage (dict.get is enough)."""


def make_handler(path='/api/health', command='GET', headers=None,
                 addr=('127.0.0.1', 54321), body=b''):
    """Build a BandHandler without a socket.

    object.__new__ skips __init__; the attributes BaseHTTPRequestHandler
    touches when sending a response are set manually."""
    h = object.__new__(server.BandHandler)
    h.path = path
    h.command = command
    h.client_address = addr
    h.headers = FakeHeaders(headers or {})
    h.rfile = BytesIO(body)
    h.wfile = BytesIO()
    h.request_version = 'HTTP/1.0'
    h._headers_buffer = []
    h.requestline = '{} {} HTTP/1.0'.format(command, path)
    return h


def run_api(path='/api/health', command='GET', headers=None,
            addr=('127.0.0.1', 54321), body=b''):
    """Run handle_api on a fake handler; return (status, parsed_json)."""
    h = make_handler(path=path, command=command, headers=headers,
                     addr=addr, body=body)
    h.handle_api()
    raw = h.wfile.getvalue()
    status = int(raw.split(b'\r\n', 1)[0].split()[1])
    payload = raw.split(b'\r\n\r\n', 1)[1]
    return status, json.loads(payload.decode())


class SettingsFileCase(unittest.TestCase):
    """Base: point SETTINGS_FILE/CONFIG_FILE/EXPORT_DIR at a temp dir and
    reset the snapshot. (server.py resolves MODDIR to the repo's grand-
    parent when the file lives at the repo root, so the default config
    paths point outside the checkout — every test that may persist
    anything must be redirected here.)"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_file = server.SETTINGS_FILE
        self._orig_config = server.CONFIG_FILE
        self._orig_export = server.EXPORT_DIR
        self._orig_settings = server.SETTINGS
        server.SETTINGS_FILE = Path(self._tmp.name) / "settings.json"
        server.CONFIG_FILE = Path(self._tmp.name) / "bands.json"
        server.EXPORT_DIR = Path(self._tmp.name)
        server.SETTINGS = {"bind": "127.0.0.1", "token": None}

    def tearDown(self):
        server.SETTINGS_FILE = self._orig_file
        server.CONFIG_FILE = self._orig_config
        server.EXPORT_DIR = self._orig_export
        server.SETTINGS = self._orig_settings
        self._tmp.cleanup()


class TestSettingsLoad(SettingsFileCase):
    """config/settings.json loading: defaults on absent/invalid input."""

    def test_missing_file_defaults(self):
        self.assertEqual(server._load_settings(),
                         {"bind": "127.0.0.1", "token": None,
                          "drop_log": False, "band_camping": True})

    def test_malformed_json_defaults(self):
        server.SETTINGS_FILE.write_text("{not json")
        self.assertEqual(server._load_settings(),
                         {"bind": "127.0.0.1", "token": None,
                          "drop_log": False, "band_camping": True})

    def test_invalid_bind_falls_back_to_localhost(self):
        server.SETTINGS_FILE.write_text(
            json.dumps({"bind": "9.9.9.9", "token": "tok"}))
        self.assertEqual(server._load_settings(),
                         {"bind": "127.0.0.1", "token": "tok",
                          "drop_log": False, "band_camping": True})

    def test_invalid_token_downgrades_lan_bind(self):
        """A-105/A-218: a persisted LAN bind without a usable token is the
        remote-lockout state (every non-loopback client 401s and the
        recovery POST is itself gated). Loading it must downgrade the bind
        to loopback-only instead of accepting the lockout."""
        server.SETTINGS_FILE.write_text(
            json.dumps({"bind": "0.0.0.0", "token": 12345}))
        self.assertEqual(server._load_settings(),
                         {"bind": "127.0.0.1", "token": None,
                          "drop_log": False, "band_camping": True})

    def test_lan_bind_without_token_field_downgrades(self):
        server.SETTINGS_FILE.write_text(
            json.dumps({"bind": "0.0.0.0"}))
        self.assertEqual(server._load_settings(),
                         {"bind": "127.0.0.1", "token": None,
                          "drop_log": False, "band_camping": True})

    def test_lan_bind_with_valid_token_kept(self):
        server.SETTINGS_FILE.write_text(
            json.dumps({"bind": "0.0.0.0", "token": "ok-token"}))
        self.assertEqual(server._load_settings(),
                         {"bind": "0.0.0.0", "token": "ok-token",
                          "drop_log": False, "band_camping": True})

    def test_load_persisted_values(self):
        server.SETTINGS_FILE.write_text(
            json.dumps({"bind": "0.0.0.0", "token": "tok"}))
        self.assertEqual(server._load_settings(),
                         {"bind": "0.0.0.0", "token": "tok",
                          "drop_log": False, "band_camping": True})

    def test_load_persisted_drop_log(self):
        server.SETTINGS_FILE.write_text(
            json.dumps({"bind": "127.0.0.1", "drop_log": True}))
        loaded = server._load_settings()
        self.assertTrue(loaded["drop_log"])
        self.assertEqual(loaded["bind"], "127.0.0.1")


class TestSettingsSave(SettingsFileCase):
    """Atomic settings persistence (M7)."""

    def test_save_persists_snapshot(self):
        server.SETTINGS["bind"] = "0.0.0.0"
        server.SETTINGS["token"] = "tok-1"
        server._save_settings()
        self.assertEqual(
            json.loads(server.SETTINGS_FILE.read_text()),
            {"bind": "0.0.0.0", "token": "tok-1"})

    def test_atomic_failure_keeps_old_content(self):
        server.SETTINGS["token"] = "old-token"
        server._save_settings()  # baseline on disk
        with mock.patch("json.dump", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                server._save_settings()
        # Old content intact, no temp litter left behind.
        self.assertEqual(
            json.loads(server.SETTINGS_FILE.read_text()),
            {"bind": "127.0.0.1", "token": "old-token"})
        leftovers = [p.name for p in Path(self._tmp.name).iterdir()
                     if p.name != "settings.json"]
        self.assertEqual(leftovers, [])

    def test_update_settings_save_failure_reports_error(self):
        with mock.patch("json.dump", side_effect=RuntimeError("disk full")):
            res = server.BandHandler.update_settings(
                None, {"lan_enabled": True})
        self.assertFalse(res["ok"])
        self.assertIn("settings save failed", res["error"])

    def test_failed_save_does_not_mutate_live_settings(self):
        """A-153/A-017: a failed save must not rotate the token or flip the
        bind in the live snapshot — clients keep their credential and the
        server keeps its current exposure."""
        server.SETTINGS = {"bind": "127.0.0.1", "token": "old-token",
                           "drop_log": False}
        server._save_settings()  # baseline on disk
        with mock.patch("json.dump", side_effect=RuntimeError("disk full")):
            res = server.BandHandler.update_settings(
                None, {"lan_enabled": True, "regenerate": True})
        self.assertFalse(res["ok"])
        self.assertIn("settings save failed", res["error"])
        # The old token is still the live credential (not revoked), and the
        # bind did not flip to 0.0.0.0 in memory.
        self.assertEqual(server.SETTINGS["token"], "old-token")
        self.assertEqual(server.SETTINGS["bind"], "127.0.0.1")
        self.assertEqual(
            json.loads(server.SETTINGS_FILE.read_text())["token"],
            "old-token")

    def test_settings_save_failure_returns_http_500(self):
        """A-017: a failed persistence is distinguishable by HTTP status —
        status-checking clients see the failure instead of rendering the
        error body as success."""
        with mock.patch("json.dump", side_effect=RuntimeError("disk full")):
            body = json.dumps({"lan_enabled": True}).encode()
            status, res = run_api('/api/settings?action=settings', 'POST',
                                  headers=FakeHeaders(
                                      {'Content-Length': str(len(body))}),
                                  body=body)
        self.assertEqual(status, 500)
        self.assertFalse(res["ok"])

    def test_settings_validation_error_returns_400(self):
        body = json.dumps({"lan_enabled": "yes"}).encode()
        status, res = run_api('/api/settings?action=settings', 'POST',
                              headers=FakeHeaders(
                                  {'Content-Length': str(len(body))}),
                              body=body)
        self.assertEqual(status, 400)
        self.assertFalse(res["ok"])


class TestUpdateSettings(SettingsFileCase):
    """POST /api/settings semantics: lan_enabled + token lifecycle."""

    def test_enable_creates_token_and_returns_it_once(self):
        first = server.BandHandler.update_settings(
            None, {"lan_enabled": True})
        self.assertTrue(first["ok"])
        self.assertTrue(first["lan_enabled"])
        self.assertTrue(first["token_required"])
        self.assertIsInstance(first["token"], str)
        self.assertEqual(len(first["token"]), 32)  # token_urlsafe(24)

        second = server.BandHandler.update_settings(
            None, {"lan_enabled": True})
        self.assertTrue(second["ok"])
        self.assertTrue(second["lan_enabled"])
        self.assertTrue(second["token_required"])
        # Not created in this call -> not returned.
        self.assertIsNone(second["token"])
        self.assertEqual(second["token_required"], True)

    def test_regenerate_rotates_and_returns_new_token(self):
        server.SETTINGS["token"] = "old-token"
        res = server.BandHandler.update_settings(
            None, {"lan_enabled": True, "regenerate": True})
        self.assertTrue(res["ok"])
        self.assertIsInstance(res["token"], str)
        self.assertNotEqual(res["token"], "old-token")
        self.assertEqual(res["token"], server.SETTINGS["token"])
        self.assertEqual(
            json.loads(server.SETTINGS_FILE.read_text())["token"],
            res["token"])

    def test_disable_keeps_token_but_closes_lan(self):
        server.SETTINGS = {"bind": "0.0.0.0", "token": "keep-me"}
        res = server.BandHandler.update_settings(
            None, {"lan_enabled": False})
        self.assertTrue(res["ok"])
        self.assertFalse(res["lan_enabled"])
        self.assertTrue(res["token_required"])
        self.assertIsNone(res["token"])
        self.assertEqual(server.SETTINGS["bind"], "127.0.0.1")
        self.assertEqual(
            json.loads(server.SETTINGS_FILE.read_text())["bind"],
            "127.0.0.1")

    def test_non_bool_lan_enabled_rejected(self):
        for bad in ("yes", 1, None):
            res = server.BandHandler.update_settings(
                None, {"lan_enabled": bad})
            self.assertFalse(res["ok"], bad)
            self.assertIn("lan_enabled must be a bool", res["error"])

    def test_non_dict_body_rejected(self):
        res = server.BandHandler.update_settings(None, ["lan_enabled"])
        self.assertFalse(res["ok"])

    def test_get_settings_end_to_end(self):
        status, res = run_api('/api/settings?action=settings', 'GET')
        self.assertEqual(status, 200)
        self.assertEqual(res, {"ok": True, "lan_enabled": False,
                               "token_required": False})

    def test_post_settings_end_to_end(self):
        body = json.dumps({"lan_enabled": True}).encode()
        status, res = run_api(
            '/api/settings?action=settings', 'POST',
            headers=FakeHeaders({'Content-Length': str(len(body))}),
            body=body)
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])
        self.assertTrue(res["lan_enabled"])
        self.assertIsInstance(res["token"], str)
        self.assertEqual(
            json.loads(server.SETTINGS_FILE.read_text())["bind"],
            "0.0.0.0")


class TestAuth(SettingsFileCase):
    """LAN auth gate (A-026/A-049/A-158/A-178/A-189): token required for
    remote clients in LAN mode AND for state-changing loopback actions when
    a token is configured; read-only diagnostics stay exempt. Exercised
    end-to-end through handle_api, not a reimplementation of the gate."""

    def setUp(self):
        super().setUp()
        self.lan = {"bind": "0.0.0.0", "token": "sekret-token"}

    def test_loopback_read_only_exempt_without_token(self):
        """A-189: loopback read-only diagnostics stay exempt — a fresh
        phone UI can poll health with no credential."""
        server.SETTINGS = self.lan
        with mock.patch.object(
                server.BandHandler, 'modem_health',
                return_value={"status": "ok", "transport": "qmi",
                              "lte_bands": 1, "nr_bands": 0,
                              "md_session_owner": None}):
            status, _ = run_api('/api/health?action=health', 'GET',
                                addr=('127.0.0.1', 54321))
        self.assertEqual(status, 200)

    def test_ipv6_loopback_read_only_exempt(self):
        server.SETTINGS = self.lan
        with mock.patch.object(
                server.BandHandler, 'modem_health',
                return_value={"status": "ok", "transport": "qmi",
                              "lte_bands": 1, "nr_bands": 0,
                              "md_session_owner": None}):
            status, _ = run_api('/api/health?action=health', 'GET',
                                addr=('::1', 54321))
        self.assertEqual(status, 200)

    def test_loopback_state_change_requires_token(self):
        """A-178/A-189: with a token configured, an unprivileged loopback
        client cannot drive the modem without it."""
        server.SETTINGS = self.lan
        body = json.dumps({"lte": [1], "nr": []}).encode()
        status, res = run_api('/api/write?action=write', 'POST',
                              headers=FakeHeaders(
                                  {'Content-Length': str(len(body))}),
                              addr=('127.0.0.1', 54321), body=body)
        self.assertEqual(status, 401)
        self.assertEqual(res, UNAUTHORIZED)

    def test_loopback_state_change_with_token_ok(self):
        server.SETTINGS = self.lan
        body = json.dumps({"lte": [1], "nr": []}).encode()
        with mock.patch.object(server.BandHandler, "_apply_bands",
                               return_value=(True, "qmi", None)):
            status, res = run_api('/api/write?action=write', 'POST',
                                  headers=FakeHeaders(
                                      {'Content-Length': str(len(body)),
                                       'Authorization':
                                       'Bearer sekret-token'}),
                                  addr=('127.0.0.1', 54321), body=body)
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])

    def test_lan_no_token_unauthorized(self):
        server.SETTINGS = self.lan
        status, _ = run_api('/api/health?action=health', 'GET',
                            addr=('192.168.1.42', 54321))
        self.assertEqual(status, 401)

    def test_lan_correct_token_ok(self):
        server.SETTINGS = self.lan
        with mock.patch.object(
                server.BandHandler, 'modem_health',
                return_value={"status": "ok", "transport": "qmi",
                              "lte_bands": 1, "nr_bands": 0,
                              "md_session_owner": None}):
            status, _ = run_api('/api/health?action=health', 'GET',
                                headers={'Authorization':
                                         'Bearer sekret-token'},
                                addr=('192.168.1.42', 54321))
        self.assertEqual(status, 200)

    def test_lan_wrong_token_unauthorized(self):
        server.SETTINGS = self.lan
        status, _ = run_api('/api/health?action=health', 'GET',
                            headers={'Authorization': 'Bearer wrong-token'},
                            addr=('192.168.1.42', 54321))
        self.assertEqual(status, 401)

    def test_lan_disabled_no_gate(self):
        server.SETTINGS = {"bind": "127.0.0.1", "token": "sekret-token"}
        with mock.patch.object(
                server.BandHandler, 'modem_health',
                return_value={"status": "ok", "transport": "qmi",
                              "lte_bands": 1, "nr_bands": 0,
                              "md_session_owner": None}):
            status, _ = run_api('/api/health?action=health', 'GET',
                                addr=('192.168.1.42', 54321))
        self.assertEqual(status, 200)

    def test_lan_without_configured_token_rejects_all(self):
        server.SETTINGS = {"bind": "0.0.0.0", "token": None}
        status, _ = run_api('/api/health?action=health', 'GET',
                            addr=('192.168.1.42', 54321))
        self.assertEqual(status, 401)

    def test_non_ascii_token_rejected_not_crash(self):
        server.SETTINGS = self.lan
        status, _ = run_api('/api/health?action=health', 'GET',
                            headers={'Authorization': 'Bearer \u00e9\u00e8'},
                            addr=('192.168.1.42', 54321))
        self.assertEqual(status, 401)

    def test_check_auth_constant_time_no_type_error(self):
        """Token-only check: wrong/missing tokens never raise, even with
        non-ASCII input (compare_digest TypeError must not escape)."""
        server.SETTINGS = self.lan
        self.assertEqual(self._check('192.168.1.42', {}), UNAUTHORIZED)
        self.assertEqual(self._check('192.168.1.42',
                                     {"Authorization": "Bearer x"}),
                         UNAUTHORIZED)

    def _check(self, addr, headers):
        h = object.__new__(server.BandHandler)
        h.client_address = (addr, 54321)
        h.headers = FakeHeaders(headers)
        return h._check_auth()

    def test_http_401_status_and_body(self):
        server.SETTINGS = self.lan
        status, res = run_api('/api/read?action=read', 'GET',
                              addr=('192.168.1.42', 9999))
        self.assertEqual(status, 401)
        self.assertEqual(res, UNAUTHORIZED)

    def test_http_correct_token_passes_gate(self):
        server.SETTINGS = self.lan
        # /api/read answers 200 with the carrier fallback (no modem here),
        # so it is the right probe for the auth gate — /api/health would
        # legitimately answer 503 when no transport is reachable (A-02).
        status, _ = run_api('/api/read?action=read', 'GET',
                            headers={"Authorization": "Bearer sekret-token"},
                            addr=('192.168.1.42', 9999))
        self.assertEqual(status, 200)

    def test_loopback_request_not_gated_end_to_end(self):
        server.SETTINGS = self.lan
        status, _ = run_api('/api/read?action=read', 'GET',
                            addr=('127.0.0.1', 9999))
        self.assertEqual(status, 200)
    def test_get_settings_exempt_from_auth(self):
        """Bootstrap: a fresh laptop page can read token_required with no
        stored token, and no token material is leaked."""
        server.SETTINGS = self.lan
        status, res = run_api('/api/settings?action=settings', 'GET',
                              addr=('192.168.1.42', 9999))
        self.assertEqual(status, 200)
        self.assertTrue(res["token_required"])
        self.assertNotIn("token", res)

    def test_bare_settings_get_exempt(self):
        """A-158 + A-65: the auth exemption is path-based, so the documented
        bare GET /api/settings is not 401-gated for a remote LAN client, and
        the action is derived from the path (A-65) so the bare route
        dispatches and answers with the settings payload (no token value)."""
        server.SETTINGS = self.lan
        status, res = run_api('/api/settings', 'GET',
                              addr=('192.168.1.42', 9999))
        self.assertEqual(status, 200)
        self.assertEqual(res, {"ok": True, "lan_enabled": True,
                               "token_required": True})

    def test_post_settings_still_gated_in_lan_mode(self):
        server.SETTINGS = self.lan
        body = json.dumps({"lan_enabled": True}).encode()
        status, res = run_api(
            '/api/settings?action=settings', 'POST',
            headers=FakeHeaders({'Content-Length': str(len(body))}),
            addr=('192.168.1.42', 9999), body=body)
        self.assertEqual(status, 401)
        self.assertEqual(res, UNAUTHORIZED)

    def test_post_settings_with_token_in_lan_mode(self):
        server.SETTINGS = self.lan
        body = json.dumps({"lan_enabled": False}).encode()
        status, res = run_api(
            '/api/settings?action=settings', 'POST',
            headers=FakeHeaders({'Content-Length': str(len(body)),
                                 'Authorization': 'Bearer sekret-token'}),
            addr=('192.168.1.42', 9999), body=body)
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])
        self.assertFalse(res["lan_enabled"])


class TestRegenerateAuth(SettingsFileCase):
    """A-220: token rotation is gated on the CURRENT token — an
    unauthenticated caller cannot mint-and-steal a fresh credential."""

    def test_regenerate_without_token_401(self):
        server.SETTINGS = {"bind": "0.0.0.0", "token": "old-token",
                           "drop_log": False}
        server._save_settings()  # baseline on disk
        body = json.dumps({"lan_enabled": True,
                           "regenerate": True}).encode()
        status, res = run_api('/api/settings?action=settings', 'POST',
                              headers=FakeHeaders(
                                  {'Content-Length': str(len(body))}),
                              addr=('127.0.0.1', 54321), body=body)
        self.assertEqual(status, 401)
        self.assertEqual(res, UNAUTHORIZED)
        # The live and persisted token are untouched.
        self.assertEqual(server.SETTINGS["token"], "old-token")
        self.assertEqual(
            json.loads(server.SETTINGS_FILE.read_text())["token"],
            "old-token")

    def test_regenerate_with_token_rotates(self):
        server.SETTINGS = {"bind": "0.0.0.0", "token": "old-token",
                           "drop_log": False}
        body = json.dumps({"lan_enabled": True,
                           "regenerate": True}).encode()
        status, res = run_api('/api/settings?action=settings', 'POST',
                              headers=FakeHeaders(
                                  {'Content-Length': str(len(body)),
                                   'Authorization': 'Bearer old-token'}),
                              addr=('127.0.0.1', 54321), body=body)
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])
        self.assertIsInstance(res["token"], str)
        self.assertNotEqual(res["token"], "old-token")
        self.assertEqual(
            json.loads(server.SETTINGS_FILE.read_text())["token"],
            res["token"])


class TestEffectiveBindAuth(SettingsFileCase):
    """A-049: the auth gate keys on the ACTUAL listening address, not the
    pending settings value — disabling LAN must not drop the gate before
    the socket is rebound."""

    def setUp(self):
        super().setUp()
        self._orig_effective = server._EFFECTIVE_BIND
        server._EFFECTIVE_BIND = "0.0.0.0"  # socket still on the LAN
        server.SETTINGS = {"bind": "127.0.0.1", "token": "tok",
                           "drop_log": False}  # settings say loopback

    def tearDown(self):
        server._EFFECTIVE_BIND = self._orig_effective
        super().tearDown()

    def test_remote_still_gated_until_socket_rebound(self):
        status, _ = run_api('/api/health?action=health', 'GET',
                            addr=('192.168.1.42', 9999))
        self.assertEqual(status, 401)
        with mock.patch.object(
                server.BandHandler, 'modem_health',
                return_value={"status": "ok", "transport": "qmi",
                              "lte_bands": 1, "nr_bands": 0,
                              "md_session_owner": None}):
            status, _ = run_api('/api/health?action=health', 'GET',
                                headers={'Authorization': 'Bearer tok'},
                                addr=('192.168.1.42', 9999))
        self.assertEqual(status, 200)

    def test_loopback_read_only_still_exempt(self):
        with mock.patch.object(
                server.BandHandler, 'modem_health',
                return_value={"status": "ok", "transport": "qmi",
                              "lte_bands": 1, "nr_bands": 0,
                              "md_session_owner": None}):
            status, _ = run_api('/api/health?action=health', 'GET',
                                addr=('127.0.0.1', 9999))
        self.assertEqual(status, 200)


class TestLoopbackDetection(unittest.TestCase):
    """A-073: the whole 127.0.0.0/8 range is loopback, not just 127.0.0.1."""

    def is_loopback(self, addr):
        return make_handler(addr=(addr, 54321))._is_loopback()

    def test_full_loopback_range(self):
        for addr in ("127.0.0.1", "127.0.0.2", "127.42.0.9",
                     "127.255.255.254", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(self.is_loopback(addr), addr)

    def test_non_loopback_rejected(self):
        for addr in ("192.168.1.42", "10.0.0.5", "172.16.0.1", "0.0.0.0",
                     "not-an-ip"):
            self.assertFalse(self.is_loopback(addr), addr)


class TestPostOnlyMutations(unittest.TestCase):
    """A-66/A-71: state-changing endpoints reject GET — a link, prefetcher,
    retrying proxy, or <img> tag must never trigger them."""

    def test_get_write_rejected(self):
        status, res = run_api('/api/write?action=write', 'GET',
                              body=json.dumps({"lte": [1], "nr": []}).encode())
        self.assertEqual(status, 405)
        self.assertFalse(res["ok"])

    def test_get_restart_rejected(self):
        status, res = run_api('/api/restart?action=restart', 'GET')
        self.assertEqual(status, 405)
        self.assertFalse(res["ok"])

    def test_get_boot_apply_rejected(self):
        status, _ = run_api('/api/boot-apply?action=boot-apply', 'GET')
        self.assertEqual(status, 405)

    def test_get_modem_reset_rejected(self):
        status, _ = run_api('/api/modem-reset?action=modem-reset', 'GET')
        self.assertEqual(status, 405)

    def test_get_export_rejected(self):
        status, _ = run_api('/api/export?action=export', 'GET')
        self.assertEqual(status, 405)


class TestRestartSerialization(unittest.TestCase):
    """A-041: only one restart may be pending at a time."""

    def setUp(self):
        self._orig_pending = server._RESTART_PENDING
        server._RESTART_PENDING = False

    def tearDown(self):
        server._RESTART_PENDING = self._orig_pending

    def test_second_restart_while_pending_rejected(self):
        with mock.patch("subprocess.Popen"), \
             mock.patch("threading.Thread") as thread:
            first = server.BandHandler.restart_service(None)
            second = server.BandHandler.restart_service(None)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertIn("already scheduled", second["error"])
        thread.assert_called_once()  # only one restart was scheduled

    def test_pending_flag_cleared_after_restart_attempt(self):
        with mock.patch("subprocess.Popen") as popen, \
             mock.patch("time.sleep"):
            holder = {}

            class StubThread(object):
                def start(self):
                    pass

            def fake_thread(target, daemon=False):
                holder["target"] = target
                return StubThread()
            with mock.patch("threading.Thread", side_effect=fake_thread):
                res = server.BandHandler.restart_service(None)
            self.assertTrue(res["ok"])
            self.assertTrue(server._RESTART_PENDING)
            holder["target"]()  # run the restart body synchronously
            self.assertFalse(server._RESTART_PENDING)
            popen.assert_called_once()


class TestExport(SettingsFileCase):
    """POST /api/export — validated, unique, symlink-proof writes
    (A-054/A-155/A-211)."""

    def setUp(self):
        super().setUp()
        self._orig_export = server.EXPORT_DIR
        server.EXPORT_DIR = Path(self._tmp.name)
        self.h = server.BandHandler.__new__(server.BandHandler)

    def tearDown(self):
        server.EXPORT_DIR = self._orig_export
        super().tearDown()

    def test_valid_export_normalized_and_written(self):
        res = self.h.export_config({"lte": ["3", 1, "3"], "nr": [77, "78"]})
        self.assertTrue(res["ok"])
        path = Path(res["path"])
        self.assertTrue(path.exists())
        self.assertEqual(json.loads(path.read_text()),
                         {"lte": [3, 1], "nr": [77, 78]})

    def test_invalid_body_rejected(self):
        """A-155: non-dict bodies and out-of-range bands are rejected, and
        nothing is persisted."""
        res = self.h.export_config(["not", "a", "dict"])
        self.assertFalse(res["ok"])
        res = self.h.export_config({"lte": [999, "x"], "nr": [1337]})
        self.assertFalse(res["ok"])
        self.assertEqual(
            list(Path(self._tmp.name).glob("bandctl-export-*")), [])

    def test_concurrent_exports_get_unique_paths(self):
        """A-054: same-millisecond exports must not overwrite each other."""
        res1 = self.h.export_config({"lte": [1], "nr": []})
        res2 = self.h.export_config({"lte": [2], "nr": []})
        self.assertTrue(res1["ok"])
        self.assertTrue(res2["ok"])
        self.assertNotEqual(res1["path"], res2["path"])
        self.assertEqual(json.loads(Path(res1["path"]).read_text())["lte"],
                         [1])
        self.assertEqual(json.loads(Path(res2["path"]).read_text())["lte"],
                         [2])

    def test_export_does_not_follow_planted_symlink(self):
        """A-211: os.replace over a planted symlink at the destination
        replaces the LINK, never writing through to the target."""
        victim = Path(self._tmp.name) / "victim.json"
        victim.write_text("{}")
        # Pin the timestamp + random suffixes so the final name is known.
        with mock.patch("server.time.strftime",
                        return_value="20260813-000000"), \
             mock.patch("server.time.time", return_value=1.234), \
             mock.patch("secrets.token_hex",
                        side_effect=["aaaaaa", "bbbbbb"]):
            final_name = "bandctl-export-20260813-000000.234-aaaaaa.json"
            os.symlink(str(victim), Path(self._tmp.name) / final_name)
            res = self.h.export_config({"lte": [1], "nr": []})
        self.assertTrue(res["ok"])
        # The victim was not truncated/overwritten...
        self.assertEqual(json.loads(victim.read_text()), {})
        # ...and the returned path is a real file with the export content.
        self.assertFalse(Path(res["path"]).is_symlink())
        self.assertEqual(json.loads(Path(res["path"]).read_text())["lte"],
                         [1])

    def test_export_rejects_symlink_at_temp_name(self):
        """A-211: the random temp file is opened O_EXCL|O_NOFOLLOW — a
        symlink pre-planted at the temp name fails the write instead of
        redirecting it."""
        victim = Path(self._tmp.name) / "victim.json"
        victim.write_text("{}")
        tmp_name = ".export-{}-bbbbbb.tmp".format(os.getpid())
        os.symlink(str(victim), Path(self._tmp.name) / tmp_name)
        with mock.patch("server.time.strftime",
                        return_value="20260813-000000"), \
             mock.patch("server.time.time", return_value=1.234), \
             mock.patch("secrets.token_hex",
                        side_effect=["aaaaaa", "bbbbbb"]):
            res = self.h.export_config({"lte": [1], "nr": []})
        self.assertFalse(res["ok"])
        self.assertEqual(json.loads(victim.read_text()), {})


class TestWriteConfigMirror(unittest.TestCase):
    """A-189 superseded by A-68: a failed apply is never promoted to
    boot-time source of truth, so the config file is not mirrored on
    failure."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = server.CONFIG_FILE
        server.CONFIG_FILE = Path(self._tmp.name) / "bands.json"

    def tearDown(self):
        server.CONFIG_FILE = self._orig
        self._tmp.cleanup()

    def test_failed_apply_not_mirrored(self):
        h = server.BandHandler.__new__(server.BandHandler)
        with mock.patch.object(h, "_apply_bands",
                               return_value=(False, "diag",
                                             "diag write failed")):
            res = h.write_config({"lte": [1, 2], "nr": [77]})
        self.assertFalse(res["ok"])
        self.assertFalse(server.CONFIG_FILE.exists())


class TestCors(unittest.TestCase):
    """A-026: no wildcard CORS — only the trusted WebView origins are
    reflected, so arbitrary pages cannot read API responses."""

    def cors_headers(self, origin):
        headers = {'Origin': origin} if origin else {}
        h = make_handler('/api/health?action=health', 'GET',
                         headers=headers)
        h.handle_api()
        raw = h.wfile.getvalue()
        head = raw.split(b'\r\n\r\n', 1)[0]
        out = {}
        for line in head.split(b'\r\n')[1:]:
            if b': ' in line:
                k, _, v = line.partition(b': ')
                out[k.lower().decode()] = v.decode()
        return out

    def test_wildcard_never_sent(self):
        headers = self.cors_headers('https://evil.example')
        self.assertNotIn('access-control-allow-origin', headers)

    def test_ksu_origin_reflected(self):
        headers = self.cors_headers('ksu://webui/bandctl')
        self.assertEqual(headers.get('access-control-allow-origin'),
                         'ksu://webui/bandctl')

    def test_appassets_origin_reflected(self):
        headers = self.cors_headers('appassets://android_asset/index.html')
        self.assertEqual(headers.get('access-control-allow-origin'),
                         'appassets://android_asset/index.html')

    def test_no_origin_no_cors_headers(self):
        headers = self.cors_headers(None)
        self.assertNotIn('access-control-allow-origin', headers)

    def test_preflight_origin_gated(self):
        """do_OPTIONS: only trusted origins receive allow headers."""
        h = make_handler('/api/write?action=write', 'OPTIONS',
                         headers={'Origin': 'https://evil.example'})
        h.do_OPTIONS()
        raw = h.wfile.getvalue()
        self.assertNotIn(b'Access-Control-Allow-Origin', raw)


class TestHostValidation(unittest.TestCase):
    """A-178: DNS-rebinding defense — non-loopback Host headers are
    rejected."""

    def test_rebinding_host_rejected(self):
        status, res = run_api('/api/health?action=health', 'GET',
                              headers={'Host': 'evil.example'},
                              addr=('127.0.0.1', 54321))
        self.assertEqual(status, 403)
        self.assertFalse(res["ok"])

    def test_loopback_host_allowed(self):
        for host in ('localhost:8080', '127.0.0.1:8080', '[::1]:8080',
                     '127.0.0.2'):
            with mock.patch.object(
                    server.BandHandler, 'modem_health',
                    return_value={"status": "ok", "transport": "qmi",
                                  "lte_bands": 1, "nr_bands": 0,
                                  "md_session_owner": None}):
                status, _ = run_api('/api/health?action=health', 'GET',
                                    headers={'Host': host},
                                    addr=('127.0.0.1', 54321))
            self.assertEqual(status, 200, host)


class TestQmiBinarySafety(unittest.TestCase):
    """A-198: no third-party fallback — a module without the bundled QMI
    binary simply reports QMI unavailable."""

    def test_missing_bundled_binary_reports_unavailable(self):
        with mock.patch.object(server, "QMI_BIN", None):
            self.assertEqual(server._run_qmi(["--get"], 5), (None, ""))


class TestBandValidation(unittest.TestCase):
    """M8: lte/nr validation — ints 1..79, numeric strings, dedup, LTE
    non-empty; never raises (Infinity etc. -> error, not crash)."""

    def setUp(self):
        self.h = server.BandHandler.__new__(server.BandHandler)

    def test_valid_dedupes_preserving_order(self):
        bands, err = self.h._validate_bands(
            {"lte": ["3", 1, "3", 2, 1], "nr": ["77", 78]})
        self.assertIsNone(err)
        self.assertEqual(bands, {"lte": [3, 1, 2], "nr": [77, 78]})

    def test_string_digits_accepted(self):
        bands, err = self.h._validate_bands({"lte": ["1", "2"], "nr": ["78"]})
        self.assertIsNone(err)
        self.assertEqual(bands, {"lte": [1, 2], "nr": [78]})

    def test_out_of_range_rejected(self):
        bands, err = self.h._validate_bands({"lte": [80], "nr": []})
        self.assertIsNone(bands)
        self.assertIn("1-79", err)
        bands, err = self.h._validate_bands({"lte": [1], "nr": [0]})
        self.assertIsNone(bands)
        bands, err = self.h._validate_bands({"lte": [-3], "nr": []})
        self.assertIsNone(bands)

    def test_empty_lte_rejected(self):
        bands, err = self.h._validate_bands({"lte": [], "nr": [77]})
        self.assertIsNone(bands)
        self.assertIn("at least one", err)

    def test_infinity_rejected_not_crash(self):
        bands, err = self.h._validate_bands({"lte": [float('inf')], "nr": []})
        self.assertIsNone(bands)

    def test_float_and_bool_rejected(self):
        self.assertIsNone(
            self.h._validate_bands({"lte": [1.5], "nr": []})[0])
        self.assertIsNone(
            self.h._validate_bands({"lte": [True], "nr": []})[0])

    def test_non_list_and_non_dict_rejected(self):
        self.assertIsNone(self.h._validate_bands({"lte": "1,2", "nr": []})[0])
        self.assertIsNone(self.h._validate_bands({"lte": [1], "nr": "77"})[0])
        self.assertIsNone(self.h._validate_bands(None)[0])

    def test_write_config_invalid_never_applies(self):
        res = self.h.write_config({"lte": [0], "nr": []})
        self.assertFalse(res["ok"])
        self.assertIn("error", res)

    def test_wrong_rat_namespace_rejected(self):
        """A-131: an NR-only band must not be accepted in lte, and an
        LTE-only band must not be accepted in nr."""
        bands, err = self.h._validate_bands({"lte": [77], "nr": []})
        self.assertIsNone(bands)
        self.assertIn("LTE band catalog", err)
        bands, err = self.h._validate_bands({"lte": [1], "nr": [4]})
        self.assertIsNone(bands)
        self.assertIn("NR band catalog", err)


class TestBootApply(unittest.TestCase):
    """boot-apply reads config/bands.json and validates before applying."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = server.CONFIG_FILE
        server.CONFIG_FILE = Path(self._tmp.name) / "bands.json"

    def tearDown(self):
        server.CONFIG_FILE = self._orig
        self._tmp.cleanup()

    def test_missing_file_skips(self):
        res = server.BandHandler.boot_apply(None)
        self.assertEqual(res, {"ok": True, "skipped": True})

    def test_valid_file_applies_with_dedup(self):
        server.CONFIG_FILE.write_text(
            json.dumps({"lte": ["1", "2", "2"], "nr": ["77"]}))
        h = server.BandHandler.__new__(server.BandHandler)
        with mock.patch.object(h, "_apply_bands",
                               return_value=(True, "qmi", None)) as apply:
            res = h.boot_apply()
        apply.assert_called_once_with([1, 2], [77])
        self.assertEqual(res, {"ok": True, "source": "qmi",
                               "lte": ["1", "2"], "nr": ["77"]})

    def test_empty_lte_is_error_not_skip(self):
        server.CONFIG_FILE.write_text(
            json.dumps({"lte": [], "nr": ["77"]}))
        h = server.BandHandler.__new__(server.BandHandler)
        res = h.boot_apply()
        self.assertFalse(res["ok"])
        self.assertIn("at least one", res["error"])

    def test_out_of_range_is_error(self):
        server.CONFIG_FILE.write_text(json.dumps({"lte": [99], "nr": []}))
        h = server.BandHandler.__new__(server.BandHandler)
        res = h.boot_apply()
        self.assertFalse(res["ok"])

    def test_malformed_json_reports(self):
        server.CONFIG_FILE.write_text("{nope")
        h = server.BandHandler.__new__(server.BandHandler)
        self.assertEqual(h.boot_apply(),
                         {"ok": False, "error": "invalid config"})

    def test_infinity_in_file_error_not_crash(self):
        server.CONFIG_FILE.write_text(
            json.dumps({"lte": [float('inf')], "nr": []}))
        h = server.BandHandler.__new__(server.BandHandler)
        res = h.boot_apply()
        self.assertFalse(res["ok"])

    def test_apply_failure_reported(self):
        server.CONFIG_FILE.write_text(json.dumps({"lte": [1], "nr": []}))
        h = server.BandHandler.__new__(server.BandHandler)
        with mock.patch.object(h, "_apply_bands",
                               return_value=(False, "diag", "diag write failed")):
            res = h.boot_apply()
        self.assertEqual(res, {"ok": False, "error": "diag write failed"})


class TestGuardedParse(unittest.TestCase):
    """M6: malformed/oversized request bodies become error JSON, never a
    500; non-/api/ paths and unknown actions answer cleanly."""

    def test_malformed_json_body_error_not_crash(self):
        status, res = run_api('/api/write?action=write', 'POST',
                              headers=FakeHeaders({'Content-Length': '5'}),
                              body=b'{nope')
        self.assertEqual(status, 200)
        self.assertFalse(res["ok"])
        self.assertIn("invalid JSON body", res["error"])

    def test_oversized_body_rejected(self):
        big = b'x' * (server.MAX_BODY_BYTES + 1)
        status, res = run_api(
            '/api/write?action=write', 'POST',
            headers=FakeHeaders({'Content-Length': str(len(big))}),
            body=big)
        self.assertEqual(status, 200)
        self.assertFalse(res["ok"])
        self.assertIn("exceeds", res["error"])

    def test_invalid_content_length_rejected(self):
        status, res = run_api('/api/write?action=write', 'POST',
                              headers=FakeHeaders({'Content-Length': 'abc'}),
                              body=b'{}')
        self.assertEqual(status, 200)
        self.assertFalse(res["ok"])
        self.assertIn("Content-Length", res["error"])

    def test_non_api_path_unknown_action(self):
        status, res = run_api('/etc/passwd', 'GET')
        self.assertEqual(status, 400)
        self.assertEqual(res, {"error": "unknown action"})

    def test_unknown_action(self):
        status, res = run_api('/api/foo?action=nope', 'GET')
        self.assertEqual(status, 400)
        self.assertEqual(res, {"error": "unknown action"})

    def test_write_body_null_error_not_crash(self):
        status, res = run_api('/api/write?action=write', 'POST',
                              headers=FakeHeaders({'Content-Length': '4'}),
                              body=b'null')
        self.assertEqual(status, 200)
        self.assertFalse(res["ok"])
        self.assertIn("JSON object", res["error"])


class TestStaticServing(unittest.TestCase):
    """SRV-07: only index.html is served; everything else 404s."""

    def setUp(self):
        # The server resolves WEB_DIR from its own file location, which in
        # this repo layout (server.py at the root, not module/web/) points
        # outside the checkout. Patch it to a temp dir so the static-serving
        # contract is exercised deterministically.
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_web_dir = server.WEB_DIR
        server.WEB_DIR = Path(self._tmp.name)
        (server.WEB_DIR / "index.html").write_bytes(b"<html>bandctl test</html>")

    def tearDown(self):
        server.WEB_DIR = self._orig_web_dir
        self._tmp.cleanup()

    def serve(self, path):
        h = make_handler(path, 'GET')
        h._serve_static()
        raw = h.wfile.getvalue()
        return int(raw.split(b'\r\n', 1)[0].split()[1]), raw

    def test_index_served(self):
        status, raw = self.serve('/')
        self.assertEqual(status, 200)
        idx = (server.WEB_DIR / "index.html").read_bytes()
        self.assertEqual(raw.split(b'\r\n\r\n', 1)[1], idx)

    def test_index_html_served_with_query(self):
        status, _ = self.serve('/index.html?x=1')
        self.assertEqual(status, 200)

    def test_everything_else_404(self):
        for path in ('/server.py', '/__pycache__/', '/config/bands.json',
                     '/settings.json', '/foo', '/api/'):
            status, _ = self.serve(path)
            self.assertEqual(status, 404, path)


class TestModemReset(unittest.TestCase):
    """v2.4: the airplane-mode fallback must never leave airplane mode on.

    Covers the guaranteed-cleanup contract: the radio-power path stays
    airplane-free, the airplane path always disables (even when the toggle
    fails mid-way), and a disable that cannot succeed is reported loudly
    instead of silently leaving the radio off."""

    def _reset(self):
        h = make_handler('/api/modem-reset?action=modem-reset', 'POST')
        h.handle_api()
        raw = h.wfile.getvalue()
        status = int(raw.split(b'\r\n', 1)[0].split()[1])
        return status, json.loads(raw.split(b'\r\n\r\n', 1)[1])

    def test_radio_power_path_no_airplane(self):
        with mock.patch.object(server, '_cmd_available',
                               side_effect=lambda s, sub: s == 'phone'), \
             mock.patch.object(server, '_run_cmd') as run, \
             mock.patch.object(server, '_wait_for_radio_state',
                               return_value=True), \
             mock.patch.object(server, '_wait_for_radio_recovery',
                               return_value=True), \
             mock.patch.object(server, '_wait_radio_on',
                               return_value=True):
            status, payload = self._reset()
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        cmds = [c.args[0][-1] for c in run.call_args_list]
        self.assertEqual(cmds, ['off', 'on'])

    def test_radio_power_path_reports_unrecovered_state(self):
        """A-197: a reset whose radio never returns to IN_SERVICE must
        report ok:false with the observed post-reset state, not a fake
        success."""
        with mock.patch.object(server, '_cmd_available',
                               side_effect=lambda s, sub: s == 'phone'), \
             mock.patch.object(server, '_run_cmd'), \
             mock.patch.object(server, '_wait_for_radio_state',
                               return_value=True), \
             mock.patch.object(server, '_wait_for_radio_recovery',
                               return_value=False), \
             mock.patch.object(server, '_radio_reg_state',
                               return_value='OUT_OF_SERVICE'):
            status, payload = self._reset()
        self.assertEqual(status, 200)
        self.assertFalse(payload['ok'])
        self.assertIn('OUT_OF_SERVICE', payload['error'])

    def test_airplane_path_success_disables_and_verifies(self):
        with mock.patch.object(server, '_cmd_available',
                               side_effect=lambda s, sub: s == 'connectivity'), \
             mock.patch.object(server, '_run_cmd'), \
             mock.patch.object(server, '_wait_for_radio_state',
                               return_value=True), \
             mock.patch.object(server, '_wait_for_radio_recovery',
                               return_value=True), \
             mock.patch.object(server, '_disable_airplane',
                               return_value=True) as disable, \
             mock.patch.object(server, '_wait_radio_on',
                               return_value=True):
            status, payload = self._reset()
        self.assertTrue(payload['ok'])
        disable.assert_called_once()

    def test_airplane_path_reports_unrecovered_state(self):
        """A-197: airplane fallback also waits for IN_SERVICE before
        claiming success."""
        with mock.patch.object(server, '_cmd_available',
                               side_effect=lambda s, sub: s == 'connectivity'), \
             mock.patch.object(server, '_run_cmd'), \
             mock.patch.object(server, '_wait_for_radio_state',
                               return_value=True), \
             mock.patch.object(server, '_wait_radio_on',
                               return_value=True), \
             mock.patch.object(server, '_wait_for_radio_recovery',
                               return_value=False), \
             mock.patch.object(server, '_radio_reg_state',
                               return_value='POWER_OFF'), \
             mock.patch.object(server, '_disable_airplane',
                               return_value=True):
            status, payload = self._reset()
        self.assertFalse(payload['ok'])
        self.assertIn('POWER_OFF', payload['error'])

    def test_airplane_mid_toggle_failure_still_cleans_up(self):
        def boom(cmd, timeout=None):
            if 'enable' in cmd:
                raise RuntimeError('enable crashed')
            return ''
        with mock.patch.object(server, '_cmd_available',
                               side_effect=lambda s, sub: s == 'connectivity'), \
             mock.patch.object(server, '_run_cmd', side_effect=boom), \
             mock.patch.object(server, '_airplane_on', return_value=True), \
             mock.patch.object(server, '_disable_airplane',
                               return_value=True) as disable:
            status, payload = self._reset()
        self.assertFalse(payload['ok'])
        disable.assert_called_once()

    def test_airplane_left_on_reported_loudly(self):
        with mock.patch.object(server, '_cmd_available',
                               side_effect=lambda s, sub: s == 'connectivity'), \
             mock.patch.object(server, '_run_cmd'), \
             mock.patch.object(server, '_wait_for_radio_state',
                               return_value=True), \
             mock.patch.object(server, '_disable_airplane',
                               return_value=False):
            status, payload = self._reset()
        self.assertFalse(payload['ok'])
        self.assertIn('airplane mode left on', payload['error'])

    def test_radio_path_on_not_verified_fails(self):
        """A-20: the preferred path must not certify success until the
        radio verifiably leaves POWER_OFF again after power-on."""
        with mock.patch.object(server, '_cmd_available',
                               side_effect=lambda s, sub: s == 'phone'), \
             mock.patch.object(server, '_run_cmd'), \
             mock.patch.object(server, '_wait_for_radio_state',
                               return_value=True), \
             mock.patch.object(server, '_wait_radio_on',
                               return_value=False):
            status, payload = self._reset()
        self.assertEqual(status, 200)
        self.assertFalse(payload['ok'])
        self.assertIn('did not power back on', payload['error'])

    def test_preferred_ineffective_falls_back_to_airplane(self):
        """A-137: a listed-but-ineffective radio power command must not
        block the airplane-mode fallback."""
        with mock.patch.object(server, '_cmd_available',
                               side_effect=lambda s, sub: True), \
             mock.patch.object(server, '_run_cmd') as run, \
             mock.patch.object(server, '_wait_for_radio_state',
                               side_effect=[False, True]), \
             mock.patch.object(server, '_airplane_on',
                               return_value=None), \
             mock.patch.object(server, '_disable_airplane',
                               return_value=True), \
             mock.patch.object(server, '_wait_radio_on',
                               return_value=True), \
             mock.patch.object(server, '_wait_for_radio_recovery',
                               return_value=True):
            status, payload = self._reset()
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        cmds = [c.args[0] for c in run.call_args_list]
        self.assertTrue(any('airplane-mode' in c for c in cmds),
                        "airplane-mode fallback was not attempted: {}".format(cmds))

    def test_airplane_already_on_is_restored_not_cleared(self):
        """A-62: a pre-existing airplane-mode choice survives the reset —
        the fallback restores it instead of turning connectivity back on."""
        with mock.patch.object(server, '_cmd_available',
                               side_effect=lambda s, sub: s == 'connectivity'), \
             mock.patch.object(server, '_run_cmd'), \
             mock.patch.object(server, '_wait_for_radio_state',
                               return_value=True), \
             mock.patch.object(server, '_airplane_on',
                               return_value=True), \
             mock.patch.object(server, '_disable_airplane') as disable:
            status, payload = self._reset()
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        disable.assert_not_called()

    def test_airplane_path_radio_stays_off_fails(self):
        """A-148: the fallback must not report success while the radio is
        still POWER_OFF after cleanup."""
        with mock.patch.object(server, '_cmd_available',
                               side_effect=lambda s, sub: s == 'connectivity'), \
             mock.patch.object(server, '_run_cmd'), \
             mock.patch.object(server, '_wait_for_radio_state',
                               return_value=True), \
             mock.patch.object(server, '_airplane_on',
                               return_value=False), \
             mock.patch.object(server, '_disable_airplane',
                               return_value=True), \
             mock.patch.object(server, '_wait_radio_on',
                               return_value=False):
            status, payload = self._reset()
        self.assertEqual(status, 200)
        self.assertFalse(payload['ok'])
        self.assertIn('did not power back on', payload['error'])

    def test_disable_airplane_unverifiable_prop_fails(self):
        """A-46: cleanup whose verification is unavailable (getprop
        failure -> None) must never be certified as success."""
        with mock.patch.object(server, '_run_cmd'), \
             mock.patch.object(server, '_airplane_on', return_value=None), \
             mock.patch.object(server.time, 'sleep'):
            self.assertFalse(server._disable_airplane())


class TestDropLog(unittest.TestCase):
    """v2.5: the debug drop logger — API toggle + watchdog pieces."""

    def setUp(self):
        self._orig = server.SETTINGS.get("drop_log")
        server.SETTINGS["drop_log"] = False

    def tearDown(self):
        server.SETTINGS["drop_log"] = self._orig

    def _get(self):
        return run_api('/api/drop-log?action=drop-log')

    def _post(self, enabled):
        body = json.dumps({'enabled': enabled}).encode()
        return run_api('/api/drop-log?action=drop-log', 'POST',
                       headers=FakeHeaders(
                           {'Content-Length': str(len(body))}),
                       body=body)

    def test_get_default_disabled(self):
        with mock.patch.object(server, '_save_settings'):
            status, payload = self._get()
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        self.assertFalse(payload['enabled'])
        self.assertIn('drop_log', payload['dir'])

    def test_post_toggles_and_persists(self):
        with mock.patch.object(server, '_save_settings') as save:
            status, payload = self._post(True)
        self.assertEqual(status, 200)
        self.assertTrue(payload['enabled'])
        self.assertTrue(server.SETTINGS['drop_log'])
        save.assert_called_once()
        with mock.patch.object(server, '_save_settings'):
            status, payload = self._post(False)
        self.assertFalse(server.SETTINGS['drop_log'])

    def test_post_rejects_non_bool(self):
        with mock.patch.object(server, '_save_settings'):
            status, payload = run_api(
                '/api/drop-log?action=drop-log', 'POST',
                body=json.dumps({'enabled': 'yes'}).encode())
        self.assertEqual(status, 200)
        self.assertFalse(payload['ok'])

    def test_drop_state_detects_power_off(self):
        fake = "mServiceState={mVoiceRegState=3(POWER_OFF), " \
               "mDataRegState=3(POWER_OFF), mOperatorAlphaLong=null}\n"
        with mock.patch.object(server, '_run_dumpsys', return_value=fake):
            reg = server._drop_state()
        self.assertEqual(reg['service_state'], 'POWER_OFF')

    def test_snapshot_text_includes_context(self):
        with mock.patch.object(server, '_run_dumpsys',
                               return_value="mServiceState={mVoiceRegState=1(OUT_OF_SERVICE)}\nmCallState=0\n"), \
             mock.patch.object(server, '_run_cmd',
                               side_effect=lambda args, timeout=None:
                               'Wifi is enabled\n' if 'wifi' in args
                               else 'inet 192.168.8.1/24\n'
                               if 'addr' in args
                               else '08-05 15:43:43 PHONE0 REG_HOME\n'), \
             mock.patch.object(server, '_read_sys',
                               side_effect=lambda p: '123' if 'wlan0' in p
                               else None):
            text = server._drop_snapshot_text({'service_state': 'OUT_OF_SERVICE'})
        self.assertIn('OUT_OF_SERVICE', text)
        self.assertIn('call_state: 0', text)
        self.assertIn('wifi: Wifi is enabled', text)
        self.assertIn('counters:', text)
        # A-185/A-219: the snapshot must NOT persist raw radio-buffer
        # lines (IMSI/phone numbers) — a redacted count marker replaces
        # them, never the PHONE0 payload itself.
        self.assertIn('omitted (privacy redacted)', text)
        self.assertNotIn('REG_HOME', text)


class TestShippedIndexHtml(unittest.TestCase):
    """A-163: the SHIPPED web/index.html must parse as HTML.

    The static-serving contract tests above stub their own index.html; this
    smoke test loads the page the server actually serves, so a broken
    shipped page (e.g. an unclosed tag) cannot ship undetected.
    """

    def test_shipped_index_html_parses(self):
        from html.parser import HTMLParser

        path = Path(__file__).parent / "web" / "index.html"
        self.assertTrue(path.exists(), "shipped UI missing at web/index.html")
        html = path.read_bytes()
        self.assertTrue(html.strip(), "shipped UI is empty")
        lowered = html.lower()
        self.assertIn(b"<html", lowered)
        self.assertIn(b"</html>", lowered)
        # A lenient HTML parser must be able to consume the whole page.
        parser = HTMLParser()
        parser.feed(html.decode('utf-8', 'replace'))
        parser.close()


# --- Real-format dumpsys fixtures (A-184/A-208/A-209) -------------------
# Drawn from the modem_logs/ captures: nested braces in the mServiceState
# object, dual-RAT blocks, IWLAN transport entries, and the collapse block.

REAL_REG_HEALTHY = (
    "    mCallState=0\n"
    "    mServiceState={mVoiceRegState=0(IN_SERVICE), mDataRegState=0(IN_SERVICE), "
    "mChannelNumber=3150, duplexMode()=1, mCellBandwidths=[20000], "
    "mOperatorAlphaLong=ROGERS, mOperatorAlphaShort=ROGERS, "
    "isManualNetworkSelection=false(automatic), getRilVoiceRadioTechnology=14(LTE), "
    "getRilDataRadioTechnology=14(LTE), mCssIndicator=unsupported, mNetworkId=-1, "
    "mSystemId=-1, mCdmaRoamingIndicator=-1, mCdmaDefaultRoamingIndicator=-1, "
    "mIsEmergencyOnly=false, isUsingCarrierAggregation=false, mArfcnRsrpBoost=0, "
    "mNetworkRegistrationInfos=[NetworkRegistrationInfo{ domain=PS "
    "transportType=WLAN registrationState=NOT_REG_OR_SEARCHING "
    "mInitialRegistrationState=NOT_REG_OR_SEARCHING roamingType=NOT_ROAMING "
    "accessNetworkTechnology=IWLAN rejectCause=0 emergencyEnabled=false "
    "availableServices=[] cellIdentity=null voiceSpecificInfo=null "
    "dataSpecificInfo=null nrState=**** rRplmn= isUsingCarrierAggregation=false}], "
    "mNrFrequencyRange=0, mOperatorAlphaLongRaw=ROGERS, mOperatorAlphaShortRaw=ROGERS, "
    "mIsDataRoamingFromRegistration=false, mIsIwlanPreferred=false}\n"
    "    mSignalStrength=SignalStrength:{mCdma=CellSignalStrengthCdma: cdmaDbm=2147483647 "
    "cdmaEcio=2147483647 evdoDbm=2147483647 evdoEcio=2147483647 evdoSnr=2147483647 level=0,"
    "mGsm=CellSignalStrengthGsm: rssi=2147483647 ber=2147483647 mTa=2147483647 mLevel=0,"
    "mWcdma=CellSignalStrengthWcdma: ss=2147483647 ber=2147483647 rscp=2147483647 ecno=2147483647 level=0,"
    "mTdscdma=CellSignalStrengthTdscdma: rssi=2147483647 ber=2147483647 rscp=2147483647 level=0,"
    "mLte=CellSignalStrengthLte: rssi=-71 rsrp=-105 rsrq=-13 rssnr=15 "
    "cqiTableIndex=2147483647 cqi=2147483647 ta=2147483647 level=3 parametersUseForLevel=0,"
    "mNr=CellSignalStrengthNr:{ csiRsrp = 2147483647 csiRsrq = 2147483647 "
    "csiCqiTableIndex = 2147483647 csiCqiReport = [] ssRsrp = 2147483647 ssRsrq = 2147483647 "
    "ssSinr = 2147483647 ssCqiTableIndex = 2147483647 ssCqiReport = [] level = 0 }}\n"
)

REAL_REG_COLLAPSED = (
    "    mCallState=0\n"
    "    mServiceState={mVoiceRegState=1(OUT_OF_SERVICE), mDataRegState=1(OUT_OF_SERVICE), "
    "mChannelNumber=0, duplexMode()=0, mCellBandwidths=[], mOperatorAlphaLong=null, "
    "mOperatorAlphaShort=null, isManualNetworkSelection=false(automatic), "
    "getRilVoiceRadioTechnology=0(Unknown), getRilDataRadioTechnology=0(Unknown), "
    "mCssIndicator=unsupported, mNetworkId=0, mSystemId=0, mCdmaRoamingIndicator=0, "
    "mCdmaDefaultRoamingIndicator=0, mIsEmergencyOnly=false, "
    "isUsingCarrierAggregation=false, mArfcnRsrpBoost=0, mNetworkRegistrationInfos=[], "
    "mNrFrequencyRange=0, mOperatorAlphaLongRaw=null, mOperatorAlphaShortRaw=null, "
    "mIsDataRoamingFromRegistration=false, mIsIwlanPreferred=false}\n"
    "    mSignalStrength=SignalStrength:{mCdma=CellSignalStrengthCdma: cdmaDbm=2147483647 "
    "cdmaEcio=2147483647 evdoDbm=2147483647 evdoEcio=2147483647 evdoSnr=2147483647 level=0,"
    "mLte=CellSignalStrengthLte: rssi=2147483647 rsrp=2147483647 rsrq=2147483647 "
    "rssnr=2147483647 cqiTableIndex=2147483647 cqi=2147483647 ta=2147483647 level=0 "
    "parametersUseForLevel=0,mNr=CellSignalStrengthNr:{ csiRsrp = 2147483647 "
    "csiRsrq = 2147483647 csiCqiTableIndex = 2147483647 csiCqiReport = [] "
    "ssRsrp = 2147483647 ssRsrq = 2147483647 ssSinr = 2147483647 "
    "ssCqiTableIndex = 2147483647 ssCqiReport = [] level = 0 }}\n"
)

# Compact drop/non-drop registration dicts for watchdog tests.
_DROP_REG = {"service_state": "OUT_OF_SERVICE", "data_state": "OUT_OF_SERVICE",
             "network_type": "Unknown", "operator": None, "roaming": None}
_GOOD_REG = {"service_state": "IN_SERVICE", "data_state": "IN_SERVICE",
             "network_type": "LTE", "operator": "ROGERS", "roaming": False}


class _SeqTime(object):
    """Fake time module for watchdog tests: monotonic returns a scripted
    sequence, strftime a fixed stamp, so durations and refreshes are
    deterministic (A-154)."""

    def __init__(self, monos):
        self._monos = list(monos)

    def monotonic(self):
        if not self._monos:
            raise StopIteration("monotonic sequence exhausted")
        return self._monos.pop(0)

    def strftime(self, fmt, t=None):
        return "2026-08-13 00:00:00"

    def time(self):
        return 1750000000.0

    def sleep(self, s):
        pass


class TestDumpsysParsers(unittest.TestCase):
    """A-184: the UI-facing dumpsys parsers (signal object/legacy,
    registration object/legacy, band camping) — both format branches
    guarded against the format drift an Android update can cause."""

    def test_parse_signal_object_modern(self):
        parsed = server._parse_signal_strength(REAL_REG_HEALTHY)
        self.assertEqual(parsed["tech"], "LTE")
        self.assertEqual(parsed["rsrp_dbm"], -105)
        self.assertEqual(parsed["rsrq_db"], -13)
        self.assertEqual(parsed["level"], 3)

    def test_parse_signal_object_rejects_all_invalid(self):
        parsed = server._parse_signal_strength(REAL_REG_COLLAPSED)
        self.assertIsNone(parsed)

    def test_parse_signal_legacy_flat_list(self):
        text = "SignalStrength: 99 0 99 99 99 99 99 -71 -111 -13 15 2 1\n"
        parsed = server._parse_signal_strength(text)
        self.assertEqual(parsed["tech"], "LTE")
        self.assertEqual(parsed["rsrp_dbm"], -111)
        self.assertEqual(parsed["rsrq_db"], -13)
        self.assertEqual(parsed["level"], 3)  # -111 -> threshold band 3

    def test_parse_registration_modern_object(self):
        reg = server._parse_registration(REAL_REG_HEALTHY)
        self.assertEqual(reg["service_state"], "IN_SERVICE")
        self.assertEqual(reg["data_state"], "IN_SERVICE")
        self.assertEqual(reg["network_type"], "LTE")
        self.assertEqual(reg["operator"], "ROGERS")
        self.assertFalse(reg["roaming"])

    def test_parse_registration_modern_collapsed(self):
        reg = server._parse_registration(REAL_REG_COLLAPSED)
        self.assertEqual(reg["service_state"], "OUT_OF_SERVICE")
        self.assertEqual(reg["data_state"], "OUT_OF_SERVICE")
        self.assertEqual(reg["network_type"], "Unknown")
        self.assertIsNone(reg["operator"])

    def test_parse_registration_legacy_flat(self):
        text = ("mServiceState=ServiceState: 0 0 true home ROGERS 302720 LTE\n"
                "mRoaming=1\nmNetworkType=13\nmOperatorAlphaLong=ROGERS\n")
        reg = server._parse_registration(text)
        self.assertEqual(reg["service_state"], "IN_SERVICE")
        self.assertEqual(reg["data_state"], "IN_SERVICE")
        self.assertEqual(reg["network_type"], "LTE")
        self.assertEqual(reg["operator"], "ROGERS")
        self.assertTrue(reg["roaming"])

    def test_parse_registration_unparseable_returns_none(self):
        """A-144: an mServiceState block that parses nothing is a
        telemetry failure, not an all-null success."""
        for text in ("mServiceState={}\n",
                     "mServiceState={mChannelNumber=3150}\n",
                     "mServiceState=ServiceState: ???\n"):
            self.assertIsNone(server._parse_registration(text), text)

    def test_parse_registration_captures_iwlan_transport(self):
        """A-208: the WLAN/IWLAN transport entry inside
        mNetworkRegistrationInfos must survive into the parsed dict even
        though the collapsed block reads getRil*Technology=0(Unknown)."""
        reg = server._parse_registration(REAL_REG_HEALTHY)
        self.assertEqual(reg["transports"],
                         [{"transport": "WLAN", "tech": "IWLAN"}])

    def test_parse_band_camping_object(self):
        text = ("mCellInfo={mRegistered=YES\n"
                "CellInfoLte:{mRegistered=YES mTimeStampType=2 "
                "mCellIdentity=CellIdentityLte:{ mCi=210655 mPci=262 "
                "mTac=6348 mEarfcn=2050 mBands=[4] mBandwidth=20000 }}\n}")
        self.assertEqual(server._parse_band_camping(text), (2050, 4, "LTE"))

    def test_read_registration_unparseable_reports_error(self):
        """A-144 end-to-end: an all-null snapshot becomes an explicit
        error response instead of a deceptively successful payload."""
        with mock.patch.object(server, '_run_dumpsys',
                               return_value="mServiceState={}\n"):
            res = server.BandHandler.read_registration(None)
        self.assertIn("error", res)


class _SyncThread(object):
    """Runs the target synchronously — for auto-recovery tests (A-196)
    the spawned worker must complete before the poll returns."""

    def __init__(self, target, daemon=None):
        self._target = target

    def start(self):
        self._target()


class TestDropWatchdog(unittest.TestCase):
    """A-209 family: the watchdog chain (detection -> snapshot ->
    rotation -> recovery) driven through _drop_log_poll with real-format
    registration dicts and fast intervals."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = server.DROP_LOG_DIR
        self._orig_gap = server.DROP_SNAP_GAP
        self._orig_confirm = server.DROP_RECOVERY_CONFIRM
        self._orig_max = server.DROP_LOG_MAX_FILES
        self._orig_setting = server.SETTINGS.get("drop_log")
        server.DROP_LOG_DIR = Path(self._tmp.name)
        server.DROP_SNAP_GAP = 60
        server.DROP_RECOVERY_CONFIRM = 2
        server.DROP_LOG_MAX_FILES = 40
        server.SETTINGS["drop_log"] = True
        self._episode_counter = server._EPISODE_COUNTER

    def tearDown(self):
        server.DROP_LOG_DIR = self._orig_dir
        server.DROP_SNAP_GAP = self._orig_gap
        server.DROP_RECOVERY_CONFIRM = self._orig_confirm
        server.DROP_LOG_MAX_FILES = self._orig_max
        server.SETTINGS["drop_log"] = self._orig_setting
        self._tmp.cleanup()

    def _watch(self):
        return server._DropWatch()

    def _snapshot_stub(self, reg):
        return "registration: {}\ncontext-stub\n".format(json.dumps(reg))

    def _poll(self, w, reg, snap=None, fake_time=None):
        """Run one watchdog poll with `reg` as the registration.
        `snap` overrides the snapshot writer (None -> stub); `fake_time`
        swaps in a scripted clock."""
        patchers = [mock.patch.object(server, '_drop_state', return_value=reg)]
        if snap is None:
            patchers.append(mock.patch.object(
                server, '_drop_snapshot_text', side_effect=self._snapshot_stub))
        else:
            patchers.append(mock.patch.object(
                server, '_drop_snapshot_text', side_effect=snap))
        if fake_time is not None:
            patchers.append(mock.patch.object(server, 'time', fake_time))
        with ExitStack() as stack:
            for p in patchers:
                stack.enter_context(p)
            return server._drop_log_poll(w)

    def _episode_files(self):
        return sorted(p.name for p in Path(server.DROP_LOG_DIR).iterdir()
                      if p.name.startswith("drop_"))

    def test_real_format_chain_detection_to_recovery(self):
        """A-209: the real-format registry (nested braces, dual-RAT
        blocks, IWLAN entries) drives the full watchdog chain: detect on
        the collapse, snapshot, recover with duration."""
        w = self._watch()
        self._poll(w, server._parse_registration(REAL_REG_COLLAPSED))
        self.assertTrue(w.in_drop)
        path = w.episode_file
        self._poll(w, _GOOD_REG)
        self._poll(w, _GOOD_REG)
        self.assertFalse(w.in_drop)
        text = path.read_text()
        self.assertIn("=== DROP DETECTED", text)
        self.assertIn("OUT_OF_SERVICE", text)
        self.assertIn("=== RECOVERED", text)
        self.assertIn("(duration", text)

    def test_real_format_snapshot_includes_iwlan_transport(self):
        """A-208/A-209: the snapshot written for the IWLAN phase carries
        the WLAN transport context in the structured registration."""
        w = self._watch()
        self._poll(w, server._parse_registration(REAL_REG_HEALTHY),
                   snap=server._drop_snapshot_text)  # real snapshot writer
        self.assertTrue(w.in_drop)  # IWLAN transport = drop signature
        text = w.episode_file.read_text()
        self.assertIn("WLAN", text)
        self.assertIn("IWLAN", text)

    def test_telemetry_failure_is_not_recovery(self):
        """A-053/A-181: a dumpsys failure (None) mid-episode must NOT
        stamp RECOVERED or reset the episode — the state is unknown."""
        w = self._watch()
        self._poll(w, _DROP_REG)
        self.assertTrue(w.in_drop)
        path = w.episode_file
        self._poll(w, None)  # telemetry failure
        self.assertTrue(w.in_drop)
        self.assertNotIn("RECOVERED", path.read_text())
        # Recovery only on an explicit non-drop state.
        self._poll(w, _GOOD_REG)
        self._poll(w, _GOOD_REG)
        self.assertFalse(w.in_drop)
        self.assertIn("=== RECOVERED", path.read_text())

    def test_data_only_outage_detected(self):
        """A-085: data_state OUT_OF_SERVICE with voice IN_SERVICE is a
        drop episode."""
        w = self._watch()
        reg = {"service_state": "IN_SERVICE", "data_state": "OUT_OF_SERVICE",
               "network_type": "LTE", "operator": "ROGERS", "roaming": False}
        self._poll(w, reg)
        self.assertTrue(w.in_drop)

    def test_service_emergency_detected(self):
        """A-108: SERVICE_EMERGENCY opens a drop episode."""
        w = self._watch()
        reg = {"service_state": "SERVICE_EMERGENCY",
               "data_state": "SERVICE_EMERGENCY",
               "network_type": "Unknown", "operator": None, "roaming": None}
        self._poll(w, reg)
        self.assertTrue(w.in_drop)

    def test_iwlan_transport_detected(self):
        """A-176: an IWLAN/VoWiFi transport with both states IN_SERVICE
        triggers the episode (the field drop signature)."""
        w = self._watch()
        reg = {"service_state": "IN_SERVICE", "data_state": "IN_SERVICE",
               "network_type": "IWLAN", "operator": "ROGERS", "roaming": False}
        self._poll(w, reg)
        self.assertTrue(w.in_drop)

    def test_long_episode_stays_one_file(self):
        """A-059/A-187: refresh snapshots append to the SAME episode
        file — no orphaned per-gap files, one recovery marker."""
        server.DROP_SNAP_GAP = 1
        w = self._watch()
        # Three consecutive drop polls spanning the snap gap (monotonic
        # 1000 -> 1001.5 -> 1003: two refreshes, one file).
        fake = _SeqTime([1000.0, 1001.5, 1003.0, 1004.0, 1005.0])
        for _ in range(3):
            self._poll(w, _DROP_REG, fake_time=fake)
        path = w.episode_file
        self._poll(w, _GOOD_REG, fake_time=fake)
        self._poll(w, _GOOD_REG, fake_time=fake)
        files = self._episode_files()
        self.assertEqual(len(files), 1)
        text = path.read_text()
        self.assertIn("snapshot refresh", text)
        self.assertEqual(text.count("=== RECOVERED"), 1)

    def test_same_second_episodes_get_unique_files(self):
        """A-128: two episodes in the same wall second must not share a
        file (unique per-process episode id in the name)."""
        w = self._watch()
        self._poll(w, _DROP_REG)
        path1 = w.episode_file
        self._poll(w, _GOOD_REG)
        self._poll(w, _GOOD_REG)
        self._poll(w, _DROP_REG)
        path2 = w.episode_file
        self._poll(w, _GOOD_REG)
        self._poll(w, _GOOD_REG)
        self.assertEqual(len(self._episode_files()), 2)
        self.assertNotEqual(path1.name, path2.name)

    def test_disable_mid_episode_closes_with_duration(self):
        """A-093: turning drop logging off during an episode writes a
        duration-bearing closure marker instead of discarding it."""
        w = self._watch()
        self._poll(w, _DROP_REG)
        path = w.episode_file
        server.SETTINGS["drop_log"] = False
        with mock.patch.object(server, '_drop_state', return_value=_DROP_REG):
            server._drop_log_poll(w)
        self.assertFalse(w.in_drop)
        text = path.read_text()
        self.assertIn("=== EPISODE CLOSED", text)
        self.assertIn("(duration", text)

    def test_recovery_append_failure_keeps_episode(self):
        """A-115: the RECOVERED marker is written BEFORE episode state
        is cleared — a failed append leaves the episode open for retry
        instead of losing the recovery boundary."""
        w = self._watch()
        self._poll(w, _DROP_REG)
        path = w.episode_file

        def failing_open(*a, **k):
            raise OSError("disk full")

        with mock.patch.object(server, '_drop_state', return_value=_GOOD_REG), \
             mock.patch('server.open', side_effect=failing_open), \
             mock.patch.object(server, '_drop_snapshot_text',
                               side_effect=self._snapshot_stub):
            with self.assertRaises(OSError):
                server._drop_log_poll(w)  # recovery poll 1 (no write yet)
                server._drop_log_poll(w)  # recovery poll 2 (append fails)
        # Episode state survives the failed append.
        self.assertTrue(w.in_drop)
        self.assertEqual(w.episode_file, path)
        self.assertNotIn("RECOVERED", path.read_text())
        # Next poll retries and succeeds.
        self._poll(w, _GOOD_REG)
        self.assertFalse(w.in_drop)
        self.assertIn("=== RECOVERED", path.read_text())

    def test_blip_requires_two_confirmed_polls(self):
        """A-199: a single IN_SERVICE blip between drops must not close
        the episode; recovery needs DROP_RECOVERY_CONFIRM consecutive
        non-drop polls."""
        w = self._watch()
        self._poll(w, _DROP_REG)   # episode opens
        path = w.episode_file
        self._poll(w, _GOOD_REG)   # blip — recovery_polls=1
        self.assertTrue(w.in_drop)
        self.assertNotIn("RECOVERED", path.read_text())
        self._poll(w, _DROP_REG)   # still dropping — blip discarded
        self.assertTrue(w.in_drop)
        self._poll(w, _GOOD_REG)   # 1
        self._poll(w, _GOOD_REG)   # 2 -> recovered
        self.assertFalse(w.in_drop)
        self.assertIn("=== RECOVERED", path.read_text())

    def test_duration_uses_monotonic_not_wall_clock(self):
        """A-154: duration derives from time.monotonic, so a backward
        wall-clock step between detection and recovery cannot persist a
        negative duration."""
        w = self._watch()
        # Detection at mono 1000, recovery at mono 1120 -> 120s even
        # though wall-clock strftime stays fixed (a frozen/backward clock).
        fake = _SeqTime([1000.0, 1060.0, 1120.0])
        self._poll(w, _DROP_REG, fake_time=fake)   # detection
        path = w.episode_file
        self._poll(w, _GOOD_REG, fake_time=fake)   # poll 1
        self._poll(w, _GOOD_REG, fake_time=fake)   # poll 2 -> recovered
        self.assertFalse(w.in_drop)
        text = path.read_text()
        self.assertIn("(duration 120s)", text)
        self.assertNotIn("duration -", text)

    def test_reconcile_orphaned_episode_on_startup(self):
        """A-188: a restart mid-episode leaves an active marker; startup
        reconcile closes the orphan with an INTERRUPTED line."""
        os.makedirs(server.DROP_LOG_DIR, exist_ok=True)
        name = "drop_20260813_000000_1_0.txt"
        (server.DROP_LOG_DIR / name).write_text("=== DROP DETECTED ===\n")
        marker = server.DROP_LOG_DIR / server._ACTIVE_EPISODE_FILE
        marker.write_text(json.dumps({"file": name, "pid": 1}))
        server._reconcile_open_episode()
        text = (server.DROP_LOG_DIR / name).read_text()
        self.assertIn("=== EPISODE INTERRUPTED", text)
        self.assertFalse(marker.exists())

    def test_rotation_caps_episode_files(self):
        """A-043/A-207: the drop_log dir keeps only the newest
        DROP_LOG_MAX_FILES episode files, never growing unbounded."""
        os.makedirs(server.DROP_LOG_DIR, exist_ok=True)
        for i in range(server.DROP_LOG_MAX_FILES + 5):
            (server.DROP_LOG_DIR / "drop_20260813_{:04d}_1_{}.txt".format(
                i, i)).write_text("x")
        server._rotate_drop_log()
        self.assertEqual(len(self._episode_files()), server.DROP_LOG_MAX_FILES)

    def test_auto_recover_invoked_once_per_episode(self):
        """A-196: after the grace period a sustained drop invokes the
        modem reset once per episode (not once per poll)."""
        calls = []
        w = self._watch()
        fake = _SeqTime([1000.0, 1001.0])
        with mock.patch.object(server, '_modem_reset',
                               side_effect=lambda: (calls.append(1),
                                                    {"ok": True})), \
             mock.patch.object(server, 'AUTO_RECOVER_GRACE', 0), \
             mock.patch.object(server, '_AUTO_RECOVER_TIMES', []), \
             mock.patch.object(server.threading, 'Thread', _SyncThread):
            self._poll(w, _DROP_REG, fake_time=fake)   # opens episode, grace 0
            self._poll(w, _DROP_REG, fake_time=fake)   # still dropping
        self.assertEqual(len(calls), 1)  # once per episode, not per poll
        self.assertTrue(w.auto_recover_attempted)

    def test_auto_recover_rate_limited(self):
        """A-196: no more than AUTO_RECOVER_MAX_PER_HOUR auto-recoveries
        within the window."""
        calls = []
        w = self._watch()
        fake = _SeqTime([1000.0])
        with mock.patch.object(server, '_modem_reset',
                               side_effect=lambda: (calls.append(1),
                                                    {"ok": True})), \
             mock.patch.object(server, 'AUTO_RECOVER_GRACE', 0), \
             mock.patch.object(server, 'AUTO_RECOVER_MAX_PER_HOUR', 1), \
             mock.patch.object(server, '_AUTO_RECOVER_TIMES', [990.0]), \
             mock.patch.object(server.threading, 'Thread', _SyncThread):
            self._poll(w, _DROP_REG, fake_time=fake)
        self.assertEqual(len(calls), 0)  # window already exhausted


class TestDropLogConcurrency(unittest.TestCase):
    """A-116: the drop-log toggle mutation + save is one critical
    section — concurrent toggles can never report a state that was not
    the one persisted."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_file = server.SETTINGS_FILE
        self._orig_settings = server.SETTINGS
        server.SETTINGS_FILE = Path(self._tmp.name) / "settings.json"
        server.SETTINGS = {"bind": "127.0.0.1", "token": None,
                           "drop_log": False}

    def tearDown(self):
        server.SETTINGS_FILE = self._orig_file
        server.SETTINGS = self._orig_settings
        self._tmp.cleanup()

    def test_concurrent_toggles_serialize_mutation_and_save(self):
        results = []
        errors = []

        def toggle(enabled):
            try:
                res = server.BandHandler.update_drop_log(
                    None, {"enabled": enabled})
                results.append((enabled, res["enabled"]))
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=toggle, args=(v,))
                   for v in (True, False) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        # Every response reported exactly the value its own request set.
        for requested, reported in results:
            self.assertEqual(requested, reported)
        # Memory and disk agree on the final state.
        self.assertEqual(
            server.SETTINGS["drop_log"],
            json.loads(server.SETTINGS_FILE.read_text())["drop_log"])


class TestTransportErrorStatus(unittest.TestCase):
    """A-02: signal/registration/health transport failures must be
    surfaced as non-200 so the UI's r.ok check shows the error instead of
    rendering healthy/neutral states."""

    def test_signal_dumpsys_failure_is_503(self):
        with mock.patch.object(server, '_run_dumpsys',
                               side_effect=RuntimeError('boom')):
            status, res = run_api('/api/signal?action=signal')
        self.assertEqual(status, 503)
        self.assertIn('error', res)

    def test_signal_no_data_is_503(self):
        with mock.patch.object(server, '_run_dumpsys', return_value=''):
            status, _ = run_api('/api/signal?action=signal')
        self.assertEqual(status, 503)

    def test_registration_failure_is_503(self):
        with mock.patch.object(server, '_run_dumpsys',
                               side_effect=RuntimeError('boom')):
            status, res = run_api('/api/registration?action=registration')
        self.assertEqual(status, 503)
        self.assertIn('error', res)

    def test_health_error_status_is_503(self):
        with mock.patch.object(server, '_run_qmi',
                               return_value=(None, '')), \
             mock.patch.object(server, 'read_bands',
                               side_effect=RuntimeError('no diag')):
            status, res = run_api('/api/health?action=health')
        self.assertEqual(status, 503)
        self.assertEqual(res['status'], 'error')

    def test_health_degraded_stays_200(self):
        # A transport that answers but has no bands is degraded, not an
        # error — the frontend must still render it. QMI output must
        # carry the success result TLV to be trusted (A-14).
        with mock.patch.object(server, '_run_qmi',
                               return_value=(0, '  result: status=0 SUCCESS\n'
                                              '    LTE bands: (none)\n'
                                              '    NR5G SA bands: (none)\n')):
            status, res = run_api('/api/health?action=health')
        self.assertEqual(status, 200)
        self.assertEqual(res['status'], 'degraded')

    def test_camping_read_failure_is_503(self):
        # A-142: a read failure must not look like an empty sample list.
        with mock.patch.object(server.BandHandler, 'read_band_camping',
                               return_value={"ok": False,
                                             "error": "read failed"}):
            status, res = run_api('/api/band-camping?action=band-camping')
        self.assertEqual(status, 503)
        self.assertFalse(res['ok'])


class TestRegistrationLabels(unittest.TestCase):
    """A-47: modern network-type labels with punctuation (LTE_CA, HSPA+)
    must parse instead of being dropped to null."""

    def test_lte_ca_label_parsed(self):
        svc = "{mVoiceRegState=0(IN_SERVICE), mDataRegState=0(IN_SERVICE), " \
              "getRilDataRadioTechnology=19(LTE_CA)}"
        reg = server._parse_service_state(svc, svc)
        self.assertEqual(reg['network_type'], 'LTE_CA')

    def test_hspa_plus_label_parsed(self):
        svc = "{mVoiceRegState=0(IN_SERVICE), mDataRegState=0(IN_SERVICE), " \
              "getRilVoiceRadioTechnology=15(HSPA+)}"
        reg = server._parse_service_state(svc, svc)
        self.assertEqual(reg['network_type'], 'HSPA+')

    def test_end_to_end_dump(self):
        dump = "mServiceState={mVoiceRegState=0(IN_SERVICE), " \
               "mDataRegState=0(IN_SERVICE), " \
               "getRilDataRadioTechnology=19(LTE_CA), " \
               "mOperatorAlphaLong=ROGERS}\n"
        reg = server._parse_registration(dump)
        self.assertEqual(reg['network_type'], 'LTE_CA')
        self.assertEqual(reg['service_state'], 'IN_SERVICE')


class TestDualSimPreference(unittest.TestCase):
    """A-50: with separate Phone Id blocks, the IN_SERVICE subscription
    (registration and signal) must win over the first, out-of-service
    record."""

    def test_registration_prefers_in_service_phone(self):
        dump = (" Phone Id=0\n"
                " mServiceState={mVoiceRegState=1(OUT_OF_SERVICE), "
                "mDataRegState=1(OUT_OF_SERVICE), mOperatorAlphaLong=ROGERS}\n"
                " Phone Id=1\n"
                " mServiceState={mVoiceRegState=0(IN_SERVICE), "
                "mDataRegState=0(IN_SERVICE), mOperatorAlphaLong=BELL}\n")
        reg = server._parse_registration(dump)
        self.assertEqual(reg['service_state'], 'IN_SERVICE')
        self.assertEqual(reg['operator'], 'BELL')

    def test_single_phone_keeps_first_record(self):
        dump = ("mServiceState={mVoiceRegState=1(OUT_OF_SERVICE), "
                "mDataRegState=1(OUT_OF_SERVICE)}\n")
        reg = server._parse_registration(dump)
        self.assertEqual(reg['service_state'], 'OUT_OF_SERVICE')

    def test_signal_prefers_in_service_phone(self):
        sigdump = (" Phone Id=0\n"
                   " mServiceState={mVoiceRegState=1(OUT_OF_SERVICE)}\n"
                   " mSignalStrength=SignalStrength:{primary=CellSignalStrengthLte, "
                   "mLte=CellSignalStrengthLte: rssi=-111 rsrp=-125 rsrq=-15 level=1}\n"
                   " Phone Id=1\n"
                   " mServiceState={mVoiceRegState=0(IN_SERVICE)}\n"
                   " mSignalStrength=SignalStrength:{primary=CellSignalStrengthLte, "
                   "mLte=CellSignalStrengthLte: rssi=-99 rsrp=-85 rsrq=-8 level=4}\n")
        sig = server._parse_signal_strength(sigdump)
        self.assertEqual(sig['rsrp_dbm'], -85)
        self.assertEqual(sig['level'], 4)


class TestCarrierDetection(unittest.TestCase):
    """A-72: mixed-SIM operator numerics must not trigger Rogers-specific
    band exclusions; a single Rogers slot still does."""

    def test_single_rogers_slot_is_rogers(self):
        self.assertEqual(server.carrier_for_mccmnc('302720'), 'rogers')
        self.assertEqual(server.carrier_for_mccmnc('302720,'), 'rogers')

    def test_mixed_sims_are_other(self):
        self.assertEqual(server.carrier_for_mccmnc('302720,310260'), 'other')
        self.assertEqual(server.carrier_for_mccmnc('310260,302720'), 'other')

    def test_other_and_empty_are_other(self):
        self.assertEqual(server.carrier_for_mccmnc('310260'), 'other')
        self.assertEqual(server.carrier_for_mccmnc(''), 'other')
        self.assertEqual(server.carrier_for_mccmnc(None), 'other')


class TestSignalValidation(unittest.TestCase):
    """A-133/A-140: sentinel levels and non-sentinel junk metrics must not
    reach the UI as live measurements."""

    def test_sentinel_level_normalized_to_none(self):
        obj = ("SignalStrength:{primary=CellSignalStrengthLte, "
               "mLte=CellSignalStrengthLte: rssi=-99 rsrp=-95 rsrq=-10 "
               "level=2147483647}")
        sig = server._parse_signal_object(obj)
        self.assertEqual(sig['rsrp_dbm'], -95)
        self.assertIsNone(sig['level'])

    def test_out_of_domain_level_normalized_to_none(self):
        obj = ("SignalStrength:{primary=CellSignalStrengthLte, "
               "mLte=CellSignalStrengthLte: rssi=-99 rsrp=-95 rsrq=-10 level=9}")
        self.assertIsNone(server._parse_signal_object(obj)['level'])

    def test_junk_rsrp_rsrq_rejected(self):
        obj = ("SignalStrength:{primary=CellSignalStrengthLte, "
               "mLte=CellSignalStrengthLte: rssi=0 rsrp=0 rsrq=50 level=9}")
        self.assertIsNone(server._parse_signal_object(obj))

    def test_legacy_99_rejected(self):
        self.assertFalse(server._valid_signal(99))
        self.assertFalse(server._valid_signal(2147483647))
        self.assertTrue(server._valid_signal(-95))


class TestNrCsiFallback(unittest.TestCase):
    """A-99: valid csiRsrp/csiRsrq must be used when the SS measurements
    are sentinels/unavailable."""

    def test_csi_used_when_ss_sentinel(self):
        obj = ("SignalStrength:{primary=CellSignalStrengthNr, "
               "mNr=CellSignalStrengthNr:{ csiRsrp = -95 csiRsrq = -10 "
               "ssRsrp = 2147483647 ssRsrq = 2147483647 level = 3 }}")
        sig = server._parse_signal_object(obj)
        self.assertEqual(sig['tech'], 'NR')
        self.assertEqual(sig['rsrp_dbm'], -95)
        self.assertEqual(sig['rsrq_db'], -10)

    def test_ss_preferred_when_valid(self):
        obj = ("SignalStrength:{primary=CellSignalStrengthNr, "
               "mNr=CellSignalStrengthNr:{ csiRsrp = -95 csiRsrq = -10 "
               "ssRsrp = -90 ssRsrq = -8 level = 3 }}")
        sig = server._parse_signal_object(obj)
        self.assertEqual(sig['rsrp_dbm'], -90)
        self.assertEqual(sig['rsrq_db'], -8)


class TestRadioRegStateLegacy(unittest.TestCase):
    """A-98: modem-reset verification must accept the bare legacy numeric
    registration form, not only the parenthesized object form."""

    def test_bare_numeric_state(self):
        with mock.patch.object(server, '_run_dumpsys',
                               return_value="mServiceState={mVoiceRegState=3}"):
            self.assertEqual(server._radio_reg_state(), 'POWER_OFF')

    def test_parenthesized_state(self):
        with mock.patch.object(server, '_run_dumpsys',
                               return_value="mVoiceRegState=3(POWER_OFF)"):
            self.assertEqual(server._radio_reg_state(), 'POWER_OFF')


class TestMdSessionOwner(unittest.TestCase):
    """A-143: the ioctl's in-place buffer mutation must be unpacked, not
    treated as a return value."""

    def test_owner_pid_extracted(self):
        import struct
        def fake_ioctl(fd, req, buf):
            struct.pack_into('<IIiI', buf, 0, 1, 0, 4242, 0)
            return 0
        with mock.patch('fcntl.ioctl', side_effect=fake_ioctl), \
             mock.patch('os.open', return_value=3), \
             mock.patch('os.close'):
            self.assertEqual(server._query_md_pid('/dev/diag'), 4242)


class TestModemResetSerialized(unittest.TestCase):
    """A-51: a second concurrent modem-reset must be refused, not raced."""

    def test_concurrent_reset_refused(self):
        server._MODEM_RESET_LOCK.acquire()
        try:
            with mock.patch.object(server, '_cmd_available',
                                   return_value=False):
                res = server.BandHandler.modem_reset(None)
        finally:
            server._MODEM_RESET_LOCK.release()
        self.assertFalse(res['ok'])
        self.assertIn('already in progress', res['error'])

    def test_reset_runs_after_lock_released(self):
        with mock.patch.object(server, '_cmd_available',
                               return_value=False):
            res = server.BandHandler.modem_reset(None)
        self.assertFalse(res['ok'])
        self.assertNotIn('already in progress', res['error'])


class TestPollGuard(unittest.TestCase):
    """A-34: a concurrent poll reuses the last successful result instead
    of stacking a second dumpsys; errors are never cached."""

    def setUp(self):
        self._orig_sig = server._SIGNAL_READ['last']
        self._orig_reg = server._REG_READ['last']
        server._SIGNAL_READ['last'] = None
        server._REG_READ['last'] = None

    def tearDown(self):
        server._SIGNAL_READ['last'] = self._orig_sig
        server._REG_READ['last'] = self._orig_reg

    def test_busy_returns_cached_result(self):
        server._SIGNAL_READ['last'] = {"rsrp_dbm": -95, "rsrq_db": -10,
                                       "level": 3, "tech": "LTE",
                                       "timestamp": 1}
        server._SIGNAL_READ['lock'].acquire()
        try:
            with mock.patch.object(server, '_run_dumpsys',
                                   side_effect=AssertionError('no dumpsys')):
                res = server.BandHandler.read_signal(None)
        finally:
            server._SIGNAL_READ['lock'].release()
        self.assertEqual(res['rsrp_dbm'], -95)

    def test_success_cached_error_not(self):
        calls = {'n': 0}
        def fn():
            calls['n'] += 1
            if calls['n'] == 1:
                return {"error": "boom"}
            return {"rsrp_dbm": -80, "rsrq_db": -7, "level": 4,
                    "tech": "LTE", "timestamp": 2}
        first = server._poll_once(server._SIGNAL_READ, fn)
        second = server._poll_once(server._SIGNAL_READ, fn)
        self.assertIn('error', first)
        self.assertEqual(server._SIGNAL_READ['last'], second)
        self.assertNotIn('error', second)


class TestBareRoutes(unittest.TestCase):
    """A-65: documented bare routes (no ?action=) must dispatch, and
    unknown actions must be a real client error (400), not a 200."""

    def test_bare_read_route(self):
        status, res = run_api('/api/read', 'GET')
        self.assertEqual(status, 200)
        self.assertIn('source', res)

    def test_bare_defaults_route(self):
        status, res = run_api('/api/defaults', 'GET')
        self.assertEqual(status, 200)
        self.assertEqual(res['carrier'], 'other')

    def test_bare_settings_route(self):
        status, res = run_api('/api/settings', 'GET')
        self.assertEqual(status, 200)
        self.assertTrue(res['ok'])

    def test_bare_camping_route_with_limit(self):
        status, res = run_api('/api/band-camping?limit=5', 'GET')
        self.assertEqual(status, 200)
        self.assertIn('samples', res)


class TestBandCampingRead(unittest.TestCase):
    """A-74/A-200: the camping endpoint serializes (log path is a string,
    never a pathlib.Path); A-136/A-139: sentinel EARFCN and invalid bands
    are not rendered as camped cells."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = server.BAND_CAMPING_LOG
        server.BAND_CAMPING_LOG = Path(self._tmp.name) / 'band_camping.log'

    def tearDown(self):
        server.BAND_CAMPING_LOG = self._orig
        self._tmp.cleanup()

    def test_endpoint_returns_samples_and_str_path(self):
        server.BAND_CAMPING_LOG.write_text(
            "1750000000000,2050,4\n1750000005000,2050,\n")
        status, res = run_api('/api/band-camping?action=band-camping&limit=5')
        self.assertEqual(status, 200)
        self.assertTrue(res['ok'])
        self.assertIsInstance(res['log'], str)
        self.assertEqual(res['samples'],
                         [{'timestamp': 1750000000000, 'earfcn': 2050,
                           'band': 4},
                          {'timestamp': 1750000005000, 'earfcn': 2050,
                           'band': None}])

    def test_absent_log_is_empty_not_error(self):
        status, res = run_api('/api/band-camping?action=band-camping')
        self.assertEqual(status, 200)
        self.assertEqual(res['samples'], [])
        self.assertIsInstance(res['log'], str)

    def test_earfcn_sentinel_not_a_cell(self):
        self.assertEqual(
            server._parse_band_camping(
                "mCellIdentity=CellIdentityLte:{ mEarfcn=2147483647, mBands=[4] }"),
            (None, None, None))

    def test_earfcn_out_of_domain_not_a_cell(self):
        self.assertEqual(
            server._parse_band_camping(
                "mCellIdentity=CellIdentityLte:{ mEarfcn=70000, mBands=[4] }"),
            (None, None, None))

    def test_invalid_band_identities_rejected(self):
        self.assertEqual(
            server._parse_band_camping(
                "mCellIdentity=CellIdentityLte:{ mEarfcn=2050, mBands=[0] }"),
            (2050, None, "LTE"))
        self.assertEqual(
            server._parse_band_camping(
                "mCellIdentity=CellIdentityLte:{ mEarfcn=2050, mBands=[999] }"),
            (2050, None, "LTE"))

    def test_valid_cell_parsed(self):
        self.assertEqual(
            server._parse_band_camping(
                "mCellIdentity=CellIdentityLte:{ mEarfcn=2050, mBands=[4] }"),
            (2050, 4, "LTE"))


class TestCampingLogBounded(unittest.TestCase):
    """A-28: reads use only the log tail, and the sampler trims the log to
    a bounded size."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_log = server.BAND_CAMPING_LOG
        server.BAND_CAMPING_LOG = Path(self._tmp.name) / 'camp.log'

    def tearDown(self):
        server.BAND_CAMPING_LOG = self._orig_log
        self._tmp.cleanup()

    def test_read_tail_returns_last_lines(self):
        log = Path(self._tmp.name) / 'camp.log'
        lines = ["{},{},{}".format(1750000000000 + i, 2000 + i,
                                   (i % 79) + 1) for i in range(5000)]
        log.write_text("\n".join(lines) + "\n")
        tail = server._read_tail(log, 50)
        self.assertEqual(len(tail), 50)
        self.assertEqual(tail[-1], lines[-1])
        self.assertEqual(tail[0], lines[-50])

    def test_read_tail_small_file(self):
        log = Path(self._tmp.name) / 'camp.log'
        log.write_text("a,b,c\n")
        self.assertEqual(server._read_tail(log, 5), ["a,b,c"])

    def test_trim_caps_line_count(self):
        log = Path(self._tmp.name) / 'big.log'
        log.write_text("\n".join("line{}".format(i) for i in range(10000))
                       + "\n")
        server._trim_band_camping_log(log, max_lines=10)
        self.assertEqual(len(log.read_text().splitlines()), 10)

    def test_sample_appends_csv_line(self):
        with mock.patch.object(server, '_run_dumpsys',
                               return_value="mCellIdentity="
                               "CellIdentityLte:{ mEarfcn=2050, mBands=[4] }"):
            self.assertTrue(server._band_camping_sample())
        line = server.BAND_CAMPING_LOG.read_text().strip()
        parts = line.split(',')
        self.assertEqual(parts[1:], ['2050', '4', 'LTE'])
        self.assertTrue(parts[0].isdigit())


class TestCampingToggle(unittest.TestCase):
    """A-120/A-201: the sampler is gated on a persisted settings toggle
    with a POST endpoint (like the drop logger), and GET reports it."""

    def setUp(self):
        self._orig = server.SETTINGS.get("band_camping")
        server.SETTINGS["band_camping"] = True

    def tearDown(self):
        server.SETTINGS["band_camping"] = self._orig

    def test_post_toggles_and_persists(self):
        body = json.dumps({'enabled': False}).encode()
        with mock.patch.object(server, '_save_settings') as save:
            status, payload = run_api('/api/band-camping?action=band-camping',
                                      'POST',
                                      headers=FakeHeaders(
                                          {'Content-Length': str(len(body))}),
                                      body=body)
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        self.assertFalse(payload['enabled'])
        self.assertFalse(server.SETTINGS['band_camping'])
        save.assert_called_once()

    def test_post_rejects_non_bool(self):
        body = json.dumps({'enabled': 'yes'}).encode()
        with mock.patch.object(server, '_save_settings'):
            status, payload = run_api(
                '/api/band-camping?action=band-camping', 'POST',
                headers=FakeHeaders({'Content-Length': str(len(body))}),
                body=body)
        self.assertEqual(status, 200)
        self.assertFalse(payload['ok'])

    def test_get_reports_enabled(self):
        status, payload = run_api('/api/band-camping?action=band-camping')
        self.assertEqual(status, 200)
        self.assertTrue(payload['enabled'])


class TestExport(unittest.TestCase):
    """A-82/A-135: exports are atomic (no partial file on interruption)
    and pruned to the newest EXPORT_KEEP files."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = server.EXPORT_DIR
        server.EXPORT_DIR = self._tmp.name
        self.h = server.BandHandler.__new__(server.BandHandler)

    def tearDown(self):
        server.EXPORT_DIR = self._orig
        self._tmp.cleanup()

    def test_interrupted_write_leaves_no_partial_export(self):
        before = set(os.listdir(server.EXPORT_DIR))
        with mock.patch('json.dump',
                        side_effect=RuntimeError('interrupted')):
            res = self.h.export_config({"lte": [1]})
        self.assertFalse(res['ok'])
        self.assertEqual(set(os.listdir(server.EXPORT_DIR)), before)
        self.assertEqual([f for f in os.listdir(server.EXPORT_DIR)
                          if f.endswith('.tmp')], [])

    def test_exports_pruned_to_newest(self):
        for i in range(12):
            p = os.path.join(server.EXPORT_DIR,
                             "bandctl-export-20260813-{:02d}00.json".format(i))
            with open(p, 'w') as f:
                json.dump({"lte": [1]}, f)
        res = self.h.export_config({"lte": ["1"], "nr": []})
        self.assertTrue(res['ok'])
        exports = sorted(f for f in os.listdir(server.EXPORT_DIR)
                         if f.startswith('bandctl-export-'))
        self.assertEqual(len(exports), server.EXPORT_KEEP)
        self.assertEqual(exports[-1], os.path.basename(res['path']))

    def test_export_writes_valid_json(self):
        res = self.h.export_config({"lte": ["1"], "nr": ["77"]})
        self.assertTrue(res['ok'])
        self.assertTrue(os.path.exists(res['path']))
        # A-155: the export validates like /api/write, so numeric strings
        # are normalized to ints in the persisted file.
        self.assertEqual(json.load(open(res['path'])),
                         {"lte": [1], "nr": [77]})


class TestSeedRepair(unittest.TestCase):
    """A-134: an interrupted install-time seed (truncated bands.json) is
    repaired at startup; a valid user config is never touched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = server.CONFIG_FILE
        server.CONFIG_FILE = Path(self._tmp.name) / 'bands.json'

    def tearDown(self):
        server.CONFIG_FILE = self._orig
        self._tmp.cleanup()

    def test_truncated_file_repaired(self):
        server.CONFIG_FILE.write_text('{"lte":')
        with mock.patch.object(server, '_get_prop', return_value='302720'):
            server.seed_config_if_absent()
        data = json.loads(server.CONFIG_FILE.read_text())
        self.assertIn('lte', data)
        self.assertEqual(data['lte'], server._ROGERS_BANDS['lte'])

    def test_valid_config_kept(self):
        server.CONFIG_FILE.write_text(json.dumps({"lte": ["1"], "nr": []}))
        with mock.patch.object(server, '_get_prop', return_value='302720'):
            server.seed_config_if_absent()
        self.assertEqual(json.loads(server.CONFIG_FILE.read_text()),
                         {"lte": ["1"], "nr": []})

    def test_non_rogers_not_seeded(self):
        server.CONFIG_FILE.write_text('{"lte":')
        with mock.patch.object(server, '_get_prop',
                               return_value='310260'):
            server.seed_config_if_absent()
        self.assertEqual(server.CONFIG_FILE.read_text(), '{"lte":')


class TestBootApplyNoSeed(unittest.TestCase):
    """A-216: boot apply is read-only w.r.t. the config — it must not
    seed/recreate bands.json, so deleting the file stays a skip."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = server.CONFIG_FILE
        server.CONFIG_FILE = Path(self._tmp.name) / 'bands.json'

    def tearDown(self):
        server.CONFIG_FILE = self._orig
        self._tmp.cleanup()

    def test_boot_apply_does_not_seed(self):
        with mock.patch.object(server, 'seed_config_if_absent') as seed:
            res = server.BandHandler.boot_apply(None)
        self.assertEqual(res, {"ok": True, "skipped": True})
        seed.assert_not_called()


class TestHttpSemantics(unittest.TestCase):
    """A-60/A-168/A-169: bounded connection timeout, no version
    fingerprinting, HTTP/1.1 keep-alive, and explicit chunked rejection."""

    def test_connection_timeout_set(self):
        self.assertIsNotNone(server.BandHandler.timeout)
        self.assertGreater(server.BandHandler.timeout, 0)

    def test_version_string_does_not_fingerprint_python(self):
        h = make_handler()
        self.assertEqual(h.version_string(), 'Bandctl/2.6')
        self.assertNotIn('Python', h.version_string())

    def test_protocol_version_http11(self):
        self.assertEqual(server.BandHandler.protocol_version, 'HTTP/1.1')

    def test_chunked_body_rejected_501(self):
        status, res = run_api(
            '/api/write?action=write', 'POST',
            headers=FakeHeaders({'Transfer-Encoding': 'chunked'}),
            body=b'4\r\ntest\r\n0\r\n\r\n')
        self.assertEqual(status, 501)
        self.assertFalse(res['ok'])
        self.assertIn('chunked', res['error'])

    def test_unread_body_closes_connection(self):
        # Error paths that skip the body (chunked, 401, unknown action)
        # must disable keep-alive so leftover bytes are not misparsed as a
        # pipelined request.
        def _run(path, command, headers, body=b''):
            h = make_handler(path=path, command=command, headers=headers,
                             body=body)
            h.handle_api()
            return h

        h = _run('/api/write?action=write', 'POST',
                 {'Transfer-Encoding': 'chunked'})
        self.assertTrue(h.close_connection)

        h = _run('/api/nope?action=nope', 'POST',
                 {'Content-Length': '5'}, body=b'hello')
        self.assertTrue(h.close_connection)

        h = _run('/api/nope?action=nope', 'GET', {})
        self.assertFalse(getattr(h, 'close_connection', False))

        h = _run('/api/write?action=write', 'POST',
                 {'Content-Length': str(server.MAX_BODY_BYTES + 1)},
                 body=b'')
        self.assertTrue(h.close_connection)

    def test_401_with_body_closes_connection(self):
        server.SETTINGS = {"bind": "0.0.0.0", "token": "sekret"}
        try:
            body = b'{"lan_enabled": true}'
            h = make_handler('/api/settings?action=settings', 'POST',
                             headers={'Content-Length': str(len(body))},
                             addr=('192.168.1.42', 9999), body=body)
            h.handle_api()
            self.assertEqual(
                int(h.wfile.getvalue().split(b'\r\n', 1)[0].split()[1]), 401)
            self.assertTrue(h.close_connection)
        finally:
            server.SETTINGS = {"bind": "127.0.0.1", "token": None}


class TestQmiNrSort(unittest.TestCase):
    """A-221: the NR union is sorted numerically, matching the LTE order."""

    def test_nr_sorted_numerically(self):
        out = ("    LTE bands: 1 2\n"
               "    NR5G SA bands: 1 20\n"
               "    NR5G NSA bands: 2 3\n")
        parsed = server._parse_qmi_get(out)
        self.assertEqual(parsed['nr'], ['1', '2', '3', '20'])
        self.assertEqual(parsed['lte'], ['1', '2'])


class ConfigFileCase(unittest.TestCase):
    """Point CONFIG_FILE (and the write revision) at a temp dir so read/
    write/boot-apply tests are deterministic and never touch the checkout."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = server.CONFIG_FILE
        self._orig_rev = server._CONFIG_REV
        server.CONFIG_FILE = Path(self._tmp.name) / "bands.json"
        server._CONFIG_REV = 0

    def tearDown(self):
        server.CONFIG_FILE = self._orig
        server._CONFIG_REV = self._orig_rev
        self._tmp.cleanup()


class TestQmiParse(unittest.TestCase):
    """A-122/A-145: QMI output parsing — base+extension union and the
    LTE/NR catalog filter."""

    def test_unions_base_and_extension_masks(self):
        out = ("  result: status=0 error=0x0000 SUCCESS\n"
               "    LTE bands: 1 3 12 \n"
               "    NR5G SA bands: 1 77 \n"
               "    LTE bands: 66 \n"
               "    NR5G SA bands: 78 \n"
               "    NR5G NSA bands: 79 \n")
        parsed = server._parse_qmi_get(out)
        self.assertEqual(parsed["lte"], ["1", "3", "12", "66"])
        self.assertEqual(parsed["nr"], ["1", "77", "78", "79"])

    def test_filters_out_of_catalog_nr_bands(self):
        """A-145: an NR band above the catalog (e.g. 257) is dropped so
        the reported set can always be displayed and re-applied."""
        out = "    LTE bands: 1 3 12 \n    NR5G SA bands: 257 77 \n"
        parsed = server._parse_qmi_get(out)
        self.assertEqual(parsed["lte"], ["1", "3", "12"])
        self.assertEqual(parsed["nr"], ["77"])

    def test_filters_out_of_catalog_lte_bands(self):
        out = "    LTE bands: 1 77 \n"  # 77 is an NR-only band
        parsed = server._parse_qmi_get(out)
        self.assertEqual(parsed["lte"], ["1"])

    def test_no_lte_line_returns_none(self):
        self.assertIsNone(server._parse_qmi_get("    NR5G SA bands: 77 \n"))


class TestQmiRead(ConfigFileCase):
    """A-14/A-44: failed or partial QMI reads must never be authoritative."""

    def setUp(self):
        super().setUp()
        self.h = server.BandHandler.__new__(server.BandHandler)

    def test_successful_qmi_read(self):
        with mock.patch.object(server, '_run_qmi',
                               return_value=(0, "  result: status=0 SUCCESS\n"
                                               "    LTE bands: 1 3 \n"
                                               "    NR5G SA bands: 77 \n")):
            res = self.h._read_qmi_config()
        self.assertTrue(res["ok"])
        self.assertEqual(res["source"], "qmi")
        self.assertEqual(res["lte"], ["1", "3"])
        self.assertEqual(res["nr"], ["77"])
        self.assertIn("rev", res)

    def test_nonzero_exit_not_authoritative(self):
        with mock.patch.object(server, '_run_qmi',
                               return_value=(1, "    LTE bands: 1 3 \n")):
            self.assertIsNone(self.h._read_qmi_config())

    def test_failure_status_not_authoritative(self):
        """A-14: a status=1 response that still contains mask TLVs must
        not be parsed as a valid read."""
        with mock.patch.object(server, '_run_qmi',
                               return_value=(0, "  result: status=1 error=0x0002 FAILURE\n"
                                               "    LTE bands: 1 3 \n")):
            self.assertIsNone(self.h._read_qmi_config())

    def test_read_config_falls_through_on_qmi_failure(self):
        with mock.patch.object(server, '_run_qmi',
                               return_value=(0, "    LTE bands: 1 3 \n")), \
             mock.patch.object(server, 'read_bands',
                               side_effect=OSError("no diag")):
            res = self.h.read_config()
        self.assertTrue(res["ok"])
        self.assertEqual(res["source"], "default")

    def test_health_ignores_qmi_failure_status(self):
        with mock.patch.object(server, '_run_qmi',
                               return_value=(0, "  result: status=1 FAILURE\n"
                                               "    LTE bands: 1 3 \n")), \
             mock.patch.object(server, 'read_bands',
                               side_effect=OSError("no diag")):
            res = self.h.modem_health()
        self.assertEqual(res["status"], "error")
        self.assertNotEqual(res["transport"], "qmi")
        self.assertIn("pid", res)


class TestQmiWrite(unittest.TestCase):
    """A-138/A-13: QMI apply certification — exit code, result TLV, and
    read-back verification."""

    def setUp(self):
        self.h = server.BandHandler.__new__(server.BandHandler)

    def test_nonzero_exit_with_success_output_rejected(self):
        """A-138: printing `result: status=0` then exiting nonzero must
        not certify a successful apply."""
        with mock.patch.object(server, '_run_qmi',
                               side_effect=[(7, "  result: status=0 SUCCESS\n"),
                                            (0, "  result: status=0 SUCCESS\n    LTE bands: 1 3 \n")]):
            ok = self.h._write_qmi_config([1, 3], [])
        self.assertFalse(ok)

    def test_failure_status_rejected(self):
        with mock.patch.object(server, '_run_qmi',
                               return_value=(0, "  result: status=1 FAILURE\n")):
            self.assertFalse(self.h._write_qmi_config([1], []))

    def test_readback_missing_requested_band_rejected(self):
        """A-13: a SET that silently drops a requested band (the helper's
        LTE mask cannot represent bands >64) must not be certified."""
        with mock.patch.object(server, '_run_qmi',
                               side_effect=[(0, "  result: status=0 SUCCESS\n"),
                                            (0, "  result: status=0 SUCCESS\n    LTE bands: 1 3 \n"),
                                            (0, "  result: status=0 SUCCESS\n    LTE bands: 1 3 \n")]), \
             mock.patch.object(server.time, 'sleep'):
            ok = self.h._write_qmi_config([1, 3, 66], [])
        self.assertFalse(ok)

    def test_readback_confirms_success(self):
        with mock.patch.object(server, '_run_qmi',
                               side_effect=[(0, "  result: status=0 SUCCESS\n"),
                                            (0, "  result: status=0 SUCCESS\n    LTE bands: 1 3 66 \n    NR5G SA bands: 77 \n")]):
            ok = self.h._write_qmi_config([1, 3, 66], [77])
        self.assertTrue(ok)


class TestWriteConfig(ConfigFileCase):
    """write_config: mirror-only-on-success (A-68), save failure reported
    (A-16), serialization + rev conflict (A-45/A-97)."""

    def setUp(self):
        super().setUp()
        self.h = server.BandHandler.__new__(server.BandHandler)

    def test_failed_apply_not_persisted(self):
        """A-68: a failed apply must not be promoted to boot-time source
        of truth."""
        with mock.patch.object(self.h, '_apply_bands',
                               return_value=(False, "diag", "diag write failed")):
            res = self.h.write_config({"lte": [1], "nr": []})
        self.assertFalse(res["ok"])
        self.assertFalse(server.CONFIG_FILE.exists())

    def test_save_failure_reported(self):
        """A-16: a persistence failure must not return ok:true."""
        with mock.patch.object(self.h, '_apply_bands',
                               return_value=(True, "qmi", None)), \
             mock.patch.object(self.h, '_save_config_file',
                               side_effect=OSError("disk full")):
            res = self.h.write_config({"lte": [1], "nr": []})
        self.assertFalse(res["ok"])
        self.assertIn("config save failed", res["error"])

    def test_success_persists_and_increments_rev(self):
        with mock.patch.object(self.h, '_apply_bands',
                               return_value=(True, "qmi", None)):
            res = self.h.write_config({"lte": [1, 3], "nr": [77]})
        self.assertTrue(res["ok"])
        self.assertEqual(res["rev"], 1)
        saved = json.loads(server.CONFIG_FILE.read_text())
        self.assertEqual(saved["lte"], [1, 3])
        self.assertEqual(saved["nr"], [77])

    def test_stale_rev_rejected_without_apply(self):
        """A-97: a write based on an older read is rejected when another
        client changed the config meanwhile."""
        server._CONFIG_REV = 5
        with mock.patch.object(self.h, '_apply_bands') as apply:
            res = self.h.write_config({"lte": [1], "nr": [], "rev": 4})
        self.assertFalse(res["ok"])
        self.assertIn("changed by another client", res["error"])
        apply.assert_not_called()


class TestReadConfigFile(ConfigFileCase):
    """_read_config_file validation (A-42), corruption reporting (A-149),
    and the diag empty-read fallthrough (A-15)."""

    def setUp(self):
        super().setUp()
        self.h = server.BandHandler.__new__(server.BandHandler)

    def test_valid_file_normalized(self):
        server.CONFIG_FILE.write_text(
            json.dumps({"lte": ["1", "1", "3"], "nr": ["77"]}))
        res = self.h._read_config_file()
        self.assertTrue(res["ok"])
        self.assertEqual(res["source"], "config_file")
        self.assertEqual(res["lte"], ["1", "3"])

    def test_out_of_catalog_band_reported(self):
        """A-42: an invalid persisted file is reported, not trusted as the
        live modem configuration."""
        server.CONFIG_FILE.write_text(json.dumps({"lte": ["77"], "nr": []}))
        res = self.h._read_config_file()
        self.assertFalse(res["ok"])
        self.assertIn("invalid config file", res["error"])

    def test_corrupt_file_reported_not_defaults(self):
        """A-149: malformed JSON must not silently become carrier
        defaults."""
        server.CONFIG_FILE.write_text("{truncated")
        res = self.h._read_config_file()
        self.assertFalse(res["ok"])
        self.assertIn("config file corrupt", res["error"])

    def test_missing_file_carrier_defaults(self):
        res = self.h._read_config_file()
        self.assertTrue(res["ok"])
        self.assertEqual(res["source"], "default")

    def test_diag_empty_read_falls_through(self):
        """A-15: a failed/empty diag read must not be labelled as an
        authoritative diag config."""
        server.CONFIG_FILE.write_text(json.dumps({"lte": [1], "nr": []}))
        with mock.patch.object(server, '_run_qmi', return_value=(None, "")), \
             mock.patch.object(server, 'read_bands',
                               return_value={'lte_bands': [], 'nr_bands': []}):
            res = self.h.read_config()
        self.assertTrue(res["ok"])
        self.assertEqual(res["source"], "config_file")
        self.assertEqual(res["lte"], ["1"])

    def test_diag_read_success(self):
        with mock.patch.object(server, '_run_qmi', return_value=(None, "")), \
             mock.patch.object(server, 'read_bands',
                               return_value={'lte_bands': [1, 3],
                                             'nr_bands': [77]}):
            res = self.h.read_config()
        self.assertTrue(res["ok"])
        self.assertEqual(res["source"], "diag")
        self.assertEqual(res["lte"], ["1", "3"])


class TestBandCamping(unittest.TestCase):
    """A-30: NR serving-cell parsing, plus the existing LTE path."""

    def test_lte_camping(self):
        text = ("mCellInfo=CellInfoLte:{mRegistered=YES mTimeStamp=123 "
                "mCellIdentity=CellIdentityLte:{mEarfcn=2050 mBands=[4]} }\n")
        self.assertEqual(server._parse_band_camping(text), (2050, 4, "LTE"))

    def test_nr_camping_with_mbands(self):
        text = ("mCellInfo=CellInfoNr:{mRegistered=YES mTimeStamp=123 "
                "mCellIdentity=CellIdentityNr:{mNrarfcn=633334 mBands=[78]} }\n")
        self.assertEqual(server._parse_band_camping(text), (633334, 78, "NR"))

    def test_nr_camping_arfcn_fallback(self):
        text = "mCellIdentity=CellIdentityNr:{mNrarfcn=620000}\n"
        self.assertEqual(server._parse_band_camping(text), (620000, 77, "NR"))

    def test_no_cell_returns_none(self):
        self.assertEqual(server._parse_band_camping("mWhatever=1\n"),
                         (None, None, None))


class TestBootApplyNoSeed(ConfigFileCase):
    """A-119: a missing bands.json is a permanent skip — never re-seeded,
    even when the operator property identifies Rogers."""

    def test_missing_file_skips_even_on_rogers(self):
        with mock.patch.object(server, '_get_prop', return_value="302720"):
            res = server.BandHandler.boot_apply(None)
        self.assertEqual(res, {"ok": True, "skipped": True})
        self.assertFalse(server.CONFIG_FILE.exists())


class TestRestartPid(unittest.TestCase):
    """A-24/A-118: restart and health expose the server pid so a client
    can prove a restart by observing a pid change."""

    def test_restart_response_carries_pid(self):
        with mock.patch.object(server.subprocess, 'Popen'):
            res = server.BandHandler.restart_service(None)
        self.assertTrue(res["ok"])
        self.assertEqual(res["pid"], os.getpid())

    def test_health_carries_pid(self):
        h = server.BandHandler.__new__(server.BandHandler)
        with mock.patch.object(server, '_run_qmi',
                               return_value=(0, "  result: status=0 SUCCESS\n"
                                               "    LTE bands: 1 3 \n")):
            res = h.modem_health()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["transport"], "qmi")
        self.assertEqual(res["pid"], os.getpid())


class TestDropLogRollback(unittest.TestCase):
    """A-63: a failed drop-log persistence restores the in-memory value."""

    def setUp(self):
        self._orig = server.SETTINGS.get("drop_log")
        server.SETTINGS["drop_log"] = True

    def tearDown(self):
        server.SETTINGS["drop_log"] = self._orig

    def test_failed_save_restores_old_value(self):
        with mock.patch.object(server, '_save_settings',
                               side_effect=OSError("disk full")):
            res = server.BandHandler.update_drop_log(None, {"enabled": False})
        self.assertFalse(res["ok"])
        self.assertTrue(server.SETTINGS["drop_log"])


class TestDefaults(unittest.TestCase):
    """A-58/A-186: crash-pair-free defaults and the carrier-detection
    flag."""

    def test_all_bands_default_excludes_crash_pair(self):
        d = server.defaults_for_carrier("other")
        self.assertNotIn("7", d["lte"])
        self.assertNotIn("66", d["lte"])
        self.assertNotIn("7", d["nr"])
        self.assertNotIn("66", d["nr"])

    def test_rogers_default_still_excludes_crash_pair(self):
        d = server.defaults_for_carrier("rogers")
        self.assertNotIn("66", d["lte"])

    def test_read_defaults_flags_undetected_carrier(self):
        """A-58: an unreadable operator property must not silently select
        unrestricted defaults."""
        with mock.patch.object(server, '_get_prop', return_value=""):
            res = server.BandHandler.read_defaults(None)
        self.assertFalse(res["carrier_detected"])
        self.assertEqual(res["carrier"], "other")

    def test_read_defaults_detected_rogers(self):
        with mock.patch.object(server, '_get_prop',
                               side_effect=lambda name: "302720"
                               if "numeric" in name else "ROGERS"):
            res = server.BandHandler.read_defaults(None)
        self.assertTrue(res["carrier_detected"])
        self.assertEqual(res["carrier"], "rogers")


class TestAtomicDirFsync(SettingsFileCase):
    """A-141: the atomic writer fsyncs the parent directory after the
    rename so the replacement survives a crash (file fsync + dir fsync)."""

    def test_save_fsyncs_file_and_directory(self):
        with mock.patch("os.fsync") as fsync:
            server.SETTINGS["bind"] = "0.0.0.0"
            server._save_settings()
        self.assertGreaterEqual(fsync.call_count, 2)


if __name__ == '__main__':
    unittest.main()
