# crawloop POC Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the POC of a self-healing structured-data crawler: a registry of per-page-family crawlers (LLM-generated Python), an extraction-regeneration Loop, and an Access-Recovery loop — proven end-to-end by a deterministic test that breaks a layout and watches the system heal.

**Architecture:** All I/O flows through an injected `FetchContext` (network) and `Completer` (LLM) so every component is testable offline. Crawlers are generated Python modules gated by an AST allowlist and run sandboxed during testing. A SQLite+files registry holds per-family *version ladders*. Two background loops share one shape — try strategies → validate deterministically → save the winner → escalate after k rounds — one for extraction (The Loop), one for fetching (Access Recovery).

**Tech Stack:** Python 3.12 · asyncio · httpx · Playwright/Patchright · parsel · Pydantic v2 · litellm · SQLite (stdlib) · pytest.

**Design reference:** `docs/design.html` (v2, approved 2026-06-13). Section numbers below (§N) point there.

**Conventions for every task:** DRY · YAGNI · TDD (test first, watch it fail, minimal code, watch it pass) · commit after each green step · exact paths · `ruff` clean. Inject dependencies; never call network/LLM from a unit test. Branch: do all work on `main` (fresh repo) committing frequently.

**LLM testing rule:** No unit test calls a real model. Use `FakeCompleter` (canned responses). The single flagship E2E test uses recorded cassettes, so CI needs no API keys.

---

## Milestone 0 — Scaffold

### Task 0.1: Initialize repo + tooling

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `README.md`, `crawloop/__init__.py`, `tests/__init__.py`, `conftest.py`

**Step 1:** `git init` (env: not yet a repo). Configure `.gitignore` for Python (`__pycache__/`, `.venv/`, `*.db`, `.pytest_cache/`, `crawlers/`, `fixtures/snapshots/`, `.env`).

**Step 2:** Write `pyproject.toml`:

```toml
[project]
name = "crawloop"
version = "0.0.1"
requires-python = ">=3.12"
dependencies = [
  "httpx>=0.27", "parsel>=1.9", "pydantic>=2.7", "pyyaml>=6",
  "litellm>=1.40", "playwright>=1.44", "patchright>=1.44",
]
[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "ruff>=0.5", "respx>=0.21"]
[project.scripts]
crawloop = "crawloop.cli:main"
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
[tool.ruff]
line-length = 100
```

**Step 3:** `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`. Run `pytest` → expect "no tests ran" (exit 5) — proves the harness works.

**Step 4: Commit** `chore: scaffold crawloop project`.

---

## Milestone 1 — Data spine: schemas + contract

### Task 1.1: Output schemas + versioned registry

**Files:**
- Create: `schemas/__init__.py`, `schemas/product.py`, `schemas/quote.py`, `crawloop/schemas.py`
- Test: `tests/test_schemas.py`

**Step 1: Failing test** (`tests/test_schemas.py`):

```python
import pytest
from decimal import Decimal
from crawloop.schemas import get_schema, SchemaNotFound

def test_get_schema_by_versioned_ref():
    model = get_schema("Product@1")
    obj = model(name="A", price=Decimal("9.99"), in_stock=True, url="https://x.com/a")
    assert obj.price == Decimal("9.99")

def test_volatile_fields_exposed():
    assert "price" in get_schema("Product@1").VOLATILE

def test_unknown_ref_raises():
    with pytest.raises(SchemaNotFound):
        get_schema("Nope@1")

def test_extra_fields_forbidden():
    model = get_schema("Product@1")
    with pytest.raises(Exception):
        model(name="A", price=Decimal("1"), in_stock=True, url="https://x.com/a", junk=1)
```

**Step 2:** Run → FAIL (module missing).

**Step 3: Implement.** `schemas/product.py` is the §10 example (Product with `VOLATILE: ClassVar[set[str]]`). `schemas/quote.py`:

```python
from typing import ClassVar
from pydantic import BaseModel, ConfigDict, HttpUrl

class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    author: str
    tags: list[str] = []
    url: HttpUrl | None = None
    VOLATILE: ClassVar[set[str]] = set()
```

`crawloop/schemas.py` — registry that maps `"Name@ver"` → model class by scanning the `schemas/` package; `SchemaNotFound` exception; `get_schema(ref)`; `schema_json(ref)` returning `model.model_json_schema()` (used by prompts). Registration: a module-level dict keyed `f"{cls.__name__}@1"`; bump convention documented in README.

**Step 4:** Run → PASS. **Step 5: Commit** `feat: versioned output schema registry`.

### Task 1.2: Crawler contract

**Files:**
- Create: `crawloop/contract.py`
- Test: `tests/test_contract.py`

**Step 1: Failing test** — a hand-written fake crawler satisfies `Crawler`, returns a valid `CrawlResult`, and `CrawlResult` rejects a non-list `items`.

**Step 2:** Run → FAIL.

**Step 3: Implement** §5 exactly: `CrawlResult(BaseModel)` (`items: list[dict]`, `next_url: str | None`), `FetchContext(Protocol)` (`fetch`, `fetch_rendered`, `absolutize`, `parse_money`, `clean_text`), `Crawler(Protocol)` (`family`, `schema_ref`, `async crawl`). Implement the helper coercions (`absolutize` via `urllib.parse.urljoin`; `parse_money` strips currency symbols/whitespace → `Decimal`, returns `None` on failure; `clean_text` collapses whitespace) as module functions reused by the real `FetchContext` later.

**Step 4:** PASS. **Step 5: Commit** `feat: crawler contract + coercion helpers`.

---

## Milestone 2 — Safety: AST gate + allowlist (the trust boundary)

### Task 2.1: AST allowlist gate

**Files:**
- Create: `crawloop/safety.py`
- Test: `tests/test_ast_gate.py`, `tests/fixtures_malicious/*.py`

This is load-bearing (§6a). Be thorough.

**Step 1: Failing test** — a corpus. Each "bad" sample must be rejected; each "good" sample accepted.

```python
import pytest
from crawloop.safety import ast_check, ASTViolation

GOOD = '''
from parsel import Selector
import re
from crawloop.contract import Crawler, CrawlResult, FetchContext
class C(Crawler):
    family = "x"; schema_ref = "Product@1"
    async def crawl(self, url, ctx):
        sel = Selector(await ctx.fetch(url))
        return CrawlResult(items=[{"name": sel.css("h1::text").get()}])
'''

BAD = {
    "import_os": "import os",
    "import_subprocess": "import subprocess",
    "import_httpx": "import httpx",
    "import_requests": "import requests",
    "dunder_import": "x = __import__('os')",
    "eval": "y = eval('1+1')",
    "exec": "exec('x=1')",
    "compile": "compile('1','<s>','eval')",
    "open_file": "f = open('/etc/passwd')",
    "getattr_str": "getattr(obj, 'system')",
    "dunder_attr": "x = ().__class__.__bases__",
    "from_os_import": "from os import system",
}

def test_good_passes():
    assert ast_check(GOOD) == []

@pytest.mark.parametrize("name,src", list(BAD.items()))
def test_bad_rejected(name, src):
    violations = ast_check(src)
    assert violations, f"{name} should be rejected"

def test_check_or_raise():
    with pytest.raises(ASTViolation):
        ast_check("import os", raise_on_violation=True)
```

**Step 2:** Run → FAIL.

