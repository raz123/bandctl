"""Qualcomm Diag Protocol - HDLC framing and CRC handling.

Matches the kernel-verified protocol contract (drivers/char/diag on the
4.19.325-aptusitu kernel, docs/protocol_kernel.md):

- HDLC frame layout: ``[payload][~crc16 low][~crc16 high][0x7E]`` -- no
  leading flag; escape 0x7D/0x7E as 0x7D + (byte ^ 0x20).
- CRC-16/CCITT reflected (poly 0x8408, seed 0xFFFF); the emitted footer is
  the complement, low byte first.
- NV read/write command building and response parsing (status sits at
  offset 5, *before* data_len at 6-7, data at 8).
"""
import struct
from typing import Optional

# HDLC constants
HDLC_FLAG = 0x7E
HDLC_ESCAPE = 0x7D
HDLC_ESCAPE_XOR = 0x20

# Diag command codes (legacy NV commands, forwarded verbatim to the modem)
DIAG_CMD_NV_READ = 0x3D  # DIAG_NV_READ_F
DIAG_CMD_NV_WRITE = 0x3E  # DIAG_NV_WRITE_F

# NV items (Qualcomm standard)
NV_LTE_BAND_PREF = 0x06828  # LTE band preference
NV_NR5G_BAND_PREF = 0x06946  # NR band preference (SM8250)


