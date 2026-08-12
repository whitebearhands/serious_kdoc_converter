"""HWP 5.x 레코드 리더, UTF-16LE 텍스트 추출, 스트림 압축해제."""

from __future__ import annotations
import struct
import zlib
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..utils import SKDConvError

# ─── 레코드 태그 상수 ────────────────────────────────
TAG_PARA_HEADER   = 0x0042
TAG_PARA_TEXT     = 0x0043
TAG_CHAR_SHAPE    = 0x0044
TAG_PARA_SHAPE    = 0x0045
TAG_CTRL_HEADER   = 0x0047
TAG_LIST_HEADER   = 0x0048
TAG_TABLE         = 0x004D
TAG_EQEDIT        = 0x0058

TAG_ID_MAPPINGS   = 0x0011
TAG_FACE_NAME     = 0x0013
TAG_DOC_CHAR_SHAPE = 0x0015
TAG_DOC_PARA_SHAPE = 0x0019
TAG_DOC_STYLE     = 0x001A

# 특수 문자 코드
_CHAR_LINE        = 0x0000
_CHAR_SECTION_BREAK = 0x000A
_CHAR_PARA        = 0x000D
_CHAR_TAB         = 0x0009
_CHAR_HYPHEN      = 0x001E
_CHAR_NBSP        = 0x001F
_CHAR_FIXED_NBSP  = 0x0018
_CHAR_FIXED_WIDTH = 0x0019

# FileHeader 플래그
FLAG_COMPRESSED  = 1 << 0
FLAG_ENCRYPTED   = 1 << 1
FLAG_DISTRIBUTION = 1 << 2
FLAG_DRM         = 1 << 4

_MAX_RECORDS = 500_000
_MAX_DECOMPRESS_SIZE = 100 * 1024 * 1024


@dataclass
class HwpRecord:
    tag_id: int
    level: int
    size: int
    data: bytes


@dataclass
class HwpFileHeader:
    signature: str
    version_major: int
    flags: int


@dataclass
class HwpParaShape:
    outline_level: int


@dataclass
class HwpCharShape:
    font_size: int
    attr_flags: int


@dataclass
class HwpStyle:
    name: str
    name_ko: str
    char_shape_id: int
    para_shape_id: int
    type: int


@dataclass
class HwpDocInfo:
    char_shapes: list[HwpCharShape] = field(default_factory=list)
    para_shapes: list[HwpParaShape] = field(default_factory=list)
    styles: list[HwpStyle] = field(default_factory=list)


def read_records(data: bytes) -> list[HwpRecord]:
    records: list[HwpRecord] = []
    offset = 0

    while offset + 4 <= len(data) and len(records) < _MAX_RECORDS:
        hdr = struct.unpack_from("<I", data, offset)[0]
        offset += 4

        tag_id = hdr & 0x3FF
        level = (hdr >> 10) & 0x3FF
        size = (hdr >> 20) & 0xFFF

        if size == 0xFFF:
            if offset + 4 > len(data):
                break
            size = struct.unpack_from("<I", data, offset)[0]
            offset += 4

        if offset + size > len(data):
            break
        records.append(HwpRecord(tag_id=tag_id, level=level, size=size, data=data[offset:offset + size]))
        offset += size

    return records


def decompress_stream(data: bytes) -> bytes:
    if len(data) >= 2 and data[0] == 0x78:
        try:
            return zlib.decompress(data)
        except Exception:
            pass
    return zlib.decompress(data, -15)


def parse_file_header(data: bytes) -> HwpFileHeader:
    if len(data) < 40:
        raise SKDConvError("FileHeader가 너무 짧습니다 (최소 40바이트)")
    sig = data[:32].decode("utf-8", errors="replace").rstrip("\x00")
    version_major = data[35]
    flags = struct.unpack_from("<I", data, 36)[0]
    return HwpFileHeader(signature=sig, version_major=version_major, flags=flags)


