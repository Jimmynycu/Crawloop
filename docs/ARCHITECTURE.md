# Architecture

A high-level map of crawloop. For the full design see
[design.html](design.html); for the build plan see
[plans/2026-06-13-crawloop-poc.md](plans/2026-06-13-crawloop-poc.md).

> **Status:** proof of concept. It demonstrates the self-heal + access-recovery
> loop on sites you own or are authorized to crawl. It is not a production scraper.

## The idea in one paragraph

You point crawloop at an **authorized** URL and a target **schema**. It extracts
records with cheap, generated, deterministic crawlers (the fast path, no LLM). When a
site redesign breaks a crawler — or a fetch gets blocked — it **serves the data now**
via an LLM fallback while it **regenerates a fresh crawler in the background**. Every
fetch is gated by a hard domain allowlist, every generated crawler is AST-checked
before it ever runs, and every promotion and recovery is written to an audit trail.
"Serve now, heal in the background" is the whole contract.

## Runtime flow

The [`Engine`](../crawloop/engine.py) is the front door. `request(url, schema?)`
composes every piece below into one decision tree:

```
request(url, schema?)
   │
   ▼
1. AUTHORIZE ───── off-allowlist? ──► UnauthorizedDomain (hard stop, never healed)
   │ (config)
   ▼
2. ROUTE ───────── which registered family owns this URL?
   │ (router)
   ├── hit  ──► 3. KNOWN FAMILY
   │             run the version ladder (executor)  ── validates? ──► serve (no LLM)
   │             │ all versions fail → classify (loop.trigger):
   │             │   DRIFT      → serve now via T2 (fallback) + trigger Loop once
   │             │   BLOCKED_*  → escalate access ladder (loop.access_recovery), retry once
   │             │   TRANSIENT  → retry, capped, with backoff
   │             │   GONE       → return empty, do not regenerate
   │
   └── miss ──► 4. UNKNOWN FAMILY (bootstrap)
                 require a schema → serve now via T2 → register a family
                 (URL pattern + schema) → trigger the Loop to grow a first crawler
```

It is bounded everywhere: recovery retries the family once, transient is capped,
drift triggers regeneration exactly once. No monitor, no background worker pool — that
is YAGNI for the POC.

## The pieces

### Contract — [`crawloop/contract.py`](../crawloop/contract.py)

The narrow interface everything is written against. A `Crawler` is anything with
`family`, `schema_ref`, and `async crawl(url, ctx) -> CrawlResult`. Crawlers never
touch the network directly — they receive a `FetchContext` (a `Protocol` exposing
`fetch`, `fetch_rendered`, and pure helpers like `absolutize` / `parse_money` /
`clean_text`). This indirection is what lets generated code stay sandboxed: it can
only reach the network through the injected context, which is allowlist-gated and
rate-limited. Both are runtime-checkable `Protocol`s, so there is no base class to
inherit and generated crawlers stay tiny.

### Registry — [`crawloop/registry.py`](../crawloop/registry.py)

The source of truth for *which crawlers exist, which version is live, and what
happened*. It owns a small SQLite DB (families, the per-family **version ladder**, the
**audit trail**, the per-domain access store, run history) and the on-disk crawler
files under `crawlers_dir/<family>/v<n>.py`. It is also a **trust boundary**:

- `add_version` runs the AST gate **before** writing anything to disk, so ungated
  source is never persisted, and records `source_sha = sha256(source)`.
- `load_crawler` reads the file **exactly once**, then gates, integrity-checks
  (re-hash vs `source_sha`), compiles, and executes that single in-memory string —
  no second disk read, so the bytes that were checked are the exact bytes that run.
- Every SQL statement binds values with `?`; identifiers and values are never
  interpolated into SQL.

Two output-schema concerns sit beside it: [`schemas.py`](../crawloop/schemas.py)
is a versioned registry that discovers Pydantic models in the top-level `schemas/`
directory and registers them as `Name@1`, and [`router.py`](../crawloop/router.py)
is a pure URL-regex router that answers "which registered family owns this URL?"
deterministically (sorted by family name; a malformed pattern is skipped, never
fatal).

### Loop — [`crawloop/loop/`](../crawloop/loop/)

The regeneration state machine — the part that "heals." [`driver.run_loop`](../crawloop/loop/driver.py)
drives a family from "we need a crawler / the current one broke" to either a promoted
new version or a bounded escalation. The pipeline:

1. **Sample** ([`sampler.py`](../crawloop/loop/sampler.py)) — fetch a few fresh
   pages of the family.
