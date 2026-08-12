"""HWP 5.x 바이너리 파서 — OLE2 컨테이너 → 섹션 → Markdown."""

from __future__ import annotations
import struct
import zlib
from dataclasses import dataclass, field
from typing import Optional, Callable

import olefile

from .record import (
    read_records, decompress_stream, parse_file_header, parse_doc_info,
    extract_text_with_controls, extract_equation_text,
    TAG_PARA_HEADER, TAG_PARA_TEXT, TAG_CHAR_SHAPE, TAG_CTRL_HEADER,
    TAG_LIST_HEADER, TAG_TABLE, TAG_EQEDIT,
    FLAG_COMPRESSED, FLAG_ENCRYPTED, FLAG_DISTRIBUTION, FLAG_DRM,
    HwpRecord, HwpDocInfo, HwpCharShape, HwpParaShape, HwpStyle,
)
from .crypto import decrypt_view_text
from .cfb_lenient import parse_lenient_cfb, LenientCfbContainer
from ..table.builder import build_table, blocks_to_markdown, flatten_layout_tables, MAX_COLS, MAX_ROWS
from ..types import (
    CellContext, IRBlock, IRTable, DocumentMetadata,
    InternalParseResult, ParseOptions, ParseWarning, OutlineItem,
    InlineStyle, ExtractedImage,
    HEADING_RATIO_H1, HEADING_RATIO_H2, HEADING_RATIO_H3,
)
from ..utils import SKDConvError, sanitize_href
from ..page_range import parse_page_range
from ..hwpx.equation import hml_to_latex

MAX_SECTIONS = 100
MAX_TOTAL_DECOMPRESS = 100 * 1024 * 1024

_TAG_SHAPE_COMPONENT = 0x004A
_CTRL_ID_EQEDIT = "deqe"


# ─── 공개 진입점 ─────────────────────────────────────

def parse_hwp5_document(data: bytes, options: Optional[ParseOptions] = None) -> InternalParseResult:
    warnings: list[ParseWarning] = []

    # OLE2/CFB 파싱: olefile → lenient fallback
    ole: Optional[olefile.OleFileIO] = None
    lenient: Optional[LenientCfbContainer] = None

    try:
        ole = olefile.OleFileIO(data)
    except Exception:
        try:
            lenient = parse_lenient_cfb(data)
            warnings.append(ParseWarning(message="손상된 CFB 컨테이너 — lenient 모드로 복구", code="LENIENT_CFB_RECOVERY"))
        except Exception as e:
            raise SKDConvError(f"CFB 컨테이너 파싱 실패 (strict 및 lenient 모두): {e}") from e

    def find_stream(path: str) -> Optional[bytes]:
        norm = path.lstrip("/")
        parts = norm.split("/")
        if ole:
            try:
                if ole.exists(norm):
                    return ole.openstream(norm).read()
                if ole.exists(norm.replace("/", "\\")):
                    return ole.openstream(norm.replace("/", "\\")).read()
            except Exception:
                pass
            return None
        return lenient.find_stream(path) if lenient else None

    header_data = find_stream("/FileHeader")
    if not header_data:
        raise SKDConvError("FileHeader 스트림 없음")
    header = parse_file_header(header_data)
    if header.signature != "HWP Document File":
        raise SKDConvError("HWP 시그니처 불일치")
    if header.flags & FLAG_ENCRYPTED:
        raise SKDConvError("암호화된 HWP는 지원하지 않습니다")
    if header.flags & FLAG_DRM:
        raise SKDConvError("DRM 보호된 HWP는 지원하지 않습니다")

    compressed = bool(header.flags & FLAG_COMPRESSED)
    distribution = bool(header.flags & FLAG_DISTRIBUTION)

    metadata = DocumentMetadata(version=f"{header.version_major}.x")
    if ole:
        _extract_hwp5_metadata(ole, metadata)

    doc_info = _parse_doc_info_stream(find_stream("/DocInfo"), compressed)

    if distribution:
        sections = _find_view_text_sections(find_stream, compressed)
    else:
        sections = _find_sections(find_stream, ole, lenient, compressed)

    if not sections:
        raise SKDConvError("섹션 스트림을 찾을 수 없습니다")

    metadata.page_count = len(sections)

    page_filter = parse_page_range(options.pages, len(sections)) if options and options.pages else None
    total_target = len(page_filter) if page_filter else len(sections)

    blocks: list[IRBlock] = []
    nested_counter = {"count": 0}
    total_decomp = 0
    parsed = 0

    for si, section_data in enumerate(sections):
        if page_filter and (si + 1) not in page_filter:
            continue
        try:
            buf = (decompress_stream(section_data) if not distribution and compressed else section_data)
            total_decomp += len(buf)
            if total_decomp > MAX_TOTAL_DECOMPRESS:
                raise SKDConvError("총 압축 해제 크기 초과 (decompression bomb 의심)")
            recs = read_records(buf)
            sb = _parse_section(recs, doc_info, warnings, si + 1, nested_counter)
            blocks.extend(sb)
            parsed += 1
            if options and options.on_progress:
                options.on_progress(parsed, total_target)
        except SKDConvError:
            raise
        except Exception as e:
            warnings.append(ParseWarning(page=si + 1, message=f"섹션 {si + 1} 파싱 실패: {e}", code="PARTIAL_PARSE"))

    images = _extract_hwp5_images(find_stream, ole, lenient, blocks, compressed, warnings)

    flat = flatten_layout_tables(blocks)

    if doc_info:
        _detect_hwp5_headings(flat, doc_info)

    outline = [
        OutlineItem(level=b.level, text=b.text, page_number=b.page_number)
        for b in flat if b.type == "heading" and b.level and b.text
    ]

    markdown = blocks_to_markdown(flat)
    return InternalParseResult(
        markdown=markdown,
        blocks=flat,
        metadata=metadata,
        outline=outline if outline else None,
        warnings=warnings if warnings else None,
        images=images if images else None,
    )


