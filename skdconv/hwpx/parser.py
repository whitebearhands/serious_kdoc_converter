"""HWPX 파서 — manifest 멀티섹션, colSpan/rowSpan, 중첩 테이블, 손상 ZIP 복구"""

from __future__ import annotations

import io
import re
import struct
import zlib
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import lxml.etree as ET

from ..types import (
    CellContext, DocumentMetadata, ExtractedImage, InlineStyle,
    InternalParseResult, IRBlock, OutlineItem, ParseOptions, ParseWarning,
    HEADING_RATIO_H1, HEADING_RATIO_H2, HEADING_RATIO_H3,
)
from ..utils import KordocError, is_path_traversal, precheck_zip_size, sanitize_href, strip_dtd
from ..table.builder import MAX_COLS, MAX_ROWS, blocks_to_markdown, build_table, convert_table_to_text
from ..page_range import parse_page_range
from .equation import hml_to_latex

MAX_DECOMPRESS_SIZE = 100 * 1024 * 1024
MAX_ZIP_ENTRIES = 500
MAX_XML_DEPTH = 200


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


def _find_child(parent: ET._Element, local_name: str) -> Optional[ET._Element]:
    for child in parent:
        if _tag(child) == local_name:
            return child
    return None


def _find_descendant(node: ET._Element, target: str, depth: int = 0) -> Optional[ET._Element]:
    if depth > 5:
        return None
    for child in node:
        if _tag(child) == target:
            return child
        found = _find_descendant(child, target, depth + 1)
        if found is not None:
            return found
    return None


def _parse_xml(text: str) -> ET._Element:
    cleaned = strip_dtd(text)
    return ET.fromstring(cleaned.encode("utf-8"), parser=ET.XMLParser(recover=True))


def _clamp_span(val: int, max_val: int) -> int:
    return max(1, min(val, max_val))


# ─── 스타일 맵 ─────────────────────────────────────────

@dataclass
class CharProperty:
    font_size: Optional[float] = None
    bold: Optional[bool] = None
    italic: Optional[bool] = None
    font_name: Optional[str] = None


@dataclass
class HwpxStyleMap:
    char_properties: Dict[str, CharProperty] = field(default_factory=dict)
    styles: Dict[str, dict] = field(default_factory=dict)


def _parse_char_properties(root: ET._Element, result: HwpxStyleMap) -> None:
    for el in root.iter():
        if _tag(el) not in ("charPr", "char_pr"):
            continue
        pr_id = _attr(el, "id") or _attr(el, "IDRef") or ""
        if not pr_id:
            continue
        prop = CharProperty()
        height = _attr(el, "height")
        if height:
            try:
                h = int(height)
                if h > 0:
                    prop.font_size = h / 100.0
            except ValueError:
                pass
        bold = _attr(el, "bold")
        if bold in ("true", "1"):
            prop.bold = True
        italic = _attr(el, "italic")
        if italic in ("true", "1"):
            prop.italic = True
        for child in el.iter():
            child_tag = _tag(child)
            if child_tag in ("fontface", "fontRef"):
                face = _attr(child, "face") or _attr(child, "FontFace")
                if face:
                    prop.font_name = face
                    break
        result.char_properties[pr_id] = prop


def _parse_style_elements(root: ET._Element, result: HwpxStyleMap) -> None:
    for i, el in enumerate(root.iter()):
        if _tag(el) != "style":
            continue
        st_id = _attr(el, "id") or _attr(el, "IDRef") or str(i)
        name = _attr(el, "name") or _attr(el, "engName") or ""
        char_pr_id = _attr(el, "charPrIDRef")
        para_pr_id = _attr(el, "paraPrIDRef")
        result.styles[st_id] = {"name": name, "char_pr_id": char_pr_id, "para_pr_id": para_pr_id}


def _extract_hwpx_styles(zf: zipfile.ZipFile) -> HwpxStyleMap:
    result = HwpxStyleMap()
    names_lower = {n.lower(): n for n in zf.namelist()}
    header_paths = ["Contents/header.xml", "header.xml", "Contents/head.xml", "head.xml"]
    for hp in header_paths:
        actual = names_lower.get(hp.lower())
        if not actual:
            continue
        try:
            xml = zf.read(actual).decode("utf-8", errors="replace")
            root = _parse_xml(xml)
            _parse_char_properties(root, result)
            _parse_style_elements(root, result)
            break
        except Exception:
            continue
    return result