**Step 3: Implement** `safety.py`:
- `ALLOWED_IMPORTS = {"parsel", "re", "json", "datetime", "decimal", "urllib.parse", "urllib", "crawloop.contract"}`.
- `ast_check(source, *, raise_on_violation=False) -> list[str]`: parse with `ast.parse`; walk with an `ast.NodeVisitor`. Flag: `Import`/`ImportFrom` whose root module ∉ allowlist; any `Call` to names `{"eval","exec","compile","open","__import__","getattr","setattr","delattr","globals","locals","vars"}`; any `Attribute` whose `attr` starts and ends with `__` (dunder access); any `Name` id `__import__`. Return list of human-readable violation strings; raise `ASTViolation` if requested.
- Note in docstring: this runs at registration AND module load (Task 5.x calls it before importing any crawler).

**Step 4:** PASS (all params). **Step 5: Commit** `feat: AST allowlist gate for generated crawlers`.

### Task 2.2: Config + domain allowlist

**Files:**
- Create: `crawloop/config.py`, `authorized_domains.yaml` (the §6b example)
- Test: `tests/test_config.py`

**Step 1: Failing test** — load config; `is_authorized("books.toscrape.com")` True, unknown False; `domain_config(...)` returns `max_rps`/`render_js`/`access_strategies`; `respect_robots` defaults False; `assert_authorized(url)` raises `UnauthorizedDomain` for off-list URL.

**Step 2:** FAIL. **Step 3: Implement** dataclasses `DomainConfig`, `AppConfig`; `load_config(path)` via `yaml.safe_load`; helpers above; `UnauthorizedDomain` exception. Parse `access_strategies` into a normalized list of `(kind, params)`.

**Step 4:** PASS. **Step 5: Commit** `feat: config loader + hard domain allowlist`.

---

## Milestone 3 — Test infrastructure: fixture server

### Task 3.1: Local fixture server with mutate + block modes

**Files:**
- Create: `tests/fixture_server/__init__.py`, `tests/fixture_server/server.py`, `tests/fixture_server/pages/books_listing.html`
- Test: `tests/test_fixture_server.py`

The whole E2E rests on this. It must serve a books-listing page and support runtime mode switches.

**Step 1: Failing test** — start server on an ephemeral port; GET listing → 200 with `article.product_pod`; set `mode="mutated"` → same data under renamed classes (`.price_color`→`.price-box`, `article.product_pod`→`div.card`); set `mode="blocked"` → 403 + Cloudflare-ish body unless header `x-test-bypass: ok` present, then 200.

**Step 2:** FAIL.

**Step 3: Implement** a `http.server`/`ThreadingHTTPServer` wrapper class `FixtureServer` with `.url`, `.start()`, `.stop()`, and mutable `.mode` (`"normal"|"mutated"|"blocked"`) plus `.bypass_header`. Two hand-written HTML templates (normal + mutated) holding the SAME 3 books (name/price/stock/href) so expected JSON is identical across layouts — that's what proves a heal preserves correctness. Pagination: listing links to `?page=2` with one more book, `page=2` has `li.next` absent.

**Step 4:** PASS. **Step 5: Commit** `test: fixture server with mutate + block modes`.

### Task 3.2: pytest fixture exposing the server

**Files:** Modify `conftest.py`. Add a `fixture_server` pytest fixture (function-scoped, yields a started `FixtureServer`, resets `mode="normal"`). Commit `test: fixture_server pytest fixture`.

---

## Milestone 4 — Access layer (fetch + recovery)

### Task 4.1: Per-domain async rate limiter

**Files:** Create `crawloop/access.py`; Test `tests/test_rate_limiter.py`.

**Step 1: Failing test** — `RateLimiter(max_rps=10)`; two consecutive `await rl.acquire("d")` calls are spaced ≥ ~0.1s (assert elapsed ≥ 0.09 using `asyncio` event-loop time, not wall clock to keep CI stable). Different domains don't block each other.

**Step 2:** FAIL. **Step 3: Implement** token-bucket/sleep-gap limiter keyed by domain using `asyncio.Lock` + last-timestamp via `loop.time()`. **Step 4:** PASS. **Step 5: Commit** `feat: per-domain async rate limiter`.

### Task 4.2: AccessStrategy protocol + concrete strategies

