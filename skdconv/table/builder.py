"""2-pass colSpan/rowSpan 테이블 빌더 및 Markdown 변환"""

from __future__ import annotations

import re
from typing import List, Optional, Set

from ..types import CellContext, IRBlock, IRCell, IRTable
from ..utils import sanitize_href

MAX_COLS = 200
MAX_ROWS = 10000

# HWP 자동생성 도형/개체 대체텍스트 정규식
_HWP_SHAPE_ALT_TEXT_RE = re.compile(
    r"(?:모서리가 둥근 |둥근 )?(?:사각형|직사각형|정사각형|원|타원|삼각형|이등변 삼각형|직각 삼각형|선|직선|곡선|화살표|굵은 화살표|이중 화살표|오각형|육각형|팔각형|별|[4-8]점별|십자|십자형|구름|구름형|마름모|도넛|평행사변형|사다리꼴|부채꼴|호|반원|물결|번개|하트|빗금|블록 화살표|수식|표|그림|개체|그리기\s?개체|묶음\s?개체|글상자|수식\s?개체|OLE\s?개체)\s?입니다\.?"
)


def _make_empty_cell() -> IRCell:
    return IRCell(text="", col_span=1, row_span=1)


def _make_grid(num_rows: int, num_cols: int) -> List[List[IRCell]]:
    return [[_make_empty_cell() for _ in range(num_cols)] for _ in range(num_rows)]


def build_table(rows: List[List[CellContext]]) -> IRTable:
    if len(rows) > MAX_ROWS:
        rows = rows[:MAX_ROWS]
    num_rows = len(rows)

    # colAddr/rowAddr가 있으면 직접 배치 (HWPX cellAddr, HWP5 colAddr/rowAddr)
    has_addr = any(
        c.col_addr is not None and c.row_addr is not None
        for row in rows
        for c in row
    )
    if has_addr:
        return _build_table_direct(rows, num_rows)

    # Pass 1: maxCols 계산
    max_cols = 0
    temp_occupied: List[List[bool]] = [[] for _ in range(num_rows)]

    for row_idx in range(num_rows):
        col_idx = 0
        for cell in rows[row_idx]:
            while col_idx < MAX_COLS and (
                col_idx < len(temp_occupied[row_idx]) and temp_occupied[row_idx][col_idx]
            ):
                col_idx += 1
            if col_idx >= MAX_COLS:
                break

            for r in range(row_idx, min(row_idx + cell.row_span, num_rows)):
                row_occ = temp_occupied[r]
                for c in range(col_idx, min(col_idx + cell.col_span, MAX_COLS)):
                    while len(row_occ) <= c:
                        row_occ.append(False)
                    row_occ[c] = True

            col_idx += cell.col_span
            if col_idx > max_cols:
                max_cols = col_idx

    if max_cols == 0:
        return IRTable(rows=0, cols=0, cells=[], has_header=False)

    # Pass 2: 실제 배치
    grid = _make_grid(num_rows, max_cols)
    occupied: List[List[bool]] = [[False] * max_cols for _ in range(num_rows)]

    for row_idx in range(num_rows):
        col_idx = 0
        cell_idx = 0

        while col_idx < max_cols and cell_idx < len(rows[row_idx]):
            while col_idx < max_cols and occupied[row_idx][col_idx]:
                col_idx += 1
            if col_idx >= max_cols:
                break

            cell = rows[row_idx][cell_idx]
            grid[row_idx][col_idx] = IRCell(
                text=cell.text.strip(),
                col_span=cell.col_span,
                row_span=cell.row_span,
            )

            for r in range(row_idx, min(row_idx + cell.row_span, num_rows)):
                for c in range(col_idx, min(col_idx + cell.col_span, max_cols)):
                    occupied[r][c] = True

            col_idx += cell.col_span
            cell_idx += 1

    return _trim_and_return(grid, num_rows, max_cols)


