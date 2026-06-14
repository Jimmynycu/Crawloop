"""T2 direct extraction + oracle (Task 8.3): trim_html + direct_extract.

Two halves, both offline:

* ``trim_html`` is pure — a cheap prompt-cost reduction over raw HTML. Cases
  prove scripts/styles/comments are dropped, length is capped, and structural
  attributes (class/id/data-*) survive.
* ``direct_extract`` is the LLM extractor and the Loop's oracle. Every case
  drives it with a :class:`FakeCompleter` (NO network): a clean success, a
  one-repair recovery, repairs exhausted -> ``ExtractionFailed``, and fenced
  ```json``` output parsed correctly. Assertions inspect ``completer.calls`` to
  prove the prompts (system + user with schema + trimmed html, and the error on
  a repair) were actually sent.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from crawloop.fallback import ExtractionFailed, direct_extract
from crawloop.htmlutil import trim_html
from crawloop.llm import FakeCompleter


# --- trim_html (pure) -------------------------------------------------------- #


def test_trim_html_removes_script_and_style_and_comment_contents():
    html = (
        "<html><head><style>.a{color:red}</style>"
        "<script>var secret = 1; alert('x')</script></head>"
        "<body><!-- hidden note --><p>Visible</p></body></html>"
    )
    out = trim_html(html)
    assert "Visible" in out
    assert "secret" not in out
    assert "color:red" not in out
    assert "hidden note" not in out


def test_trim_html_caps_length():
    html = "<p>" + ("x" * 50_000) + "</p>"
    out = trim_html(html, max_chars=8000)
    assert len(out) <= 8000


def test_trim_html_preserves_class_and_id_and_data_attributes():
    html = (
        '<div class="product-card" id="p1" data-sku="ABC-123" aria-label="Widget">'
        "<span>Widget</span></div>"
    )
    out = trim_html(html)
    assert 'class="product-card"' in out
    assert 'id="p1"' in out
    assert 'data-sku="ABC-123"' in out
    assert 'aria-label="Widget"' in out


def test_trim_html_collapses_whitespace():
    html = "<p>a</p>\n\n\n          <p>b</p>"
    out = trim_html(html)
    # Long runs of whitespace collapse; both paragraphs survive.
    assert "a" in out and "b" in out
    assert "          " not in out


# --- direct_extract helpers -------------------------------------------------- #


def _product_json(name: str, url: str) -> dict:
    return {"name": name, "price": "12.50", "in_stock": True, "url": url}


_THREE_PRODUCTS = [
    _product_json("Alpha", "https://shop.example.com/a"),
    _product_json("Beta", "https://shop.example.com/b"),
    _product_json("Gamma", "https://shop.example.com/c"),
]

_SAMPLE_HTML = (
    '<div class="grid"><article class="product-card">'
    "<h2>Alpha</h2><span>£12.50</span></article></div>"
)


# --- direct_extract: clean success ------------------------------------------- #


def test_direct_extract_returns_validated_items_on_first_try():
    fake = FakeCompleter([json.dumps(_THREE_PRODUCTS)])
    items = asyncio.run(direct_extract(_SAMPLE_HTML, "Product@1", fake))

    assert len(items) == 3
    assert [it["name"] for it in items] == ["Alpha", "Beta", "Gamma"]
    # Exactly one call; both prompts present.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["system"]  # non-empty system prompt
    assert call["user"]  # non-empty user prompt


def test_direct_extract_user_prompt_carries_schema_and_trimmed_html():
    fake = FakeCompleter([json.dumps(_THREE_PRODUCTS)])
    asyncio.run(direct_extract(_SAMPLE_HTML, "Product@1", fake))

    user = fake.calls[0]["user"]
    # Schema JSON appears (a Product property name is a reliable, stable marker).
    assert "in_stock" in user
    # The (trimmed) HTML appears — the class attr survives trimming and shows up.
    assert "product-card" in user


def test_direct_extract_passes_model_through_to_completer():
    fake = FakeCompleter([json.dumps(_THREE_PRODUCTS)])
    asyncio.run(direct_extract(_SAMPLE_HTML, "Product@1", fake, model="anthropic/some-model"))
    assert fake.calls[0]["model"] == "anthropic/some-model"


# --- direct_extract: one repair then success --------------------------------- #


def test_direct_extract_repairs_once_then_succeeds():
    # First response is malformed JSON; second is a valid array.
    fake = FakeCompleter(["this is not json at all", json.dumps(_THREE_PRODUCTS)])
    items = asyncio.run(direct_extract(_SAMPLE_HTML, "Product@1", fake, max_repairs=1))

    assert len(items) == 3
    # Exactly two calls: original + one repair.
    assert len(fake.calls) == 2
    # The repair prompt references the previous output being invalid.
    repair_user = fake.calls[1]["user"]
    assert "invalid" in repair_user.lower()


# --- direct_extract: repairs exhausted -> ExtractionFailed ------------------- #


def test_direct_extract_raises_after_repairs_exhausted_on_invalid_schema():
    # Parseable JSON, but each item fails the Product schema (price <= 0 -> gt=0
    # violation). Two bad responses with max_repairs=1 => original + 1 repair,
    # both invalid, then raise.
    bad = [{"name": "X", "price": "-5.00", "in_stock": True, "url": "https://e.com/x"}]
    fake = FakeCompleter([json.dumps(bad), json.dumps(bad)])
    with pytest.raises(ExtractionFailed):
        asyncio.run(direct_extract(_SAMPLE_HTML, "Product@1", fake, max_repairs=1))
    assert len(fake.calls) == 2


def test_direct_extract_raises_when_never_a_list():
    # A JSON object (not a list) is structurally wrong every time.
    obj = json.dumps({"not": "a list"})
    fake = FakeCompleter([obj, obj])
    with pytest.raises(ExtractionFailed):
        asyncio.run(direct_extract(_SAMPLE_HTML, "Product@1", fake, max_repairs=1))


# --- direct_extract: fenced ```json output parsed --------------------------- #


def test_direct_extract_strips_json_code_fences():
    fenced = "```json\n" + json.dumps(_THREE_PRODUCTS) + "\n```"
    fake = FakeCompleter([fenced])
    items = asyncio.run(direct_extract(_SAMPLE_HTML, "Product@1", fake))
    assert len(items) == 3


def test_direct_extract_strips_bare_code_fences():
    fenced = "```\n" + json.dumps(_THREE_PRODUCTS) + "\n```"
    fake = FakeCompleter([fenced])
    items = asyncio.run(direct_extract(_SAMPLE_HTML, "Product@1", fake))
    assert len(items) == 3
