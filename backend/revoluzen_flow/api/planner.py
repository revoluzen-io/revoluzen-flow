"""Planner API — convert intent to workflow IR via AI or rules."""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from revoluzen_flow.ai.clarify_draft import (
    CLARIFY_MARKER,
    ClarificationSet,
    DraftEndpoint,
    DraftSlots,
    asks_for_new_pipeline,
    clarification_payload,
    clarification_set_from_slots,
    detect_draft_slots,
    detect_missing_draft_fields,
    render_clarification_card,
    slug_for_connection_type,
)
from revoluzen_flow.ai.clarify_to_template import (
    match_template_from_intent_and_answers,
    parse_answers_freeform,
    populate_template,
)
from revoluzen_flow.ai.draft_choices import (
    DraftChoices,
    apply_choices_to_workflow,
    choices_from_answers,
    choices_from_reply,
    choices_from_slots,
    describe_choices_for_person,
    describe_choices_for_planner,
    draft_notes,
    read_workspace_inventory,
    source_columns,
)
from revoluzen_flow.auth.deps import current_user_optional, current_workspace_id, require_min_rank_when_identified
from revoluzen_flow.planner.rule_planner import RulePlanner
from revoluzen_flow.planner.templates import TEMPLATES, create_from_template
from revoluzen_flow.planner.ai_client import ai_generate_pipeline, ai_generate_text, is_ai_available
from revoluzen_flow.ai.usage_sources import ai_call_details
from revoluzen_flow.planner.ir_from_json import planner_json_to_ir
from revoluzen_flow.ir.schema import Workflow, Step, StepConnection, StepType, NodePosition

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/planner", tags=["planner"])


def _safe_workspace_id(request: Request) -> str:
    """Wrap current_workspace_id so dep failures surface as readable
    HTTP errors — same pattern as api/workflows.py."""
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc


def _caller_user_id(request: Request) -> str | None:
    """User id for AI config lookup, so a provider saved in the app is used."""
    user = current_user_optional(request)
    return getattr(user, "id", None) if user else None


class IntentRequest(BaseModel):
    intent: str


class ChatMessage(BaseModel):
    role: str = "user"
    content: str
    # What was picked on a clarify card: ids and names only. Each one is looked
    # up again in the caller's workspace, so a forged id resolves to nothing.
    choices: dict[str, Any] | None = None


def _ai_json_to_workflow(data: dict, workspace_id: str = "default") -> Workflow | None:
    """Convert AI-generated JSON to a proper Workflow IR.

    Stamps the provided ``workspace_id`` on the result so the new
    pipeline lands in the caller's tenant — the AI output has no
    concept of workspace, so the caller is authoritative here.
    """
    # Index → id translation lives in planner.ir_from_json, shared with the
    # Copilot's draft_pipeline_from_intent tool, which used to skip it and
    # hand back drafts whose connections said `from_step: 0`.
    ir = planner_json_to_ir(data, name=data.get("name") or "AI Pipeline")
    if ir is None:
        return None

    steps: list[Step] = []
    for raw_step in ir["steps"]:
        try:
            st = StepType(raw_step.get("type", "transform"))
        except ValueError:
            st = StepType.TRANSFORM
        pos = raw_step.get("position") or {}
        steps.append(Step(
            id=raw_step["id"],
            type=st,
            label=raw_step.get("label", ""),
            params=raw_step.get("params", {}),
            position=NodePosition(x=pos.get("x", 0), y=pos.get("y", 100)),
        ))

    connections = [
        StepConnection(from_step=c["from_step"], to_step=c["to_step"])
        for c in ir["connections"]
    ]

    return Workflow(
        name=ir["name"],
        description=ir.get("description", ""),
        workspace_id=workspace_id or "default",
        steps=steps,
        connections=connections,
    )