# ─── 메타데이터 ──────────────────────────────────────────

def _parse_dublin_core(xml: str, meta: DocumentMetadata) -> None:
    try:
        root = _parse_xml(xml)

        def _get(*tag_names: str) -> Optional[str]:
            for el in root.iter():
                if _tag(el) in tag_names:
                    t = (el.text or "").strip()
                    if t:
                        return t
            return None

        meta.title = meta.title or _get("title")
        meta.author = meta.author or _get("creator", "lastModifiedBy")
        meta.description = meta.description or _get("description", "subject")
        meta.created_at = meta.created_at or _get("created", "creation-date")
        meta.modified_at = meta.modified_at or _get("modified", "date")
        kw = _get("keyword", "keywords")
        if kw and not meta.keywords:
            meta.keywords = [k.strip() for k in re.split(r"[,;]", kw) if k.strip()]
    except Exception:
        pass


def _extract_hwpx_metadata(zf: zipfile.ZipFile) -> DocumentMetadata:
    meta = DocumentMetadata()
    names_lower = {n.lower(): n for n in zf.namelist()}
    for mp in ["meta.xml", "meta-inf/meta.xml", "docprops/core.xml"]:
        actual = names_lower.get(mp)
        if not actual:
            continue
        try:
            xml = zf.read(actual).decode("utf-8", errors="replace")
            _parse_dublin_core(xml, meta)
            if meta.title or meta.author:
                break
        except Exception:
            continue
    return meta


# ─── 섹션 경로 해석 ──────────────────────────────────────

def _parse_section_paths_from_manifest(xml: str) -> List[str]:
    try:
        root = _parse_xml(xml)
        items: Dict[str, str] = {}
        spine_ids: List[str] = []

        def _is_section_id(id_: str) -> bool:
            return id_.lower().startswith("s") or "section" in id_.lower()

        for el in root.iter():
            tag = _tag(el)
            if tag == "item":
                item_id = _attr(el, "id") or ""
                href = _attr(el, "href") or ""
                media_type = _attr(el, "media-type") or ""
                if not _is_section_id(item_id) and "xml" not in media_type:
                    continue
                if href and not href.startswith("/") and not href.startswith("Contents/"):
                    if _is_section_id(item_id):
                        href = "Contents/" + href
                if item_id and href:
                    items[item_id] = href
            elif tag == "itemref":
                idref = _attr(el, "idref") or ""
                if idref:
                    spine_ids.append(idref)

        if spine_ids:
            ordered = [items[i] for i in spine_ids if i in items]
            if ordered:
                return ordered

        return sorted(
            [v for k, v in items.items() if _is_section_id(k)],
            key=lambda x: x
        )
    except Exception:
        return []


def _resolve_section_paths(zf: zipfile.ZipFile) -> List[str]:
    names = set(zf.namelist())
    names_lower = {n.lower(): n for n in names}

    for mp in ["Contents/content.hpf", "content.hpf"]:
        actual = names_lower.get(mp.lower())
        if not actual:
            continue
        try:
            xml = zf.read(actual).decode("utf-8", errors="replace")
            paths = _parse_section_paths_from_manifest(xml)
            if paths:
                return paths
        except Exception:
            continue

    # fallback: Section*.xml 직접 검색
    section_files = sorted(
        n for n in names
        if re.search(r"[Ss]ection\d+\.xml$", n)
    )
    return section_files


# ─── 이미지 추출 ─────────────────────────────────────────

_MIME_MAP = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png", "gif": "image/gif",
    "bmp": "image/bmp", "tif": "image/tiff", "tiff": "image/tiff",
    "wmf": "image/wmf", "emf": "image/emf", "svg": "image/svg+xml",
}


def _mime_to_ext(mime: str) -> str:
    if "jpeg" in mime: return "jpg"
    if "png" in mime: return "png"
    if "gif" in mime: return "gif"
    if "bmp" in mime: return "bmp"
    if "tiff" in mime: return "tif"
    if "wmf" in mime: return "wmf"
    if "emf" in mime: return "emf"
    if "svg" in mime: return "svg"
    return "bin"