2. **Oracle** ([`fallback.py`](../crawloop/fallback.py), "T2") — per sample, the
   LLM reads the HTML against the schema to produce the trusted "what the answer
   should be." Samples whose oracle fails are dropped; fewer than ~3 usable oracles
   escalates (3 independent samples bound the oracle's own error).
3. **Context** — load the family's golden fixtures (regression reference) and the
   active version's source (for repair prompts). A brand-new family just has neither —
   same code path, so bootstrap is `run_loop` with nothing registered yet. Two
   **deterministic, no-/one-LLM** candidates are also built here when samples embed a
   JSON island: a value-path crawler ([`jsonpath.py`](../crawloop/loop/jsonpath.py)) and
   a path-map crawler ([`pathmap.py`](../crawloop/loop/pathmap.py)).
4. **Rounds** — [`codegen.py`](../crawloop/loop/codegen.py) writes `k` candidate
   crawler sources; [`gauntlet.py`](../crawloop/loop/gauntlet.py) sandbox-runs each
   ([`sandbox.py`](../crawloop/loop/sandbox.py)) and scores it through the validator.
   A passing winner is **promoted** ([`promote.py`](../crawloop/loop/promote.py));
   otherwise the scores feed the next round's prompt.
5. **Exhausted** — `max_rounds` with no winner → escalate.

Failure classification ([`trigger.py`](../crawloop/loop/trigger.py)) and access
escalation ([`access_recovery.py`](../crawloop/loop/access_recovery.py)) also live
here; both are small, bounded async state machines.

### Validator — [`crawloop/validator.py`](../crawloop/validator.py)

The layered correctness gates the gauntlet trusts. Pure functions over an
*already-extracted* item list — it never fetches and never runs crawler code. The
gates: **schema** (coerce through the Pydantic model, `extra="forbid"`), **field
floors** (required fields must clear a fill-rate threshold — catches a crawler that
silently emits `None`), **item-count floor** (catches a collapse to far fewer items),
**oracle agreement** and **fixture regression** (the same comparison engine against
the T2 oracle and against golden fixtures), and a soft **history cross-check** on
volatile numeric fields. This is what makes "did this candidate actually work?" an
objective, deterministic decision.

### Access — [`crawloop/access.py`](../crawloop/access.py)

Turns a request into HTML and, when a site blocks us (429 / 401 / 403 / a
challenge page), escalates through an ordered **strategy ladder** until one gets
through. It defines `RateLimiter` (per-domain minimum-interval async gate), the
concrete strategies (`PlainHTTP`, `HeaderFetch`, `BackoffRetry`, `BrowserFetch`,
`CaptchaSolver`), the typed `FetchBlocked` / `FetchError` errors, and
`RealFetchContext` — the concrete `FetchContext` injected into generated crawlers.
The `build_strategy` factory is the single place mapping config strategy kinds to
instances, used by both the fast path and the recovery loop. Everything here only
ever runs against allowlisted domains.

## Cross-cutting boundaries

- **Allowlist** ([`config.py`](../crawloop/config.py),
  `authorized_domains.yaml`) — the first and last word on whether a URL may be
  fetched. Checked up front and on every redirect hop; an off-list URL is a hard
  policy stop, never routed into healing or recovery.
- **AST gate** ([`safety.py`](../crawloop/safety.py)) — the only thing between
  LLM-generated source and the host. A conservative allowlist of imports/builtins,
  run at registration *and* at every load. Paired with subprocess sandboxing at run
  time (the gauntlet) because it is a static defense.
- **LLM port** ([`llm.py`](../crawloop/llm.py)) — every model call goes through
  the `Completer` Protocol, never `litellm` directly. Production uses
  `LiteLLMCompleter` (reads `ANTHROPIC_API_KEY` from the env); tests inject
  `FakeCompleter`, so no unit test reaches a real model or the network.

## Module map

| Path | Role |
| --- | --- |
| `crawloop/engine.py` | Front door; wires the runtime flow above |
| `crawloop/contract.py` | `Crawler` / `FetchContext` protocols + pure helpers |
| `crawloop/registry.py` | SQLite metadata, on-disk crawlers, version ladder, audit trail |
| `crawloop/router.py` | Pure URL→family regex router |
| `crawloop/schemas.py` | Versioned output-schema registry (`schemas/` discovery) |
| `crawloop/validator.py` | Correctness gates (schema, fill, count, oracle, regression) |
| `crawloop/access.py` | Rate limiter, fetch strategies, recovery ladder, `RealFetchContext` |
| `crawloop/fallback.py` | T2 direct LLM extractor (serve-now + loop oracle) |
| `crawloop/safety.py` | AST allowlist gate for generated code |
| `crawloop/config.py` | App config + allowlist enforcement |
| `crawloop/llm.py` | `Completer` port + fake/litellm implementations |
| `crawloop/executor.py` | Runs a family's version ladder against a URL |
| `crawloop/loop/` | Regeneration state machine (sample → oracle → codegen → gauntlet → promote) |
| `crawloop/cli.py` | `crawloop` command-line entry point |
| `schemas/` | Contributed Pydantic output schemas |
| `prompts/` | LLM prompt templates (extract / codegen / pathmap) |

## See also

- [design.html](design.html) — full system design (the authoritative reference).
- [plans/](plans/) — the build plan this POC was implemented against.
- [../CONTRIBUTING.md](../CONTRIBUTING.md) — dev setup, test discipline, safety rules.
