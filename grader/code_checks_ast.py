"""libclang-based (real AST) implementations of the code_checks constructs
in code_checks.py, for a more precise check than the regex heuristics there
can give -- e.g. an exact binary-vs-unary `&` (a genuinely different cursor
kind, not "what precedes it"), no comment/string-literal ambiguity (clang
never sees comment/literal *contents* as code in the first place), and
`function_def_used`'s reachability computed from the compiler's own
understanding of each call, not a regex-built approximation of one.

This module is entirely optional and never imported at module load time by
code_checks.py: every entry point here either returns a real result or
raises `AstUnavailable` (or lets a `clang`-internal exception propagate),
and the caller in code_checks.py treats *any* exception from this module as
"AST detection unavailable for this construct/file -- fall back to the
regex-based check", never a hard failure of the grading run itself.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class AstUnavailable(Exception):
    """Raised (and always caught by code_checks.py) when libclang can't be
    trusted for this file or doesn't support a given construct -- the
    caller's response is always the same: fall back to the regex check."""


@functools.lru_cache(maxsize=1)
def _resource_dir() -> str | None:
    """Best-effort discovery of a system clang's resource directory (the
    one holding compiler-builtin headers like stddef.h/stdarg.h).
    `pip install libclang` ships only libclang.so, not a full toolchain --
    without pointing it at a real resource dir, parsing a file that pulls
    in glibc's <stdio.h> produces a FATAL "'stddef.h' file not found"
    diagnostic. In practice this alone didn't corrupt the AST for ordinary
    intro-C student code in testing (no stddef/stdarg-dependent
    constructs), but pointing at a real one avoids relying on that.  Tries
    a handful of common clang binary names since the exact version varies
    by machine; returns None (not an error) if none are found -- callers
    just parse without `-resource-dir` in that case, and the
    `expected_function_names` completeness check in `build_context` is the
    real safety net if that ends up mattering for a given file.
    """
    for name in ("clang", "clang-18", "clang-17", "clang-16", "clang-15", "clang-14"):
        path = shutil.which(name)
        if not path:
            continue
        try:
            out = subprocess.run(
                [path, "-print-resource-dir"], capture_output=True, text=True, timeout=5
            )
        except OSError:
            continue
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return None


@dataclass
class AstContext:
    cindex: object  # the `clang.cindex` module itself, so callers never need their own import
    source_path: Path
    by_kind: dict  # CursorKind -> list[Cursor], main-file cursors only
    function_defs: dict  # name -> Cursor (FUNCTION_DECL, is_definition(), main-file only)


def build_context(source_path: Path, standard: str, expected_function_names: set[str]) -> AstContext:
    """Parse `source_path` and collect every cursor that belongs to it
    (not to an #included header) in one pass, bucketed by kind.

    `expected_function_names` is code_checks._find_all_function_defs's
    regex-based result for the same file -- already proven reliable (see
    AUTOGRADER_DESIGN.md §3.1.1). If the AST is missing a function
    definition the regex scan found, that's treated as an untrustworthy
    parse for this file (e.g. a resource-dir/include problem quietly
    dropped part of the program) and raises `AstUnavailable` rather than
    silently returning incomplete results -- the AST is allowed to find
    *more* than the regex scan (it's the more capable of the two), just
    never less.
    """
    import clang.cindex as cindex  # deferred: this is the only hard dependency on libclang being installed

    args = [f"-std={standard}"]
    resource_dir = _resource_dir()
    if resource_dir:
        args.append(f"-resource-dir={resource_dir}")
    tu = cindex.Index.create().parse(str(source_path), args=args)

    resolved = source_path.resolve()
    by_kind: dict = {}
    function_defs: dict = {}
    for cur in tu.cursor.walk_preorder():
        f = cur.location.file
        if f is None:
            continue
        if Path(f.name).resolve() != resolved:
            continue
        by_kind.setdefault(cur.kind, []).append(cur)
        if cur.kind == cindex.CursorKind.FUNCTION_DECL and cur.is_definition():
            function_defs.setdefault(cur.spelling, cur)

    missing = expected_function_names - set(function_defs)
    if missing:
        raise AstUnavailable(
            f"parse looks incomplete -- missing function definition(s) {sorted(missing)} "
            f"that a plain text scan already found in this file"
        )

    return AstContext(cindex=cindex, source_path=source_path, by_kind=by_kind, function_defs=function_defs)


# Constructs that are a single, unambiguous statement/expression kind --
# no operator-spelling or children-shape logic needed.
_SIMPLE_KINDS = {
    "switch": "SWITCH_STMT",
    "case": "CASE_STMT",
    "if": "IF_STMT",
    "for_loop": "FOR_STMT",
    "while_loop": "WHILE_STMT",
    "do_while": "DO_STMT",
    "ternary": "CONDITIONAL_OPERATOR",
}

# BINARY_OPERATOR/COMPOUND_ASSIGNMENT_OPERATOR share a cursor shape but not a
# kind -- both are checked for each, since the regex version's plain
# substring match (`%`, `^`, ...) also matches inside a compound-assignment
# spelling (`%=`, `^=`, ...). `&`/`|` are deliberately *not* handled via a
# generic "starts with" check the way the others safely could be: "&&"/"||"
# also start with "&"/"|", so each entry below is the exact, closed set of
# spellings that count -- not a prefix.
_BINARY_OP_SPELLINGS = {
    "modulo": {"%", "%="},
    "bitwise_xor": {"^", "^="},
    "bitwise_and": {"&", "&="},
    "bitwise_or": {"|", "|="},
    "bitwise_shift_left": {"<<", "<<="},
    "bitwise_shift_right": {">>", ">>="},
}

