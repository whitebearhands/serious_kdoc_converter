"""HWPX/HWP equation script → LaTeX

hml-equation-parser (Python, Apache 2.0) 기반 포팅.
원본: https://github.com/OpenBapul/hml-equation-parser
"""

from __future__ import annotations

CONVERT_MAP: dict[str, str] = {
    "TIMES": "\\times", "times": "\\times",
    "LEFT": "\\left", "RIGHT": "\\right",
    "under": "\\underline",
    "SMALLSUM": "\\sum", "sum": "\\sum",
    "SMALLPROD": "\\prod", "prod": "\\prod",
    "SMALLINTER": "\\cap",
    "CUP": "\\cup",
    "OPLUS": "\\oplus", "OMINUS": "\\ominus", "OTIMES": "\\otimes",
    "ODIV": "\\oslash", "ODOT": "\\odot",
    "LOR": "\\lor", "LAND": "\\land",
    "SUBSET": "\\subset", "SUPERSET": "\\supset",
    "SUBSETEQ": "\\subseteq", "SUPSETEQ": "\\supseteq",
    "IN": "\\in", "OWNS": "\\owns", "NOTIN": "\\notin",
    "LEQ": "\\leq", "GEQ": "\\geq",
    "<<": "\\ll", ">>": "\\gg", "<<<": "\\lll", ">>>": "\\ggg",
    "PREC": "\\prec", "SUCC": "\\succ",
    "UPLUS": "\\uplus",
    "±": "\\pm", "-+": "\\mp", "÷": "\\div",
    "CIRC": "\\circ", "BULLET": "\\bullet", "DEG": " ^\\circ",
    "AST": "\\ast", "STAR": "\\bigstar", "BIGCIRC": "\\bigcirc",
    "EMPTYSET": "\\emptyset",
    "THEREFORE": "\\therefore", "BECAUSE": "\\because", "EXIST": "\\exists",
    "!=": "\\neq",
    "SMCOPROD": "\\coprod", "coprod": "\\coprod",
    "SQCAP": "\\sqcap", "SQCUP": "\\sqcup",
    "SQSUBSET": "\\sqsubset", "SQSUBSETEQ": "\\sqsubseteq",
    "BIGSQCUP": "\\bigsqcup",
    "BIGOPLUS": "\\bigoplus", "BIGOTIMES": "\\bigotimes",
    "BIGODOT": "\\bigodot", "BIGUPLUS": "\\biguplus",
    "inter": "\\bigcap", "union": "\\bigcup",
    "UNDEROVER": "",
    "SIM": "\\sim", "APPROX": "\\approx", "SIMEQ": "\\simeq", "CONG": "\\cong",
    "==": "\\equiv",
    "DIAMOND": "\\diamond", "FORALL": "\\forall",
    "prime": "'", "Partial": "\\partial", "INF": "\\infty", "PROPTO": "\\propto",
    "lim": "\\lim", "Lim": "\\lim",
    "larrow": "\\leftarrow", "->": "\\rightarrow",
    "uparrow": "\\uparrow", "downarrow": "\\downarrow",
    "LARROW": "\\Leftarrow", "RARROW": "\\Rightarrow",
    "UPARROW": "\\Uparrow", "DOWNARROW": "\\Downarrow",
    "udarrow": "\\updownarrow",
    "<->": "\\leftrightarrow",
    "UDARROW": "\\Updownarrow", "LRARROW": "\\Leftrightarrow",
    "NWARROW": "\\nwarrow", "SEARROW": "\\searrow",
    "NEARROW": "\\nearrow", "SWARROW": "\\swarrow",
    "HOOKLEFT": "\\hookleftarrow", "HOOKRIGHT": "\\hookrightarrow",
    "PVER": "\\|", "MAPSTO": "\\mapsto",
    "CDOTS": "\\cdots", "LDOTS": "\\ldots", "VDOTS": "\\vdots", "DDOTS": "\\ddots",
    "DAGGER": "\\dagger", "DDAGGER": "\\ddagger", "DOTEQ": "\\doteq",
    "ASYMP": "\\asymp",
    "TRIANGLE": "\\triangle", "NABLA": "\\nabla",
    "ANGLE": "\\angle",
    "VDASH": "\\vdash", "DASHV": "\\dashv",
    "BOT": "\\bot", "TOP": "\\top", "MODELS": "\\models",
    "LAPLACE": "\\mathcal{L}",
    "sqrt": "\\sqrt",
    "int": "\\int", "dint": "\\iint", "tint": "\\iiint", "oint": "\\oint",
    "alpha": "\\alpha", "beta": "\\beta", "gamma": "\\gamma", "delta": "\\delta",
    "epsilon": "\\epsilon", "zeta": "\\zeta", "eta": "\\eta", "theta": "\\theta",
    "iota": "\\iota", "kappa": "\\kappa", "lambda": "\\lambda", "mu": "\\mu",
    "nu": "\\nu", "xi": "\\xi", "pi": "\\pi",
    "rho": "\\rho", "sigma": "\\sigma", "tau": "\\tau", "upsilon": "\\upsilon",
    "phi": "\\phi", "chi": "\\chi", "psi": "\\psi", "omega": "\\omega",
    "GAMMA": "\\Gamma", "DELTA": "\\Delta", "THETA": "\\Theta",
    "LAMBDA": "\\Lambda", "XI": "\\Xi", "PI": "\\Pi",
    "SIGMA": "\\Sigma", "UPSILON": "\\Upsilon",
    "PHI": "\\Phi", "PSI": "\\Psi", "OMEGA": "\\Omega",
}

