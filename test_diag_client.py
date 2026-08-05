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
import os
import sys
import threading
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))  # module/diag

from diag_client import DiagClient  # noqa: E402


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


if __name__ == '__main__':
    unittest.main()
