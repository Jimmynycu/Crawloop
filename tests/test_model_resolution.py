"""Provider-agnostic model resolution.

crawloop claims "provider-agnostic via litellm", but the CLI only went live when
``ANTHROPIC_API_KEY`` was set and hardwired ``anthropic/claude-fable-5``. These
pin the fix: detect ANY supported provider key, and pick a sane default model for
whichever key is present (cheap by default — the loop escalates on its own).
"""

from __future__ import annotations

from crawloop.llm import default_model, escalation_model, has_provider_key


def test_has_provider_key_true_for_openai():
    assert has_provider_key({"OPENAI_API_KEY": "sk-x"}) is True


def test_has_provider_key_true_for_gemini():
    assert has_provider_key({"GEMINI_API_KEY": "g-x"}) is True


def test_has_provider_key_false_when_none_present():
    assert has_provider_key({"UNRELATED": "x"}) is False


def test_default_model_prefers_anthropic_when_its_key_is_present():
    m = default_model({"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"})
    assert m.startswith("anthropic/")


def test_default_model_uses_openai_when_only_openai_key():
    assert default_model({"OPENAI_API_KEY": "o"}).startswith("openai/")


def test_default_model_uses_gemini_when_only_gemini_key():
    assert default_model({"GEMINI_API_KEY": "g"}).startswith("gemini/")


# --- escalation_model: the new loop-engineering technique -------------------- #


def test_escalation_model_upgrades_cheap_openai_to_strong():
    # The cheap default can't always clear the 0.98 gauntlet on a multi-record
    # page; the loop retries codegen with a stronger model before giving up.
    assert escalation_model("openai/gpt-4o-mini") == "openai/gpt-4o"


def test_escalation_model_none_when_already_strong():
    # No escalation if the model is already top-tier — avoid pointless extra spend.
    assert escalation_model("openai/gpt-4o") is None
    assert escalation_model("anthropic/claude-fable-5") is None