def _extract_images_from_zip(
    zf: zipfile.ZipFile,
    blocks: List[IRBlock],
    warnings: List[ParseWarning],
) -> List[ExtractedImage]:
    images: List[ExtractedImage] = []
    names = set(zf.namelist())
    img_idx = 0

    for block in blocks:
        if block.type != "image" or not block.text:
            continue
        ref = block.text
        candidates = [f"BinData/{ref}", f"Contents/BinData/{ref}", ref]

        # 확장자 없는 ref → 확장자 붙여서 탐색
        resolved = None
        if "." not in ref:
            for prefix in [f"BinData/{ref}", f"Contents/BinData/{ref}"]:
                for name in names:
                    if name.startswith(prefix) and "." in name[len(prefix):]:
                        resolved = name
                        break
                if resolved:
                    break

        all_candidates = ([resolved] if resolved else []) + candidates
        found = False
        for path in all_candidates:
            if not path or is_path_traversal(path) or path not in names:
                continue
            try:
                data = zf.read(path)
                img_idx += 1
                ext = path.rsplit(".", 1)[-1].lower() if "." in path else "png"
                mime = _MIME_MAP.get(ext, "image/png")
                filename = f"image_{img_idx:03d}.{_mime_to_ext(mime)}"
                images.append(ExtractedImage(filename=filename, data=data, mime_type=mime))
                block.text = filename
                found = True
                break
            except Exception:
                pass

        if not found:
            warnings.append(ParseWarning(
                page=block.page_number,
                message=f"이미지 파일 없음: {ref}",
                code="SKIPPED_IMAGE",
            ))
            block.type = "paragraph"
            block.text = f"[이미지: {ref}]"

    return images


# ─── 손상 ZIP 복구 ───────────────────────────────────────

def _extract_from_broken_zip(data: bytes) -> InternalParseResult:
    warnings = [ParseWarning(
        message="손상된 ZIP 구조 — Local File Header 기반 복구 모드",
        code="BROKEN_ZIP_RECOVERY",
    )]
    blocks: List[IRBlock] = []
    pos = 0
    total_decompressed = 0
    entry_count = 0
    section_num = 0
    nested_counter = [0]

    while pos < len(data) - 30:
        # PK\x03\x04 시그니처 스캔
        if data[pos:pos + 4] != b"PK\x03\x04":
            next_pk = data.find(b"PK\x03\x04", pos + 1)
            if next_pk == -1:
                break
            pos = next_pk
            continue

        entry_count += 1
        if entry_count > MAX_ZIP_ENTRIES:
            break

        try:
            method = struct.unpack_from("<H", data, pos + 8)[0]
            comp_size = struct.unpack_from("<I", data, pos + 18)[0]
            name_len = struct.unpack_from("<H", data, pos + 26)[0]
            extra_len = struct.unpack_from("<H", data, pos + 28)[0]
        except struct.error:
            break

        if name_len > 1024 or extra_len > 65535:
            pos += 30 + name_len + extra_len
            continue

        file_start = pos + 30 + name_len + extra_len
        if file_start + comp_size > len(data):
            break
        if comp_size == 0 and method != 0:
            pos = file_start
            continue

        name_bytes = data[pos + 30: pos + 30 + name_len]
        try:
            name = name_bytes.decode("utf-8", errors="replace")
        except Exception:
            pos = file_start + comp_size
            continue

        if is_path_traversal(name):
            pos = file_start + comp_size
            continue

        file_data = data[file_start: file_start + comp_size]
        pos = file_start + comp_size

        if "section" not in name.lower() or not name.endswith(".xml"):
            continue

        try:
            if method == 0:
                content = file_data.decode("utf-8", errors="replace")
            elif method == 8:
                decompressed = zlib.decompress(file_data, -15)
                content = decompressed.decode("utf-8", errors="replace")
            else:
                continue

            total_decompressed += len(content) * 2
            if total_decompressed > MAX_DECOMPRESS_SIZE:
                raise KordocError("압축 해제 크기 초과")

            section_num += 1
            blocks.extend(_parse_section_xml(content, None, warnings, section_num, nested_counter))
        except KordocError:
            raise
        except Exception:
            continue

    if not blocks:
        raise KordocError("손상된 HWPX에서 섹션 데이터를 복구할 수 없습니다")

    markdown = blocks_to_markdown(blocks)
    return InternalParseResult(markdown=markdown, blocks=blocks, warnings=warnings)


# ─── 헤딩 감지 ───────────────────────────────────────────

