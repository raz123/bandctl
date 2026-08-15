"""Unit tests for DiagClient's single-in-flight-reader guarantee.

The abandoned-reader race (fixed in the v2.2 audit wave): a timed-out
_timed_read() leaves a daemon thread blocked in os.read() on the fd. A
subsequent read on that SAME fd would spawn a second concurrent reader,
and the two would race for the modem's delayed NV reply: whichever reader
loses the race blocks again, and the reply is consumed by the abandoned
thread's unobserved result dict. The caller then sees None -- the UI
reported empty bands from an authoritative-looking 'diag' read.

The fix guarantees at most one in-flight reader per fd. On timeout the fd
is marked dirty (flag + abandoned-thread reference); before any subsequent
read, if the prior reader is still alive, the fd is closed and reopened
(fresh MD session) so a read can never block behind a hung modem for more
than one timeout.

These tests simulate the diag fd with os.pipe() pairs: no /dev/diag or
kernel session is touched -- _open/_create_md_session are monkeypatched to
hand out fresh pipe fds, and DiagClient.__init__ runs normally on top.
"""
import errno
import os
import struct
import sys
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))  # module/diag

import diag_client  # noqa: E402
from diag_client import (  # noqa: E402
    DiagClient, NV_LTE_BAND_PREF, NV_NR5G_BAND_PREF,
)


class TestAbandonedReaderRace(unittest.TestCase):
    """A timed-out read must not leak a concurrent reader onto the fd."""

    def _make_client(self, timeout=0.05):
        """A DiagClient whose 'device' is a simulated pipe session.

        Each fake open() allocates a fresh pipe pair; the read end becomes
        client.fd, the write end is used by the test to stage payloads.
        """
        client = DiagClient.__new__(DiagClient)
        state = {'fds': []}

        def fake_open():
            r, w = os.pipe()
            state['fds'].append((r, w))
            client.fd = r

        def fake_session():
            pass  # no ioctl in the simulation

        client._open = fake_open  # plain attr: no MethodType binding (self not prepended)
        client._create_md_session = fake_session
        DiagClient.__init__(client, device='<pipe-sim>', timeout=timeout)
        return client, state

    def _close_state(self, client, state):
        # The read ends are owned by the client (closed/reopened by it);
        # only the write ends need explicit closing here.
        client.close()
        for _r, w in state['fds']:
            try:
                os.close(w)
            except OSError:
                pass

    def test_timed_out_read_does_not_consume_later_read(self):
        # First read: no data available -> times out; the daemon reader
        # stays blocked in os.read() on the ORIGINAL fd (the modem's reply
        # is delayed). The second read must retire that fd (close+reopen,
        # fresh session) instead of racing the stuck reader, and must
        # return the payload staged for the NEW session.
        client, state = self._make_client(timeout=0.05)
        deliver_b = threading.Timer(
            0.25, lambda: os.write(state['fds'][1][1], b'fresh-b')
        )
        deliver_b.start()
        try:
            self.assertIsNone(client._timed_read(client.timeout))
            self.assertTrue(client._fd_dirty)
            self.assertIsNotNone(client._reader_thread)
            self.assertEqual(len(state['fds']), 1)

            result = client._timed_read(0.5)
            self.assertEqual(result, b'fresh-b')   # its OWN payload, not None
            self.assertEqual(len(state['fds']), 2)  # fd was closed+reopened
            self.assertIsNone(client._reader_thread)
        finally:
            deliver_b.cancel()
            self._close_state(client, state)

    def test_finished_reader_reuses_fd(self):
        # When the abandoned reader exits on its own (the delayed reply
        # finally arrived), the next read reuses the same fd: no reopen,
        # and the new request's payload is delivered intact.
        client, state = self._make_client(timeout=0.05)
        try:
            self.assertIsNone(client._timed_read(client.timeout))

            os.write(state['fds'][0][1], b'stale')
            reader = client._reader_thread
            reader.join(1.0)
            self.assertFalse(reader.is_alive())

            os.write(state['fds'][0][1], b'next')
            self.assertEqual(client._timed_read(0.5), b'next')
            self.assertEqual(len(state['fds']), 1)  # no reopen
        finally:
            self._close_state(client, state)


