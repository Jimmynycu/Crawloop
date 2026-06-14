"""Subprocess entry point that executes ONE candidate crawler against ONE page.

This script is spawned by :func:`crawloop.loop.sandbox.run_in_sandbox` as a
child process; it is never imported by the parent. It is the *runtime* half of
the candidate trust boundary (the AST gate is the static half, already run by the
parent before spawn): even though the source is gated, it runs here — in its own
process, under the parent's wall-clock timeout — so a candidate that misbehaves
at runtime is contained to a killable child.

Protocol (all over the stdio pipes, so the parent never has to share memory with
the candidate):

* STDIN  — one JSON object ``{"source": str, "html": str, "url": str}``.
* STDOUT — on success, ``{"ok": true, "items": [...]}``; on ANY failure,
  ``{"ok": false, "error": "<repr>"}`` and a non-zero exit code.

The context handed to the crawler is OFFLINE: ``fetch``/``fetch_rendered`` both
return the single stored ``html`` and ignore the URL (this is a single-page
sandbox — there is no network and no second page to reach). The coercion helpers
delegate to :mod:`crawloop.contract` so they behave exactly as in production.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys

from crawloop import contract


class _OfflineContext:
    """An offline :class:`~crawloop.contract.FetchContext`.

    ``fetch`` and ``fetch_rendered`` both return the one stored HTML string and
    ignore their ``url`` argument — the sandbox runs a candidate against a single
    captured page with NO network access of any kind. The coercion helpers
    delegate to the shared contract module so a candidate sees the same
    absolutize/parse_money/clean_text behaviour it would in production.
    """

    def __init__(self, html: str) -> None:
        self._html = html

    async def fetch(self, url: str) -> str:
        return self._html

    async def fetch_rendered(self, url: str, wait_for: str | None = None) -> str:
        return self._html

    def absolutize(self, base: str, href: str | None):
        return contract.absolutize(base, href)

    def parse_money(self, raw):
        return contract.parse_money(raw)

    def clean_text(self, raw):
        return contract.clean_text(raw)


def _find_crawler_class(module_ns: dict):
    """Return the single crawler class defined in ``module_ns``.

    A crawler class is identified structurally (the candidate may name it
    anything): a ``type`` that carries CONCRETE ``family`` and ``schema_ref``
    attributes and an ``async def crawl``. The imported ``Crawler`` Protocol is
    naturally excluded — it declares ``family``/``schema_ref`` only as
    annotations, so they are not present as attribute *values* on the class.

    Raises ``ValueError`` if there is not exactly one such class, so a module
    that defines none (or several) fails cleanly as a runtime error rather than
    guessing.
    """
    found = []
    for value in module_ns.values():
        if not isinstance(value, type):
            continue
        if not (hasattr(value, "family") and hasattr(value, "schema_ref")):
            continue
        crawl = getattr(value, "crawl", None)
        if crawl is None or not inspect.iscoroutinefunction(crawl):
            continue
        found.append(value)
    if len(found) != 1:
        raise ValueError(
            f"expected exactly one Crawler class in candidate module, found {len(found)}"
        )
    return found[0]


def _run(source: str, html: str, url: str) -> list[dict]:
    """Compile + exec ``source``, instantiate its crawler, run ``crawl`` offline.

    The source is compiled and exec'd into a FRESH module namespace (not the
    runner's globals), the single crawler class is located, instantiated with no
    arguments, and its ``crawl`` coroutine is driven to completion with
    :func:`asyncio.run` against the offline context. Returns the result's
    ``items`` list.
    """
    code = compile(source, "<candidate>", "exec")
    module_ns: dict = {"__name__": "<candidate>"}
    exec(code, module_ns)  # noqa: S102 — gated source, run inside the sandbox child

    crawler_cls = _find_crawler_class(module_ns)
    crawler = crawler_cls()
    ctx = _OfflineContext(html)
    result = asyncio.run(crawler.crawl(url, ctx))
    return list(result.items)


def main() -> None:
    """Read the job from STDIN, run it, and emit the JSON result on STDOUT.

    Any exception — bad JSON, a compile error, no crawler class, or a failure
    inside ``crawl`` — is caught and reported as ``{"ok": false, "error": ...}``
    with a non-zero exit, so the parent always gets a parseable line and never a
    silent crash.
    """
    try:
        payload = json.loads(sys.stdin.read())
        items = _run(payload["source"], payload["html"], payload["url"])
    except BaseException as exc:  # noqa: BLE001 — last-resort: report, never leak a traceback
        sys.stdout.write(json.dumps({"ok": False, "error": repr(exc)}))
        sys.stdout.flush()
        sys.exit(1)
    sys.stdout.write(json.dumps({"ok": True, "items": items}, default=str))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
