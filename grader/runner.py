"""Orchestrates: compile once per student+question, run every test case
against that one binary, and hand back raw results for scorer.py to turn
into marks. This module knows nothing about marks or matching -- only about
compiling and running.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from grader.char_input_fix import analyze_and_fix_char_input
from grader.code_checks import CodeCheckResult, run_code_checks
from grader.config_schema import Question, TestCase
from grader.sandbox import CompileResult, RunResult, Sandbox
from grader.scanf_address_fix import analyze_and_fix_scanf_address


@dataclass
class TestOutcome:
    test: TestCase
    run_result: RunResult | None  # None only when compilation itself failed
    passed: bool = False
    pass_reasons: list[str] = field(default_factory=list)  # non-empty only when passed via a tolerance, not an exact match


@dataclass
class QuestionResult:
    question: Question
    compile_result: CompileResult
    outcomes: list[TestOutcome] = field(default_factory=list)
    code_check: CodeCheckResult | None = None  # None if question has no code_checks configured
    char_input_issues: list[str] = field(default_factory=list)  # only non-empty when question.adjust_char_input caught something
    scanf_address_issues: list[str] = field(default_factory=list)  # only non-empty when question.adjust_scanf_address caught something


def grade_submission(
    sandbox: Sandbox,
    question: Question,
    source_path: Path,
    binary_dest: Path,
    matcher_fn: Callable[[TestCase, str], tuple[bool, list[str]]],
) -> QuestionResult:
    char_input_issues: list[str] = []
    scanf_address_issues: list[str] = []
    compile_source = source_path
    if question.adjust_char_input or question.adjust_scanf_address:
        # Both fixups operate on -- and each independently re-scans -- whatever
        # source text they're handed, so they compose freely regardless of
        # order: one rewrites format strings, the other rewrites argument
        # lists, and neither touches what the other looks at.
        working_source = source_path.read_text(errors="replace")
        if question.adjust_char_input:
            working_source, char_input_issues = analyze_and_fix_char_input(working_source)
        if question.adjust_scanf_address:
            working_source, scanf_address_issues = analyze_and_fix_scanf_address(working_source)

        if char_input_issues or scanf_address_issues:
            # A patched copy lives alongside the compiled binary (same per-student/
            # per-question directory) so it's inspectable after a run, same as the
            # binary itself -- never overwrites the student's actual submission.
            compile_source = binary_dest.parent / "student_scanf_fixed.c"
            compile_source.parent.mkdir(parents=True, exist_ok=True)
            compile_source.write_text(working_source)

    compile_result = sandbox.compile_c(
        compile_source, binary_dest, question.compile_standard, question.compile_flags
    )
    if not compile_result.success:
        return QuestionResult(
            question=question,
            compile_result=compile_result,
            outcomes=[TestOutcome(test=t, run_result=None, passed=False) for t in question.tests],
            char_input_issues=char_input_issues,
            scanf_address_issues=scanf_address_issues,
        )

    code_check = None
    if question.code_checks_mode is not None:
        code_check = run_code_checks(
            source_path,
            question.code_checks_require,
            question.code_checks_forbid,
            require_match=question.code_checks_require_match,
            compile_standard=question.compile_standard,
        )

    outcomes = []
    for test in question.tests:
        run_result = sandbox.run(compile_result.binary_path, test.input_text, question.limits)
        passed, pass_reasons = (False, [])
        if run_result.status == "OK":
            passed, pass_reasons = matcher_fn(test, run_result.stdout)
        outcomes.append(
            TestOutcome(test=test, run_result=run_result, passed=passed, pass_reasons=pass_reasons)
        )
    return QuestionResult(
        question=question,
        compile_result=compile_result,
        outcomes=outcomes,
        code_check=code_check,
        char_input_issues=char_input_issues,
        scanf_address_issues=scanf_address_issues,
    )


def self_check_gold(sandbox: Sandbox, question: Question, tmp_binary_path: Path) -> list[str]:
    """Compile+run the gold solution through the exact same harness a student
    would go through. Returns names of any tests the gold program fails, plus
    a synthetic entry if the gold program itself violates its own
    code_checks -- should always be empty. A non-empty result means a test
    case (or the code_checks configuration) is wrong, not that a student is
    wrong, and grading should not proceed until fixed.
    """
    from grader.scorer import get_test_matcher  # local import: avoids a module-load cycle with scorer.py

    matcher = get_test_matcher(question)
    result = grade_submission(sandbox, question, question.gold_path, tmp_binary_path, matcher)
    if not result.compile_result.success:
        # compile_result.stderr is already capped at COMPILE_FLAGS_MAX_STDERR
        # (sandbox.py) -- no need for a second, much stricter truncation here,
        # which previously cut the message off at 200 chars and could hide
        # the actual fatal error behind an earlier warning (e.g. a real
        # "undefined reference to `sqrt'" link error past the 200-char mark,
        # with only an unrelated scanf warning visible before the cutoff).
        return [f"<gold program failed to compile: {result.compile_result.stderr}>"]
    failures = [o.test.name for o in result.outcomes if not o.passed]
    if result.code_check is not None and not result.code_check.satisfied:
        detail = []
        if result.code_check.missing_required:
            detail.append(f"missing required: {result.code_check.missing_required}")
        if result.code_check.found_forbidden:
            detail.append(f"uses forbidden: {[n for n, _ in result.code_check.found_forbidden]}")
        failures.append(f"<gold program violates its own code_checks: {'; '.join(detail)}>")
    return failures
