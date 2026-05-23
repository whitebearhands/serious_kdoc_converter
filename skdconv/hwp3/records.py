"""HWP3 고정 크기 레코드 파싱 (signature + DocInfo + DocSummary)."""

from __future__ import annotations
from dataclasses import dataclass
from .reader import Reader
from .johab import decode_hchar_string

SIGNATURE_PREFIX = b"HWP Document File V3.00"
SIGNATURE_LEN = 30
DOC_INFO_SIZE = 128
DOC_SUMMARY_SIZE = 9 * 112


@dataclass
class Hwp3Header:
    compressed: int
    encrypted: int
    info_block_length: int
    title: str
    subject: str
    author: str
    date: str


def read_header(reader: Reader) -> Hwp3Header:
    sig = reader.read_bytes(SIGNATURE_LEN)
    if sig[:len(SIGNATURE_PREFIX)] != SIGNATURE_PREFIX:
        raise ValueError("HWP3: invalid file signature")

    doc_info_start = reader.position()
    reader.skip(96)
    encrypted = reader.read_u16()       # offset 96
    reader.skip(124 - 98)               # → 124
    compressed = reader.read_u8()       # offset 124
    reader.skip(1)                      # sub_revision (125)
    info_block_length = reader.read_u16()  # offset 126-127
    if reader.position() != doc_info_start + DOC_INFO_SIZE:
        raise ValueError("HWP3: DocInfo size mismatch")

    summary_start = reader.position()
    title = decode_hchar_string(reader.read_bytes(112))
    subject = decode_hchar_string(reader.read_bytes(112))
    author = decode_hchar_string(reader.read_bytes(112))
    date = decode_hchar_string(reader.read_bytes(112))
    reader.skip(5 * 112)
    if reader.position() != summary_start + DOC_SUMMARY_SIZE:
        raise ValueError("HWP3: DocSummary size mismatch")

    return Hwp3Header(
        compressed=compressed,
        encrypted=encrypted,
        info_block_length=info_block_length,
        title=title,
        subject=subject,
        author=author,
        date=date,
    )
