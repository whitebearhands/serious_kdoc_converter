"""DOCX OMML (Office Math ML) → LaTeX 변환 — 간략 구현."""

from __future__ import annotations

import lxml.etree as ET


def _tag(el: ET._Element) -> str:
    t = el.tag
    return t.split("}", 1)[1] if "}" in t else t


def _kids(parent: ET._Element, name: str) -> list[ET._Element]:
    return [child for child in parent if _tag(child) == name]


def _first_kid(parent: ET._Element, name: str) -> ET._Element | None:
    for child in parent:
        if _tag(child) == name:
            return child
    return None


def _run_to_latex(r: ET._Element) -> str:
    return "".join(t.text or "" for t in _kids(r, "t"))


_FUNC_NAMES = {
    "sin", "cos", "tan", "cot", "sec", "csc",
    "sinh", "cosh", "tanh", "coth",
    "arcsin", "arccos", "arctan",
    "log", "ln", "lg", "exp",
    "det", "dim", "gcd", "inf", "sup", "lim", "max", "min",
    "Pr", "arg",
}


def _el_to_latex(el: ET._Element) -> str:
    tag = _tag(el)

    if tag in ("oMath", "oMathPara"):
        return "".join(_el_to_latex(c) for c in el)

    if tag == "r":
        return _run_to_latex(el)

    if tag == "f":  # 분수
        num = _first_kid(el, "num")
        den = _first_kid(el, "den")
        n = "".join(_el_to_latex(c) for c in num) if num is not None else ""
        d = "".join(_el_to_latex(c) for c in den) if den is not None else ""
        return f"\\frac{{{n}}}{{{d}}}"

    if tag == "rad":  # 근호
        deg = _first_kid(el, "deg")
        e = _first_kid(el, "e")
        body = "".join(_el_to_latex(c) for c in e) if e is not None else ""
        if deg is not None:
            d = "".join(_el_to_latex(c) for c in deg)
            if d:
                return f"\\sqrt[{d}]{{{body}}}"
        return f"\\sqrt{{{body}}}"

    if tag == "sSup":  # 위첨자
        e = _first_kid(el, "e")
        sup = _first_kid(el, "sup")
        base = "".join(_el_to_latex(c) for c in e) if e is not None else ""
        exp = "".join(_el_to_latex(c) for c in sup) if sup is not None else ""
        return f"{{{base}}}^{{{exp}}}"

    if tag == "sSub":  # 아래첨자
        e = _first_kid(el, "e")
        sub = _first_kid(el, "sub")
        base = "".join(_el_to_latex(c) for c in e) if e is not None else ""
        s = "".join(_el_to_latex(c) for c in sub) if sub is not None else ""
        return f"{{{base}}}_{{{s}}}"

    if tag == "sSubSup":  # 위아래첨자
        e = _first_kid(el, "e")
        sub = _first_kid(el, "sub")
        sup = _first_kid(el, "sup")
        base = "".join(_el_to_latex(c) for c in e) if e is not None else ""
        s = "".join(_el_to_latex(c) for c in sub) if sub is not None else ""
        p = "".join(_el_to_latex(c) for c in sup) if sup is not None else ""
        return f"{{{base}}}_{{{s}}}^{{{p}}}"

    if tag == "nary":  # 연산자 (∑, ∏, ∫ 등)
        e = _first_kid(el, "e")
        body = "".join(_el_to_latex(c) for c in e) if e is not None else ""
        return f"\\sum {body}"

    if tag == "d":  # 괄호
        e_els = _kids(el, "e")
        content = " ".join("".join(_el_to_latex(c) for c in e) for e in e_els)
        return f"\\left({content}\\right)"

    if tag == "m":  # 행렬
        mr_els = _kids(el, "mr")
        rows = []
        for mr in mr_els:
            e_els = _kids(mr, "e")
            rows.append(" & ".join("".join(_el_to_latex(c) for c in e) for e in e_els))
        return "\\begin{matrix}" + " \\\\ ".join(rows) + "\\end{matrix}"

    # 기타 — 자식 재귀
    return "".join(_el_to_latex(c) for c in el)


def omml_element_to_latex(el: ET._Element) -> str:
    """<m:oMath> 또는 <m:oMathPara> 엘리먼트를 LaTeX 문자열로 변환."""
    try:
        return _el_to_latex(el).strip()
    except Exception:
        return ""


def is_display_math(el: ET._Element) -> bool:
    """oMathPara이면 display math ($$...$$)."""
    return _tag(el) == "oMathPara"
