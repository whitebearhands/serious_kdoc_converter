"""BIFF8 Shared String Table 디코더.

SST + 후속 CONTINUE 레코드를 결합 버퍼로 만들어 XLUnicodeRichExtendedString을
순차 디코딩. 각 CONTINUE 경계의 첫 바이트는 새 flags 바이트로 재해석한다.
"""

from __future__ import annotations
import struct
from typing import Optional

from .record import BiffRecord, OP_SST, combine_with_continue
from .encoding import decode_utf16le


def _pad_to_utf16(compressed: bytes) -> bytes:
    out = bytearray(len(compressed) * 2)
    for i, b in enumerate(compressed):
        out[i * 2] = b
    return bytes(out)


def _parse_string(
    buf: bytes, offset: int, segments: list[int]
) -> Optional[tuple[str, int]]:
    """단일 XLUnicodeRichExtendedString 디코딩 → (text, consumed_bytes)."""
    if offset + 3 > len(buf):
        return None

    cch = struct.unpack_from("<H", buf, offset)[0]
    flags = buf[offset + 2]
    off = offset + 3

    high_byte = bool(flags & 0x01)
    ext_st = bool(flags & 0x04)
    rich_st = bool(flags & 0x08)

    c_run = 0
    cb_ext_rst = 0
    if rich_st:
        if off + 2 > len(buf):
            return None
        c_run = struct.unpack_from("<H", buf, off)[0]
        off += 2
    if ext_st:
        if off + 4 > len(buf):
            return None
        cb_ext_rst = struct.unpack_from("<I", buf, off)[0]
        off += 4

    # 문자 데이터 읽기 — CONTINUE 경계마다 새 flags 재해석
    char_chunks: list[bytes] = []
    chars_read = 0

    while chars_read < cch:
        next_boundary = next((s for s in segments if s > off), len(buf))

        remain_chars = cch - chars_read
        bytes_per_char = 2 if high_byte else 1
        bytes_avail = next_boundary - off
        chars_in_run = min(remain_chars, bytes_avail // bytes_per_char)
        bytes_to_read = chars_in_run * bytes_per_char

        if bytes_to_read > 0:
            slc = buf[off:off + bytes_to_read]
            char_chunks.append(slc if high_byte else _pad_to_utf16(slc))
            off += bytes_to_read
            chars_read += chars_in_run

        if chars_read < cch:
            # CONTINUE 경계 — 새 flags 바이트
            if off >= len(buf):
                return None
            flags = buf[off]
            high_byte = bool(flags & 0x01)
            off += 1

    text = decode_utf16le(b"".join(char_chunks))

    if rich_st:
        off += 4 * c_run
    if ext_st:
        off += cb_ext_rst

    if off > len(buf):
        off = len(buf)

    return text, off - offset


def decode_sst(records: list[BiffRecord]) -> list[str]:
    """레코드 배열에서 SST를 찾아 디코딩. 없으면 빈 리스트."""
    sst_index = next((i for i, r in enumerate(records) if r.opcode == OP_SST), -1)
    if sst_index < 0:
        return []

    combined, segments, _ = combine_with_continue(records, sst_index)
    if len(combined) < 8:
        return []

    cst_unique = struct.unpack_from("<I", combined, 4)[0]

    strings: list[str] = []
    off = 8
    for _ in range(cst_unique):
        if off >= len(combined):
            break
        result = _parse_string(combined, off, segments)
        if result is None:
            break
        text, consumed = result
        strings.append(text)
        off += consumed

    return strings
