"""XLS (BIFF8) 파서 — Workbook 스트림 → IRBlock[].

흐름:
  1. cfb_lenient로 OLE2 컨테이너 → "Workbook" 스트림 추출
  2. read_records로 BIFF 레코드 시퀀스 파싱
  3. Globals 서브스트림: BoundSheet8 수집 + SST 디코딩
  4. 각 시트 BOF 인덱스 찾기 → extract_sheet_cells
  5. RawSheet → heading + IRTable 블록 변환
"""

from __future__ import annotations
import struct
from dataclasses import dataclass
from typing import Optional

from ..utils import SKDConvError
from ..types import IRBlock, CellContext, DocumentMetadata, InternalParseResult, ParseOptions, ParseWarning
from ..table.builder import build_table, blocks_to_markdown
from ..hwp5.cfb_lenient import parse_lenient_cfb
from .record import (
    BiffRecord,
    OP_BOF, OP_EOF, OP_BOUNDSHEET8, OP_FILEPASS, OP_CODEPAGE,
    DT_GLOBALS, DT_WORKSHEET,
    read_records, decode_bof,
)
from .sst import decode_sst
from .cell import extract_sheet_cells, RawSheet, CellValue
from .encoding import decode_utf16le

_MAX_SHEETS = 100
_MAX_ROWS   = 100_000
_MAX_COLS   = 1_000


# ─── BoundSheet8 ──────────────────────────────────────

@dataclass
class _BoundSheet:
    name: str
    lbply_pos: int  # Workbook 스트림 절대 오프셋
    dt: int         # 0=Worksheet, 1=Macro, 2=Chart


def _decode_bound_sheet(data: bytes) -> Optional[_BoundSheet]:
    """BoundSheet8: lbPlyPos(4) hsState(1) dt(1) stName(ShortXLUnicodeString)."""
    if len(data) < 8:
        return None
    lbply_pos = struct.unpack_from("<I", data, 0)[0]
    dt = data[5]
    cch = data[6]
    flags = data[7]
    high_byte = bool(flags & 0x01)
    start = 8

    if high_byte:
        end = min(start + cch * 2, len(data))
        name = decode_utf16le(data[start:end])
    else:
        end = min(start + cch, len(data))
        slc = data[start:end]
        padded = bytearray(len(slc) * 2)
        for i, b in enumerate(slc):
            padded[i * 2] = b
        name = decode_utf16le(bytes(padded))

    return _BoundSheet(name=name, lbply_pos=lbply_pos, dt=dt)


# ─── Globals 처리 ──────────────────────────────────────

def _process_globals(
    records: list[BiffRecord],
) -> tuple[list[_BoundSheet], list[str], int, bool, int]:
    """→ (sheets, sst, code_page, encrypted, end_index)."""
    if not records or records[0].opcode != OP_BOF:
        raise SKDConvError("XLS: 첫 레코드가 BOF가 아님")

    bof = decode_bof(records[0].data)
    if bof is None or bof[1] != DT_GLOBALS:
        raise SKDConvError("XLS: Globals 서브스트림 BOF 누락")

    sheets: list[_BoundSheet] = []
    code_page = 1200
    encrypted = False

    i = 1
    while i < len(records):
        r = records[i]
        if r.opcode == OP_EOF:
            i += 1
            break
        if r.opcode == OP_BOUNDSHEET8:
            bs = _decode_bound_sheet(r.data)
            if bs:
                sheets.append(bs)
        elif r.opcode == OP_CODEPAGE and len(r.data) >= 2:
            code_page = struct.unpack_from("<H", r.data, 0)[0]
        elif r.opcode == OP_FILEPASS:
            encrypted = True
        i += 1

    globals_records = records[:i]
    sst = decode_sst(globals_records)

    return sheets, sst, code_page, encrypted, i


# ─── 시트 BOF 인덱스 찾기 ──────────────────────────────

def _find_sheet_bof_index(records: list[BiffRecord], lbply_pos: int) -> int:
    # 정확한 offset 매칭 우선
    for idx, r in enumerate(records):
        if r.opcode == OP_BOF and r.offset == lbply_pos:
            return idx

    # 폴백: 두 번째 BOF (첫 번째는 Globals)
    bof_indices = [i for i, r in enumerate(records) if r.opcode == OP_BOF]
    if len(bof_indices) > 1:
        return bof_indices[1]
    return -1


# ─── RawSheet → IRBlock[] ──────────────────────────────

def _cell_value_to_text(v: CellValue) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        if v == int(v) and not (v != v):  # integer-valued float, not NaN
            return str(int(v))
        cleaned = f"{v:.15g}"
        return cleaned
    return str(v)


