"""Detects and auto-corrects another common beginner `scanf` mistake, distinct
from (and independent of) the missing-space-before-`%c` one in
char_input_fix.py: passing a scalar variable's *value* instead of its
*address* -- `scanf("%d", n)` instead of `scanf("%d", &n)`. `scanf` always
writes through a pointer, so the missing `&` means the variable is never
actually written: on most platforms this is silent undefined behavior (the
variable keeps whatever garbage/zero value it started with) rather than a
crash, which makes it a particularly confusing bug for a student to diagnose
-- every downstream `if`/`switch` on that variable then behaves as if the
input was never read at all.

Like char_input_fix.py, this never touches marks by itself: for a question
with `adjust_scanf_address: true`, a patched copy of the offending argument
list (each bare scalar variable prefixed with `&`) is compiled and run
instead of the student's submission, and the mistake is reported.

This is deliberately conservative -- it only fixes an argument when it's
confident doing so is correct, and silently leaves anything it isn't sure
about untouched (never guesses, never inserts a wrong `&`):

  - the argument must be a bare identifier (`n`, not `&n`, `*p`, `arr[i]`, or
    any other expression) with nothing else going on;
  - that identifier must be found declared as a plain scalar (`int`, `char`,
    `float`, `double`, `long`, `short`, with or without `unsigned`/`signed`)
    -- never as a pointer (`int *p`) or an array (`char buf[64]`) anywhere in
    the file. A name declared as a scalar in one place and a pointer/array in
    another (e.g. reused across separate functions) is ambiguous and is
    therefore excluded entirely, never fixed;
  - its scanf conversion must be one that actually expects a pointer -- `%s`,
    `%S`, and `%[...]` already expect a bare array/pointer with no `&`, so
    those are left alone;
  - the format string's argument-consuming specifier count (skipping `%%` and
    suppressed `%*d`-style assignments) must exactly match the number of
    arguments passed -- a mismatched call is a different, unrelated problem
    and is left entirely alone rather than risk pairing the wrong specifier
    with the wrong argument.

Declaration scanning is a textual heuristic (same spirit as code_checks.py's
construct scanner), not a real C parser: it does not track scope, so it will
not catch every possible declaration style, but it never trades that
incompleteness for a wrong edit -- when in doubt, it does nothing.
"""

from __future__ import annotations

import re

from grader.code_checks import strip_comments_and_literals

_SCANF_CALL_RE = re.compile(r"\bscanf\s*\(")
_LENGTH_MODIFIERS = ("hh", "h", "ll", "l", "L", "j", "z", "t", "q")
_BARE_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
_DECL_STATEMENT_RE = re.compile(
    r"(?<![\w])"
    r"(?:(?:unsigned|signed|static|const|register|volatile)\s+)*"
    r"(?:int|char|float|double|long|short)"
    r"(?:\s+(?:int|long|short))*"
    r"\s+([^;{}()]+);"
)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _split_top_level_commas(text: str) -> list[str]:
    parts = []
    depth = 0
    start = 0
    for i, c in enumerate(text):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _classify_declarator(part: str) -> tuple[str, bool] | None:
    """Returns (name, is_pointer_or_array), or None if `part` isn't a simple
    declarator this heuristic can confidently classify."""
    part = part.strip()
    if not part:
        return None
    eq_idx = part.find("=")
    if eq_idx != -1:
        part = part[:eq_idx].strip()
    is_pointer = False
    while part.startswith("*"):
        is_pointer = True
        part = part[1:].strip()
    bracket_idx = part.find("[")
    if bracket_idx != -1:
        part = part[:bracket_idx].strip()
        is_pointer = True  # array -- same "don't add &" treatment as a pointer
    if not re.fullmatch(r"[A-Za-z_]\w*", part):
        return None
    return part, is_pointer


def _declared_scalar_vars(cleaned: str) -> set[str]:
    scalar_vars: set[str] = set()
    pointer_or_array_vars: set[str] = set()
    for m in _DECL_STATEMENT_RE.finditer(cleaned):
        for part in _split_top_level_commas(m.group(1)):
            classified = _classify_declarator(part)
            if classified is None:
                continue
            name, is_pointer = classified
            (pointer_or_array_vars if is_pointer else scalar_vars).add(name)
    # A name that shows up as both a scalar and a pointer/array (e.g. reused
    # across different functions) is ambiguous -- never fix it either way.
    return scalar_vars - pointer_or_array_vars


