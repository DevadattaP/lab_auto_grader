"""Per-student markdown reports + the class-wide summary.csv gradebook."""

from __future__ import annotations

import ast
import csv
import html as html_escape
import re
from pathlib import Path

import markdown as markdown_lib

from grader.discover import StudentMapping
from grader.scorer import QuestionScore


def _earned(d: dict) -> float:
    if d["passed"]:
        return d["weight"]
    if d.get("partial_earned") is not None:
        return d["partial_earned"]
    return 0.0


def _format_mark(d: dict) -> str:
    if d["passed"]:
        reasons = d.get("pass_reasons")
        if reasons:
            return f"PASS ({', '.join(reasons)})"
        return "PASS"
    if d.get("partial_earned") is not None:
        return (
            f"PARTIAL ({d['lines_matched']}/{d['lines_total']} lines, "
            f"+{d['partial_earned']:g}/{d['weight']:g} marks)"
        )
    if d["status"] == "OK":
        # Ran fine, just produced the wrong output -- "FAIL (OK)" reads as
        # contradictory, so only show a parenthetical for an actual
        # abnormal-run category (TIMEOUT, RUNTIME_ERROR, ...).
        return "FAIL"
    detail = f": {d['message']}" if d.get("message") else ""
    return f"FAIL ({d['status']}{detail})"


def _render_test_list(details: list[dict]) -> list[str]:
    lines = []
    for d in details:
        lines.append(f"  - `{d['name']}`: {_format_mark(d)}")
        # A tolerated pass needs the same expected/actual detail as a failure
        # -- otherwise it's indistinguishable from an exact match and the
        # tolerated mistake goes unnoticed. Not stripped -- a difference that
        # is purely leading/trailing whitespace (the reason the test needed a
        # tolerance in the first place) must stay visible here, not get
        # erased right before display.
        if not d["passed"] or d.get("pass_reasons"):
            lines.append(f"    - input: `{d['input']!r}`")
            lines.append(f"    - expected: `{d['expected']!r}`")
            lines.append(f"    - actual: `{d['actual']!r}`")
    return lines


def _render_code_check(qs: QuestionScore) -> list[str]:
    status = "PASSED" if qs.code_check_satisfied else "FAILED"
    if qs.code_check_mode == "gate":
        lines = [f"- Code checks (gate): {status}"]
    elif qs.code_check_mode == "penalty":
        lines = [f"- Code checks (penalty, up to -{qs.code_check_marks_total:g} marks): {status}"]
    elif qs.code_check_mode == "line_gate":
        # No pass/fail at the question level -- each gated line's own marks
        # (shown as PARTIAL in the test list above) already reflect whether
        # its required construct was used, so this is descriptive only.
        lines = ["- Code checks (line-gate): per-line marks above already require each line's construct"]
    else:
        lines = [
            f"- Code checks (bonus, {qs.code_check_marks_total:g} marks): "
            f"{status} ({qs.code_check_marks_earned:g}/{qs.code_check_marks_total:g})"
        ]
    if qs.code_check_found or qs.code_check_missing:
        lines.append(f"  - require ({qs.code_check_require_match} of the following):")
    for name, line_no in qs.code_check_found:
        lines.append(f"    - `{name}`: found (line {line_no})")
    for name in qs.code_check_missing:
        lines.append(f"    - `{name}`: **not found**")
    for name, line_no in qs.code_check_forbidden:
        lines.append(f"  - forbidden `{name}`: found at line {line_no} -- **not allowed**")
    if qs.gated_to_zero:
        lines.append(
            f"  - all marks for this question zeroed due to gate violation "
            f"(test score would otherwise have been {qs.raw_test_marks_earned:g})"
        )
    if qs.code_check_mode == "penalty" and qs.penalty_applied > 0:
        lines.append(
            f"  - penalty applied: -{qs.penalty_applied:g} marks "
            f"(test score {qs.raw_test_marks_earned:g} -> {qs.marks_earned:g})"
        )
    for note in qs.code_check_fallback_notes:
        lines.append(f"  - note: {note}")
    return lines


