"""Qualcomm Diag Client - Direct /dev/diag interface.

Talks to the modem through /dev/diag without NSG, following the
kernel-verified protocol contract (docs/protocol_kernel.md):

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
        """Close the device; the kernel restores USB mode and frees the session."""
        self._fd_dirty = False
        self._reader_thread = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

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

        Returns True if the write was accepted.
        """
        if self.fd is None:
            raise RuntimeError("Diag device not open")
        try:
            os.write(
                self.fd,
                struct.pack('<I', USER_SPACE_DATA_TYPE) + hdlc_encode(cmd_bytes),
            )
            return True
        except OSError:
            return False

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

            def _reader():
                try:
                    result['data'] = os.read(self.fd, 16384)
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
        more than one timeout. A prior reader that finished on its own
        leaves the fd reusable.

        Raises RuntimeError if the session cannot be re-created (e.g. the
        modem diag session is now owned by another client).
        """
        if not self._fd_dirty:
            return
        self._fd_dirty = False
        prior = self._reader_thread
        self._reader_thread = None
        if prior is None or not prior.is_alive():
            return  # reader exited on its own; fd is clean, reuse it
        # The abandoned reader is still blocked on the old file
        # description. Retire it: close the fd, open a fresh one, and
        # re-create the memory-device session on it.
        self.close()
        self._open()
        try:
            self._create_md_session()
        except BaseException:
            self.close()
            raise

    def _drain_mask_notifications(self, timeout: Optional[float]) -> bytes:
        """Drain the 5 mask notifications queued by the kernel at open().

        Each read returns ``[data_type int32 LE][blob]``; the mask blobs
        (data_type 0x1/0x2/0x4/0x100/0x200) are discarded. Returns any chunk
        that was NOT a mask notification (e.g. an early MD stream) so the
        caller can process it.
        """
        leftover = bytearray()
        for _ in range(5):
            chunk = self._timed_read(timeout)
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
        HDLC-decodes each item and returns the payload whose first byte is
        0x3D/0x3E (an NV response), falling back to the first successfully
        decoded frame.

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
            first_decoded = None
            for _ in range(num_data):
                if len(buf) < pos + 4:
                    return None, buf  # partial item header
                item_len = struct.unpack_from('<I', buf, pos)[0]
                pos += 4
                if len(buf) < pos + item_len:
                    return None, buf  # partial frame
                payload = hdlc_decode(bytes(buf[pos:pos + item_len]))
                pos += item_len
                if payload is None:
                    continue
                if first_decoded is None:
                    first_decoded = payload
                if payload and payload[0] in (0x3D, 0x3E):
                    return payload, bytearray(buf[pos:])
            if first_decoded is not None:
                return first_decoded, bytearray(buf[pos:])
            buf = bytearray(buf[pos:])  # whole block consumed, no NV frame
        return None, buf

    def read_response(self, timeout: Optional[float] = None) -> Optional[bytes]:
        """Read and return the payload of the first NV-type (0x3D/0x3E) frame.

        Drains the 5 initial mask notifications, then parses the
        USER_SPACE_DATA_TYPE stream, HDLC-decoding each item. Returns None
        on timeout or I/O error.
        """
        if self.fd is None:
            raise RuntimeError("Diag device not open")
        if timeout is None:
            timeout = self.timeout

        try:
            buf = bytearray(self._drain_mask_notifications(timeout))
            while True:
                payload, buf = self._scan_stream(buf)
                if payload is not None:
                    return payload
                chunk = self._timed_read(timeout)
                if chunk is None or chunk == b'':
                    return None  # timeout, or device closed
                buf.extend(chunk)
        except OSError:
            return None

    def read_nv(self, nv_id: int, slot: int = 0) -> Optional[dict]:
        """Read an NV item from the modem.

        Returns the ``parse_nv_read_response`` dict
        (``{nv_id, sub_id, status, data, success}``) or None on failure.
        """
        if not self.send_command(build_nv_read_cmd(nv_id, slot)):
            return None
        response = self.read_response()
        if response is None:
            return None
        # Echo-validate: a response carrying a DIFFERENT nv_id (e.g. the
        # other band's reply surfacing via _scan_stream's fallback) must
        # not be attributed to this request.
        return parse_nv_read_response(response, nv_id, slot)

    def write_nv(self, nv_id: int, data: bytes, slot: int = 0) -> bool:
        """Write an NV item; True when the 0x3E echo reports status 0."""
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
        SM8250); each is parsed as an 8-byte little-endian bitmask.
        """
        lte_result = self.read_nv(NV_LTE_BAND_PREF, slot)
        nr_result = self.read_nv(NV_NR5G_BAND_PREF, slot)

        lte_bands = []
        nr_bands = []

        if lte_result and lte_result['success'] and len(lte_result['data']) >= 8:
            mask = struct.unpack('<Q', lte_result['data'][:8])[0]
            lte_bands = band_bitmask_to_list(mask)

        if nr_result and nr_result['success'] and len(nr_result['data']) >= 8:
            mask = struct.unpack('<Q', nr_result['data'][:8])[0]
            nr_bands = band_bitmask_to_list(mask)

        return {
            'lte_bands': lte_bands,
            'nr_bands': nr_bands,
        }

    def set_band_config(self, lte_bands: list, nr_bands: list, slot: int = 0) -> bool:
        """Write LTE/NR band configuration.

        Packs each band list into an 8-byte little-endian bitmask and writes
        NV 0x06828 (LTE) and NV 0x06946 (NR). Returns True only if both
        writes succeeded.
        """
        lte_mask = band_list_to_bitmask(lte_bands)
        nr_mask = band_list_to_bitmask(nr_bands)

        lte_ok = self.write_nv(NV_LTE_BAND_PREF, struct.pack('<Q', lte_mask), slot)
        nr_ok = self.write_nv(NV_NR5G_BAND_PREF, struct.pack('<Q', nr_mask), slot)

        return lte_ok and nr_ok


# Convenience functions
def read_bands(device: str = "/dev/diag") -> dict:
    """Read the band configuration from the modem.

    Returns a dict with 'lte_bands' and 'nr_bands' lists.
    """
    with DiagClient(device) as client:
        return client.get_band_config()


def write_bands(lte_bands: list, nr_bands: list, device: str = "/dev/diag") -> bool:
    """Write the band configuration to the modem.

    Returns True if all writes succeeded.
    """
    with DiagClient(device) as client:
        return client.set_band_config(lte_bands, nr_bands)
