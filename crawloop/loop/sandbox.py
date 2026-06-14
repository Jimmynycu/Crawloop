"""Run an LLM-generated candidate crawler in an isolated subprocess (Task 9.3).

This is the SECURITY-CRITICAL boundary where generated Python is actually
executed. The defense is layered:

1. **Static gate, before anything else.** :func:`run_in_sandbox` calls
   :func:`crawloop.safety.ast_check` with ``raise_on_violation=True`` FIRST.
   A source that trips the gate raises :class:`~crawloop.safety.ASTViolation`
   and **no subprocess is ever spawned** — the gate is the first line, not a
   formality run after we've already handed the code to an interpreter.
2. **Process isolation.** Gate-passing source is run in a *child* process
   (``sys.executable`` running :mod:`crawloop.loop._runner`), so it cannot
   touch the parent's memory, and we can kill it.
3. **Wall-clock timeout.** ``subprocess.run(..., timeout=...)`` bounds runtime; on
   expiry it terminates the child and we raise :class:`SandboxTimeout`. A runaway
   candidate (e.g. ``while True``) cannot hang the parent.
4. **Offline by construction.** The child builds a context whose ``fetch*`` only
   ever returns the one stored HTML (see :mod:`_runner`) — there is no network in
   the sandbox at all.
5. **Best-effort memory cap.** On Linux a ``RLIMIT_AS`` cap is installed in the
   child via ``preexec_fn``; elsewhere it is a deliberate no-op (a tight address-
   space cap can break interpreter startup on macOS, and the timeout is the
   primary control regardless).
6. **Scrubbed environment.** The child is spawned with a minimal ``env`` (see
   :func:`_child_env`) instead of inheriting the parent's — so secrets in the
   parent's environment (``ANTHROPIC_API_KEY``, ``EXAMPLE_WAF_TOKEN``, any creds)
   are never handed to candidate code. The AST gate already blocks ``os``/env
   access, so this is defense in depth, not the primary control.

The parent communicates with the child purely over stdio (JSON in, JSON out), so
it never shares an object with untrusted code and never blocks on a half-read
pipe — ``subprocess.run`` owns draining and killing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from crawloop.safety import ast_check

# The child script. Located relative to THIS file so it is found regardless of
# cwd or how the package was installed.
_RUNNER = Path(__file__).parent / "_runner.py"

# Best-effort child address-space cap (bytes). Applied only on Linux (see module
# docstring); 512 MiB is plenty for parsel over a single page and well under what
# would let a candidate exhaust the host.
_MEM_LIMIT_BYTES = 512 * 1024 * 1024

# How many characters of the child's stderr to attach to a SandboxError. Bounded
# so a noisy traceback can't blow up the exception message / logs.
_STDERR_TAIL = 2000


class SandboxTimeout(Exception):
    """Raised when the candidate did not finish within the wall-clock ``timeout``;
    the child has been terminated. Distinct from :class:`SandboxError` so the
    caller can tell "too slow / looping" from "ran and failed"."""


class SandboxError(Exception):
    """Raised when the candidate ran but did not produce a usable result — it
    raised inside ``crawl``, defined no crawler class, emitted unparseable
    output, or exited non-zero. Carries the child's error text / stderr tail."""


def _limit_child_memory() -> None:  # pragma: no cover - runs in the forked child
    """``preexec_fn``: cap the child's address space on Linux; no-op elsewhere.

    Wrapped in a broad try/except and gated to Linux because a tight
    ``RLIMIT_AS`` can prevent CPython from even starting on some platforms
    (notably macOS, which reserves large virtual mappings). Best-effort by
    design — the wall-clock timeout is the primary containment.
    """
    if not sys.platform.startswith("linux"):
        return
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (_MEM_LIMIT_BYTES, _MEM_LIMIT_BYTES))
    except Exception:
        # Never let a limit-setting failure abort the child spawn.
        return


def _child_env() -> dict[str, str]:
    """The minimal environment handed to the candidate subprocess.

    Deliberately does NOT inherit the parent's environment: secrets the parent
    holds (``ANTHROPIC_API_KEY``, ``EXAMPLE_WAF_TOKEN``, arbitrary creds) must never
    reach candidate code. Only ``PATH`` is carried — enough for the interpreter
    to start.

    Notably this does NOT need ``PYTHONPATH``/``VIRTUAL_ENV`` to let the child
    ``import crawloop``: the package is installed editable via a ``.pth``
    finder in the interpreter's own ``site-packages``, which is resolved at
    interpreter level (from ``sys.executable``), not from the environment — so a
    PATH-only env still imports it cleanly. If a future packaging change moved the
    package onto an env-provided ``PYTHONPATH``, this is the one place to add the
    minimum needed.
    """
    return {"PATH": os.environ.get("PATH", "")}


def run_in_sandbox(
    source: str,
    html: str,
    *,
    url: str = "https://sandbox.local/",
    timeout: float = 30.0,
) -> list[dict]:
    """Execute candidate ``source`` against stored ``html`` in an isolated child.

    The AST gate runs FIRST: an :class:`~crawloop.safety.ASTViolation`
    propagates and no subprocess is spawned. Otherwise the source + html + url are
    sent as JSON to the runner child, which extracts the records offline and
    prints them back. Returns the extracted ``items`` on success.

    Raises:
        ASTViolation: the source failed the static gate (never executed).
        SandboxTimeout: the child exceeded ``timeout`` and was killed.
        SandboxError: the child ran but failed (exception in ``crawl``, no
            crawler class, unparseable output, or non-zero exit).
    """
    # 1) Static gate FIRST — gate violations never reach a subprocess.
    ast_check(source, raise_on_violation=True)

    payload = json.dumps({"source": source, "html": html, "url": url})

    # preexec_fn is POSIX-only; on Windows it is unsupported, so omit it there.
    preexec = _limit_child_memory if sys.platform != "win32" else None

    # 2) Run in a child with a hard wall-clock timeout and a SCRUBBED env (so the
    #    candidate never inherits the parent's secrets). capture_output drains both
    #    pipes; on timeout subprocess.run terminates the child and waits, so the
    #    parent never hangs on a runaway candidate or an unread pipe.
    try:
        proc = subprocess.run(
            [sys.executable, str(_RUNNER)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout,
            preexec_fn=preexec,
            env=_child_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxTimeout(
            f"candidate exceeded {timeout}s wall-clock limit and was terminated"
        ) from exc

    return _parse_result(proc)


def _parse_result(proc: subprocess.CompletedProcess[str]) -> list[dict]:
    """Turn the child's stdout/stderr/returncode into items or a SandboxError.

    Success is signalled by a parseable ``{"ok": true, "items": [...]}`` on
    stdout. Anything else — ``ok: false``, unparseable stdout, or a non-zero exit
    — is a :class:`SandboxError` carrying the child's error text and a bounded
    tail of stderr for debugging.
    """
    stderr_tail = (proc.stderr or "")[-_STDERR_TAIL:].strip()

    try:
        result = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        raise SandboxError(
            f"candidate produced no parseable result "
            f"(exit={proc.returncode}); stderr: {stderr_tail or '<empty>'}"
        ) from None

    if result.get("ok"):
        return list(result.get("items", []))

    error = result.get("error", "<no error reported>")
    detail = f"{error}" + (f"; stderr: {stderr_tail}" if stderr_tail else "")
    raise SandboxError(f"candidate failed: {detail}")
