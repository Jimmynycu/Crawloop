"""The command-line interface (Task 11.1): a thin operator front-end.

``crawloop`` (entry point ``crawloop.cli:main``) is glue, nothing more —
every subcommand is a few lines that build the shared infrastructure
(:class:`~crawloop.config.AppConfig`, :class:`~crawloop.registry.Registry`,
an httpx client, a browser runner, a :class:`~crawloop.llm.Completer`) and
hand off to the engine or the registry. The heavy logic all lives behind those;
the CLI parses argv, dispatches, and prints. The commands:

* ``crawl <url> [--schema NAME@VER] [--json]`` — serve one URL through the
  :class:`~crawloop.engine.Engine` (the full §8 flow) and print the
  :class:`~crawloop.engine.RequestResult`.
* ``family list`` / ``family show <family>`` — read the registry's families and
  one family's version ladder + status.
* ``loop run <family> <seed_url...>`` — run the regeneration Loop for a family.
* ``access status`` — print the persistent per-domain access store.
* ``audit [family]`` — print the audit trail (optionally one family's).

LLM/browser policy: only the commands that actually crawl/regenerate (``crawl``,
``loop run``) need a real model + browser, and those are constructed lazily and
ONLY when ``--offline`` is not set (and an API key is present). The read-only
commands (``family``/``access``/``audit``) never build either. ``--offline``
forces the scripted-but-empty :class:`~crawloop.llm.FakeCompleter`, so a
``crawl``/``loop`` invocation can be exercised without a key.

``main`` returns a process exit code (0 on success, non-zero on a usage error or
a failed command) and never lets ``SystemExit`` from argparse escape — a bad
invocation becomes a clean non-zero return with argparse's usage message.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
from collections.abc import Sequence
from pathlib import Path

from crawloop.access import build_http_client
from crawloop.config import AppConfig, load_config
from crawloop.engine import Engine
from crawloop.llm import Completer, FakeCompleter, LiteLLMCompleter
from crawloop.loop.driver import LoopResult, run_loop
from crawloop.registry import Registry

# Defaults used when a flag is omitted, so the common case is just
# ``crawloop crawl <url>``. The config is the project allowlist; the db and
# dirs live under a local ``.crawloop`` working directory.
_DEFAULT_CONFIG = "authorized_domains.yaml"
_DEFAULT_DB = ".crawloop/registry.db"
_DEFAULT_CRAWLERS_DIR = ".crawloop/crawlers"
_DEFAULT_FIXTURES_DIR = ".crawloop/fixtures"


def build_parser() -> argparse.ArgumentParser:
    """The argparse parser for the whole CLI (one place, so help stays in sync).

    Global options (``--config``/``--db``/``--crawlers-dir``/``--fixtures-dir``/
    ``--offline``) live on the top parser so they apply to every subcommand;
    each subcommand adds only its own positional/flags. ``required=True`` on the
    subparsers makes "no subcommand" an argparse usage error (a clean non-zero),
    and the same for the nested ``family``/``loop``/``access`` groups.
    """
    parser = argparse.ArgumentParser(
        prog="crawloop",
        description="Self-healing structured-data crawler (POC).",
    )
    parser.add_argument(
        "--config", default=_DEFAULT_CONFIG, help="path to authorized_domains.yaml"
    )
    parser.add_argument("--db", default=_DEFAULT_DB, help="path to the registry SQLite db")
    parser.add_argument(
        "--crawlers-dir", default=_DEFAULT_CRAWLERS_DIR, help="dir for crawler code files"
    )
    parser.add_argument(
        "--fixtures-dir", default=_DEFAULT_FIXTURES_DIR, help="dir for golden fixtures"
    )

    # --offline is defined ONCE here and inherited (via parents=) by exactly the
    # subcommands that build a model/browser (crawl, loop run), so it can be
    # written naturally after the subcommand — e.g. ``crawl <url> --offline`` —
    # while staying a single definition. The read-only commands neither need nor
    # accept it.
    offline_parent = argparse.ArgumentParser(add_help=False)
    offline_parent.add_argument(
        "--offline",
        action="store_true",
        help="never construct a real model/browser (no API key needed)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_crawl = sub.add_parser(
        "crawl", parents=[offline_parent], help="serve one URL through the engine"
    )
    p_crawl.add_argument("url", help="the URL to crawl")
    p_crawl.add_argument(
        "--schema", default=None, help="target schema (e.g. Product@1) for a new family"
    )
    p_crawl.add_argument("--json", action="store_true", help="print the result as JSON")

    p_family = sub.add_parser("family", help="inspect registered families")
    family_sub = p_family.add_subparsers(dest="family_command", required=True)
    p_family_list = family_sub.add_parser("list", help="list all families")
    p_family_list.add_argument("--json", action="store_true", help="print as JSON")
    p_family_show = family_sub.add_parser("show", help="show one family's ladder")
    p_family_show.add_argument("family", help="the family name")
    p_family_show.add_argument("--json", action="store_true", help="print as JSON")

    p_loop = sub.add_parser("loop", help="run the regeneration loop")
    loop_sub = p_loop.add_subparsers(dest="loop_command", required=True)
    p_loop_run = loop_sub.add_parser(
        "run", parents=[offline_parent], help="run the loop for a family"
    )
    p_loop_run.add_argument("family", help="the family to regenerate")
    p_loop_run.add_argument("seed_urls", nargs="+", help="one or more seed URLs")
    p_loop_run.add_argument("--json", action="store_true", help="print as JSON")

    p_access = sub.add_parser("access", help="inspect the access store")
    access_sub = p_access.add_subparsers(dest="access_command", required=True)
    p_access_status = access_sub.add_parser("status", help="list per-domain access rows")
    p_access_status.add_argument("--json", action="store_true", help="print as JSON")

    p_audit = sub.add_parser("audit", help="print the audit trail")
    p_audit.add_argument("family", nargs="?", default=None, help="optional family filter")
    p_audit.add_argument("--json", action="store_true", help="print as JSON")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and dispatch to a subcommand. Returns a process exit code.

    Argparse usage errors (no/unknown subcommand, missing positional) raise
    ``SystemExit``; we catch it and return its code so ``main`` always returns an
    int rather than terminating, which keeps it callable from tests. A command
    that itself fails (e.g. ``family show`` on an unknown family) returns a
    non-zero code via its handler.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse already printed usage/err to stderr; surface its code (2 for a
        # usage error) as a non-zero return instead of exiting the interpreter.
        return int(exc.code or 0)

    handlers = {
        "crawl": _cmd_crawl,
        "family": _cmd_family,
        "loop": _cmd_loop,
        "access": _cmd_access,
        "audit": _cmd_audit,
    }
    return handlers[args.command](args)


# --------------------------------------------------------------------------- #
# Dependency construction (the one place deps are wired)
# --------------------------------------------------------------------------- #


def _open_registry(args: argparse.Namespace) -> Registry:
    """Open the registry at ``--db`` with crawler files under ``--crawlers-dir``.

    The read-only commands need ONLY this — no config, client, model, or browser
    — so it is split out from the heavier :func:`_build_engine`.
    """
    return Registry(db_path=args.db, crawlers_dir=Path(args.crawlers_dir))


def _make_completer(offline: bool) -> Completer:
    """The model adapter: a real :class:`LiteLLMCompleter`, or — when ``offline``
    (or no ``ANTHROPIC_API_KEY`` is set) — an empty :class:`FakeCompleter`.

    The FakeCompleter raises if it is ever actually called, which is the point:
    an offline ``crawl`` that genuinely needs the model (a drift/bootstrap that
    reaches T2) fails loudly rather than silently hitting a real endpoint. The
    happy registry path never calls it, so ``--offline crawl`` of a healthy
    family works with no key.
    """
    if offline or not os.environ.get("ANTHROPIC_API_KEY"):
        return FakeCompleter([])
    return LiteLLMCompleter()


def _make_browser_runner(offline: bool, config: AppConfig) -> object:
    """The browser runner for the access ladder.

    Online, this is the real :class:`~crawloop.browser.PlaywrightBrowserRunner`
    (constructed lazily — it launches no browser until a render is actually
    requested, and re-enforces the allowlist on every navigation). ``--offline``
    returns a minimal stub whose ``render`` raises if a command ever reaches the
    browser path, so an offline run can never silently spin up a browser. Commands
    that never render (the common HTTP path) are unaffected either way.
    """
    if offline:
        return _StubBrowserRunner()
    from crawloop.browser import PlaywrightBrowserRunner

    return PlaywrightBrowserRunner(config)


class _StubBrowserRunner:
    """A no-op :class:`~crawloop.access.BrowserRunner` for ``--offline`` CLI
    runs.

    Offline runs must never launch a browser; if a crawl genuinely needs a
    rendered page this raises so the failure is explicit rather than a silent
    empty page (or an unexpected browser launch).
    """

    async def render(self, url, *, stealth, wait_for=None, extra_headers=None) -> str:
        raise RuntimeError(
            "browser rendering is disabled for --offline runs "
            f"(needed for {url!r}); run without --offline to enable it"
        )


def _build_engine(args: argparse.Namespace, registry: Registry, client) -> Engine:
    """Assemble an :class:`Engine` from the parsed args + an open ``client``.

    Loads the allowlist config, builds the (lazy) completer + browser runner per
    ``--offline``, and points the engine's fixtures at ``--fixtures-dir``. This
    is the ONLY place the crawl/loop commands wire the engine, so the dep graph
    lives in one spot.
    """
    config: AppConfig = load_config(args.config)
    completer = _make_completer(args.offline)
    return Engine(
        config,
        registry,
        completer,
        client=client,
        browser_runner=_make_browser_runner(args.offline, config),
        fixtures_dir=Path(args.fixtures_dir),
        # Offline runs have no model to call, so the hybrid tail-fill is disabled —
        # the fast path stays deterministic-only (the offline completer would raise).
        offline=args.offline,
    )


# --------------------------------------------------------------------------- #
# Command handlers (each thin: build deps -> call engine/registry -> print)
# --------------------------------------------------------------------------- #


def _cmd_crawl(args: argparse.Namespace) -> int:
    """Serve one URL through the engine and print the result."""

    async def _run() -> int:
        registry = _open_registry(args)
        async with build_http_client() as client:
            engine = _build_engine(args, registry, client)
            result = await engine.request(args.url, schema=args.schema)
        _print_request_result(result, as_json=args.json)
        return 0

    return asyncio.run(_run())


def _cmd_family(args: argparse.Namespace) -> int:
    """``family list`` / ``family show <family>`` — read-only registry views."""
    registry = _open_registry(args)
    if args.family_command == "list":
        families = registry.all_families()
        if args.json:
            print(json.dumps(families, indent=2))
        elif not families:
            print("(no families registered)")
        else:
            for fam in families:
                print(
                    f"{fam['family']}  [{fam['status']}]  schema={fam['schema_ref']}  "
                    f"patterns={fam['url_patterns']}"
                )
        return 0

    # show
    fam = registry.get_family(args.family)
    if fam is None:
        print(f"error: unknown family {args.family!r}", flush=True)
        return 1
    ladder = registry.version_ladder(args.family)
    if args.json:
        print(json.dumps({"family": fam, "ladder": ladder}, indent=2))
    else:
        print(f"{fam['family']}  [{fam['status']}]  schema={fam['schema_ref']}")
        if not ladder:
            print("  (no versions)")
        for rung in ladder:
            print(
                f"  v{rung['n']}  {rung['status']}  "
                f"runs={rung['runs']} successes={rung['successes']}"
            )
    return 0


def _cmd_loop(args: argparse.Namespace) -> int:
    """``loop run <family> <seed_url...>`` — run the regeneration Loop."""

    async def _run() -> int:
        registry = _open_registry(args)
        fam = registry.get_family(args.family)
        if fam is None:
            print(f"error: unknown family {args.family!r}", flush=True)
            return 1
        schema_ref = fam["schema_ref"]
        async with build_http_client() as client:
            engine = _build_engine(args, registry, client)
            # Reuse the engine's wired context/completer/model rather than
            # re-deriving them here — run_loop is the same call the engine makes
            # on the drift path, so threading the engine's pieces keeps one wiring.
            result: LoopResult = await run_loop(
                args.family,
                list(args.seed_urls),
                engine._ctx,  # noqa: SLF001 — the CLI is a trusted in-package caller
                registry,
                engine._completer,  # noqa: SLF001
                schema_ref,
                fixtures_dir=Path(args.fixtures_dir),
                model=engine._model,  # noqa: SLF001
            )
        _print_loop_result(result, as_json=args.json)
        return 0

    return asyncio.run(_run())


def _cmd_access(args: argparse.Namespace) -> int:
    """``access status`` — print the persistent per-domain access store."""
    registry = _open_registry(args)
    rows = registry.access_rows()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("(no access rows)")
        return 0
    for row in rows:
        print(
            f"{row['domain']}  working_strategy={row['working_strategy']}  "
            f"status={row['status']}  updated_at={row['updated_at']}"
        )
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    """``audit [family]`` — print the audit trail (newest first)."""
    registry = _open_registry(args)
    entries = registry.read_audit(args.family)
    if args.json:
        print(json.dumps(entries, indent=2))
        return 0
    if not entries:
        print("(no audit entries)")
        return 0
    for entry in entries:
        print(
            f"{entry['ts']}  {entry['event']}  family={entry['family']}  "
            f"data={json.dumps(entry['data'])}"
        )
    return 0


# --------------------------------------------------------------------------- #
# Printing helpers (one place per result type, so text/JSON stay consistent)
# --------------------------------------------------------------------------- #


def _print_request_result(result, *, as_json: bool) -> None:
    """Print a :class:`~crawloop.engine.RequestResult` as text or JSON.

    The JSON view is a flat, machine-readable summary (item ``count`` rather than
    the full items, plus the loop/recovery fields) so scripts can consume it; the
    text view is a one-line human summary followed by the items.
    """
    if as_json:
        summary = {
            "source": result.source,
            "family": result.family,
            "used_version": result.used_version,
            "count": len(result.items),
            "recovered_strategy": result.recovered_strategy,
            "reason": result.reason,
            "loop": dataclasses.asdict(result.loop) if result.loop is not None else None,
        }
        print(json.dumps(summary, indent=2, default=str))
        return
    print(
        f"source={result.source} family={result.family} "
        f"version={result.used_version} items={len(result.items)} "
        f"reason={result.reason!r}"
    )
    if result.recovered_strategy is not None:
        print(f"recovered_strategy={result.recovered_strategy}")
    if result.loop is not None:
        print(
            f"loop: ok={result.loop.ok} version={result.loop.version} "
            f"rounds={result.loop.rounds} reason={result.loop.reason!r}"
        )
    for item in result.items:
        print(f"  {json.dumps(item, default=str)}")


def _print_loop_result(result: LoopResult, *, as_json: bool) -> None:
    """Print a :class:`LoopResult` as text or JSON."""
    if as_json:
        print(json.dumps(dataclasses.asdict(result), indent=2, default=str))
        return
    print(
        f"loop: ok={result.ok} version={result.version} rounds={result.rounds} "
        f"escalated={result.escalated} reason={result.reason!r}"
    )


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