MIDDLE_CONVERT_MAP: dict[str, str] = {
    "matrix": "HULKMATRIX", "pmatrix": "HULKPMATRIX",
    "bmatrix": "HULKBMATRIX", "dmatrix": "HULKDMATRIX",
    "eqalign": "HULKEQALIGN", "cases": "HULKCASE",
    "vec": "HULKVEC", "dyad": "HULKDYAD",
    "acute": "HULKACUTE", "grave": "HULKGRAVE",
    "dot": "HULKDOT", "ddot": "HULKDDOT",
    "bar": "HULKBAR", "hat": "HULKHAT",
    "check": "HULKCHECK", "arch": "HULKARCH",
    "tilde": "HULKTILDE", "BOX": "HULKBOX",
    "OVERBRACE": "HULKOVERBRACE", "UNDERBRACE": "HULKUNDERBRACE",
}

BAR_CONVERT_MAP: dict[str, str] = {
    "HULKVEC": "\\overrightarrow", "HULKDYAD": "\\overleftrightarrow",
    "HULKACUTE": "\\acute", "HULKGRAVE": "\\grave",
    "HULKDOT": "\\dot", "HULKDDOT": "\\ddot",
    "HULKBAR": "\\overline", "HULKHAT": "\\widehat",
    "HULKCHECK": "\\check", "HULKARCH": "\\overset{\\frown}",
    "HULKTILDE": "\\widetilde", "HULKBOX": "\\boxed",
}

MATRIX_CONVERT_MAP: dict[str, dict] = {
    "HULKMATRIX":  {"begin": "\\begin{matrix}",  "end": "\\end{matrix}",  "remove_outer": True},
    "HULKPMATRIX": {"begin": "\\begin{pmatrix}", "end": "\\end{pmatrix}", "remove_outer": True},
    "HULKBMATRIX": {"begin": "\\begin{bmatrix}", "end": "\\end{bmatrix}", "remove_outer": True},
    "HULKDMATRIX": {"begin": "\\begin{vmatrix}", "end": "\\end{vmatrix}", "remove_outer": True},
    "HULKCASE":    {"begin": "\\begin{cases}",   "end": "\\end{cases}",   "remove_outer": True},
    "HULKEQALIGN": {"begin": "\\eqalign{",        "end": "}",              "remove_outer": False},
}

BRACE_CONVERT_MAP: dict[str, str] = {
    "HULKOVERBRACE": "\\overbrace",
    "HULKUNDERBRACE": "\\underbrace",
}