def parse_doc_info(records: list[HwpRecord]) -> HwpDocInfo:
    char_shapes: list[HwpCharShape] = []
    para_shapes: list[HwpParaShape] = []
    styles: list[HwpStyle] = []

    for rec in records:
        if rec.tag_id == TAG_DOC_PARA_SHAPE and rec.size >= 4:
            flags = struct.unpack_from("<I", rec.data, 0)[0]
            outline_level = (flags >> 25) & 0x07
            para_shapes.append(HwpParaShape(outline_level=outline_level))

        if rec.tag_id == TAG_DOC_CHAR_SHAPE and rec.size >= 50:
            font_size = struct.unpack_from("<I", rec.data, 42)[0]
            attr_flags = struct.unpack_from("<I", rec.data, 46)[0]
            char_shapes.append(HwpCharShape(font_size=font_size, attr_flags=attr_flags))
        elif rec.tag_id == TAG_DOC_CHAR_SHAPE and rec.size >= 18:
            char_shapes.append(HwpCharShape(font_size=0, attr_flags=0))

        if rec.tag_id == TAG_DOC_STYLE and rec.size >= 8:
            try:
                off = 0
                name_len = struct.unpack_from("<H", rec.data, off)[0]; off += 2
                name_bytes = name_len * 2
                name = rec.data[off:off + name_bytes].decode("utf-16-le", errors="replace") if name_bytes > 0 and off + name_bytes <= rec.size else ""
                off += name_bytes

                name_ko = ""
                if off + 2 <= rec.size:
                    name_ko_len = struct.unpack_from("<H", rec.data, off)[0]; off += 2
                    name_ko_bytes = name_ko_len * 2
                    if name_ko_bytes > 0 and off + name_ko_bytes <= rec.size:
                        name_ko = rec.data[off:off + name_ko_bytes].decode("utf-16-le", errors="replace")
                    off += name_ko_bytes

                stype = rec.data[off] if off < rec.size else 0; off += 1
                off += 2  # nextStyleId
                off += 2  # langId
                para_shape_id = struct.unpack_from("<H", rec.data, off)[0] if off + 2 <= rec.size else 0; off += 2
                char_shape_id = struct.unpack_from("<H", rec.data, off)[0] if off + 2 <= rec.size else 0

                styles.append(HwpStyle(name=name, name_ko=name_ko, char_shape_id=char_shape_id, para_shape_id=para_shape_id, type=stype))
            except Exception:
                pass

    return HwpDocInfo(char_shapes=char_shapes, para_shapes=para_shapes, styles=styles)


InlineControlResolver = Callable[[str], Optional[str]]


def extract_text(data: bytes) -> str:
    return extract_text_with_controls(data)


def extract_text_with_controls(data: bytes, resolve_control: Optional[InlineControlResolver] = None) -> str:
    result: list[str] = []
    i = 0

    while i + 1 < len(data):
        ch = struct.unpack_from("<H", data, i)[0]
        i += 2

        if ch == _CHAR_LINE:
            result.append("\n")
        elif ch == _CHAR_SECTION_BREAK:
            if i + 16 <= len(data) and struct.unpack_from("<H", data, i)[0] == 0x000B:
                ctrl_id = data[i + 2:i + 6].decode("ascii", errors="replace")
                replacement = resolve_control(ctrl_id) if resolve_control else None
                if replacement:
                    result.append(replacement)
                i += 16
            else:
                result.append("\n")
                if i + 14 <= len(data):
                    i += 14
        elif ch == _CHAR_PARA:
            pass
        elif ch == _CHAR_HYPHEN:
            result.append("-")
        elif ch == _CHAR_NBSP:
            result.append(" ")
        elif ch == _CHAR_FIXED_NBSP:
            result.append(" ")
        elif ch == _CHAR_FIXED_WIDTH:
            result.append(" ")
        elif ch == _CHAR_TAB:
            result.append("\t")
            if i + 14 <= len(data):
                i += 14
        elif 0x0001 <= ch <= 0x001F:
            is_extended = (1 <= ch <= 3) or (11 <= ch <= 12) or (14 <= ch <= 18) or (21 <= ch <= 23)
            is_inline = (4 <= ch <= 9) or (19 <= ch <= 20)
            if (is_extended or is_inline) and i + 14 <= len(data):
                ctrl_id = data[i:i + 4].decode("ascii", errors="replace")
                replacement = resolve_control(ctrl_id) if resolve_control else None
                if replacement:
                    result.append(replacement)
                i += 14
        elif ch >= 0x0020:
            # UTF-16 surrogate pair
            if 0xD800 <= ch <= 0xDBFF and i + 1 < len(data):
                lo = struct.unpack_from("<H", data, i)[0]
                if 0xDC00 <= lo <= 0xDFFF:
                    i += 2
                    cp = ((ch - 0xD800) << 10) + (lo - 0xDC00) + 0x10000
                    result.append(chr(cp))
                    continue
            result.append(chr(ch))

    return "".join(result)


def extract_equation_text(data: bytes) -> Optional[str]:
    if len(data) < 6:
        return None
    script_length = struct.unpack_from("<H", data, 4)[0]
    start = 6
    end = start + script_length * 2
    if script_length <= 0 or end > len(data):
        return None
    eq = data[start:end].decode("utf-16-le", errors="replace").replace("\x00", "").strip()
    return eq or None
