"""HWPML 2.x 파서 — XML 기반 한컴 문서"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

import lxml.etree as ET

from ..types import (
    CellContext, DocumentMetadata, InternalParseResult, IRBlock,
    OutlineItem, ParseOptions, ParseWarning,
)
from ..utils import KordocError, strip_dtd
from ..table.builder import blocks_to_markdown, build_table
from ..page_range import parse_page_range

MAX_XML_DEPTH = 200
MAX_TABLE_ROWS = 5000
MAX_TABLE_COLS = 500
MAX_HWPML_BYTES = 50 * 1024 * 1024


def _tag(el: ET._Element) -> str:
    t = el.tag
    return t.split("}", 1)[1] if "}" in t else t


def _attr(el: ET._Element, name: str) -> Optional[str]:
    for k, v in el.attrib.items():
        k_local = k.split("}", 1)[1] if "}" in k else k
        if k_local == name:
            return v
    return None


def _find_child(parent: ET._Element, tag: str) -> Optional[ET._Element]:
    for child in parent:
        if _tag(child) == tag:
            return child
    return None


def _text_content(el: ET._Element) -> str:
    return "".join(el.itertext())


# ─── ParaShape 맵 ────────────────────────────────────────

def _build_para_shape_map(root: ET._Element) -> Dict[str, Optional[int]]:
    """HeadingType 기반 ParaShape ID → 헤딩 레벨 맵."""
    result: Dict[str, Optional[int]] = {}
    head = _find_child(root, "HEAD")
    if head is None:
        return result
    mapping = _find_child(head, "MAPPINGTABLE")
    if mapping is None:
        return result
    para_list = _find_child(mapping, "PARASHAPELIST")
    if para_list is None:
        return result

    for el in para_list:
        if _tag(el) != "PARASHAPE":
            continue
        ps_id = _attr(el, "Id") or ""
        heading_type = _attr(el, "HeadingType") or "None"
        level_str = _attr(el, "Level") or "0"
        try:
            level = int(level_str)
        except ValueError:
            level = 0

        if heading_type == "Outline":
            safe_level = max(0, level)
            heading_level = min(safe_level + 1, 6)
            result[ps_id] = heading_level
        else:
            result[ps_id] = None

    return result


# ─── 텍스트 추출 ─────────────────────────────────────────

def _collect_char_text(node: ET._Element, parts: List[str], depth: int = 0) -> None:
    if depth > MAX_XML_DEPTH:
        return
    for child in node:
        tag = _tag(child)
        if tag == "CHAR":
            t = _text_content(child)
            if t:
                parts.append(t)
        elif tag in ("TABLE", "PICTURE", "SHAPEOBJECT", "AUTONUM"):
            pass
        else:
            _collect_char_text(child, parts, depth + 1)


def _extract_paragraph_text(p: ET._Element) -> str:
    parts: List[str] = []
    _collect_char_text(p, parts)
    return "".join(parts).strip()


def _collect_cell_text(node: ET._Element, parts: List[str], depth: int = 0) -> None:
    if depth > 20:
        return
    for child in node:
        tag = _tag(child)
        if tag == "P":
            t = _extract_paragraph_text(child)
            if t:
                parts.append(t)
        elif tag == "TABLE":
            parts.append("[중첩 테이블]")
        else:
            _collect_cell_text(child, parts, depth + 1)


def _extract_cell_text(cell_el: ET._Element) -> str:
    parts: List[str] = []
    _collect_cell_text(cell_el, parts)
    return "\n".join(p for p in parts if p).strip()


# ─── 테이블 파싱 ─────────────────────────────────────────

def _parse_table(
    el: ET._Element,
    blocks: List[IRBlock],
    para_shape_map: Dict[str, Optional[int]],
    section_num: int,
    warnings: List[ParseWarning],
) -> None:
    try:
        row_count = int(_attr(el, "RowCount") or "0")
        col_count = int(_attr(el, "ColCount") or "0")
    except ValueError:
        return

    if row_count == 0 or col_count == 0:
        return
    if row_count > MAX_TABLE_ROWS or col_count > MAX_TABLE_COLS:
        warnings.append(ParseWarning(
            message=f"테이블 크기 초과 ({row_count}x{col_count}) — 스킵",
            code="TRUNCATED_TABLE",
        ))
        return

    cells: List[CellContext] = []
    for row_el in el:
        if _tag(row_el) != "ROW":
            continue
        for cell_el in row_el:
            if _tag(cell_el) != "CELL":
                continue
            try:
                col_addr = int(_attr(cell_el, "ColAddr") or "0")
                row_addr = int(_attr(cell_el, "RowAddr") or "0")
                col_span = max(1, min(int(_attr(cell_el, "ColSpan") or "1"), MAX_TABLE_COLS))
                row_span = max(1, min(int(_attr(cell_el, "RowSpan") or "1"), MAX_TABLE_ROWS))
            except ValueError:
                col_addr = row_addr = 0
                col_span = row_span = 1

            cell_text = _extract_cell_text(cell_el)
            cells.append(CellContext(
                text=cell_text,
                col_span=col_span,
                row_span=row_span,
                col_addr=col_addr,
                row_addr=row_addr,
            ))

    if not cells:
        return

    # 그리드 배치 (colAddr/rowAddr 기반)
    grid: List[List[Optional[CellContext]]] = [
        [None] * col_count for _ in range(row_count)
    ]
    for cell in cells:
        r, c = cell.row_addr or 0, cell.col_addr or 0
        if r >= row_count or c >= col_count:
            continue
        grid[r][c] = cell
        for dr in range(cell.row_span):
            for dc in range(cell.col_span):
                if dr == 0 and dc == 0:
                    continue
                if r + dr < row_count and c + dc < col_count:
                    grid[r + dr][c + dc] = CellContext("", 1, 1)

    empty = CellContext("", 1, 1)
    cell_rows = [[cell if cell is not None else empty for cell in row] for row in grid]

    table = build_table(cell_rows)
    blocks.append(IRBlock(type="table", table=table, page_number=section_num))


# ─── 섹션 워크 ───────────────────────────────────────────

def _walk_content(
    node: ET._Element,
    blocks: List[IRBlock],
    para_shape_map: Dict[str, Optional[int]],
    section_num: int,
    warnings: List[ParseWarning],
    in_header_footer: bool,
    depth: int = 0,
) -> None:
    if depth > MAX_XML_DEPTH:
        return

    for child in node:
        tag = _tag(child)

        if tag in ("HEADER", "FOOTER"):
            continue

        if tag == "P":
            if not in_header_footer:
                ps_id = _attr(child, "ParaShape") or ""
                heading_level = para_shape_map.get(ps_id)
                text = _extract_paragraph_text(child)
                if text:
                    if heading_level is not None:
                        blocks.append(IRBlock(
                            type="heading",
                            text=text,
                            level=heading_level,
                            page_number=section_num,
                        ))
                    else:
                        blocks.append(IRBlock(
                            type="paragraph",
                            text=text,
                            page_number=section_num,
                        ))
            continue

        if tag == "TABLE":
            if not in_header_footer:
                _parse_table(child, blocks, para_shape_map, section_num, warnings)
            continue

        _walk_content(child, blocks, para_shape_map, section_num, warnings, in_header_footer, depth + 1)


def _count_sections(body: ET._Element) -> int:
    return sum(1 for child in body if _tag(child) == "SECTION")


# ─── 메인 파서 ───────────────────────────────────────────

def parse_hwpml_document(data: bytes, options: Optional[ParseOptions] = None) -> InternalParseResult:
    if len(data) > MAX_HWPML_BYTES:
        mb = len(data) / 1024 / 1024
        raise KordocError(f"HWPML 파일 크기 초과 ({mb:.1f}MB > 50MB)")

    text = data.decode("utf-8", errors="replace").lstrip("﻿")
    text = text.replace("&nbsp;", "&#160;")
    xml_str = strip_dtd(text)

    warnings: List[ParseWarning] = []

    try:
        root = ET.fromstring(xml_str.encode("utf-8"), parser=ET.XMLParser(recover=True))
    except ET.XMLSyntaxError as e:
        warnings.append(ParseWarning(message=f"HWPML XML 파싱 실패: {e}", code="MALFORMED_XML"))
        return InternalParseResult(markdown="", blocks=[], warnings=warnings)

    # ─── 메타데이터 추출 ──────────────────────────────────
    metadata = DocumentMetadata()
    doc_summary = _find_child(root, "DOCSUMMARY")
    if doc_summary is not None:
        title_el = _find_child(doc_summary, "TITLE")
        author_el = _find_child(doc_summary, "AUTHOR")
        date_el = _find_child(doc_summary, "DATE")
        if title_el is not None:
            metadata.title = _text_content(title_el).strip() or None
        if author_el is not None:
            metadata.author = _text_content(author_el).strip() or None
        if date_el is not None:
            metadata.created_at = _text_content(date_el).strip() or None

    # ─── HEAD: ParaShape 맵 구축 ──────────────────────────
    para_shape_map = _build_para_shape_map(root)

    # ─── BODY 파싱 ────────────────────────────────────────
    body = _find_child(root, "BODY")
    if body is None:
        return InternalParseResult(markdown="", blocks=[], metadata=metadata, warnings=warnings or None)

    blocks: List[IRBlock] = []
    total_sections = _count_sections(body)
    page_filter = parse_page_range(options.pages, total_sections) if options and options.pages else None
    section_idx = 0

    for child in body:
        if _tag(child) != "SECTION":
            continue
        section_idx += 1
        if page_filter and section_idx not in page_filter:
            continue
        _walk_content(child, blocks, para_shape_map, section_idx, warnings, False)

    # ─── 헤딩 트리 ────────────────────────────────────────
    outline = [
        OutlineItem(level=b.level or 1, text=b.text or "", page_number=b.page_number)
        for b in blocks
        if b.type == "heading" and b.text
    ]

    markdown = blocks_to_markdown(blocks)

    has_meta = any(v is not None for v in [metadata.title, metadata.author, metadata.created_at])
    return InternalParseResult(
        markdown=markdown,
        blocks=blocks,
        metadata=metadata if has_meta else None,
        outline=outline or None,
        warnings=warnings or None,
    )
