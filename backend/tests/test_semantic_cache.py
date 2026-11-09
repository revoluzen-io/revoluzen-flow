"""
Tests for the agent's answer cache.

The key only sees the last user text, so a run that has moved past its first
message must never read or write the cache: two runs with different tool
results would otherwise share one answer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from revoluzen_flow.ai import semantic_cache
from revoluzen_flow.ai.agent import LLMResponse
from revoluzen_flow.ai.semantic_cache import cached_call


@dataclass
class FakeLLM:
    calls: list[list[dict]] = field(default_factory=list)

    async def call(self, *, system, messages, tools):
        self.calls.append(messages)
        return LLMResponse(
            text=f"answer {len(self.calls)}",
            tool_uses=[],
            stop_reason="end_turn",
            tokens_in=10,
            tokens_out=5,
        )


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.delenv("REVOLUZEN_FLOW_DISABLE_SEMANTIC_CACHE", raising=False)
    semantic_cache.clear()
    yield
    semantic_cache.clear()


def _ask(llm: FakeLLM, messages: list[dict]) -> LLMResponse:
    return asyncio.run(
        cached_call(llm.call, system="sys", messages=messages, tools=[], tenant_id="w1", user_role="admin")
    )


def _question(text: str = "How many pipelines failed?") -> list[dict]:
    return [{"role": "user", "content": text}]


def _after_tool_call(result: str) -> list[dict]:
    return [
        *_question(),
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "list_runs", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": result}],
        },
    ]


def test_same_single_question_is_served_from_the_cache():
    llm = FakeLLM()

    first = _ask(llm, _question())
    second = _ask(llm, _question())

    assert len(llm.calls) == 1
    assert second.text == first.text
    assert (second.tokens_in, second.tokens_out) == (0, 0)
    assert semantic_cache.stats().hits == 1


def test_different_single_questions_do_not_share_an_answer():
    llm = FakeLLM()

    _ask(llm, _question("How many pipelines failed?"))
    _ask(llm, _question("How many pipelines passed?"))

    assert len(llm.calls) == 2


def test_different_tool_results_do_not_share_an_answer():
    llm = FakeLLM()

    first = _ask(llm, _after_tool_call("3 failed"))
    second = _ask(llm, _after_tool_call("0 failed"))

    assert len(llm.calls) == 2
    assert second.text != first.text


def test_identical_tool_results_are_not_cached_either():
    llm = FakeLLM()

    _ask(llm, _after_tool_call("3 failed"))
    _ask(llm, _after_tool_call("3 failed"))

    assert len(llm.calls) == 2
    assert semantic_cache.stats().entries == 0


def test_same_follow_up_after_different_turns_does_not_share_an_answer():
    llm = FakeLLM()

    def follow_up(earlier: str) -> list[dict]:
        return [
            *_question(earlier),
            {"role": "assistant", "content": "Shall I go ahead?"},
            *_question("Yes, proceed"),
        ]

    _ask(llm, follow_up("Delete the staging tables"))
    _ask(llm, follow_up("Rerun the nightly load"))

    assert len(llm.calls) == 2
