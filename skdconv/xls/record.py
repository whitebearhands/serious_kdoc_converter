"""BIFF8 (Excel 97-2003) 레코드 리더."""

from __future__ import annotations
import struct
from dataclasses import dataclass
from typing import Optional

# ─── Opcode 상수 ─────────────────────────────────────

OP_BOF         = 0x0809
OP_EOF         = 0x000A
OP_CONTINUE    = 0x003C

OP_BOUNDSHEET8 = 0x0085
OP_SST         = 0x00FC
OP_EXTSST      = 0x00FF
OP_CODEPAGE    = 0x0042
OP_DATE1904    = 0x0022
OP_FILEPASS    = 0x002F

OP_NUMBER      = 0x0203
OP_RK          = 0x027E
OP_MULRK       = 0x00BD
OP_LABELSST    = 0x00FD
OP_LABEL       = 0x0204
OP_FORMULA     = 0x0006
OP_STRING      = 0x0207
OP_BOOLERR     = 0x0205
OP_BLANK       = 0x0201
OP_MULBLANK    = 0x00BE
OP_MERGECELLS  = 0x00E5
OP_ROW         = 0x0208

DT_GLOBALS   = 0x0005
DT_WORKSHEET = 0x0010
DT_CHART     = 0x0020
DT_MACRO     = 0x0040

_MAX_RECORDS = 1_000_000


@dataclass
class BiffRecord:
    opcode: int
    data: bytes
    offset: int


def read_records(stream: bytes) -> list[BiffRecord]:
    out: list[BiffRecord] = []
    offset = 0

    while offset + 4 <= len(stream) and len(out) < _MAX_RECORDS:
        rec_offset = offset
        opcode = struct.unpack_from("<H", stream, offset)[0]
        length = struct.unpack_from("<H", stream, offset + 2)[0]
        offset += 4

        if offset + length > len(stream):
            data = stream[offset:]
            out.append(BiffRecord(opcode=opcode, data=data, offset=rec_offset))
            break

        data = stream[offset:offset + length]
        out.append(BiffRecord(opcode=opcode, data=data, offset=rec_offset))
        offset += length

    return out


def combine_with_continue(
    records: list[BiffRecord], start_index: int
) -> tuple[bytes, list[int], int]:
    """SST 등 CONTINUE로 분할된 레코드 결합.

    Returns (combined_data, segment_boundaries, next_index).
    segment_boundaries: 각 청크 끝의 누적 오프셋.
    """
    first = records[start_index]
    chunks: list[bytes] = [first.data]
    segments: list[int] = [len(first.data)]
    i = start_index + 1
    total = len(first.data)

    while i < len(records) and records[i].opcode == OP_CONTINUE:
        chunk = records[i].data
        chunks.append(chunk)
        total += len(chunk)
        segments.append(total)
        i += 1

    return b"".join(chunks), segments, i


def decode_bof(data: bytes) -> Optional[tuple[int, int]]:
    """(vers, dt) or None."""
    if len(data) < 4:
        return None
    vers = struct.unpack_from("<H", data, 0)[0]
    dt = struct.unpack_from("<H", data, 2)[0]
    return vers, dt


def decode_rk(rk: int) -> float:
    """RK 32bit 압축 숫자 디코딩."""
    f_div100 = (rk & 0x01) != 0
    f_int = (rk & 0x02) != 0

    if f_int:
        # 30bit 부호확장 (rk를 signed 32-bit로 해석 후 >> 2)
        rk_s32 = rk if rk < 0x80000000 else rk - 0x100000000
        num = float(rk_s32 >> 2)
    else:
        # double 복원: rk[31..2] → double 비트[63..34]
        high32 = rk & 0xFFFFFFFC
        buf = struct.pack("<II", 0, high32)
        num = struct.unpack("<d", buf)[0]

    return num / 100 if f_div100 else num


def decode_mul_rk(data: bytes) -> Optional[tuple[int, list[tuple[int, int, float]]]]:
    """MulRk 디코딩 → (row, [(col, ixfe, value)])."""
    if len(data) < 6:
        return None
    row = struct.unpack_from("<H", data, 0)[0]
    col_first = struct.unpack_from("<H", data, 2)[0]
    col_last = struct.unpack_from("<H", data, len(data) - 2)[0]
    count = col_last - col_first + 1
    if count <= 0:
        return row, []

    cells: list[tuple[int, int, float]] = []
    off = 4
    for i in range(count):
        if off + 6 > len(data) - 2:
            break
        ixfe = struct.unpack_from("<H", data, off)[0]
        rk = struct.unpack_from("<I", data, off + 2)[0]
        cells.append((col_first + i, ixfe, decode_rk(rk)))
        off += 6

    return row, cells


def read_cell_header(data: bytes) -> Optional[tuple[int, int, int]]:
    """(row, col, ixfe) or None."""
    if len(data) < 6:
        return None
    row = struct.unpack_from("<H", data, 0)[0]
    col = struct.unpack_from("<H", data, 2)[0]
    ixfe = struct.unpack_from("<H", data, 4)[0]
    return row, col, ixfe