def _find_brackets(s: str, start: int, forward: bool) -> tuple[int, int]:
    if forward:
        open_pos = s.find("{", start)
        if open_pos == -1:
            raise ValueError("cannot find bracket")
        depth = 1
        for i in range(open_pos + 1, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
            if depth == 0:
                return open_pos, i + 1
        raise ValueError("cannot find bracket")
    else:
        rev = s[::-1].translate(str.maketrans("{}", "}{"))
        new_start = len(s) - start - 1
        rs, re_ = _find_brackets(rev, new_start, True)
        return len(s) - re_, len(s) - rs


def _find_outer_brackets(s: str, start: int) -> tuple[int, int]:
    idx = start - 1
    while idx >= 0:
        if s[idx] == "{":
            break
        idx -= 1
    if idx < 0:
        raise ValueError("cannot find bracket")
    return _find_brackets(s, idx, True)


def _replace_frac(s: str) -> str:
    while True:
        pos = s.find("over")
        if pos == -1:
            break
        try:
            num_s, num_e = _find_brackets(s, pos, False)
            before = s[:num_s]
            after = s[pos + 4:]
            s = before + "\\frac" + s[num_s:num_e] + after
        except ValueError:
            break
    return s


def _replace_root_of(s: str) -> str:
    while True:
        root_pos = s.find("root")
        if root_pos == -1:
            break
        try:
            of_pos = s.find("of")
            if of_pos == -1:
                break
            e1s, e1e = _find_brackets(s, root_pos, True)
            e2s, e2e = _find_brackets(s, of_pos, True)
            e1 = s[e1s + 1:e1e - 1]
            e2 = s[e2s + 1:e2e - 1]
            s = s[:root_pos] + "\\sqrt" + f"[{e1}]" + "{" + e2 + "}" + s[e2e + 1:]
        except ValueError:
            break
    return s


def _replace_all_matrix(s: str) -> str:
    def _replace_elements(bracket_str: str) -> str:
        inner = bracket_str[1:-1]
        inner = inner.replace("#", " \\\\ ")
        inner = inner.replace("&amp;", "&")
        return inner

    for mat_key, mat_elem in MATRIX_CONVERT_MAP.items():
        while True:
            pos = s.find(mat_key)
            if pos == -1:
                break
            try:
                es, ee = _find_brackets(s, pos, True)
                elem = _replace_elements(s[es:ee])
                if mat_elem["remove_outer"]:
                    bs, be = _find_outer_brackets(s, pos)
                    before, after = s[:bs], s[be:]
                else:
                    before, after = s[:pos], s[ee:]
                s = before + mat_elem["begin"] + elem + mat_elem["end"] + after
            except ValueError:
                break
    return s


def _replace_all_bar(s: str) -> str:
    for bar_key, bar_elem in BAR_CONVERT_MAP.items():
        while True:
            pos = s.find(bar_key)
            if pos == -1:
                break
            try:
                es, ee = _find_brackets(s, pos, True)
                bs, be = _find_outer_brackets(s, pos)
                elem = s[es:ee]
                s = s[:bs] + bar_elem + elem + s[be:]
            except ValueError:
                break
    return s


def _replace_all_brace(s: str) -> str:
    for brace_key, brace_elem in BRACE_CONVERT_MAP.items():
        while True:
            pos = s.find(brace_key)
            if pos == -1:
                break
            try:
                e1s, e1e = _find_brackets(s, pos, True)
                e2s, e2e = _find_brackets(s, e1e, True)
                elem1, elem2 = s[e1s:e1e], s[e2s:e2e]
                s = s[:pos] + brace_elem + elem1 + "^" + elem2 + s[e2e:]
            except ValueError:
                break
    return s


def hml_to_latex(hml_eq: str) -> str:
    """HWPX equation script → LaTeX 문자열 변환."""
    if not hml_eq:
        return ""
    s = hml_eq.replace("`", " ")
    s = s.replace("{", " { ").replace("}", " } ").replace("&", " & ")

    tokens = s.split(" ")
    result = []
    for t in tokens:
        if t in CONVERT_MAP:
            result.append(CONVERT_MAP[t])
        elif t in MIDDLE_CONVERT_MAP:
            result.append(MIDDLE_CONVERT_MAP[t])
        elif t:
            result.append(t)

    # \left { → \left \{
    for i in range(1, len(result)):
        if result[i] == "{" and result[i - 1] == "\\left":
            result[i] = "\\{"
        if result[i] == "}" and result[i - 1] == "\\right":
            result[i] = "\\}"

    out = " ".join(result)
    out = _replace_frac(out)
    out = _replace_root_of(out)
    out = _replace_all_matrix(out)
    out = _replace_all_bar(out)
    out = _replace_all_brace(out)
    return out
