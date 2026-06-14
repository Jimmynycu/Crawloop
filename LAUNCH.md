# LAUNCH.md — crawloop launch plan

A concrete plan to maximize the star spike on launch day, grounded only in this
repo's actual state: the offline self-heal demo and the architectural cost argument.
No measured comparison to any specific system appears here — every claim below is
either something you can watch run offline, or a structural property of compiling a
crawler instead of calling an LLM on every page.

**North star for this launch:** convert the *self-heal + compile-once-run-free* story
into stars, while staying honest about what it is — a POC that wins by construction on
cost/latency/determinism/drift-resilience and is exact on the high-value core fields
it compiles, and does **not yet** reproduce a wide, normalized schema with
deterministic code (it falls back to the LLM there — never worse output). Over-claiming
gets punished on HN/r/programming and torches the spike; under-claiming wastes a
genuinely strong hook. The line we walk: *"strong architecture, honest POC, see for
yourself — it heals offline, no API key."*

---

## 0. The honest core (read this first — every asset below is downstream of it)

What is true today, from this repo:

- **Self-heal works end-to-end, offline, deterministically.** `examples/selfheal_demo.py`
  (and the matching `tests/test_selfheal_e2e.py`) drive the *real* engine (registry,
  executor, validator, sandbox, access-recovery) with the only fake being a scripted
  model — **no API key, no network beyond localhost**. It shows the whole cycle: fast
  path (no LLM) → layout breaks → LLM fallback serves *now* → background Loop promotes a
  v2 → next request uses the healed v2 (no LLM) → a fetch is blocked → access-recovery
  clears it → audit trail records both events.
- **The cost model is the structural win.** An LLM-per-page approach calls the model on
  every page, forever; crawloop pays a one-time bootstrap to compile a crawler, then runs
  deterministic code for free per page. The LLM bill amortizes for crawloop and never
  amortizes for per-page — that is an architectural argument, not a measured number.
- **Determinism.** The same page run twice yields byte-identical output, because the
  steady-state path is plain code, not a sampled model.
- **Safety is built in, not bolted on.** Hard domain allowlist enforced on every fetch
  *and every redirect hop* (SSRF-safe), AST-check on every generated crawler before it
  runs, sandboxed candidate execution, full audit trail (SQLite + `audit.jsonl`).
- **The whole suite is offline** (scripted fake model + localhost fixture server) — no
  API key, no network.

What is **not** true today (say it before someone else does):

- It does **not** reproduce a wide, normalized schema with deterministic code as
  completely as a per-page LLM does. crawloop is exact on the core it compiles (numbers,
  identity, URLs) and falls back to the LLM for the normalized/derived tail — so it is
  never worse on output, but on those fields it is paying the LLM, not beating it.
- **The live-LLM path is exercised only via a scripted stub** in tests. Real runs need a
  key and a smoke test.
- It is a **POC, by design**, for sites **you own or are authorized to crawl** — not a
  general-purpose scraper. The allowlist is mandatory and has no override.

Everything below leads with the true parts and never hides the false ones.

---

## 1. Positioning one-liner (+ 2 alternates)

**Primary (lead with self-heal + cost model, the two strongest true claims):**

> **crawloop — a self-healing web crawler that serves data the moment a site
> redesign breaks it, then regenerates a fresh free-to-run extractor in the background.**

**Alternate A (cost-model-forward):**

> **Stop paying an LLM for every page. crawloop compiles a deterministic crawler once,
> runs it free per page, and auto-heals it with an LLM only when the site changes.**

**Alternate B (engineering-forward, for HN/r/programming):**

> **A crawler that treats a broken extractor like a cache miss: serve from the LLM now,
> regenerate deterministic code in the background, gate every fetch with an allowlist and
> every generated crawler with an AST check.**

Tagline (one line, for repo description / X bio):
*Serve now, heal in the background. Compile once, run free, self-heal on layout drift.*

---

## 2. Show HN

**Title** (HN rewards plain + specific; no hype words, no emoji):

> **Show HN: crawloop – a self-healing scraper that serves data while it regenerates**

Backups if the above feels long:
- `Show HN: A web crawler that self-heals when the site's layout changes`
- `Show HN: Serve scraped data from an LLM now, regenerate free deterministic code later`

