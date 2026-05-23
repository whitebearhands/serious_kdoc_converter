"""DOCX (Office Open XML Document) 파서

ZIP + XML 구조를 파싱하여 IRBlock[]로 변환.
w:p → paragraph/heading, w:tbl → table, w:drawing → image.
"""

from __future__ import annotations

import io
import zipfile
from typing import Dict, List, Optional, Set, Tuple

import lxml.etree as ET

from ..types import (
    DocumentMetadata, ExtractedImage, InternalParseResult, IRBlock,
    IRCell, IRTable, ParseOptions, ParseWarning,
)
from ..utils import KordocError, precheck_zip_size, strip_dtd
from ..table.builder import blocks_to_markdown
from .equation import is_display_math, omml_element_to_latex

MAX_DECOMPRESS_SIZE = 100 * 1024 * 1024


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


def _children(parent: ET._Element, local_name: str) -> List[ET._Element]:
    return [child for child in parent if _tag(child) == local_name]


def _find_all(parent: ET._Element, local_name: str) -> List[ET._Element]:
    return [el for el in parent.iter() if _tag(el) == local_name]


def _first(parent: ET._Element, local_name: str) -> Optional[ET._Element]:
    for child in parent:
        if _tag(child) == local_name:
            return child
    return None


def _parse_xml(text: str) -> ET._Element:
    cleaned = strip_dtd(text)
    return ET.fromstring(cleaned.encode("utf-8"), parser=ET.XMLParser(recover=True))


# ─── 스타일 파싱 ────────────────────────────────────────

def _parse_styles(xml: str) -> Dict[str, Dict]:
    """styleId → {name, outline_level}"""
    root = _parse_xml(xml)
    styles: Dict[str, Dict] = {}

    for el in _find_all(root, "style"):
        style_id = _attr(el, "styleId")
        if not style_id:
            continue

        name_els = _children(el, "name")
        name = _attr(name_els[0], "val") or "" if name_els else ""

        based_on_els = _children(el, "basedOn")
        based_on = _attr(based_on_els[0], "val") if based_on_els else None

        outline_level: Optional[int] = None
        ppr_els = _children(el, "pPr")
        if ppr_els:
            outline_els = _children(ppr_els[0], "outlineLvl")
            if outline_els:
                val = _attr(outline_els[0], "val")
                if val is not None:
                    try:
                        outline_level = int(val)
                    except ValueError:
                        pass

        if outline_level is None:
            import re
            m = re.match(r"^heading\s*(\d+)$", name, re.IGNORECASE)
            if m:
                outline_level = int(m.group(1)) - 1

        styles[style_id] = {
            "name": name,
            "based_on": based_on,
            "outline_level": outline_level,
        }

    return styles


# ─── 번호 매기기 파싱 ──────────────────────────────────

def _parse_numbering(xml: str) -> Dict[str, Dict[int, Dict]]:
    """numId → {level → {num_fmt, level}}"""
    root = _parse_xml(xml)

    abstract_nums: Dict[str, Dict[int, Dict]] = {}
    for el in _find_all(root, "abstractNum"):
        abstract_id = _attr(el, "abstractNumId")
        if not abstract_id:
            continue
        levels: Dict[int, Dict] = {}
        for lvl in _children(el, "lvl"):
            try:
                ilvl = int(_attr(lvl, "ilvl") or "0")
            except ValueError:
                ilvl = 0
            num_fmt_els = _children(lvl, "numFmt")
            num_fmt = _attr(num_fmt_els[0], "val") or "bullet" if num_fmt_els else "bullet"
            levels[ilvl] = {"num_fmt": num_fmt, "level": ilvl}
        abstract_nums[abstract_id] = levels

    nums: Dict[str, Dict[int, Dict]] = {}
    for el in _find_all(root, "num"):
        num_id = _attr(el, "numId")
        if not num_id:
            continue
        abstract_refs = _children(el, "abstractNumId")
        if abstract_refs:
            ref = _attr(abstract_refs[0], "val")
            if ref and ref in abstract_nums:
                nums[num_id] = abstract_nums[ref]

    return nums


# ─── 관계 파싱 ─────────────────────────────────────────

