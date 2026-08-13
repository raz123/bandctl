"""Unit tests for the Band Controller HTTP server (v2.2 wave).

Covers the LAN-access settings feature (bind + bearer token), the auth
gate (token required for remote clients in LAN mode and for state-
changing loopback actions when a token is configured; read-only
diagnostics exempt), band validation (M8), guarded parsing (M6),
atomic config writes (M7), and the static-serving contract (SRV-07).

Follows the module/diag/tests/test_protocol.py convention
(unittest.TestCase classes, runnable under pytest).

The tests never touch the modem: band validation and boot-apply paths
are exercised with stubbed _apply_bands, and requests run against a
socket-free BandHandler built with object.__new__.
"""
import json
import os
import sys
import tempfile
import unittest
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
                          "drop_log": False})

    def test_malformed_json_defaults(self):
        server.SETTINGS_FILE.write_text("{not json")
        self.assertEqual(server._load_settings(),
                         {"bind": "127.0.0.1", "token": None,
                          "drop_log": False})

    def test_invalid_bind_falls_back_to_localhost(self):
        server.SETTINGS_FILE.write_text(
            json.dumps({"bind": "9.9.9.9", "token": "tok"}))
        self.assertEqual(server._load_settings(),
                         {"bind": "127.0.0.1", "token": "tok",
                          "drop_log": False})

    def test_invalid_token_downgrades_lan_bind(self):
        """A-105/A-218: a persisted LAN bind without a usable token is the
        remote-lockout state (every non-loopback client 401s and the
        recovery POST is itself gated). Loading it must downgrade the bind
        to loopback-only instead of accepting the lockout."""
        server.SETTINGS_FILE.write_text(
            json.dumps({"bind": "0.0.0.0", "token": 12345}))
        self.assertEqual(server._load_settings(),
                         {"bind": "127.0.0.1", "token": None,
                          "drop_log": False})

    def test_lan_bind_without_token_field_downgrades(self):
        server.SETTINGS_FILE.write_text(
            json.dumps({"bind": "0.0.0.0"}))
        self.assertEqual(server._load_settings(),
                         {"bind": "127.0.0.1", "token": None,
                          "drop_log": False})

    def test_lan_bind_with_valid_token_kept(self):
        server.SETTINGS_FILE.write_text(
            json.dumps({"bind": "0.0.0.0", "token": "ok-token"}))
        self.assertEqual(server._load_settings(),
                         {"bind": "0.0.0.0", "token": "ok-token",
                          "drop_log": False})

    def test_load_persisted_values(self):
        server.SETTINGS_FILE.write_text(
            json.dumps({"bind": "0.0.0.0", "token": "tok"}))
        self.assertEqual(server._load_settings(),
                         {"bind": "0.0.0.0", "token": "tok",
                          "drop_log": False})

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
        status, _ = run_api('/api/health?action=health', 'GET',
                            addr=('127.0.0.1', 54321))
        self.assertEqual(status, 200)

    def test_ipv6_loopback_read_only_exempt(self):
        server.SETTINGS = self.lan
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
        status, _ = run_api('/api/health?action=health', 'GET',
                            headers={"Authorization": "Bearer sekret-token"},
                            addr=('192.168.1.42', 9999))
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
        """A-158: the auth exemption is path-based, so the documented bare
        GET /api/settings is not 401-gated for a remote LAN client (the
        route still needs the ?action= settings param to dispatch, which
        is the API's existing convention)."""
        server.SETTINGS = self.lan
        status, res = run_api('/api/settings', 'GET',
                              addr=('192.168.1.42', 9999))
        self.assertNotEqual(status, 401)
        self.assertEqual(res, {"error": "unknown action"})

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
        status, _ = run_api('/api/health?action=health', 'GET',
                            headers={'Authorization': 'Bearer tok'},
                            addr=('192.168.1.42', 9999))
        self.assertEqual(status, 200)

    def test_loopback_read_only_still_exempt(self):
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
    """A-189: a valid-but-failed apply still mirrors the config file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = server.CONFIG_FILE
        server.CONFIG_FILE = Path(self._tmp.name) / "bands.json"

    def tearDown(self):
        server.CONFIG_FILE = self._orig
        self._tmp.cleanup()

    def test_failed_apply_mirrors_config_file(self):
        h = server.BandHandler.__new__(server.BandHandler)
        with mock.patch.object(h, "_apply_bands",
                               return_value=(False, "diag",
                                             "diag write failed")):
            res = h.write_config({"lte": [1, 2], "nr": [77]})
        self.assertFalse(res["ok"])
        self.assertEqual(json.loads(server.CONFIG_FILE.read_text()),
                         {"lte": [1, 2], "nr": [77]})


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
        self.assertEqual(status, 200)
        self.assertEqual(res, {"error": "unknown action"})

    def test_unknown_action(self):
        status, res = run_api('/api/foo?action=nope', 'GET')
        self.assertEqual(status, 200)
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
                               return_value=True):
            status, payload = self._reset()
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        cmds = [c.args[0][-1] for c in run.call_args_list]
        self.assertEqual(cmds, ['off', 'on'])

    def test_airplane_path_success_disables_and_verifies(self):
        with mock.patch.object(server, '_cmd_available',
                               side_effect=lambda s, sub: s == 'connectivity'), \
             mock.patch.object(server, '_run_cmd'), \
             mock.patch.object(server, '_wait_for_radio_state',
                               return_value=True), \
             mock.patch.object(server, '_disable_airplane',
                               return_value=True) as disable:
            status, payload = self._reset()
        self.assertTrue(payload['ok'])
        disable.assert_called_once()

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
        # A-185: the snapshot must NOT persist raw radio-buffer lines
        # (IMSI/phone numbers) — an explicit privacy marker replaces them.
        self.assertIn('radio tail: omitted', text)
        self.assertNotIn('PHONE0', text)
        self.assertNotIn('REG_HOME', text)


if __name__ == '__main__':
    unittest.main()
