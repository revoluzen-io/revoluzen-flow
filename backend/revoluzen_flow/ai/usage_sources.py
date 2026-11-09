"""Record the AI calls made outside the Copilot, for Insights → Activity.

The Copilot's own runs are saved by AgentRunner and by
``api/agent.py:_record_quick_answer``. Everything else that asks a model —
the Editor's pipeline drafting and assistant chat, the step and SQL
helpers, run and health summaries, notification summaries, and the
Copilot's plain-English failure explanation — is saved here, so Activity's
tokens, cost and failure counts cover all AI use.

Each call is one row in the trace store (``agent_traces``) with ``source``
naming where it came from. Copilot rows leave ``source`` empty.

The hub functions in ``planner/ai_client.py`` record every call they make.
A caller that knows more than they do — what the person typed, or that the
call is part of a Copilot request — says so with :func:`ai_call_details`
around the call, rather than through new arguments, so the existing test
doubles of those functions keep working.

Not recorded: the AI Provider page's connection tests, which only check
that a key works.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import uuid
from typing import Any, Iterator

logger = logging.getLogger(__name__)

COPILOT = "copilot"

# source id -> (page it comes from, what it did, in the words Activity shows)
SOURCES: dict[str, tuple[str, str]] = {
    "planner.ai_generate_pipeline": ("editor", "Draft a pipeline"),
    "planner.fix_missing_column": ("editor", "Correct a drafted pipeline"),
    "canvas-chat": ("editor", "Assistant chat"),
    "embedded.suggest_next_node": ("editor", "Suggest the next step"),
    "embedded.auto_fill_config": ("editor", "Fill in a step's settings"),
    "embedded.assist_node": ("editor", "Help with a step"),
    "embedded.diagnose_error": ("editor", "Explain a step's error"),
    "ai.transform.explain": ("editor", "Explain a transform"),
    "ai.transform.suggest_sql": ("editor", "Suggest SQL"),
    "ai.transform.explain_selection": ("editor", "Explain selected SQL"),
    "ai.transform.fix_error": ("editor", "Fix a SQL error"),
    "ai.post_run_summary": ("executions", "Summarise a run"),
    "ai.metrics.health_summary": ("dashboard", "Summarise workspace health"),
    "ai.connection_test_diagnose": ("connections", "Explain a failed connection test"),
    "notifications.summary": ("notifications", "Write a notification"),
    "copilot.draft_pipeline": ("copilot", "Draft a pipeline"),
    "copilot.explain_failure": ("copilot", "Explain a failed run"),
}

OTHER_FEATURE = "Other AI feature"

_details: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "revoluzen_flow_ai_call_details", default=None,
)


@contextlib.contextmanager
def ai_call_details(*, source: str | None = None, intent: str | None = None) -> Iterator[None]:
    """Tell the recorder where the next AI calls come from.

    ``source`` replaces the hub function's own label (for example the
    Copilot's draft tool using the Editor's pipeline planner); ``intent``
    is what the person typed, shown as the request on Activity instead of
    the feature's name. Both apply to every call made inside the block.
    """
    token = _details.set({"source": source or "", "intent": intent or ""})
    try:
        yield
    finally:
        _details.reset(token)


def is_copilot(source: str | None) -> bool:
    return not source or source == COPILOT


def feature_label(source: str | None) -> str:
    """What a call did, in the words Activity shows."""
    if is_copilot(source):
        return "Copilot"
    return SOURCES.get(source or "", ("", OTHER_FEATURE))[1]


def feature_page(source: str | None) -> str:
    """The page a call came from, as a page key ("editor", "copilot", …)."""
    return SOURCES.get(source or "", ("", ""))[0]


def tokens_from_reply(data: Any) -> tuple[int, int]:
    """Input and output tokens from a provider's reply.

    Anthropic says ``usage.input_tokens`` / ``output_tokens``; OpenAI-style
    APIs ``usage.prompt_tokens`` / ``completion_tokens``; Ollama
    ``prompt_eval_count`` / ``eval_count``. Anything missing counts as 0.
    """
    if not isinstance(data, dict):
        return 0, 0
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    tokens_in = usage.get("input_tokens", usage.get("prompt_tokens", data.get("prompt_eval_count", 0)))
    tokens_out = usage.get("output_tokens", usage.get("completion_tokens", data.get("eval_count", 0)))
    try:
        return int(tokens_in or 0), int(tokens_out or 0)
    except (TypeError, ValueError):
        return 0, 0


def record_ai_call(
    *,
    source: str,
    user_id: str | None,
    workspace_id: str | None,
    provider: str | None,
    model: str | None,
    latency_ms: int,
    tokens_in: int,
    tokens_out: int,
    success: bool,
    error: str | None = None,
    intent: str | None = None,
) -> None:
    """Save one AI call made outside the Copilot. Best-effort: never raises.

    ``success`` means the feature could use the answer; a reply that came
    back but could not be read counts as a failed call, because the
    feature fell back to its non-AI behaviour.
    """
    try:
        from revoluzen_flow.main import app_state  # type: ignore

        store = app_state.get("trace_store")
        if store is None:
            return
        details = _details.get() or {}
        source = details.get("source") or source
        intent = (details.get("intent") or intent or "").strip()
        label = feature_label(source)
        uid = user_id if user_id and user_id != "anonymous" else None
        run_id = uuid.uuid4().hex
        steps: list[dict[str, Any]] = []
        if not success:
            steps.append({
                "step_id": f"{run_id}-model",
                "tool_name": f"llm_call:{source}",
                "tool_tier": "read",
                "outcome": "llm_failure",
                "decision_reason": (error or "The model's answer could not be used.")[:300],
                "latency_ms": int(latency_ms or 0),
                "tokens_in": int(tokens_in or 0),
                "tokens_out": int(tokens_out or 0),
            })
        store.store(
            run_id=run_id,
            user_id=uid,
            workspace_id=workspace_id or "default",
            page=feature_page(source),
            user_intent=intent or label,
            outcome="success" if success else "llm_failure",
            iterations=1,
            total_tokens_in=int(tokens_in or 0),
            total_tokens_out=int(tokens_out or 0),
            elapsed_ms=int(latency_ms or 0),
            steps=steps,
            final_text="",
            model=model or None,
            provider=provider or None,
            source=source,
        )
    except Exception:  # noqa: BLE001 — recording must never break an AI feature
        logger.debug("record_ai_call skipped", exc_info=True)
