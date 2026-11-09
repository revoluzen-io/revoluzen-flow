"""Stand-ins for the model, all speaking the agent loop's client protocol.

* :class:`ScriptedClient` plays fixed turns. The oracle is a script built from a
  case's reference answer, and the tests use scripts to play models that cheat.
* :class:`RecordingClient` wraps a real provider and keeps every reply.
* :class:`ReplayClient` plays a recording back. Tool calls still run against
  today's code, so a recorded model keeps testing the platform without a key.

The agent loop picks its tool cap and iteration limit from the client's class
name. The recording and replay wrappers keep the real client's name in theirs,
so a replayed local-model run is offered the same tools the live run was.
"""

from __future__ import annotations

import json
import re
from typing import Any

from revoluzen_flow.ai.agent import LLMResponse, LLMToolUse

from .cases import LAST_DRAFT, RepairCase

_DRAFT_ID = re.compile(r"wf-draft-[A-Za-z0-9_-]+")


def _latest_draft_id(messages: list[dict[str, Any]]) -> str | None:
    """The draft id in the most recent tool result, as a model would read it."""
    for message in reversed(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block in reversed(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                found = _DRAFT_ID.findall(str(block.get("content") or ""))
                if found:
                    return found[-1]
    return None


def _with_inputs(resp: LLMResponse, rewrite: Any) -> LLMResponse:
    return LLMResponse(
        text=resp.text,
        tool_uses=[LLMToolUse(id=t.id, name=t.name, input=rewrite(t.input)) for t in resp.tool_uses],
        stop_reason=resp.stop_reason,
        tokens_in=resp.tokens_in,
        tokens_out=resp.tokens_out,
    )


def response_to_dict(resp: LLMResponse) -> dict[str, Any]:
    return {
        "text": resp.text,
        "tool_uses": [{"id": t.id, "name": t.name, "input": t.input} for t in resp.tool_uses],
        "stop_reason": resp.stop_reason,
        "tokens_in": resp.tokens_in,
        "tokens_out": resp.tokens_out,
    }


def response_from_dict(d: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        text=d.get("text") or "",
        tool_uses=[
            LLMToolUse(id=t["id"], name=t["name"], input=t.get("input") or {})
            for t in d.get("tool_uses") or []
        ],
        stop_reason=d.get("stop_reason") or "end_turn",
        tokens_in=int(d.get("tokens_in") or 0),
        tokens_out=int(d.get("tokens_out") or 0),
    )


def tool_turn(name: str, tool_input: dict[str, Any], call_id: str = "call") -> LLMResponse:
    return LLMResponse(
        text="",
        tool_uses=[LLMToolUse(id=call_id, name=name, input=tool_input)],
        stop_reason="tool_use",
    )


def text_turn(text: str) -> LLMResponse:
    return LLMResponse(text=text, tool_uses=[], stop_reason="end_turn")


class ScriptedClient:
    """Plays a fixed list of turns, then ends the conversation."""

    def __init__(self, turns: list[LLMResponse], final_text: str = "") -> None:
        self._turns = list(turns)
        self._final_text = final_text
        self.calls = 0
        self.tools_offered: list[str] = []
        self.system_prompts: list[str] = []
        self.messages_seen: list[list[dict[str, Any]]] = []

    async def call(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_token: Any = None,
    ) -> LLMResponse:
        del on_token
        self.calls += 1
        self.tools_offered = [t["name"] for t in tools]
        self.system_prompts.append(system)
        self.messages_seen.append(list(messages))
        if self._turns:
            latest = _latest_draft_id(messages)

            def fill(tool_input: dict[str, Any]) -> dict[str, Any]:
                if tool_input.get("draft_id") == LAST_DRAFT:
                    return {**tool_input, "draft_id": latest}
                return tool_input

            return _with_inputs(self._turns.pop(0), fill)
        return text_turn(self._final_text)


def oracle_for(case: RepairCase) -> ScriptedClient:
    """An ideal assistant: makes the reference tool calls, then gives the reference answer."""
    turns = [
        tool_turn(step["tool"], step["input"], call_id=f"oracle-{i}")
        for i, step in enumerate(case.reference)
    ]
    return ScriptedClient(turns, final_text=case.reference_text)


def mock_client() -> ScriptedClient:
    """A floor: never edits anything and never claims anything."""
    return ScriptedClient([], final_text="I can't tell what is wrong with this pipeline.")


class RecordingClient:
    """Wraps a real client and keeps each reply for a cassette."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.replies: list[dict[str, Any]] = []

    async def call(self, **kwargs: Any) -> LLMResponse:
        resp = await self.inner.call(**kwargs)
        self.replies.append(response_to_dict(resp))
        return resp


class ReplayClient:
    """Plays recorded replies in order. Tools run live against today's code."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self._replies = list(replies)

    async def call(self, **kwargs: Any) -> LLMResponse:
        if not self._replies:
            return text_turn("(cassette exhausted)")
        resp = response_from_dict(self._replies.pop(0))
        # Draft ids are random per run. A recorded id means "the draft I was just
        # given", so point it at the draft this replay just produced.
        latest = _latest_draft_id(kwargs.get("messages") or [])

        def refresh(tool_input: dict[str, Any]) -> dict[str, Any]:
            if not latest:
                return tool_input
            return json.loads(_DRAFT_ID.sub(latest, json.dumps(tool_input)))

        return _with_inputs(resp, refresh)


def named_like(client_cls: type, real_name: str, *args: Any) -> Any:
    """Instantiate ``client_cls`` under a class name that contains ``real_name``."""
    named = type(f"{client_cls.__name__}{real_name}", (client_cls,), {})
    return named(*args)
