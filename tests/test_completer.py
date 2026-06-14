"""Completer abstraction (Task 8.2): FakeCompleter + LiteLLMCompleter wiring.

No test here touches a real model or the network. ``FakeCompleter`` is the
substitute used everywhere in the suite: it pops canned responses in order and
records every call. The single ``LiteLLMCompleter`` test monkeypatches
``litellm.acompletion`` with an async stub purely to prove the wiring — that the
two-message ``[system, user]`` list is built and ``choices[0].message.content``
is returned — never to reach a network.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from crawloop.llm import Completer, FakeCompleter, LiteLLMCompleter


# --- FakeCompleter ----------------------------------------------------------- #


def test_fake_completer_pops_responses_in_order():
    fake = FakeCompleter(["first", "second"])
    out1 = asyncio.run(fake.complete(system="s", user="u1", model="m"))
    out2 = asyncio.run(fake.complete(system="s", user="u2", model="m"))
    assert out1 == "first"
    assert out2 == "second"


def test_fake_completer_records_every_call():
    fake = FakeCompleter(["x", "y"])
    asyncio.run(fake.complete(system="sys-a", user="usr-a", model="model-a"))
    asyncio.run(fake.complete(system="sys-b", user="usr-b", model="model-b"))
    assert fake.calls == [
        {"system": "sys-a", "user": "usr-a", "model": "model-a"},
        {"system": "sys-b", "user": "usr-b", "model": "model-b"},
    ]


def test_fake_completer_raises_with_clear_message_when_exhausted():
    fake = FakeCompleter(["only-one"])
    asyncio.run(fake.complete(system="s", user="u", model="m"))
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - asserting on message below
        asyncio.run(fake.complete(system="s", user="u", model="m"))
    # The message should make the test failure obvious, not be a bare IndexError.
    assert "exhausted" in str(excinfo.value).lower()


def test_fake_completer_is_a_completer_instance():
    # @runtime_checkable Protocol: the fake satisfies the structural type.
    assert isinstance(FakeCompleter([]), Completer)


# --- LiteLLMCompleter (wiring only; acompletion monkeypatched) --------------- #


def test_litellm_completer_builds_two_message_list_and_returns_content(monkeypatch):
    captured: dict = {}

    async def fake_acompletion(*, model, messages):
        captured["model"] = model
        captured["messages"] = messages
        # Mimic litellm's response object: resp.choices[0].message.content
        message = SimpleNamespace(content="stubbed-completion")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)

    completer = LiteLLMCompleter()
    out = asyncio.run(
        completer.complete(system="SYS", user="USR", model="anthropic/claude-fable-5")
    )

    assert out == "stubbed-completion"
    assert captured["model"] == "anthropic/claude-fable-5"
    assert captured["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USR"},
    ]


def test_litellm_completer_is_a_completer_instance():
    assert isinstance(LiteLLMCompleter(), Completer)