def _render_char_input_issues(qs: QuestionScore) -> list[str]:
    if not qs.char_input_issues:
        return []
    lines = [
        "- scanf `%c` mistake(s) detected -- your submission was compiled with a "
        "corrected copy for grading (your submitted file is unchanged):",
    ]
    lines.extend(f"  - {issue}" for issue in qs.char_input_issues)
    return lines


def _render_scanf_address_issues(qs: QuestionScore) -> list[str]:
    if not qs.scanf_address_issues:
        return []
    lines = [
        "- scanf missing-`&` mistake(s) detected -- your submission was compiled with a "
        "corrected copy for grading (your submitted file is unchanged):",
    ]
    lines.extend(f"  - {issue}" for issue in qs.scanf_address_issues)
    return lines


def _render_question_section(qs: QuestionScore, *, show_hidden: bool = True) -> list[str]:
    """One question's section, from its `## qid: title` header through the
    trailing blank line. Factored out of render_student_report so the
    live-lab platform's single-question report (render_live_question_report,
    below) can reuse the exact same rendering instead of duplicating it.
    `show_hidden=False` omits the "Hidden tests" line entirely (not just
    leaves it at 0/0) -- used for a live, open-tests-only run where there is
    no hidden_summary content to report and showing "Hidden tests: 0/0"
    would misleadingly read as "this question has no hidden tests" rather
    than "hidden tests aren't run until the lab ends".
    """
    lines = [f"## {qs.qid}: {qs.title} — {qs.marks_earned:g} / {qs.marks_total:g}", ""]
    if not qs.compile_ok:
        if qs.not_submitted:
            lines.append("- **No submission found for this question.**")
        else:
            lines.append("- Compiled: **no**")
        if qs.compile_stderr:
            lines.append("")
            lines.append("```")
            lines.append(qs.compile_stderr.strip())
            lines.append("```")
        lines.extend(_render_char_input_issues(qs))
        lines.extend(_render_scanf_address_issues(qs))
        lines.append("")
        return lines

    lines.append("- Compiled: yes")
    lines.extend(_render_char_input_issues(qs))
    lines.extend(_render_scanf_address_issues(qs))
    open_earned = sum(_earned(d) for d in qs.open_details)
    open_total = sum(d["weight"] for d in qs.open_details)
    lines.append(f"- Open tests: {open_earned:g}/{open_total:g}")
    lines.extend(_render_test_list(qs.open_details))
    if show_hidden:
        hidden_earned = sum(_earned(d) for d in qs.hidden_summary)
        hidden_total = sum(d["weight"] for d in qs.hidden_summary)
        lines.append(f"- Hidden tests: {hidden_earned:g}/{hidden_total:g}")
        lines.extend(_render_test_list(qs.hidden_summary))

    if qs.code_check_mode is not None:
        lines.extend(_render_code_check(qs))

    lines.append("")
    return lines


def render_student_report(roll_no: str, question_scores: list[QuestionScore], anomalies: list[str]) -> str:
    lines = [f"# Report — {roll_no}", ""]
    if anomalies:
        lines.append("**Anomalies:** " + "; ".join(anomalies))
        lines.append("")

    total_earned = 0.0
    total_possible = 0.0
    for qs in question_scores:
        total_earned += qs.marks_earned
        total_possible += qs.marks_total
        lines.extend(_render_question_section(qs))

    lines.append(f"## Total: {total_earned:g} / {total_possible:g}")
    lines.append("")
    return "\n".join(lines)


def render_live_question_report(roll_no: str, qs: QuestionScore) -> str:
    """Single-question live-run report, rewritten on every Run click (see
    LIVE_LAB_DESIGN.md §7.3) -- same rendering vocabulary as one question's
    section of render_student_report, just for one question at a time,
    refreshed live instead of produced once at lab-end, and hidden-test-free
    by construction: `qs` is expected to come from an open-tests-only run
    (grader.student_view.run_open_tests), so hidden_summary is always empty
    and is omitted from the rendering entirely rather than shown as 0/0.
    """
    lines = [
        f"# Live run — {roll_no}",
        "",
        "_Open tests only — hidden tests are graded after the lab ends._",
        "",
    ]
    lines.extend(_render_question_section(qs, show_hidden=False))
    return "\n".join(lines)