# ─── 스트림 탐색 ─────────────────────────────────────

def _find_sections(
    find_stream: Callable[[str], Optional[bytes]],
    ole: Optional[olefile.OleFileIO],
    lenient: Optional[LenientCfbContainer],
    compressed: bool,
) -> list[bytes]:
    sections: list[tuple[int, bytes]] = []

    for i in range(MAX_SECTIONS):
        raw = find_stream(f"/BodyText/Section{i}")
        if not raw:
            raw = find_stream(f"BodyText/Section{i}")
        if not raw:
            break
        sections.append((i, raw))

    if not sections and lenient:
        for e in lenient.entries():
            if len(sections) >= MAX_SECTIONS:
                break
            if e.name.startswith("Section"):
                raw = lenient.find_stream(e.name)
                if raw:
                    idx = int(e.name[7:]) if e.name[7:].isdigit() else 0
                    sections.append((idx, raw))

    return [s for _, s in sorted(sections, key=lambda x: x[0])]


def _find_view_text_sections(find_stream: Callable[[str], Optional[bytes]], compressed: bool) -> list[bytes]:
    sections: list[tuple[int, bytes]] = []
    for i in range(MAX_SECTIONS):
        raw = find_stream(f"/ViewText/Section{i}")
        if not raw:
            break
        try:
            dec = decrypt_view_text(raw, compressed)
            sections.append((i, dec))
        except Exception:
            break
    return [s for _, s in sorted(sections, key=lambda x: x[0])]


# ─── DocInfo ─────────────────────────────────────────

def _parse_doc_info_stream(raw: Optional[bytes], compressed: bool) -> Optional[HwpDocInfo]:
    if not raw:
        return None
    try:
        buf = decompress_stream(raw) if compressed else raw
        return parse_doc_info(read_records(buf))
    except Exception:
        return None


# ─── 메타데이터 ──────────────────────────────────────