def _build_table_direct(rows: List[List[CellContext]], num_rows: int) -> IRTable:
    """colAddr/rowAddr 절대 좌표 기반 직접 배치."""
    max_cols = 0
    for row in rows:
        for cell in row:
            end = (cell.col_addr or 0) + cell.col_span
            if end > max_cols:
                max_cols = end
    if max_cols > MAX_COLS:
        max_cols = MAX_COLS
    if max_cols == 0:
        return IRTable(rows=0, cols=0, cells=[], has_header=False)

    grid = _make_grid(num_rows, max_cols)

    for row in rows:
        for cell in row:
            r = cell.row_addr or 0
            c = cell.col_addr or 0
            if r >= num_rows or c >= max_cols or r < 0 or c < 0:
                continue

            grid[r][c] = IRCell(
                text=cell.text.strip(),
                col_span=cell.col_span,
                row_span=cell.row_span,
            )
            # 병합 영역 마킹 (빈 셀로)
            for dr in range(cell.row_span):
                for dc in range(cell.col_span):
                    if dr == 0 and dc == 0:
                        continue
                    if r + dr < num_rows and c + dc < max_cols:
                        grid[r + dr][c + dc] = _make_empty_cell()

    return _trim_and_return(grid, num_rows, max_cols)


def _trim_and_return(grid: List[List[IRCell]], num_rows: int, max_cols: int) -> IRTable:
    """빈 후행 열 제거 후 IRTable 반환."""
    effective_cols = max_cols
    while effective_cols > 0:
        if any(
            row[effective_cols - 1].text.strip()
            for row in grid
            if effective_cols - 1 < len(row)
        ):
            break
        effective_cols -= 1

    if effective_cols < max_cols and effective_cols > 0:
        trimmed = [row[:effective_cols] for row in grid]
        return IRTable(rows=num_rows, cols=effective_cols, cells=trimmed, has_header=num_rows > 1)
    return IRTable(rows=num_rows, cols=max_cols, cells=grid, has_header=num_rows > 1)


def convert_table_to_text(rows: List[List[CellContext]]) -> str:
    lines = []
    for row in rows:
        parts = [c.text.strip().replace("\n", " ").replace("|", "\\|") for c in row]
        parts = [p for p in parts if p]
        if parts:
            lines.append(" / ".join(parts))
    return "\n".join(lines)


def _escape_gfm(text: str) -> str:
    return text.replace("~", "\\~")


def _sanitize_text(text: str) -> str:
    result = text
    # Supplementary Private Use Area (U+F0000–U+FFFFD) 제거
    result = re.sub(r"[\U000F0000-\U000FFFFD]", "", result)
    result = _HWP_SHAPE_ALT_TEXT_RE.sub("", result)
    result = re.sub(r"  +", " ", result).strip()

    # 균등배분 스페이스 정리 (예: "현 장 대 응" → "현장대응")
    if len(result) <= 30 and " " in result:
        tokens = result.split(" ")
        korean_single = sum(
            1 for t in tokens
            if len(t) == 1 and re.match(r"[가-힯ㄱ-ㆎ]", t)
        )
        if len(tokens) >= 3 and korean_single / len(tokens) >= 0.7:
            result = "".join(tokens)

    return result


def flatten_layout_tables(blocks: List[IRBlock]) -> List[IRBlock]:
    """레이아웃 테이블 감지 및 해체 — IRBlock 레벨에서 수행."""
    result: List[IRBlock] = []

    for block in blocks:
        if block.type != "table" or block.table is None:
            result.append(block)
            continue

        tbl = block.table
        num_rows, num_cols, cells = tbl.rows, tbl.cols, tbl.cells

        # 1x1 테이블은 기존 로직에서 처리
        if num_rows == 1 and num_cols == 1:
            result.append(block)
            continue

        if num_rows <= 3:
            total_newlines = 0
            total_text_len = 0
            for r in range(num_rows):
                for c in range(num_cols):
                    t = cells[r][c].text if r < len(cells) and c < len(cells[r]) else ""
                    total_newlines += t.count("\n")
                    total_text_len += len(t)

            if total_newlines > 5 or (num_rows <= 2 and total_text_len > 300):
                for r in range(num_rows):
                    for c in range(num_cols):
                        cell_text = cells[r][c].text.strip() if r < len(cells) and c < len(cells[r]) else ""
                        if not cell_text:
                            continue
                        for line in cell_text.split("\n"):
                            trimmed = line.strip()
                            if not trimmed:
                                continue
                            result.append(IRBlock(
                                type="paragraph",
                                text=trimmed,
                                page_number=block.page_number,
                            ))
                continue

        result.append(block)

    return result


