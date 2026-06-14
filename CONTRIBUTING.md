# Contributing to crawloop

Thanks for your interest. This is a proof-of-concept for a self-healing,
schema-driven web crawler. Contributions that keep it small, safe, and well-tested
are very welcome.

By contributing you agree your contributions are licensed under the project's
[Apache-2.0 license](LICENSE).

## Ground rules

This project only crawls sites you **own or are explicitly authorized** to crawl.
The domain allowlist (`authorized_domains.yaml`) is a hard gate, not a suggestion.
Do not add code, tests, or fixtures that fetch arbitrary third-party sites, and do
not weaken the allowlist or the AST gate to make something convenient.

## Development setup

Requires **Python 3.12+** (CI runs 3.12 and 3.13).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Before you push

Both of these run in CI on every push and PR — run them locally first:

```bash
ruff check .
pytest -q
```

The full suite is offline and deterministic: it must pass with **no network access
and no real LLM calls**.

## Testing conventions

- **No network, no real models.** Use the in-repo fixture server
  (`tests/fixture_server/`, exposed via the `fixture_server` fixture in
  `conftest.py`) for HTTP, and `crawloop.llm.FakeCompleter` anywhere an LLM
  would be called. `respx` is available for stubbing `httpx`.
- **Determinism.** Anything that stamps a timestamp accepts an injectable `now`;
  pin it in tests. Inject backoff/sleep so tests are instant.
- **Cover the gate, not just the happy path.** Changes to the validator, the AST
  gate, the access ladder, or the loop should add tests for the failure/escalation
  branches too.

## Code style

- `ruff` is the linter (line length 100); keep `ruff check .` clean.
- Match the surrounding style. Modules carry a top-of-file docstring explaining
  *why* the module exists and how it fits the runtime flow — keep that up to date
  when you change behavior.
- Prefer extending an existing module over adding a parallel one for the same job.

## Touching the safety-sensitive parts

These are trust boundaries. Changes here get extra scrutiny and **must** include
tests:

- `crawloop/safety.py` — the AST allowlist that gates every generated crawler
  before it runs. Loosening `ALLOWED_IMPORTS` or the banned-builtins set reopens
  code-execution / network-egress holes. Justify any such change explicitly in the
  PR.
- `crawloop/access.py` and the recovery loop — all fetching is allowlist-gated
  and per-domain rate-limited. Don't add a path that bypasses `FetchContext`.
- `crawloop/registry.py` — generated source is AST-gated *before* it is written
  to disk and integrity-checked (sha256) at load. All SQL binds parameters with
  `?`; never interpolate values into SQL.

Never commit credentials, API keys, or proxy URLs. The production LLM adapter reads
`ANTHROPIC_API_KEY` from the environment via `litellm`; secrets stay in your env,
not the repo.

## New output schemas

Output schemas are plain Pydantic models dropped into `schemas/` (see
`schemas/product.py` for the shape, including the `VOLATILE` class var). The
registry discovers them automatically and versions them as `Name@1`.

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the high-level map, the full
[docs/design.html](docs/design.html) for the system design, and
[docs/plans/](docs/plans/) for the build plan.

## Pull requests

Branch off `main`, keep PRs focused, and fill out the PR template (it includes the
safety-invariant checklist). Open an issue first for anything large or behavior-changing
so we can agree on the approach.