def _extract_hwp5_metadata(ole: olefile.OleFileIO, metadata: DocumentMetadata) -> None:
    for name in ("\x05HwpSummaryInformation", "\x05SummaryInformation"):
        try:
            if not ole.exists(name):
                continue
            raw = ole.openstream(name).read()
            if len(raw) < 48:
                continue
            num_sets = struct.unpack_from("<I", raw, 24)[0]
            if not num_sets:
                continue
            set_offset = struct.unpack_from("<I", raw, 44)[0]
            if set_offset >= len(raw) - 8:
                continue
            num_props = struct.unpack_from("<I", raw, set_offset + 4)[0]
            if not num_props or num_props > 100:
                continue
            for i in range(num_props):
                entry_off = set_offset + 8 + i * 8
                if entry_off + 8 > len(raw):
                    break
                prop_id = struct.unpack_from("<I", raw, entry_off)[0]
                prop_off = set_offset + struct.unpack_from("<I", raw, entry_off + 4)[0]
                if prop_off + 8 > len(raw):
                    continue
                if prop_id not in (2, 4, 6):
                    continue
                prop_type = struct.unpack_from("<I", raw, prop_off)[0]
                if prop_type != 0x1E:
                    continue
                str_len = struct.unpack_from("<I", raw, prop_off + 4)[0]
                if str_len == 0 or str_len > 10000 or prop_off + 8 + str_len > len(raw):
                    continue
                s = raw[prop_off + 8:prop_off + 8 + str_len].decode("utf-8", errors="replace").rstrip("\x00").strip()
                if s:
                    if prop_id == 2:
                        metadata.title = s
                    elif prop_id == 4:
                        metadata.author = s
                    elif prop_id == 6:
                        metadata.description = s
            return
        except Exception:
            continue


# ─── 이미지 추출 ─────────────────────────────────────

def _detect_image_mime(data: bytes) -> Optional[str]:
    if len(data) < 4:
        return None
    if data[0] == 0x89 and data[1] == 0x50 and data[2] == 0x4E and data[3] == 0x47:
        return "image/png"
    if data[0] == 0xFF and data[1] == 0xD8 and data[2] == 0xFF:
        return "image/jpeg"
    if data[0] == 0x47 and data[1] == 0x49 and data[2] == 0x46:
        return "image/gif"
    if data[0] == 0x42 and data[1] == 0x4D:
        return "image/bmp"
    if data[0] == 0xD7 and data[1] == 0xCD and data[2] == 0xC6 and data[3] == 0x9A:
        return "image/wmf"
    if data[0] == 0x01 and data[1] == 0x00 and data[2] == 0x00 and data[3] == 0x00:
        return "image/emf"
    return None


def _extract_hwp5_images(
    find_stream: Callable[[str], Optional[bytes]],
    ole: Optional[olefile.OleFileIO],
    lenient: Optional[LenientCfbContainer],
    blocks: list[IRBlock],
    compressed: bool,
    warnings: list[ParseWarning],
) -> list[ExtractedImage]:
    bin_map: dict[int, tuple[bytes, str]] = {}

    import re as _re
    bin_re = _re.compile(r"[Bb][Ii][Nn](\d{4})$")

    if ole:
        for entry in ole.listdir():
            if not entry or len(entry) < 2:
                continue
            if entry[0].upper() != "BINDATA":
                continue
            name = entry[-1]
            m = bin_re.search(name)
            if not m:
                continue
            idx = int(m.group(1))
            try:
                raw = ole.openstream("/".join(entry)).read()
                if compressed:
                    try:
                        raw = decompress_stream(raw)
                    except Exception:
                        pass
                bin_map[idx] = (raw, name)
            except Exception:
                pass
    elif lenient:
        for e in lenient.entries():
            m = bin_re.search(e.name)
            if not m:
                continue
            idx = int(m.group(1))
            raw = lenient.find_stream(e.name)
            if not raw:
                continue
            if compressed:
                try:
                    raw = decompress_stream(raw)
                except Exception:
                    pass
            bin_map[idx] = (raw, e.name)

    if not bin_map:
        return []

    images: list[ExtractedImage] = []
    img_idx = 0

    for block in blocks:
        if block.type != "image" or not block.text:
            continue
        try:
            bin_id = int(block.text)
        except ValueError:
            continue
        if bin_id not in bin_map:
            warnings.append(ParseWarning(page=block.page_number, message=f"BinData {bin_id} 없음", code="SKIPPED_IMAGE"))
            block.type = "paragraph"
            block.text = f"[이미지: BinData {bin_id}]"
            continue

        raw, name = bin_map[bin_id]
        mime = _detect_image_mime(raw)
        if not mime:
            warnings.append(ParseWarning(page=block.page_number, message=f"BinData {bin_id}: 알 수 없는 이미지 형식", code="SKIPPED_IMAGE"))
            block.type = "paragraph"
            block.text = f"[이미지: {name}]"
            continue

        img_idx += 1
        ext = "jpg" if "jpeg" in mime else mime.split("/")[-1]
        filename = f"image_{img_idx:03d}.{ext}"
        images.append(ExtractedImage(filename=filename, data=bytes(raw), mime_type=mime))
        block.text = filename
        from ..types import ImageData
        block.image_data = ImageData(data=bytes(raw), mime_type=mime, filename=name)

    return images


