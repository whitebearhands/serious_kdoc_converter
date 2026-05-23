"""Lenient CFB (OLE2/Compound File Binary) 파서.

표준 파서가 거부하는 손상된 HWP 파일을 열기 위한 폴백.
직접 헤더/FAT/디렉토리를 파싱하여 스트림 데이터를 추출.
출처: rhwp (MIT) + MS-CFB spec
"""

from __future__ import annotations
import struct
import zlib
from dataclasses import dataclass
from typing import Optional

_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_END_OF_CHAIN = 0xFFFFFFFE
_FREE_SECT = 0xFFFFFFFF

_MAX_CHAIN_LENGTH = 1_000_000
_MAX_DIR_ENTRIES = 100_000
_MAX_STREAM_SIZE = 100 * 1024 * 1024


@dataclass
class _DirEntry:
    name: str
    type: int  # 0=unknown, 1=storage, 2=stream, 5=root
    start_sector: int
    size: int


class LenientCfbContainer:
    def __init__(self, entries: list[_DirEntry], _reader: "_CfbReader") -> None:
        self._entries = entries
        self._reader = _reader

    def find_stream(self, path: str) -> Optional[bytes]:
        normalized = path.lstrip("/")
        entry = self._find_entry_by_path(normalized)
        if entry is None or entry.type != 2:
            return None
        data = self._reader.read_stream_data(entry)
        return data if data else None

    def entries(self) -> list[_DirEntry]:
        return [e for e in self._entries if e.type == 2]

    def _find_entry_by_path(self, path: str) -> Optional[_DirEntry]:
        parts = path.split("/")
        if len(parts) == 1:
            return next((e for e in self._entries if e.name == parts[0] and e.type == 2), None)

        stream_name = "/".join(parts[1:])
        last_part = parts[-1]

        for e in self._entries:
            if e.type == 2 and e.name == stream_name:
                return e
        return next((e for e in self._entries if e.type == 2 and e.name == last_part), None)


class _CfbReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._sector_size = 512
        self._mini_sector_size = 64
        self._mini_stream_cutoff = 4096
        self._fat: list[int] = []
        self._mini_fat: Optional[list[int]] = None
        self._mini_stream: Optional[bytes] = None

    def _sector_offset(self, sid: int) -> int:
        return 512 + sid * self._sector_size

    def _read_sector(self, sid: int) -> bytes:
        off = self._sector_offset(sid)
        end = off + self._sector_size
        if end > len(self._data):
            return b"\x00" * self._sector_size
        return self._data[off:end]

    def _read_chain(self, start: int, max_bytes: int) -> bytes:
        if start in (_END_OF_CHAIN, _FREE_SECT):
            return b""
        if max_bytes > _MAX_STREAM_SIZE:
            raise ValueError("스트림이 너무 큽니다")

        chunks: list[bytes] = []
        current = start
        total = 0
        visited: set[int] = set()

        while current not in (_END_OF_CHAIN, _FREE_SECT) and total < max_bytes:
            if current in visited or len(visited) >= _MAX_CHAIN_LENGTH:
                break
            visited.add(current)
            buf = self._read_sector(current)
            remaining = max_bytes - total
            chunks.append(buf[:remaining] if remaining < self._sector_size else buf)
            total += min(len(buf), remaining)
            current = self._fat[current] if current < len(self._fat) else _END_OF_CHAIN

        return b"".join(chunks)

    def _get_mini_fat(self) -> list[int]:
        if self._mini_fat is not None:
            return self._mini_fat
        self._mini_fat = []
        return self._mini_fat

    def _get_mini_stream(self) -> bytes:
        if self._mini_stream is not None:
            return self._mini_stream
        self._mini_stream = b""
        return self._mini_stream

    def _read_mini_stream(self, start: int, size: int) -> bytes:
        mft = self._get_mini_fat()
        ms = self._get_mini_stream()
        if not mft or not ms:
            return b""

        chunks: list[bytes] = []
        current = start
        total = 0
        visited: set[int] = set()

        while current not in (_END_OF_CHAIN, _FREE_SECT) and total < size:
            if current in visited or len(visited) >= _MAX_CHAIN_LENGTH:
                break
            visited.add(current)
            off = current * self._mini_sector_size
            remaining = size - total
            chunk_size = min(self._mini_sector_size, remaining)
            if off + chunk_size <= len(ms):
                chunks.append(ms[off:off + chunk_size])
            total += chunk_size
            current = mft[current] if current < len(mft) else _END_OF_CHAIN

        return b"".join(chunks)

    def read_stream_data(self, entry: _DirEntry) -> bytes:
        if entry.size == 0:
            return b""
        if entry.size < self._mini_stream_cutoff:
            mini = self._read_mini_stream(entry.start_sector, entry.size)
            if mini:
                return mini
        return self._read_chain(entry.start_sector, entry.size)


