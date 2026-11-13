"""Per-request tool gating for local LLMs.

Background (2026-05-22). The agent registers ~24 tools. Sending all 24
schemas to a CPU-class 7B model every turn is a real problem:

  - Each tool schema adds ~80 tokens to the prompt. 24 * 80 = ~1.9 K
    tokens of tool definitions before the user's question even starts.
  - Local 7B models pick the wrong tool more often as the candidate set
    grows. Empirically, dropping from 24 → 6 tools roughly halves the
    "model called list_executions when I asked about connections"
    failure mode.
  - The OS-default Ollama context (`num_ctx=2048`) silently truncated
    the system prompt + tool block (fixed in `ollama_client.py` on the
    same day); even after raising num_ctx to 8192, fewer tools means
    less head-of-prompt that the model has to attend to.

This module exposes ``select_tools(page, prompt, available_tools)`` that
returns a curated subset (default cap: 8 tools) by combining:

  1. **Floor tools** — always included regardless of page/prompt.
  2. **Page-based picks** — tools known to be relevant to the current
     UI surface (editor / executions / connections / etc).
  3. **Keyword-based boosts** — tools whose names match terms in the
     prompt ("failure", "schedule", "connection", ...).
  4. **Deterministic fill** — if we're under the cap, fill from a stable
     ordering so the candidate set is reproducible (good for caching).

Selection is ON for all providers by default (2026-06-18; was Ollama-only):
the caller (agent.py) gives Ollama a tight cap (8) for routing accuracy and
cloud a looser cap (14) — every tool schema is ~80 tokens re-sent each loop, so
trimming the long tail saves tokens/$ on cloud too while keeping broad coverage.
Override with ``REVOLUZEN_FLOW_TOOL_SELECTOR=off``.

Bypass entirely with ``REVOLUZEN_FLOW_DISABLE_TOOL_SELECTOR=1`` if you want
every call to see every tool (useful for debugging which tool a model
WOULD have picked given the choice).
"""

from __future__ import annotations

import os
from typing import Iterable, Sequence

from revoluzen_flow.ai.tools.base import ToolDefinition


# Tools that should ALWAYS be in the candidate set. The floor exists so
# the model never gets stuck without an "I need to find this" escape
# hatch. Keep this list small — every floor entry costs prompt tokens
# even when the page wouldn't otherwise surface it.
_FLOOR_TOOLS: tuple[str, ...] = (
    # Registered as `get_workspace_overview` — the bare name here matched
    # nothing, so `_add`'s `name not in by_name` guard silently dropped it
    # and the floor was two tools, not three (fixed 2026-10-26).
    "get_workspace_overview",  # generic "what's in this workspace" question
    "recall_history",          # cross-cutting RAG retrieval
    "lookup_help_topic",       # in-app help atlas
)

# How many slots a prompt keyword may claim ahead of the page pack.
#
# Page packs run up to six tools; with the floor that alone fills the tight
# (8) cap, so on those pages keyword boosts were unreachable — a security
# or schedule question asked from the editor got the editor's tools and
# nothing else. The prompt is the stronger signal (the user said what they
# want; the page is only a prior), so it gets first refusal on a few slots.
# Capped rather than unlimited so a prompt matching several groups cannot
# crowd the page pack out entirely.
_KEYWORD_RESERVE = 3

# Tools a page cannot do its job without. They go in right after the floor, ahead
# of keyword boosts. Without this, a request mentioning a failure from the editor
# filled the tight cap with failure tools, and a local model had no way to edit
# the pipeline it was asked to fix.
_PAGE_ANCHORS: dict[str, tuple[str, ...]] = {
    # `test_pipeline_draft` is an anchor for the same reason as the other
    # two: without it the editor's repair loop cannot close. The agent can
    # edit and it can see columns, but it cannot find out whether its edit
    # worked, so it stops at the first fault. Registering the tool was not
    # enough — the first live run after it shipped called it zero times,
    # because the selector never offered it on this page.
    "editor": ("modify_pipeline_step", "inspect_step_columns", "test_pipeline_draft"),
}


