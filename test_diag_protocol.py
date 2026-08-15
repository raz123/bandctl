"""Regression tests for the packaged diag protocol module.

Covers the kernel-verified protocol contract: CRC-16/CCITT (reflected,
raw crc_ccitt, complemented footer), HDLC encode/decode (escapes, strict
framing: unterminated frames and trailing bytes rejected), NV command
build/parse (layout, endianness, echo validation), and band bitmask
round-trips including bands above 64.
"""
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


class TestCrcCcitt(unittest.TestCase):
    """A-163: the CRC check value the kernel's crc_ccitt must produce."""

    def test_check_value_123456789(self):
        # Reflected CRC-16/CCITT, seed 0xFFFF, NO final complement: the raw
        # value is 0x6F91; the frame footer is the complement, 0x906E,
        # emitted low byte first (the classic "0x906E footer" check value).
        self.assertEqual(protocol.crc_ccitt(b"123456789"), 0x6F91)
        self.assertEqual((~protocol.crc_ccitt(b"123456789")) & 0xFFFF, 0x906E)

    def test_footer_bytes_emitted_low_first(self):
        frame = protocol.hdlc_encode(b"123456789")
        self.assertEqual(frame[-3:-1], b"\x6e\x90")  # crc lo, crc hi
        self.assertEqual(frame[-1], protocol.HDLC_FLAG)


class TestHdlcFrame(unittest.TestCase):
    def test_round_trip_plain(self):
        payload = b"\x3d\x28\x68\x00\x00\x00\x00\x02\x00AB"
        self.assertEqual(protocol.hdlc_decode(protocol.hdlc_encode(payload)), payload)

    def test_round_trip_escape_bytes(self):
        # 0x7D and 0x7E inside the payload must survive the escape dance.
        payload = b"\x3d\x7e\x7d\x00\x7e\x7d"
        self.assertEqual(protocol.hdlc_decode(protocol.hdlc_encode(payload)), payload)

    def test_empty_payload_frame_rejected(self):
        # The decoder requires at least one byte beyond the CRC footer
        # (len(data) >= 3 pre-existing contract), so an empty payload
        # frame -- payload b"" + 2 CRC bytes + flag -- does not decode.
        self.assertIsNone(protocol.hdlc_decode(protocol.hdlc_encode(b"")))

    def test_unterminated_frame_rejected(self):
        # A frame missing its trailing 0x7E (CRC would still validate)
        # must NOT be accepted as complete (A-80).
        frame = protocol.hdlc_encode(b"payload")
        self.assertIsNone(protocol.hdlc_decode(frame[:-1]))

    def test_trailing_bytes_rejected(self):
        # Bytes after the terminating 0x7E mean a corrupted/concatenated
        # item; the decoder must reject rather than silently drop them
        # (A-127).
        frame = protocol.hdlc_encode(b"payload")
        self.assertIsNone(protocol.hdlc_decode(frame + b"\x00\xff"))

    def test_leading_flags_tolerated(self):
        # The kernel decoder accepts one stray leading flag at message start.
        frame = protocol.hdlc_encode(b"payload")
        self.assertEqual(
            protocol.hdlc_decode(b"\x7e" + frame), b"payload"
        )

    def test_crc_corruption_rejected(self):
        frame = bytearray(protocol.hdlc_encode(b"payload"))
        frame[-3] ^= 0xFF  # corrupt the CRC low byte
        self.assertIsNone(protocol.hdlc_decode(bytes(frame)))

    def test_empty_and_truncated_frames_rejected(self):
        self.assertIsNone(protocol.hdlc_decode(b""))
        self.assertIsNone(protocol.hdlc_decode(b"\x7e"))
        self.assertIsNone(protocol.hdlc_decode(b"\x7e\x7e"))
        # Two bytes cannot hold payload + CRC + flag.
        self.assertIsNone(protocol.hdlc_decode(b"\x3d\x01\x7e"))

    def test_dangling_escape_rejected(self):
        self.assertIsNone(protocol.hdlc_decode(b"\x3d\x7d"))


class TestNvCommandBuild(unittest.TestCase):
    def test_read_cmd_layout_and_endianness(self):
        cmd = protocol.build_nv_read_cmd(26664, 3)
        self.assertEqual(len(cmd), 12)
        self.assertEqual(cmd[0], protocol.DIAG_CMD_NV_READ)
        self.assertEqual(cmd[1:3], b"\x28\x68")  # 0x06828 LE
        self.assertEqual(cmd[3:5], b"\x03\x00")  # sub_id 3 LE
        self.assertEqual(cmd[5:], b"\x00" * 7)   # pad

    def test_write_cmd_layout_and_endianness(self):
        cmd = protocol.build_nv_write_cmd(26950, b"ABCD", 0)
        self.assertEqual(len(cmd), 11)  # 7 header + 4 data
        self.assertEqual(cmd[0], protocol.DIAG_CMD_NV_WRITE)
        self.assertEqual(cmd[1:3], b"\x46\x69")  # 0x06946 LE
        self.assertEqual(cmd[5:7], b"\x04\x00")  # data_len 4 LE
        self.assertEqual(cmd[7:], b"ABCD")


