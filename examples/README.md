# Examples

Runnable demos you can try in under a minute. **No API key, no network, no setup beyond `pip install`.**

---

## 🩹 `selfheal_demo.py` — watch a crawler heal itself

The one-command "wow": a website gets redesigned out from under a crawler, and you watch the system **keep serving correct data through the breakage, write its own replacement, and go back to running for free** — start to finish, with no human and no real LLM.

### Run it

```bash
pip install -e ".[dev]"
python examples/selfheal_demo.py
```

That's the whole setup. It runs offline against a tiny local web server and a committed "cassette" of canned model responses, so it's fully deterministic and needs no API key. (Already installed the project? Just run the second line.)

### What it shows

The script narrates the full self-heal cycle as five steps:

| Step | What happens | The point |
|------|--------------|-----------|
| **1. Bootstrap** | A registered crawler (v1) serves a books listing | The happy path costs **zero** model calls — it's free, deterministic code |
| **2. Mutate** | The local site is silently redesigned: every CSS hook is renamed | v1 still "runs" but now extracts **nothing** — the classic silent scraper outage |
| **3. Detect** | The next request notices the drift | Instead of returning junk, it's classified as a layout change |
| **4. Regenerate** | An LLM serves correct data **now** (the `fallback` tier), and *in the same request* a loop writes, sandboxes, validates, and **promotes** a new crawler (v2) | The caller never sees an outage; the fix needs **no human** |
| **5. Free again** | The next request is served by the healed v2 | Back to **zero** model calls — and v2 even paginates, so it gathers all four books |

The headline numbers the demo prints: **0 model calls** when healthy → **6 calls** during the one self-heal (1 to serve now + 3 to learn the page + 2 to write the crawler) → **0 model calls** again forever after. You pay the model once, not on every page.

### Sample output

```text
======================================================================
  STEP 1  BOOTSTRAP — a crawler is live and serving for free
======================================================================
  ...
  Records returned:
    - A Light in the Attic  £51.77   in stock
    - Soumission            £50.10   OUT of stock
    - Tipping the Velvet    £53.74   in stock

  source       = registry   (straight from the registry)
  used_version = 1
  model calls  = 0   <- ZERO. The happy path is free.

======================================================================
  STEP 2  MUTATE — the website is redesigned overnight
======================================================================
  Same books, same prices — but every CSS hook the crawler relied
  on has been renamed by the redesign:

    article.product_pod  ->  div.card
    p.price_color        ->  span.price-box
    p.availability       ->  span.stock
  ...

======================================================================
  STEP 3  DETECT + REGENERATE — heal without an outage
======================================================================
  ...
  Records returned (DURING the redesign, with no working crawler):
    - A Light in the Attic  £51.77   in stock
    - Soumission            £50.10   OUT of stock
    - Tipping the Velvet    £53.74   in stock

  source = fallback   <- an LLM read the raw HTML and served NOW
  reason = drift->fallback
  ...
    loop.ok      = True
    loop.reason  = promoted
    new version  = v2   (now the active crawler)

  Audit trail (what actually changed, recorded for you):
    active version on the ladder : v2
    'promote' audit entries      : 1  (to v2)
    golden fixtures written      : 3
    model calls this request     : 6  (1 to serve now + 3 to learn + 2 to write the crawler)

======================================================================
  STEP 5  FREE AGAIN — the healed crawler serves the new layout
======================================================================
  ...
  Records returned:
    - A Light in the Attic  £51.77   in stock
    - Sharp Objects         £47.82   in stock
    - Soumission            £50.10   OUT of stock
    - Tipping the Velvet    £53.74   in stock

  source       = registry   (back to the registry)
  used_version = 2   <- the self-written crawler
  model calls  = 0   <- ZERO again. Healed and free.
```

(Your run also prints the intro and a closing before/after summary; the listing URL's port is random per run.)

### How it stays honest (no real model, no network)

This demo runs the **real** engine, registry, executor, sandbox, and validator. The *only* substitution is the language model:

- **The local "website"** is `tests/fixture_server` — a localhost HTTP server that serves the same four books under two CSS layouts and flips between them at runtime (that's the "redesign"). The demo imports it directly rather than copying it.
- **The "LLM"** is a `FakeCompleter` that replays a committed cassette of canned responses (`tests/cassettes/selfheal.json`), exactly the offline pattern used by the end-to-end test. The cassette is hand-authored and checked in; the only thing substituted at runtime is the fixture server's random port.

In other words, this is the same machinery proven by **`tests/test_selfheal_e2e.py`**, just driven as a narrated story instead of as assertions. If you want the assertion-backed version, run:

```bash
pytest tests/test_selfheal_e2e.py -v
```

### Notes

- **Deterministic & re-runnable.** Each run uses a fresh temp directory for generated crawlers and golden fixtures, and cleans it up on exit — so the output is identical every time and nothing is left behind.
- **Want to see the generated crawler?** In `selfheal_demo.py`, comment out the `shutil.rmtree(work, ...)` cleanup line in `_run()` and re-run; the self-written `v2.py` and the golden fixtures will be left in the printed temp directory for inspection.