**First comment (the honest framing — post immediately after submitting):**

> Author here. I kept hitting the same two problems with scrapers: (1) a site redesign
> silently breaks your extractor and you ship garbage until someone notices, and (2) if
> you "solve" it by having an LLM read every page, you pay per page forever and your
> output isn't reproducible.
>
> crawloop treats a broken extractor like a cache miss. A request flows: allowlist
> gate → route to a page "family" → run that family's ladder of cheap generated
> crawlers. If a crawler validates, you get the data with zero LLM calls. If they all
> fail, the failure is classified: a *layout drift* is served right now by an LLM reading
> the page against your schema, while a background loop samples a few pages, uses that
> LLM as an oracle, generates and gauntlet-scores candidate crawlers, and promotes a new
> version. A *block* (429 / login wall) escalates a per-domain access-recovery ladder.
> The contract is "serve now, heal in the background."
>
> The cost model is the real point, and it's structural rather than a benchmark: you pay
> a one-time bootstrap to compile a crawler, then run deterministic code for free per
> page — the LLM-per-page approach pays the model on every page, forever, so its bill
> never amortizes. The steady-state output is also byte-for-byte reproducible because
> it's plain code, not a sampled model.
>
> **Honest limits, up front:** this is a POC. It does *not* yet reproduce a wide,
> normalized schema with deterministic code as completely as a per-page LLM — it's exact
> on the core it compiles (numbers, identity, URLs) and falls back to the LLM for the
> normalized/derived tail, so it's never worse on output but it isn't "beating the LLM"
> there either. The live-LLM path is currently exercised via a scripted stub. It's
> deliberately allowlist-only — for sites you own or are authorized to crawl, not a
> general scraper.
>
> The self-heal cycle is real and you can watch it run offline with no API key:
> `python examples/selfheal_demo.py` (or `pytest tests/test_selfheal_e2e.py -s`). Happy
> to go deep on the loop, the gauntlet scoring, or the access-recovery ladder.

**Why this works on HN:** leads with a real problem, explains the mechanism concretely
(HN respects mechanism), states the structural win, then volunteers the limitations
before the top commenter does — which on HN reads as credibility, not weakness, and
heads off the "this is just X" pile-on.

---

## 3. Per-subreddit angle

Each subreddit gets a *different first sentence and proof*, because the audiences reward
different things. Never cross-post the same title verbatim — tailor the hook.

### r/Python
- **Angle:** clean, modern Python (3.12+, async, Pydantic schemas, parsel, AST-gated
  codegen, sandboxed subprocess). Lead with the code shape and the offline demo.
- **Title:** *"I built a self-healing crawler in Python — generated extractors that
  regenerate themselves when a site changes (runs offline, no API key)"*
- **First-paragraph proof:** "Output schemas are plain Pydantic models you drop in a
  folder; every generated crawler is AST-checked before it runs and executed in a
  sandbox; the whole self-heal cycle has an offline deterministic demo you can run with
  `python examples/selfheal_demo.py`."
- **Talking point that lands here:** the schema-as-a-`.py`-file ergonomics + `VOLATILE`
  fields for tolerant validation. Pythonistas like the API surface.

### r/webscraping
- **Angle:** the maintenance-pain solution. This sub *lives* the "site changed, my
  scraper broke at 3am" problem. That's the whole pitch.
- **Title:** *"Self-healing scraper: when the layout breaks, it serves data from an LLM
  immediately and regenerates a free deterministic extractor in the background"*
- **First-paragraph proof:** the cost model (compile once, then free per page vs paying
  the model on every page forever) + the drift story. This audience instantly gets why
  per-page-LLM is a cost trap and why selector-based scrapers rot.
- **Be ready for:** the ethics/ToS question and "show me it beating Scrapy on a real
  site." Answer with the allowlist-by-design stance and the offline demo. **Do not
  over-claim anti-bot** — the stealth/browser rung is real but should be demonstrated,
  not asserted.

### r/MachineLearning
- **Angle:** the *architecture*, framed as LLM-as-compiler / LLM-as-oracle. The
  interesting ML idea is "use the expensive model once to teach cheap deterministic code,
  cross-check with a gauntlet, only pay the model again on drift."