# ─── 헤딩 감지 ───────────────────────────────────────

def _detect_hwp5_headings(blocks: list[IRBlock], doc_info: HwpDocInfo) -> None:
    base_font_size = 0.0

    for style in doc_info.styles:
        name_lower = (style.name_ko or style.name).lower()
        if any(k in name_lower for k in ("바탕", "본문", "normal", "body")):
            if style.char_shape_id < len(doc_info.char_shapes):
                cs = doc_info.char_shapes[style.char_shape_id]
                if cs.font_size > 0:
                    base_font_size = cs.font_size / 10.0
                    break

    if base_font_size == 0:
        freq: dict[float, int] = {}
        for b in blocks:
            if b.style and b.style.font_size:
                freq[b.style.font_size] = freq.get(b.style.font_size, 0) + 1
        if freq:
            base_font_size = max(freq, key=lambda k: freq[k])

    if base_font_size <= 0:
        return

    import re
    for block in blocks:
        if block.type == "heading":
            continue
        if block.type != "paragraph" or not block.text:
            continue
        text = block.text.strip()
        if not text or len(text) > 200 or text.isdigit():
            continue

        level = 0
        if block.style and block.style.font_size:
            ratio = block.style.font_size / base_font_size
            if ratio >= HEADING_RATIO_H1:
                level = 1
            elif ratio >= HEADING_RATIO_H2:
                level = 2
            elif ratio >= HEADING_RATIO_H3:
                level = 3

        if re.match(r"^제\d+[장절편]\s", text) and len(text) <= 50:
            if level == 0:
                level = 2
        elif re.match(r"^제\d+(조의?\d*)\s*[（(]", text) and len(text) <= 80:
            if level == 0:
                level = 3

        if level > 0:
            block.type = "heading"
            block.level = level


# ─── 섹션 파싱 ───────────────────────────────────────

def _is_equation_ctrl(ctrl_id: str) -> bool:
    return ctrl_id in (_CTRL_ID_EQEDIT, "eqed")


def _format_equation(eq: str) -> str:
    norm = hml_to_latex(eq)
    if not norm:
        return ""
    return f"${norm.replace('$', chr(92) + '$')}$"


def _extract_equation_from_ctrl(records: list[HwpRecord], ctrl_idx: int) -> Optional[str]:
    ctrl_level = records[ctrl_idx].level
    for j in range(ctrl_idx + 1, min(ctrl_idx + 10, len(records))):
        r = records[j]
        if r.level <= ctrl_level:
            break
        if r.tag_id != TAG_EQEDIT:
            continue
        eq = extract_equation_text(r.data)
        return _format_equation(eq) if eq else None
    return None


def _render_text_with_equations(text_records: list[bytes], equations: list[str]) -> str:
    queue = list(equations)

    def resolver(ctrl_id: str) -> Optional[str]:
        if not _is_equation_ctrl(ctrl_id) or not queue:
            return None
        return queue.pop(0)

    return "".join(extract_text_with_controls(d, resolver) for d in text_records)


def _extract_bin_data_id(records: list[HwpRecord], ctrl_idx: int) -> int:
    ctrl_level = records[ctrl_idx].level
    for j in range(ctrl_idx + 1, min(ctrl_idx + 50, len(records))):
        r = records[j]
        if r.level <= ctrl_level:
            break
        if r.tag_id > _TAG_SHAPE_COMPONENT and r.level > ctrl_level + 1 and len(r.data) >= 4:
            possible_id = struct.unpack_from("<H", r.data, 0)[0]
            if possible_id < 10000:
                return possible_id
    return -1


def _extract_note_text(records: list[HwpRecord], ctrl_idx: int) -> Optional[str]:
    ctrl_level = records[ctrl_idx].level
    texts: list[str] = []
    text_records: list[bytes] = []
    equations: list[str] = []

    def flush() -> None:
        t = _render_text_with_equations(text_records, equations).strip()
        if t:
            texts.append(t)
        text_records.clear()
        equations.clear()

    for j in range(ctrl_idx + 1, min(ctrl_idx + 100, len(records))):
        r = records[j]
        if r.level <= ctrl_level:
            break
        if r.tag_id == TAG_PARA_HEADER:
            flush()
        if r.tag_id == TAG_PARA_TEXT:
            text_records.append(r.data)
        if r.tag_id == TAG_CTRL_HEADER and len(r.data) >= 4:
            ctrl_id = r.data[:4].decode("ascii", errors="replace")
            if _is_equation_ctrl(ctrl_id):
                eq = _extract_equation_from_ctrl(records, j)
                if eq:
                    equations.append(eq)

    flush()
    return " ".join(texts) if texts else None


