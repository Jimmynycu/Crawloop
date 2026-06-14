"""AST allowlist gate for LLM-generated crawler code — the trust boundary.

This is the only thing standing between LLM-generated source and the host. It is
run at crawler REGISTRATION (the registry's ``add_version`` AST-checks before it
ever writes code to disk, M5) and again at every module LOAD (the loader
re-checks the on-disk file before importing it, so a file tampered with after
registration is still caught, M5). Both call sites use ``raise_on_violation=True``.

The gate is deliberately conservative: it allows a tiny fixed set of imports and
rejects anything not clearly safe. It is a *static* defense — it does not execute
the code — so it is paired with subprocess sandboxing at runtime (M8). Treat any
change that loosens these rules as security-sensitive.
"""

from __future__ import annotations

import ast
import re

# Dotted module names a generated crawler may import. A module ``M`` is allowed
# iff it equals one of these or is a strict sub-module of one (``M.startswith(a + ".")``).
# Note ``crawloop.contract`` is allowed but ``crawloop`` /
# ``crawloop.registry`` / ``crawloop.safety`` are NOT — generated code may
# only touch the public contract, never the loop's internals.
# Only ``urllib.parse`` is allowed (URL helpers like ``urljoin``/``quote``); bare
# ``urllib`` is deliberately EXCLUDED so ``urllib.request`` cannot be reached for
# out-of-band network egress that would bypass FetchContext, the domain allowlist,
# and rate limiting. Re-adding "urllib" would reopen that hole.
ALLOWED_IMPORTS = {
    "parsel",
    "re",
    "json",
    "datetime",
    "decimal",
    "urllib.parse",
    "crawloop.contract",
}

# Builtins that grant code-execution, attribute reflection, or scope access.
# Any ``Call`` whose func is a bare ``Name`` with one of these ids is rejected.
BANNED_CALL_NAMES = {
    "eval",
    "exec",
    "compile",
    "open",
    "__import__",
    "getattr",
    "setattr",
    "delattr",
    "globals",
    "locals",
    "vars",
    "breakpoint",
    "input",
    "memoryview",
    "__build_class__",
}

# Bare references to these names are rejected even when they are NOT directly
# called, because the reference alone hands the caller a dangerous capability:
# it can be aliased (``e = eval; e(src)``), bound via walrus, used as a
# decorator, or — for ``__builtins__`` — indexed to reach *any* builtin
# (``__builtins__["eval"]``). This is a strict superset of BANNED_CALL_NAMES so
# the call-site rule is subsumed, but we keep both for clear, specific messages.
# ``__builtins__`` is the whole builtins namespace; a generated crawler never
# needs it. (NB: ``object``/``type`` are intentionally NOT here — they are
# ordinary and only become dangerous via dunder attributes, which the
# attribute rule already blocks.)
BANNED_BARE_NAMES = BANNED_CALL_NAMES | {"__builtins__"}

# Attribute names that grant a runtime attribute-traversal channel the static AST
# cannot see. ``str.format``/``format_map`` interpret a format mini-language at
# runtime, so ``"{0.__class__.__bases__}".format(x)`` reaches arbitrary attributes
# (and ``__globals__`` -> secrets / ``__builtins__``) from a plain string literal
# the gate only sees as a Constant. Generated crawlers use f-strings instead.
BANNED_ATTRS = {"format", "format_map"}

_DUNDER_RE = re.compile(r"^__.*__$")


class ASTViolation(Exception):
    """Raised when gated source contains a construct outside the allowlist."""


def _module_allowed(module: str) -> bool:
    return any(module == a or module.startswith(a + ".") for a in ALLOWED_IMPORTS)


class _Gate(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[str] = []

    def _flag(self, msg: str, node: ast.AST) -> None:
        line = getattr(node, "lineno", "?")
        self.violations.append(f"{msg} (line {line})")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if not _module_allowed(alias.name):
                self._flag(f"disallowed import: {alias.name!r}", node)
        # Do not descend; an Import has no child statements worth visiting.

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # Relative imports (``from . import x``, ``from ..pkg import y``) are always
        # rejected: their resolved module name is ambiguous at static-check time.
        if node.level and node.level > 0:
            self._flag("relative import is not allowed", node)
            return
        module = node.module or ""
        if not _module_allowed(module):
            self._flag(f"disallowed import-from: {module!r}", node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id in BANNED_CALL_NAMES:
            self._flag(f"banned call: {func.id!r}", node)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if _DUNDER_RE.match(node.attr):
            self._flag(f"dunder attribute access: {node.attr!r}", node)
        elif node.attr in BANNED_ATTRS:
            self._flag(f"banned attribute: {node.attr!r}", node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        # Reject a bare reference to any dangerous builtin, not only when it is
        # called: aliasing/walrus/decorator use (``e = eval``, ``@eval``) and
        # ``__builtins__[...]`` indexing all reach the capability without a
        # direct ``Call`` on the name. A call site additionally trips visit_Call
        # for a more specific message, which is fine.
        #
        # ALSO reject any bare dunder name. Module globals like ``__loader__`` /
        # ``__spec__`` are the live import machinery at module-load time:
        # ``__loader__.source_to_code(b"import os", "<x>")`` compiles arbitrary
        # bytes and ``FunctionType(code, {})()`` runs them in-process (the empty
        # globals get a real ``__builtins__`` auto-injected), and
        # ``__loader__.get_data(path)`` reads arbitrary files. A dunder name is
        # never legitimate in a generated crawler, so reject the whole class.
        if node.id in BANNED_BARE_NAMES or _DUNDER_RE.match(node.id):
            self._flag(f"reference to banned name: {node.id!r}", node)
        self.generic_visit(node)


def ast_check(source: str, *, raise_on_violation: bool = False) -> list[str]:
    """Return a list of human-readable violation strings for ``source``.

    An empty list means the source is within the allowlist. A ``SyntaxError``
    (unparseable source) is itself treated as a single violation rather than
    being allowed to propagate. When ``raise_on_violation`` is true and there is
    at least one violation, raise :class:`ASTViolation` with all of them joined.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        violations = [f"syntax error: {exc}"]
    else:
        gate = _Gate()
        gate.visit(tree)
        violations = gate.violations

    if raise_on_violation and violations:
        raise ASTViolation("; ".join(violations))
    return violations