class TestScanStream(unittest.TestCase):
    """_scan_stream against constructed MD stream buffers (A-61/A-75/A-163)."""

    @staticmethod
    def _client():
        return DiagClient.__new__(DiagClient)

    @staticmethod
    def _nv_read_resp(nv_id=NV_LTE_BAND_PREF, data=b"AB"):
        return (b"\x3d" + struct.pack('<H', nv_id) + struct.pack('<H', 0)
                + b"\x00" + struct.pack('<H', len(data)) + data)

    def _block(self, items):
        out = bytearray(struct.pack('<II', diag_client.USER_SPACE_DATA_TYPE, len(items)))
        for payload in items:
            frame = diag_client.hdlc_encode(payload)
            out += struct.pack('<I', len(frame)) + frame
        return bytes(out)

    def test_golden_nv_read_stream(self):
        # A fully-formed MD stream: one USER_SPACE_DATA_TYPE block with a
        # single NV read response item (A-163 golden fixture).
        client = self._client()
        payload = self._nv_read_resp()
        payload_out, remaining = client._scan_stream(bytearray(self._block([payload])))
        self.assertEqual(payload_out, payload)
        self.assertEqual(remaining, b"")

    def test_golden_stream_across_reads(self):
        # The same block split across reads: partial scans retain the
        # buffer, and the NV frame appears once the tail arrives.
        client = self._client()
        payload = self._nv_read_resp()
        block = self._block([payload])
        split = len(block) // 2
        first, buf = client._scan_stream(bytearray(block[:split]))
        self.assertIsNone(first)
        second, buf = client._scan_stream(buf)
        self.assertIsNone(second)  # still partial
        buf += bytearray(block[split:])
        payload_out, remaining = client._scan_stream(buf)
        self.assertEqual(payload_out, payload)
        self.assertEqual(remaining, b"")

    def test_notification_before_nv_frame_is_skipped(self):
        # A valid non-NV notification interleaved before the NV frame must
        # be skipped, not returned as the response (A-61): returning it
        # would strand the real NV frame for a later command.
        client = self._client()
        notif = b"\x01notification"
        nv = self._nv_read_resp()
        payload_out, _ = client._scan_stream(bytearray(self._block([notif, nv])))
        self.assertEqual(payload_out, nv)

    def test_non_nv_only_stream_yields_nothing(self):
        client = self._client()
        notif = b"\x01notification"
        payload_out, remaining = client._scan_stream(bytearray(self._block([notif, notif])))
        self.assertIsNone(payload_out)
        self.assertEqual(remaining, b"")

    def test_malformed_item_len_discards_buffer(self):
        # An impossible item_len must not be retained for unbounded growth
        # (A-75): the buffer is discarded so the caller resyncs.
        client = self._client()
        block = (struct.pack('<II', diag_client.USER_SPACE_DATA_TYPE, 1)
                 + struct.pack('<I', 0xFFFFFFFF) + b"\x00" * 16)
        payload_out, remaining = client._scan_stream(bytearray(block))
        self.assertIsNone(payload_out)
        self.assertEqual(remaining, b"")


class TestReadResponseDeadline(unittest.TestCase):
    """A-32: read_response enforces ONE overall deadline for all reads."""

    def _make_client(self, timeout=0.05):
        client = DiagClient.__new__(DiagClient)
        state = {'fds': []}

        def fake_open():
            r, w = os.pipe()
            state['fds'].append((r, w))
            client.fd = r

        def fake_session():
            pass

        client._open = fake_open
        client._create_md_session = fake_session
        DiagClient.__init__(client, device='<pipe-sim>', timeout=timeout)
        return client, state

    def _close_state(self, client, state):
        client.close()
        for _r, w in state['fds']:
            try:
                os.close(w)
            except OSError:
                pass

    def test_overall_deadline_bounds_total_wait(self):
        # Feed a stream that never completes a frame (data_type 0x20,
        # num_data 1, item_len 1000, then a dribble of 4-byte chunks every
        # 20 ms). Each chunk arrives well within the timeout, so a
        # per-read timeout would loop forever; the overall deadline must
        # bound the whole call.
        client, state = self._make_client(timeout=0.05)
        header = struct.pack('<III', diag_client.USER_SPACE_DATA_TYPE, 1, 1000)
        chunks = [header[i:i + 4] for i in range(0, 12, 4)]
        chunks += [b"\x00\x00\x00\x00"] * 60

        def feeder():
            for chunk in chunks:
                time.sleep(0.02)
                os.write(state['fds'][-1][1], chunk)

        t = threading.Thread(target=feeder, daemon=True)
        t.start()
        try:
            start = time.monotonic()
            result = client.read_response(0.3)
            elapsed = time.monotonic() - start
            self.assertIsNone(result)
            self.assertLess(elapsed, 0.9)  # bounded by the deadline, not the dribble
        finally:
            t.join()
            self._close_state(client, state)


class TestSendCommandWrites(unittest.TestCase):
    """A-56: short diag writes must be retried and reported honestly."""

    def _make_client(self, timeout=0.05):
        client = DiagClient.__new__(DiagClient)
        state = {'fds': []}

        def fake_open():
            r, w = os.pipe()
            state['fds'].append((r, w))
            client.fd = r

        def fake_session():
            pass

        client._open = fake_open
        client._create_md_session = fake_session
        DiagClient.__init__(client, device='<pipe-sim>', timeout=timeout)
        return client, state

    def _close_state(self, client, state):
        client.close()
        for _r, w in state['fds']:
            try:
                os.close(w)
            except OSError:
                pass

    def test_short_write_reported_as_failure(self):
        # The kernel accepts only 1 byte of the 19-byte buffer, then the
        # device errors: send_command must return False, not True.
        client, state = self._make_client()
        calls = []

        def flaky_write(fd, data):
            calls.append(len(data))
            if len(calls) == 1:
                return 1  # short write
            raise OSError(errno.EIO, "diag write failed")

        try:
            with mock.patch('os.write', side_effect=flaky_write):
                self.assertFalse(client.send_command(b"\x3d" + b"\x00" * 11))
            self.assertGreater(len(calls), 1)  # it retried the remainder
        finally:
            self._close_state(client, state)

    def test_short_write_retried_until_complete(self):
        # A partial write followed by a full write of the remainder must
        # succeed: the kernel saw the whole buffer.
        client, state = self._make_client()
        cmd = b"\x3d" + b"\x00" * 11
        calls = []

        def partial_write(fd, data):
            calls.append(len(data))
            if len(calls) == 1:
                return 1
            return len(data)  # full remainder accepted

        try:
            with mock.patch('os.write', side_effect=partial_write):
                self.assertTrue(client.send_command(cmd))
            # 4-byte type prefix + HDLC frame
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1], 4 + len(diag_client.hdlc_encode(cmd)) - 1)
        finally:
            self._close_state(client, state)