def _extract_textbox_text(records: list[HwpRecord], ctrl_idx: int) -> Optional[str]:
    ctrl_level = records[ctrl_idx].level
    texts: list[str] = []
    text_records: list[bytes] = []
    equations: list[str] = []

    def flush() -> None:
        t = _render_text_with_equations(text_records, equations).strip()
        if t:
            texts.append(t)
        text_records.clear()
        equations.clear()

    for j in range(ctrl_idx + 1, min(ctrl_idx + 200, len(records))):
        r = records[j]
        if r.level <= ctrl_level:
            break
        if r.tag_id == TAG_PARA_HEADER:
            flush()
        if r.tag_id == TAG_PARA_TEXT:
            text_records.append(r.data)
        if r.tag_id == TAG_CTRL_HEADER and len(r.data) >= 4:
            ctrl_id = r.data[:4].decode("ascii", errors="replace")
            if _is_equation_ctrl(ctrl_id):
                eq = _extract_equation_from_ctrl(records, j)
                if eq:
                    equations.append(eq)

    flush()
    return "\n".join(texts) if texts else None


def _extract_hyperlink_url(data: bytes) -> Optional[str]:
    import re
    try:
        http_sig = "http".encode("utf-16-le")
        idx = data.find(http_sig)
        if idx < 0:
            return None
        end = idx
        while end + 1 < len(data):
            if struct.unpack_from("<H", data, end)[0] == 0:
                break
            end += 2
        url = data[idx:end].decode("utf-16-le", errors="replace")
        if re.match(r"^https?://.+", url) and len(url) < 2000:
            return url
    except Exception:
        pass
    return None


def _resolve_char_style(char_shape_ids: list[int], doc_info: HwpDocInfo) -> Optional[InlineStyle]:
    if not char_shape_ids or not doc_info.char_shapes:
        return None
    freq: dict[int, int] = {}
    dominant_id = char_shape_ids[0]
    max_count = 0
    for csid in char_shape_ids:
        c = freq.get(csid, 0) + 1
        freq[csid] = c
        if c > max_count:
            max_count = c
            dominant_id = csid

    if dominant_id >= len(doc_info.char_shapes):
        return None
    cs = doc_info.char_shapes[dominant_id]
    style = InlineStyle()
    if cs.font_size > 0:
        style.font_size = cs.font_size / 10.0
    if cs.attr_flags & 0x01:
        style.italic = True
    if cs.attr_flags & 0x02:
        style.bold = True
    return style if (style.font_size or style.bold or style.italic) else None


