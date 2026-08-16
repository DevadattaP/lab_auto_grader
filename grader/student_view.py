"""Student-facing question/run views for the live-lab platform.

The key guarantee here (see LIVE_LAB_DESIGN.md §7.1): hidden tests never
leak into a live "Run" response by construction, not by after-the-fact
redaction. `open_only` builds a Question whose `tests` list simply never
contains a hidden TestCase, so no dict/field anywhere in the rest of the
call chain (runner.grade_submission -> scorer.score_question) could carry
hidden input/expected/actual data even if a future bug in scorer.py's own
redaction were introduced -- there is nothing hidden to leak once the
Question object itself doesn't have it.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path

from grader.config_schema import Question
from grader.runner import grade_submission
from grader.sandbox import Sandbox
from grader.scorer import QuestionScore, get_test_matcher, score_question


@dataclass
class StudentQuestionSummary:
    qid: str
    title: str
    description: str
    open_test_count: int
    marks_open: float


def student_question_summary(question: Question) -> StudentQuestionSummary:
    """The question list view (`/api/questions`) -- title/description/open-
    test-count/open-marks only. No gold path, no hidden test count or
    marks, no checker/code_checks internals -- nothing here reveals more
    than what a question's own description already tells a student."""
    return StudentQuestionSummary(
        qid=question.qid,
        title=question.title,
        description=question.description,
        open_test_count=len(question.tests_in("open")),
        marks_open=question.marks_open,
    )


def open_only(question: Question) -> Question:
    """A Question with only its open tests. `marks_hidden` is also zeroed
    (not just `tests` filtered) so `Question.total_marks` -- and therefore
    every `QuestionScore.marks_total` computed from it -- reflects only what
    was actually attempted, rather than showing e.g. "18 / 100" against a
    marks_total that still silently included 80 hidden marks nothing here
    ever ran for."""
    return dataclasses.replace(question, tests=question.tests_in("open"), marks_hidden=0.0)


def run_open_tests(
    sandbox: Sandbox,
    question: Question,
    source_path: Path,
    binary_dest: Path,
) -> QuestionScore:
    """Compile the student's current source and run it against this
    question's open tests only -- the "Run" button's server-side call chain,
    mirroring the exact 3-call sequence grade.py._grade_student_question
    uses for the batch grader (get_test_matcher -> grade_submission ->
    score_question), just against `open_only(question)` instead of the full
    question. Returns the same QuestionScore shape the batch grader
    produces: `open_details` populated, `hidden_summary` always empty (no
    hidden TestCase ever entered `question`), and `marks_total` equal to
    `question.marks_open` (plus a code_checks bonus, if configured) -- the
    correct ceiling to display for an open-only run.
    """
    q_open = open_only(question)
    matcher = get_test_matcher(q_open)
    result = grade_submission(sandbox, q_open, source_path, binary_dest, matcher)
    return score_question(result)