def write_summary_csv(
    path: Path,
    question_ids: list[str],
    rows: list[tuple[str, dict[str, QuestionScore], list[str]]],
    student_mapping: StudentMapping | None = None,
) -> None:
    """rows: list of (roll_no, {qid: QuestionScore}, anomalies), already
    sorted by roll_no by the caller. `student_mapping` (see discover.py) is
    optional enrichment: when it has an 'ip' and/or 'name' column, those are
    added as the first column(s), in that order, ahead of roll_no -- a roll_no
    with no matching entry just gets blank cells there, never a dropped row.
    """
    path = Path(path)
    mapping = student_mapping or StudentMapping()
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        header = []
        if mapping.has_ip:
            header.append("ip")
        if mapping.has_name:
            header.append("name")
        header += ["roll_no", *question_ids, "total", "anomalies"]
        writer.writerow(header)
        for roll_no, scores_by_qid, anomalies in rows:
            info = mapping.entries.get(roll_no, {})
            row = []
            if mapping.has_ip:
                row.append(info.get("ip", ""))
            if mapping.has_name:
                row.append(info.get("name", ""))
            row.append(roll_no)
            total = 0.0
            max_marks = 0.0
            for qid in question_ids:
                qs = scores_by_qid.get(qid)
                if qs is None:
                    row.append("")
                else:
                    row.append(f"{qs.marks_earned:g}/{qs.marks_total:g}")
                    total += qs.marks_earned
                    max_marks += qs.marks_total
            row.append(f"{total:g}/{max_marks:g}")
            row.append("; ".join(anomalies))
            writer.writerow(row)


def _question_label(qid: str) -> str:
    """"q01_result_grade" -> "Q01" -- every question id in this project is
    "q<NN>_<name>", so the leading "q<NN>" segment alone is a short, stable
    per-student-facing label without pulling in the (often long) title."""
    return qid.split("_", 1)[0].upper()


def write_grade_feedback_md(
    path: Path,
    question_ids: list[str],
    rows: list[tuple[str, dict[str, QuestionScore], list[str]]],
    student_mapping: StudentMapping | None = None,
) -> None:
    """A short, distributable-per-student feedback sheet -- one `## roll_no -
    name` section per student (name if `student_mapping` has one), one line
    per question's earned/total, then a Total line, in the same roll-number
    order as `rows` (already sorted by the caller). Unlike report_<roll_no>.md
    this carries no test detail, code check detail, or scanf-fixup notes --
    just the marks -- so it's safe to hand back to students as-is.
    """
    mapping = student_mapping or StudentMapping()
    blocks = ["# Grade feedback report"]
    for roll_no, scores_by_qid, _anomalies in rows:
        name = mapping.entries.get(roll_no, {}).get("name")
        lines = [f"## {roll_no} - {name}" if name else f"## {roll_no}"]
        total = 0.0
        max_marks = 0.0
        for qid in question_ids:
            qs = scores_by_qid.get(qid)
            if qs is None:
                lines.append(f"- {_question_label(qid)} : -")
            else:
                lines.append(f"- {_question_label(qid)} : {qs.marks_earned:g}/{qs.marks_total:g}")
                total += qs.marks_earned
                max_marks += qs.marks_total
        lines.append(f"- Total = {total:g}/{max_marks:g}")
        blocks.append("\n".join(lines))

    Path(path).write_text("\n\n".join(blocks) + "\n")


# --------------------------------------------------------------------------
# report markdown -> HTML, shared by every UI that displays a rendered report
# (ui/app.py's dashboard/result viewer, server_student's "My Report" page,
# server_admin's live dashboard) -- one place that knows how to turn this
# module's own markdown output back into readable HTML, instead of each
# consumer re-implementing (and inevitably drifting from) the same
# postprocessing.
# --------------------------------------------------------------------------