- **Title (use the [P] project tag):** *"[P] Using an LLM as a one-shot oracle to compile
  deterministic extractors — then only re-invoking it on distribution drift"*
- **First-paragraph proof:** the promote gates (≥3 independent oracles, per-item
  agreement, item-count match) and the drift classifier — accuracy is gated against an
  LLM oracle, so the honest framing is "match LLM-grade extraction on the compiled fields
  at a fraction of the runtime cost," not "beat LLMs."
- **Be ready for:** "this is just distillation / codegen + verification." Agree, name it,
  then point to the specifics: the gauntlet scoring, the promote gates, the version
  ladder, the access-recovery ladder.

### r/programming
- **Angle:** the systems design, told as a story. This is the most skeptical, most
  anti-marketing audience — lead with the design insight, not the product.
- **Title:** *"Treating a broken web scraper like a cache miss: serve from an LLM now,
  regenerate the fast path in the background"*
- **First-paragraph proof:** the request-flow paragraph (authorize → route → version
  ladder → classify failure → serve+heal), plus the safety posture (allowlist on every
  redirect hop, AST gate, sandbox, audit trail). Link the design doc (`docs/design.html`).
- **Be ready for:** the harshest "why not just X" and "this is overengineered" takes.
  The defense is the honest POC framing + the one concrete killer fact: it heals offline,
  deterministically, with no API key, and you can read the demo. Concede the POC limits
  fast and do not argue.

---

## 4. X / Twitter thread outline

Format: 7 tweets, one idea each, a visual on 1 / 3 / 5. Lead with the hook, the demo GIF
goes on tweet 1 (highest reach), honesty tweet near the end (it builds trust and is very
re-quotable). Keep the repo link in the **last** tweet (links suppress reach early).

1. **Hook + GIF.** "Your scraper breaks every time a site redesigns. What if it just…
   healed itself? [self-heal GIF: break → serve now → regenerate → healed]. A thread on
   crawloop 🧵"
2. **The problem, sharpened.** "Two bad options today: (a) brittle selectors that rot, or
   (b) have an LLM read every page — accurate but you pay per page *forever* and it's not
   reproducible. crawloop takes a third path."
3. **The mechanism + diagram.** "It treats a broken extractor like a cache miss: serve
   the data *now* from an LLM, regenerate cheap deterministic code in the background,
   promote it after it passes a gauntlet. [simple flow diagram]"
4. **The cost model.** "Compile once, then run free per page. The LLM-every-page approach
   pays the model on every page, forever — so its bill never amortizes and yours does.
   That's structural, not a benchmark."
5. **Determinism + proof.** "Steady state is plain code, so the same page twice is
   byte-identical — no sampling drift. You can watch the whole self-heal cycle run
   offline with no API key. [terminal recording]"
6. **Radical honesty (the trust tweet).** "Honest limits: it's a POC. It does NOT yet
   reproduce a wide, normalized schema with deterministic code as completely as a per-page
   LLM — it nails the core it compiles and falls back to the LLM for the tail (never worse
   output). Live-LLM path is stubbed in tests. Allowlist-only by design."
7. **CTA + link.** "It heals offline with no API key — `python examples/selfheal_demo.py`.
   Code + design doc: [GitHub link]. Would love feedback on the loop + gauntlet design."

Pinned-reply move: drop the asciinema link as the first reply to tweet 1 so people who
want the live terminal recording get it without leaving the thread.

---

## 5. Demo asset plan (asciinema + GIF)

The demo must show the **self-heal cycle**, because that is the unique, true,
visually-legible thing. The flagship asset is `examples/selfheal_demo.py`, which drives
the same engine cycle the E2E test asserts and prints a banner per step (fast path →
break → serve-now → promote v2 → reuse healed → blocked → recovered → audit) so the
recording reads as a story, not a test log.

### Prereqs (one time)
```bash
# asciinema for the cast, agg for cast->GIF (both via Homebrew on macOS)
brew install asciinema agg
# project env
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### Primary — record the offline self-heal demo
```bash
# Record. --idle-time-limit 2 caps idle gaps so pauses don't bloat the GIF.
# ANTHROPIC_API_KEY= proves on-camera that it needs no key.
asciinema rec selfheal.cast \
  --idle-time-limit 2 \
  --title "crawloop: serve-now, heal-in-background (offline demo)" \
  --command "bash -lc 'source .venv/bin/activate && ANTHROPIC_API_KEY= python examples/selfheal_demo.py'"