def _parse_section(
    records: list[HwpRecord],
    doc_info: Optional[HwpDocInfo],
    warnings: list[ParseWarning],
    section_num: int,
    counter: dict,
) -> list[IRBlock]:
    blocks: list[IRBlock] = []
    i = 0

    while i < len(records):
        rec = records[i]

        if rec.tag_id == TAG_PARA_HEADER and rec.level == 0:
            paragraph, tables, next_idx, char_shape_ids, para_shape_id = _parse_paragraph_with_tables(records, i, counter)
            if paragraph is not None:
                block = IRBlock(type="paragraph", text=paragraph, page_number=section_num)
                if doc_info and char_shape_ids:
                    sty = _resolve_char_style(char_shape_ids, doc_info)
                    if sty:
                        block.style = sty
                if doc_info and 0 <= para_shape_id < len(doc_info.para_shapes):
                    ol = doc_info.para_shapes[para_shape_id].outline_level
                    if 1 <= ol <= 6:
                        block.type = "heading"
                        block.level = ol
                blocks.append(block)
            for t in tables:
                blocks.append(IRBlock(type="table", table=t, page_number=section_num))
            i = next_idx
            continue

        if rec.tag_id == TAG_CTRL_HEADER and rec.level <= 1 and len(rec.data) >= 4:
            ctrl_id = rec.data[:4].decode("ascii", errors="replace")
            if ctrl_id in (" lbt", "tbl "):
                table, next_idx = _parse_table_block(records, i, counter)
                if table:
                    blocks.append(IRBlock(type="table", table=table, page_number=section_num))
                i = next_idx
                continue

            if ctrl_id in ("gso ", " osg"):
                bin_id = _extract_bin_data_id(records, i)
                if bin_id >= 0:
                    blocks.append(IRBlock(type="image", text=str(bin_id), page_number=section_num))
                else:
                    box_text = _extract_textbox_text(records, i)
                    if box_text:
                        blocks.append(IRBlock(type="paragraph", text=box_text, page_number=section_num))
            elif ctrl_id in (" elo", "ole "):
                warnings.append(ParseWarning(page=section_num, message=f"스킵된 제어 요소: {ctrl_id.strip()}", code="SKIPPED_IMAGE"))
            elif ctrl_id in ("fn  ", " nf ", "en  ", " ne "):
                note = _extract_note_text(records, i)
                if note and blocks:
                    last = blocks[-1]
                    if last.type == "paragraph":
                        last.footnote_text = (last.footnote_text + "; " + note) if last.footnote_text else note
            elif ctrl_id in ("%tok", "klnk"):
                url = _extract_hyperlink_url(rec.data)
                if url and blocks:
                    last = blocks[-1]
                    if last.type == "paragraph" and not last.href:
                        last.href = sanitize_href(url)

        i += 1

    return blocks


def _parse_paragraph_with_tables(
    records: list[HwpRecord],
    start_idx: int,
    counter: dict,
) -> tuple[Optional[str], list[IRTable], int, list[int], int]:
    start_level = records[start_idx].level
    text_records: list[bytes] = []
    equations: list[str] = []
    tables: list[IRTable] = []
    char_shape_ids: list[int] = []

    para_data = records[start_idx].data
    para_shape_id = struct.unpack_from("<H", para_data, 8)[0] if len(para_data) >= 10 else -1

    i = start_idx + 1
    while i < len(records):
        rec = records[i]
        if rec.tag_id == TAG_PARA_HEADER and rec.level <= start_level:
            break
        if rec.tag_id == TAG_PARA_TEXT:
            text_records.append(rec.data)
        if rec.tag_id == TAG_CHAR_SHAPE and len(rec.data) >= 8:
            for off in range(0, len(rec.data) - 7, 8):
                char_shape_ids.append(struct.unpack_from("<I", rec.data, off + 4)[0])
        if rec.tag_id == TAG_CTRL_HEADER and len(rec.data) >= 4:
            ctrl_id = rec.data[:4].decode("ascii", errors="replace")
            if _is_equation_ctrl(ctrl_id):
                eq = _extract_equation_from_ctrl(records, i)
                if eq:
                    equations.append(eq)
            elif ctrl_id in (" lbt", "tbl "):
                t, next_i = _parse_table_block(records, i, counter)
                if t:
                    tables.append(t)
                i = next_i
                continue
        i += 1

    text = _render_text_with_equations(text_records, equations).strip()
    return (text if text else None), tables, i, char_shape_ids, para_shape_id


def _parse_table_block(
    records: list[HwpRecord],
    start_idx: int,
    counter: dict,
) -> tuple[Optional[IRTable], int]:
    table_level = records[start_idx].level
    i = start_idx + 1
    rows = cols = 0
    cells: list[CellContext] = []

    while i < len(records):
        rec = records[i]
        if rec.tag_id == TAG_PARA_HEADER and rec.level <= table_level:
            break
        if rec.tag_id == TAG_CTRL_HEADER and rec.level <= table_level:
            break
        if rec.tag_id == TAG_TABLE and len(rec.data) >= 8:
            rows = min(struct.unpack_from("<H", rec.data, 4)[0], MAX_ROWS)
            cols = min(struct.unpack_from("<H", rec.data, 6)[0], MAX_COLS)
        if rec.tag_id == TAG_LIST_HEADER:
            cell, next_i = _parse_cell_block(records, i, table_level, counter)
            if cell:
                cells.append(cell)
            i = next_i
            continue
        i += 1

    if not rows or not cols or not cells:
        return None, i

    has_addr = any(c.col_addr is not None and c.row_addr is not None for c in cells)
    if has_addr:
        cell_rows = _arrange_cells(rows, cols, cells)
        ir_cells = [
            [{"text": c.text.strip(), "colSpan": c.col_span, "rowSpan": c.row_span} for c in row]
            for row in cell_rows
        ]
        from ..types import IRCell
        ir_cell_rows = [
            [IRCell(text=c["text"], col_span=c["colSpan"], row_span=c["rowSpan"]) for c in row]
            for row in ir_cells
        ]
        return IRTable(rows=rows, cols=cols, cells=ir_cell_rows, has_header=rows > 1), i

    cell_rows = _arrange_cells(rows, cols, cells)
    return build_table(cell_rows), i