def _parse_rels(xml: str) -> Dict[str, str]:
    root = _parse_xml(xml)
    result = {}
    for rel in _find_all(root, "Relationship"):
        rel_id = _attr(rel, "Id")
        target = _attr(rel, "Target")
        if rel_id and target:
            result[rel_id] = target
    return result


# ─── 각주 파싱 ─────────────────────────────────────────

def _parse_footnotes(xml: str) -> Dict[str, str]:
    root = _parse_xml(xml)
    notes: Dict[str, str] = {}
    for fn in _find_all(root, "footnote"):
        fn_id = _attr(fn, "id")
        if not fn_id or fn_id in ("0", "-1"):
            continue
        texts: List[str] = []
        for p in _find_all(fn, "p"):
            for r in _find_all(p, "r"):
                for t in _children(r, "t"):
                    texts.append(t.text or "")
        notes[fn_id] = "".join(texts).strip()
    return notes


# ─── OMML 수집 ────────────────────────────────────────

def _collect_omml_roots(p: ET._Element) -> List[ET._Element]:
    """단락 내 최상위 oMath / oMathPara 수집."""
    out: List[ET._Element] = []

    def walk(node: ET._Element) -> None:
        for child in node:
            tag = _tag(child)
            if tag in ("oMath", "oMathPara"):
                out.append(child)
            else:
                walk(child)

    walk(p)
    return out


# ─── Run 텍스트 추출 ──────────────────────────────────

def _extract_run(r: ET._Element) -> Tuple[str, bool, bool]:
    """(text, bold, italic)"""
    t_els = _children(r, "t")
    text = "".join(t.text or "" for t in t_els)

    bold = False
    italic = False
    rpr_els = _children(r, "rPr")
    if rpr_els:
        bold = bool(_children(rpr_els[0], "b"))
        italic = bool(_children(rpr_els[0], "i"))

    return text, bold, italic


# ─── 단락 파싱 ─────────────────────────────────────────

def _parse_paragraph(
    p: ET._Element,
    styles: Dict[str, Dict],
    numbering: Dict[str, Dict[int, Dict]],
    footnotes: Dict[str, str],
    rels: Dict[str, str],
) -> Optional[IRBlock]:
    ppr_els = _children(p, "pPr")
    style_id = ""
    num_id = ""
    ilvl = 0

    if ppr_els:
        pstyle_els = _children(ppr_els[0], "pStyle")
        if pstyle_els:
            style_id = _attr(pstyle_els[0], "val") or ""

        numpr_els = _children(ppr_els[0], "numPr")
        if numpr_els:
            numid_els = _children(numpr_els[0], "numId")
            ilvl_els = _children(numpr_els[0], "ilvl")
            num_id = _attr(numid_els[0], "val") or "" if numid_els else ""
            try:
                ilvl = int(_attr(ilvl_els[0], "val") or "0") if ilvl_els else 0
            except ValueError:
                ilvl = 0

    parts: List[str] = []
    has_bold = False
    has_italic = False
    href: Optional[str] = None
    footnote_text: Optional[str] = None

    # 하이퍼링크
    for hl in _children(p, "hyperlink"):
        r_id = _attr(hl, "id")
        hl_text_parts: List[str] = []
        for r in _find_all(hl, "r"):
            t, _, _ = _extract_run(r)
            hl_text_parts.append(t)
        text = "".join(hl_text_parts)
        if text:
            if r_id and r_id in rels:
                href = rels[r_id]
            parts.append(text)

    # 일반 run
    for r in _children(p, "r"):
        # 하이퍼링크 내부 run은 이미 처리됨
        parent_tag = _tag(r.getparent()) if r.getparent() is not None else ""
        if parent_tag == "hyperlink":
            continue

        text, bold, italic = _extract_run(r)
        if bold:
            has_bold = True
        if italic:
            has_italic = True

        fn_refs = _children(r, "footnoteReference")
        if fn_refs:
            fn_id = _attr(fn_refs[0], "id")
            if fn_id and fn_id in footnotes:
                footnote_text = footnotes[fn_id]

        if text:
            parts.append(text)

    # OMML 수식
    for om in _collect_omml_roots(p):
        latex = omml_element_to_latex(om)
        if not latex:
            continue
        if is_display_math(om):
            parts.append(f" $${latex}$$ ")
        else:
            parts.append(f" ${latex}$ ")

    import re
    text = re.sub(r"[ \t]{2,}", " ", "".join(parts)).strip()
    if not text:
        return None

    style = styles.get(style_id, {})
    outline = style.get("outline_level")
    if outline is not None and 0 <= outline <= 5:
        return IRBlock(type="heading", text=text, level=outline + 1)

    if num_id and num_id != "0":
        num_def = numbering.get(num_id, {})
        level_info = num_def.get(ilvl, {})
        list_type = "unordered" if level_info.get("num_fmt") == "bullet" else "ordered"
        return IRBlock(type="list", text=text, list_type=list_type)

    block = IRBlock(type="paragraph", text=text)
    if href:
        block.href = href
    if footnote_text:
        block.footnote_text = footnote_text
    return block