@router.post("/generate")
async def generate_plan(
    body: IntentRequest,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Convert natural language intent to a workflow IR.

    Any pipeline auto-saved from a high-confidence plan is stamped
    with the caller's workspace so AI-generated pipelines never leak
    across tenant boundaries.
    """
    planner = RulePlanner()
    result = planner.plan(body.intent)

    # Auto-save if high confidence
    if result.workflow and result.confidence >= 0.5:
        result.workflow.workspace_id = workspace_id or "default"
        from revoluzen_flow.main import app_state
        store = app_state["store"]
        version = store.save(result.workflow, change_summary=f"Generated from intent: {body.intent}")
        return {
            **result.dict(),
            "saved": True,
            "version": version.version,
        }

    return {**result.dict(), "saved": False}


@router.get("/templates")
async def list_templates():
    """List available pipeline templates."""
    return TEMPLATES


@router.post("/templates/{template_key}", dependencies=[Depends(require_min_rank_when_identified("developer"))])
async def use_template(
    template_key: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Create a workflow from a template inside the caller's
    workspace."""
    if template_key not in TEMPLATES:
        return {"error": f"Unknown template: {template_key}"}

    workflow = create_from_template(template_key)
    if not workflow:
        return {"error": "Failed to create workflow from template"}

    workflow.workspace_id = workspace_id or "default"

    from revoluzen_flow.main import app_state
    store = app_state["store"]
    version = store.save(workflow, change_summary=f"Created from template: {template_key}")

    return {
        "id": workflow.id,
        "version": version.version,
        "workflow": workflow.model_dump(mode="json"),
    }


_QUESTION_PREFIX = ("what", "how", "why", "when", "where", "who", "is", "are", "does", "do", "can", "should")
_GREETING_RE = re.compile(r"^(hi|hello|hey|howdy|good\s+(morning|afternoon|evening))[\s!.,]*$", re.IGNORECASE)
_EXPLAIN_HINTS = ("explain", "describe this", "show me the sql", "show sql", "show me the code", "what does this")


def _looks_like_question_or_greeting(text: str) -> bool:
    """Cheap intent-shape check so the chat endpoint can refuse to
    plan a pipeline from non-create messages. The rule planner has
    no concept of conversation state, so without this it would
    return its "Could not determine data source" boilerplate for
    every non-create message — confusing for users asking questions
    about the canvas they already have."""
    t = (text or "").strip().lower()
    if not t:
        return True
    if _GREETING_RE.match(t):
        return True
    first = t.split()[0] if t.split() else ""
    if first in _QUESTION_PREFIX and "?" in t:
        return True
    if first in _QUESTION_PREFIX[:8]:  # what/how/why/when/where/who/is/are without requiring '?'
        return True
    if any(h in t for h in _EXPLAIN_HINTS):
        return True
    return False


@router.post("/chat")
async def chat(
    messages: list[ChatMessage],
    request: Request,
    preview: bool = False,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Chat endpoint — uses AI when available, falls back to rule
    planner. Any workflow auto-saved from the chat is stamped with
    the caller's workspace. With ``preview`` nothing is saved: the draft
    comes back for review and /apply-draft saves it on confirmation."""
    next_step = (
        "Review the draft below, then apply it to your canvas."
        if preview
        else "The pipeline is on your canvas. Click any node to configure it, or click **Run All** to execute."
    )
    if not messages:
        return {"reply": "What kind of data pipeline would you like to build?", "workflow": None}

    last_msg = messages[-1].content

    # Refuse to plan a pipeline from clearly-non-create messages.
    # Frontend already classifies intent client-side and only routes
    # create/modify here, but we add the same guard server-side as
    # defence-in-depth — protects future callers (CLI, scripts) from
    # the "Could not determine data source" garbage when they send a
    # question. The frontend's client-side question handler renders
    # the actual canvas-aware answer; we just have to NOT confuse it
    # with a planner failure.
    if not messages[-1].choices and _looks_like_question_or_greeting(last_msg):
        return {
            "reply": (
                "I help build and modify pipelines from natural language. "
                "Ask me to build a pipeline (e.g. *\"Load orders.csv, "
                "deduplicate by order_id, write to Parquet\"*), or use "
                "the canvas's own tools to inspect the current pipeline."
            ),
            "workflow": None,
            "confidence": 0,
            "ai_powered": False,
            "intent": "question_or_greeting",
        }

    # Clarify-before-draft (May 17 2026 — Phase 2A): if the prompt is a
    # build-pipeline request that doesn't say where the data comes from or
    # where it goes, ask BEFORE running the planner, listing what this
    # workspace really has. Without this, the planner silently guesses every
    # gap and the user gets back a draft they have to fix in 5 places.
    #
    # Skipped on follow-up turns where the previous assistant message
    # already asked questions — at that point the user is answering,
    # not making a new request. The answers then travel to the draft as
    # `choices`, whichever planner ends up writing it.
    answering = _answers_clarify_card(messages)
    request_index = _original_request_index(messages) if answering else len(messages) - 1
    request_text = messages[request_index].content or ""
    inventory = read_workspace_inventory(workspace_id or "default")
    choices: DraftChoices | None = None

    try:
        if answering:
            slots = detect_draft_slots(request_text, inventory) if inventory is not None else None
            if inventory is not None:
                raw_answers = messages[-1].choices
                choices = (
                    choices_from_answers(raw_answers, slots, inventory) if raw_answers
                    else choices_from_reply(last_msg, slots, inventory)
                )
            template_reply = _template_draft(
                last_msg, request_text, slots, choices,
                workspace_id=workspace_id, preview=preview, next_step=next_step,
            )
            if template_reply is not None:
                return template_reply
        elif inventory is None:
            # The workspace could not be read: generic chips are still a question.
            cset = detect_missing_draft_fields(last_msg)
            if cset is not None:
                return _clarify_card_reply(cset)
        else:
            slots = detect_draft_slots(last_msg, inventory)
            if slots is not None:
                if slots.questions:
                    return _clarify_card_reply(clarification_set_from_slots(slots))
                choices = choices_from_slots(slots)
    except Exception:
        # Clarification engine failures must never block the chat —
        # fall through to the existing planner path.
        logger.warning("clarify-before-draft failed; drafting without it", exc_info=True)

    # Try AI first if available
    user_id = _caller_user_id(request)
    ai_failed = ""
    if is_ai_available(user_id=user_id, workspace_id=workspace_id):
        columns = source_columns(choices, workspace_id or "default") if choices is not None else []
        chat_msgs = _planner_messages(messages, request_index, answering, choices, columns)
        # Activity shows what the person asked, not the prompt built from it.
        with ai_call_details(intent=request_text):
            ai_result = await ai_generate_pipeline(chat_msgs, user_id=user_id, workspace_id=workspace_id)
        if ai_result is None:
            ai_failed = (
                "The AI provider could not complete this request, so this comes from "
                "the basic rule planner and may miss parts of what you asked. "
                "Check the connection on the AI Provider page and try again.\n\n"
            )

        if ai_result and ai_result.get("steps"):
            workflow = _ai_json_to_workflow(ai_result, workspace_id=workspace_id)
            if workflow and workflow.steps:
                from revoluzen_flow.main import app_state
                if choices is not None:
                    apply_choices_to_workflow(workflow, choices)
                workflow, ai_result, still_wrong = await _correct_missing_columns(
                    workflow, ai_result, chat_msgs, choices, user_id=user_id, workspace_id=workspace_id,
                )
                if not preview:
                    app_state["store"].save(workflow, change_summary=f"AI Chat: {request_text[:100]}")

                explanation = ai_result.get("explanation", "")
                step_names = " → ".join(s.label for s in workflow.steps)
                reply = f"I've drafted your pipeline: **{workflow.name}**\n\n" if preview else f"I've created your pipeline: **{workflow.name}**\n\n"
                reply += f"Steps: {step_names}\n\n"
                if explanation:
                    reply += f"{explanation}\n\n"
                reply += _choices_text(choices, workflow)
                if still_wrong:
                    reply += f"Check this before you run it: {still_wrong}\n\n"
                reply += next_step

                return {
                    "reply": reply,
                    "workflow": workflow.model_dump(mode="json"),
                    "confidence": 0.95,
                    "ai_powered": True,
                }
        elif ai_result and ai_result.get("explanation") and not (choices is not None and choices.has_endpoints):
            return {
                "reply": ai_result["explanation"],
                "workflow": None,
                "confidence": 0,
                "ai_powered": True,
            }

    # Fall back to rule planner
    planner = RulePlanner()
    result = planner.plan(request_text, assume_source=choices is not None and choices.binds_source)

    if result.workflow and result.confidence >= 0.5:
        result.workflow.workspace_id = workspace_id or "default"
        from revoluzen_flow.main import app_state
        if choices is not None:
            apply_choices_to_workflow(result.workflow, choices)
        if not preview:
            app_state["store"].save(result.workflow, change_summary=f"Chat: {request_text[:100]}")

        reply = ai_failed
        reply += f"I've drafted a pipeline: **{result.workflow.name}**\n\n" if preview else f"I've created a pipeline: **{result.workflow.name}**\n\n"
        reply += f"Steps: {' → '.join(s.label for s in result.workflow.steps)}\n\n"
        reply += _choices_text(choices, result.workflow)
        if answering and (choices is None or choices.destination is None):
            # A typed answer can leave this open, and the planner still needs an end.
            reply += "I did not find a destination in your answer, so this writes to a Parquet file. Change it on the canvas if you want it elsewhere.\n\n"
        reply += next_step

        return {
            "reply": reply,
            "workflow": result.workflow.model_dump(mode="json"),
            "confidence": result.confidence,
            "ai_powered": False,
        }
    elif result.needs_ai:
        return {
            "reply": f"{ai_failed}I understood parts of your request but need more detail.\n\n{result.explanation}\n\nCould you be more specific about the data source and operations?",
            "workflow": None,
            "confidence": result.confidence,
            "ai_powered": False,
        }
    else:
        return {
            "reply": "I'm not sure what pipeline you need. Try something like:\n\n- *\"Load orders.csv, deduplicate by order_id, output to parquet\"*\n- *\"Read sales.csv, filter amount > 100, calculate daily revenue\"*",
            "workflow": None,
            "confidence": 0,
            "ai_powered": False,
        }


def _answers_clarify_card(messages: list[ChatMessage]) -> bool:
    """Is this turn an answer to the card the last reply showed?"""
    if len(messages) < 2:
        return False
    if messages[-2].role != "assistant" or CLARIFY_MARKER not in (messages[-2].content or ""):
        return False
    # A whole new request typed instead of an answer starts over.
    return bool(messages[-1].choices) or not asks_for_new_pipeline(messages[-1].content)


def _original_request_index(messages: list[ChatMessage]) -> int:
    """Walk back past the card to the build request it asked about."""
    index = len(messages) - 1
    while (
        index >= 2
        and messages[index - 1].role == "assistant"
        and CLARIFY_MARKER in (messages[index - 1].content or "")
        and messages[index - 2].role == "user"
    ):
        index -= 2
    return index


def _clarify_card_reply(cset: ClarificationSet) -> dict[str, Any]:
    return {
        "reply": render_clarification_card(cset),
        "workflow": None,
        "confidence": 0,
        "ai_powered": False,
        "intent": "clarify_first",
        "clarification": clarification_payload(cset),
    }


def _planner_messages(
    messages: list[ChatMessage],
    request_index: int,
    answering: bool,
    choices: DraftChoices | None,
    columns: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """The conversation a model sees: role and text, plus the choices as context.

    On an answer the card itself is left out. The model gets the request and what
    was decided, which is what it needs, instead of a list of options to reason about.
    """
    turns = messages[: request_index + 1] if answering else messages
    chat_msgs = [{"role": m.role, "content": m.content} for m in turns]
    extra: list[str] = []
    if answering and not messages[-1].choices and messages[-1].content:
        extra.append(f"The person also said: {messages[-1].content}")
    context = describe_choices_for_planner(choices, columns) if choices is not None else ""
    if context:
        extra.append(context)
    if extra:
        chat_msgs[-1]["content"] = "\n\n".join([chat_msgs[-1]["content"], *extra])
    return chat_msgs


# What a draft may run by itself, before anyone has seen it: steps that only read
# rows and shape them with fixed settings. Sinks count because a sample run skips
# their writes. Anything that runs code or the model's own SQL waits for a person.
_TRIAL_STEP_TYPES = frozenset({
    "source", "local_table_source", "db_source", "csv_source", "json_source", "parquet_source",
    "filter", "aggregate", "sort", "deduplicate", "join", "derived_column", "rename", "typecast", "union",
    "destination", "local_table_sink", "db_sink", "csv_sink", "json_sink",
})
_MISSING_COLUMN_RE = re.compile(r'Referenced column "([^"]+)" not found[^\n]*(?:\s*Candidate bindings: ([^\n]+))?')
_CORRECTION_ATTEMPTS = 2


def _missing_column_in(workflow: Workflow) -> str:
    """Try a draft on a small sample, writing nothing, and say which column it got wrong.

    Only that goes back to the model: the wording of a missing-column error holds
    column names, and other errors can quote values from the rows. Empty when
    nothing is wrong, or when the draft can't be tried here.
    """
    if any(step.type.value not in _TRIAL_STEP_TYPES for step in workflow.steps):
        return ""
    try:
        from revoluzen_flow.engine.executor import WorkflowExecutor
        from revoluzen_flow.main import app_state
        from revoluzen_flow.security.execution_codes import mint_for_run

        result = WorkflowExecutor(data_dir=app_state["data_dir"], app_state=app_state).execute_workflow(
            workflow, preview_limit=0, preview_mode=True, sample_row_limit=50,
            execution_code=mint_for_run(workflow),
        )
    except Exception:  # noqa: BLE001 — a draft that can't be tried is still a draft
        logger.warning("could not try the draft on a sample", exc_info=True)
        return ""
    for step in workflow.steps:
        ran = result.step_results.get(step.id)
        found = _MISSING_COLUMN_RE.search(ran.error or "") if ran is not None and ran.status == "error" else None
        if found:
            closest = f" The closest columns are: {found.group(2).strip()}." if found.group(2) else ""
            return f'The step "{step.label}" uses a column called "{found.group(1)}", which isn\'t there.{closest}'
    return ""


async def _correct_missing_columns(
    workflow: Workflow,
    ai_result: dict[str, Any],
    chat_msgs: list[dict[str, str]],
    choices: DraftChoices | None,
    *,
    user_id: str | None,
    workspace_id: str,
) -> tuple[Workflow, dict[str, Any], str]:
    """A model can name a column that doesn't exist, even when told which do.

    Returns the draft to show, its model output, and what is still wrong with it.
    """
    history = list(chat_msgs)
    for _ in range(_CORRECTION_ATTEMPTS):
        problem = await anyio.to_thread.run_sync(_missing_column_in, workflow)
        if not problem:
            return workflow, ai_result, ""
        history += [
            {"role": "assistant", "content": json.dumps(ai_result, default=str)},
            {"role": "user", "content": f"That draft can't run. {problem} Send the whole corrected pipeline, in the same format."},
        ]
        with ai_call_details(source="planner.fix_missing_column"):
            retry = await ai_generate_pipeline(history, user_id=user_id, workspace_id=workspace_id)
        corrected = _ai_json_to_workflow(retry, workspace_id=workspace_id) if retry and retry.get("steps") else None
        if not corrected or not corrected.steps:
            return workflow, ai_result, problem
        if choices is not None:
            apply_choices_to_workflow(corrected, choices)
        workflow, ai_result = corrected, retry
    return workflow, ai_result, await anyio.to_thread.run_sync(_missing_column_in, workflow)


def _choices_text(choices: DraftChoices | None, workflow: Workflow) -> str:
    """What the draft reads and writes, in the reply under it."""
    if choices is None:
        return ""
    parts = [describe_choices_for_person(choices, workflow), *draft_notes(choices, workflow)]
    return "".join(f"{part}\n\n" for part in parts if part)


_WRITE_MODE_WORD_FOR_TEMPLATE = {"append": "append", "replace": "overwrite", "merge": "upsert"}

# <your-table>, <business_key>, <soap-envelope-...>. Not the XML element names a
# template's SQL comments mention, which are in capitals.
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"<[a-z][A-Za-z0-9_-]*>")


def _template_draft(
    last_msg: str,
    request_text: str,
    slots: DraftSlots | None,
    choices: DraftChoices | None,
    *,
    workspace_id: str,
    preview: bool,
    next_step: str,
) -> dict[str, Any] | None:
    """Phase 2F (May 18 2026) — answer the clarification with a template.

    Re-detects source / sink types from the ORIGINAL build-pipeline prompt and
    matches them against an enterprise template, whose placeholders are filled
    from the answers. Returns None when no template fits, and the planner drafts
    it instead.
    """
    legacy = detect_missing_draft_fields(request_text)
    parsed = parse_answers_freeform(last_msg, legacy.questions if legacy is not None else ())
    answers = dict(parsed.values)
    source_type = legacy.source_type if legacy is not None else None
    sink_type = legacy.sink_type if legacy is not None else None

    destination: DraftEndpoint | None = None
    if choices is not None:
        answers.update(choices.answers)
        if choices.write_mode:
            answers["sql_server_write_mode"] = _WRITE_MODE_WORD_FOR_TEMPLATE[choices.write_mode]
        if choices.source is not None and choices.source.kind == "connection":
            source_type = slug_for_connection_type(choices.source.connection_type)
        if choices.destination is not None and choices.destination.kind != "text":
            destination = choices.destination
            if destination.kind == "connection":
                sink_type = slug_for_connection_type(destination.connection_type)

    # Matched before any names go in: a connection called "CDC Postgres" isn't a
    # request for the CDC template.
    template_key = match_template_from_intent_and_answers(source_type, sink_type, answers)
    if not template_key:
        return None
    # Every template writes through a database connection, so another kind of
    # destination fits none of them. A plain upsert needs no template once the
    # table and key are known: the Destination step does it, and this template
    # would leave SQL for the person to finish.
    if destination is not None and (destination.kind != "connection" or template_key == "sql_server_upsert"):
        return None

    if destination is not None:
        answers["sql_server_connection"] = destination.name
        answers["db_connection"] = destination.name
        if destination.table:
            answers["sql_server_table"] = destination.table

    wf = populate_template(template_key, answers, choices=choices)
    if wf is None or not wf.steps:
        return None
    wf.workspace_id = workspace_id or "default"
    if not preview:
        from revoluzen_flow.main import app_state
        app_state["store"].save(
            wf,
            change_summary=f"Template ({template_key}) populated from clarify answers",
        )

    reply = (
        f"I've drafted **{wf.name}** from a built-in template.\n\n"
        f"Steps: {' → '.join(s.label for s in wf.steps)}\n\n"
    )
    reply += _choices_text(choices, wf)
    if any(_TEMPLATE_PLACEHOLDER_RE.search(str(step.params)) for step in wf.steps):
        reply += (
            "Some settings still hold a placeholder in angle brackets. Open those "
            "steps and fill them in before you run it.\n\n"
        )
    reply += next_step
    return {
        "reply": reply,
        "workflow": wf.model_dump(mode="json"),
        "confidence": 0.9,
        "ai_powered": False,
        "intent": "template_populated",
        "template_key": template_key,
        "matched_fields": list(parsed.matched_fields),
        "unmatched_fields": list(parsed.unmatched_fields),
    }


class ApplyDraftRequest(BaseModel):
    name: str = "AI Pipeline"
    description: str = ""
    steps: list[Step]
    connections: list[StepConnection] = Field(default_factory=list)


def _point_at_uploaded_files(steps: list, workspace_id: str) -> None:
    """Let a draft find the files it names.

    People and models say "tickets.csv", but Storage keeps an upload under a
    stamped name, so a draft that used the plain name failed with "no files
    found" even though the file was right there on the Storage page.
    """
    from revoluzen_flow.datastore.models import OBJECT_KIND_FILE
    from revoluzen_flow.datastore.store import get_store

    try:
        objects = get_store().list_objects(workspace_id, kind=OBJECT_KIND_FILE)
    except Exception:  # noqa: BLE001
        return
    newest_by_name: dict[str, str] = {}
    for obj in objects:  # newest first
        newest_by_name.setdefault(obj.name, obj.path)
    for step in steps:
        named = step.params.get("file_path")
        if isinstance(named, str) and named in newest_by_name:
            step.params["file_path"] = newest_by_name[named]
            if step.params.get("path") == named:
                step.params["path"] = newest_by_name[named]


@router.post("/apply-draft", dependencies=[Depends(require_min_rank_when_identified("developer"))])
async def apply_draft(
    body: ApplyDraftRequest,
    request: Request,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Save a reviewed chat draft as a new pipeline in the caller's workspace.

    Only the draft's content is taken from the request: the id, status and
    workspace are set here, so applying can't overwrite another pipeline.
    """
    if not body.steps:
        raise HTTPException(400, "A draft needs at least one step")
    _point_at_uploaded_files(body.steps, workspace_id or "default")
    from revoluzen_flow.api.workflows import _owner_fields
    workflow = Workflow(
        name=body.name,
        description=body.description,
        workspace_id=workspace_id or "default",
        steps=body.steps,
        connections=body.connections,
        **_owner_fields(request),
    )
    from revoluzen_flow.main import app_state
    app_state["store"].save(workflow, change_summary=f"Applied chat draft: {workflow.name[:100]}")
    return {"workflow": workflow.model_dump(mode="json")}


@router.get("/ai-status")
async def ai_status(request: Request, workspace_id: str = Depends(_safe_workspace_id)):
    """Check if AI provider is configured."""
    return {"ai_available": is_ai_available(user_id=_caller_user_id(request), workspace_id=workspace_id)}


# ── Canvas chat (Assistant on the editor page) ─────────────────────────────
#
# The Assistant panel sends the full chat history PLUS a snapshot of the
# editor's canvas (nodes/edges/parameters/validation/status). The LLM gets
# a system prompt that frames it as a data-pipeline assistant aware of the
# user's current canvas. The reply is plain natural language — no JSON
# parsing — so questions like "what does this do" / "what are the issues" /
# "can we build a pipeline from sql for each table" feel like a real chat.
#
# Returns `{ reply, ai_powered }`. When `ai_available()` is False the
# frontend falls back to its client-side smart handlers (which are
# canvas-aware too, just keyword-driven).

class CanvasNode(BaseModel):
    id: str
    type: str
    label: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class CanvasEdge(BaseModel):
    source: str
    target: str
    condition: str | None = None


class CanvasParameter(BaseModel):
    name: str
    type: str | None = None
    default: Any | None = None
    required: bool | None = None


class CanvasValidationIssue(BaseModel):
    level: str
    step_id: str | None = None
    message: str


class CanvasContext(BaseModel):
    workflow_id: str | None = None
    workflow_name: str = "(unsaved)"
    status: str = "draft"
    version: int = 0
    nodes: list[CanvasNode] = Field(default_factory=list)
    edges: list[CanvasEdge] = Field(default_factory=list)
    parameters: list[CanvasParameter] = Field(default_factory=list)
    issues: list[CanvasValidationIssue] = Field(default_factory=list)


class CanvasChatRequest(BaseModel):
    messages: list[ChatMessage]
    canvas: CanvasContext


def _canvas_snapshot_for_prompt(c: CanvasContext) -> str:
    """Render the canvas as a compact, LLM-readable block. We cap each
    list at 50 entries to bound token cost — pipelines with hundreds of
    nodes will get summarised."""
    lines: list[str] = []
    lines.append(f"PIPELINE: {c.workflow_name!r} (id={c.workflow_id or 'unsaved'}, status={c.status}, v{c.version})")
    if not c.nodes:
        lines.append("CANVAS: empty (no nodes yet)")
        return "\n".join(lines)
    lines.append(f"CANVAS: {len(c.nodes)} step(s), {len(c.edges)} connection(s)")
    lines.append("")
    lines.append("STEPS:")
    for n in c.nodes[:50]:
        # Params printed shallowly so the LLM sees the connector_type /
        # file_path / table / condition without exploding token cost.
        keys = ", ".join(f"{k}={v!r}" for k, v in list((n.params or {}).items())[:6])
        lines.append(f"  - {n.id} [{n.type}] {n.label or ''} {('('+keys+')') if keys else ''}".rstrip())
    if len(c.nodes) > 50:
        lines.append(f"  ... and {len(c.nodes) - 50} more steps")
    lines.append("")
    lines.append("CONNECTIONS:")
    for e in c.edges[:50]:
        cond = f" [{e.condition}]" if e.condition else ""
        lines.append(f"  - {e.source} -> {e.target}{cond}")
    if len(c.edges) > 50:
        lines.append(f"  ... and {len(c.edges) - 50} more connections")
    if c.parameters:
        lines.append("")
        lines.append("PARAMETERS:")
        for p in c.parameters[:30]:
            req = " [required]" if p.required else ""
            default = f" default={p.default!r}" if p.default is not None else ""
            lines.append(f"  - {p.name} : {p.type or 'any'}{req}{default}")
    if c.issues:
        lines.append("")
        lines.append("VALIDATION ISSUES:")
        for i in c.issues[:20]:
            sid = f" ({i.step_id[:8]})" if i.step_id else ""
            lines.append(f"  - [{i.level}]{sid} {i.message}")
    return "\n".join(lines)


CANVAS_CHAT_SYSTEM_PROMPT = """You are Revoluzen Flow Assistant, an expert data-engineering copilot embedded in Revoluzen Flow's pipeline editor. The user is looking at a pipeline canvas and may ask questions about it, request changes, or describe new pipelines they want built.

Rules:
- Answer the user's QUESTION directly using the canvas snapshot you're given. Do not pretend the canvas is empty if it has nodes; do not fabricate node names.
- Be concise. Markdown is fine. Use bullets for lists. Code fences for SQL/JSON. No preamble like "Sure! I'd be happy to..." — go straight to the answer.
- When the user asks to BUILD or MODIFY a pipeline, briefly describe the plan (sources -> transforms -> destinations) and tell them they can click **Generate** in the prompt card to commit it, or you can do it for them if they confirm. Do NOT invent JSON in the chat reply.
- When the user asks "what does this do" / "explain" / "describe" — walk through the steps using the actual labels and step types from the snapshot.
- When the user asks about issues / errors / validation — read the VALIDATION ISSUES section and report them. If empty, say so plainly.
- When the user asks about last-run status or schedules — say you don't have that in this context unless it's in the snapshot.
- Never claim to have created or modified a pipeline. The user controls the canvas; you only advise.
- If the user asks something unrelated to data pipelines (weather, jokes, world facts), politely redirect them back to the canvas."""


@router.post("/canvas-chat")
async def canvas_chat(
    body: CanvasChatRequest,
    request: Request,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Natural-language chat anchored to the editor's canvas state.

    Returns the LLM's plain-text reply. When no AI provider is
    configured, returns `ai_available=False` so the frontend can fall
    back to its client-side smart handlers.
    """
    if not body.messages:
        return {"reply": "What can I help you build or explain?", "ai_powered": False, "ai_available": False}

    user_id = _caller_user_id(request)
    if not is_ai_available(user_id=user_id, workspace_id=workspace_id):
        return {
            "reply": "",
            "ai_powered": False,
            "ai_available": False,
        }

    snapshot = _canvas_snapshot_for_prompt(body.canvas)
    system_prompt = f"{CANVAS_CHAT_SYSTEM_PROMPT}\n\n--- CURRENT CANVAS ---\n{snapshot}\n--- END CANVAS ---"

    chat_msgs = [{"role": m.role, "content": m.content} for m in body.messages]
    asked = next((m["content"] for m in reversed(chat_msgs) if m["role"] == "user"), "")
    with ai_call_details(intent=asked):
        text = await ai_generate_text(
            chat_msgs,
            system_prompt=system_prompt,
            source_label="canvas-chat",
            user_id=user_id,
            workspace_id=workspace_id,
            max_tokens=1024,
        )
    if not text:
        return {
            "reply": "",
            "ai_powered": False,
            "ai_available": True,
            "error": "LLM call returned no text",
        }
    return {"reply": text.strip(), "ai_powered": True, "ai_available": True}
