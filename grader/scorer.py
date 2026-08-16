"""Matchers (compare actual vs expected output) and per-question scoring.

Open-test failures carry full detail (input/expected/actual) since they're
safe to show students. Hidden-test failures carry only pass/fail + category
-- never leak hidden input/expected output into a student-facing report.
"""

from __future__ import annotations

import importlib.util
import itertools
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from grader.code_checks import CodeCheckResult
from grader.config_schema import Question, TestCase
from grader.runner import QuestionResult

MatcherFn = Callable[[str, str, str], bool]
# (test, actual) -> (passed, reasons) -- reasons is non-empty only when the
# pass happened via a tolerance (a composable matcher option or ignore_typo),
# not an exact match. See get_test_matcher.
TestMatcherFn = Callable[[TestCase, str], tuple[bool, list[str]]]

_PUNCTUATION_TABLE = str.maketrans("", "", ".,:;!?'\"")

# Tuned for the failure mode this exists to catch (one mistyped letter in an
# otherwise-correct word, e.g. "aperator" for "operator") without also
# excusing a wrong answer:
_TYPO_MAX_DISTANCE = 1
# a 1-edit "typo" on a word this short is really just a different word (e.g.
# "no" vs "on") -- require exact match below this length.
_TYPO_MIN_WORD_LEN = 4


def _normalize_lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]


def _strip_trailing_blank(lines: list[str]) -> list[str]:
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _normalize_partial_line(line: str, line_norm: dict) -> str:
    """Per-line version of `_normalize_for_matching`, for partial-credit
    scoring -- applies the *question's own* ignore_case/ignore_whitespace/
    ignore_punctuation/symbol_groups (whichever are set in `line_norm`,
    typically `question.matcher_options`) to one line at a time. Without
    this, partial credit only ever did a bare rstrip-and-compare regardless
    of what tolerances the question's actual matcher was configured with, so
    two lines a student's *real* submission would pass under (e.g.
    `Secret Code:ak97` vs `Secret Code : ak97` with `ignore_whitespace: true`
    enabled) silently failed to earn partial credit for looking different
    line-for-line, even though full-credit matching already treats them as
    identical.
    """
    return _normalize_for_matching(
        line,
        ignore_case=bool(line_norm.get("ignore_case", False)),
        ignore_whitespace=bool(line_norm.get("ignore_whitespace", False)),
        ignore_punctuation=bool(line_norm.get("ignore_punctuation", False)),
        symbol_groups=line_norm.get("symbol_groups"),
    )


def _is_numeric_shaped(s: str) -> bool:
    """Same digit-check `_extra_output_tolerates` uses: strip at most one
    '.' and one '-', then require what's left to be all digits."""
    t = s.replace(".", "", 1).replace("-", "", 1)
    return bool(t) and t.isdigit()