def _has_merged_cells(table: IRTable) -> bool:
    for row in table.cells:
        for cell in row:
            if cell.col_span > 1 or cell.row_span > 1:
                return True
    return False


def _contains_inline_math(text: str) -> bool:
    return bool(re.search(r"(^|[^\\])\$(?=\S)(?:\\.|[^$\n])+?\S\$", text))


def _table_contains_inline_math(table: IRTable) -> bool:
    for row in table.cells:
        for cell in row:
            if _contains_inline_math(cell.text):
                return True
    return False


def _table_to_html(table: IRTable) -> str:
    cells, num_rows, num_cols = table.cells, table.rows, table.cols
    skip: Set[str] = set()
    lines = ["<table>"]

    for r in range(num_rows):
        tag = "th" if r == 0 else "td"
        row_html = []
        for c in range(num_cols):
            if f"{r},{c}" in skip:
                continue
            cell = cells[r][c] if r < len(cells) and c < len(cells[r]) else None
            if cell is None:
                continue

            for dr in range(cell.row_span):
                for dc in range(cell.col_span):
                    if dr == 0 and dc == 0:
                        continue
                    if r + dr < num_rows and c + dc < num_cols:
                        skip.add(f"{r + dr},{c + dc}")

            text = _sanitize_text(cell.text).replace("\n", "<br>")
            attrs = []
            if cell.col_span > 1:
                attrs.append(f'colspan="{cell.col_span}"')
            if cell.row_span > 1:
                attrs.append(f'rowspan="{cell.row_span}"')
            attr_str = " " + " ".join(attrs) if attrs else ""
            row_html.append(f"<{tag}{attr_str}>{text}</{tag}>")

        if row_html:
            lines.append(f"<tr>{''.join(row_html)}</tr>")

    lines.append("</table>")
    return "\n".join(lines)


def _table_to_markdown(table: IRTable) -> str:
    if table.rows == 0 or table.cols == 0:
        return ""

    cells, num_rows, num_cols = table.cells, table.rows, table.cols

    # 병합 셀이 있으면 HTML로, 수식 있으면 GFM으로
    if _has_merged_cells(table) and not _table_contains_inline_math(table):
        return _table_to_html(table)

    # 1x1 → 구조화된 텍스트
    if num_rows == 1 and num_cols == 1:
        content = _sanitize_text(cells[0][0].text)
        if not content:
            return ""
        result_lines = []
        for line in content.split("\n"):
            trimmed = line.strip()
            if not trimmed:
                continue
            if re.match(r"^\d+\.\s", trimmed):
                result_lines.append(f"**{_escape_gfm(trimmed)}**")
            elif re.match(r"^[가-힣]\.\s", trimmed):
                result_lines.append(f"  {_escape_gfm(trimmed)}")
            else:
                result_lines.append(_escape_gfm(trimmed))
        return "\n".join(result_lines)

    # 1열 다행 → 각 행을 별도 라인
    if num_cols == 1 and num_rows >= 2:
        return "\n".join(
            _escape_gfm(_sanitize_text(row[0].text)).replace("\n", " ")
            for row in cells
            if row and row[0].text
        )

    # 일반 GFM 테이블
    display: List[List[str]] = [[""] * num_cols for _ in range(num_rows)]
    skip: Set[str] = set()

    for r in range(num_rows):
        c = 0
        while c < num_cols:
            if f"{r},{c}" in skip:
                c += 1
                continue
            cell = cells[r][c] if r < len(cells) and c < len(cells[r]) else None
            if cell is None:
                c += 1
                continue

            display[r][c] = (
                _escape_gfm(_sanitize_text(cell.text))
                .replace("|", "\\|")
                .replace("\n", "<br>")
            )

            for dr in range(cell.row_span):
                for dc in range(cell.col_span):
                    if dr == 0 and dc == 0:
                        continue
                    if r + dr < num_rows and c + dc < num_cols:
                        skip.add(f"{r + dr},{c + dc}")

            c += cell.col_span

    # rowSpan 잔류 처리: 완전 빈 행 제거, 첫 열만 있는 행 전파
    unique_rows: List[List[str]] = []
    pending_first_col = ""

    for r in range(len(display)):
        row = display[r]
        if all(cell == "" for cell in row):
            continue

        non_empty_cols = [cell for cell in row if cell != ""]
        has_skip_in_row = any(f"{r},{c}" in skip for c in range(num_cols))
        if (
            not has_skip_in_row
            and len(non_empty_cols) == 1
            and row[0] != ""
            and all(cell == "" for cell in row[1:])
        ):
            pending_first_col = row[0]
            continue

        if pending_first_col and row[0] == "":
            row[0] = pending_first_col
            pending_first_col = ""
        else:
            pending_first_col = ""
        unique_rows.append(row)

    if not unique_rows:
        return ""

    md = []
    md.append("| " + " | ".join(unique_rows[0]) + " |")
    md.append("| " + " | ".join("---" for _ in unique_rows[0]) + " |")
    for row in unique_rows[1:]:
        md.append("| " + " | ".join(row) + " |")
    return "\n".join(md)


