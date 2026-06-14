"""crawloop — a self-healing structured-data web scraper.

The LLM is used as a *compiler* and an *oracle*, not a per-page runtime: it writes
a cheap deterministic crawler once, and on a site redesign it regenerates a fresh
version and promotes it only when it agrees with the oracle. Most users drive the
``crawloop`` CLI; this package also exposes a small importable API so the engine
can be embedded:

    from crawloop import Engine, run_loop

See the README for end-to-end usage and the offline demo.
"""

from crawloop.engine import Engine, RequestResult
from crawloop.loop.driver import LoopResult, run_loop

__all__ = ["Engine", "RequestResult", "LoopResult", "run_loop"]
