"""HWP 3.0 텍스트 추출 파서.

단일 binary stream: signature + DocInfo + DocSummary + InfoBlock + Body.
Body 는 paragraph list (compressed != 0 이면 raw deflate).
출처: rhwp/src/parser/hwp3/mod.rs (Apache-2.0)
"""

from __future__ import annotations
import zlib
import struct
from dataclasses import dataclass, field
from typing import Optional

from ..types import IRBlock, InternalParseResult, DocumentMetadata, ParseOptions, ParseWarning
from .johab import decode_johab, JOHAB_UNMAPPED
from .reader import Reader
from .records import read_header

PARA_HEADER_FIXED_SIZE = 43
PARA_SHAPE_SIZE = 187
LINE_INFO_SIZE = 14
INLINE_CHAR_SHAPE_SIZE = 31

# (extraBytes, extraHchar, emit_char)  — None emit means skip silently
_SIMPLE_CTRL: dict[int, tuple[int, int, str | None]] = {
    9:  (0,   0,  "\t"),
    7:  (6,   3,  "￼"),
    8:  (6,   3,  "￼"),
    18: (6,   3,  " "),
    19: (6,   3,  "￼"),
    20: (6,   3,  "￼"),
    21: (6,   3,  "￼"),
    22: (22,  11, "￼"),
    23: (8,   4,  "￼"),
    24: (4,   2,  "-"),
    25: (4,   2,  "-"),
    26: (244, 122, "￼"),
    28: (62,  31, "￼"),
    30: (2,   1,  " "),
    31: (2,   1,  " "),
}


@dataclass
class _ParaCtx:
    paragraphs: list[str] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)


def parse_hwp3_document(data: bytes, _options: Optional[ParseOptions] = None) -> InternalParseResult:
    reader = Reader(data)
    header = read_header(reader)

    if header.encrypted != 0:
        err = ValueError("HWP3 본문이 암호로 보호되어 있어 추출할 수 없습니다.")
        err.code = "ENCRYPTED"  # type: ignore[attr-defined]
        raise err

    reader.skip(header.info_block_length)
    tail = reader.read_to_end()

    warnings: list[ParseWarning] = []
    if header.compressed != 0:
        try:
            body = zlib.decompress(tail, -15)  # raw deflate
        except Exception as e:
            raise ValueError(f"HWP3 압축 해제 실패: {e}") from e
    else:
        body = tail

    body_reader = Reader(body)
    ctx = _ParaCtx(warnings=warnings)
    try:
        _skip_font_faces_and_styles(body_reader)
        _parse_paragraph_list(body_reader, ctx)
    except Exception as e:
        warnings.append(ParseWarning(
            code="PARTIAL_PARSE",
            message=f"HWP3 paragraph stream 도중 파싱 중단: {e}",
        ))

    non_empty = [p for p in ctx.paragraphs if p]
    text = "\n\n".join(non_empty)
    blocks: list[IRBlock] = [IRBlock(type="paragraph", text=p) for p in ctx.paragraphs]

    metadata = DocumentMetadata(
        title=header.title or None,
        author=header.author or None,
        description=header.subject or None,
        created_at=header.date or None,
        version="3.0",
    )

    return InternalParseResult(
        markdown=text,
        blocks=blocks,
        metadata=metadata,
        warnings=warnings if warnings else None,
    )


def _skip_font_faces_and_styles(reader: Reader) -> None:
    STYLE_RECORD_SIZE = 20 + 31 + 187  # = 238
    for _ in range(7):
        n = reader.read_u16()
        reader.skip(n * 40)
    n_styles = reader.read_u16()
    reader.skip(n_styles * STYLE_RECORD_SIZE)


