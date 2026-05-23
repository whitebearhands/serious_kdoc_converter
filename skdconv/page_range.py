"""페이지/섹션 범위 파싱 유틸리티"""

from __future__ import annotations

from typing import List, Set, Union


def parse_page_range(spec: Union[List[int], str], max_pages: int) -> Set[int]:
    """페이지 범위 지정을 1-based set으로 변환.

    spec: [1,2,3] 또는 "1-3" 또는 "1,3,5-7"
    max_pages: 최대 페이지 수 (클램핑 상한)
    returns: 1-based 페이지 번호 set
    """
    result: Set[int] = set()
    if max_pages <= 0:
        return result

    if isinstance(spec, list):
        for n in spec:
            page = round(n)
            if 1 <= page <= max_pages:
                result.add(page)
        return result

    if not isinstance(spec, str) or not spec.strip():
        return result

    for part in spec.split(","):
        trimmed = part.strip()
        if not trimmed:
            continue
        if "-" in trimmed:
            parts = trimmed.split("-", 1)
            try:
                start = max(1, int(parts[0].strip()))
                end = min(max_pages, int(parts[1].strip()))
                for i in range(start, end + 1):
                    result.add(i)
            except ValueError:
                pass
        else:
            try:
                page = int(trimmed)
                if 1 <= page <= max_pages:
                    result.add(page)
            except ValueError:
                pass

    return result