# `~` has no compound-assignment form in C and is always prefix (unlike
# `++`/`--`, the only postfix unary operators, which `_operator_spelling`
# below doesn't need to handle correctly since nothing here ever checks
# for them).
_UNARY_OP_SPELLINGS = {
    "bitwise_not": {"~"},
}


def _operator_spelling(cur, cindex) -> str | None:
    """The literal operator token of a BINARY_OPERATOR /
    COMPOUND_ASSIGNMENT_OPERATOR / (prefix) UNARY_OPERATOR cursor --
    clang.cindex doesn't expose this as a plain string property on these
    cursor kinds, so it's derived from tokens: the operator is whatever
    token(s) sit between the operand(s)' own token spans.
    """
    children = list(cur.get_children())
    toks = [t.spelling for t in cur.get_tokens()]
    if cur.kind == cindex.CursorKind.UNARY_OPERATOR:
        if len(children) != 1:
            return None
        operand_n = len(list(children[0].get_tokens()))
        if len(toks) <= operand_n:
            return None
        return "".join(toks[: len(toks) - operand_n])  # prefix assumption -- fine, `~` is always prefix
    if len(children) != 2:
        return None
    lhs, rhs = children
    lhs_n = len(list(lhs.get_tokens()))
    rhs_n = len(list(rhs.get_tokens()))
    op_toks = toks[lhs_n : len(toks) - rhs_n]
    return "".join(op_toks) if op_toks else None


def _function_def_used(ctx: AstContext) -> list[int]:
    """AST equivalent of code_checks._find_function_def_used: BFS from
    `main` over the real call graph (a CALL_EXPR's spelling, resolved
    against the same file's FUNCTION_DECL definitions) instead of a
    regex-approximated one. Immune to the regex version's one-level
    nested-call-argument limit and its `#define`-continuation blind spot;
    a call made *through* a function-pointer *variable* is still invisible
    to this too, same as the regex version -- see that function's
    docstring for why that's a data-flow problem parsing alone doesn't
    solve, not something this AST version fixes for free.
    """
    cindex = ctx.cindex
    defs = ctx.function_defs
    if "main" not in defs:
        return []
    call_kind = cindex.CursorKind.CALL_EXPR
    visited = {"main"}
    frontier = [defs["main"]]
    while frontier:
        cur = frontier.pop()
        for callee in cur.walk_preorder():
            if callee.kind != call_kind:
                continue
            name = callee.spelling
            if name in defs and name not in visited:
                visited.add(name)
                frontier.append(defs[name])
    return sorted(c.location.line for name, c in defs.items() if name != "main" and name in visited)


def detect(ctx: AstContext, name: str) -> list[int]:
    """AST equivalent of code_checks._detect. Raises AstUnavailable for a
    construct with no AST mapping here (currently just `regex:<pattern>`,
    which code_checks.py never even routes here -- see its own dispatch)."""
    cindex = ctx.cindex

    if name in _SIMPLE_KINDS:
        kind = getattr(cindex.CursorKind, _SIMPLE_KINDS[name])
        return sorted(c.location.line for c in ctx.by_kind.get(kind, []))

    if name == "else":
        lines = []
        for c in ctx.by_kind.get(cindex.CursorKind.IF_STMT, []):
            children = list(c.get_children())
            if len(children) == 3:  # cond, then, else -- 2 children means no else branch
                # The IF_STMT cursor's own location is the `if` keyword's line;
                # the else-branch child's location is where its own `{` (or
                # single statement) starts, right after the `else` keyword --
                # that's the line the regex version's `\belse\b` search finds.
                lines.append(children[2].location.line)
        return sorted(lines)

    if name == "if_else":
        return detect(ctx, "if") + detect(ctx, "else")

    if name in _BINARY_OP_SPELLINGS:
        targets = _BINARY_OP_SPELLINGS[name]
        lines = []
        for kind_name in ("BINARY_OPERATOR", "COMPOUND_ASSIGNMENT_OPERATOR"):
            for c in ctx.by_kind.get(getattr(cindex.CursorKind, kind_name), []):
                if _operator_spelling(c, cindex) in targets:
                    lines.append(c.location.line)
        return sorted(lines)

    if name in _UNARY_OP_SPELLINGS:
        targets = _UNARY_OP_SPELLINGS[name]
        return sorted(
            c.location.line
            for c in ctx.by_kind.get(cindex.CursorKind.UNARY_OPERATOR, [])
            if _operator_spelling(c, cindex) in targets
        )

    if name == "function_def_used":
        return _function_def_used(ctx)

    if name.startswith("function_call:"):
        fn_name = name.split(":", 1)[1]
        return sorted(
            c.location.line for c in ctx.by_kind.get(cindex.CursorKind.CALL_EXPR, []) if c.spelling == fn_name
        )

    raise AstUnavailable(f"no AST-based check implemented for construct '{name}'")
