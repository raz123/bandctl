"""Qualcomm Diag Client - Direct /dev/diag interface.

Talks to the modem through /dev/diag without NSG, following the
kernel-verified protocol contract (drivers/char/diag on the
4.19.325-aptusitu kernel):

- Write: ``[0x20 int32 LE][HDLC frame]`` -- no separate token byte.
- Read: drain the 5 mask notifications queued at open(), then parse the
  USER_SPACE_DATA_TYPE stream ``[0x20][num_data][len][HDLC frame]...``.
- Timeout: select()/poll() are useless on this fd (the kernel has no .poll
  handler), so a daemon-thread join implements the timeout around the
  blocking read().
- The client owns the modem memory-device session exclusively; creating it
  is a hard prerequisite for any 0x20 write.
"""
import errno
import fcntl
import os
import struct
import threading
import time
from typing import Optional

try:
    from .protocol import (
        hdlc_encode, hdlc_decode,
        build_nv_read_cmd, build_nv_write_cmd,
        parse_nv_read_response, parse_nv_write_response,
        band_bitmask_to_list, band_list_to_bitmask,
        NV_LTE_BAND_PREF, NV_NR5G_BAND_PREF,
    )
except ImportError:  # imported as a top-level module (module/diag on sys.path)
    from protocol import (
        hdlc_encode, hdlc_decode,
        build_nv_read_cmd, build_nv_write_cmd,
        parse_nv_read_response, parse_nv_write_response,
        band_bitmask_to_list, band_list_to_bitmask,
        NV_LTE_BAND_PREF, NV_NR5G_BAND_PREF,
    )

# ioctl numbers (include/linux/diagchar.h)
DIAG_IOCTL_SWITCH_LOGGING = 7

# data_type values returned by read() (include/linux/diagchar.h)
MSG_MASKS_TYPE = 0x1
LOG_MASKS_TYPE = 0x2
EVENT_MASKS_TYPE = 0x4
DCI_LOG_MASKS_TYPE = 0x100
DCI_EVENT_MASKS_TYPE = 0x200
USER_SPACE_DATA_TYPE = 0x20

# Stream-size ceilings for the HDLC scanner. A malformed item_len field
# (or a corrupted stream) must not be able to grow the retained buffer
# without limit: an item larger than _MAX_FRAME_LEN is treated as
# garbage and the buffer is discarded (resync), and read_response drops
# the accumulated buffer once it exceeds _MAX_STREAM_LEN.
_MAX_FRAME_LEN = 65536      # generous ceiling for one diag item
_MAX_STREAM_LEN = 262144    # ceiling for the accumulate-then-rescan buffer

# The 5 mask notifications a fresh client must drain before NV data
# (diagchar_core.c:364-375).
MASK_NOTIFICATION_TYPES = frozenset((
    MSG_MASKS_TYPE, LOG_MASKS_TYPE, EVENT_MASKS_TYPE,
    DCI_LOG_MASKS_TYPE, DCI_EVENT_MASKS_TYPE,
))

# diag_logging_mode_param_t, __packed, 24 bytes (diagchar.h:650-662):
#   uint32 req_mode; uint32 peripheral_mask; uint32 pd_mask;
#   uint8  mode_param; uint8 diag_id; uint8 pd_val; uint8 reserved;
#   int    peripheral; int device_mask;
# req_mode=2 -> MEMORY_DEVICE_MODE (userspace enum); peripheral_mask=0x0003 ->
# DIAG_CON_APSS|DIAG_CON_MPSS; device_mask=1 -> local proc.
_LOGGING_PARAM = struct.pack(
    '<IIIBBBBii',
    2,       # req_mode: MEMORY_DEVICE_MODE
    0x0003,  # peripheral_mask: APSS | MPSS
    0,       # pd_mask
    0,       # mode_param
    0,       # diag_id
    0,       # pd_val
    0,       # reserved
    -1,      # peripheral: unspecified
    1,       # device_mask: bit 0 = local proc
)


