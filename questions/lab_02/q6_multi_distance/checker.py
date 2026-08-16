"""Custom checker for the Multi-Distance Calculator.

The program's menu/prompt text ("1. Euclidean Distance", "Enter choice: ",
...) is re-printed on every loop iteration and every student is free to word
or format it however they like -- it isn't part of what this question is
actually testing. This checker ignores all of that and only checks for the
program's *result* lines: the four "<Metric> Distance : <value>" lines and
the three fixed status messages (INVALID ORDER / Invalid choice / Exiting.
Goodbye!), found anywhere in the raw output regardless of what menu text a
student glued them onto (e.g. an un-newlined "Enter choice: " prompt right
before the first result of that iteration).

Numeric values (the distance itself, and Minkowski's `p`) are parsed and
rounded to _ROUND_DP decimal places before comparing, rather than compared
as literal text -- the reference solution prints 4 decimal places for a
distance and 1 for `p`, but a student who's logically correct may
reasonably print a different number of decimal places (5, 2, no decimal
point on an integer `p`, ...) and shouldn't lose marks for that alone.
See _ROUND_DP's own comment for the precision/cushion trade-off in picking
its value -- either way, a genuinely wrong computation will essentially
never land within a few decimal places by coincidence.

The label-value separator (":" in the reference solution, e.g.
"Euclidean Distance : 7.0711") also accepts any extra separator this
question's matcher.symbol_groups declares interchangeable with ":" (e.g.
"=", for a student who writes "Euclidean distance = 7.0711") -- but *only*
when the value has a decimal point. A real distance is always printed with
decimals per the assignment spec; a bare small integer right after that
same separator is far more likely to be a menu entry a student numbered
"Euclidean distance = 1" than an actual (and coincidentally very wrong)
result, so requiring a decimal point on any separator other than the
canonical ":" avoids misreading the menu itself as a result line.

Re-applies this question's own `matcher.options` (ignore_case,
ignore_whitespace, ignore_punctuation, symbol_groups) by reading them back
out of the neighboring question.yaml -- none of that wrapping runs
automatically for matcher.type: custom (see README's "Matcher types
available" section), so a custom checker that still wants it has to redo it
itself. Reuses grader.scorer's actual normalization helper rather than
reimplementing it, so behavior here can't silently drift from what those
options mean for every other question. `allow_extra_output` is deliberately
NOT re-applied here -- its "actual must end with expected" heuristic solves
a narrower version of the exact problem this checker's regex-based
extraction already solves more generally (locating the real content
regardless of what surrounds it), so it would add nothing.

Also defines extract(actual) -> str, which grader.scorer uses to give
`partial`/`ignore_typo` (both implemented as a *raw* line-by-line compare
against expected_text, outside this checker's control) the same filtered,
numerically-canonicalized view check() itself compares against -- without
it, those two tolerances would compare expected_text against raw,
unfiltered, un-rounded stdout and almost never line up. See
load_custom_extractor in grader/scorer.py.

The metric-name keyword itself (e.g. "Minkowski") is also typo-tolerant, not
just an exact (case-insensitive) match: extraction runs *before* the
framework's own `ignore_typo` fallback ever gets a chance to run (that
fallback compares whatever this module's extract() already produced -- see
its docstring above), so a misspelled keyword (a student who wrote
"Minkowsi Distance (p=1.000000) : 12.0000" for "Minkowski Distance...")
would otherwise never even become a recognized event for ignore_typo to
rescue, regardless of that test's own ignore_typo setting. `_closest_metric`
reuses grader.scorer's own 1-edit-distance word comparison (the same
tolerance `ignore_typo` grants everywhere else in this grader) so "what
counts as a typo" can't quietly drift between the two, and only kicks in for
an actual misspelling -- a correctly-spelled word (any case) is returned
as-is, preserving the student's original case for ignore_case (applied
later, in check()) to decide on; only a genuine typo gets normalized to the
canonical spelling.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from grader.scorer import _normalize_for_matching, _word_matches_with_typo

# See module docstring -- matches the reference solution's own distance
# precision (4 d.p.); lower it for more cushion against last-digit
# rounding-mode/algorithm noise between two correct implementations, higher
# for stricter precision.
_ROUND_DP = 4

_INT_OR_FLOAT = r"[-+]?\d+(?:\.\d+)?"
_FLOAT_ONLY = r"[-+]?\d+\.\d+"
_LABEL_WORD = r"[A-Za-z]+"

_SIMPLE_METRICS = ("Euclidean", "Manhattan", "Chebyshev")
_MINKOWSKI = ("Minkowski",)


def _closest_metric(word: str, candidates: tuple[str, ...]) -> str | None:
    """Fuzzy-matches a captured label word against known-correct metric
    names, tolerating the same 1-edit typo (case-insensitive)
    grader.scorer's ignore_typo already forgives elsewhere. Returns `word`
    itself on an exact (case-insensitive) match -- preserving whatever case
    the student actually used, for ignore_case to decide on later -- the
    *canonical* spelling on a genuine typo (so the extracted line always
    compares byte-for-byte against a correctly-spelled expected line
    regardless of which near-spelling a student used), or None if `word`
    isn't close to any candidate at all.
    """
    word_l = word.lower()
    for candidate in candidates:
        if word_l == candidate.lower():
            return word
    for candidate in candidates:
        if _word_matches_with_typo(candidate.lower(), word_l):
            return candidate
    return None


def _matcher_options() -> dict:
    raw = yaml.safe_load((Path(__file__).parent / "question.yaml").read_text()) or {}
    matcher = raw.get("matcher") or {}
    return {k: v for k, v in matcher.items() if k != "type"}


def _extra_separators(opts: dict) -> list[str]:
    """Any symbol this question's matcher.symbol_groups declares
    interchangeable with ':', other than ':' itself."""
    extra = []
    for group in opts.get("symbol_groups") or []:
        if ":" in group:
            extra.extend(s for s in group if s != ":")
    return extra


def _build_event_re(opts: dict) -> re.Pattern:
    extra = _extra_separators(opts)
    # ':' always accepted with either an integer or a decimal value -- it's
    # the format the spec actually asks for, so there's no menu-collision
    # risk to guard against. Any extra declared-interchangeable separator is
    # only accepted with a decimal value (see module docstring).
    if extra:
        alt_sep = "|".join(re.escape(s) for s in sorted(set(extra), key=len, reverse=True))
        sep_value = (
            rf"(?:\s*:\s*(?P<val{{n}}>{_INT_OR_FLOAT})"
            rf"|\s*(?:{alt_sep})\s*(?P<val{{n}}b>{_FLOAT_ONLY}))"
        )
    else:
        sep_value = rf"\s*:\s*(?P<val{{n}}>{_INT_OR_FLOAT})"

    # The label word itself is captured generically (any run of letters)
    # rather than a hardcoded "Euclidean|Manhattan|..." alternation --
    # extract() fuzzy-matches the captured word against the canonical names
    # afterward (_closest_metric), so a misspelled keyword is still
    # recognized as that metric instead of the whole line silently
    # vanishing from extraction. The structural requirements around it
    # (Distance, then a real separator+value, or Minkowski's "(p=...)")
    # still gate what counts as a candidate at all -- an unrelated word
    # followed by "Distance :" is not a menu/prompt shape a student
    # normally produces, but _closest_metric rejects it as not being any
    # known metric name either way, so it never gets extracted regardless.
    label1 = rf"(?P<label1>{_LABEL_WORD})\s+Distance"
    label2 = rf"(?P<label2>{_LABEL_WORD})\s+Distance\s*\(\s*p\s*=\s*(?P<p>{_INT_OR_FLOAT})\s*\)"
    label3 = r"(?P<label3>INVALID\s+ORDER|Invalid\s+choice|Exiting\.?\s*Goodbye!?)"

    pattern = (
        label1 + sep_value.format(n="1")
        + "|" + label2 + sep_value.format(n="2")
        + "|" + label3
    )
    # re.IGNORECASE always on: this only affects *detection* (does a
    # recognizable event exist here at all), not the correctness verdict --
    # the captured text preserves whatever case was actually present, and
    # ignore_case (applied later, in check()) decides whether case
    # differences there are forgiven.
    return re.compile(pattern, re.IGNORECASE)


def _canon_num(s: str) -> str:
    return f"{round(float(s), _ROUND_DP):.{_ROUND_DP}f}"


def extract(actual: str) -> str:
    """Reduce raw stdout down to just its result/status lines, in the order
    they occur, with every numeric value rounded to a canonical precision --
    discards the menu and any prompts regardless of how a student worded or
    newline-terminated them (the pattern is searched for anywhere in the
    text rather than anchored to a line start), and discards any
    excess/insufficient decimal-place formatting a student used."""
    event_re = _build_event_re(_matcher_options())
    lines = []
    for m in event_re.finditer(actual):
        if m.group("label1"):
            metric = _closest_metric(m.group("label1"), _SIMPLE_METRICS)
            if metric is None:
                continue
            val = m.group("val1") or m.group("val1b")
            lines.append(f"{metric} Distance : {_canon_num(val)}")
        elif m.group("label2"):
            metric = _closest_metric(m.group("label2"), _MINKOWSKI)
            if metric is None:
                continue
            val = m.group("val2") or m.group("val2b")
            lines.append(f"{metric} Distance (p={_canon_num(m.group('p'))}) : {_canon_num(val)}")
        elif m.group("label3"):
            lines.append(m.group("label3"))
    return "\n".join(lines)


def check(expected: str, actual: str, test_input: str) -> bool:
    opts = _matcher_options()
    kwargs = dict(
        ignore_case=bool(opts.get("ignore_case", False)),
        ignore_whitespace=bool(opts.get("ignore_whitespace", False)),
        ignore_punctuation=bool(opts.get("ignore_punctuation", False)),
        symbol_groups=opts.get("symbol_groups"),
    )
    norm_expected = _normalize_for_matching(extract(expected), **kwargs)
    norm_actual = _normalize_for_matching(extract(actual), **kwargs)
    return norm_expected == norm_actual
