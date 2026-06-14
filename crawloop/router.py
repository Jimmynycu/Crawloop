"""The URL-regex router (Task 10.1): which registered family owns a URL.

The engine's first decision on every request is "do we already have a crawler
family for this URL?". A family is registered with ``url_patterns`` — a JSON list
of regex strings — and :func:`route` answers that question by returning the
family whose patterns match the URL, or ``None`` if none do.

Two properties make this safe and predictable:

* **Deterministic order.** Families are iterated in the order
  :meth:`Registry.all_families` returns them (sorted by family name), and the
  FIRST family with a matching pattern wins. So when a URL is covered by more
  than one family's patterns, which family handles it is stable across runs and
  independent of insertion order.
* **A bad pattern is skipped, never fatal.** Operator/LLM-derived patterns are
  compiled defensively: a malformed regex in one family's list is skipped (that
  one pattern is ignored) rather than raising out of :func:`route` and breaking
  routing for every other family. One corrupt row cannot take down the router.

The router is a PURE read over stored patterns — it never fetches, never runs
crawler code, and never touches the LLM.
"""

from __future__ import annotations

import re

from crawloop.registry import Registry


def _matches(url: str, patterns: list[str]) -> bool:
    """Whether any of ``patterns`` matches ``url`` (``re.search`` semantics).

    Each pattern is compiled in isolation; a pattern that is not a valid regex is
    skipped (it simply never matches) so one malformed entry cannot raise out of
    the router. ``re.search`` (not ``fullmatch``) is used so operators can write
    loose substring hooks (e.g. ``"/catalogue/"``) as well as fully anchored
    patterns (``"^https?://host/...$"``).
    """
    for pattern in patterns:
        try:
            if re.search(pattern, url) is not None:
                return True
        except re.error:
            # Malformed pattern: ignore this one rung, keep checking the rest.
            continue
    return False


def route(url: str, registry: Registry) -> str | None:
    """Return the family whose ``url_patterns`` match ``url``, else ``None``.

    Families are walked in :meth:`Registry.all_families` order (by family name);
    the first family any of whose patterns matches ``url`` wins. A family with no
    patterns never matches; a malformed pattern within a family's list is skipped
    (see :func:`_matches`) so it can neither match nor crash the walk.
    """
    for family in registry.all_families():
        if _matches(url, family["url_patterns"]):
            return family["family"]
    return None
