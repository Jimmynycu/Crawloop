<div align="center">

<img src="assets/hero.svg" alt="crawloop — compile your LLM scraper into free, deterministic, self-healing code" width="100%">

<br>

<!-- badges -->
[![CI](https://github.com/Jimmynycu/Crawloop/actions/workflows/ci.yml/badge.svg)](https://github.com/Jimmynycu/Crawloop/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-532%20passing-brightgreen.svg)](#-30-second-quickstart-no-api-key)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](#-contributing)

<br>

### Compile your LLM scraper into free, deterministic, self-healing code.

Point it at a page and a schema. It generates a **cheap deterministic crawler**, serves data **instantly via an LLM** the moment a redesign breaks it, and **regenerates a fresh crawler in the background** — so you pay the LLM once, not on every page forever.

</div>

> [!IMPORTANT]
> **Status — honest:** working proof of concept, not a production scraper. The full self-heal + access-recovery loop is proven end-to-end **offline** (zero API key). The architecture wins by construction on **cost, latency, determinism, and drift-resilience** (see [the design tradeoff](#-the-design-tradeoff)) and is exact on the high-value core fields it compiles; reproducing a *wide, normalized* schema with deterministic code is the hard, still-open step, and when a family can't be compiled to the bar it safely falls back to the LLM — never worse on output. We'd rather you know that going in.

<br>

<div align="center">

[**30-second quickstart**](#-30-second-quickstart-no-api-key) · [**How it works**](#-how-it-works) · [**See it heal**](#-see-it-heal) · [**The tradeoff**](#-the-design-tradeoff) · [**Features**](#-features) · [**Roadmap**](#-roadmap)

</div>

---

## 🤔 Why this exists

LLM-per-page scrapers are seductive — point a model at HTML, get JSON. But in production they have **two structural problems that never go away**:

- **You pay per page, forever.** Every page, every re-crawl, every run hits the model. At a few cents a page that's real money at scale — and unlike code, the bill never amortizes. Crawl a million pages and you pay a million times.
- **They break silently.** When a site redesigns, an LLM doesn't *know* it broke. It confidently extracts the wrong thing (or nothing) at the same hardcoded confidence score. There is no drift signal — you find out from downstream garbage, days later.

crawloop flips the model. **The LLM is a compiler and a teacher, not a runtime.** It writes a deterministic crawler once, acts as the oracle that grades regenerated versions, and steps in *only* during a breakage to serve data while a fresh crawler is built. Steady state runs on free, instant, byte-reproducible code.

> **The contract, in five words: _serve now, heal in the background._**

---

## ⚡ 30-second quickstart (no API key)

The flagship demo is the **complete self-heal cycle running entirely offline** — a scripted model and a localhost fixture server, so it needs **no `ANTHROPIC_API_KEY` and no network**. It is the proof that the whole loop works.

```bash
git clone https://github.com/Jimmynycu/Crawloop.git
cd Crawloop
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Watch the full break → serve → regenerate → reuse → recover cycle, offline:
python examples/selfheal_demo.py
```

That narrated demo — and the matching end-to-end test ([`tests/test_selfheal_e2e.py`](tests/test_selfheal_e2e.py)) — drives the **real engine** through:

| Step | What happens | Cost |
|------|--------------|------|
| 1 · **Fast path** | a healthy generated crawler extracts records with no LLM call | **$0** |
| 2 · **Break** | the fixture site's layout is mutated (a simulated redesign) | — |
| 3 · **Serve now** | drift is detected; the page is served *immediately* via the T2 LLM fallback against the schema | paid, once |
| 4 · **Heal** | the Loop samples pages, uses the LLM as an oracle, gauntlet-scores candidates, and **promotes a v2** | paid, once |
| 5 · **Reuse** | the next request runs the healed crawler | **$0 again** |
| 6 · **Recover** | a 403 block is hit, the per-domain access ladder escalates, gets through, and **saves the winning strategy** | — |

```bash
python -m pytest   # the full suite — 532 tests, all offline, all without a key
```

> [!NOTE]
> Real runs (against your own authorized sites) need an API key for the T2 fallback and the Loop. The demo above proves the machinery first, for free.

---

## ⚙️ How it works

A request flows through a **version ladder of cheap deterministic crawlers** first; the LLM is only ever reached on a real breakage.

<div align="center">

<img src="assets/how-it-works.svg" alt="How crawloop routes a request: authorize, route to a page family, run its version ladder of generated crawlers; on failure, classify and either serve via the LLM while regenerating, or escalate the access ladder" width="100%">

</div>

**Authorize** (allowlist gate) → **route** to a registered page *family* → run that family's **version ladder** of generated crawlers (the cheap, fast path). If a version validates, items are served with **no LLM call**.

If every version fails, the failure is **classified**:

- **Drift** → served *now* by **T2** (the LLM reading the HTML against the schema) while the **regeneration Loop** rebuilds a crawler in the background.
- **Block** (429 / login wall / anti-bot) → the **access-recovery** ladder escalates and retries.
- **Transient** error → retried.
- **Gone** (404/410) → stops.

> 📐 **See the full runtime architecture → [docs/design.html#arch](docs/design.html#arch)** — tiers T0/T1/T2/Loop/Access, the two self-healing loops, and the safety model.

---

## 🔧 See it heal

<div align="center">

<img src="assets/demo.svg" alt="crawloop self-heal demo: break the layout, serve through it, regenerate, run free again" width="100%">

</div>

> Run it yourself in ~30s, no API key: **`python examples/selfheal_demo.py`** — the real engine drives the full cycle (a committed cassette stands in for the LLM).

```
  ┌─────────────┐   site redesigns    ┌──────────────────────┐
  │  v1 crawler  │  ───────────────▶   │  drift detected      │
  │  (free, fast)│   layout mutates    │  validation fails    │
  └─────────────┘                      └──────────┬───────────┘
                                                   │
                         ┌─────────────────────────┼─────────────────────────┐
                         ▼                          ▼                         │
                ┌─────────────────┐      ┌────────────────────┐              │
                │ SERVE NOW (T2)  │      │ HEAL (background)   │              │
                │ LLM reads HTML  │      │ Loop: oracle →      │              │
                │ → data this     │      │ codegen → gauntlet  │              │
                │ request, paid   │      │ → promote v2        │              │
                └─────────────────┘      └─────────┬──────────┘              │
                                                    │  v2 promoted            │
                                                    ▼                         │
                                          ┌────────────────────┐             │
                                          │  v2 crawler         │ ◀───────────┘
                                          │  FREE again, $0     │   every page after
                                          └────────────────────┘
```

The break is **never an outage and never silent**: the user keeps getting data (paid, briefly), the system *knows* it drifted, and within minutes it's back to free deterministic extraction — automatically, with every promotion written to an audit trail.

---

## 📊 The design tradeoff

The table below contrasts the two **architectures** — not a benchmark, no measured numbers from any system. It is the structural argument for compiling a crawler instead of calling an LLM on every page; the axes follow directly from *"code runs vs a model runs"* and from *"the system has a drift signal vs it doesn't."*

| Dimension | crawloop | Generic LLM-per-page | Why |
|---|---|---|---|
| **Cost model** | compile **once**, then run free | a model call on **every page, forever** | the LLM bill amortizes for crawloop, never for per-page |
| **Latency** | code (parsel) — local, no round-trip | an LLM round-trip per page | deterministic code has no network step in steady state |
| **Determinism** | byte-identical output for the same page | may vary run-to-run | code is deterministic; sampling is not |
| **Drift handling** | detects validation drift → self-heals | no signal; ships wrong data blind | crawloop validates each extraction and knows when it broke |
| **Worst case** | safely falls back to the LLM = parity | — | a family it can't compile is served by the LLM, never worse |

This is the **design tradeoff**, stated up front, not a measured comparison: the win is structural (one-time compile vs per-page-forever, code vs round-trip, deterministic vs variable, self-healing vs silent).

> **Honest counterpoint:** reproducing a *wide, normalized, deeply-nested* schema with deterministic code is the hard, still-open step — and when crawloop can't compile a family to the bar, it falls back to the LLM, spending only the one-time bootstrap.

---

## ✨ Features

<table>
<tr>
<td width="50%" valign="top">

#### 💸 Compile-once extraction
LLM-generated deterministic Python crawlers; steady-state runs at **$0 and milliseconds** per page.

</td>
<td width="50%" valign="top">

#### 🔧 Self-healing on drift
Layout change → instant LLM fallback **+** background regeneration of a new crawler version. No outage, no silence.

</td>
</tr>
<tr>
<td width="50%" valign="top">

#### 🪜 Version ladder, not overwrite
Each family keeps an ordered `v1, v2, v3…` of immutable crawlers; healing *appends* a version and flips the active pointer (handles gradual redesigns & A/B layouts), with one-command rollback.

</td>
<td width="50%" valign="top">

#### 🔓 Access recovery
A 429 / login wall / anti-bot block isn't terminal: an ordered, per-domain ladder (backoff → stealth browser → session → bypass token) escalates until one gets through, and the **winning strategy is saved** and reused.

</td>
</tr>
<tr>
<td width="50%" valign="top">

#### 🛡️ Hard allowlist, enforced on every hop
No URL outside [`authorized_domains.yaml`](authorized_domains.yaml) can ever be fetched; cross-host/SSRF redirects are refused.

</td>
<td width="50%" valign="top">

#### 📦 Generated code is sandboxed
Every candidate crawler is **AST-checked** (import/call allowlist, no dunder escapes) and run in a resource-capped subprocess before it can ever touch a real page.

</td>
</tr>
<tr>
<td width="50%" valign="top">

#### 🧩 Pluggable Pydantic schemas
Drop a `BaseModel` in [`schemas/`](schemas/); it's auto-registered as `Name@1`. Mark `VOLATILE` fields so the validator compares price/stock tolerantly.

</td>
<td width="50%" valign="top">

#### 🧾 Full audit trail
Every promotion and access recovery is recorded (SQLite + `audit.jsonl`): what the system did, and why, reviewable after the fact.

</td>
</tr>
<tr>
<td width="50%" valign="top">

#### 🔌 Provider-agnostic
Model calls go through [litellm](https://github.com/BerriAI/litellm); codegen/oracle/judge model ids are config-swappable.

</td>
<td width="50%" valign="top">

#### ✅ Proven offline
532 tests, a scripted model, and a controllable fixture server: the whole loop is exercised with **no API key and no network**.

</td>
</tr>
</table>

---

## 📦 Install

Requires **Python 3.12+**.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest  # 532 tests, no API key needed
```

The access ladder's browser rungs use a real `PlaywrightBrowserRunner` / `StealthBrowserRunner` ([`crawloop/browser.py`](crawloop/browser.py)) that **re-enforces the allowlist on every navigation and in-page redirect** (the browser bypasses the guarded HTTP client, so it gates itself). Install the browser binaries once: `playwright install`. Gated live browser tests are in [`tests/test_browser_live.py`](tests/test_browser_live.py) (`RUN_BROWSER_TESTS=1`).

#### 🔑 Environment variables

No secret is ever stored in the repo or config; the config only *names* the env var to read.

- **`ANTHROPIC_API_KEY`** (or your provider's key) — required for *real* runs (T2 fallback + the Loop call the model via litellm). Not needed for the test suite or for `--offline` on a healthy family.
- **Per-domain credentials / tokens** — named by the `*_env` fields in `access_strategies` (e.g. `session` → `creds_env`, `bypass_token` → `value_env`, `proxy_env`), read from the environment at fetch time.

---

## 🚀 CLI usage

Installed as `crawloop` (entry point `crawloop.cli:main`). Global options (`--config`, `--db`, `--crawlers-dir`, `--fixtures-dir`) default to `authorized_domains.yaml` and a local `.crawloop/` working dir.

```bash
# Crawl one URL through the full engine (authorize → route → ladder → heal):
crawloop crawl https://books.toscrape.com/catalogue/page-1.html --schema Product@1

# Same, machine-readable:
crawloop crawl https://books.toscrape.com/catalogue/page-1.html --schema Product@1 --json

# Inspect the registry:
crawloop family list
crawloop family show books.toscrape.com/product_list

# Run the regeneration loop by hand (seeds = pages to sample):
crawloop loop run books.toscrape.com/product_list \
    https://books.toscrape.com/catalogue/page-1.html \
    https://books.toscrape.com/catalogue/page-2.html

# Inspect the per-domain access store and the audit trail:
crawloop access status
crawloop audit                                    # all events
crawloop audit books.toscrape.com/product_list    # one family

# Add --offline to forbid constructing a real model/browser (a healthy family's
# fast path needs neither; a drift/bootstrap then fails loudly, not over the network):
crawloop crawl https://books.toscrape.com/catalogue/page-1.html --offline
```

`--schema` is required only for a **new** (unrouted) URL the engine has to bootstrap from; a known family uses its stored schema.

### Adding a schema

Output schemas are plain Pydantic models contributed as `.py` files in [`schemas/`](schemas/). Drop in a `BaseModel` subclass; it's auto-discovered and registered under `f"{ClassName}@1"`. Use `extra="forbid"` so unexpected keys are caught, and declare `VOLATILE` for fields that change often so the validator compares them tolerantly:

```python
# schemas/product.py
from typing import ClassVar
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class Product(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    price: Decimal = Field(gt=0, lt=1_000_000)
    currency: str = Field(pattern=r"^[A-Z]{3}$", default="GBP")
    in_stock: bool
    url: HttpUrl
    image_url: HttpUrl | None = None
    VOLATILE: ClassVar[set[str]] = {"price", "in_stock"}
```

### `authorized_domains.yaml` — the hard allowlist + crawl policy

This file is the operator's explicit, mandatory allowlist. **Every fetch must pass `AppConfig.assert_authorized`**, so a URL whose host isn't listed can never be requested. It also carries per-domain policy: rate limit, JS rendering, and the ordered **access strategies** tried when a block is hit.

```yaml
respect_robots: false          # POC default (see Authorized use). Flip to honor robots.txt.

domains:
  - domain: books.toscrape.com
    max_rps: 1.0
    render_js: false

  - domain: shop.example.com
    max_rps: 0.5
    render_js: true
    note: "owned by us; authorized 2026-06-12"
    access_strategies:         # ordered ladder; recovery walks it and persists the winner
      - backoff                #   plain GET with exponential backoff on 429/5xx
      - stealth_browser        #   patched headless browser render
      - session: { login_url: "https://shop.example.com/login", creds_env: "EXAMPLE_LOGIN" }
      - bypass_token: { header: "x-waf-bypass", value_env: "EXAMPLE_WAF_TOKEN" }
    proxy_env: "EXAMPLE_PROXY_URL"
```

---

## 🛡️ Authorized use / ethics

This is a tool for crawling sites **you own or are explicitly authorized to crawl**. It is deliberately *not* a general-purpose scraper for sites you have no relationship with.

- **The allowlist is mandatory.** `authorized_domains.yaml` is a hard gate on every fetch (including every redirect hop — a cross-host/SSRF redirect to an unlisted host is refused). No override.
- **`respect_robots` defaults OFF** because the intended targets are owned/authorized properties. Flip it to `true` to honor `robots.txt`; decide deliberately per deployment. *(Note: the flag is parsed but not yet enforced — see the roadmap.)*
- **The CAPTCHA rung is opt-in and authorized-domains-only.** The system never auto-defeats a captcha: `captcha_solver` raises unless an operator has explicitly set `authorized: true` for that domain *and* wired a provider (none ships here). Stealth browser and bypass tokens are likewise explicit per-domain opt-ins — courtesy headers and rate limits are the default, not evasion.
- **Audit trail.** Every promotion and access recovery is recorded so what the system did, and why, is reviewable.

> ⚠️ If you would not be comfortable explaining a crawl to the site's owner, it does not belong on the allowlist.

---

## 🗺️ Roadmap

Stated candidly — these are the gaps between *"promising POC"* and *"drop-in replacement."*

**🚧 Current blocker**

- **Oracle reliability on huge JSON islands** — the bootstrap oracle (the LLM "teacher") returns empty too often when it has to read a 100K+ minified `__NEXT_DATA__` blob (a record buried tens of thousands of bytes deep in a six-figure-byte island), which prevents the loop from promoting + tail-filling end-to-end on those sites. Smarter JSON slicing for the oracle is the next thing to harden — it's what unblocks the hybrid's live completeness demo.

**✅ Recently landed** (built this cycle, tests green)

- **Core-deterministic + LLM-tail hybrid** ([`crawloop/hybrid.py`](crawloop/hybrid.py)) — the deterministic crawler fills the core for free; one small LLM call fills only the residual fields it leaves blank (**$0 when there are none**), merged into a complete record. Mechanism proven offline; live demo on giant-JSON sites awaits the oracle-reliability fix above.
- **Real `BrowserRunner`** ([`crawloop/browser.py`](crawloop/browser.py), Playwright + Patchright) — the `browser`/`stealth_browser` rungs and JS-rendered pages work, with the allowlist re-enforced on every navigation/redirect (verified by live browser tests).
- **Wheel packaging** — a clean wheel now ships every subpackage (incl. `crawloop.loop`).

**🔭 Next up**

- **JSON-first codegen** — try a page's embedded JSON island (`ld+json` / `__NEXT_DATA__`) before DOM selectors. On sites that ship a complete JSON island this gives 100%-deterministic extraction; generalizing it should rescue harder families.
- **Enforce `respect_robots`** — the flag is parsed but currently has no downstream effect.
- **Schema-width-aware defaults** — so the promote bar and HTML trimming don't need per-target hand-tuning.
- **PyPI publish & live-model smoke test** — a clean wheel now builds and ships every subpackage (verified incl. `crawloop.loop`); what remains is publishing to PyPI and a real-model smoke test (the LLM path is currently exercised via a scripted stub).

**🚫 Intentionally out of scope** for this POC (Phase 2): non-LLM fingerprint healing (T1), DOM-shingle family routing + structural-drift early alarm, sampled production LLM-judge, distribution monitors + scheduled canaries, a web dashboard, Postgres, and concurrency hardening.

---

## 🤝 Contributing

PRs welcome — especially the open roadmap items above (**oracle reliability on huge JSON islands** and **JSON-first codegen robustness** are the highest-impact right now). Good first steps:

1. Fork, branch, and `pip install -e ".[dev]"`.
2. Run `python -m pytest` (532 tests, no key needed) and `ruff check .` — both must stay green.
3. Add tests for your change; the offline fixture server (`tests/fixture_server/`) lets you exercise the full loop deterministically.
4. Open a PR describing the behavior change and how you verified it.

Found a bug or have a design question? Open an issue. If you're reporting an extraction gap, a link to the page (one you're authorized to share) and the schema helps enormously.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide.

---

## 📄 License

Released under the **Apache License 2.0** — see [LICENSE](LICENSE).

<div align="center">
<br>
<sub>Built to demonstrate the self-heal + access-recovery loop on sites you own or are authorized to crawl. If this saved you from an LLM bill that never ends, a ⭐ helps others find it.</sub>
</div>