def _mask_to_bytes(mask: int) -> bytes:
    """Pack a band bitmask for the NV wire (little-endian).

    Uses at least 8 bytes (the classic LTE/NR item width); masks that set
    a band above 64 grow to however many bytes are needed (A-12).
    """
    nbytes = max(8, (mask.bit_length() + 7) // 8)
    return mask.to_bytes(nbytes, 'little')


class DiagClient:
    """Direct interface to Qualcomm diag port (owns the modem MD session)."""

    def __init__(self, device: str = "/dev/diag", timeout: float = 2.0):
        """Open the diag device and create the memory-device session.

        Args:
            device: Path to the diag device node.
            timeout: Read timeout in seconds.

        Raises:
            RuntimeError: if another client (e.g. NSG's diag_md) already
                owns the modem session, or SWITCH_LOGGING otherwise fails.
        """
        self.device_path = device
        self.timeout = timeout
        self.fd = None
        # Reader-lifecycle state for the abandoned-reader fix: at most one
        # in-flight os.read() per fd. _reader_lock serializes readers;
        # _fd_dirty + _reader_thread mark a timed-out read whose daemon
        # reader is still blocked on the fd (see _ensure_clean_reader).
        self._reader_thread = None
        self._fd_dirty = False
        self._reader_lock = threading.Lock()
        self._open()
        try:
            self._create_md_session()
        except BaseException:
            self.close()
            raise

    def _open(self):
        """Open the diag device with read/write access.

        O_NONBLOCK is deliberately not used: the kernel ignores it for
        reads (no O_NONBLOCK handling in diagchar_read) and there is no
        .poll, so blocking reads with a thread timeout are the mechanism.
        """
        try:
            self.fd = os.open(self.device_path, os.O_RDWR)
        except PermissionError:
            raise PermissionError(
                f"Cannot open {self.device_path}: permission denied. "
                "Run as root or add your user to the diag group."
            ) from None
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Diag device {self.device_path} not found. "
                "Ensure the diag driver is loaded."
            ) from None

    def _create_md_session(self):
        """DIAG_IOCTL_SWITCH_LOGGING (ioctl 7): create the MD session.

        Mandatory: without it every 0x20 write fails with -EINVAL
        (diagchar_core.c:3566-3572). Sessions are exclusive per peripheral
        (diagchar_core.c:1324-1403).
        """
        try:
            # fcntl.ioctl (os.ioctl does not exist in Termux's Python build)
            fcntl.ioctl(self.fd, DIAG_IOCTL_SWITCH_LOGGING, _LOGGING_PARAM)
        except OSError as e:
            if e.errno in (errno.EEXIST, errno.EINVAL):
                raise RuntimeError(
                    "SWITCH_LOGGING failed: the modem diag session is already "
                    "owned by another client (likely NSG's diag_md). Stop NSG "
                    "or release the session, then retry."
                ) from e
            raise RuntimeError(
                f"SWITCH_LOGGING failed (errno {e.errno}): {e.strerror}"
            ) from e

    def close(self):
        """Close the device; the kernel restores USB mode and frees the session.

        A reader abandoned by a timed-out read is still blocked in
        os.read() on the (now closed) fd it captured at thread start.
        Joining it here reaps the thread whenever the driver unblocks
        reads on close; a driver that never unblocks leaves a harmless
        daemon thread pinned to a dead fd (it can no longer touch any
        live descriptor).
        """
        self._fd_dirty = False
        prior = self._reader_thread
        self._reader_thread = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        if prior is not None:
            prior.join(timeout=0.1)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def send_command(self, cmd_bytes: bytes) -> bool:
        """Send a raw diag command: ``[0x20 int32 LE][HDLC frame]``.

        No separate token byte is written: bytes 4..7 of the buffer are the
        first 4 bytes of the HDLC frame, which the kernel re-reads as the
        remote-proc indicator (diagchar_core.c:4102, 3542-3543). For NV
        commands those bytes are positive, so the command stays local.

        Retries short writes (os.write may accept fewer bytes than
        requested) and returns True only when the FULL buffer was accepted;
        a truncated command would otherwise make the following read wait
        for a response that never comes.
        """
        if self.fd is None:
            raise RuntimeError("Diag device not open")
        data = struct.pack('<I', USER_SPACE_DATA_TYPE) + hdlc_encode(cmd_bytes)
        written = 0
        try:
            while written < len(data):
                n = os.write(self.fd, data[written:])
                if n <= 0:
                    return False  # kernel accepted no further bytes
                written += n
        except OSError:
            return False
        return True

    def _timed_read(self, timeout: Optional[float]) -> Optional[bytes]:
        """Blocking ``os.read`` with a timeout, via a daemon-thread join.

        select()/poll() cannot be used on /dev/diag: there is no .poll
        handler (diagchar_core.c:4391-4401), so they always report the fd
        as readable and read() still blocks. On timeout the daemon reader
        thread is abandoned and the fd is marked dirty; the NEXT read
        retires it (close+reopen, see _ensure_clean_reader) so a second
        reader never races the abandoned one on the same fd for the
        modem's delayed NV reply.

        Returns the bytes read, or None on timeout. Raises OSError on I/O
        failure.
        """
        with self._reader_lock:
            self._ensure_clean_reader()
            result = {}
            # Capture the fd NOW: the closure must never re-evaluate
            # self.fd at execution time, or a reader abandoned by a
            # timeout could grab a replacement descriptor opened by a
            # later _ensure_clean_reader and steal its bytes (A-129).
            fd = self.fd

            def _reader():
                try:
                    result['data'] = os.read(fd, 16384)
                except BaseException as exc:  # OSError, KeyboardInterrupt, ...
                    result['error'] = exc

            thread = threading.Thread(target=_reader, daemon=True)
            self._reader_thread = thread
            thread.start()
            thread.join(timeout if timeout is not None else None)
            if thread.is_alive():
                # Timed out: the daemon reader is still blocked in
                # os.read() on this fd. Keep its reference and mark the fd
                # dirty so the next read knows to close+reopen instead of
                # spawning a second concurrent reader on the same fd.
                self._fd_dirty = True
                return None
            self._reader_thread = None
            if 'error' in result:
                raise result['error']
            return result['data']

    def _ensure_clean_reader(self):
        """Retire an abandoned reader before starting a new read.

        A timed-out read leaves a daemon thread blocked in ``os.read()``
        on the current fd. A second reader on the SAME fd would race it
        for the modem's delayed NV reply: whichever thread loses the race
        blocks again, and the reply is consumed by the abandoned thread's
        unobserved result dict -- the caller sees None (the UI's empty
        bands from an authoritative-looking 'diag' read).

        Called (under ``_reader_lock``) before every read. When the fd is
        dirty and the prior reader is still alive, the fd is closed and
        reopened (fresh MD session): the new reader runs on a different
        file description, so no read can block behind a hung modem for
        more than one timeout. ``close()`` joins the abandoned reader, so
        a driver that unblocks reads on close also reaps the thread
        instead of leaking one per timeout. A prior reader that finished
        on its own leaves the fd reusable.

        Raises RuntimeError if the session cannot be re-created (e.g. the
        modem diag session is now owned by another client).
        """
        if not self._fd_dirty:
            return
        self._fd_dirty = False
        if self._reader_thread is None or not self._reader_thread.is_alive():
            self._reader_thread = None
            return
        # The abandoned reader is still blocked on the old file
        # description. Retire it: close the fd (which also joins the
        # abandoned reader), open a fresh one, and re-create the
        # memory-device session on it.
        self.close()
        self._open()
        try:
            self._create_md_session()
        except BaseException:
            self.close()
            raise

    def _retire_stale_reader(self):
        """Retire any abandoned reader BEFORE a command is written.

        A timed-out read leaves the fd dirty. If the next NV command is
        written first and the cleanup happens inside read_response's first
        read, the command is retired along with the old session before its
        response can be observed (A-126). Cleaning up here -- before
        send_command -- keeps the command on the fresh session.
        """
        with self._reader_lock:
            self._ensure_clean_reader()

    def _drain_mask_notifications(self, deadline: float) -> bytes:
        """Drain the 5 mask notifications queued by the kernel at open().

        Each read returns ``[data_type int32 LE][blob]``; the mask blobs
        (data_type 0x1/0x2/0x4/0x100/0x200) are discarded. Returns any chunk
        that was NOT a mask notification (e.g. an early MD stream) so the
        caller can process it. Each read is bounded by the time remaining
        until ``deadline`` (monotonic), so the whole drain cannot exceed
        the caller's timeout.
        """
        leftover = bytearray()
        for _ in range(5):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            chunk = self._timed_read(remaining)
            if chunk is None:
                break
            if len(chunk) < 4:
                leftover.extend(chunk)
                break
            data_type = struct.unpack_from('<I', chunk, 0)[0]
            if data_type in MASK_NOTIFICATION_TYPES:
                continue  # discard the mask blob
            leftover.extend(chunk)
            break
        return bytes(leftover)

    def _scan_stream(self, buf: bytearray):
        """Scan an MD stream buffer for an NV response frame.

        Stream layout: ``[0x20][num_data][ (len uint32, HDLC frame) * num_data ]``.
        HDLC-decodes each item and returns the payload of the first NV
        response (first byte 0x3D/0x3E). Non-NV frames (notifications,
        other command replies) are skipped: returning one as a fallback
        would let a stray notification be attributed to the caller's NV
        request and leave the real NV frame stranded in the stream
        (A-61).

        A data_type other than 0x20, or an item_len that exceeds
        ``_MAX_FRAME_LEN`` (malformed/impossible), discards the buffer so
        the caller resynchronizes on the next read instead of retaining
        garbage forever (A-75).

        Returns ``(payload, remaining_buffer)``. When no complete NV frame
        is available yet it returns ``(None, buf)`` with the buffer intact
        so the caller can append the next read chunk and rescan (handles
        partial frames across reads and num_data == 0).
        """
        while len(buf) >= 8:
            data_type = struct.unpack_from('<I', buf, 0)[0]
            if data_type != USER_SPACE_DATA_TYPE:
                # Unknown notification we cannot size (one blob per read) --
                # discard the whole chunk.
                return None, bytearray()
            num_data = struct.unpack_from('<I', buf, 4)[0]
            pos = 8
            for _ in range(num_data):
                if len(buf) < pos + 4:
                    return None, buf  # partial item header
                item_len = struct.unpack_from('<I', buf, pos)[0]
                pos += 4
                if item_len > _MAX_FRAME_LEN:
                    # Impossible length: discard the buffer and resync.
                    return None, bytearray()
                if len(buf) < pos + item_len:
                    return None, buf  # partial frame
                payload = hdlc_decode(bytes(buf[pos:pos + item_len]))
                pos += item_len
                if payload and payload[0] in (0x3D, 0x3E):
                    return payload, bytearray(buf[pos:])
            buf = bytearray(buf[pos:])  # whole block consumed, no NV frame
        return None, buf

    def read_response(self, timeout: Optional[float] = None) -> Optional[bytes]:
        """Read and return the payload of the first NV-type (0x3D/0x3E) frame.

        Drains the 5 initial mask notifications, then parses the
        USER_SPACE_DATA_TYPE stream, HDLC-decoding each item. Returns None
        on timeout or I/O error.

        The caller's timeout is an OVERALL deadline: every individual read
        gets only the time remaining, so a stream that keeps dribbling
        partial bytes cannot extend the wait indefinitely (A-32). The
        accumulated buffer is also capped: if it grows past
        ``_MAX_STREAM_LEN`` without yielding a frame, it is discarded and
        the scan resynchronizes on the next read instead of accumulating
        memory forever (A-75).
        """
        if self.fd is None:
            raise RuntimeError("Diag device not open")
        if timeout is None:
            timeout = self.timeout

        deadline = time.monotonic() + timeout
        try:
            buf = bytearray(self._drain_mask_notifications(deadline))
            while True:
                payload, buf = self._scan_stream(buf)
                if payload is not None:
                    return payload
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                chunk = self._timed_read(remaining)
                if chunk is None or chunk == b'':
                    return None  # timeout, or device closed
                buf.extend(chunk)
                if len(buf) > _MAX_STREAM_LEN:
                    buf = bytearray()  # resync: drop the malformed accumulation
        except OSError:
            return None

    def read_nv(self, nv_id: int, slot: int = 0) -> Optional[dict]:
        """Read an NV item from the modem.

        Returns the ``parse_nv_read_response`` dict
        (``{nv_id, sub_id, status, data, success}``) or None on failure.
        """
        # Clean up a reader abandoned by a previous timeout BEFORE the
        # command is written: writing first and cleaning up inside
        # read_response would retire the command along with the old
        # session (A-126).
        self._retire_stale_reader()
        if not self.send_command(build_nv_read_cmd(nv_id, slot)):
            return None
        response = self.read_response()
        if response is None:
            return None
        # Echo-validate: a response carrying a DIFFERENT nv_id (e.g. the
        # other band's reply surfacing via _scan_stream) must not be
        # attributed to this request.
        return parse_nv_read_response(response, nv_id, slot)

    def write_nv(self, nv_id: int, data: bytes, slot: int = 0) -> bool:
        """Write an NV item; True when the 0x3E echo reports status 0."""
        # Same ordering guarantee as read_nv: retire any abandoned reader
        # before the command is written (A-126).
        self._retire_stale_reader()
        if not self.send_command(build_nv_write_cmd(nv_id, data, slot)):
            return False
        response = self.read_response()
        if response is None:
            return False
        parsed = parse_nv_write_response(response, nv_id, slot)
        return bool(parsed and parsed['success'])

    def get_band_config(self, slot: int = 0) -> dict:
        """Read current LTE/NR band configuration.

        Reads NV 0x06828 (LTE band pref) and NV 0x06946 (NR band pref on
        SM8250); each is parsed as a little-endian bitmask. The mask is an
        arbitrary-precision integer, so bands above 64 (LTE B66/B71, NR
        B77/B78) round-trip instead of being dropped (A-12).
        """
        lte_result = self.read_nv(NV_LTE_BAND_PREF, slot)
        nr_result = self.read_nv(NV_NR5G_BAND_PREF, slot)

        lte_bands = []
        nr_bands = []

        if lte_result and lte_result['success'] and len(lte_result['data']) >= 8:
            mask = int.from_bytes(lte_result['data'][:16], 'little')
            lte_bands = band_bitmask_to_list(mask)

        if nr_result and nr_result['success'] and len(nr_result['data']) >= 8:
            mask = int.from_bytes(nr_result['data'][:16], 'little')
            nr_bands = band_bitmask_to_list(mask)

        return {
            'lte_bands': lte_bands,
            'nr_bands': nr_bands,
        }

    def set_band_config(self, lte_bands: list, nr_bands: list, slot: int = 0) -> bool:
        """Write LTE/NR band configuration.

        Packs each band list into a little-endian bitmask (8 bytes when
        the mask fits, more for bands above 64) and writes NV 0x06828
        (LTE) and NV 0x06946 (NR). Returns True only if both writes
        succeeded.

        The two NV items are independent writes, so a second-write failure
        would otherwise leave the modem half-applied (new LTE mask, old NR
        mask). The previous values are read first and the first write is
        rolled back when the second fails, so a reported failure leaves
        the modem on its original configuration (A-88).
        """
        lte_prev = self.read_nv(NV_LTE_BAND_PREF, slot)
        nr_prev = self.read_nv(NV_NR5G_BAND_PREF, slot)

        lte_mask = band_list_to_bitmask(lte_bands)
        nr_mask = band_list_to_bitmask(nr_bands)

        lte_ok = self.write_nv(NV_LTE_BAND_PREF, _mask_to_bytes(lte_mask), slot)
        if not lte_ok:
            return False

        nr_ok = self.write_nv(NV_NR5G_BAND_PREF, _mask_to_bytes(nr_mask), slot)
        if not nr_ok:
            # Roll the first write back so the modem is not left with a
            # half-applied configuration.
            if lte_prev and lte_prev['success'] and len(lte_prev['data']) >= 8:
                self.write_nv(NV_LTE_BAND_PREF, lte_prev['data'][:16], slot)
            return False
        return True


# Convenience functions
# Serialize all /dev/diag session use: the MD session is exclusive per
# client (DIAG_IOCTL_SWITCH_LOGGING fails if another client owns it), and
# the threaded HTTP server would otherwise race concurrent read/write
# calls (v2.4 hardening). External diag clients (NSG, Termux tools) are
# outside this lock — contention with them surfaces as the session's own
# clear error.
_DIAG_LOCK = threading.Lock()


def read_bands(device: str = "/dev/diag") -> dict:
    """Read the band configuration from the modem.

    Returns a dict with 'lte_bands' and 'nr_bands' lists.
    """
    with _DIAG_LOCK:
        with DiagClient(device) as client:
            return client.get_band_config()


def write_bands(lte_bands: list, nr_bands: list, device: str = "/dev/diag") -> bool:
    """Write the band configuration to the modem.

    Returns True if all writes succeeded.
    """
    with _DIAG_LOCK:
        with DiagClient(device) as client:
            return client.set_band_config(lte_bands, nr_bands)