class TestNvReadParse(unittest.TestCase):
    READ_RESP = b"\x3d\x28\x68\x00\x00\x00\x02\x00AB"  # nv 0x06828, status 0, data_len 2, data "AB"

    def test_parse_matching_request(self):
        parsed = protocol.parse_nv_read_response(self.READ_RESP, 0x06828, 0)
        self.assertEqual(parsed["nv_id"], 0x06828)
        self.assertEqual(parsed["sub_id"], 0)
        self.assertEqual(parsed["status"], 0)
        self.assertEqual(parsed["data"], b"AB")
        self.assertTrue(parsed["success"])

    def test_echo_mismatch_rejected(self):
        # A different nv_id (e.g. the NR reply surfacing for an LTE
        # request) must be rejected, not misattributed (A-150).
        self.assertIsNone(protocol.parse_nv_read_response(self.READ_RESP, 0x06946, 0))
        self.assertIsNone(protocol.parse_nv_read_response(self.READ_RESP, 0x06828, 1))
        # Without expectations, any well-formed frame is accepted.
        self.assertEqual(protocol.parse_nv_read_response(self.READ_RESP)["nv_id"], 0x06828)

    def test_nonzero_status_reported(self):
        resp = b"\x3d\x28\x68\x00\x00\x0b\x02\x00AB"  # status 11
        parsed = protocol.parse_nv_read_response(resp)
        self.assertEqual(parsed["status"], 11)
        self.assertFalse(parsed["success"])

    def test_wrong_command_byte_rejected(self):
        self.assertIsNone(protocol.parse_nv_read_response(b"\x3e\x28\x68\x00\x00\x00"))

    def test_short_and_truncated_rejected(self):
        self.assertIsNone(protocol.parse_nv_read_response(b"\x3d"))
        self.assertIsNone(protocol.parse_nv_read_response(b"\x3d\x28\x68\x00\x00\x00\x00"))
        # data_len 100 but only 2 bytes of data present
        truncated = b"\x3d\x28\x68\x00\x00\x00\x64\x00AB"
        self.assertIsNone(protocol.parse_nv_read_response(truncated))


class TestNvWriteParse(unittest.TestCase):
    def test_parse_matching_request(self):
        resp = b"\x3e\x46\x69\x00\x00\x00"  # nv 0x06946, sub 0, status 0
        parsed = protocol.parse_nv_write_response(resp, 0x06946, 0)
        self.assertEqual(parsed["nv_id"], 0x06946)
        self.assertTrue(parsed["success"])

    def test_echo_mismatch_rejected(self):
        resp = b"\x3e\x46\x69\x00\x00\x00"
        self.assertIsNone(protocol.parse_nv_write_response(resp, 0x06828, 0))

    def test_nonzero_status_reported(self):
        resp = b"\x3e\x46\x69\x00\x00\x01"
        self.assertFalse(protocol.parse_nv_write_response(resp)["success"])

    def test_short_rejected(self):
        self.assertIsNone(protocol.parse_nv_write_response(b"\x3e\x46\x69"))


class TestBandBitmask(unittest.TestCase):
    def test_round_trip_low_bands(self):
        bands = [1, 3, 20, 64]
        mask = protocol.band_list_to_bitmask(bands)
        self.assertEqual(protocol.band_bitmask_to_list(mask), bands)

    def test_high_bands_not_dropped(self):
        # LTE B66/B71 and NR B77/B78 must survive a round trip (A-12).
        bands = [66, 71, 77, 78]
        mask = protocol.band_list_to_bitmask(bands)
        self.assertEqual(protocol.band_bitmask_to_list(mask), bands)
        self.assertGreater(mask, 0xFFFFFFFFFFFFFFFF)  # exceeds 64 bits

    def test_mixed_round_trip(self):
        bands = [1, 2, 64, 66, 71, 77, 78]
        self.assertEqual(
            protocol.band_bitmask_to_list(protocol.band_list_to_bitmask(bands)), bands
        )

    def test_empty_list(self):
        self.assertEqual(protocol.band_list_to_bitmask([]), 0)
        self.assertEqual(protocol.band_bitmask_to_list(0), [])

    def test_negative_and_zero_ignored(self):
        self.assertEqual(protocol.band_list_to_bitmask([0, -5, 3]), 0b100)


if __name__ == "__main__":
    unittest.main()
