"""skdconv 공통 타입 정의"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Literal, Optional, Union

# ─── Intermediate Representation ─────────────────────────

IRBlockType = Literal["paragraph", "table", "heading", "list", "image", "separator"]

FileType = Literal["hwpx", "hwp", "hwp3", "hwpml", "pdf", "xlsx", "xls", "docx", "unknown"]

WarningCode = Literal[
    "SKIPPED_IMAGE",
    "SKIPPED_OLE",
    "TRUNCATED_TABLE",
    "OCR_FALLBACK",
    "UNSUPPORTED_ELEMENT",
    "BROKEN_ZIP_RECOVERY",
    "HIDDEN_TEXT_FILTERED",
    "MALFORMED_XML",
    "PARTIAL_PARSE",
    "LENIENT_CFB_RECOVERY",
]

ErrorCode = Literal[
    "EMPTY_INPUT",
    "UNSUPPORTED_FORMAT",
    "ENCRYPTED",
    "DRM_PROTECTED",
    "CORRUPTED",
    "DECOMPRESSION_BOMB",
    "ZIP_BOMB",
    "IMAGE_BASED_PDF",
    "NO_SECTIONS",
    "PARSE_ERROR",
    "MISSING_DEPENDENCY",
]

DiffChangeType = Literal["added", "removed", "modified", "unchanged"]

OcrProvider = Callable[[bytes, int, Literal["image/png"]], str]


@dataclass
class CellContext:
    text: str
    col_span: int = 1
    row_span: int = 1
    col_addr: Optional[int] = None
    row_addr: Optional[int] = None


@dataclass
class BoundingBox:
    page: int
    x: float
    y: float
    width: float
    height: float


@dataclass
class InlineStyle:
    bold: Optional[bool] = None
    italic: Optional[bool] = None
    font_size: Optional[float] = None
    font_name: Optional[str] = None


@dataclass
class ImageData:
    data: bytes
    mime_type: str
    filename: Optional[str] = None


@dataclass
class IRCell:
    text: str
    col_span: int = 1
    row_span: int = 1


@dataclass
class IRTable:
    rows: int
    cols: int
    cells: List[List[IRCell]]
    has_header: bool


@dataclass
class IRBlock:
    type: IRBlockType
    text: Optional[str] = None
    table: Optional[IRTable] = None
    level: Optional[int] = None
    page_number: Optional[int] = None
    bbox: Optional[BoundingBox] = None
    style: Optional[InlineStyle] = None
    list_type: Optional[Literal["ordered", "unordered"]] = None
    children: Optional[List[IRBlock]] = None
    href: Optional[str] = None
    footnote_text: Optional[str] = None
    image_data: Optional[ImageData] = None


# ─── 메타데이터 ─────────────────────────────────────

@dataclass
class DocumentMetadata:
    title: Optional[str] = None
    author: Optional[str] = None
    creator: Optional[str] = None
    created_at: Optional[str] = None
    modified_at: Optional[str] = None
    page_count: Optional[int] = None
    version: Optional[str] = None
    description: Optional[str] = None
    keywords: Optional[List[str]] = None


# ─── 파싱 옵션 ──────────────────────────────────────

@dataclass
class ParseOptions:
    pages: Optional[Union[List[int], str]] = None
    ocr: Optional[OcrProvider] = None
    on_progress: Optional[Callable[[int, int], None]] = None
    remove_header_footer: Optional[bool] = None
    file_path: Optional[str] = None
    formula_ocr: bool = False


# ─── 파싱 경고 ──────────────────────────────────────

@dataclass
class ParseWarning:
    message: str
    code: WarningCode
    page: Optional[int] = None


@dataclass
class OutlineItem:
    level: int
    text: str
    page_number: Optional[int] = None


# ─── 추출된 이미지 ──────────────────────────────────

@dataclass
class ExtractedImage:
    filename: str
    data: bytes
    mime_type: str


# ─── 파싱 결과 ──────────────────────────────────────

@dataclass
class ParseSuccess:
    file_type: FileType
    markdown: str
    blocks: List[IRBlock]
    success: bool = field(default=True, init=False)
    page_count: Optional[int] = None
    is_image_based: Optional[bool] = None
    metadata: Optional[DocumentMetadata] = None
    outline: Optional[List[OutlineItem]] = None
    warnings: Optional[List[ParseWarning]] = None
    images: Optional[List[ExtractedImage]] = None


@dataclass
class ParseFailure:
    file_type: FileType
    error: str
    success: bool = field(default=False, init=False)
    code: Optional[ErrorCode] = None
    page_count: Optional[int] = None
    is_image_based: Optional[bool] = None


ParseResult = Union[ParseSuccess, ParseFailure]


# ─── 문서 비교 (Diff) ───────────────────────────────

@dataclass
class CellDiff:
    type: DiffChangeType
    before: Optional[str] = None
    after: Optional[str] = None


@dataclass
class BlockDiff:
    type: DiffChangeType
    before: Optional[IRBlock] = None
    after: Optional[IRBlock] = None
    cell_diffs: Optional[List[List[CellDiff]]] = None
    similarity: Optional[float] = None


@dataclass
class DiffStats:
    added: int = 0
    removed: int = 0
    modified: int = 0
    unchanged: int = 0


@dataclass
class DiffResult:
    stats: DiffStats
    diffs: List[BlockDiff]


# ─── 양식 인식 ──────────────────────────────────────

@dataclass
class FormField:
    label: str
    value: str
    row: int
    col: int


@dataclass
class FormResult:
    fields: List[FormField]
    confidence: float


# ─── 헤딩 감지 공통 임계값 ──────────────────────────

HEADING_RATIO_H1 = 1.5
HEADING_RATIO_H2 = 1.3
HEADING_RATIO_H3 = 1.15


# ─── 내부 파서 반환 타입 ─────────────────────────────

@dataclass
class InternalParseResult:
    markdown: str
    blocks: List[IRBlock]
    metadata: Optional[DocumentMetadata] = None
    outline: Optional[List[OutlineItem]] = None
    warnings: Optional[List[ParseWarning]] = None
    images: Optional[List[ExtractedImage]] = None
    is_image_based: Optional[bool] = None