**Files:** Modify `crawloop/access.py`; Test `tests/test_access_strategies.py`.

Strategies are ordered attempts to TURN a request into HTML (§6b ladder). Each implements:

```python
class AccessStrategy(Protocol):
    name: str
    async def fetch(self, url: str, *, render: bool) -> FetchOutcome: ...
# FetchOutcome = ok(html) | blocked(status, marker) | error(exc)
```

POC strategies (test each against the fixture server / respx mock):
- `PlainHTTP` (httpx GET, real headers) — clears `normal`.
- `BackoffRetry(inner, tries=3)` — wraps another; retries on 429/5xx/`error` with exponential sleep (use injected `sleep` fn so tests don't wait).
- `BrowserFetch` / `StealthFetch` — **POC: thin wrappers** that record they were chosen and delegate to a `BrowserRunner` protocol (real impl = Playwright/Patchright, test impl = fake returning the bypassed HTML). YAGNI: don't bring up a real browser in unit tests.
- `SessionFetch(creds_provider)` — adds an auth header/cookie from injected provider; clears the fixture `blocked` mode when its header matches.
- `BypassToken(header, value)` — adds the owner header; clears fixture `blocked` mode (header `x-test-bypass: ok`).
- `CaptchaSolver` — **interface only**, raises `NotEnabled` unless `authorized=True` configured (open-question default: shipped, off).

**Step 1: Failing tests** — `PlainHTTP` returns `ok` on normal mode, `blocked` on blocked mode; `BypassToken` returns `ok` on blocked mode; `BackoffRetry` retries N times then gives up (assert call count via a flaky fake).
**Steps 2-4:** TDD each. **Step 5: Commit** `feat: access strategy ladder (plain/backoff/session/bypass/browser stubs)`.

### Task 4.3: FetchContext implementation

**Files:** Modify `crawloop/access.py`; Test `tests/test_fetch_context.py`.

**Step 1: Failing test** — `RealFetchContext(config, registry)` enforces allowlist (off-list URL → `UnauthorizedDomain`), applies rate limit, calls the domain's first working strategy, returns HTML; `absolutize/parse_money/clean_text` delegate to contract helpers.

**Step 3: Implement** `RealFetchContext` building its strategy list from `domain_config.access_strategies` (default `[PlainHTTP, BackoffRetry]`). On `ok` → return html. On `blocked`/`error` → raise typed `FetchBlocked(status, marker)` / `FetchError` so the classifier (Task 8.1) can categorize. **Step 4:** PASS. **Step 5: Commit** `feat: RealFetchContext with allowlist + rate limit + strategy dispatch`.

### Task 4.4: Access Recovery loop

**Files:** Create `crawloop/loop/access_recovery.py`; Test `tests/test_access_recovery.py`.

**Step 1: Failing test** — fixture server in `blocked` mode; `recover_access(domain, config, registry)` walks the ladder, finds the `bypass_token` strategy gets through, persists `working_strategy="bypass_token"` to the registry, returns success. Second call uses saved strategy first (assert ordering). After `k` full failed ladders (force all to fail) → returns `Escalated` and marks domain `escalated`.

**Step 3: Implement** the escalate-until-success machine (§9 box): order = `[saved_winner?] + configured_ladder`; first strategy that yields `ok` wins; persist winner; cap attempts; `Escalated` result + audit entry on exhaustion. **Step 4:** PASS. **Step 5: Commit** `feat: access-recovery loop with saved-winner reuse + escalation`.

---

## Milestone 5 — Registry (SQLite + files + version ladder)

### Task 5.1: Registry schema + family/version CRUD

**Files:** Create `crawloop/registry.py`, `crawloop/migrations.sql`; Test `tests/test_registry.py`.

**Step 1: Failing test** — open `Registry(":memory:")`; `upsert_family(family, url_patterns, schema_ref)`; `add_version(family, source)` writes `crawlers/<slug>/vN.py` (use a tmp `crawlers_dir`) and a DB row, returns `N`; `version_ladder(family)` returns versions newest-first with statuses; `set_active(family, n)` reorders; `record_run(family, version, ok)` updates `success_30d`/counts; `rollback(family)` flips active to previous.

**Step 3: Implement.** Tables: `families`, `versions` (family, n, status ∈ active|fallback|archived, path, promoted_at, runs, successes), `audit`, `domain_access` (domain, working_strategy, status), `run_history` (family, url, version, extracted_json, ts) for the §10 gate-5 cross-check. Slugify family → dir name (`books.toscrape.com/product_list` → `books_toscrape__product_list`). `add_version` runs `ast_check(source, raise_on_violation=True)` BEFORE writing — registry never stores ungated code.

**Step 4:** PASS. **Step 5: Commit** `feat: SQLite+files registry with version ladder`.

### Task 5.2: Safe crawler loader

**Files:** Modify `registry.py`; Test `tests/test_loader.py`.

**Step 1: Failing test** — `load_crawler(family, version)` re-runs `ast_check` on the file, imports it in an isolated module namespace, returns the `Crawler` subclass instance; a tampered file (post-write `import os` injected) raises `ASTViolation` at load.

**Step 3: Implement** load via `importlib.util.spec_from_file_location` after `ast_check`. Find the single `Crawler` subclass in the module. **Step 4:** PASS. **Step 5: Commit** `feat: gated crawler loader`.

### Task 5.3: Audit log

**Files:** Modify `registry.py`; Test `tests/test_audit.py`. `write_audit(event_dict)` appends a row + mirrors to `crawlers/audit.jsonl`; `read_audit(family)` filters. Match the §9 audit entry shape. Commit `feat: audit log`.

---

## Milestone 6 — Executor (runtime version ladder)

### Task 6.1: Single-version run + pagination

**Files:** Create `crawloop/executor.py`; Test `tests/test_executor.py`.

**Step 1: Failing test** — against fixture server (normal): `run_version(crawler, start_url, ctx, max_pages=5)` follows `next_url`, concatenates `items` across pages, stops at `max_pages` or when `next_url is None`; returns `(items, pages_fetched)`.

**Step 3: Implement** the pagination driver here (NOT in crawler code — keeps rate-limit central per §5). **Step 4:** PASS. **Step 5: Commit** `feat: executor pagination driver`.

### Task 6.2: Version-ladder walk + snapshotting

**Files:** Modify `executor.py`; Test `tests/test_version_ladder.py`.

**Step 1: Failing test** (§7 core) — registry with v2 (active, parses `div.card`) and v1 (fallback, parses `article.product_pod`); fixture in `normal` (v1 layout): `run_family(family, url, ctx, validator)` tries active v2 → validator fails → falls to v1 → validates → returns v1 items + `used_version=1`. Snapshot stored every Nth run (`snapshot_every=1` in test) under `fixtures/snapshots/<slug>/`.

**Step 3: Implement** the ladder walk: iterate `version_ladder` newest-first, run each, validate (Task 7), first pass wins; record run; store HTML snapshot via ctx hook. Raise `AllVersionsFailed(reason)` if none pass (caller → classifier). **Step 4:** PASS. **Step 5: Commit** `feat: runtime version-ladder walk + snapshots`.

---

## Milestone 7 — Validator (correctness gates 1–5)

### Task 7.1: Gate 1 schema + Gate 2 field floors

**Files:** Create `crawloop/validator.py`; Test `tests/test_validator_basic.py`.

**Step 1: Failing tests** — `validate_items(items, schema_ref, baseline)` → `ValidationReport(ok, failures, fill_rates, item_count)`. Type/extra errors → `ok=False` with per-item messages. Fill-rate floor: if a non-optional field is null in >X% (config, default 20%) → fail. Item-count floor vs `baseline` (e.g. <50% of trailing median) → fail.

**Step 3: Implement** gates 1–2. Coerce each item through the Pydantic model; collect failures; compute per-field fill rate; compare count to baseline. **Step 4:** PASS. **Step 5: Commit** `feat: validator gates 1-2 (schema + field floors)`.

### Task 7.2: Gates 3–5 (oracle agreement, fixture regression, history cross-check)

**Files:** Modify `validator.py`; Test `tests/test_validator_semantic.py`.

**Step 1: Failing tests:**
- `oracle_agreement(candidate_items, oracle_items, schema_ref)` → per-field precision/recall; stable fields need exact match, `VOLATILE` fields use tolerance (numeric: normalized equality ignoring currency/whitespace; bool: exact). Returns `agreement` float.
- `fixture_regression(crawler, fixtures)` → runs crawler on stored HTML, compares to expected JSON with volatile tolerance → pass fraction.
- `history_crosscheck(items, history_rows, schema_ref)` → flags volatile fields that jumped beyond a tolerance band vs last-known-good (the VAT-shift alarm); returns warnings (non-fatal at runtime, fatal in gauntlet if severe).

**Step 3: Implement** with a shared `field_equal(a, b, volatile: bool)` helper (DRY — used by gauntlet too). **Step 4:** PASS. **Step 5: Commit** `feat: validator gates 3-5 (oracle/fixture/history)`.

---

## Milestone 8 — Failure classifier + T2 LLM fallback

### Task 8.1: Failure classifier

**Files:** Create `crawloop/loop/trigger.py`; Test `tests/test_classifier.py`.

**Step 1: Failing test** (§8 table) — `classify(exc_or_report)` maps: `FetchError`/timeout/5xx → `TRANSIENT`; `AllVersionsFailed`/schema-fail/empty/fill-collapse → `DRIFT`; `FetchBlocked(429)` → `BLOCKED_RATE`; `FetchBlocked(401|403)` → `BLOCKED_AUTH`; challenge/captcha marker → `BLOCKED_CHALLENGE`; 404/410/soft-404 → `GONE`.

**Step 3: Implement** as a pure function returning a `FailureClass` enum. Soft-404 = 200 + body matches configurable "not found" markers. **Step 4:** PASS. **Step 5: Commit** `feat: failure classifier`.

### Task 8.2: Completer abstraction + litellm impl

**Files:** Create `crawloop/llm.py`; Test `tests/test_completer.py`.

**Step 1: Failing test** — `FakeCompleter(["hello"])` returns canned text; `Completer` protocol has `async def complete(self, *, system, user, model) -> str`. (Real `LiteLLMCompleter` wraps `litellm.acompletion`; not unit-tested against network.)

**Step 3: Implement** protocol + `FakeCompleter` (records calls, pops responses) + `LiteLLMCompleter`. Config maps roles → model ids (`codegen_model="anthropic/claude-fable-5"`, `oracle_model` same, `judge_model="anthropic/claude-haiku-4-5"`). **Step 4:** PASS. **Step 5: Commit** `feat: Completer abstraction (fake + litellm)`.

### Task 8.3: T2 direct extraction (also the oracle)

**Files:** Create `crawloop/fallback.py`, `prompts/extract.j2`; Test `tests/test_fallback.py`.

**Step 1: Failing test** — `direct_extract(html, schema_ref, completer)` builds the prompt from `prompts/extract.j2` + `schema_json(ref)`, calls completer, parses JSON, validates via Gate 1, returns items. `FakeCompleter` returns the known 3-book JSON → items validate. Malformed JSON → one repair retry (feed the error back), then raise.

**Step 3: Implement.** Prose prompt lives in `prompts/extract.j2` (per house rule: no long strings inline). Trim HTML to ~5KB keeping id/class/data-*/aria (shared `trim_html()` util in `crawloop/htmlutil.py`, its own task if it grows). **Step 4:** PASS. **Step 5: Commit** `feat: T2 LLM direct extraction + oracle`.

---

## Milestone 9 — The Loop (extraction regeneration)

### Task 9.1: Sampler

**Files:** Create `crawloop/loop/sampler.py`; Test `tests/test_sampler.py`. `collect_samples(family, ctx, registry, n=3)` → ≥3 fresh HTMLs (from `run_history` URLs, else follow listing links). Test against fixture server. Commit `feat: loop sampler`.

### Task 9.2: Codegen

**Files:** Create `crawloop/loop/codegen.py`, `prompts/codegen.j2`; Test `tests/test_codegen.py`.

**Step 1: Failing test** — `generate_candidates(samples, oracle_jsons, schema_ref, prev_source, failure_report, completer, k=2)` builds the §9-step-3 prompt and returns `k` source strings (FakeCompleter returns a valid books crawler). Each returned source passes `ast_check`.

**Step 3: Implement.** Prompt template in `prompts/codegen.j2` includes: contract source, `schema_json`, trimmed samples, oracle JSONs, previous version + failure report. Parse fenced ```python blocks from the completion. **Step 4:** PASS. **Step 5: Commit** `feat: loop codegen`.

### Task 9.3: Sandboxed candidate execution

**Files:** Create `crawloop/loop/sandbox.py`, `crawloop/loop/_runner.py`; Test `tests/test_sandbox.py`.

**Step 1: Failing test** — `run_in_sandbox(source, html, timeout=30)` AST-checks, then runs the crawler in a subprocess (`_runner.py` reads source+html from stdin, executes `crawl` with an offline `ctx` whose `fetch` just returns the provided html, prints items JSON), returns items; an infinite-loop source → `SandboxTimeout`; AST-bad source → `ASTViolation` (never spawns).

**Step 3: Implement** via `subprocess.run([sys.executable, _runner.py], input=..., timeout=...)` with the offline ctx. This is §6a rung 3. **Step 4:** PASS. **Step 5: Commit** `feat: subprocess sandbox for candidate crawlers`.

### Task 9.4: Gauntlet (scoring + acceptance)

**Files:** Create `crawloop/loop/gauntlet.py`; Test `tests/test_gauntlet.py`.

**Step 1: Failing test** — `score_candidate(source, samples, oracle_jsons, fixtures, schema_ref)` → `CandidateScore(schema_valid, oracle_agreement, fixture_pass, exec_errors)`. `run_gauntlet(candidates, ...)` returns the best candidate meeting the §9 bar (100% schema-valid, ≥0.98 oracle agreement, 100% fixture pass) or `None`. A candidate that reads the wrong element (low agreement) is rejected; the correct one wins.

**Step 3: Implement** — sandbox-run each candidate on each sample, validate (Gate 1), compare to oracle (Gate 3), regress on fixtures (Gate 4), aggregate. LLM is NOT consulted here (deterministic decision per §2). **Step 4:** PASS. **Step 5: Commit** `feat: gauntlet scoring + acceptance bar`.

### Task 9.5: Promote + the full Loop driver

**Files:** Create `crawloop/loop/promote.py`, `crawloop/loop/driver.py`; Test `tests/test_loop_driver.py`.

**Step 1: Failing test** — end-to-end with `FakeCompleter` scripted to return a correct crawler: `run_loop(family, ctx, registry, completer, schema_ref)` → SAMPLE→ORACLE→CODEGEN→GAUNTLET→PROMOTE → registry has new active version, audit entry written, fixtures refreshed. Scripted to return only bad candidates for 3 rounds → family `escalated`, no promotion.

**Step 3: Implement** the driver wiring 9.1–9.4 + `promote()` (registry `add_version`+`set_active`, fixture refresh from agreed samples, audit). Retry loop with failure report appended; 3-round cap → escalate. New-family bootstrap = same driver with `prev_source=None`. **Step 4:** PASS. **Step 5: Commit** `feat: full extraction Loop driver`.

---

## Milestone 10 — Router + Engine

### Task 10.1: Router

**Files:** Create `crawloop/router.py`; Test `tests/test_router.py`. `route(url, registry)` → matching family by `url_patterns` regex, or `None` (→ new-family flow). Commit `feat: URL-regex router`.

### Task 10.2: Engine orchestration (`request()`)

**Files:** Create `crawloop/engine.py`; Test `tests/test_engine.py`.

**Step 1: Failing test** — `Engine.request(url, schema=None)`:
- known family, normal → fast path items, no LLM (assert `FakeCompleter` uncalled).
- known family, drift (fixture mutated, single version) → T2 fallback returns items NOW + a Loop job is enqueued (assert).
- blocked fixture → Access Recovery runs, then succeeds.
- unknown url + schema given → new-family bootstrap.

**Step 3: Implement** the §8 flow: route → `run_family` → on `AllVersionsFailed` classify → DRIFT: T2 + enqueue loop; BLOCKED\*: access recovery then retry; TRANSIENT: retry; GONE: mark. Loop runs via an injected executor (in tests, run synchronously; in CLI, background task). **Step 4:** PASS. **Step 5: Commit** `feat: engine orchestration (request path)`.

---

## Milestone 11 — CLI + flagship E2E

### Task 11.1: CLI

**Files:** Create `crawloop/cli.py`; Test `tests/test_cli.py`. Commands: `crawl <url> [--schema]`, `family list|show <family>`, `loop run <family>`, `access status`, `audit <family>`. Thin argparse over `Engine`/`Registry`. Smoke-test `crawl` against fixture server with a pre-seeded crawler. Commit `feat: CLI`.

### Task 11.2: Flagship self-heal + access-recovery E2E

**Files:** Create `tests/test_selfheal_e2e.py`, `tests/cassettes/*.json`; Test = itself.

**Step 1: Write the test** (the product proof, deterministic):

```
1. Bootstrap family on fixture server (normal) via run_loop with cassette-backed completer → assert v1 extracts the 3 books correctly.
2. Flip server mode="mutated" (renamed classes) → Engine.request → v1 fails validation → T2 fallback (cassette) returns correct 3 books NOW (assert) → run_loop → v2 promoted (assert active==2, audit has promote).
3. Engine.request again (still mutated) → fast path via v2, no LLM call (assert completer uncalled this step), correct data.
4. Flip server mode="blocked" → Engine.request → Access Recovery applies bypass_token, gets through, saves winner (assert domain_access.working_strategy) → correct data.
5. Assert audit.jsonl integrity (bootstrap + promote + access events).
```

**Step 2:** Run → drive to green. Record cassettes by running once against a real model behind an env flag (`RECORD=1`), then commit the JSON so CI replays offline.

**Step 3:** `pytest tests/test_selfheal_e2e.py -v` → PASS with no network. **Step 4: Commit** `test: flagship self-heal + access-recovery E2E`.

### Task 11.3: README + run docs

**Files:** `README.md`. Document: install, `authorized_domains.yaml`, env vars (`ANTHROPIC_API_KEY`, per-domain creds), `crawloop crawl …`, the authorized-scope/ethics note (allowlist, robots toggle, captcha opt-in) from §6. Commit `docs: README + usage`.

---

## Done = POC acceptance

- `pytest` green, `ruff` clean.
- Flagship E2E proves: bootstrap → break layout → fallback serves → Loop promotes v2 → fast path reuses → blocked → access recovery heals — all offline.
- Every generated crawler passes the AST gate at write AND load.
- No unit test touches network or a real model.

## Deferred to Phase 2 (NOT in this plan)

T1 fingerprint healing · DOM-shingle routing + structural drift alarm · production LLM-judge sampling · distribution monitors + canary scheduler · captcha-solver provider · proxy rotation · dashboard · Postgres · list+detail two-step crawls.