# Page-specific tool packs. The page identifier comes from
# ``PageContext.page`` — see ``frontend/src/hooks/usePageContext.ts`` for
# the canonical strings. Use ``startswith`` matching so sub-routes
# (e.g. ``editor.canvas`` vs ``editor.diff``) share the editor pack.
_PAGE_TOOLS: dict[str, tuple[str, ...]] = {
    "editor": (
        "validate_pipeline",
        "summarize_pipeline",
        # Before reconfiguring a step, the agent needs to know which
        # parameters that step actually has — a renamed join column needs
        # `key_mode: mapped_keys`, not a new `join_key`.
        "explain_step",
        "modify_pipeline_step",
        "draft_pipeline_from_intent",
        # `list_templates` was here and is not: with test_pipeline_draft
        # now anchored, the pack is one over the cloud cap, and starting
        # from a template is a "build something new" affordance while
        # everything else on this page serves repairing what is open. It
        # is still reachable from a prompt that mentions templates.
    ),
    "executions": (
        "list_executions",
        "get_running_executions",
        "query_metrics",
        "summarize_pipeline",
    ),
    "connections": (
        "inspect_connections",
        "list_catalog",
    ),
    "pipelines": (
        "list_pipelines",
        "list_projects",
        "summarize_pipeline",
        "list_schedules",
        # What the selected pipeline was asked to do. Last, so on the tight
        # cap it only takes a slot left over.
        "get_pipeline_spec",
    ),
    "projects": (
        "list_projects",
        "list_pipelines",
    ),
    "schedules": (
        "list_schedules",
        "get_next_scheduled",
        "list_pipelines",
    ),
    "triggers": (
        "list_pipelines",
        "list_executions",
    ),
    "alerts": (
        "list_alerts",
        "draft_alert_rule",
        "list_executions",
    ),
    "templates": (
        "list_templates",
        "draft_pipeline_from_intent",
    ),
    "dashboard": (
        "get_installation_health",
        "list_steward_findings",
        "query_metrics",
        "list_pipelines",
        "list_executions",
    ),
    "insights": (
        "get_installation_health",
        "list_steward_findings",
        "query_metrics",
        "list_executions",
    ),
    "steward": (
        "list_steward_findings",
        "get_installation_health",
    ),
    "trust": (
        # First deliberately: floor(3) + this pack already fills the
        # tight (8) cap, so anything after the 5th entry here is only a
        # candidate on the looser cloud cap. The posture tool IS the
        # subject of this page — without it the model answers security
        # questions from general knowledge instead of this install.
        "get_security_posture",
        "get_installation_health",
        "list_steward_findings",
        "get_user_role",
    ),
}


# Prompt-keyword → tool boost. Keys are lowercase substrings. When ANY
# substring matches the user's prompt, the listed tools become candidates
# regardless of the current page. Order within a list matters — earlier
# entries win if the cap is tight.
_KEYWORD_TOOLS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    # Security posture — FIRST on purpose. A security question is usually
    # also a "failing" question ("why is master key permissions failing"),
    # and the failures group below would otherwise spend the first three
    # slots on list_executions/recall_history before posture is reached.
    # "not checked" is here because that is the literal status text users
    # read off the page and paste into the Copilot.
    (("security", "secure", "master key", "secret.key", "posture",
      "encrypt", "icacls", "acl", "hardening", "harden", "not checked",
      "security header", "vulnerab"),
     ("get_security_posture",)),
    # Failures / errors / debugging. Incidents first: they say what changed
    # since the pipeline last worked, which is the first question.
    (("fail", "error", "broke", "crash", "timeout", "incident", "outage", "what changed"),
     ("get_incidents", "list_executions", "recall_history", "get_installation_health")),
    # Connection-related
    (("connection", "credential", "auth"),
     ("inspect_connections", "list_catalog")),
    # Schedules
    (("schedule", "cron", "run at", "next run"),
     ("list_schedules", "get_next_scheduled")),
    # Permissions / role. "permission" is ambiguous: it means RBAC ("who
    # can run this pipeline") but also file permissions ("Master key
    # permissions: Failed"). Both tools are candidates so the model picks
    # from the wording rather than being handed only the RBAC one.
    (("role", "permission", "rbac", "who can"),
     ("get_user_role", "get_security_posture")),
    # Reports / output
    (("report", "summary", "summarise", "summarize"),
     ("compose_report", "summarize_pipeline")),
    # Business words: the workspace's glossary says what they mean here.
    (("glossary", "business term", "definition of", "what does", "metric", "kpi", "means here"),
     ("lookup_business_terms",)),
    # Data Quality rules: a shared rule set may already check these columns.
    (("quality rule", "rule set", "data quality", "validation rule", "validate", "dq "),
     ("list_quality_rule_sets", "explain_step")),
    # Join keys: which columns line up, from the values in both inputs.
    (("join", "match on", "key column", "join key", "lookup join", "mapped_keys"),
     ("suggest_join_keys", "explain_step")),
    # What a pipeline is for and how to tell it worked: the intent spec and
    # its contract verdict.
    (("spec", "requirement", "acceptance", "criteria", "criterion", "what is this pipeline for",
      "what's this pipeline for", "purpose", "business owner", "contract", "promise", "outcome"),
     ("get_pipeline_spec",)),
    # Health / audit / punch-list
    (("health", "audit", "punch list", "what should i fix", "what needs"),
     ("get_installation_health", "list_steward_findings")),
    # Steward advisories — duplicates, governance, compliance, risk review
    (("steward", "duplicate", "governance", "compliance", "risk", "redundant", "flag"),
     ("list_steward_findings", "get_installation_health")),
    # Build / draft / create
    (("create", "build", "draft", "make a pipeline"),
     ("draft_pipeline_from_intent", "apply_pipeline_draft")),
    # Web search / fetch. These tools are only in the candidate set when
    # the operator has enabled web access (otherwise they aren't registered,
    # so `_add`'s `name not in by_name` guard makes this boost inert). Without
    # this entry the selector trims web_search/web_fetch out of the 14-tool
    # cap and the model denies web access even when it's on. Also covers the
    # connector-discovery phrasing (find an API's docs / OpenAPI spec).
    (("web", "internet", "online", "search the", "look up", "look it up",
      "google", "browse", "http://", "https://", "fetch ", "url",
      "api documentation", "api docs", "openapi", "swagger",
      "find the api", "docs for", "spec for",
      # external-integration phrasings — "get the FactoHR api", "integrate
      # stripe", "connect to salesforce". These are where a user wants an
      # external system, so the web tools must be candidates.
      " api", "integrat", "connector for", "connect to", "sdk", "endpoint"),
     ("web_search", "web_fetch")),
)