def _parse_format_specifiers(fmt: str) -> list[dict]:
    """Returns, in order, one entry per `%...` conversion actually present in
    `fmt` (skipping literal `%%`): {"conv": <char>, "suppressed": <bool>}.
    `conv` is '[' for a `%[...]` scanset, matching the whole bracket
    construct so nothing inside it is misread as further specifiers.
    """
    specs = []
    i = 0
    n = len(fmt)
    while i < n:
        if fmt[i] != "%":
            i += 1
            continue
        i += 1
        if i < n and fmt[i] == "%":
            i += 1
            continue
        suppressed = False
        if i < n and fmt[i] == "*":
            suppressed = True
            i += 1
        while i < n and fmt[i].isdigit():
            i += 1
        for lm in _LENGTH_MODIFIERS:
            if fmt[i : i + len(lm)] == lm:
                i += len(lm)
                break
        if i >= n:
            break
        conv = fmt[i]
        i += 1
        if conv == "[":
            if i < n and fmt[i] == "^":
                i += 1
            if i < n and fmt[i] == "]":
                i += 1
            while i < n and fmt[i] != "]":
                i += 1
            if i < n:
                i += 1
        specs.append({"conv": conv, "suppressed": suppressed})
    return specs


def _parse_call_args(source: str, open_paren_pos: int) -> tuple[list[tuple[int, int]], int]:
    """Splits a call's arguments on top-level commas, tracking string/char
    literals (so a comma inside one is never treated as a separator) and
    nested brackets of any kind (so a comma inside a nested call/subscript
    isn't either). Returns (list of (start, end) raw spans in `source`, one
    per argument, index just past the call's closing paren).
    """
    n = len(source)
    depth = 1
    i = open_paren_pos + 1
    start = i
    args: list[tuple[int, int]] = []
    while i < n and depth > 0:
        c = source[i]
        if c == '"' or c == "'":
            quote = c
            i += 1
            while i < n and source[i] != quote:
                if source[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            i += 1
            continue
        if c in "([{":
            depth += 1
            i += 1
            continue
        if c in ")]}":
            depth -= 1
            if depth == 0:
                args.append((start, i))
                i += 1
                break
            i += 1
            continue
        if c == "," and depth == 1:
            args.append((start, i))
            i += 1
            start = i
            continue
        i += 1
    return args, i


def analyze_and_fix_scanf_address(source: str) -> tuple[str, list[str]]:
    """Scans every `scanf` call in `source`. Returns (fixed_source, issues):
    `fixed_source` is `source` unchanged if nothing needed fixing, otherwise a
    copy with `&` inserted before each offending bare-scalar argument.
    `issues` is a human-readable line per scanf call that needed a fix.
    """
    cleaned = strip_comments_and_literals(source)
    scalar_vars = _declared_scalar_vars(cleaned)

    issues: list[str] = []
    edits: list[tuple[int, int, str]] = []

    for m in _SCANF_CALL_RE.finditer(cleaned):
        call_start = m.start()
        paren_pos = m.end() - 1
        arg_spans, _call_end = _parse_call_args(source, paren_pos)
        if len(arg_spans) < 2:
            continue  # no value arguments at all -- nothing to check

        fmt_start, fmt_end = arg_spans[0]
        fmt_raw = source[fmt_start:fmt_end].strip()
        if len(fmt_raw) < 2 or fmt_raw[0] != '"' or fmt_raw[-1] != '"':
            continue  # format string isn't a plain literal (variable/macro) -- not analyzable
        fmt_text = fmt_raw[1:-1]

        value_spans = arg_spans[1:]
        consuming_specs = [s for s in _parse_format_specifiers(fmt_text) if not s["suppressed"]]
        if len(consuming_specs) != len(value_spans):
            continue  # mismatched arg count is a different problem -- don't guess pairings

        call_fixes = []
        for spec, (astart, aend) in zip(consuming_specs, value_spans):
            if spec["conv"] in ("s", "S", "["):
                continue  # array/char* argument -- no & needed
            raw = source[astart:aend]
            stripped = raw.strip()
            if not _BARE_IDENTIFIER_RE.fullmatch(stripped):
                continue  # not a plain identifier (already &x, *p, arr[i], ...) -- leave alone
            if stripped not in scalar_vars:
                continue  # unknown, or known pointer/array -- leave alone
            ident_start = astart + (len(raw) - len(raw.lstrip()))
            call_fixes.append(stripped)
            edits.append((ident_start, ident_start, "&"))

        if call_fixes:
            issues.append(
                f"line {_line_of(source, call_start)}: scanf(...) -- missing '&' before "
                f"{', '.join(call_fixes)} (adjusted to &{', &'.join(call_fixes)} for grading)"
            )

    if not edits:
        return source, []

    fixed_source = source
    for start, _end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        fixed_source = fixed_source[:start] + replacement + fixed_source[start:]
    return fixed_source, issues
