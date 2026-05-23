"""BIFF8 Worksheet 서브스트림의 셀 레코드 → RawCell 변환."""

from __future__ import annotations
import struct
from dataclasses import dataclass, field
from typing import Optional, Union

from .record import (
    BiffRecord,
    OP_BOF, OP_EOF,
    OP_NUMBER, OP_RK, OP_MULRK, OP_LABELSST, OP_LABEL,
    OP_FORMULA, OP_STRING, OP_BOOLERR, OP_BLANK, OP_MULBLANK, OP_MERGECELLS,
    decode_rk, decode_mul_rk, read_cell_header,
)
from .encoding import decode_utf16le

CellValue = Union[str, int, float, bool, None]


@dataclass
class RawCell:
    row: int
    col: int
    value: CellValue


@dataclass
class MergeRange:
    r1: int
    c1: int
    r2: int
    c2: int


@dataclass
class RawSheet:
    bof_offset: int
    cells: list[RawCell] = field(default_factory=list)
    merges: list[MergeRange] = field(default_factory=list)


def _error_code_to_text(code: int) -> str:
    return {
        0x00: "#NULL!",
        0x07: "#DIV/0!",
        0x0F: "#VALUE!",
        0x17: "#REF!",
        0x1D: "#NAME?",
        0x24: "#NUM!",
        0x2A: "#N/A",
    }.get(code, f"#ERR{code}")


def _decode_label_string(data: bytes) -> str:
    """Label 레코드 문자열 디코딩. 구조: row(2) col(2) ixfe(2) cch(2) flags(1) rgb."""
    if len(data) < 9:
        return ""
    cch = struct.unpack_from("<H", data, 6)[0]
    flags = data[8]
    high_byte = bool(flags & 0x01)
    start = 9

    if high_byte:
        end = min(start + cch * 2, len(data))
        return decode_utf16le(data[start:end])
    else:
        end = min(start + cch, len(data))
        slc = data[start:end]
        padded = bytearray(len(slc) * 2)
        for i, b in enumerate(slc):
            padded[i * 2] = b
        return decode_utf16le(bytes(padded))


def _decode_formula_result(val: bytes) -> tuple[bool, CellValue]:
    """(is_string_ref, value). is_string_ref=True → 직후 String 레코드 참조."""
    if len(val) < 8:
        return False, None
    tail = struct.unpack_from("<H", val, 6)[0]
    if tail == 0xFFFF:
        code = val[0]
        if code == 0x00:
            return True, None   # string ref
        if code == 0x01:
            return False, val[2] == 1
        if code == 0x02:
            return False, _error_code_to_text(val[2])
        return False, None      # empty
    num = struct.unpack_from("<d", val, 0)[0]
    return False, num


def _decode_formula_string_record(data: bytes) -> str:
    """Formula 직후 String 레코드. 구조: cch(2) flags(1) rgb."""
    if len(data) < 3:
        return ""
    cch = struct.unpack_from("<H", data, 0)[0]
    flags = data[2]
    high_byte = bool(flags & 0x01)
    start = 3

    if high_byte:
        end = min(start + cch * 2, len(data))
        return decode_utf16le(data[start:end])
    else:
        end = min(start + cch, len(data))
        slc = data[start:end]
        padded = bytearray(len(slc) * 2)
        for i, b in enumerate(slc):
            padded[i * 2] = b
        return decode_utf16le(bytes(padded))


def extract_sheet_cells(
    records: list[BiffRecord], bof_index: int, sst: list[str]
) -> tuple[RawSheet, int]:
    """단일 Worksheet 서브스트림 (BOF~EOF) 셀 레코드 추출."""
    cells: list[RawCell] = []
    merges: list[MergeRange] = []
    bof_offset = records[bof_index].offset

    i = bof_index + 1
    while i < len(records):
        rec = records[i]
        if rec.opcode == OP_EOF:
            i += 1
            break
        if rec.opcode == OP_BOF:
            break

        op = rec.opcode
        data = rec.data

        if op == OP_NUMBER:
            h = read_cell_header(data)
            if h and len(data) >= 14:
                row, col, _ = h
                val = struct.unpack_from("<d", data, 6)[0]
                cells.append(RawCell(row=row, col=col, value=val))

        elif op == OP_RK:
            h = read_cell_header(data)
            if h and len(data) >= 10:
                row, col, _ = h
                rk = struct.unpack_from("<I", data, 6)[0]
                cells.append(RawCell(row=row, col=col, value=decode_rk(rk)))

        elif op == OP_MULRK:
            m = decode_mul_rk(data)
            if m:
                row, mul_cells = m
                for col, _ixfe, val in mul_cells:
                    cells.append(RawCell(row=row, col=col, value=val))

        elif op == OP_LABELSST:
            h = read_cell_header(data)
            if h and len(data) >= 10:
                row, col, _ = h
                isst = struct.unpack_from("<I", data, 6)[0]
                cells.append(RawCell(row=row, col=col, value=sst[isst] if isst < len(sst) else ""))

        elif op == OP_LABEL:
            h = read_cell_header(data)
            if h:
                row, col, _ = h
                cells.append(RawCell(row=row, col=col, value=_decode_label_string(data)))

        elif op == OP_FORMULA:
            h = read_cell_header(data)
            if h and len(data) >= 14:
                row, col, _ = h
                is_str_ref, val = _decode_formula_result(data[6:14])
                if is_str_ref:
                    nxt = records[i + 1] if i + 1 < len(records) else None
                    if nxt and nxt.opcode == OP_STRING:
                        cells.append(RawCell(row=row, col=col, value=_decode_formula_string_record(nxt.data)))
                        i += 1
                    else:
                        cells.append(RawCell(row=row, col=col, value=""))
                else:
                    cells.append(RawCell(row=row, col=col, value=val))

        elif op == OP_BOOLERR:
            h = read_cell_header(data)
            if h and len(data) >= 8:
                row, col, _ = h
                v = data[6]
                is_err = data[7] == 1
                if is_err:
                    cells.append(RawCell(row=row, col=col, value=_error_code_to_text(v)))
                else:
                    cells.append(RawCell(row=row, col=col, value=v == 1))

        elif op == OP_MERGECELLS:
            if len(data) >= 2:
                cmcs = struct.unpack_from("<H", data, 0)[0]
                off = 2
                for _ in range(cmcs):
                    if off + 8 > len(data):
                        break
                    r1 = struct.unpack_from("<H", data, off)[0]
                    r2 = struct.unpack_from("<H", data, off + 2)[0]
                    c1 = struct.unpack_from("<H", data, off + 4)[0]
                    c2 = struct.unpack_from("<H", data, off + 6)[0]
                    merges.append(MergeRange(r1=r1, c1=c1, r2=r2, c2=c2))
                    off += 8

        i += 1

    return RawSheet(bof_offset=bof_offset, cells=cells, merges=merges), i
