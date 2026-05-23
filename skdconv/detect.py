"""매직 바이트 기반 파일 포맷 감지"""

from __future__ import annotations

import io
import zipfile
from typing import Literal

from .types import FileType

_HWP3_PREFIX = b"HWP Document File V3.00"


def is_zip_file(data: bytes) -> bool:
    return len(data) >= 4 and data[:4] == b"PK\x03\x04"


def is_hwpx_file(data: bytes) -> bool:
    return is_zip_file(data)


def is_old_hwp_file(data: bytes) -> bool:
    return len(data) >= 4 and data[:4] == b"\xd0\xcf\x11\xe0"


def is_hwp3_file(data: bytes) -> bool:
    return len(data) >= len(_HWP3_PREFIX) and data[: len(_HWP3_PREFIX)] == _HWP3_PREFIX


def is_pdf_file(data: bytes) -> bool:
    return len(data) >= 4 and data[:4] == b"%PDF"


def is_hwpml_file(data: bytes) -> bool:
    head = data[:512].decode("utf-8", errors="replace").lstrip("﻿")
    return head.lstrip().startswith("<?xml") and "<HWPML" in head


def detect_format(data: bytes) -> FileType:
    """동기 포맷 감지 — ZIP은 모두 'hwpx'로 반환 (세분화는 detect_zip_format 사용)."""
    if len(data) < 4:
        return "unknown"
    if is_hwp3_file(data):
        return "hwp3"
    if is_zip_file(data):
        return "hwpx"
    if is_old_hwp_file(data):
        return "hwp"
    if is_pdf_file(data):
        return "pdf"
    if is_hwpml_file(data):
        return "hwpml"
    return "unknown"


def detect_ole2_format(data: bytes) -> Literal["hwp", "xls", "unknown"]:
    """OLE2 컨테이너 내부 스트림 기반 포맷 세분화.

    HWP 5.x, XLS 모두 OLE2이므로 스트림 이름으로 구분.
    """
    try:
        import olefile  # type: ignore

        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            entries = {e[0] for e in ole.listdir()}
            if "Workbook" in entries or "Book" in entries:
                return "xls"
            if "FileHeader" in entries:
                return "hwp"
            if any(e == "DocInfo" or e.startswith("Section") for e in entries):
                return "hwp"
        return "unknown"
    except Exception:
        return "unknown"


def detect_zip_format(data: bytes) -> Literal["hwpx", "xlsx", "docx", "unknown"]:
    """ZIP 내부 구조 기반 포맷 세분화.

    HWPX, XLSX, DOCX 모두 ZIP이므로 내부 파일로 구분.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            name_set = set(names)
            if "xl/workbook.xml" in name_set:
                return "xlsx"
            if "word/document.xml" in name_set:
                return "docx"
            if "Contents/content.hpf" in name_set or "mimetype" in name_set:
                return "hwpx"
            if any(n.startswith("Contents/") for n in names):
                return "hwpx"
        return "unknown"
    except Exception:
        return "unknown"