def _detect_hwpx_headings(blocks: List[IRBlock], style_map: HwpxStyleMap) -> None:
    size_freq: Dict[float, int] = {}
    for b in blocks:
        if b.style and b.style.font_size:
            fs = b.style.font_size
            size_freq[fs] = size_freq.get(fs, 0) + 1

    base_font_size = 0.0
    max_count = 0
    for size, count in size_freq.items():
        if count > max_count:
            max_count = count
            base_font_size = size

    for block in blocks:
        if block.type != "paragraph" or not block.text:
            continue
        text = block.text.strip()
        if not text or len(text) > 200 or text.isdigit():
            continue

        level = 0
        if base_font_size > 0 and block.style and block.style.font_size:
            ratio = block.style.font_size / base_font_size
            if ratio >= HEADING_RATIO_H1:
                level = 1
            elif ratio >= HEADING_RATIO_H2:
                level = 2
            elif ratio >= HEADING_RATIO_H3:
                level = 3

        compact = re.sub(r"\s+", "", text)
        if re.match(r"^제\d+[조장절편]", compact) and len(text) <= 50:
            if level == 0:
                level = 3

        if level > 0:
            block.type = "heading"
            block.level = level


# ─── 섹션 XML 파싱 ───────────────────────────────────────

@dataclass
class TableState:
    rows: List[List[CellContext]] = field(default_factory=list)
    current_row: List[CellContext] = field(default_factory=list)
    cell: Optional[CellContext] = None


def _extract_image_ref(el: ET._Element) -> Optional[str]:
    for child in el:
        child_tag = _tag(child)
        if child_tag in ("imgRect", "img", "imgClip"):
            ref = _attr(child, "binaryItemIDRef") or _attr(child, "href")
            if ref:
                return ref
        nested = _extract_image_ref(child)
        if nested:
            return nested
    return _attr(el, "binaryItemIDRef") or None


