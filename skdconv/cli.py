"""skdconv CLI — 문서를 마크다운으로 변환."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from .utils import VERSION


def _parse_fields(fields_str: str) -> dict[str, str]:
    """'key=value,key2=value2' 또는 JSON 문자열 → dict."""
    s = fields_str.strip()
    if s.startswith("{"):
        return json.loads(s)
    result: dict[str, str] = {}
    # 쉼표+한글/영문+= 패턴으로 분리
    import re
    pairs = re.split(r",(?=[가-힣A-Za-z])", s)
    for pair in pairs:
        idx = pair.find("=")
        if idx > 0:
            result[pair[:idx].strip()] = pair[idx + 1:].strip()
    return result


def _cmd_parse(args: argparse.Namespace) -> int:
    from . import parse
    from .detect import detect_format
    from .types import ParseOptions

    valid_formats = {"markdown", "json"}
    if args.format not in valid_formats:
        sys.stderr.write(f"[skdconv] 지원하지 않는 형식: {args.format} (markdown 또는 json)\n")
        return 1

    exit_code = 0
    files = args.files
    for fi, file_path in enumerate(files):
        abs_path = os.path.realpath(file_path)
        file_name = os.path.basename(abs_path)
        file_prefix = f"[{fi + 1}/{len(files)}] " if len(files) > 1 else ""

        try:
            file_size = os.path.getsize(abs_path)
            if file_size > 500 * 1024 * 1024:
                sys.stderr.write(
                    f"\n[skdconv] SKIP: {file_name} — 파일이 너무 큽니다 "
                    f"({file_size / 1024 / 1024:.1f}MB)\n"
                )
                exit_code = 1
                continue

            with open(abs_path, "rb") as f:
                data = f.read()

            fmt = detect_format(data)
            if not args.silent:
                sys.stderr.write(f"[skdconv] {file_prefix}{file_name} ({fmt}) ...")

            opts = ParseOptions(file_path=abs_path)
            if args.pages:
                opts.pages = args.pages
            if args.no_header_footer:
                opts.remove_header_footer = True

            result = parse(data, opts)

            if not result.success:
                sys.stderr.write(" FAIL\n")
                sys.stderr.write(f"  → {result.error}\n")
                exit_code = 1
                continue

            if not args.silent:
                sys.stderr.write(" OK\n")

            if args.format == "json":
                output = json.dumps(
                    {
                        "file_type": result.file_type,
                        "markdown": result.markdown,
                        "page_count": result.page_count,
                        "warnings": [
                            {"message": w.message, "code": w.code, "page": w.page}
                            for w in (result.warnings or [])
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            else:
                output = result.markdown

            if args.output and len(files) == 1:
                Path(args.output).write_text(output, encoding="utf-8")
                if not args.silent:
                    sys.stderr.write(f"  → {args.output}\n")
            elif args.out_dir:
                os.makedirs(args.out_dir, exist_ok=True)
                ext = ".json" if args.format == "json" else ".md"
                stem = os.path.splitext(file_name)[0]
                out_path = os.path.join(args.out_dir, stem + ext)
                Path(out_path).write_text(output, encoding="utf-8")
                if not args.silent:
                    sys.stderr.write(f"  → {out_path}\n")
            else:
                sys.stdout.write(output + "\n")

        except Exception as err:
            sys.stderr.write(f"\n[skdconv] ERROR: {file_name} — {err}\n")
            exit_code = 1

    return exit_code


def main(argv: Optional[list[str]] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="skdconv",
        description="모두 파싱해버리겠다 - HWP, HWPX, XLSX, XLS, DOCX -> Markdown",
    )
    parser.add_argument("--version", action="version", version=f"skdconv {VERSION}")
    parser.add_argument("files", nargs="*", metavar="FILE", help="변환할 파일 경로")
    parser.add_argument("-o", "--output", metavar="PATH", help="출력 파일 경로 (단일 파일 시)")
    parser.add_argument("-d", "--out-dir", metavar="DIR", help="출력 디렉토리 (다중 파일 시)")
    parser.add_argument("-p", "--pages", metavar="RANGE", help="페이지/섹션 범위 (예: 1-3, 1,3,5)")
    parser.add_argument(
        "--format", choices=["markdown", "json"], default="markdown", help="출력 형식"
    )
    parser.add_argument(
        "--no-header-footer", action="store_true", help="PDF 머리글/바닥글 자동 제거"
    )
    parser.add_argument("--silent", action="store_true", help="진행 메시지 숨기기")

    args = parser.parse_args(argv)

    if args.files:
        sys.exit(_cmd_parse(args))
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
