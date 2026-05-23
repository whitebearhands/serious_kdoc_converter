"""HWP 3.0 상용 조합형 → 유니코드 디코더.

cho/jung/jong 비트 분해로 0xAC00 한글 음절 매핑.
한자/기호 등은 johab_symbols 테이블로 처리.
출처: rhwp/src/parser/hwp3/johab.rs (Apache-2.0)
"""

from __future__ import annotations
from .johab_symbols import JOHAB_SYMBOLS

CHO_MAP: tuple[int, ...] = (
    -1, -1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18,
    -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1,
)
JUNG_MAP: tuple[int, ...] = (
    -1, -1, -1, 0, 1, 2, 3, 4, -1, -1, 5, 6, 7, 8, 9, 10, -1, -1, 11, 12, 13, 14,
    15, 16, -1, -1, 17, 18, 19, 20, -1, -1,
)
JONG_MAP: tuple[int, ...] = (
    -1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, -1, 17, 18, 19,
    20, 21, 22, 23, 24, 25, 26, 27, -1, -1,
)

JOHAB_UNMAPPED = -1


def _lookup_symbol(ch: int) -> int | None:
    lo, hi = 0, len(JOHAB_SYMBOLS) // 2 - 1
    while lo <= hi:
        mid = (lo + hi) >> 1
        k = JOHAB_SYMBOLS[mid * 2]
        if k == ch:
            return JOHAB_SYMBOLS[mid * 2 + 1]
        if k < ch:
            lo = mid + 1
        else:
            hi = mid - 1
    return None


def decode_johab(ch: int) -> int:
    """HWP3 hchar (u16) → 유니코드 코드포인트. 매핑 실패 시 JOHAB_UNMAPPED."""
    if ch < 0x80:
        return ch

    if ch >= 0x8000:
        cho_idx = (ch >> 10) & 0x1F
        jung_idx = (ch >> 5) & 0x1F
        jong_idx = ch & 0x1F

        cho = CHO_MAP[cho_idx] if cho_idx < len(CHO_MAP) else -1
        jung = JUNG_MAP[jung_idx] if jung_idx < len(JUNG_MAP) else -1
        jong = JONG_MAP[jong_idx] if jong_idx < len(JONG_MAP) else -1

        if cho != -1 and jung != -1:
            if jong == -1:
                jong = 0
            return 0xAC00 + cho * 588 + jung * 28 + jong

        hit = _lookup_symbol(ch)
        if hit is not None:
            return hit

    return JOHAB_UNMAPPED


def decode_hchar_string(data: bytes) -> str:
    """HWP3 hchar 스트림 (u16 LE) → str. DocSummary 영역용."""
    out = []
    i = 0
    while i + 1 < len(data):
        ch = data[i] | (data[i + 1] << 8)
        if ch == 0:
            break
        cp = decode_johab(ch)
        if cp != JOHAB_UNMAPPED:
            out.append(chr(cp))
        i += 2
    return "".join(out)


def decode_hwp3_string(data: bytes) -> str:
    """HWP3 byte 스트림 (1바이트 ASCII < 0x80, 2바이트 johab >= 0x80) → str."""
    out = []
    i = 0
    while i < len(data):
        b1 = data[i]
        if b1 == 0:
            break
        if b1 < 0x80:
            out.append(chr(b1))
            i += 1
        elif i + 1 < len(data):
            ch = (b1 << 8) | data[i + 1]
            cp = decode_johab(ch)
            if cp != JOHAB_UNMAPPED:
                out.append(chr(cp))
            i += 2
        else:
            i += 1
    return "".join(out)