def _page_bucket(page: str | None) -> str | None:
    """Map a page string like ``editor.canvas`` to a bucket key (``editor``)."""
    if not page:
        return None
    head = page.split(".", 1)[0].strip().lower()
    return head or None


def _is_enabled_for_provider(provider_hint: str | None) -> bool:
    """Selection defaults: ON for every provider.

    2026-06-18 — extended from Ollama-only to all providers. Cloud models
    handle 24 schemas *correctly*, but every schema is ~80 tokens re-sent
    on every loop iteration, so all-26-tools-every-turn was a real token
    (and $) tax for cloud users too. Cloud gets a looser cap than Ollama
    (see select_tools) so the broader candidate set is preserved while the
    long tail of irrelevant tools is trimmed. Fully env-overridable:
    set REVOLUZEN_FLOW_TOOL_SELECTOR=off (or REVOLUZEN_FLOW_DISABLE_TOOL_SELECTOR=1) to
    restore every-tool-every-call.
    """
    override = os.environ.get("REVOLUZEN_FLOW_TOOL_SELECTOR", "").strip().lower()
    if override in {"1", "true", "yes", "on", "always"}:
        return True
    if override in {"0", "false", "no", "off", "never"}:
        return False
    # Default: on for all providers.
    return True


def select_tools(
    *,
    available_tools: Sequence[ToolDefinition],
    page: str | None,
    prompt: str,
    provider_hint: str | None = None,
    max_tools: int = 8,
) -> list[ToolDefinition]:
    """Return a curated list of tools for this request.

    ``available_tools`` is the result of ``registry.filter_by_tiers(...)``
    — i.e. RBAC has already happened. This function only narrows the set
    further by relevance to the page + prompt.

    Returns ``available_tools`` unchanged when:
      - selection is disabled via ``REVOLUZEN_FLOW_TOOL_SELECTOR=off`` or
        ``REVOLUZEN_FLOW_DISABLE_TOOL_SELECTOR=1``,
      - or the available set is already at/below ``max_tools``.
    """
    if os.environ.get("REVOLUZEN_FLOW_DISABLE_TOOL_SELECTOR", "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        return list(available_tools)

    if not _is_enabled_for_provider(provider_hint):
        return list(available_tools)

    if len(available_tools) <= max_tools:
        return list(available_tools)

    by_name: dict[str, ToolDefinition] = {t.name: t for t in available_tools}
    picked: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name in seen or name not in by_name:
            return
        if len(picked) >= max_tools:
            return
        picked.append(name)
        seen.add(name)

    # 1. Floor — always present
    for name in _FLOOR_TOOLS:
        _add(name)

    # 1b. Web tools are a CONDITIONAL floor: they only appear in
    # `available_tools` when the operator enabled web access, and when they
    # do we keep them every turn. Keyword matching alone was too brittle —
    # "add Aconex as a source" has no web keyword yet clearly needs the web.
    # The system prompt tells the model to use them for external systems.
    for name in ("web_search", "web_fetch"):
        _add(name)

    # 1c. What this page cannot work without. Only tools the role may use are in
    # by_name, so a read-only user is never offered the edit tool here.
    bucket = _page_bucket(page)
    for name in _PAGE_ANCHORS.get(bucket or "", ()):
        _add(name)

    # 2. Keyword boosts, up to the reserve. The prompt beats the page: a
    # user asking about security from the editor means the security
    # question, not the editor. Bounded by _KEYWORD_RESERVE so the page
    # pack still gets most of the budget.
    p = (prompt or "").lower()
    keyword_picks: list[str] = []
    for triggers, boosted in _KEYWORD_TOOLS:
        if any(t in p for t in triggers):
            for name in boosted:
                if name in by_name and name not in keyword_picks:
                    keyword_picks.append(name)
    reserved_floor = len(picked)
    for name in keyword_picks:
        if len(picked) - reserved_floor >= _KEYWORD_RESERVE:
            break
        _add(name)

    # 3. Page bucket
    if bucket and bucket in _PAGE_TOOLS:
        for name in _PAGE_TOOLS[bucket]:
            _add(name)

    # 3b. Any keyword picks that did not fit the reserve — they still beat
    # the alphabetical fill below, they just do not displace the page pack.
    for name in keyword_picks:
        _add(name)

    # 4. Deterministic fill — by-name alphabetical order. Stable for
    # cache keys; doesn't accidentally pick destructive tools first
    # because the registry already filtered by tier.
    for name in sorted(by_name.keys()):
        if len(picked) >= max_tools:
            break
        _add(name)

    return [by_name[n] for n in picked if n in by_name]


__all__ = ["select_tools"]
