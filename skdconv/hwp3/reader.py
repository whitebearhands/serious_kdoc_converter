"""HWP3 binary stream reader — little-endian LE cursor."""

from __future__ import annotations
import struct


class InsufficientDataError(Exception):
    def __init__(self, requested: int, available: int) -> None:
        super().__init__(f"HWP3: insufficient data (need {requested}, have {available})")
        self.requested = requested
        self.available = available


class Reader:
    def __init__(self, buf: bytes, start: int = 0) -> None:
        self._buf = buf
        self._pos = start

    def position(self) -> int:
        return self._pos

    def remaining(self) -> int:
        return len(self._buf) - self._pos

    def eof(self) -> bool:
        return self._pos >= len(self._buf)

    def skip(self, n: int) -> None:
        self._ensure(n)
        self._pos += n

    def _ensure(self, n: int) -> None:
        if self._pos + n > len(self._buf):
            raise InsufficientDataError(n, len(self._buf) - self._pos)

    def read_u8(self) -> int:
        self._ensure(1)
        v = self._buf[self._pos]
        self._pos += 1
        return v

    def read_u16(self) -> int:
        self._ensure(2)
        (v,) = struct.unpack_from("<H", self._buf, self._pos)
        self._pos += 2
        return v

    def read_u32(self) -> int:
        self._ensure(4)
        (v,) = struct.unpack_from("<I", self._buf, self._pos)
        self._pos += 4
        return v

    def read_bytes(self, n: int) -> bytes:
        self._ensure(n)
        data = self._buf[self._pos:self._pos + n]
        self._pos += n
        return data

    def read_to_end(self) -> bytes:
        data = self._buf[self._pos:]
        self._pos = len(self._buf)
        return data