def _extract_text_from_node(node: ET._Element) -> str:
    parts = []
    if node.text:
        parts.append(node.text)
    for child in node:
        parts.append(_extract_text_from_node(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts).strip()


def _make_nested_table_marker(counter: list, rows: List[List[CellContext]]) -> str:
    counter[0] += 1
    first_row = rows[0] if rows else []
    hint_parts = [c.text.strip().replace("\n", " ") for c in first_row if c.text.strip()]
    hint = " | ".join(hint_parts)
    if len(hint) > 60:
        hint = hint[:60] + "…"
    if hint:
        return f"[중첩 테이블 #{counter[0]}: {hint}]"
    return f"[중첩 테이블 #{counter[0]}]"


def _extract_paragraph_info(
    para: ET._Element,
    style_map: Optional[HwpxStyleMap],
) -> tuple[str, Optional[str], Optional[str], Optional[InlineStyle]]:
    """(text, href, footnote, style) 반환"""
    text_parts: List[str] = []
    href: Optional[str] = None
    footnote: Optional[str] = None
    char_pr_id: Optional[str] = None

    _SKIP_TAGS = {
        "ctrl", "fieldBegin", "fieldEnd", "parameters", "stringParam",
        "integerParam", "boolParam", "floatParam", "secPr", "colPr",
        "linesegarray", "lineseg", "pic", "shape", "drawingObject",
        "shapeComment", "drawText", "tbl",
    }

    def walk(node: ET._Element) -> None:
        nonlocal href, footnote, char_pr_id

        if node.text:
            text_parts.append(node.text)

        for child in node:
            tag = _tag(child)

            if tag in _SKIP_TAGS:
                if child.tail:
                    text_parts.append(child.tail)
                continue

            if tag == "t":
                walk(child)
            elif tag == "tab":
                leader = _attr(child, "leader")
                if leader and leader != "0":
                    text_parts.append("\x1F")
                else:
                    text_parts.append("\t")
            elif tag == "br":
                if (_attr(child, "type") or "line") == "line":
                    text_parts.append("\n")
            elif tag in ("fwSpace", "hwSpace"):
                text_parts.append(" ")
            elif tag == "hyperlink":
                url = _attr(child, "url") or _attr(child, "href") or ""
                if url:
                    safe = sanitize_href(url)
                    if safe:
                        href = safe
                walk(child)
            elif tag in ("footNote", "endNote", "fn", "en"):
                note = _extract_text_from_node(child)
                if note:
                    footnote = (footnote + "; " + note) if footnote else note
            elif tag == "equation":
                script_el = _find_child(child, "script")
                raw = _extract_text_from_node(script_el) if script_el is not None else ""
                if raw.strip():
                    try:
                        latex = hml_to_latex(raw).strip()
                        if latex:
                            text_parts.append(f" ${latex}$ ")
                    except Exception:
                        pass
            elif tag == "r":
                rpr = _attr(child, "charPrIDRef")
                if rpr and not char_pr_id:
                    char_pr_id = rpr
                walk(child)
            else:
                walk(child)

            if child.tail:
                text_parts.append(child.tail)

    walk(para)

    text = "".join(text_parts)
    # 목차 리더 마커 이후 제거
    leader_idx = text.find("\x1F")
    if leader_idx >= 0:
        text = text[:leader_idx]

    text = re.sub(r"[ \t]+", " ", text).strip()

    # OLE/이미지 대체 텍스트 필터
    if re.match(r"^그림입니다\.?\s*원본\s*그림의\s*(이름|크기)", text):
        text = ""
    text = re.sub(
        r"(?:모서리가 둥근 |둥근 )?(?:사각형|직사각형|정사각형|원|타원|삼각형|선|직선|곡선|화살표|오각형|육각형|팔각형|별|십자|구름|마름모|도넛|평행사변형|사다리꼴|개체|그리기\s?개체|묶음\s?개체|글상자|표|그림|OLE\s?개체)\s?입니다\.?",
        "", text
    ).strip()

    # 스타일 정보
    style: Optional[InlineStyle] = None
    if style_map and char_pr_id:
        prop = style_map.char_properties.get(char_pr_id)
        if prop:
            if prop.font_size or prop.bold or prop.italic:
                style = InlineStyle(
                    font_size=prop.font_size,
                    bold=prop.bold or None,
                    italic=prop.italic or None,
                    font_name=prop.font_name,
                )

    return text, href, footnote, style


def _extract_draw_text_blocks(
    node: ET._Element,
    blocks: List[IRBlock],
    style_map: Optional[HwpxStyleMap],
    section_num: Optional[int],
) -> None:
    for child in node:
        tag = _tag(child)
        if tag == "subList":
            _extract_draw_text_blocks(child, blocks, style_map, section_num)
        elif tag in ("p", "para"):
            text, href, footnote, style = _extract_paragraph_info(child, style_map)
            text = text.strip()
            if text:
                blocks.append(IRBlock(
                    type="paragraph",
                    text=text,
                    style=style,
                    page_number=section_num,
                ))


def _walk_section(
    node: ET._Element,
    blocks: List[IRBlock],
    table_ctx: Optional[TableState],
    table_stack: List[TableState],
    style_map: Optional[HwpxStyleMap],
    warnings: List[ParseWarning],
    section_num: Optional[int],
    nested_counter: list,
    depth: int = 0,
) -> Optional[TableState]:
    if depth > MAX_XML_DEPTH:
        return table_ctx

    for child in node:
        tag = _tag(child)

        if tag == "tbl":
            if table_ctx is not None:
                table_stack.append(table_ctx)
            new_table = TableState()
            new_table = _walk_section(
                child, blocks, new_table, table_stack,
                style_map, warnings, section_num, nested_counter, depth + 1
            ) or new_table

            if new_table.rows:
                if table_stack:
                    parent_table = table_stack.pop()
                    nested_cols = max((len(r) for r in new_table.rows), default=0)
                    marker = _make_nested_table_marker(nested_counter, new_table.rows)
                    if new_table.rows and len(new_table.rows) >= 3 and nested_cols >= 2:
                        blocks.append(IRBlock(
                            type="table",
                            table=build_table(new_table.rows),
                            page_number=section_num,
                        ))
                    else:
                        nested_text = convert_table_to_text(new_table.rows)
                        if parent_table.cell:
                            sep = "\n" if parent_table.cell.text else ""
                            parent_table.cell.text += sep + marker + "\n" + nested_text
                    if parent_table.cell:
                        sep = "\n" if parent_table.cell.text else ""
                        parent_table.cell.text += sep + marker
                    table_ctx = parent_table
                else:
                    blocks.append(IRBlock(
                        type="table",
                        table=build_table(new_table.rows),
                        page_number=section_num,
                    ))
                    table_ctx = None
            else:
                table_ctx = table_stack.pop() if table_stack else None

        elif tag == "tr":
            if table_ctx is not None:
                table_ctx.current_row = []
                table_ctx = _walk_section(
                    child, blocks, table_ctx, table_stack,
                    style_map, warnings, section_num, nested_counter, depth + 1
                )
                if table_ctx and table_ctx.current_row:
                    table_ctx.rows.append(table_ctx.current_row)
                    table_ctx.current_row = []

        elif tag == "tc":
            if table_ctx is not None:
                table_ctx.cell = CellContext(text="", col_span=1, row_span=1)
                table_ctx = _walk_section(
                    child, blocks, table_ctx, table_stack,
                    style_map, warnings, section_num, nested_counter, depth + 1
                )
                if table_ctx and table_ctx.cell is not None:
                    table_ctx.current_row.append(table_ctx.cell)
                    table_ctx.cell = None

        elif tag == "cellAddr":
            if table_ctx and table_ctx.cell is not None:
                ca = _attr(child, "colAddr")
                ra = _attr(child, "rowAddr")
                try:
                    if ca is not None:
                        table_ctx.cell.col_addr = int(ca)
                    if ra is not None:
                        table_ctx.cell.row_addr = int(ra)
                except ValueError:
                    pass

        elif tag == "cellSpan":
            if table_ctx and table_ctx.cell is not None:
                try:
                    cs = int(_attr(child, "colSpan") or "1")
                    rs = int(_attr(child, "rowSpan") or "1")
                    table_ctx.cell.col_span = _clamp_span(cs if cs > 0 else 1, MAX_COLS)
                    table_ctx.cell.row_span = _clamp_span(rs if rs > 0 else 1, MAX_ROWS)
                except ValueError:
                    pass

        elif tag == "p":
            text, href, footnote, style = _extract_paragraph_info(child, style_map)
            if text:
                if table_ctx and table_ctx.cell is not None:
                    sep = "\n" if table_ctx.cell.text else ""
                    table_ctx.cell.text += sep + text
                elif table_ctx is None:
                    block = IRBlock(type="paragraph", text=text, page_number=section_num)
                    if style:
                        block.style = style
                    if href:
                        block.href = href
                    if footnote:
                        block.footnote_text = footnote
                    blocks.append(block)

            # <p> 내부 tbl/이미지 처리
            table_ctx = _walk_paragraph_children(
                child, blocks, table_ctx, table_stack,
                style_map, warnings, section_num, nested_counter, depth + 1
            )

        elif tag in ("pic", "shape", "drawingObject"):
            draw_text = _find_descendant(child, "drawText")
            if draw_text is not None:
                _extract_draw_text_blocks(draw_text, blocks, style_map, section_num)
            else:
                img_ref = _extract_image_ref(child)
                if img_ref:
                    blocks.append(IRBlock(type="image", text=img_ref, page_number=section_num))
                elif section_num:
                    warnings.append(ParseWarning(
                        page=section_num,
                        message=f"스킵된 요소: {tag}",
                        code="SKIPPED_IMAGE",
                    ))

        else:
            table_ctx = _walk_section(
                child, blocks, table_ctx, table_stack,
                style_map, warnings, section_num, nested_counter, depth + 1
            )

    return table_ctx


def _walk_paragraph_children(
    node: ET._Element,
    blocks: List[IRBlock],
    table_ctx: Optional[TableState],
    table_stack: List[TableState],
    style_map: Optional[HwpxStyleMap],
    warnings: List[ParseWarning],
    section_num: Optional[int],
    nested_counter: list,
    depth: int = 0,
) -> Optional[TableState]:
    if depth > MAX_XML_DEPTH:
        return table_ctx

    for child in node:
        tag = _tag(child)

        if tag == "tbl":
            if table_ctx is not None:
                table_stack.append(table_ctx)
            new_table = TableState()
            new_table = _walk_section(
                child, blocks, new_table, table_stack,
                style_map, warnings, section_num, nested_counter, depth + 1
            ) or new_table
            if new_table.rows:
                if table_stack:
                    parent_table = table_stack.pop()
                    marker = _make_nested_table_marker(nested_counter, new_table.rows)
                    blocks.append(IRBlock(
                        type="table",
                        table=build_table(new_table.rows),
                        page_number=section_num,
                    ))
                    if parent_table.cell:
                        sep = "\n" if parent_table.cell.text else ""
                        parent_table.cell.text += sep + marker
                    table_ctx = parent_table
                else:
                    blocks.append(IRBlock(
                        type="table",
                        table=build_table(new_table.rows),
                        page_number=section_num,
                    ))
                    table_ctx = None
            else:
                table_ctx = table_stack.pop() if table_stack else None

        elif tag in ("pic", "shape", "drawingObject"):
            draw_text = _find_descendant(child, "drawText")
            if draw_text is not None:
                _extract_draw_text_blocks(draw_text, blocks, style_map, section_num)
            else:
                img_ref = _extract_image_ref(child)
                if img_ref:
                    blocks.append(IRBlock(type="image", text=img_ref, page_number=section_num))

        elif tag == "drawText":
            _extract_draw_text_blocks(child, blocks, style_map, section_num)

        else:
            table_ctx = _walk_paragraph_children(
                child, blocks, table_ctx, table_stack,
                style_map, warnings, section_num, nested_counter, depth + 1
            )

    return table_ctx


def _parse_section_xml(
    xml: str,
    style_map: Optional[HwpxStyleMap],
    warnings: List[ParseWarning],
    section_num: Optional[int],
    nested_counter: list,
) -> List[IRBlock]:
    try:
        root = _parse_xml(xml)
    except Exception as e:
        if warnings is not None:
            warnings.append(ParseWarning(
                page=section_num,
                message=f"섹션 XML 파싱 실패: {e}",
                code="MALFORMED_XML",
            ))
        return []
    blocks: List[IRBlock] = []
    _walk_section(root, blocks, None, [], style_map, warnings, section_num, nested_counter)
    return blocks


# ─── 메인 파서 ───────────────────────────────────────────

def parse_hwpx_document(data: bytes, options: Optional[ParseOptions] = None) -> InternalParseResult:
    precheck_zip_size(data, MAX_DECOMPRESS_SIZE, MAX_ZIP_ENTRIES)

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        return _extract_from_broken_zip(data)

    warnings: List[ParseWarning] = []

    with zf:
        actual_entries = len(zf.namelist())
        if actual_entries > MAX_ZIP_ENTRIES:
            raise KordocError("ZIP 엔트리 수 초과 (ZIP bomb 의심)")

        # DRM 감지 (간략 — COM fallback 미구현)
        try:
            manifest_xml = zf.read("META-INF/manifest.xml").decode("utf-8", errors="replace")
            if "encryption-data" in manifest_xml:
                raise KordocError("DRM 암호화된 HWPX 파일입니다.")
        except KeyError:
            pass

        # 메타데이터
        metadata = _extract_hwpx_metadata(zf)

        # 스타일 맵
        style_map = _extract_hwpx_styles(zf)

        # 섹션 경로
        section_paths = _resolve_section_paths(zf)
        if not section_paths:
            raise KordocError("HWPX에서 섹션 파일을 찾을 수 없습니다")

        metadata.page_count = len(section_paths)

        page_filter = parse_page_range(options.pages, len(section_paths)) if options and options.pages else None
        total_target = len(page_filter) if page_filter else len(section_paths)

        blocks: List[IRBlock] = []
        nested_counter = [0]
        names = set(zf.namelist())
        parsed = 0

        for si, path in enumerate(section_paths):
            if page_filter and (si + 1) not in page_filter:
                continue
            if path not in names:
                continue
            try:
                xml = zf.read(path).decode("utf-8", errors="replace")
                new_blocks = _parse_section_xml(xml, style_map, warnings, si + 1, nested_counter)
                blocks.extend(new_blocks)
                parsed += 1
                if options and options.on_progress:
                    options.on_progress(parsed, total_target)
            except KordocError:
                raise
            except Exception as e:
                warnings.append(ParseWarning(
                    page=si + 1,
                    message=f"섹션 {si + 1} 파싱 실패: {e}",
                    code="PARTIAL_PARSE",
                ))

        # 이미지 추출
        images = _extract_images_from_zip(zf, blocks, warnings)

    # 헤딩 감지
    _detect_hwpx_headings(blocks, style_map)

    outline = [
        OutlineItem(level=b.level or 1, text=b.text or "", page_number=b.page_number)
        for b in blocks
        if b.type == "heading" and b.text
    ]

    markdown = blocks_to_markdown(blocks)
    return InternalParseResult(
        markdown=markdown,
        blocks=blocks,
        metadata=metadata,
        outline=outline or None,
        warnings=warnings or None,
        images=images or None,
    )
