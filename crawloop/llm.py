"""The LLM ``Completer`` abstraction: one tiny port, two implementations.

Every place this POC talks to a language model — the T2 direct extractor
(:mod:`crawloop.fallback`), and later the M9 regeneration loop — depends on
the :class:`Completer` Protocol, never on ``litellm`` directly. That keeps the
provider swappable and, more importantly, keeps the tests honest: they inject a
:class:`FakeCompleter`, so no unit test ever reaches a real model or the
network.

* :class:`Completer` — the structural port: ``await complete(system, user,
  model) -> str``.
* :class:`FakeCompleter` — the test double used everywhere: pops canned
  responses in order and records every call for assertions.
* :class:`LiteLLMCompleter` — the production adapter over
  ``litellm.acompletion``. The only thing that knows the litellm wire shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

# Supported provider env keys, in preference order. crawloop is provider-agnostic
# via litellm; the only provider-specific bits are which env var holds the key and
# a sane default model per provider.
_PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY")
_DEFAULT_MODEL_BY_KEY = {
    "ANTHROPIC_API_KEY": "anthropic/claude-fable-5",
    "OPENAI_API_KEY": "openai/gpt-4o-mini",
    "GEMINI_API_KEY": "gemini/gemini-2.0-flash",
}


def has_provider_key(env: Mapping[str, str]) -> bool:
    """True if any supported LLM provider key is present in ``env``."""
    return any(env.get(k) for k in _PROVIDER_KEYS)


def default_model(env: Mapping[str, str]) -> str:
    """Pick a sane default model for whichever provider key is present.

    Preference order ANTHROPIC -> OPENAI -> GEMINI. Cheap by default; the
    regeneration loop escalates to a stronger model only when a candidate cannot
    clear the gauntlet (see :func:`crawloop.loop.driver.run_loop`). Falls back to
    the Anthropic default when no key is set (used only once a key exists)."""
    for key in _PROVIDER_KEYS:
        if env.get(key):
            return _DEFAULT_MODEL_BY_KEY[key]
    return "anthropic/claude-fable-5"


# Loop-engineering technique — tiered model escalation. A cheap model is great for
# the oracle and the fast path, but its codegen may not clear the strict promote
# gauntlet (>=0.98 per-item agreement, exact count) on a hard multi-record page.
# When no candidate clears the bar, the loop retries codegen ONCE with a stronger
# model from this map; the promoted artifact is still free deterministic code, so
# the one-time stronger-model cost amortizes. None => already top-tier, don't escalate.
_ESCALATION = {
    "openai/gpt-4o-mini": "openai/gpt-4o",
    "anthropic/claude-haiku-4-5": "anthropic/claude-fable-5",
}


def escalation_model(model: str) -> str | None:
    """The stronger model to retry codegen with, or ``None`` if ``model`` is already
    top-tier. See :func:`crawloop.loop.driver.run_loop`."""
    return _ESCALATION.get(model)


@runtime_checkable
class Completer(Protocol):
    """A single async text completion over a system + user prompt.

    The whole surface the rest of the system needs from "an LLM". Implementations
    return the model's text content as a plain string; callers (the extractor,
    the regenerator) own parsing/validation of that text.
    """

    async def complete(self, *, system: str, user: str, model: str) -> str: ...


class FakeCompleter:
    """A scripted :class:`Completer` for tests — never calls a model.

    Construct it with the exact responses you want, in order; each
    :meth:`complete` call pops the next one. Every call is also appended to
    :attr:`calls` as ``{"system", "user", "model"}`` so a test can assert what
    the code under test sent (e.g. that a repair prompt carried the prior error).
    Popping past the end raises ``RuntimeError`` with a clear message rather than
    a bare ``IndexError``, so an over-call shows up as an obvious test failure.
    """

    def __init__(self, responses: list[str]) -> None:
        # Copy so the caller's list isn't mutated as we pop, and reverse so we
        # can pop(-1) in O(1) while still handing them back in given order.
        self._queue = list(reversed(responses))
        self.calls: list[dict] = []

    async def complete(self, *, system: str, user: str, model: str) -> str:
        self.calls.append({"system": system, "user": user, "model": model})
        if not self._queue:
            raise RuntimeError(
                "FakeCompleter exhausted: complete() was called more times than the "
                f"{len(self.calls) - 1} canned response(s) it was given"
            )
        return self._queue.pop()


class LiteLLMCompleter:
    """The production :class:`Completer`, backed by ``litellm.acompletion``.

    The single place that knows litellm's request/response shape: it builds the
    two-message ``[system, user]`` list, awaits ``litellm.acompletion``, and
    returns ``resp.choices[0].message.content``. ``litellm`` is imported lazily
    inside :meth:`complete` (so importing this module never forces the litellm
    import) and referenced as a module attribute so tests can monkeypatch
    ``litellm.acompletion`` with an async stub — no network in any test.
    """

    async def complete(self, *, system: str, user: str, model: str) -> str:
        import litellm

        resp = await litellm.acompletion(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content
