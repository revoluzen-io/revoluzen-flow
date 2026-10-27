"""The selector must hand the model `get_security_posture` for security questions.

Registering a tool is not enough. agent.py narrows the candidate set to
8 tools (local) or 14 (cloud) before any schema reaches the model, via
`select_tools`. A tool that is registered but never selected is invisible
to the Copilot — which is exactly the state `get_security_posture` was in
when it was first added: no page pack, no keyword group, and an
alphabetical fill that never reached it.

The tight cap is the real test. On the trust page, floor(3) + the trust
pack already consume the 8 slots, so the posture tool survives only
because it is FIRST in that pack. These tests fail if someone reorders
it, which is the whole point.
"""

from __future__ import annotations

import pytest

from revoluzen_flow.ai.tool_selector import select_tools
from revoluzen_flow.ai.tools import INITIAL_TOOLS
from revoluzen_flow.ai.tools.base import ToolTier

POSTURE = "get_security_posture"

# Mirror what agent.py passes: RBAC has already filtered by tier, and an
# admin sees read + safe-write + high-impact.
ALL_TOOLS = [t for t in INITIAL_TOOLS]

OLLAMA_CAP = 8   # agent.py: provider_hint == "ollama"
CLOUD_CAP = 14   # agent.py: everything else


def _names(page, prompt, cap):
    return [
        t.name
        for t in select_tools(
            available_tools=ALL_TOOLS, page=page, prompt=prompt, max_tools=cap
        )
    ]


def test_selector_actually_narrows():
    """Guard the guard — if the cap stopped biting these tests prove nothing."""
    assert len(ALL_TOOLS) > CLOUD_CAP, (
        "The registry no longer exceeds the cloud cap, so select_tools returns "
        "everything and the tests below pass vacuously."
    )
    assert len(_names("trust", "hello", CLOUD_CAP)) == CLOUD_CAP


@pytest.mark.parametrize("cap", [OLLAMA_CAP, CLOUD_CAP])
def test_trust_page_always_offers_posture(cap):
    """The page whose subject IS the posture must always offer the tool."""
    assert POSTURE in _names("trust", "is this install secure?", cap)


@pytest.mark.parametrize("cap", [OLLAMA_CAP, CLOUD_CAP])
def test_trust_page_offers_posture_even_for_a_vague_prompt(cap):
    """No keyword help — this must come from the page pack alone."""
    assert POSTURE in _names("trust", "what does this mean", cap)


@pytest.mark.parametrize(
    "prompt",
    [
        "why is master key permissions failing",
        "what does Status Not checked mean",
        "is my install secure",
        "are my credentials encrypted",
        "how do I fix this with icacls",
        "what is secret.key",
        "check the security headers",
    ],
)
@pytest.mark.parametrize("cap", [OLLAMA_CAP, CLOUD_CAP])
def test_security_questions_reach_posture_from_any_page(prompt, cap):
    """Users ask these from the dashboard and the editor too, not just /trust."""
    for page in ("dashboard", "editor", "executions", None):
        names = _names(page, prompt, cap)
        assert POSTURE in names, f"{prompt!r} on page {page!r} (cap {cap}) -> {names}"


def test_failing_security_prompt_beats_the_failures_group():
    """"failing" matches the debug group too; posture must still get a slot.

    This is the ordering the keyword table encodes: the security group sits
    above the failures group so the first slots do not go to
    list_executions / recall_history on a security question.
    """
    names = _names("dashboard", "why is master key permissions failing", OLLAMA_CAP)
    assert POSTURE in names
    assert names.index(POSTURE) < len(names)


def test_permission_prompts_offer_both_readings():
    """'permissions' is ambiguous — RBAC and file ACLs. Offer both tools."""
    names = _names("dashboard", "I don't understand these permissions", CLOUD_CAP)
    assert POSTURE in names
    assert "get_user_role" in names


def test_posture_is_read_tier_so_rbac_never_strips_it():
    """A write tier would be filtered out for read-only roles."""
    tool = next(t for t in INITIAL_TOOLS if t.name == POSTURE)
    assert tool.tier is ToolTier.READ


def test_unrelated_prompt_does_not_drag_posture_in():
    """Negative control — the boost must be specific, not a catch-all.

    If this fails the keyword list has grown a term so generic that every
    question now burns a slot on the security tool.
    """
    names = _names("editor", "add a filter step after the source", OLLAMA_CAP)
    assert POSTURE not in names