# ─── 테이블 파싱 ────────────────────────────────────────

def _parse_table(
    tbl: ET._Element,
    styles: Dict[str, Dict],
    numbering: Dict[str, Dict[int, Dict]],
    footnotes: Dict[str, str],
    rels: Dict[str, str],
) -> Optional[IRBlock]:
    tr_els = _children(tbl, "tr")
    if not tr_els:
        return None

    rows: List[List[IRCell]] = []
    max_cols = 0

    for tr in tr_els:
        tc_els = _children(tr, "tc")
        row: List[IRCell] = []

        for tc in tc_els:
            col_span = 1
            row_span = 1
            tcpr_els = _children(tc, "tcPr")
            is_vmerge_continue = False

            if tcpr_els:
                gs_els = _children(tcpr_els[0], "gridSpan")
                if gs_els:
                    try:
                        col_span = int(_attr(gs_els[0], "val") or "1")
                    except ValueError:
                        pass

                vm_els = _children(tcpr_els[0], "vMerge")
                if vm_els:
                    val = _attr(vm_els[0], "val")
                    if val != "restart" and val is not None:
                        row.append(IRCell(text="", col_span=col_span, row_span=0))
                        is_vmerge_continue = True

            if is_vmerge_continue:
                continue

            cell_texts: List[str] = []
            for p_el in _children(tc, "p"):
                blk = _parse_paragraph(p_el, styles, numbering, footnotes, rels)
                if blk and blk.text:
                    cell_texts.append(blk.text)

            row.append(IRCell(text="\n".join(cell_texts), col_span=col_span, row_span=row_span))

        rows.append(row)
        if len(row) > max_cols:
            max_cols = len(row)

    # vMerge rowSpan 후처리
    for c in range(max_cols):
        for r in range(len(rows)):
            if c >= len(rows[r]):
                continue
            cell = rows[r][c]
            if cell is None or cell.row_span == 0:
                continue
            span = 1
            for nr in range(r + 1, len(rows)):
                if c < len(rows[nr]) and rows[nr][c].row_span == 0:
                    span += 1
                else:
                    break
            rows[r][c] = IRCell(text=cell.text, col_span=cell.col_span, row_span=span)

    # rowSpan=0 placeholder 제거
    clean_rows = [[cell for cell in row if cell.row_span != 0] for row in rows]
    clean_rows = [row for row in clean_rows if row]
    if not clean_rows:
        return None

    cols = max(
        sum(cell.col_span for cell in row)
        for row in clean_rows
    )

    table = IRTable(
        rows=len(clean_rows),
        cols=cols,
        cells=clean_rows,
        has_header=len(clean_rows) > 1,
    )
    return IRBlock(type="table", table=table)


# ─── 이미지 추출 ────────────────────────────────────────

