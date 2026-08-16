"""Per-student markdown reports + the class-wide summary.csv gradebook."""

from __future__ import annotations

import csv
from pathlib import Path

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
        lines.append(f"## {qs.qid}: {qs.title} — {qs.marks_earned:g} / {qs.marks_total:g}")
        lines.append("")
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
            continue

        lines.append("- Compiled: yes")
        lines.extend(_render_char_input_issues(qs))
        lines.extend(_render_scanf_address_issues(qs))
        open_earned = sum(_earned(d) for d in qs.open_details)
        open_total = sum(d["weight"] for d in qs.open_details)
        hidden_earned = sum(_earned(d) for d in qs.hidden_summary)
        hidden_total = sum(d["weight"] for d in qs.hidden_summary)
        lines.append(f"- Open tests: {open_earned:g}/{open_total:g}")
        lines.extend(_render_test_list(qs.open_details))
        lines.append(f"- Hidden tests: {hidden_earned:g}/{hidden_total:g}")
        lines.extend(_render_test_list(qs.hidden_summary))

        if qs.code_check_mode is not None:
            lines.extend(_render_code_check(qs))

        lines.append("")

    lines.append(f"## Total: {total_earned:g} / {total_possible:g}")
    lines.append("")
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