class TestBandConfigWriteRollback(unittest.TestCase):
    """A-88: a second-write failure must roll back the first write."""

    def _make_client(self):
        client = DiagClient.__new__(DiagClient)
        state = {'fds': []}
        client.fd = -1
        return client, state

    def _close_state(self, client, state):
        for _r, w in state['fds']:
            try:
                os.close(w)
            except OSError:
                pass

    def test_second_write_failure_rolls_back_first(self):
        client, state = self._make_client()
        prev_lte = struct.pack('<Q', 0b11)  # previous LTE mask: bands 1,2
        calls = []

        def fake_read_nv(nv_id, slot=0):
            if nv_id == NV_LTE_BAND_PREF:
                return {'success': True, 'data': prev_lte,
                        'nv_id': nv_id, 'sub_id': slot, 'status': 0}
            return {'success': True, 'data': struct.pack('<Q', 0),
                    'nv_id': nv_id, 'sub_id': slot, 'status': 0}

        def fake_write_nv(nv_id, data, slot=0):
            calls.append((nv_id, bytes(data), slot))
            if nv_id == NV_NR5G_BAND_PREF:
                return False  # second write fails
            return True

        client.read_nv = fake_read_nv
        client.write_nv = fake_write_nv
        try:
            self.assertFalse(client.set_band_config([1], [1]))
            self.assertEqual([c[0] for c in calls],
                             [NV_LTE_BAND_PREF, NV_NR5G_BAND_PREF, NV_LTE_BAND_PREF])
            self.assertEqual(calls[2][1], prev_lte)  # restored the prior LTE mask
        finally:
            self._close_state(client, state)

    def test_success_path_writes_both_with_high_bands(self):
        client, state = self._make_client()
        writes = []

        def fake_read_nv(nv_id, slot=0):
            return {'success': True, 'data': struct.pack('<Q', 0),
                    'nv_id': nv_id, 'sub_id': slot, 'status': 0}

        def fake_write_nv(nv_id, data, slot=0):
            writes.append((nv_id, bytes(data)))
            return True

        client.read_nv = fake_read_nv
        client.write_nv = fake_write_nv
        try:
            self.assertTrue(client.set_band_config([1, 66], [78]))
            self.assertEqual([w[0] for w in writes],
                             [NV_LTE_BAND_PREF, NV_NR5G_BAND_PREF])
            # Band 66 needs 9 mask bytes; the wire must carry them (A-12).
            self.assertGreater(len(writes[0][1]), 8)
        finally:
            self._close_state(client, state)


class TestCommandBeforeReaderCleanup(unittest.TestCase):
    """A-126: an NV command must be written AFTER stale-reader cleanup."""

    def _make_client(self, timeout=0.05):
        client = DiagClient.__new__(DiagClient)
        state = {'fds': []}

        def fake_open():
            r, w = os.pipe()
            state['fds'].append((r, w))
            client.fd = r

        def fake_session():
            pass

        client._open = fake_open
        client._create_md_session = fake_session
        DiagClient.__init__(client, device='<pipe-sim>', timeout=timeout)
        return client, state

    def _close_state(self, client, state):
        client.close()
        for _r, w in state['fds']:
            try:
                os.close(w)
            except OSError:
                pass

    def test_read_nv_command_lands_on_fresh_session(self):
        client, state = self._make_client(timeout=0.05)
        try:
            # Leave a stale (timed-out) reader behind.
            self.assertIsNone(client._timed_read(client.timeout))
            self.assertTrue(client._fd_dirty)

            written_fds = []

            def record_write(fd, data):
                written_fds.append(fd)
                return len(data)

            with mock.patch('os.write', side_effect=record_write):
                result = client.read_nv(1, 0)
            self.assertIsNone(result)  # nothing staged on the new session

            # The session was reopened and the command was written to the
            # NEW session's fd -- not retired with the old one (A-126).
            self.assertEqual(len(state['fds']), 2)
            self.assertEqual(written_fds, [state['fds'][1][0]])
        finally:
            self._close_state(client, state)


if __name__ == '__main__':
    unittest.main()