def crc_ccitt(data: bytes) -> int:
    """Return the RAW kernel CRC-16/CCITT of ``data``.

    Reflected CRC-16/CCITT: polynomial 0x8408, seed 0xFFFF, NO final
    complement -- byte-for-byte the same result as the kernel's
    ``crc_ccitt()`` in lib/crc-ccitt.c (the table the diag HDLC layer uses,
    entry[128] == 0x8408). Callers that build or check a frame footer apply
    ``~crc & 0xFFFF`` themselves and emit it low byte first.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return crc & 0xFFFF


def hdlc_encode(payload: bytes) -> bytes:
    """Encode ``payload`` into a canonical diag HDLC frame.

    Layout: ``[payload][~crc low][~crc high][0x7E]``. No leading flag (the
    kernel's encoder never emits one; a stray leading 0x7E is only tolerated
    at decode time). Bytes 0x7D/0x7E in both the payload and the CRC bytes
    are escaped as 0x7D followed by ``byte ^ 0x20``. The CRC is computed
    over the unescaped payload.
    """
    crc = (~crc_ccitt(payload)) & 0xFFFF
    body = payload + bytes((crc & 0xFF, crc >> 8))  # low byte first

    frame = bytearray()
    for byte in body:
        if byte == HDLC_FLAG or byte == HDLC_ESCAPE:
            frame.append(HDLC_ESCAPE)
            frame.append(byte ^ HDLC_ESCAPE_XOR)
        else:
            frame.append(byte)
    frame.append(HDLC_FLAG)
    return bytes(frame)


def hdlc_decode(frame: bytes) -> Optional[bytes]:
    """Decode an HDLC frame ``[payload][crc_lo][crc_hi][0x7E]``.

    Tolerates stray leading 0x7E flags at frame start (the kernel decoder
    accepts one at message start). Verifies the CRC: ``crc_ccitt(payload) ^
    0xFFFF`` must equal the little-endian uint16 before the trailing 0x7E.

    Returns the decoded payload, or None on any malformation/CRC failure.
    """
    if not frame:
        return None

    data = bytearray()
    i = 0
    n = len(frame)
    # Tolerate stray leading flags (non-canonical, but accepted by the kernel)
    while i < n and frame[i] == HDLC_FLAG:
        i += 1

    while i < n:
        byte = frame[i]
        if byte == HDLC_ESCAPE:
            i += 1
            if i >= n:
                return None
            data.append(frame[i] ^ HDLC_ESCAPE_XOR)
        elif byte == HDLC_FLAG:
            break  # end of frame (trailing terminator)
        else:
            data.append(byte)
        i += 1

    # data == payload + 2 CRC bytes (the trailing 0x7E was consumed by `break`)
    if len(data) < 3:
        return None

    payload = bytes(data[:-2])
    received_crc = data[-2] | (data[-1] << 8)
    if (crc_ccitt(payload) ^ 0xFFFF) != received_crc:
        return None
    return payload


def build_nv_read_cmd(nv_id: int, slot: int = 0) -> bytes:
    """Build a 12-byte NV read command (DIAG_NV_READ_F = 0x3D).

    Layout: ``0x3D + nv_id LE + sub_id LE + 7 pad bytes`` (12 bytes total,
    the classic QCDM layout the modem accepts).
    """
    payload = bytearray(12)
    payload[0] = DIAG_CMD_NV_READ
    struct.pack_into('<H', payload, 1, nv_id)
    struct.pack_into('<H', payload, 3, slot)  # sub_id (0 = primary)
    return bytes(payload)


def build_nv_write_cmd(nv_id: int, data: bytes, slot: int = 0) -> bytes:
    """Build an NV write command (DIAG_NV_WRITE_F = 0x3E).

    Layout: ``0x3E + nv_id LE + sub_id LE + data_len LE + data``
    (7 + len(data) bytes).
    """
    header = bytearray(7)
    header[0] = DIAG_CMD_NV_WRITE
    struct.pack_into('<H', header, 1, nv_id)
    struct.pack_into('<H', header, 3, slot)
    struct.pack_into('<H', header, 5, len(data))
    return bytes(header) + data


def parse_nv_read_response(
    response: bytes,
    expected_nv_id: Optional[int] = None,
    expected_sub_id: Optional[int] = None,
) -> Optional[dict]:
    """Parse an NV read response (nv_read_rsp_type).

    Layout: ``rsp 0x3D | nv_id LE | sub_id LE | status | data_len LE | data``
    -- status sits at offset 5, *before* data_len at 6-7 and data at 8.

    ``expected_nv_id`` / ``expected_sub_id`` mirror the caller's request:
    when given, the echoed values in the response must match or None is
    returned. This stops a stray status frame (e.g. the _scan_stream
    fallback surfacing the LTE reply for an NR read, or vice versa) from
    being misattributed to the caller's request.

    Returns ``{nv_id, sub_id, status, data, success}`` or None on error.
    """
    if len(response) < 8 or response[0] != DIAG_CMD_NV_READ:
        return None

    nv_id = struct.unpack('<H', response[1:3])[0]
    sub_id = struct.unpack('<H', response[3:5])[0]
    if expected_nv_id is not None and nv_id != expected_nv_id:
        return None
    if expected_sub_id is not None and sub_id != expected_sub_id:
        return None
    status = response[5]
    data_len = struct.unpack('<H', response[6:8])[0]

    if len(response) < 8 + data_len:
        return None

    data = response[8:8 + data_len]
    return {
        'nv_id': nv_id,
        'sub_id': sub_id,
        'status': status,
        'data': data,
        'success': status == 0,
    }


def parse_nv_write_response(
    response: bytes,
    expected_nv_id: Optional[int] = None,
    expected_sub_id: Optional[int] = None,
) -> Optional[dict]:
    """Parse an NV write response (nv_write_rsp_type).

    Layout: ``rsp 0x3E | nv_id LE | sub_id LE | status`` -- 6 bytes total,
    status at offset 5.

    ``expected_nv_id`` / ``expected_sub_id`` mirror the caller's request:
    when given, the echoed values in the response must match or None is
    returned (same misattribution guard as parse_nv_read_response).

    Returns ``{nv_id, sub_id, status, success}`` or None on error.
    """
    if len(response) < 6 or response[0] != DIAG_CMD_NV_WRITE:
        return None

    nv_id = struct.unpack('<H', response[1:3])[0]
    sub_id = struct.unpack('<H', response[3:5])[0]
    if expected_nv_id is not None and nv_id != expected_nv_id:
        return None
    if expected_sub_id is not None and sub_id != expected_sub_id:
        return None
    status = response[5]
    return {
        'nv_id': nv_id,
        'sub_id': sub_id,
        'status': status,
        'success': status == 0,
    }


def band_bitmask_to_list(mask: int, max_band: int = 79) -> list:
    """Convert a band bitmask to the list of enabled band numbers.

    Bit (band - 1) set means the band is enabled (Bit 0 = Band 1).
    """
    bands = []
    for i in range(max_band):
        if mask & (1 << i):
            bands.append(i + 1)
    return bands


def band_list_to_bitmask(bands: list) -> int:
    """Convert a list of band numbers to a bitmask (Band N -> bit N-1).

    Ponytail: this builds a 64-bit mask, which is fine for LTE (bands 1-64),
    but NR bands 77/78 fall outside the 64-bit ceiling and are silently
    dropped here. If NR 77/78 support is ever needed, use an
    arbitrary-precision int end to end and stop packing the mask into 8
    bytes (struct '<Q') on the wire.
    """
    mask = 0
    for band in bands:
        if 1 <= band <= 64:
            mask |= (1 << (band - 1))
    return mask
