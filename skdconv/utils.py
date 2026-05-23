"""skdconv 공용 유틸리티"""

from __future__ import annotations

import re
import struct
from typing import Optional, Tuple

VERSION = "1.0.3"


class KordocError(Exception):
    pass


def sanitize_error(err: BaseException) -> str:
    if isinstance(err, KordocError):
        return str(err)
    return "문서 처리 중 오류가 발생했습니다"


def is_path_traversal(name: str) -> bool:
    """ZIP 엔트리 경로의 경로 순회 여부 판별."""
    if "\x00" in name:
        return True
    normalized = name.replace("\\", "/")
    segments = normalized.split("/")
    if any(s == ".." for s in segments):
        return True
    if normalized.startswith("/"):
        return True
    if re.match(r"^[A-Za-z]:", normalized):
        return True
    return False


def precheck_zip_size(
    data: bytes,
    max_uncompressed_size: int = 100 * 1024 * 1024,
    max_entries: int = 500,
) -> Tuple[int, int]:
    """ZIP bomb 사전 검사 — Central Directory에서 비압축 합계와 엔트리 수 확인."""
    try:
        length = len(data)
        eocd_offset = -1
        for i in range(length - 22, max(0, length - 65557) - 1, -1):
            if struct.unpack_from("<I", data, i)[0] == 0x06054B50:
                eocd_offset = i
                break
        if eocd_offset < 0:
            return 0, 0

        entry_count = struct.unpack_from("<H", data, eocd_offset + 10)[0]
        if entry_count > max_entries:
            raise KordocError(f"ZIP 엔트리 수 초과: {entry_count} (최대 {max_entries})")

        cd_size = struct.unpack_from("<I", data, eocd_offset + 12)[0]
        cd_offset = struct.unpack_from("<I", data, eocd_offset + 16)[0]
        if cd_offset + cd_size > length:
            return 0, entry_count

        total_uncompressed = 0
        pos = cd_offset
        for _ in range(entry_count):
            if pos + 46 > cd_offset + cd_size:
                break
            sig = struct.unpack_from("<I", data, pos)[0]
            if sig != 0x02014B50:
                break
            total_uncompressed += struct.unpack_from("<I", data, pos + 24)[0]
            name_len = struct.unpack_from("<H", data, pos + 28)[0]
            extra_len = struct.unpack_from("<H", data, pos + 30)[0]
            comment_len = struct.unpack_from("<H", data, pos + 32)[0]
            pos += 46 + name_len + extra_len + comment_len

        if total_uncompressed > max_uncompressed_size:
            mb = total_uncompressed / 1024 / 1024
            max_mb = max_uncompressed_size / 1024 / 1024
            raise KordocError(
                f"ZIP 비압축 크기 초과: {mb:.1f}MB (최대 {max_mb:.0f}MB)"
            )

        return total_uncompressed, entry_count
    except KordocError:
        raise
    except Exception:
        return 0, 0


def strip_dtd(xml: str) -> str:
    """XXE/Billion Laughs 방지 — DOCTYPE 제거."""
    return re.sub(r"<!DOCTYPE\s[^[>]*(\[[\s\S]*?\])?\s*>", "", xml, flags=re.IGNORECASE)


_SAFE_HREF_RE = re.compile(r"^(?:https?:|mailto:|tel:|#)", re.IGNORECASE)


def sanitize_href(href: str) -> Optional[str]:
    """하이퍼링크 URL 살균 — javascript: 등 XSS 위험 스킴 차단."""
    trimmed = href.strip()
    if not trimmed or not _SAFE_HREF_RE.match(trimmed):
        return None
    return trimmed


def safe_min(arr: list) -> float:
    result = float("inf")
    for v in arr:
        if v < result:
            result = v
    return result


def safe_max(arr: list) -> float:
    result = float("-inf")
    for v in arr:
        if v > result:
            result = v
    return result


def classify_error(err: BaseException) -> str:
    """에러를 구조화된 ErrorCode로 분류."""
    msg = str(err)
    if "암호화" in msg:
        return "ENCRYPTED"
    if "DRM" in msg:
        return "DRM_PROTECTED"
    if "ZIP bomb" in msg or "ZIP 비압축 크기 초과" in msg or "ZIP 엔트리 수 초과" in msg:
        return "ZIP_BOMB"
    if "bomb" in msg or "크기 초과" in msg or "압축 해제" in msg:
        return "DECOMPRESSION_BOMB"
    if "이미지 기반" in msg:
        return "IMAGE_BASED_PDF"
    if "섹션" in msg and ("찾을 수 없" in msg or "없음" in msg):
        return "NO_SECTIONS"
    if "시그니처" in msg or "복구할 수 없" in msg:
        return "CORRUPTED"
    return "PARSE_ERROR"