def _score_partial_lines(
    expected: str, actual: str, weight: float, line_norm: dict | None = None
) -> tuple[float, int, int]:
    """Line-by-line partial credit for a `partial: true` test case: award
    weight/N for each matching line of expected output instead of
    all-or-nothing (e.g. a two-line test where line 1 is an even/odd check
    and line 2 is an unrelated case-toggle -- getting only one right should
    earn half marks, not zero). Uses the same line normalization as
    `exact_trim` (rstrip each line, ignore trailing blank lines), plus
    whichever of the question's own ignore_case/ignore_whitespace/
    ignore_punctuation/symbol_groups tolerances `line_norm` carries (see
    `_normalize_partial_line`), so partial credit doesn't penalize the same
    formatting differences the question's actual matcher already forgives.

    A blank line in the *expected* output (after normalization) is dropped
    entirely, from both the count of lines needed and the pool a student's
    lines are checked against -- a blank line is incidental spacing (e.g.
    separating two sections of a multi-part answer), not a piece of content
    this question is actually testing, so neither having nor lacking one in
    the actual output should move the score.

    All of a student's actual lines are joined into one contiguous blob (no
    separator) before searching -- this deliberately erases the distinction
    between "two pieces separated by a real `\\n`" and "two pieces glued
    together by a `printf` that forgot its trailing `\\n`", since a student
    fixing the second into the first wouldn't have changed the *content*,
    only incidental formatting this scoring already tries to be lenient
    about elsewhere.

    Matching works outward from the *longest* run of consecutive still-
    unmatched expected lines, concatenated together, searched for as one
    unit in whatever of that blob hasn't been consumed yet -- shrinking one
    line at a time until something matches (or nothing does, and the first
    of those lines is left unmatched for this pass). A run of 2+ lines is
    allowed to match anywhere in the blob, since a multi-line run is long
    and specific enough that a wrong answer coincidentally reproducing all
    of it is essentially impossible. So is an *isolated* single line, most
    of the time -- e.g. `"NO SECRET PAIR"` glued onto unrelated text with a
    missing `\n` -- so a run of length 1 gets the same anywhere-in-the-blob
    search too, by default.

    Both lengths get one more check right at the match's own edges: if the
    run's first (or last) line is itself immediately preceded (or followed)
    by a digit in the blob, *and* that edge of the run is itself a digit,
    the match is rejected. That specific shape means what was actually
    matched is only a fragment of a longer, different digit run in the
    actual output -- e.g. `"GCD = 5"` is a genuine prefix of a wrong
    `"GCD = 51"`, and `"LCM = 15"` is a genuine prefix of a wrong
    `"LCM = 150"` -- so without this check, a wrong numeric answer that
    happens to start (or continue) with the right digits would silently
    earn credit. This reuses the exact digit-vs-non-digit distinction
    `_extra_output_tolerates` already relies on for the same reason
    (`_is_numeric_shaped`), just applied at the specific two characters
    that meet at a match's boundary rather than to a whole leftover prefix.

    The digit guard doesn't help for a *non-numeric* line that happens to be
    a complete substring of a different, wrong answer with otherwise clean
    boundaries -- `"Coprime"` inside a wrong `"Not Coprime"` has a space
    before it and end-of-string after, passing every check above, the same
    way it always would. No purely textual rule can close that: telling
    "two independently-correct printf calls that got glued together by a
    missing \\n" apart from "one wrong value/phrase that happens to
    textually contain a different, correct one" needs to know which chunks
    came from separate printf calls, information that's gone once it's all
    just one stdout string. That's what `line_norm["strict_lines"]` (from
    `matcher.strict_lines` in question.yaml) is for: any expected line whose
    normalized text exactly matches an entry there is held to the original,
    stricter single-line rule instead -- must equal an *entire* actual line,
    not just be found somewhere inside one -- confining a question's own
    known containment-risk pairs (e.g. `strict_lines: ["Coprime", "Not
    Coprime"]`) back to the safe behavior, while every other single line on
    that same question still benefits from the more lenient search. A
    question that needs to close this gap for output shapes `strict_lines`
    can't express cleanly can still do so itself with a custom checker (see
    e.g. q1_secret_pair_detector's checker.py, which anchors on required
    keywords instead of raw text position). Returns
    (marks_earned, lines_matched, lines_total).
    """
    line_norm = line_norm or {}
    exp_lines = [_normalize_partial_line(l, line_norm) for l in _strip_trailing_blank(_normalize_lines(expected))]
    exp_lines = [line for line in exp_lines if line != ""]
    total = len(exp_lines)
    if total == 0:
        return 0.0, 0, 0

    strict_lines = {_normalize_partial_line(l, line_norm) for l in (line_norm.get("strict_lines") or [])}

    act_lines = [_normalize_partial_line(l, line_norm) for l in _strip_trailing_blank(_normalize_lines(actual))]
    act_lines = [line for line in act_lines if line != ""]
    blob = ""
    boundaries = []  # (start, end) span of each original actual line within blob
    for line in act_lines:
        start = len(blob)
        blob += line
        boundaries.append((start, len(blob)))

    matched = 0
    i = 0
    while i < total:
        remaining = total - i
        advanced = False
        for length in range(remaining, 0, -1):
            combo = "".join(exp_lines[i : i + length])
            if not combo:
                continue
            idx = blob.find(combo)
            if idx == -1:
                continue
            end = idx + len(combo)
            if length == 1 and combo in strict_lines:
                # Flagged as containment-risky (matcher.strict_lines) -- only
                # a byte-for-byte match against one whole actual line is
                # acceptable here, same as every single line used to require
                # before the digit-guarded search below existed.
                if (idx, end) not in boundaries:
                    continue
            else:
                if idx > 0 and blob[idx - 1].isdigit() and combo[0].isdigit():
                    continue
                if end < len(blob) and blob[end].isdigit() and combo[-1].isdigit():
                    continue
            matched += length
            # Consume the matched span so it can't satisfy another expected
            # line too, shifting every later line span's recorded position
            # to account for the removed text.
            blob = blob[:idx] + blob[end:]
            shift = end - idx
            boundaries = [
                (s, e) if e <= idx else (s - shift, e - shift)
                for s, e in boundaries
                if e <= idx or s >= end
            ]
            i += length
            advanced = True
            break
        if not advanced:
            i += 1
    return weight * matched / total, matched, total


