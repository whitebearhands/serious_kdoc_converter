"""skdconv — 한국 공문서를 마크다운으로 변환하는 파서 라이브러리."""

from .types import (
    IRBlock,
    IRCell,
    IRTable,
    CellContext,
    ParseResult,
    ParseSuccess,
    ParseFailure,
    ParseOptions,
    DocumentMetadata,
    ParseWarning,
    OutlineItem,
    ExtractedImage,
    FileType,
    InternalParseResult,
)
from .utils import KordocError, VERSION
from .detect import detect_format, detect_zip_format, detect_ole2_format

__version__ = VERSION


def parse(data: bytes, options: "ParseOptions | None" = None) -> ParseResult:
    """포맷 자동 감지 후 파싱 → ParseResult."""
    from .utils import classify_error

    if not data:
        return ParseFailure(file_type="unknown", error="입력 데이터가 비어 있습니다", code="EMPTY_INPUT")

    fmt = detect_format(data)
    # ZIP/OLE2 내부 구조로 세분화
    if fmt == "hwpx":
        refined = detect_zip_format(data)
        if refined in ("xlsx", "docx", "hwpx"):
            fmt = refined
    elif fmt == "hwp":
        refined = detect_ole2_format(data)
        if refined in ("hwp", "xls"):
            fmt = refined

    try:
        result: InternalParseResult
        if fmt == "hwpx":
            from .hwpx.parser import parse_hwpx_document
            result = parse_hwpx_document(data, options)
        elif fmt == "hwp":
            from .hwp5.parser import parse_hwp5_document
            result = parse_hwp5_document(data, options)
        elif fmt == "hwp3":
            from .hwp3.parser import parse_hwp3_document
            result = parse_hwp3_document(data, options)
        elif fmt == "hwpml":
            from .hwpml.parser import parse_hwpml_document
            result = parse_hwpml_document(data, options)
        elif fmt == "xlsx":
            from .xlsx.parser import parse_xlsx_document
            result = parse_xlsx_document(data, options)
        elif fmt == "xls":
            from .xls.parser import parse_xls_document
            result = parse_xls_document(data, options)
        elif fmt == "docx":
            from .docx.parser import parse_docx_document
            result = parse_docx_document(data, options)
        else:
            return ParseFailure(file_type=fmt, error=f"지원하지 않는 포맷: {fmt}", code="UNSUPPORTED_FORMAT")

        md = result.metadata
        return ParseSuccess(
            file_type=fmt,
            markdown=result.markdown,
            blocks=result.blocks,
            page_count=md.page_count if md else None,
            metadata=md,
            outline=result.outline,
            warnings=result.warnings,
            images=result.images,
        )
    except Exception as e:
        code = classify_error(e)
        return ParseFailure(file_type=fmt, error=str(e), code=code)


__all__ = [
    "IRBlock",
    "IRCell",
    "IRTable",
    "CellContext",
    "ParseResult",
    "ParseSuccess",
    "ParseFailure",
    "ParseOptions",
    "DocumentMetadata",
    "ParseWarning",
    "OutlineItem",
    "ExtractedImage",
    "FileType",
    "KordocError",
    "VERSION",
    "detect_format",
    "detect_zip_format",
    "detect_ole2_format",
    "parse",
]