def _parse_paragraph_list(reader: Reader, ctx: _ParaCtx) -> None:
    while True:
        if reader.eof():
            return

        follow_prev = reader.read_u8()
        char_count = reader.read_u16()
        if char_count == 0:
            reader.skip(40)
            return

        line_count = reader.read_u16()
        if char_count > 60000 or line_count > 4096:
            ctx.warnings.append(ParseWarning(
                code="PARTIAL_PARSE",
                message=f"HWP3 비정상 paragraph 헤더 (char_count={char_count}, line_count={line_count}) → 이후 stream 포기",
            ))
            return

        include_char_shape = reader.read_u8()
        reader.skip(1)   # flags
        reader.skip(4)   # special_char_flags
        reader.skip(1)   # style_index
        reader.skip(31)  # rep_char_shape
        if follow_prev == 0:
            reader.skip(PARA_SHAPE_SIZE)

        reader.skip(line_count * LINE_INFO_SIZE)

        if include_char_shape != 0:
            for _ in range(char_count):
                flag = reader.read_u8()
                if flag != 1:
                    reader.skip(INLINE_CHAR_SHAPE_SIZE)

        try:
            text = _parse_char_stream(reader, char_count, ctx)
            ctx.paragraphs.append(text)
        except Exception as e:
            ctx.warnings.append(ParseWarning(
                code="PARTIAL_PARSE",
                message=f"HWP3 paragraph #{len(ctx.paragraphs)} char stream 파싱 실패: {e}",
            ))
            return


def _parse_char_stream(reader: Reader, char_count: int, ctx: _ParaCtx) -> str:
    out: list[str] = []
    i = 0
    while i < char_count:
        ch = reader.read_u16()
        i += 1

        if ch == 13:
            out.append("\n")
            continue
        if ch == 0:
            continue
        if ch >= 32:
            cp = decode_johab(ch)
            if cp != JOHAB_UNMAPPED:
                out.append(chr(cp))
            continue

        simple = _SIMPLE_CTRL.get(ch)
        if simple:
            extra_bytes, extra_hchar, emit = simple
            reader.skip(extra_bytes)
            i += extra_hchar
            if emit:
                out.append(emit)
            continue

        # ch = 10/11/12/14/15/16/17/27/29 등: 8 byte 추가 헤더
        header_val1 = reader.read_u32()
        reader.read_u16()  # ch2
        i += 3

        if ch == 10:
            out.append(_parse_table_like(reader, ctx))
        elif ch == 11:
            _parse_picture(reader)
        elif ch == 12:
            reader.skip(84)
        elif ch == 14:
            reader.skip(84)
        elif ch == 15:
            reader.skip(8)
            _parse_paragraph_list(reader, ctx)
        elif ch == 16:
            reader.skip(10)
            _parse_paragraph_list(reader, ctx)
        elif ch == 17:
            reader.skip(14)
            _parse_paragraph_list(reader, ctx)
        elif ch == 29:
            if header_val1 < 1_000_000:
                reader.skip(header_val1)
        else:
            if not any(w.code == "UNSUPPORTED_ELEMENT" for w in ctx.warnings):
                ctx.warnings.append(ParseWarning(
                    code="UNSUPPORTED_ELEMENT",
                    message=f"HWP3 부분 처리 제어 문자 ch={ch} (이후 동일 코드 경고 생략)",
                ))

    return "".join(out).strip()


def _parse_table_like(reader: Reader, ctx: _ParaCtx) -> str:
    info = reader.read_bytes(84)
    cell_count = struct.unpack_from("<H", info, 80)[0] or 1
    if cell_count > 256:
        ctx.warnings.append(ParseWarning(
            code="PARTIAL_PARSE",
            message=f"HWP3 표 cell_count={cell_count} 비정상 — 표 본문 추출 포기",
        ))
        raise ValueError(f"HWP3 비정상 cell_count={cell_count}")

    reader.skip(27 * cell_count)
    for _ in range(cell_count):
        _parse_paragraph_list(reader, ctx)
    _parse_paragraph_list(reader, ctx)
    return ""


def _parse_picture(reader: Reader) -> None:
    info = reader.read_bytes(348)
    n_ext = struct.unpack_from("<I", info, 0)[0]
    if 0 < n_ext < 100 * 1024 * 1024:
        reader.skip(n_ext)
