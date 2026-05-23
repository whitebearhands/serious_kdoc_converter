"""XLS 문자열 인코딩 디코더."""

from __future__ import annotations


def decode_utf16le(buf: bytes) -> str:
    return buf.decode("utf-16-le", errors="replace")


def decode_compressed(buf: bytes) -> str:
    """Compressed Unicode (1바이트/문자, ISO-8859-1)."""
    return buf.decode("latin-1", errors="replace")


def decode_cp949(buf: bytes) -> str:
    try:
        return buf.decode("euc-kr", errors="replace")
    except LookupError:
        return buf.decode("latin-1", errors="replace")


def decode_by_codepage(buf: bytes, code_page: int) -> str:
    if code_page == 1200:
        return decode_utf16le(buf)
    if code_page == 949:
        return decode_cp949(buf)
    if code_page == 1252:
        try:
            return buf.decode("cp1252", errors="replace")
        except LookupError:
            return buf.decode("latin-1", errors="replace")
    if code_page == 65001:
        return buf.decode("utf-8", errors="replace")
    return buf.decode("latin-1", errors="replace")
