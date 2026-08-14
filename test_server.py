"""Unit tests for the Band Controller HTTP server (v2.2 wave).

Covers the LAN-access settings feature (bind + bearer token), the auth
gate (token required iff LAN enabled and client not loopback; GET
/api/settings exempt), band validation (M8), guarded parsing (M6),
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
    """Base: point SETTINGS_FILE at a temp dir and reset the snapshot."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_file = server.SETTINGS_FILE
        self._orig_settings = server.SETTINGS
        server.SETTINGS_FILE = Path(self._tmp.name) / "settings.json"
        server.SETTINGS = {"bind": "127.0.0.1", "token": None}

    def tearDown(self):
        server.SETTINGS_FILE = self._orig_file
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

    def test_invalid_token_normalized_to_null(self):
        server.SETTINGS_FILE.write_text(
            json.dumps({"bind": "0.0.0.0", "token": 12345}))
        self.assertEqual(server._load_settings(),
                         {"bind": "0.0.0.0", "token": None,
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
    """LAN auth gate: token required iff LAN + non-loopback."""

    def setUp(self):
        super().setUp()
        self.lan = {"bind": "0.0.0.0", "token": "sekret-token"}

    def allowed(self, addr, headers=None, action='read', command='GET'):
        """Replicate handle_api's gate: _auth_required decides, then
        _check_auth validates the credential. True = request passes."""
        h = object.__new__(server.BandHandler)
        h.client_address = addr
        h.headers = FakeHeaders(headers or {})
        h.command = command
        if not h._auth_required(action):
            return True
        return h._check_auth() is None

    def test_loopback_exempt_without_token(self):
        server.SETTINGS = self.lan
        self.assertTrue(self.allowed(('127.0.0.1', 54321)))

    def test_ipv6_loopback_exempt(self):
        server.SETTINGS = self.lan
        self.assertTrue(self.allowed(('::1', 54321)))

    def test_lan_no_token_unauthorized(self):
        server.SETTINGS = self.lan
        self.assertFalse(self.allowed(('192.168.1.42', 54321)))

    def test_lan_correct_token_ok(self):
        server.SETTINGS = self.lan
        self.assertTrue(self.allowed(
            ('192.168.1.42', 54321),
            {"Authorization": "Bearer sekret-token"}))

    def test_lan_wrong_token_unauthorized(self):
        server.SETTINGS = self.lan
        self.assertFalse(self.allowed(
            ('192.168.1.42', 54321),
            {"Authorization": "Bearer wrong-token"}))

    def test_lan_disabled_no_gate(self):
        server.SETTINGS = {"bind": "127.0.0.1", "token": "sekret-token"}
        self.assertTrue(self.allowed(('192.168.1.42', 54321)))

    def test_lan_without_configured_token_rejects_all(self):
        server.SETTINGS = {"bind": "0.0.0.0", "token": None}
        self.assertFalse(self.allowed(('192.168.1.42', 54321)))

    def test_non_ascii_token_rejected_not_crash(self):
        server.SETTINGS = self.lan
        self.assertFalse(self.allowed(
            ('192.168.1.42', 54321),
            {"Authorization": "Bearer \u00e9\u00e8"}))

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

    def test_loopback_request_not_gated_end_to_end(self):
        server.SETTINGS = self.lan
        status, _ = run_api('/api/health?action=health', 'GET',
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
        self.assertIn('radio tail', text)


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


if __name__ == '__main__':
    unittest.main()