def blocks_to_markdown(blocks: List[IRBlock]) -> str:
    lines: List[str] = []
    i = 0

    while i < len(blocks):
        block = blocks[i]

        if block.type == "heading" and block.text:
            prefix = "#" * min(block.level or 2, 6)
            heading_text = _sanitize_text(block.text)
            if heading_text:
                lines.extend(["", f"{prefix} {heading_text}", ""])
            i += 1
            continue

        if block.type == "image" and block.text:
            # 연속된 이미지 블록(문서에서 나란히 배치)은 한 줄로 출력
            imgs = [f"![image]({block.text})"]
            j = i + 1
            while (
                j < len(blocks)
                and blocks[j].type == "image"
                and blocks[j].text
                and blocks[j].page_number == block.page_number
            ):
                imgs.append(f"![image]({blocks[j].text})")
                j += 1
            lines.extend(["", " ".join(imgs), ""])
            i = j
            continue

        if block.type == "separator":
            lines.extend(["", "---", ""])
            i += 1
            continue

        if block.type == "list" and block.text:
            list_text = _sanitize_text(block.text)
            if list_text:
                already_numbered = block.list_type == "ordered" and re.match(r"^\d+\.\s", list_text)
                prefix = "" if already_numbered else ("1. " if block.list_type == "ordered" else "- ")
                lines.append(f"{prefix}{list_text}")
                if block.children:
                    for child in block.children:
                        child_prefix = "1." if child.list_type == "ordered" else "-"
                        lines.append(f"  {child_prefix} {child.text or ''}")
            i += 1
            continue

        if block.type == "paragraph" and block.text:
            text = _sanitize_text(block.text)
            if text:
                # 별표 패턴
                if re.match(r"^\[별표\s*\d+", text):
                    next_block = blocks[i + 1] if i + 1 < len(blocks) else None
                    if (
                        next_block
                        and next_block.type == "paragraph"
                        and next_block.text
                        and re.search(r"관련\)?$", next_block.text)
                    ):
                        lines.extend(["", f"## {text} {next_block.text}", ""])
                        i += 2
                        continue
                    else:
                        lines.extend(["", f"## {text}", ""])
                        i += 1
                        continue

                if re.match(r"^\([^)]*조[^)]*관련\)$", text):
                    lines.extend([f"*{text}*", ""])
                    i += 1
                    continue

                if block.href:
                    href = sanitize_href(block.href)
                    if href:
                        text = f"[{text}]({href})"

                if block.footnote_text:
                    text += f" (주: {block.footnote_text})"

                lines.extend([_escape_gfm(text), ""])

        elif block.type == "table" and block.table:
            if lines and lines[-1] != "":
                lines.append("")
            table_md = _table_to_markdown(block.table)
            if table_md:
                lines.append(table_md)
                lines.append("")

        i += 1

    return "\n".join(lines).strip()