# Convert to GIF for Twitter/Reddit inline embeds.
agg --theme monokai --font-size 22 selfheal.cast selfheal.gif

# (optional) Upload the cast for the HN/X "live terminal" link.
asciinema upload selfheal.cast
```

### Secondary — CLI walkthrough GIF (a shorter asset for r/Python)
Shows the operator surface (offline fast path costs no model call, then the read-only
registry/audit views). Note: an offline `crawl` of a *cold* registry will need the model
and fail loudly by design — pre-seed a family first so the fast path serves with no LLM.
```bash
asciinema rec cli.cast --idle-time-limit 2 --title "crawloop CLI" --command "bash -lc '
  source .venv/bin/activate
  set -x
  crawloop crawl https://books.toscrape.com/catalogue/page-1.html --schema Product@1 --offline
  crawloop family list
  crawloop audit
'"
agg --theme monokai --font-size 22 cli.cast cli.gif
```

### GIF hygiene
- Target < 8 MB and < 30s so it auto-plays inline on Twitter and Old Reddit.
- Keep terminal at ~90 cols so text is legible on mobile.
- First frame must show the banner/title (people scrub to frame 0).
- Make a 1280×720 static cover (the first self-heal step) for the X card and the GitHub
  social-preview image.

---

## 6. Timing / day-of-week guidance

All times **US Eastern** (HN/Reddit traffic is US-weighted).

- **Best day: Tuesday or Wednesday.** Avoid Mon (weekend backlog buries you), Fri/Sat/Sun
  (low traffic, your post ages out before the weekday crowd sees it).
- **Show HN: post 8:30–10:00 AM ET.** Hits the US-morning + EU-afternoon overlap; gives
  the post the full US day to climb. Front-page survival on HN is about early velocity, so
  be at the keyboard to answer the first comments within minutes.
- **Reddit: stagger, don't blast.** Lead with **r/webscraping** (most receptive) the same
  morning ~9–11 AM ET. Then **r/Python** the *next* day ~9 AM ET (its audience dislikes
  simultaneous multi-sub spraying). Hold **r/programming** and **r/MachineLearning** for a
  day-2/day-3 follow when you can point to "discussed on HN" as social proof.
- **X/Twitter: post the thread ~9:00 AM ET**, same morning as Show HN, so a single
  audience can boost both. Re-share the thread once ~6–7 PM ET for the second US wave.
- **Never launch the week of a major holiday or a huge tech event** (it eats attention).
- **Have ~3–4 hours of clear calendar after posting.** The spike is won or lost in the
  first few hours of replies; a launch you can't babysit is a launch you shouldn't do.

---

## 7. Pre-launch checklist (must ALL be true before posting)

**Hard blockers (do not post until done):**

- [ ] **Repo is public on GitHub.** Create the remote and push — nothing to star until
      this is done.
- [x] **A LICENSE file exists** — Apache-2.0 is committed at `LICENSE` (and the README
      badge matches).
- [ ] **`examples/selfheal_demo.py` runs offline** with `ANTHROPIC_API_KEY= python
      examples/selfheal_demo.py` — the demo link is the single highest-leverage asset.
- [ ] **The demo GIF + asciinema cast are recorded, uploaded, and embedded** in the README
      at the top (above the fold).
- [ ] **`pytest` is green on a clean checkout**, and specifically
      `ANTHROPIC_API_KEY= python -m pytest tests/test_selfheal_e2e.py -s` passes — because
      you're telling people to run exactly that.

**Strong recommendations (do before posting):**

- [ ] **README leads with the GIF + the one-liner + the honest-limits box** so skeptics
      see the limitations immediately (this is your reputation shield).
- [ ] **GitHub repo "About": one-liner + topics** (`web-scraping`, `llm`, `python`,
      `self-healing`, `crawler`, `playwright`) + the social-preview image (§5).
- [ ] **A real-LLM smoke test has been run once** against an allowlisted site so you can
      honestly answer "does the live model path work?" with "yes, smoke-tested." If you
      can't, say "the live path is untested" — but know that weakens the demo.
- [ ] **`pip install -e ".[dev]"` works from a fresh clone on a clean machine.**
- [ ] **CONTRIBUTING note / "good first issue" labels** — a star spike brings drive-by
      contributors; give them a door.
- [ ] **Decide and pre-write the answer to "is this legal/ethical?"** (see §8) — it *will*
      be the top question on r/webscraping.

---

## 8. Honest talking points + likely tough questions (with answers)

### The talking points to repeat (all true, all in this repo)
- **Serve now, heal in the background.** A broken extractor doesn't take you down — the
  LLM fallback serves immediately while a fresh crawler regenerates.
- **Compile once, run free per page** — the LLM bill amortizes for crawloop and never
  amortizes for per-page (structural, not a benchmark).
- **Deterministic** — same page → same bytes, because steady state is plain code.
- **Safe by construction:** hard allowlist on every fetch *and* every redirect hop,
  AST-check on all generated code, sandboxed execution, full audit trail.
- **It heals offline, with no API key — and you can read the demo and the test.**
- **It degrades gracefully:** when codegen can't hit the bar, it falls back to the LLM =
  parity, never worse output (you just "spend" the one-time bootstrap).

### Tough questions — and the honest answer to each

**Q: Isn't this just an LLM writing a scraper / distillation + a try/except?**
A: The codegen is the easy part; the value is the *loop around it* — drift classification,
an LLM oracle with promote gates (≥3 oracles, per-item agreement, item-count match), a
gauntlet that scores candidates before promotion, a version ladder, and access-recovery
for blocks. And critically: the LLM is invoked *once* to teach cheap code, then only again
on drift — that's the cost model a plain "LLM writes a scraper" doesn't give you.

**Q: Does it actually beat a per-page LLM on accuracy?**
A: No — not on a wide, normalized schema, and I won't claim it does. It is *exact on the
high-value core* it compiles (numbers, identity, URLs) and falls back to the LLM for the
normalized/derived tail, so it's never worse on output but it isn't beating the LLM there.
The honest bottom line: ahead on the cost model, latency, determinism, and
drift-resilience; not a full accuracy replacement yet.

**Q: Is this legal? Isn't a "self-healing scraper" just a more aggressive bot?**
A: It's deliberately **not** a general-purpose scraper. The allowlist is mandatory and has
no override — a host that isn't listed *cannot* be fetched, including via redirect. It's
built for sites you own or are explicitly authorized to crawl. The CAPTCHA rung is opt-in,
authorized-domains-only, and ships with no provider; anti-bot evasion is a per-domain
explicit opt-in, not the default. The stance: "if you wouldn't be comfortable explaining a
crawl to the site's owner, it doesn't belong on the allowlist."

**Q: Has the real LLM path ever run, or is it all mocked?**
A: The tests all use a scripted fake model + localhost fixtures (that's *why* they're
deterministic and need no key). The live `LiteLLMCompleter` path is [smoke-tested once /
untested — fill in the truth before launch].

**Q: Why not Scrapy / Playwright / firecrawl / [tool]?**
A: Those are fetch/parse tooling; this is a layer *above* — the self-heal control loop and
the cost model. You could implement the crawlers on top of any of them (parsel is used
here). The contribution is "what happens when the extractor breaks," not the fetching.

**Q: Production-ready?**
A: No, and I say so plainly — it's a POC proven against a fixture server. The README lists
exactly what to address before a real run (live-LLM smoke test, robots enforcement,
concurrency hardening). I'd rather you know the edges than discover them.

**Q: What's the actual moat / why would I use this over rolling my own try/except?**
A: The honest answer: today, if you scrape a handful of pages from one stable site, you
don't need this. The value shows up when you (a) crawl enough pages that per-page LLM cost
hurts, (b) maintain *many* families where layout drift is a constant maintenance tax, and
(c) need reproducibility + an audit trail. If none of those is true for you, the README
will tell you that too.

**Closing posture for every thread:** lead with the true win, concede the limits *fast and
without defensiveness*, point to the offline demo as proof, and treat hard questions as
free QA. The honesty is not a liability here — for this project, it *is* the marketing.
