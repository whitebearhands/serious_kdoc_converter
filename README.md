# Serious KDocument Converter(skdconv)

[![PyPI version](https://img.shields.io/pypi/v/skdconv)](https://pypi.org/project/skdconv/)
[![Python versions](https://img.shields.io/pypi/pyversions/skdconv)](https://pypi.org/project/skdconv/)
[![License](https://img.shields.io/pypi/l/skdconv)](LICENSE)

한국 공문서(HWP, HWPX, HWPML, XLS, XLSX, DOCX)를 마크다운으로 변환하는 파서 라이브러리입니다.

## 지원 포맷

| 포맷 | 설명 |
|------|------|
| HWP 3.x | 한글 구버전 |
| HWP 5.x | 한글 바이너리 |
| HWPX | 한글 XML 기반 |
| HWPML | 한글 XML 내보내기 |
| XLSX | Excel 2007+ |
| XLS | Excel 97-2003 |
| DOCX | Word 2007+ |

## 설치

```bash
pip install skdconv
```

의존성: `lxml`, `olefile`, `pycryptodome`

## 사용법

### 라이브러리

```python
import skdconv

with open("문서.hwpx", "rb") as f:
    data = f.read()

result = skdconv.parse(data)

if result.success:
    print(result.markdown)
    print(result.page_count)
else:
    print(result.error, result.code)
```

#### ParseOptions

```python
from skdconv import ParseOptions

opts = ParseOptions(
    pages="1-3",               # 페이지/섹션 범위
    remove_header_footer=True, # 머리글/바닥글 제거
)
result = skdconv.parse(data, opts)
```

#### ParseSuccess 필드

| 필드 | 타입 | 설명 |
|------|------|------|
| `file_type` | `str` | 감지된 포맷 |
| `markdown` | `str` | 변환된 마크다운 |
| `blocks` | `list[IRBlock]` | 문서 블록 트리 |
| `page_count` | `int \| None` | 페이지 수 |
| `metadata` | `DocumentMetadata \| None` | 문서 메타데이터 |
| `outline` | `list[OutlineItem] \| None` | 목차 |
| `warnings` | `list[ParseWarning] \| None` | 파싱 경고 |
| `images` | `list[ExtractedImage] \| None` | 추출된 이미지 |

### CLI

```bash
# 마크다운으로 출력
skdconv 문서.hwpx

# 파일로 저장
skdconv 문서.hwpx -o output.md

# 여러 파일을 디렉토리에 저장
skdconv *.hwpx -d output/

# JSON 형식 출력
skdconv 문서.xlsx --format json

# 페이지 범위 지정
skdconv 문서.hwp -p 1-5

# 머리글/바닥글 제거
skdconv 문서.hwpx --no-header-footer
```

## 라이선스

MIT