def _score_partial_lines_gated(
    expected: str,
    actual: str,
    weight: float,
    line_gate: dict[int, str],
    code_check: CodeCheckResult | None,
    line_norm: dict | None = None,
) -> tuple[float, int, int]:
    """Like `_score_partial_lines`, but a gated line (per `code_checks.line_gate`)
    only counts as matched when its text is correct *and* the construct
    required for that line was actually found in the submission's source --
    e.g. a student who gets line 0 (odd/even) byte-correct via `%` instead of
    `&` doesn't earn that line's share, even though the text alone would have
    matched. Ungated lines fall back to plain text matching, same as
    `_score_partial_lines`, including the question's own ignore_case/
    ignore_whitespace/ignore_punctuation/symbol_groups tolerances via
    `line_norm`. Deliberately does *not* drop blank expected lines the way
    `_score_partial_lines` does -- `line_gate`'s keys are 0-based indices
    into the expected line sequence exactly as the question author wrote it,
    so removing a line here would silently shift every later index's
    construct requirement onto the wrong line. Returns
    (marks_earned, lines_matched, lines_total).
    """
    line_norm = line_norm or {}
    exp_lines = [_normalize_partial_line(l, line_norm) for l in _strip_trailing_blank(_normalize_lines(expected))]
    act_lines = [_normalize_partial_line(l, line_norm) for l in _strip_trailing_blank(_normalize_lines(actual))]
    total = len(exp_lines)
    if total == 0:
        return 0.0, 0, 0
    found_constructs = {name for name, _ in code_check.found_required} if code_check else set()
    matched = 0
    for i, line in enumerate(exp_lines):
        text_ok = i < len(act_lines) and act_lines[i] == line
        construct = line_gate.get(i)
        construct_ok = construct is None or construct in found_constructs
        if text_ok and construct_ok:
            matched += 1
    return weight * matched / total, matched, total


def match_exact_trim(expected: str, actual: str, test_input: str) -> bool:
    return _strip_trailing_blank(_normalize_lines(expected)) == _strip_trailing_blank(
        _normalize_lines(actual)
    )


def match_exact(expected: str, actual: str, test_input: str) -> bool:
    return expected == actual


def match_token(expected: str, actual: str, test_input: str) -> bool:
    return expected.split() == actual.split()


def make_float_tol_matcher(eps: float):
    def _match(expected: str, actual: str, test_input: str) -> bool:
        exp_tokens = expected.split()
        act_tokens = actual.split()
        if len(exp_tokens) != len(act_tokens):
            return False
        for e, a in zip(exp_tokens, act_tokens):
            try:
                if abs(float(e) - float(a)) > eps:
                    return False
            except ValueError:
                if e != a:
                    return False
        return True

    return _match