def _parse_cell_block(
    records: list[HwpRecord],
    start_idx: int,
    table_level: int,
    counter: dict,
) -> tuple[Optional[CellContext], int]:
    rec = records[start_idx]
    cell_level = rec.level
    texts: list[str] = []
    text_records: list[bytes] = []
    equations: list[str] = []

    def flush() -> None:
        t = _render_text_with_equations(text_records, equations).strip()
        if t:
            texts.append(t)
        text_records.clear()
        equations.clear()

    col_span = row_span = 1
    col_addr: Optional[int] = None
    row_addr: Optional[int] = None

    if len(rec.data) >= 16:
        col_addr = struct.unpack_from("<H", rec.data, 8)[0]
        row_addr = struct.unpack_from("<H", rec.data, 10)[0]
        cs = struct.unpack_from("<H", rec.data, 12)[0]
        rs = struct.unpack_from("<H", rec.data, 14)[0]
        if cs > 0:
            col_span = min(cs, MAX_COLS)
        if rs > 0:
            row_span = min(rs, MAX_ROWS)

    i = start_idx + 1
    while i < len(records):
        r = records[i]
        if r.tag_id == TAG_LIST_HEADER and r.level <= cell_level:
            break
        if r.level <= table_level and r.tag_id in (TAG_PARA_HEADER, TAG_CTRL_HEADER):
            break
        if r.tag_id == TAG_PARA_HEADER:
            flush()
        if r.tag_id == TAG_PARA_TEXT:
            text_records.append(r.data)
        if r.tag_id == TAG_CTRL_HEADER and len(r.data) >= 4:
            ctrl_id = r.data[:4].decode("ascii", errors="replace")
            if _is_equation_ctrl(ctrl_id):
                eq = _extract_equation_from_ctrl(records, i)
                if eq:
                    equations.append(eq)
            elif ctrl_id in (" lbt", "tbl "):
                flush()
                counter["count"] += 1
                texts.append(f"[중첩 테이블 #{counter['count']}]")
        i += 1

    flush()
    cell = CellContext(text="\n".join(texts), col_span=col_span, row_span=row_span, col_addr=col_addr, row_addr=row_addr)
    return cell, i


def _arrange_cells(rows: int, cols: int, cells: list[CellContext]) -> list[list[CellContext]]:
    grid: list[list[Optional[CellContext]]] = [[None] * cols for _ in range(rows)]
    has_addr = any(c.col_addr is not None and c.row_addr is not None for c in cells)

    if has_addr:
        for cell in cells:
            r = cell.row_addr if cell.row_addr is not None else 0
            c = cell.col_addr if cell.col_addr is not None else 0
            if r >= rows or c >= cols:
                continue
            grid[r][c] = cell
            for dr in range(cell.row_span):
                for dc in range(cell.col_span):
                    if dr == 0 and dc == 0:
                        continue
                    if r + dr < rows and c + dc < cols:
                        grid[r + dr][c + dc] = CellContext(text="", col_span=1, row_span=1)
    else:
        cell_idx = 0
        for r in range(rows):
            for c in range(cols):
                if cell_idx >= len(cells):
                    break
                if grid[r][c] is not None:
                    continue
                cell = cells[cell_idx]; cell_idx += 1
                grid[r][c] = cell
                for dr in range(cell.row_span):
                    for dc in range(cell.col_span):
                        if dr == 0 and dc == 0:
                            continue
                        if r + dr < rows and c + dc < cols:
                            grid[r + dr][c + dc] = CellContext(text="", col_span=1, row_span=1)

    empty = CellContext(text="", col_span=1, row_span=1)
    return [[cell if cell is not None else empty for cell in row] for row in grid]
