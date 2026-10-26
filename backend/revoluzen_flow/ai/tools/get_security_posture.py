"""
get_security_posture tool — read.

Gives the Copilot the Security & Diagnostics baseline so it can answer
"why is master key permissions failing", "is my install secure", "what do
I do about this" from THIS install rather than from general knowledge.

Why this exists. The Copilot had 32 tools and none of them could see the
trust posture, while the system prompt tells it "never refuse a
reasonable question by saying 'I don't have that capability' without
first trying the obvious tool". With no obvious tool, a question about a
failing security row was answered from the model's background knowledge
of Windows ACLs — confident, plausible, and about nobody's install in
particular. On the one page whose value is that it does not overclaim,
that was the worst available outcome.

HONESTY SCOPE. Four of the seven controls are really checked; three are
not, and the difference is load-bearing:

  • Checked now — HTTP security headers (a live loopback request),
    stored credential encryption (our own rows inspected for the ENC:v1
    envelope), master key file permissions (POSIX mode bits / NTFS DACL),
    audit retention (row count + oldest entry).
  • NOT checked — SQL input handling (needs adversarial testing),
    HTTP rate limiting and intermediate data encryption (configuration
    can be read, enforcement cannot be exercised). Reporting a config
    read as "verified" is exactly the overclaim this page avoids.

`not_checked` therefore means "nothing was tested", NOT "tested and
fine". The payload carries `coverage` and every row carries its own
`detail` scope line so the model says "this was not checked, here is
why" instead of inventing a verdict.

Read-only: inspects files and database rows, changes nothing.
"""

from __future__ import annotations

from typing import Any

from revoluzen_flow.ai.tools.base import ToolContext, ToolDefinition, ToolTier

def _empty_payload(reason: str) -> dict[str, Any]:
    """Honest stub — never an empty list that reads as 'all clear'."""
    return {
        "controls": [],
        "summary": {"verified": 0, "configured": 0, "not_checked": 0, "failed": 0},
        "coverage": reason,
    }


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    only_problems = bool(inputs.get("only_problems"))
    try:
        from revoluzen_flow.api.trust import _security_baseline
        rows = list(_security_baseline())
    except Exception as exc:  # noqa: BLE001 — a check bug must not break Copilot
        return _empty_payload(
            f"Could not read the security baseline: {type(exc).__name__}."
        )

    # The headers probe is async and lives on the diagnostics endpoint; run
    # it here too so the Copilot sees the same rows the page shows.
    try:
        from revoluzen_flow.api.trust_checks import check_security_headers
        base = getattr(ctx, "base_url", None) or "http://127.0.0.1:8000"
        rows.append(await check_security_headers(str(base)))
    except Exception:  # noqa: BLE001
        rows.append({
            "key": "security_headers", "label": "HTTP security headers",
            "status": "not_checked",
            "detail": "The loopback probe did not run for this query.",
            "checked_at": None, "evidence": None,
        })

    summary = {"verified": 0, "configured": 0, "not_checked": 0, "failed": 0}
    out: list[dict[str, Any]] = []
    for r in rows:
        status = r.get("status", "not_checked")
        summary[status] = summary.get(status, 0) + 1
        if only_problems and status not in ("failed",):
            continue
        row = {
            "control": r.get("label"),
            "key": r.get("key"),
            "status": status,
            # The scope line is the point — it says what the check did NOT
            # cover, which is what stops the model overstating a pass.
            "scope": r.get("detail"),
            "evidence": r.get("evidence"),
            "checked_at": r.get("checked_at"),
        }
        # The fix comes from the check itself — it knows the real path and
        # the caller's SID, so it can emit a command the user can paste
        # rather than a template with placeholders. Keeping it in one place
        # also means the Copilot and the page can never disagree.
        fix = r.get("remediation")
        if fix:
            row["how_to_fix"] = fix
        out.append(row)

    return {
        "controls": out,
        "summary": summary,
        "coverage": (
            "Four controls are actually checked: HTTP security headers (live "
            "loopback request), stored credential encryption (our own rows), "
            "master key file permissions (POSIX mode / NTFS DACL), audit retention "
            "(row count + oldest entry). THREE ARE NOT: SQL input handling, HTTP "
            "rate limiting and intermediate data encryption — their configuration "
            "exists but enforcement is not exercised here. A status of "
            "'not_checked' means nothing was tested, NOT that the control passed. "
            "Each row's `scope` states what that check did not cover; quote it "
            "rather than summarising it away."
        ),
    }


DEFINITION = ToolDefinition(
    name="get_security_posture",
    tier=ToolTier.READ,
    description=(
        "Read this installation's Security & Diagnostics baseline — the same rows "
        "shown on Settings → Security & Diagnostics. Returns each control with its "
        "status (verified / configured / not_checked / failed), the scope of what "
        "was actually tested, the evidence, and for failures a concrete fix. Use "
        "for 'is my install secure', 'why is master key permissions failing', "
        "'what does not checked mean', 'how do I fix this', 'are my credentials "
        "encrypted', 'security headers'. IMPORTANT: 'not_checked' means the check "
        "did not run or had nothing to inspect — never report it as passing. Three "
        "controls (SQL input handling, rate limiting, intermediate data encryption) "
        "are deliberately never checked here and need dedicated testing."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "only_problems": {
                "type": "boolean",
                "description": "Return only controls whose status is 'failed'. "
                               "Default false (all controls).",
            },
        },
    },
    output_schema={
        "controls": "list",
        "summary": "dict",
        "coverage": "str",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["security", "trust", "posture", "diagnostics", "compliance", "read"],
)
