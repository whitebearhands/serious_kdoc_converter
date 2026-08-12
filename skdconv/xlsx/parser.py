"""XLSX (Office Open XML Spreadsheet) 파서

ZIP + XML 구조를 파싱하여 IRBlock[]로 변환.
각 시트 → heading(시트명) + table(데이터) 블록.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Dict, List, Optional, Set, Tuple

import lxml.etree as ET

from ..types import (
    CellContext, DocumentMetadata, InternalParseResult, IRBlock,
    ParseOptions, ParseWarning,
)
from ..utils import SKDConvErrorError, precheck_zip_size, strip_dtd
from ..table.builder import blocks_to_markdown, build_table
from ..page_range import parse_page_range

MAX_SHEETS = 100
MAX_DECOMPRESS_SIZE = 100 * 1024 * 1024
MAX_ROWS = 10000
MAX_COLS = 200


# ─── XML 헬퍼 ──────────────────────────────────────────

def _tag(el: ET._Element) -> str:
    t = el.tag
    return t.split("}", 1)[1] if "}" in t else t


def _attr(el: ET._Element, name: str) -> Optional[str]:
    for k, v in el.attrib.items():
        k_local = k.split("}", 1)[1] if "}" in k else k
        if k_local == name:
            return v
    return None


def _find_all(parent: ET._Element, local_tag: str) -> List[ET._Element]:
    return [el for el in parent.iter() if _tag(el) == local_tag]


def _text(el: ET._Element) -> str:
    return (el.text or "").strip()


def _parse_xml(text: str) -> ET._Element:
    cleaned = strip_dtd(text)
    return ET.fromstring(cleaned.encode("utf-8"), parser=ET.XMLParser(recover=True))


# ─── 숫자값 정리 ──────────────────────────────────────

def _clean_numeric(raw: str) -> str:
    """부동소수점 아티팩트 정리 (132.30000000000001 → 132.3)."""
    if not re.match(r"^-?\d+\.\d+$", raw):
        return raw
    try:
        num = float(raw)
        if not (num == num):  # NaN
            return raw
        cleaned = f"{float(f'{num:.15g}')}"
        return cleaned
    except ValueError:
        return raw


# ─── 셀 참조 파싱 ──────────────────────────────────────

def _parse_cell_ref(ref: str) -> Optional[Tuple[int, int]]:
    """'A1' → (col=0, row=0), 'AB123' → (col=27, row=122)."""
    m = re.match(r"^([A-Z]+)(\d+)$", ref)
    if not m:
        return None
    col = 0
    for ch in m.group(1):
        col = col * 26 + (ord(ch) - 64)
    return col - 1, int(m.group(2)) - 1


def _parse_merge_ref(ref: str) -> Optional[Tuple[int, int, int, int]]:
    """'A1:C3' → (start_col, start_row, end_col, end_row)."""
    parts = ref.split(":")
    if len(parts) != 2:
        return None
    start = _parse_cell_ref(parts[0])
    end = _parse_cell_ref(parts[1])
    if start is None or end is None:
        return None
    return start[0], start[1], end[0], end[1]


# ─── 공유 문자열 파싱 ──────────────────────────────────

def _parse_shared_strings(xml: str) -> List[str]:
    root = _parse_xml(xml)
    strings = []
    for si in _find_all(root, "si"):
        t_elements = _find_all(si, "t")
        strings.append("".join(el.text or "" for el in t_elements))
    return strings


# ─── 시트 목록 파싱 ─────────────────────────────────────

def _parse_workbook(xml: str) -> List[Dict[str, str]]:
    root = _parse_xml(xml)
    sheets = []
    for el in _find_all(root, "sheet"):
        r_id = _attr(el, "id") or ""  # r:id
        sheets.append({
            "name": _attr(el, "name") or f"Sheet{len(sheets) + 1}",
            "sheetId": _attr(el, "sheetId") or "",
            "rId": r_id,
        })
    return sheets


def _parse_rels(xml: str) -> Dict[str, str]:
    root = _parse_xml(xml)
    result = {}
    for rel in _find_all(root, "Relationship"):
        rel_id = _attr(rel, "Id")
        target = _attr(rel, "Target")
        if rel_id and target:
            result[rel_id] = target
    return result


# ─── 워크시트 파싱 ──────────────────────────────────────

def _parse_worksheet(
    xml: str,
    shared_strings: List[str],
) -> Tuple[List[List[str]], List[Tuple[int, int, int, int]], int, int]:
    """Returns (grid, merges, max_row, max_col)."""
    root = _parse_xml(xml)
    grid: List[List[str]] = []
    max_row = -1
    max_col = -1

    for row_el in _find_all(root, "row"):
        row_num_str = _attr(row_el, "r") or "0"
        try:
            row_idx = int(row_num_str) - 1
        except ValueError:
            continue
        if row_idx < 0 or row_idx >= MAX_ROWS:
            continue

        for cell_el in row_el:
            if _tag(cell_el) != "c":
                continue
            ref = _attr(cell_el, "r")
            if not ref:
                continue
            pos = _parse_cell_ref(ref)
            if pos is None or pos[0] >= MAX_COLS:
                continue
            col_idx, _ = pos

            cell_type = _attr(cell_el, "t") or ""
            v_els = [el for el in cell_el if _tag(el) == "v"]
            f_els = [el for el in cell_el if _tag(el) == "f"]
            is_els = [el for el in cell_el if _tag(el) == "is"]
            value = ""

            if v_els:
                raw = (v_els[0].text or "").strip()
                if cell_type == "s":
                    try:
                        value = shared_strings[int(raw)]
                    except (ValueError, IndexError):
                        value = ""
                elif cell_type == "b":
                    value = "TRUE" if raw == "1" else "FALSE"
                else:
                    value = _clean_numeric(raw)
            elif cell_type == "inlineStr" and is_els:
                t_els = [el for el in is_els[0].iter() if _tag(el) == "t"]
                value = "".join(el.text or "" for el in t_els)

            if not value and f_els:
                value = f"={(f_els[0].text or '').strip()}"

            while len(grid) <= row_idx:
                grid.append([])
            while len(grid[row_idx]) <= col_idx:
                grid[row_idx].append("")
            grid[row_idx][col_idx] = value

            if row_idx > max_row:
                max_row = row_idx
            if col_idx > max_col:
                max_col = col_idx

    # 병합 셀
    merges: List[Tuple[int, int, int, int]] = []
    for el in _find_all(root, "mergeCell"):
        ref = _attr(el, "ref")
        if ref:
            m = _parse_merge_ref(ref)
            if m:
                merges.append(m)

    return grid, merges, max_row, max_col


# ─── 시트 → IRBlock[] 변환 ────────────────────────────

def _sheet_to_blocks(
    sheet_name: str,
    grid: List[List[str]],
    merges: List[Tuple[int, int, int, int]],
    max_row: int,
    max_col: int,
    sheet_index: int,
) -> List[IRBlock]:
    blocks: List[IRBlock] = []

    if sheet_name:
        blocks.append(IRBlock(
            type="heading",
            text=sheet_name,
            level=2,
            page_number=sheet_index + 1,
        ))

    if max_row < 0 or max_col < 0 or not grid:
        return blocks

    # 병합 맵 구성
    merge_map: Dict[str, Tuple[int, int]] = {}  # "r,c" → (col_span, row_span)
    merge_skip: Set[str] = set()
    for sc, sr, ec, er in merges:
        col_span = ec - sc + 1
        row_span = er - sr + 1
        merge_map[f"{sr},{sc}"] = (col_span, row_span)
        for r in range(sr, er + 1):
            for c in range(sc, ec + 1):
                if r != sr or c != sc:
                    merge_skip.add(f"{r},{c}")

    # 유효 행 범위 감지
    first_row = -1
    last_row = -1
    for r in range(max_row + 1):
        row = grid[r] if r < len(grid) else []
        if any(cell for cell in row):
            if first_row == -1:
                first_row = r
            last_row = r
    if first_row == -1:
        return blocks

    cell_rows: List[List[CellContext]] = []
    for r in range(first_row, last_row + 1):
        row: List[CellContext] = []
        for c in range(max_col + 1):
            key = f"{r},{c}"
            if key in merge_skip:
                continue
            text = grid[r][c] if r < len(grid) and c < len(grid[r]) else ""
            span = merge_map.get(key)
            col_span, row_span = span if span else (1, 1)
            row.append(CellContext(text=text, col_span=col_span, row_span=row_span))
        cell_rows.append(row)

    if cell_rows:
        table = build_table(cell_rows)
        if table.rows > 0:
            blocks.append(IRBlock(type="table", table=table, page_number=sheet_index + 1))

    return blocks


# ─── 메타데이터 추출 ────────────────────────────────────

def _parse_core_xml(xml: str) -> DocumentMetadata:
    meta = DocumentMetadata()
    try:
        root = _parse_xml(xml)

        def _get(tag_name: str) -> Optional[str]:
            for el in root.iter():
                if _tag(el) == tag_name:
                    return (el.text or "").strip() or None
            return None

        meta.title = _get("title") or _get("title")
        meta.author = _get("creator")
        meta.description = _get("description")
        meta.created_at = _get("created")
        meta.modified_at = _get("modified")
    except Exception:
        pass
    return meta


# ─── 메인 파서 ─────────────────────────────────────────

def parse_xlsx_document(data: bytes, options: Optional[ParseOptions] = None) -> InternalParseResult:
    precheck_zip_size(data, MAX_DECOMPRESS_SIZE)

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise SKDConvErrorError(f"유효하지 않은 XLSX 파일: {e}") from e

    warnings: List[ParseWarning] = []

    with zf:
        names = set(zf.namelist())

        if "xl/workbook.xml" not in names:
            raise SKDConvErrorError("유효하지 않은 XLSX 파일: xl/workbook.xml이 없습니다")

        # 1. 공유 문자열
        shared_strings: List[str] = []
        if "xl/sharedStrings.xml" in names:
            try:
                shared_strings = _parse_shared_strings(
                    zf.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
                )
            except Exception:
                pass

        # 2. 시트 목록
        sheets = _parse_workbook(
            zf.read("xl/workbook.xml").decode("utf-8", errors="replace")
        )
        if not sheets:
            raise SKDConvErrorError("XLSX 파일에 시트가 없습니다")

        # 3. 관계 매핑
        rels: Dict[str, str] = {}
        if "xl/_rels/workbook.xml.rels" in names:
            try:
                rels = _parse_rels(
                    zf.read("xl/_rels/workbook.xml.rels").decode("utf-8", errors="replace")
                )
            except Exception:
                pass

        # 4. 페이지 필터
        page_filter = parse_page_range(options.pages, len(sheets)) if options and options.pages else None

        # 5. 각 시트 파싱
        blocks: List[IRBlock] = []
        processed = min(len(sheets), MAX_SHEETS)

        for i in range(processed):
            if page_filter and (i + 1) not in page_filter:
                continue

            sheet = sheets[i]
            if options and options.on_progress:
                options.on_progress(i + 1, processed)

            # 시트 파일 경로 결정
            sheet_path = rels.get(sheet["rId"], "")
            if sheet_path:
                if not sheet_path.startswith("xl/") and not sheet_path.startswith("/"):
                    sheet_path = f"xl/{sheet_path}"
                elif sheet_path.startswith("/"):
                    sheet_path = sheet_path[1:]
            else:
                sheet_path = f"xl/worksheets/sheet{i + 1}.xml"

            if sheet_path not in names:
                warnings.append(ParseWarning(
                    page=i + 1,
                    message=f'시트 "{sheet["name"]}" 파일을 찾을 수 없습니다: {sheet_path}',
                    code="PARTIAL_PARSE",
                ))
                continue

            try:
                xml = zf.read(sheet_path).decode("utf-8", errors="replace")
                grid, merges, max_row, max_col = _parse_worksheet(xml, shared_strings)
                sheet_blocks = _sheet_to_blocks(
                    sheet["name"], grid, merges, max_row, max_col, i
                )
                blocks.extend(sheet_blocks)
            except Exception as e:
                warnings.append(ParseWarning(
                    page=i + 1,
                    message=f'시트 "{sheet["name"]}" 파싱 실패: {e}',
                    code="PARTIAL_PARSE",
                ))

        # 6. 메타데이터
        metadata = DocumentMetadata(page_count=processed)
        if "docProps/core.xml" in names:
            try:
                core_meta = _parse_core_xml(
                    zf.read("docProps/core.xml").decode("utf-8", errors="replace")
                )
                metadata.title = core_meta.title
                metadata.author = core_meta.author
                metadata.description = core_meta.description
                metadata.created_at = core_meta.created_at
                metadata.modified_at = core_meta.modified_at
            except Exception:
                pass

    markdown = blocks_to_markdown(blocks)
    return InternalParseResult(
        markdown=markdown,
        blocks=blocks,
        metadata=metadata,
        warnings=warnings or None,
    )
