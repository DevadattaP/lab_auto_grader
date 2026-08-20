"""Static checks on a student's C source: did they actually use (or avoid)
specific language constructs, as distinct from just matching expected output?

For beginner labs that exist to teach a specific construct ("use a switch
statement", "use the bitwise XOR operator instead of if-else"), matching
stdout alone lets a student route around the point of the exercise entirely.
This module answers a narrower question than a real C parser would: does a
named construct's *token pattern* appear in the code outside of comments and
string/char literals. That's deliberately not full parsing (no dependency on
a C frontend) -- it borrows the same idea to source what it can check.

The starting point for every check is `strip_comments_and_literals`, which
blanks out comment bodies and the *contents* of string/char literals while
preserving line structure -- without it, `printf("%d")` would falsely count
as "uses modulo" and `// use bitwise ops here` would falsely count as "uses
bitwise operators".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

VALID_CONSTRUCT_PREFIXES = ("function_call:", "regex:", "rec_function_call:")


class CodeCheckConfigError(Exception):
    """An unrecognized construct name was given in question.yaml."""


def strip_comments_and_literals(source: str) -> str:
    """Return `source` with // and /* */ comment bodies, and the contents of
    "..." and '...' literals, replaced by spaces -- same length, same line
    breaks, so line numbers and column positions of *real* code are
    unaffected. Escape sequences (\\", \\', \\\\) are honored so an escaped
    quote doesn't prematurely end a literal.
    """
    out = []
    i = 0
    n = len(source)
    while i < n:
        c = source[i]
        two = source[i : i + 2]

        if two == "//":
            out.append("  ")
            i += 2
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
            continue

        if two == "/*":
            out.append("  ")
            i += 2
            while i < n and source[i : i + 2] != "*/":
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append("  ")
                i += 2
            continue

        if c == '"' or c == "'":
            quote = c
            out.append(" ")
            i += 1
            while i < n and source[i] != quote:
                if source[i] == "\\" and i + 1 < n:
                    out.append("  " if source[i + 1] != "\n" else " \n")
                    i += 2
                    continue
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append(" ")
                i += 1
            continue

        out.append(c)
        i += 1

    return "".join(out)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _find_binary_op(cleaned: str, op: str, exclude_doubled: bool) -> list[int]:
    """Line numbers where `op` (a single-char operator: & or |) appears used
    as a *binary* operator -- i.e. immediately preceded (ignoring horizontal
    whitespace) by an identifier/number/')'/']', which a unary use (address-of
    `&x`, never applicable to `|`) would not be. This is a heuristic, not a
    parse: dense/compact student code (the common case at this level) matches
    reliably; unusual formatting can in principle evade it.
    """
    lines = []
    i = 0
    n = len(cleaned)
    while True:
        idx = cleaned.find(op, i)
        if idx == -1:
            break
        i = idx + 1
        if exclude_doubled and (
            (idx + 1 < n and cleaned[idx + 1] == op) or (idx > 0 and cleaned[idx - 1] == op)
        ):
            continue
        j = idx - 1
        while j >= 0 and cleaned[j] in " \t":
            j -= 1
        if j >= 0 and (cleaned[j].isalnum() or cleaned[j] in "_)]"):
            lines.append(_line_of(cleaned, idx))
    return lines


def _regex_lines(cleaned: str, pattern: re.Pattern) -> list[int]:
    return [_line_of(cleaned, m.start()) for m in pattern.finditer(cleaned)]


_BUILTIN_REGEXES: dict[str, re.Pattern] = {
    "switch": re.compile(r"\bswitch\s*\("),
    "case": re.compile(r"\bcase\b"),
    "if": re.compile(r"\bif\s*\("),
    "else": re.compile(r"\belse\b"),
    "for_loop": re.compile(r"\bfor\s*\("),
    "while_loop": re.compile(r"\bwhile\s*\("),
    "do_while": re.compile(r"\bdo\b"),
    "modulo": re.compile(r"%"),
    "bitwise_xor": re.compile(r"\^"),
    "bitwise_not": re.compile(r"~"),
    "bitwise_shift_left": re.compile(r"<<"),
    "bitwise_shift_right": re.compile(r">>"),
    "ternary": re.compile(r"\?"),
}

# Every built-in construct name (i.e. not a "function_call:"/"regex:" one) --
# the public list consumers outside this module (e.g. the UI's checkbox set)
# should use, rather than reaching into _BUILTIN_REGEXES directly.
KNOWN_BUILTIN_CONSTRUCTS = sorted(
    set(_BUILTIN_REGEXES)
    | {"if_else", "bitwise_and", "bitwise_or", "function_def_used", "rec_function_def_used", "array", "pointer"}
)

# Declaration-shaped: a type keyword, then a name, then [ ... ] before the
# declaration ends (`;` or `= {...}`) -- deliberately anchored to a type
# keyword at the start so a plain subscript use (`x[i] = 5;`) doesn't
# falsely count as declaring one. Same "declarations only, not parameters"
# scope as the AST version (_array_declared) -- a parameter list is always
# inside the enclosing `(...)`, which this pattern's lack of a `(` before
# the type keyword doesn't itself exclude, so callers only apply it to
# lines outside a function signature's parens (see _find_array_declared).
_ARRAY_DECL_RE = re.compile(
    r"\b(void|int|char|float|double|long|short|unsigned|signed|struct\s+\w+)\b[^;=(){}]*\w+\s*\[[^\]]*\]"
)

# A pointer declaration: type keyword, then a run of `*`/whitespace/name
# tokens containing at least one `*` before the declarator ends -- covers
# `int *p`, `char **argv`, `int* p`. Deliberately not used for `*p`
# (dereference) or `&x` (address-of) -- those are a *use*, not a
# declaration, and are handled separately by their own operator search
# below, mirroring bitwise_and/bitwise_or's binary-vs-unary split.
_POINTER_DECL_RE = re.compile(
    r"\b(void|int|char|float|double|long|short|unsigned|signed|struct\s+\w+)\b[^;=(){},]*\*"
)

# C keywords that share `keyword (...) {`'s shape with a real function
# definition (if/for/while/switch's bodies, none of which can legally be a
# student-defined function name since they're reserved words) -- must be
# excluded explicitly or `if (x) {` reads as "function named if, defined
# and used".
_CONTROL_KEYWORDS = {
    "if", "for", "while", "switch", "return", "sizeof", "do", "else",
    "goto", "case", "default", "typedef", "struct", "union", "enum",
}

# A signature `name(...)  {` -- a definition, never a call (nothing in
# student source supplies a body for a library function like printf).
# `[^;{}()]*` for the parameter list deliberately doesn't allow nested
# parens, so a function-pointer parameter (`void f(int (*cmp)(int,int))`)
# is a known miss, same spirit as `bitwise_and`'s documented heuristic gap.
_FUNC_SIG_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(([^;{}()]*)\)\s*\{")

# A call site's arguments are values/expressions; a prototype declaration's
# parameter list starts with a type keyword (`int add(int a, int b);`) --
# this is what tells `add(2, 3);` (call) apart from `add(int a, int b);`
# (declaration, not a use) when both end in `;`.
_PARAM_TYPE_LEAD = re.compile(r"^\s*(void|int|char|float|double|long|short|unsigned|signed|struct|const|_Bool)\b")


def _matching_brace(cleaned: str, open_pos: int) -> int:
    """Index of the `}` matching the `{` at `open_pos`. Exact, not a
    heuristic: `cleaned` has already had comments and string/char literal
    *contents* blanked out, which removes the only things in C that could
    fake a brace (a `'{'` char literal, a `"}"` string) -- so plain
    depth-counting on the cleaned text can't be fooled."""
    depth = 0
    i = open_pos
    n = len(cleaned)
    while i < n:
        if cleaned[i] == "{":
            depth += 1
        elif cleaned[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n  # unterminated -- shouldn't happen for source that compiled


def _find_all_function_defs(cleaned: str) -> dict[str, tuple[int, int, int]]:
    """name -> (definition_line, body_start, body_end) for every top-level
    function definition, `main` included (it's needed as the reachability
    root below, even though it's never itself a `function_def_used` match)."""
    directive_lines = {
        i + 1 for i, line in enumerate(cleaned.split("\n")) if line.lstrip().startswith("#")
    }
    defs: dict[str, tuple[int, int, int]] = {}
    for m in _FUNC_SIG_RE.finditer(cleaned):
        name = m.group(1)
        line = _line_of(cleaned, m.start())
        if name in _CONTROL_KEYWORDS or line in directive_lines:
            continue
        body_start = m.end(0) - 1  # position of the '{'
        body_end = _matching_brace(cleaned, body_start)
        defs.setdefault(name, (line, body_start, body_end))
    return defs


def _calls_in_span(cleaned: str, start: int, end: int, candidates: set[str]) -> set[str]:
    """Which of `candidates` (other known user-defined function names) are
    called somewhere in cleaned[start:end] -- i.e. this span's outgoing call
    edges. Reuses the same call-vs-declaration disambiguation as before
    (arguments that don't look like a parameter-type list)."""
    called = set()
    for name in candidates:
        call_re = re.compile(rf"\b{re.escape(name)}\s*\(((?:[^()]|\([^()]*\))*)\)\s*([;{{]?)")
        for m in call_re.finditer(cleaned, start, end):
            if m.group(2) == "{":
                continue  # a nested/other definition, not a call
            if _PARAM_TYPE_LEAD.match(m.group(1)):
                continue  # looks like a declaration's parameter-type list, not call args
            called.add(name)
            break
    return called


def _reachable_from_main(cleaned: str, defs: dict[str, tuple[int, int, int]]) -> set[str]:
    """BFS over the caller->callee graph built from each function's body
    span, starting at `main`. A function only counts as reachable if some
    chain of *actual calls* connects it back to `main` -- `main` calls `A`
    calls `B` is reachable no matter how deep, but a function that calls
    only itself (or is only called by another function that is itself
    never reached from `main`) is correctly excluded, unlike a flat
    "does this name appear as a call anywhere in the file" search."""
    if "main" not in defs:
        return set()
    visited = {"main"}
    frontier = ["main"]
    while frontier:
        node = frontier.pop()
        _, body_start, body_end = defs[node]
        candidates = set(defs) - {node}
        for callee in _calls_in_span(cleaned, body_start, body_end, candidates):
            if callee not in visited:
                visited.add(callee)
                frontier.append(callee)
    return visited


def _is_recursive_call(cleaned: str, func_name: str, body_start: int, body_end: int) -> bool:
    """Check if a function calls itself (directly recursive). Returns True if
    `func_name` appears as a call within its own body span."""
    call_re = re.compile(rf"\b{re.escape(func_name)}\s*\(((?:[^()]|\([^()]*\))*)\)\s*([;{{]?)")
    for m in call_re.finditer(cleaned, body_start, body_end):
        if m.group(2) == "{":
            continue  # a nested/other definition, not a call
        if _PARAM_TYPE_LEAD.match(m.group(1)):
            continue  # looks like a declaration's parameter-type list, not call args
        return True
    return False


def _find_recursive_functions(cleaned: str) -> list[int]:
    """Line numbers of the *definition* of each function that calls itself
    (directly recursive) AND is reachable from `main` through a chain of real
    calls, similar to `function_def_used`. A recursive function that's never
    invoked from main is correctly excluded, since it's not actually used."""
    defs = _find_all_function_defs(cleaned)
    reachable = _reachable_from_main(cleaned, defs)
    found = []
    for name, (line, body_start, body_end) in defs.items():
        if name != "main" and name in reachable and _is_recursive_call(cleaned, name, body_start, body_end):
            found.append(line)
    return sorted(found)


def _find_recursive_function_by_name(cleaned: str, target_name: str) -> list[int]:
    """Line numbers of calls to `target_name` within the definition of
    `target_name` itself (i.e., recursive calls to the named function).
    Returns empty list if the function doesn't exist or doesn't call itself."""
    defs = _find_all_function_defs(cleaned)
    if target_name not in defs:
        return []
    line, body_start, body_end = defs[target_name]
    call_re = re.compile(rf"\b{re.escape(target_name)}\s*\(((?:[^()]|\([^()]*\))*)\)\s*([;{{]?)")
    lines = []
    for m in call_re.finditer(cleaned, body_start, body_end):
        if m.group(2) == "{":
            continue  # a nested/other definition, not a call
        if _PARAM_TYPE_LEAD.match(m.group(1)):
            continue  # looks like a declaration's parameter-type list, not call args
        lines.append(_line_of(cleaned, m.start()))
    return sorted(lines)


def _find_function_def_used(cleaned: str) -> list[int]:
    """Line numbers of the *definition* of each student-defined function
    (any name, excluding `main` and C keywords) that is reachable from
    `main` through a chain of real calls -- deliberately name-agnostic,
    unlike `function_call:<name>`, for a question that leaves the
    function's name up to the student ("use a function you define
    yourself").

    A definition is `name(...) {`: a signature immediately followed by a
    body, which a declaration (`;`) or a call to a library function (never
    followed by a body in student source) never has. "Used" means reachable
    from `main` via `_reachable_from_main` -- not just "called somewhere in
    the file", which would (and previously did) wrongly credit a function
    that only calls itself, or a chain of functions that call each other
    but that `main` never actually invokes.

    Known heuristic gaps (documented, not solved here -- see
    AUTOGRADER_DESIGN.md's discussion of a future libclang-based layer):
    a call made through a function pointer (`op = add; op(x, y);`) creates
    no textual `add(` at the call site, so it's invisible to this graph --
    though that's a data-flow problem a plain AST doesn't solve either, not
    something libclang fixes for free; a `#define NAME(x) { ... }` macro
    body spanning multiple lines via `\\` continuation could be mistaken
    for a definition (single-line macros are guarded against, see
    `directive_lines` in `_find_all_function_defs`); nested calls as
    arguments (`add(square(2), 3)`) are only resolved one paren-level deep.
    """
    defs = _find_all_function_defs(cleaned)
    reachable = _reachable_from_main(cleaned, defs)
    found = [line for name, (line, _, _) in defs.items() if name != "main" and name in reachable]
    return sorted(found)


def _strip_parenthesized(cleaned: str) -> str:
    """`cleaned` with the contents of every (...) span blanked to spaces --
    same length, same line breaks. Used so a function's parameter list
    (always inside its signature's parens) can never match the array/
    pointer *declaration* patterns below, keeping "declarations only, not
    parameters" true for the regex fallback the same way the AST version
    achieves it by construction (VAR_DECL/PARM_DECL are distinct cursor
    kinds there)."""
    out = list(cleaned)
    depth = 0
    for i, c in enumerate(cleaned):
        if c == "(":
            depth += 1
            continue
        if c == ")":
            depth = max(0, depth - 1)
            continue
        if depth > 0 and c != "\n":
            out[i] = " "
    return "".join(out)


def _find_array_declared(cleaned: str) -> list[int]:
    no_params = _strip_parenthesized(cleaned)
    return _regex_lines(no_params, _ARRAY_DECL_RE)


# scanf("%d", &n) is the near-universal idiom for reading input in
# beginner C -- &n there is required syntax, not a deliberate choice to
# "use a pointer" the way int *p = &n; is, so &-args of these calls don't
# count toward the `pointer` construct (see the AST version's
# _scanf_address_of_args for the same exclusion done properly with cursors;
# this is the regex-fallback approximation of it).
_SCANF_CALL_RE = re.compile(r"\b(?:scanf|fscanf|sscanf)\s*\(((?:[^()]|\([^()]*\))*)\)")
_ADDRESS_OF_ARG_RE = re.compile(r"&\s*[A-Za-z_]\w*")


def _blank_scanf_address_args(cleaned: str) -> str:
    """`cleaned` with every direct `&name` argument inside a scanf/fscanf/
    sscanf(...) call replaced by spaces (same length) -- so `_find_unary_op`
    never sees them as an address-of use. Only touches the call's own
    argument text, not anything nested deeper (mirrors the AST version's
    "direct argument only" scope)."""
    out = list(cleaned)
    for call in _SCANF_CALL_RE.finditer(cleaned):
        start, end = call.span(1)
        args_text = cleaned[start:end]
        for m in _ADDRESS_OF_ARG_RE.finditer(args_text):
            for i in range(start + m.start(), start + m.end()):
                out[i] = " " if cleaned[i] != "\n" else "\n"
    return "".join(out)


def _find_pointer(cleaned: str) -> list[int]:
    no_params = _strip_parenthesized(cleaned)
    decl_lines = _regex_lines(no_params, _POINTER_DECL_RE)
    no_scanf_addrs = _blank_scanf_address_args(cleaned)
    deref_lines = _find_unary_op(no_scanf_addrs, "*") + _find_unary_op(no_scanf_addrs, "&")
    return sorted(set(decl_lines) | set(deref_lines))


def _find_unary_op(cleaned: str, op: str) -> list[int]:
    """Line numbers where `op` (`*` or `&`) appears used as a *prefix unary*
    operator -- i.e. NOT immediately preceded (ignoring horizontal
    whitespace) by an identifier/number/')'/']', which is exactly the
    inverse of `_find_binary_op`'s condition. Catches dereference (`*p`,
    `*(p+1)`) and address-of (`&x`); a `*` used for multiplication or a `&`
    used for bitwise-and is excluded the same way `_find_binary_op` already
    identifies those as binary."""
    lines = []
    i = 0
    n = len(cleaned)
    while True:
        idx = cleaned.find(op, i)
        if idx == -1:
            break
        i = idx + 1
        if (idx + 1 < n and cleaned[idx + 1] == op) or (idx > 0 and cleaned[idx - 1] == op):
            continue  # ** or && / || -- not a plain unary use
        j = idx - 1
        while j >= 0 and cleaned[j] in " \t":
            j -= 1
        if j >= 0 and (cleaned[j].isalnum() or cleaned[j] in "_)]"):
            continue  # binary use (a*b, a&b), not unary
        lines.append(_line_of(cleaned, idx))
    return lines


def _detect(cleaned: str, name: str) -> list[int]:
    """Line numbers where construct `name` is found in already-cleaned source."""
    if name == "if_else":
        return _regex_lines(cleaned, _BUILTIN_REGEXES["if"]) + _regex_lines(cleaned, _BUILTIN_REGEXES["else"])
    if name == "bitwise_and":
        return _find_binary_op(cleaned, "&", exclude_doubled=True)
    if name == "bitwise_or":
        return _find_binary_op(cleaned, "|", exclude_doubled=True)
    if name == "function_def_used":
        return _find_function_def_used(cleaned)
    if name == "rec_function_def_used":
        return _find_recursive_functions(cleaned)
    if name == "array":
        return _find_array_declared(cleaned)
    if name == "pointer":
        return _find_pointer(cleaned)
    if name in _BUILTIN_REGEXES:
        return _regex_lines(cleaned, _BUILTIN_REGEXES[name])
    if name.startswith("function_call:"):
        fn = re.escape(name.split(":", 1)[1])
        # Excludes a match immediately followed by `{` -- otherwise a
        # student's own `int toupper(int c) { ... }` (defining, not calling,
        # a function that happens to share a forbidden library name) would
        # wrongly count as a "call" to it, purely from the definition's own
        # signature. Found by comparing this against the AST-based version
        # (code_checks_ast.py), which only ever sees a real CALL_EXPR here.
        call_re = re.compile(rf"\b{fn}\s*\(((?:[^()]|\([^()]*\))*)\)\s*([;{{]?)")
        return [_line_of(cleaned, m.start()) for m in call_re.finditer(cleaned) if m.group(2) != "{"]
    if name.startswith("rec_function_call:"):
        fn = name.split(":", 1)[1]
        return _find_recursive_function_by_name(cleaned, fn)
    if name.startswith("regex:"):
        pattern = name.split(":", 1)[1]
        return _regex_lines(cleaned, re.compile(pattern))
    raise CodeCheckConfigError(f"unknown construct '{name}'")


def validate_construct_name(name: str) -> None:
    """Raise CodeCheckConfigError at question-load time for a typo'd or
    unsupported construct name, rather than silently never matching it during
    grading."""
    if name.startswith(VALID_CONSTRUCT_PREFIXES):
        if name.startswith("regex:"):
            try:
                re.compile(name.split(":", 1)[1])
            except re.error as e:
                raise CodeCheckConfigError(f"invalid regex in construct '{name}': {e}")
        return
    if name in KNOWN_BUILTIN_CONSTRUCTS:
        return
    raise CodeCheckConfigError(
        f"unknown construct '{name}'. Known built-ins: {KNOWN_BUILTIN_CONSTRUCTS}, "
        f"or use 'function_call:<name>' / 'rec_function_call:<name>' / 'regex:<pattern>'"
    )


@dataclass
class CodeCheckResult:
    satisfied: bool
    missing_required: list[str] = field(default_factory=list)
    found_forbidden: list[tuple[str, int]] = field(default_factory=list)  # (name, line)
    found_required: list[tuple[str, int]] = field(default_factory=list)   # (name, first line)
    fallback_notes: list[str] = field(default_factory=list)  # non-empty only when the libclang path gave way to regex for something -- see run_code_checks


def _build_ast_context(source_path: Path, cleaned: str, compile_standard: str):
    """Attempt to get an AST context for this file (see code_checks_ast.py).
    Never raises -- any failure (libclang not installed, parse failed, or
    the parse looked incomplete against `_find_all_function_defs`'s
    regex-based baseline) is reported back as a one-line note instead, and
    the caller falls back to the regex path for every construct in this
    file."""
    from grader import code_checks_ast

    expected_names = set(_find_all_function_defs(cleaned))
    try:
        return code_checks_ast.build_context(source_path, compile_standard, expected_names), []
    except Exception as e:
        return None, [f"libclang check failed ({e}) -- falling back to regex-based checks for this submission"]


def _detect_with_fallback(
    ast_ctx, cleaned: str, name: str, fallback_notes: list[str]
) -> list[int]:
    """Try the AST-based check first (when an AST context is available and
    the construct isn't the `regex:` escape hatch, which has no AST
    equivalent by design -- it's a raw pattern, not a named language
    construct). Any failure specific to this one construct (not the whole
    file) falls back to the regex check for just that construct, noting
    why in `fallback_notes` for the report."""
    if ast_ctx is not None and not name.startswith("regex:"):
        from grader import code_checks_ast

        try:
            return code_checks_ast.detect(ast_ctx, name)
        except Exception as e:
            fallback_notes.append(
                f"libclang check for '{name}' failed ({e}) -- falling back to regex-based check"
            )
    return _detect(cleaned, name)


def run_code_checks(
    source_path: Path,
    require: list[str],
    forbid: list[str],
    require_match: str = "any",
    compile_standard: str = "c11",
) -> CodeCheckResult:
    """require_match controls how `require` is satisfied:
      - "any" (default): at least one of the listed constructs must appear.
      - "all": every listed construct must appear (the only behavior before
        this option existed -- a question relying on that, e.g. one that
        needs both a bitwise AND *and* a bitwise XOR for two independent
        parts of the problem, must now set `require_match: all` explicitly).
    `forbid` is unconditionally "any" -- a single forbidden construct found
    anywhere is always a violation, there's no matching mode for it.

    Every construct is checked via a real AST (`code_checks_ast.py`, using
    libclang) when possible, falling back to the regex-based implementation
    above per-construct (or for the whole file, if even parsing it failed)
    on any failure -- never a hard error, since a beginner's C file is
    exactly the kind of source most likely to trip up a stricter parser in
    some edge case the regex heuristics happen to tolerate. Every fallback
    that occurs is recorded in the result and shown in the student's report
    (see report._render_code_check), not silently absorbed.
    """
    source = source_path.read_text(errors="replace")
    cleaned = strip_comments_and_literals(source)

    ast_ctx, fallback_notes = _build_ast_context(source_path, cleaned, compile_standard)

    missing_required = []
    found_required = []
    for name in require:
        lines = _detect_with_fallback(ast_ctx, cleaned, name, fallback_notes)
        if lines:
            found_required.append((name, lines[0]))
        else:
            missing_required.append(name)

    found_forbidden = []
    for name in forbid:
        lines = _detect_with_fallback(ast_ctx, cleaned, name, fallback_notes)
        if lines:
            found_forbidden.append((name, lines[0]))

    if require_match == "all":
        require_satisfied = not missing_required
    else:
        require_satisfied = bool(found_required) or not require

    satisfied = require_satisfied and not found_forbidden
    return CodeCheckResult(
        satisfied=satisfied,
        missing_required=missing_required,
        found_forbidden=found_forbidden,
        found_required=found_required,
        fallback_notes=fallback_notes,
    )