def _sheet_to_blocks(sheet_name: str, sheet: RawSheet, sheet_index: int) -> list[IRBlock]:
    blocks: list[IRBlock] = []

    if sheet_name:
        blocks.append(IRBlock(type="heading", text=sheet_name, level=2, page_number=sheet_index + 1))

    if not sheet.cells:
        return blocks

    max_row = -1
    max_col = -1
    for c in sheet.cells:
        if c.row > max_row:
            max_row = c.row
        if c.col > max_col:
            max_col = c.col
    for m in sheet.merges:
        if m.r2 > max_row:
            max_row = m.r2
        if m.c2 > max_col:
            max_col = m.c2

    if max_row < 0 or max_col < 0:
        return blocks

    max_row = min(max_row, _MAX_ROWS - 1)
    max_col = min(max_col, _MAX_COLS - 1)

    # 그리드 채우기
    grid: list[list[str]] = [[""] * (max_col + 1) for _ in range(max_row + 1)]
    for c in sheet.cells:
        if c.row <= max_row and c.col <= max_col:
            grid[c.row][c.col] = _cell_value_to_text(c.value)

    # 병합 맵
    merge_map: dict[tuple[int, int], tuple[int, int]] = {}  # (r,c) → (colSpan, rowSpan)
    merge_skip: set[tuple[int, int]] = set()
    for m in sheet.merges:
        r1 = min(m.r1, max_row)
        c1 = min(m.c1, max_col)
        r2 = min(m.r2, max_row)
        c2 = min(m.c2, max_col)
        merge_map[(r1, c1)] = (c2 - c1 + 1, r2 - r1 + 1)
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                if r != r1 or c != c1:
                    merge_skip.add((r, c))

    # 유효 행 트리밍
    first_row = -1
    last_row = -1
    for r in range(max_row + 1):
        if any(v for v in grid[r]):
            if first_row == -1:
                first_row = r
            last_row = r

    if first_row == -1:
        return blocks

    # CellContext[][] 빌드
    cell_rows: list[list[CellContext]] = []
    for r in range(first_row, last_row + 1):
        row: list[CellContext] = []
        for c in range(max_col + 1):
            if (r, c) in merge_skip:
                continue
            col_span, row_span = merge_map.get((r, c), (1, 1))
            row.append(CellContext(text=grid[r][c], col_span=col_span, row_span=row_span))
        cell_rows.append(row)

    if cell_rows:
        table = build_table(cell_rows)
        if table.rows > 0:
            blocks.append(IRBlock(type="table", table=table, page_number=sheet_index + 1))

    return blocks


# ─── 메인 ──────────────────────────────────────────────

def parse_xls_document(
    data: bytes, options: Optional[ParseOptions] = None
) -> InternalParseResult:
    # 1. OLE2 컨테이너 → Workbook 스트림
    try:
        cfb = parse_lenient_cfb(data)
    except Exception as e:
        raise SKDConvError(f"XLS: OLE2 시그니처 검증 실패 — {e}") from e

    wb = cfb.find_stream("/Workbook") or cfb.find_stream("/Book")
    if not wb:
        raise SKDConvError("XLS: Workbook 스트림이 없음 (BIFF5 또는 비표준 파일)")

    # 2. BIFF 레코드 시퀀스
    records = read_records(wb)
    if not records:
        raise SKDConvError("XLS: 시그니처 레코드가 없음 (Workbook 스트림 손상)")

    # 3. BIFF 버전 체크
    first_bof = decode_bof(records[0].data)
    if first_bof and first_bof[0] != 0x0600:
        raise SKDConvError(f"XLS: BIFF8(0x0600)만 지원 — 본 파일은 0x{first_bof[0]:04x}")

    # 4. Globals 처리
    sheets, sst, _code_page, encrypted, _end_idx = _process_globals(records)
    warnings: list[ParseWarning] = []

    if encrypted:
        return InternalParseResult(
            markdown="",
            blocks=[],
            metadata=DocumentMetadata(page_count=len(sheets)),
            warnings=[ParseWarning(message="XLS 파일이 암호화되어 있어 파싱할 수 없습니다", code="PARTIAL_PARSE")],
        )

    # 5. 페이지/시트 필터
    total_sheets = min(len(sheets), _MAX_SHEETS)
    page_filter: Optional[set[int]] = None
    if options and options.pages:
        from ..page_range import parse_page_range
        page_filter = parse_page_range(options.pages, total_sheets)

    # 6. 각 시트 처리
    all_blocks: list[IRBlock] = []
    for i in range(total_sheets):
        if page_filter and (i + 1) not in page_filter:
            continue
        meta = sheets[i]
        if meta.dt != 0:  # Worksheet만 처리
            continue

        bof_idx = _find_sheet_bof_index(records, meta.lbply_pos)
        if bof_idx < 0:
            warnings.append(ParseWarning(
                page=i + 1,
                message=f"시트 \"{meta.name}\" BOF를 찾을 수 없음 (lbPlyPos={meta.lbply_pos})",
                code="PARTIAL_PARSE",
            ))
            continue

        sheet_bof = decode_bof(records[bof_idx].data)
        if sheet_bof and sheet_bof[1] != DT_WORKSHEET:
            continue

        try:
            sheet, _ = extract_sheet_cells(records, bof_idx, sst)
            blocks = _sheet_to_blocks(meta.name, sheet, i)
            all_blocks.extend(blocks)
        except Exception as e:
            warnings.append(ParseWarning(
                page=i + 1,
                message=f"시트 \"{meta.name}\" 파싱 실패: {e}",
                code="PARTIAL_PARSE",
            ))

    metadata = DocumentMetadata(page_count=total_sheets)

    return InternalParseResult(
        markdown=blocks_to_markdown(all_blocks),
        blocks=all_blocks,
        metadata=metadata,
        warnings=warnings if warnings else None,
    )
