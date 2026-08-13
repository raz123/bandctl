"""Regression tests for the packaged diag protocol module."""
import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "bandctl_packaged_diag_protocol", ROOT / "diag" / "protocol.py"
)
protocol = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(protocol)


class TestPackagedDiagProtocol(unittest.TestCase):
    def test_nv_read_echo_validation_accepts_matching_request(self):
        response = b"\x3d\x01\x00\x00\x00\x00\x02\x00AB"

        parsed = protocol.parse_nv_read_response(response, 1, 0)

        self.assertEqual(parsed["data"], b"AB")
        self.assertIsNone(protocol.parse_nv_read_response(response, 2, 0))
        self.assertEqual(protocol.parse_nv_read_response(response)["nv_id"], 1)

    def test_nv_write_echo_validation_accepts_matching_request(self):
        response = b"\x3e\x01\x00\x00\x00\x00"

        parsed = protocol.parse_nv_write_response(response, 1, 0)

        self.assertTrue(parsed["success"])
        self.assertIsNone(protocol.parse_nv_write_response(response, 2, 0))


if __name__ == "__main__":
    unittest.main()