# render_student_report/_render_test_list write a failed/partial test's
# input/expected/actual as three consecutive list items via Python's
# repr() -- e.g. `'*******\n *****\n  ***\n   *\n'` -- specifically so a real
# newline or run of spaces is visible as literal `\n`/spaces in the raw .md
# file (readable in a plain editor/terminal). Markdown then renders that as
# three separate <li>label: <code>'...'</code></li> items stacked
# vertically, each showing its repr'd text as one long line with literal
# backslash-n *characters* in it, not an actual line break. This matches
# all three together (report.py always emits them as this exact
# input/expected/actual triplet, nothing else interleaved) and replaces
# them with one flex row of three bordered, titled columns -- decoding
# each field's repr text back to its real bytes and rendering it in a
# <pre> so a multi-line value (a pyramid's rows, a multi-line Secret Pair
# report, ...) shows real line breaks and indentation, side by side for
# easy comparison instead of stacked and hard to visually diff. Falls back
# to leaving the original three <li>s untouched if any field isn't valid
# repr() syntax (defensive -- should never happen against this module's own
# output, but this is markdown someone could hand-edit).
_IO_TRIPLET_RE = re.compile(
    r"<ul>\s*"
    r"<li>input: <code>(.*?)</code></li>\s*"
    r"<li>expected: <code>(.*?)</code></li>\s*"
    r"<li>actual: <code>(.*?)</code></li>\s*"
    r"</ul>",
    re.DOTALL,
)


def render_io_side_by_side(html: str) -> str:
    def repl(m: re.Match) -> str:
        cols = []
        for label, code_content in zip(("input", "expected", "actual"), m.groups()):
            raw_repr = html_escape.unescape(code_content)
            try:
                value = ast.literal_eval(raw_repr)
            except (ValueError, SyntaxError):
                return m.group(0)
            cols.append(
                f'<div class="io-col"><div class="io-col-title">{label}</div>'
                f"<pre>{html_escape.escape(value)}</pre></div>"
            )
        return f'<div class="io-row">{"".join(cols)}</div>'

    return _IO_TRIPLET_RE.sub(repl, html)


# Markdown wraps a `` `...` `` span as a bare <code>...</code> with no
# white-space CSS override, so consecutive literal spaces inside one (e.g. a
# test *name* like `pair_AK` glued to surrounding punctuation) collapse
# down to a single visible space when a browser renders the HTML -- even
# though the text node itself still has all of them. A ```fenced``` block
# renders as <pre><code>...</code></pre> instead, which already preserves
# whitespace via a `pre { white-space: pre-wrap }` rule wherever this HTML
# is embedded, so that's left alone here (matched by the first branch, kept
# verbatim). Only a bare inline span (second branch) gets its regular
# spaces swapped for &nbsp; -- sidesteps whitespace-collapsing at the HTML
# level so it doesn't matter what CSS the page embedding this HTML does or
# doesn't apply. Run render_io_side_by_side first so the input/expected/
# actual fields it already converted to <pre> (which don't need this trick
# at all) aren't also matched here.
_CODE_SPAN_RE = re.compile(r"(<pre><code>.*?</code></pre>)|(<code>.*?</code>)", re.DOTALL)


def preserve_inline_code_whitespace(html: str) -> str:
    def repl(m: re.Match) -> str:
        inline_span = m.group(2)
        return inline_span.replace(" ", "&nbsp;") if inline_span is not None else m.group(1)

    return _CODE_SPAN_RE.sub(repl, html)


def render_markdown_to_html(markdown_text: str) -> str:
    """The full pipeline: this module's markdown -> readable HTML, ready to
    embed in any of the UIs listed above. tab_length=2 matches how
    render_student_report/render_live_question_report nest list items (2
    spaces, correct CommonMark-style) -- python-markdown's default of 4
    would otherwise silently flatten every nested list (the per-test
    PASS/FAIL bullets, and the input/expected/actual bullets nested under a
    failed/tolerated test) into one single-level list.
    """
    html = markdown_lib.markdown(markdown_text, extensions=["fenced_code", "tables"], tab_length=2)
    html = render_io_side_by_side(html)
    html = preserve_inline_code_whitespace(html)
    return html