def _edit_distance(a: str, b: str) -> int:
    """Damerau-Levenshtein (restricted/OSD variant): insertion, deletion,
    substitution, or swapping one adjacent pair of characters each cost 1.
    The transposition case matters here specifically because a fat-finger
    swap of two adjacent letters (e.g. "PAYMETN" for "PAYMENT") is a very
    common typo, but 2 edits under plain Levenshtein -- without it, the most
    common typo shape would fall outside _TYPO_MAX_DISTANCE = 1 and never
    get caught by ignore_typo at all.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    len_a, len_b = len(a), len(b)
    d = [[0] * (len_b + 1) for _ in range(len_a + 1)]
    for i in range(len_a + 1):
        d[i][0] = i
    for j in range(len_b + 1):
        d[0][j] = j
    for i in range(1, len_a + 1):
        for j in range(1, len_b + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(
                d[i - 1][j] + 1,  # deletion
                d[i][j - 1] + 1,  # insertion
                d[i - 1][j - 1] + cost,  # substitution
            )
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)  # transposition
    return d[len_a][len_b]


def _word_matches_with_typo(expected_word: str, actual_word: str) -> bool:
    if expected_word == actual_word:
        return True
    # Never excuse a wrong number as a "typo" -- a wrong computed answer
    # (e.g. actual "43" for expected "42") is also 1 edit away.
    if any(ch.isdigit() for ch in expected_word) or any(ch.isdigit() for ch in actual_word):
        return False
    if len(expected_word) < _TYPO_MIN_WORD_LEN:
        return False
    return _edit_distance(expected_word, actual_word) <= _TYPO_MAX_DISTANCE


def match_with_typo_tolerance(expected: str, actual: str) -> bool:
    """Per-test fallback (`tests.<group>.<name>.ignore_typo: true`), tried only
    when the question's normal matcher already failed on this test. Compares
    expected vs actual line-by-line and word-by-word (same line/word counts
    required -- this forgives a misspelling, not a missing/extra word), each
    word pair matching exactly or within one character edit, and never for a
    word containing a digit. Independent of the question's `matcher.type`/
    options, same as how `partial` line-scoring uses its own normalization
    regardless of the configured matcher.
    """
    exp_lines = _strip_trailing_blank(_normalize_lines(expected))
    act_lines = _strip_trailing_blank(_normalize_lines(actual))
    if len(exp_lines) != len(act_lines):
        return False
    for exp_line, act_line in zip(exp_lines, act_lines):
        exp_words = exp_line.split()
        act_words = act_line.split()
        if len(exp_words) != len(act_words):
            return False
        if not all(_word_matches_with_typo(e, a) for e, a in zip(exp_words, act_words)):
            return False
    return True


def _load_custom_module(checker_path: Path):
    spec = importlib.util.spec_from_file_location(f"checker_{checker_path.parent.name}", checker_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_custom_matcher(checker_path: Path):
    module = _load_custom_module(checker_path)
    if not hasattr(module, "check"):
        raise ValueError(f"{checker_path}: custom checker must define check(expected, actual, test_input)")
    return module.check


def load_custom_extractor(checker_path: Path) -> Callable[[str], str] | None:
    """Optional companion to a custom checker's `check`: if the module also
    defines `extract(actual: str) -> str`, the grader uses it to reduce a
    student's raw stdout down to the same "shape" as `expected_text` (e.g.
    stripping interleaved menu/prompt text a checker filters out) before
    `partial`/`ignore_typo` do their own *raw* line-by-line comparisons --
    both live in code that predates `matcher.type: custom` and know nothing
    about a checker's filtering, so without this hook they'd compare
    `expected_text` against unfiltered stdout and almost never line up once
    a checker is discarding lines the expected output doesn't have. Returns
    None if the checker doesn't define one, in which case both tolerances
    fall back to the raw actual text, same as before this hook existed.
    """
    module = _load_custom_module(checker_path)
    return getattr(module, "extract", None)


def _base_matcher(question: Question) -> MatcherFn:
    if question.matcher_type == "exact_trim":
        return match_exact_trim
    if question.matcher_type == "exact":
        return match_exact
    if question.matcher_type == "token":
        return match_token
    if question.matcher_type == "float_tol":
        eps = float(question.matcher_options.get("eps", 1e-6))
        return make_float_tol_matcher(eps)
    if question.matcher_type == "custom":
        return load_custom_matcher(question.checker_path)
    raise ValueError(f"unknown matcher type: {question.matcher_type}")


def _apply_symbol_groups(text: str, symbol_groups: list[list[str]]) -> str:
    """Collapses every symbol in a group down to that group's first symbol,
    independently per group, e.g. `[["x", "X", "*"], [":", "="]]` treats
    x/X/* as interchangeable *and*, separately, `:`/`=` as interchangeable --
    so a question can accept any of them wherever the reference solution
    picked one canonical choice (e.g. `q05_vending_change`'s `"50 x 1"`
    also matching `"50 * 1"`/`"50 X 1"`). Independent of whether ignore_case
    is also on for this question. Symbols are matched literally (regex special
    characters are escaped), longest symbol first within a group so a
    multi-character symbol isn't partially shadowed by a shorter one that
    happens to be a substring of it.
    """
    for group in symbol_groups:
        if len(group) < 2:
            continue
        canonical, *rest = group
        rest = sorted(rest, key=len, reverse=True)
        pattern = "|".join(re.escape(sym) for sym in rest)
        text = re.sub(pattern, canonical, text)
    return text


def _normalize_for_matching(
    text: str,
    *,
    ignore_case: bool,
    ignore_whitespace: bool,
    ignore_punctuation: bool,
    symbol_groups: list[list[str]] | None = None,
) -> str:
    if symbol_groups:
        text = _apply_symbol_groups(text, symbol_groups)
    if ignore_case:
        text = text.lower()
    if ignore_punctuation:
        text = text.translate(_PUNCTUATION_TABLE)
    if ignore_whitespace:
        text = re.sub(r"\s+", "", text)
    return text


# Composable-option name -> what to say in the report when that option is
# the reason a test passed instead of failing (see _reasons_for_pass below).
_OPTION_REASON = {
    "ignore_case": "case not matched",
    "ignore_whitespace": "spacing not matched",
    "ignore_punctuation": "got less/extra punctuation",
    "allow_extra_output": "got something extra in output",
    "symbol_groups": "used a different (but interchangeable) symbol",
}


def _extra_output_tolerates(norm_expected: str, norm_actual: str) -> bool:
    """`allow_extra_output`'s actual comparison. Exists for exactly one
    failure mode: `printf("Enter marks: ");` with no trailing `\\n` right
    before `scanf`, which glues that leftover prompt onto the *front* of the
    real output on the same line -- e.g. actual "Enter marks: PASS" for
    expected "PASS". That means the tolerated extra text can only be a
    prefix (the prompt is printed before the real answer, never after), so
    matching requires `actual` to *end with* `expected`, not just contain it
    anywhere -- plain substring containment used to also let through case a
    logic bug that runs two `if` branches instead of one and prints both
    answers back to back ("PASSINVALID"), since "PASS" is still `in`
    "PASSINVALID" even though it isn't how the real string ends.

    If the stripped string is a digit, the leftover prefix itself is required to contain no digits. 
    For example, if expected output is "0", and actual output is "0.00", the last 0 is considered required output and 0.0 is prefix.
    Since expected is digit, and prefix and required output both are digits, its a False.
    If actual output is like "Enter marks: 100", expected output is "100", then prefix is "Enter marks: ", which has no digits, so its True.
    If actual output is "Enter marks: 100", expected output is "0", then prefix is "Enter marks: 1", which has digits, so its False.
    If actual output is "Enter marks-5INVALID", expected output is "INVALID", then prefix is "Enter marks-5", which has digits, but expected is not a digit, so its True.
    """
    stripped = norm_expected.strip()
    if not stripped or not norm_actual.endswith(stripped):
        return False
    extra_prefix = (norm_actual[: len(norm_actual) - len(stripped)]).replace(".", "", 1).replace("-", "", 1)
    return not all(ch.isdigit() for ch in extra_prefix) if extra_prefix and stripped.replace(".", "", 1).replace("-", "", 1).isdigit() else True


def _wrap_matcher(base: MatcherFn, opts: dict) -> MatcherFn:
    """Builds the tolerant matcher for exactly the given option set, on top
    of a fixed `base` matcher. Factored out of get_matcher so the same
    wrapping logic can be re-run with a *reduced* option set (leaving one
    option out at a time) to work out which option was actually load-bearing
    for a given pass -- see _reasons_for_pass.
    """
    ignore_case = bool(opts.get("ignore_case", False))
    ignore_whitespace = bool(opts.get("ignore_whitespace", False))
    ignore_punctuation = bool(opts.get("ignore_punctuation", False))
    allow_extra_output = bool(opts.get("allow_extra_output", False))
    symbol_groups = opts.get("symbol_groups") or None

    if not (
        ignore_case
        or ignore_whitespace
        or ignore_punctuation
        or allow_extra_output
        or symbol_groups
    ):
        return base

    def wrapped(expected: str, actual: str, test_input: str) -> bool:
        norm_expected = _normalize_for_matching(
            expected,
            ignore_case=ignore_case,
            ignore_whitespace=ignore_whitespace,
            ignore_punctuation=ignore_punctuation,
            symbol_groups=symbol_groups,
        )
        norm_actual = _normalize_for_matching(
            actual,
            ignore_case=ignore_case,
            ignore_whitespace=ignore_whitespace,
            ignore_punctuation=ignore_punctuation,
            symbol_groups=symbol_groups,
        )
        if allow_extra_output:
            return _extra_output_tolerates(norm_expected, norm_actual)
        return base(norm_expected, norm_actual, test_input)

    return wrapped


def get_matcher(question: Question) -> MatcherFn:
    """Wraps whichever base matcher `matcher.type` selects with a handful of
    independent, composable tolerances -- opt-in per question, since a real
    student-submission run tends to surface a handful of harmless-but-common
    mistakes (wrong case, sloppy spacing, a stray colon, an unflushed prompt
    ahead of scanf mixed into stdout) that shouldn't all cost marks on every
    question. None set (the default, and every question written before these
    existed) -> identical behavior to before, same function object even.
    """
    base = _base_matcher(question)
    if question.matcher_type == "custom":
        return base  # a custom checker gets raw text and full control -- these toggles don't apply to it
    return _wrap_matcher(base, question.matcher_options)


def _minimal_sufficient_subset(names: list[str], is_sufficient: Callable[[tuple[str, ...]], bool]) -> tuple[str, ...]:
    """Finds the smallest combination of `names` (preserving their given
    order, smallest size first, and among same-size combinations the one
    whose members appear earliest in `names`) for which `is_sufficient`
    returns True. Assumes `is_sufficient(tuple(names))` (everything on) is
    True -- callers only reach here after confirming the full option set
    passed -- so this always terminates with a real answer, never the
    all-names fallback.
    """
    for size in range(len(names) + 1):
        for combo in itertools.combinations(names, size):
            if is_sufficient(combo):
                return combo
    return tuple(names)  # unreachable given the caller's precondition


def _reasons_for_pass(question: Question, base: MatcherFn, expected: str, actual: str, test_input: str) -> list[str]:
    """Called only once the question's full matcher (base + all its enabled
    options) has already passed a test. Figures out which enabled option(s)
    explain that pass -- an exact match needs no explanation, but a pass that
    only happened because e.g. `ignore_case` was on should say so, the same
    way a FAIL names its failure category (TIMEOUT, RUNTIME_ERROR, ...).

    Finds the smallest combination of enabled options that, on its own
    (every other option off), is *sufficient* to reproduce the pass --
    rather than testing each option's *necessity* by removing it alone from
    the full set. Necessity-by-removal breaks down when two options overlap
    in what they normalize away (e.g. a question with both `ignore_case` and
    `symbol_groups` on, and a student who printed "X" for "x": either option
    alone already fixes it, so removing just one leaves the other covering
    for it, and neither ever looks "necessary" -- leave-one-out silently
    reports no reason at all for a pass that plainly wasn't an exact match).
    Minimal sufficiency doesn't have that blind spot: it directly asks "would
    this subset alone have passed?", so at least one option is always
    credited whenever the full set diverges from an exact match.
    """
    if question.matcher_type == "custom":
        return []
    opts = question.matcher_options
    enabled = [name for name in _OPTION_REASON if opts.get(name)]
    if not enabled:
        return []
    if base(expected, actual, test_input):
        return []  # matched even with every option off -- an exact match, nothing to explain

    def sufficient(combo: tuple[str, ...]) -> bool:
        # opts.get(name), not a hardcoded True -- symbol_groups needs its
        # actual list of groups threaded through, not a bare boolean.
        trial = {name: opts.get(name) for name in combo}
        return _wrap_matcher(base, trial)(expected, actual, test_input)

    combo = _minimal_sufficient_subset(enabled, sufficient)
    return [_OPTION_REASON[name] for name in combo]


# Options ignore_typo composes with before doing its word-level comparison.
# ignore_whitespace is deliberately excluded -- it strips *all* whitespace,
# which would destroy the word boundaries the typo check splits on.
_TYPO_COMPATIBLE_OPTIONS = ("ignore_case", "ignore_punctuation", "symbol_groups")


def _typo_tolerant_match(question: Question, expected: str, actual: str) -> list[str] | None:
    """`ignore_typo`'s fallback, made to compose with whichever of the
    question's other tolerances are also enabled -- a student who both got
    the case wrong *and* misspelled a word (e.g. expected "INVALID
    OPERATOR", actual "invalid aperator" on a question with `ignore_case:
    true`) needs both applied together, not just whichever one the normal
    matcher already tried and failed with. Returns None if it doesn't match
    even with typo tolerance; otherwise the reasons list (typo always first,
    plus the smallest combination of _TYPO_COMPATIBLE_OPTIONS that's also
    sufficient -- same minimal-sufficient-subset approach as
    _reasons_for_pass, and for the same reason: leave-one-out would miss a
    case where e.g. ignore_case and symbol_groups each alone already fix an
    "X" vs "x" difference).
    """
    opts = question.matcher_options if question.matcher_type != "custom" else {}
    enabled = [name for name in _TYPO_COMPATIBLE_OPTIONS if opts.get(name)]

    def norm_both(active: tuple[str, ...]) -> tuple[str, str]:
        kwargs = dict(
            ignore_case="ignore_case" in active,
            ignore_whitespace=False,
            ignore_punctuation="ignore_punctuation" in active,
            symbol_groups=opts.get("symbol_groups") if "symbol_groups" in active else None,
        )
        return _normalize_for_matching(expected, **kwargs), _normalize_for_matching(actual, **kwargs)

    norm_expected, norm_actual = norm_both(tuple(enabled))
    if not match_with_typo_tolerance(norm_expected, norm_actual):
        return None

    def sufficient(combo: tuple[str, ...]) -> bool:
        ce, ca = norm_both(combo)
        return match_with_typo_tolerance(ce, ca)

    combo = _minimal_sufficient_subset(enabled, sufficient)
    return ["spelling not matched"] + [_OPTION_REASON[name] for name in combo]


def get_test_matcher(question: Question) -> TestMatcherFn:
    """The per-test entry point runner.grade_submission actually calls.
    Wraps the question's normal matcher with each test's own `ignore_typo`
    flag: only tried as a fallback once the normal matcher has already
    failed the test, so it can never turn a would-have-passed test into a
    different kind of pass, only rescue one that failed on a pure spelling
    slip. `ignore_typo` is a per-test-case toggle (like `partial`), not a
    question-wide `matcher` option, since the failure mode is usually
    confined to whichever test's expected output happens to contain the
    word a student is prone to misspell (e.g. "INVALID OPERATOR").

    Returns (passed, reasons) rather than a plain bool: `reasons` is empty
    for a strict/exact pass or an outright fail, and non-empty exactly when
    the pass only happened via one or more tolerances -- report.py surfaces
    that instead of showing an indistinguishable plain PASS, the same way it
    already names a FAIL's category.
    """
    base = _base_matcher(question)
    wrapped = get_matcher(question)
    extractor = (
        load_custom_extractor(question.checker_path)
        if question.matcher_type == "custom" and question.checker_path
        else None
    )

    def test_matcher(test: TestCase, actual: str) -> tuple[bool, list[str]]:
        if wrapped(test.expected_text, actual, test.input_text):
            reasons = _reasons_for_pass(question, base, test.expected_text, actual, test.input_text)
            return True, reasons
        if test.ignore_typo:
            # Both sides through the extractor, not just actual -- expected_text
            # is raw YAML text the checker's check()/extract() never touches, so
            # comparing it as-is against an *extracted* actual only works by
            # coincidence (if whatever a question author hand-typed happens to
            # already be in exactly the shape extract() would produce). Running
            # both through the same extractor keeps the comparison apples-to-
            # apples regardless of that.
            typo_expected = extractor(test.expected_text) if extractor else test.expected_text
            typo_actual = extractor(actual) if extractor else actual
            reasons = _typo_tolerant_match(question, typo_expected, typo_actual)
            if reasons is not None:
                return True, reasons
        return False, []

    return test_matcher


@dataclass
class QuestionScore:
    qid: str
    title: str
    marks_earned: float
    marks_total: float
    compile_ok: bool
    compile_stderr: str
    not_submitted: bool = False
    open_details: list[dict] = field(default_factory=list)
    hidden_summary: list[dict] = field(default_factory=list)
    char_input_issues: list[str] = field(default_factory=list)  # scanf %c mistakes auto-adjusted for grading (see char_input_fix.py)
    scanf_address_issues: list[str] = field(default_factory=list)  # missing-& scanf mistakes auto-adjusted for grading (see scanf_address_fix.py)
    # code_checks (None if the question has no code_checks configured)
    code_check_mode: str | None = None
    code_check_require_match: str = "any"
    code_check_satisfied: bool | None = None
    code_check_missing: list[str] = field(default_factory=list)
    code_check_forbidden: list[tuple[str, int]] = field(default_factory=list)
    code_check_found: list[tuple[str, int]] = field(default_factory=list)
    code_check_fallback_notes: list[str] = field(default_factory=list)  # non-empty only when a libclang check failed and fell back to regex
    code_check_marks_earned: float = 0.0
    code_check_marks_total: float = 0.0
    gated_to_zero: bool = False
    penalty_applied: float = 0.0  # marks actually deducted in "penalty" mode (capped at test marks earned)
    raw_test_marks_earned: float = 0.0  # what test marks were, before a gate/penalty adjustment


def score_question(result: QuestionResult) -> QuestionScore:
    q = result.question
    if not result.compile_result.success:
        return QuestionScore(
            qid=q.qid,
            title=q.title,
            marks_earned=0.0,
            marks_total=q.total_marks,
            compile_ok=False,
            compile_stderr=result.compile_result.stderr,
            char_input_issues=result.char_input_issues,
            scanf_address_issues=result.scanf_address_issues,
        )

    test_marks_earned = 0.0
    open_details = []
    hidden_summary = []
    line_gate = q.code_checks_line_gate if q.code_checks_mode == "line_gate" else None
    # For a custom matcher, partial-credit line-scoring below otherwise
    # compares expected_text against a student's *raw* stdout -- if the
    # checker filters out interleaved text (e.g. a menu) that expected_text
    # never had in the first place, the lines never line up. An optional
    # `extract` on the checker module normalizes stdout into the same shape
    # first; see load_custom_extractor. Applied to *both* sides, not just
    # actual: expected_text is raw YAML text the checker's check()/extract()
    # never otherwise touches, so comparing it as-is against an *extracted*
    # actual only lines up by coincidence (whenever whatever a question
    # author hand-typed happens to already be in exactly the shape extract()
    # would produce, e.g. matching rounding precision) -- running both
    # through the same extractor keeps the comparison apples-to-apples
    # regardless of that.
    partial_extractor = (
        load_custom_extractor(q.checker_path) if q.matcher_type == "custom" and q.checker_path else None
    )
    # Partial credit gets the question's own composable tolerances too --
    # without this it fell back to a bare rstrip-and-compare regardless of
    # ignore_case/ignore_whitespace/ignore_punctuation/symbol_groups, so two
    # lines the question's *real* matcher already treats as identical could
    # still fail to earn partial credit for looking different line-for-line.
    line_norm = {
        "ignore_case": q.matcher_options.get("ignore_case"),
        "ignore_whitespace": q.matcher_options.get("ignore_whitespace"),
        "ignore_punctuation": q.matcher_options.get("ignore_punctuation"),
        "symbol_groups": q.matcher_options.get("symbol_groups"),
        "strict_lines": q.matcher_options.get("strict_lines"),
    }
    for outcome in result.outcomes:
        test = outcome.test
        rr = outcome.run_result

        partial_earned = lines_matched = lines_total = None
        passed = outcome.passed
        if test.partial and line_gate and rr is not None and rr.status == "OK":
            # In "line_gate" mode, even a pure-text full match isn't enough
            # on its own -- outcome.passed only checked the text, not
            # whether the gated construct for each line was actually used,
            # so this re-derives pass/partial from the construct-aware score
            # instead of trusting outcome.passed.
            expected_for_partial = partial_extractor(test.expected_text) if partial_extractor else test.expected_text
            actual_for_partial = partial_extractor(rr.stdout) if partial_extractor else rr.stdout
            earned, lines_matched, lines_total = _score_partial_lines_gated(
                expected_for_partial, actual_for_partial, test.weight, line_gate, result.code_check, line_norm
            )
            passed = earned == test.weight
            partial_earned = None if passed else earned
        elif outcome.passed:
            earned = test.weight
        elif test.partial and rr is not None and rr.status == "OK":
            # Only a completed run (right constructs, wrong-ish output) is
            # eligible -- a TIMEOUT/RUNTIME_ERROR/etc. means the run itself
            # failed abnormally, which "some lines were right" doesn't excuse.
            expected_for_partial = partial_extractor(test.expected_text) if partial_extractor else test.expected_text
            actual_for_partial = partial_extractor(rr.stdout) if partial_extractor else rr.stdout
            earned, lines_matched, lines_total = _score_partial_lines(
                expected_for_partial, actual_for_partial, test.weight, line_norm
            )
            partial_earned = earned
        else:
            earned = 0.0
        test_marks_earned += earned

        # Same shape for open and hidden -- input/expected/actual are only
        # rendered by report.py when the test failed (or passed only via a
        # tolerance, which needs the same debuggable detail so the reason is
        # actually checkable), so an ordinary passing hidden test still
        # reveals nothing.
        detail = {
            "name": test.name,
            "weight": test.weight,
            "passed": passed,
            "pass_reasons": outcome.pass_reasons,
            "status": rr.status if rr else "COMPILE_ERROR",
            "message": rr.message if rr else "",
            "input": test.input_text,
            "expected": test.expected_text,
            "actual": rr.stdout if rr else "",
            "partial_earned": partial_earned,
            "lines_matched": lines_matched,
            "lines_total": lines_total,
        }
        (open_details if test.group == "open" else hidden_summary).append(detail)

    score = QuestionScore(
        qid=q.qid,
        title=q.title,
        marks_earned=test_marks_earned,
        marks_total=q.total_marks,
        compile_ok=True,
        compile_stderr=result.compile_result.stderr,
        open_details=open_details,
        hidden_summary=hidden_summary,
        char_input_issues=result.char_input_issues,
        scanf_address_issues=result.scanf_address_issues,
    )

    cc = result.code_check
    if cc is not None:
        score.code_check_mode = q.code_checks_mode
        score.code_check_require_match = q.code_checks_require_match
        score.code_check_satisfied = cc.satisfied
        score.code_check_missing = cc.missing_required
        score.code_check_forbidden = cc.found_forbidden
        score.code_check_found = cc.found_required
        score.code_check_fallback_notes = cc.fallback_notes

        if q.code_checks_mode == "gate" and not cc.satisfied:
            score.gated_to_zero = True
            score.raw_test_marks_earned = test_marks_earned
            score.marks_earned = 0.0
        elif q.code_checks_mode == "bonus":
            score.code_check_marks_total = q.code_checks_marks
            score.code_check_marks_earned = q.code_checks_marks if cc.satisfied else 0.0
            score.marks_earned = test_marks_earned + score.code_check_marks_earned
        elif q.code_checks_mode == "penalty":
            score.code_check_marks_total = q.code_checks_penalty
            if not cc.satisfied:
                score.raw_test_marks_earned = test_marks_earned
                # can't deduct more than the student actually earned from the tests
                score.penalty_applied = min(q.code_checks_penalty, test_marks_earned)
                score.marks_earned = max(0.0, test_marks_earned - q.code_checks_penalty)

    return score