def parse_lenient_cfb(data: bytes) -> LenientCfbContainer:
    if len(data) < 512:
        raise ValueError("CFB 파일이 너무 짧습니다 (최소 512바이트)")
    if data[:8] != _CFB_MAGIC:
        raise ValueError("CFB 매직 바이트 불일치")

    sector_size_shift = struct.unpack_from("<H", data, 30)[0]
    if not (7 <= sector_size_shift <= 16):
        raise ValueError(f"유효하지 않은 섹터 크기 시프트: {sector_size_shift}")
    sector_size = 1 << sector_size_shift

    mini_sector_size_shift = struct.unpack_from("<H", data, 32)[0]
    mini_sector_size = 1 << mini_sector_size_shift

    fat_sector_count = struct.unpack_from("<I", data, 44)[0]
    if fat_sector_count > 10000:
        raise ValueError(f"FAT 섹터 수가 너무 많습니다: {fat_sector_count}")

    first_dir_sector = struct.unpack_from("<I", data, 48)[0]
    mini_stream_cutoff = struct.unpack_from("<I", data, 56)[0]
    first_mini_fat_sector = struct.unpack_from("<I", data, 60)[0]
    mini_fat_sector_count = struct.unpack_from("<I", data, 64)[0]
    first_difat_sector = struct.unpack_from("<I", data, 68)[0]
    difat_sector_count = struct.unpack_from("<I", data, 72)[0]

    reader = _CfbReader(data)
    reader._sector_size = sector_size
    reader._mini_sector_size = mini_sector_size
    reader._mini_stream_cutoff = mini_stream_cutoff

    def sector_offset(sid: int) -> int:
        return 512 + sid * sector_size

    def read_sector(sid: int) -> bytes:
        off = sector_offset(sid)
        end = off + sector_size
        if end > len(data):
            return bytes(sector_size)
        return data[off:end]

    # DIFAT → FAT 섹터 목록
    fat_sectors: list[int] = []
    for i in range(109):
        if len(fat_sectors) >= fat_sector_count:
            break
        sid = struct.unpack_from("<I", data, 76 + i * 4)[0]
        if sid in (_FREE_SECT, _END_OF_CHAIN):
            break
        fat_sectors.append(sid)

    difat_sector = first_difat_sector
    visited_difat: set[int] = set()
    for _ in range(difat_sector_count):
        if len(fat_sectors) >= fat_sector_count:
            break
        if difat_sector in (_END_OF_CHAIN, _FREE_SECT) or difat_sector in visited_difat:
            break
        visited_difat.add(difat_sector)
        buf = read_sector(difat_sector)
        entries_per = (sector_size // 4) - 1
        for i in range(entries_per):
            if len(fat_sectors) >= fat_sector_count:
                break
            sid = struct.unpack_from("<I", buf, i * 4)[0]
            if sid not in (_FREE_SECT, _END_OF_CHAIN):
                fat_sectors.append(sid)
        difat_sector = struct.unpack_from("<I", buf, entries_per * 4)[0]

    # FAT 테이블 구축
    entries_per_fat = sector_size // 4
    fat: list[int] = []
    for fi in fat_sectors:
        buf = read_sector(fi)
        for i in range(entries_per_fat):
            if i * 4 + 3 < len(buf):
                fat.append(struct.unpack_from("<I", buf, i * 4)[0])
            else:
                fat.append(_FREE_SECT)

    reader._fat = fat

    # 체인 읽기 헬퍼
    def read_chain(start: int, max_bytes: int) -> bytes:
        if start in (_END_OF_CHAIN, _FREE_SECT):
            return b""
        if max_bytes > _MAX_STREAM_SIZE:
            raise ValueError("스트림이 너무 큽니다")
        chunks: list[bytes] = []
        current = start
        total = 0
        visited: set[int] = set()
        while current not in (_END_OF_CHAIN, _FREE_SECT) and total < max_bytes:
            if current in visited or len(visited) >= _MAX_CHAIN_LENGTH:
                break
            visited.add(current)
            buf = read_sector(current)
            remaining = max_bytes - total
            chunks.append(buf[:remaining] if remaining < sector_size else buf)
            total += min(len(buf), remaining)
            current = fat[current] if current < len(fat) else _END_OF_CHAIN
        return b"".join(chunks)

    # Mini-FAT
    mini_fat: list[int] = []
    if mini_fat_sector_count > 0 and first_mini_fat_sector not in (_END_OF_CHAIN, _FREE_SECT):
        mini_fat_data = read_chain(first_mini_fat_sector, mini_fat_sector_count * sector_size)
        for i in range(len(mini_fat_data) // 4):
            mini_fat.append(struct.unpack_from("<I", mini_fat_data, i * 4)[0])
    reader._mini_fat = mini_fat

    # 디렉토리 파싱
    dir_data = read_chain(first_dir_sector, _MAX_DIR_ENTRIES * 128)
    dir_entries: list[_DirEntry] = []
    offset = 0
    while offset + 128 <= len(dir_data) and len(dir_entries) < _MAX_DIR_ENTRIES:
        name_len = struct.unpack_from("<H", dir_data, offset + 64)[0]
        if name_len <= 0 or name_len > 64:
            dir_entries.append(_DirEntry(name="", type=0, start_sector=0, size=0))
            offset += 128
            continue
        name_bytes = name_len - 2
        name = dir_data[offset:offset + name_bytes].decode("utf-16-le", errors="replace") if name_bytes > 0 else ""
        entry_type = dir_data[offset + 66]
        start_sector = struct.unpack_from("<I", dir_data, offset + 116)[0]
        size = struct.unpack_from("<I", dir_data, offset + 120)[0]
        dir_entries.append(_DirEntry(name=name, type=entry_type, start_sector=start_sector, size=size))
        offset += 128

    # Root 엔트리에서 미니 스트림 추출
    mini_stream = b""
    if dir_entries and dir_entries[0].type == 5:
        root = dir_entries[0]
        mini_stream = read_chain(root.start_sector, root.size or _MAX_STREAM_SIZE)
    reader._mini_stream = mini_stream

    # read_stream_data 오버라이드: mini/normal 자동 분기
    def read_stream_data(entry: _DirEntry) -> bytes:
        if entry.size == 0:
            return b""
        if entry.size < mini_stream_cutoff and mini_fat and mini_stream:
            chunks: list[bytes] = []
            current = entry.start_sector
            total = 0
            visited: set[int] = set()
            while current not in (_END_OF_CHAIN, _FREE_SECT) and total < entry.size:
                if current in visited or len(visited) >= _MAX_CHAIN_LENGTH:
                    break
                visited.add(current)
                off = current * mini_sector_size
                remaining = entry.size - total
                chunk = min(mini_sector_size, remaining)
                if off + chunk <= len(mini_stream):
                    chunks.append(mini_stream[off:off + chunk])
                total += chunk
                current = mini_fat[current] if current < len(mini_fat) else _END_OF_CHAIN
            result = b"".join(chunks)
            if result:
                return result
        return read_chain(entry.start_sector, entry.size)

    reader.read_stream_data = read_stream_data  # type: ignore[method-assign]

    return LenientCfbContainer(dir_entries, reader)
