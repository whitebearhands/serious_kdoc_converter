"""HWP 배포용(distribution) 문서 복호화.

ViewText/Section{N} 스트림: DISTRIBUTE_DOC_DATA(256B) → LCG+XOR → AES-128 ECB.
출처: rhwp (MIT) src/parser/crypto.rs
"""

from __future__ import annotations
import struct
import zlib

from .aes import aes128_ecb_decrypt


class _MsvcLcg:
    """MSVC CRT rand() 호환 LCG."""

    def __init__(self, seed: int) -> None:
        self._seed = seed & 0xFFFFFFFF

    def rand(self) -> int:
        # MSVC: seed = seed * 214013 + 2531011  (32-bit overflow)
        self._seed = (self._seed * 214013 + 2531011) & 0xFFFFFFFF
        return (self._seed >> 16) & 0x7FFF


def _decrypt_distribute_payload(payload: bytes) -> bytes:
    if len(payload) < 256:
        raise ValueError("배포용 payload가 256바이트 미만입니다")

    seed = struct.unpack_from("<I", payload, 0)[0]
    lcg = _MsvcLcg(seed)
    result = bytearray(payload[:256])

    i = 0
    n = 0
    key = 0
    while i < 256:
        if n == 0:
            key = lcg.rand() & 0xFF
            n = (lcg.rand() & 0x0F) + 1
        if i >= 4:
            result[i] ^= key
        i += 1
        n -= 1

    return bytes(result)


def _extract_aes_key(decrypted: bytes) -> bytes:
    offset = 4 + (decrypted[0] & 0x0F)
    if offset + 16 > len(decrypted):
        raise ValueError("AES 키 추출 실패: 오프셋이 payload 범위를 초과합니다")
    return decrypted[offset:offset + 16]


def _parse_record_header(data: bytes, offset: int) -> tuple[int, int, int]:
    """(tagId, size, headerSize) 반환."""
    if offset + 4 > len(data):
        raise ValueError("레코드 헤더 파싱 실패: 데이터 부족")
    hdr = struct.unpack_from("<I", data, offset)[0]
    tag_id = hdr & 0x3FF
    size = (hdr >> 20) & 0xFFF
    header_size = 4
    if size == 0xFFF:
        if offset + 8 > len(data):
            raise ValueError("확장 레코드 크기 파싱 실패: 데이터 부족")
        size = struct.unpack_from("<I", data, offset + 4)[0]
        header_size = 8
    return tag_id, size, header_size


_TAG_DISTRIBUTE_DOC_DATA = 0x10 + 12  # = 28


def decrypt_view_text(raw: bytes, compressed: bool) -> bytes:
    """ViewText 스트림 복호화 → 일반 BodyText 레코드 데이터."""
    tag_id, size, header_size = _parse_record_header(raw, 0)
    if tag_id != _TAG_DISTRIBUTE_DOC_DATA:
        raise ValueError(
            f"배포용 문서의 첫 레코드가 DISTRIBUTE_DOC_DATA({_TAG_DISTRIBUTE_DOC_DATA})가 아닙니다 (실제: {tag_id})"
        )

    payload_start = header_size
    payload_end = payload_start + size
    if payload_end > len(raw) or size < 256:
        raise ValueError("배포용 payload가 유효하지 않습니다")

    decrypted_payload = _decrypt_distribute_payload(raw[payload_start:payload_start + 256])
    aes_key = _extract_aes_key(decrypted_payload)

    encrypted = raw[payload_end:]
    if not encrypted:
        raise ValueError("배포용 문서에 암호화된 본문 데이터가 없습니다")

    aligned_len = len(encrypted) - (len(encrypted) % 16)
    if aligned_len == 0:
        raise ValueError("암호화된 데이터가 너무 짧습니다 (16바이트 미만)")

    decrypted = aes128_ecb_decrypt(encrypted[:aligned_len], aes_key)

    if compressed:
        try:
            return _decompress_stream(decrypted)
        except Exception:
            return decrypted

    return decrypted


def _decompress_stream(data: bytes) -> bytes:
    if len(data) >= 2 and data[0] == 0x78:
        try:
            return zlib.decompress(data)
        except Exception:
            pass
    return zlib.decompress(data, -15)