def _extract_images(
    zf: zipfile.ZipFile,
    rels: Dict[str, str],
    doc: ET._Element,
) -> Tuple[List[IRBlock], List[ExtractedImage]]:
    blocks: List[IRBlock] = []
    images: List[ExtractedImage] = []
    names = set(zf.namelist())
    img_idx = 0

    _MIME_MAP = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "bmp": "image/bmp", "wmf": "image/wmf", "emf": "image/emf",
    }

    for drawing in _find_all(doc, "drawing"):
        for blip in _find_all(drawing, "blip"):
            embed_id = _attr(blip, "embed")
            if not embed_id:
                continue
            target = rels.get(embed_id)
            if not target:
                continue

            if target.startswith("/"):
                img_path = target[1:]
            elif target.startswith("word/"):
                img_path = target
            else:
                img_path = f"word/{target}"

            if img_path not in names:
                continue

            try:
                data = zf.read(img_path)
                img_idx += 1
                ext = img_path.rsplit(".", 1)[-1].lower() if "." in img_path else "png"
                filename = f"image_{img_idx:03d}.{ext}"
                images.append(ExtractedImage(
                    filename=filename,
                    data=data,
                    mime_type=_MIME_MAP.get(ext, "image/png"),
                ))
                blocks.append(IRBlock(type="image", text=filename))
            except Exception:
                pass

    return blocks, images


# ─── 메인 파서 ─────────────────────────────────────────

def parse_docx_document(data: bytes, options: Optional[ParseOptions] = None) -> InternalParseResult:
    precheck_zip_size(data, MAX_DECOMPRESS_SIZE)

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise KordocError(f"유효하지 않은 DOCX 파일: {e}") from e

    warnings: List[ParseWarning] = []

    with zf:
        names = set(zf.namelist())

        if "word/document.xml" not in names:
            raise KordocError("유효하지 않은 DOCX 파일: word/document.xml이 없습니다")

        # 1. 관계
        rels: Dict[str, str] = {}
        if "word/_rels/document.xml.rels" in names:
            try:
                rels = _parse_rels(
                    zf.read("word/_rels/document.xml.rels").decode("utf-8", errors="replace")
                )
            except Exception:
                pass

        # 2. 스타일
        styles: Dict[str, Dict] = {}
        if "word/styles.xml" in names:
            try:
                styles = _parse_styles(
                    zf.read("word/styles.xml").decode("utf-8", errors="replace")
                )
            except Exception:
                pass

        # 3. 번호 매기기
        numbering: Dict[str, Dict[int, Dict]] = {}
        if "word/numbering.xml" in names:
            try:
                numbering = _parse_numbering(
                    zf.read("word/numbering.xml").decode("utf-8", errors="replace")
                )
            except Exception:
                pass

        # 4. 각주
        footnotes: Dict[str, str] = {}
        if "word/footnotes.xml" in names:
            try:
                footnotes = _parse_footnotes(
                    zf.read("word/footnotes.xml").decode("utf-8", errors="replace")
                )
            except Exception:
                pass

        # 5. 본문 파싱
        doc_xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
        doc = _parse_xml(doc_xml)

        body_els = _find_all(doc, "body")
        if not body_els:
            raise KordocError("DOCX 본문(w:body)을 찾을 수 없습니다")

        blocks: List[IRBlock] = []
        body = body_els[0]

        for child in body:
            tag = _tag(child)
            if tag == "p":
                blk = _parse_paragraph(child, styles, numbering, footnotes, rels)
                if blk:
                    blocks.append(blk)
            elif tag == "tbl":
                blk = _parse_table(child, styles, numbering, footnotes, rels)
                if blk:
                    blocks.append(blk)

        # 6. 이미지 추출
        _, images = _extract_images(zf, rels, doc)

        # 7. 메타데이터
        metadata = DocumentMetadata()
        if "docProps/core.xml" in names:
            try:
                core = _parse_xml(
                    zf.read("docProps/core.xml").decode("utf-8", errors="replace")
                )

                def _get(tag_name: str) -> Optional[str]:
                    for el in core.iter():
                        if _tag(el) == tag_name:
                            return (el.text or "").strip() or None
                    return None

                metadata.title = _get("title")
                metadata.author = _get("creator")
                metadata.description = _get("description")
                metadata.created_at = _get("created")
                metadata.modified_at = _get("modified")
            except Exception:
                pass

    # 8. 아웃라인
    from ..types import OutlineItem
    outline = [
        OutlineItem(level=b.level or 2, text=b.text or "")
        for b in blocks
        if b.type == "heading"
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
