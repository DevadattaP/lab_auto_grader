"""Detects and auto-corrects a specific beginner `scanf` mistake: reading a
`%c` right after a numeric/string read leaves the newline from the previous
Enter key-press sitting in stdin, so the `%c` immediately consumes that
leftover `\\n` instead of the character the student actually typed. The fix
students are taught is a leading whitespace character (usually a plain space)
in the format string before `%c`, which tells scanf to skip any pending
whitespace (including that leftover newline) first.

This module never touches marks. It only decides, for a question with
`adjust_char_input: true`, whether the student's `scanf` usage would trip over
that leftover-newline trap given our test inputs, and if so, produces a
patched copy of their source (extra leading whitespace before the offending
`%c`, stray commas removed from the same format string) so grading exercises
the student's actual logic instead of failing on this one well-known gotcha.
The mistake is still reported, just not scored as a wrong answer.

Rules for whether a given `%c` in a `scanf` format string is already fine:
  - it's the very first `scanf` call in the program, and `%c` is the first
    thing in its format string (nothing has been read yet, so there is no
    leftover newline to trip over)
  - it's already preceded by whitespace (a literal space/tab/newline, or an
    escape sequence `\\n`/`\\t`) in the same format string
  - it's the first thing in its format string, and the *previous* `scanf`
    call's format string itself ended in whitespace (that trailing whitespace
    already consumed the leftover newline before this call ever runs)

Anything else needs a space inserted right before the `%c`. Independently, any
comma anywhere in a format string that contains `%c` is always a mistake (a
comma is a literal character scanf must match verbatim against the input,
which the input files here are never written to satisfy) and is stripped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from grader.code_checks import strip_comments_and_literals

_SCANF_CALL_RE = re.compile(r"\bscanf\s*\(")
_WHITESPACE_CHARS = (" ", "\t", "\n", "\r")


@dataclass
class _ScanfCall:
    line: int
    fmt_start: int  # index into `source` of the first char *inside* the quotes
    fmt_end: int  # index into `source` one past the last char inside the quotes
    fmt_raw: str


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _find_scanf_calls(source: str, cleaned: str) -> list[_ScanfCall]:
    """Locate every real (non-commented, non-string-literal) `scanf(` call and,
    for each, the raw text of its first argument if that argument is a plain
    string literal. Calls whose first argument isn't a literal (e.g. a format
    string passed via a variable/macro) are skipped -- not analyzable with a
    textual scan, and rare in student code.
    """
    calls = []
    n = len(source)
    for m in _SCANF_CALL_RE.finditer(cleaned):
        i = m.end()  # just past the opening '('
        while i < n and source[i] in " \t\r\n":
            i += 1
        if i >= n or source[i] != '"':
            continue
        j = i + 1
        while j < n and source[j] != '"':
            if source[j] == "\\" and j + 1 < n:
                j += 2
                continue
            j += 1
        if j >= n:
            continue  # unterminated literal -- malformed source, skip
        calls.append(_ScanfCall(line=_line_of(source, m.start()), fmt_start=i + 1, fmt_end=j, fmt_raw=source[i + 1 : j]))
    return calls


def _ends_with_whitespace(fmt: str) -> bool:
    if fmt.endswith(_WHITESPACE_CHARS):
        return True
    return fmt.endswith("\\n") or fmt.endswith("\\t")


def _fix_format_string(fmt: str, *, is_first_scanf: bool, prev_ends_whitespace: bool) -> tuple[str, list[str]]:
    """Returns (fixed_fmt, mistakes) for one scanf format string that contains
    at least one `%c`. `mistakes` holds short human-readable descriptions, one
    per distinct problem category found (not one per occurrence).
    """
    mistakes: list[str] = []

    working = fmt
    if "," in working:
        mistakes.append("comma is not allowed anywhere in a scanf format string")
        working = working.replace(",", "")

    out: list[str] = []
    missing_whitespace = False
    pos = 0
    n = len(working)
    while pos < n:
        if working[pos : pos + 2] == "%c":
            prev_char = out[-1] if out else None
            prev_is_ws = prev_char in _WHITESPACE_CHARS if prev_char is not None else False
            if not prev_is_ws and len(out) >= 2 and out[-2] == "\\" and out[-1] in ("n", "t"):
                prev_is_ws = True

            allowed = prev_is_ws
            if not allowed and not out:  # %c is the first thing in the (comma-stripped) format string
                if is_first_scanf or prev_ends_whitespace:
                    allowed = True

            if not allowed:
                missing_whitespace = True
                out.append(" ")
            out.append("%")
            out.append("c")
            pos += 2
        else:
            out.append(working[pos])
            pos += 1

    if missing_whitespace:
        mistakes.append("missing whitespace (space or \\n) before %c in scanf")

    return "".join(out), mistakes


def analyze_and_fix_char_input(source: str) -> tuple[str, list[str]]:
    """Scans every `scanf` call in `source` in program order. Returns
    (fixed_source, issues): `fixed_source` is `source` unchanged if nothing
    needed fixing (same string, safe to compare with `is`/`==`), otherwise a
    copy with each offending format string patched in place (comma removed,
    a space inserted before an unguarded `%c`). `issues` is a human-readable
    line per scanf call that needed a fix, meant for the student report.
    """
    cleaned = strip_comments_and_literals(source)
    calls = _find_scanf_calls(source, cleaned)

    issues: list[str] = []
    edits: list[tuple[int, int, str]] = []
    prev_ends_whitespace = False

    for idx, call in enumerate(calls):
        fmt = call.fmt_raw
        if "%c" not in fmt:
            prev_ends_whitespace = _ends_with_whitespace(fmt)
            continue

        fixed_fmt, mistakes = _fix_format_string(
            fmt, is_first_scanf=(idx == 0), prev_ends_whitespace=prev_ends_whitespace
        )
        if mistakes:
            issues.append(
                f"line {call.line}: scanf(\"{fmt}\", ...) -- {'; '.join(mistakes)} "
                f"(adjusted to scanf(\"{fixed_fmt}\", ...) for grading)"
            )
            edits.append((call.fmt_start, call.fmt_end, fixed_fmt))
        prev_ends_whitespace = _ends_with_whitespace(fixed_fmt)

    if not edits:
        return source, []

    fixed_source = source
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        fixed_source = fixed_source[:start] + replacement + fixed_source[end:]
    return fixed_source, issues
