"""
Trust API — Gate 4 of the launch scorecard.

The trust artifact bundle is the public-facing answer to "is this safe to
run inside our environment". Per `project_revoluzen_flow_local_only_lock` and
`project_revoluzen_flow_positioning_lock` the answer leads with sovereignty (data
stays on the box) — AI is operating-model detail, not headline.

Endpoints:
  GET /api/trust/posture
    Returns the live security/sovereignty posture as a single JSON
    document. The `/trust` frontend page renders this. Stable shape so
    operators can scrape it for compliance reviews.

  GET /api/trust/eval-summary
    Returns the most recent eval harness output. Used by the trust page
    to show the empirical "AI agent gets it right" pass rate. Falls back
    to a never-run sentinel if the eval has never been executed.

  GET /api/trust/supported-models
    Returns the supported-models policy as structured JSON, matching
    `docs/supported-models.md` (which is the authoritative source).
"""

from __future__ import annotations

import json
import logging
import os
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from revoluzen_flow.audit import audit_action
from revoluzen_flow.auth.deps import (
    current_workspace_id,
    require_auth,
    require_min_rank_when_identified,
)

router = APIRouter(prefix="/api/trust", tags=["trust"])
logger = logging.getLogger(__name__)


# ── Static policy — supported models ─────────────────────────────────


# Mirror of docs/supported-models.md. Keep these in sync — the doc is the
# operator-readable copy; this struct is what the trust page renders.
_SUPPORTED_MODELS = {
    "policy_version": "1.0",
    "policy_url": "/docs/supported-models.md",
    "default_local_cpu": {
        "name": "qwen2.5:7b",
        "provider": "ollama",
        "size_gb": 4.7,
        "tool_capable": True,
        "rationale": (
            "Reliable tool-use floor (2026-05-19 revision) for CPU agentic use. "
            "Models smaller than 7B advertise tool schemas but silently return "
            "greetings instead of calling tools. Privacy: data never leaves the host."
        ),
    },
    "tiers": [
        {
            "tier": "cpu_laptop",
            "model": "qwen2.5:7b",
            "ram_min_gb": 8,
            "tool_use_latency_s": "30–60",
            "default": True,
        },
        {
            "tier": "workstation_consumer_gpu",
            "model": "qwen2.5:14b",
            "ram_min_gb": 16,
            "vram_min_gb": 12,
            "tool_use_latency_s": "1–3",
        },
        {
            "tier": "gpu_server",
            "model": "llama3.1:70b-q4",
            "ram_min_gb": 32,
            "vram_min_gb": 48,
            "tool_use_latency_s": "<1",
        },
    ],
    "cloud_escape_hatch": {
        "supported": True,
        "default": False,
        "providers_supported": ["anthropic", "openai", "openrouter", "gemini",
                                "deepseek", "groq", "mistral", "azure"],
        "warning": (
            "When a cloud provider is selected, prompt + selected tool inputs "
            "leave the host. Default is local-only. Operators MUST opt in via "
            "Insights → AI Provider."
        ),
    },
    "deprecated_recommendations": [
        # Documented record of older defaults so `git log`-style audits can
        # see the policy evolution. Keep latest at the top.
        {
            "model": "qwen2.5:3b",
            "deprecated_on": "2026-05-19",
            "reason": (
                "Sub-floor tool-use reliability — advertises tool schemas but "
                "returns greetings or empty responses instead of calling tools."
            ),
        },
        {
            "model": "qwen2.5:1.5b",
            "deprecated_on": "2026-05-19",
            "reason": (
                "Sub-floor tool-use reliability — same failure mode as the 3b "
                "(silent greetings instead of tool calls)."
            ),
        },
        {
            "model": "llama3.1:8b",
            "deprecated_on": "2026-05-03",
            "reason": "30–60s/turn on CPU laptops — unusable for tool-use.",
        },
    ],
}


# ── Posture builder ──────────────────────────────────────────────────


def _read_telemetry_consent() -> bool | None:
    """Best-effort read of the persisted telemetry consent flag.

    Missing settings default to off. Read failures remain unknown.
    """
    try:
        from revoluzen_flow.main import app_state
        db = app_state.get("db")
        if db is None:
            return None
        row = db.fetchone("SELECT data FROM settings WHERE id = 'admin_settings'")
        settings = json.loads(row["data"]) if row else {}
        value = settings.get("telemetry_enabled", False)
        return value if isinstance(value, bool) else None
    except Exception:  # noqa: BLE001
        return None


def _provider_status(user_id=None, workspace_id=None) -> dict[str, Any]:
    """Summarise the active AI provider WITHOUT leaking config details.

    Public endpoint, so we never return base_url, account, key prefixes,
    etc. — just whether the active provider is local, the model name, and
    whether tool calls are happening on-box.
    """
    try:
        from revoluzen_flow.planner.ai_client import resolve_provider
        provider, _key, model, _base = resolve_provider(user_id=user_id, workspace_id=workspace_id)
    except Exception:  # noqa: BLE001
        return {"available": False, "is_local": None, "provider": "unknown", "model": "", "status": "failed"}
    available = bool(provider and provider != "none")
    is_local = (True if provider == "ollama" else None if provider in ("none", "custom") else False)
    return {
        "available": available,
        "status": "configured" if available else "not_checked",
        "provider": provider or "none",
        "model": model or "",
        "is_local": is_local,
    }


# Controls that genuinely cannot be settled by a process inspecting
# itself. `sql_input_sanitization` needs adversarial testing of connector
# and query paths; `rate_limiting` and `data_at_rest_encryption` can have
# their configuration read but not their ENFORCEMENT exercised, and a
# config read reported as "verified" would be exactly the overclaim this
# page exists to avoid. They stay `not_checked` until something actually
# tests them.
_UNVERIFIED_CONTROLS = [
    ("sql_input_sanitization", "SQL input handling",
     "Connector and query paths require dedicated security testing."),
    ("rate_limiting", "HTTP rate limiting",
     "Enforcement has not been exercised by this check."),
    ("data_at_rest_encryption", "Intermediate data encryption",
     "Edition or configuration alone does not prove data is encrypted."),
]


def _security_baseline() -> list[dict[str, Any]]:
    """The controls this process can settle by inspecting itself.

    Declared controls are not evidence of a successful security audit —
    but refusing to look at our own machinery was not evidence of
    anything either. Every row here used to be hard-coded `not_checked`
    with no timestamp, including controls whose implementations live in
    this repository. The checks that can observe something now do, and
    the ones that cannot say why.

    Synchronous checks only. The security-headers probe is async (it
    makes a loopback request) and is added by the caller.
    """
    from revoluzen_flow.api.trust_checks import (
        check_audit_retention, check_credential_encryption,
        check_master_key_permissions,
    )
    db = None
    try:
        from revoluzen_flow.main import app_state
        db = app_state.get("db")
    except Exception:  # noqa: BLE001 — diagnostics must never 500
        db = None

    rows = [
        check_credential_encryption(db),
        check_master_key_permissions(),
        check_audit_retention(db),
    ]
    rows.extend(
        {"key": key, "label": label, "status": "not_checked", "detail": detail,
         "checked_at": None, "evidence": None}
        for key, label, detail in _UNVERIFIED_CONTROLS
    )
    # Tell the caller which rows this process can fix itself, rather than
    # making every client keep its own list of fixable keys in sync with
    # `_REMEDIATIONS`. Only on `failed`: offering to fix a passing control
    # invites someone to "fix" a control that is already correct.
    for row in rows:
        row["can_auto_fix"] = (
            row.get("status") == "failed" and row.get("key") in _REMEDIATIONS
        )
    return rows


def _sovereignty(user_id=None, workspace_id=None) -> dict[str, Any]:
    """The headline sovereignty story. Lead the trust page with this."""
    provider = _provider_status(user_id, workspace_id)
    return {
        "data_stays_local_by_default": True,
        "telemetry_default_off": True,
        "telemetry_currently_enabled": _read_telemetry_consent(),
        "active_provider_is_local": provider["is_local"],
        "active_provider_summary": provider,
        "host_os": platform.system(),
        "deployment_model": (
            "Self-hosted. Outbound network activity has not been measured."
        ),
    }


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("/posture")
def trust_posture() -> dict[str, Any]:
    """The full trust posture document. Stable shape — bump
    `posture_version` if you break compatibility for compliance scrapers.
    """
    return {
        "posture_version": "2.0",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "sovereignty": _sovereignty(),
        "security_baseline": _security_baseline(),
        "supported_models": _SUPPORTED_MODELS,
    }


@router.get("/diagnostics")
async def diagnostics(request: Request, user=Depends(require_auth)) -> dict[str, Any]:
    result = {
        "posture_version": "2.0",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "sovereignty": _sovereignty(user.id, current_workspace_id(request)),
        "security_baseline": _security_baseline(),
    }
    result["scope"] = "current_user_and_workspace"

    # The headers probe is a real request to ourselves, so it belongs on
    # the async endpoint rather than inside the synchronous baseline.
    # `request.base_url` is the address this very request arrived on, which
    # is the server we want to measure. A failure here reports
    # `not_checked`, never `failed` — learning nothing is not a finding.
    try:
        from revoluzen_flow.api.trust_checks import check_security_headers
        result["security_baseline"].append(
            await check_security_headers(str(request.base_url))
        )
    except Exception as exc:  # noqa: BLE001 — diagnostics must never 500
        logger.warning("security-headers check failed to run: %s", exc)
        result["security_baseline"].append({
            "key": "security_headers", "label": "HTTP security headers",
            "status": "not_checked",
            "detail": "The loopback probe did not run on this request.",
            "checked_at": None, "evidence": None,
        })
    result["security_baseline"].append({
        "key": "telemetry_consent", "label": "Telemetry consent setting",
        "status": "verified" if result["sovereignty"]["telemetry_currently_enabled"] is not None else "failed",
        "detail": "Consent setting read; this is not a network-egress audit.",
        "checked_at": result["as_of"],
        "evidence": "Persisted telemetry consent reader",
    })
    return result


def _remediate_master_key_perms() -> tuple[bool, str]:
    """Lock the master key down to its owner, the way creation now does.

    Returns ``(attempted, message)``. It deliberately does NOT report
    success — the caller re-runs the check and lets the observed ACL say
    whether it worked. A remediation that claims victory and reports its
    own outcome is the same overclaim this page exists to avoid.
    """
    from revoluzen_flow.security.encryptor import master_key_path

    path = master_key_path()
    if not path.exists():
        return False, f"No master key file at {path} — nothing to fix."

    if os.name == "nt":
        from revoluzen_flow.security.win_acl import harden_file
        harden_file(path)
        return True, f"Reset the DACL on {path}."

    if os.name == "posix":
        try:
            path.chmod(0o600)
            path.parent.chmod(0o700)
        except OSError as exc:
            return False, f"Could not change permissions: {exc}"
        return True, f"Set mode 600 on {path} and 700 on its directory."

    return False, f"Permissions on {os.name} are not managed by this app."


#: Controls this process can fix on its own, keyed the same as the
#: baseline rows. A control appears here only when the fix is safe to run
#: unattended and reversible in effect — the UI shows a "Fix this" button
#: exactly for the keys in this map, so adding one is the whole contract.
#: Everything else stays a printed command the operator runs themselves.
_REMEDIATIONS = {
    "master_key_perms": _remediate_master_key_perms,
}


@router.post("/remediate/{control_key}")
def remediate_control(
    control_key: str,
    request: Request,
    user=Depends(require_min_rank_when_identified("admin")),
) -> dict[str, Any]:
    """Apply the known fix for a failing control, then re-check it.

    The response carries the freshly re-run baseline row, so the caller
    renders an observation rather than this endpoint's opinion: if the
    ACL is still wrong afterwards the row still says `failed` and the
    user sees that instead of a success toast over a broken state.
    """
    fixer = _REMEDIATIONS.get(control_key)
    if fixer is None:
        raise HTTPException(404, f"No automatic fix for control {control_key!r}")

    try:
        attempted, message = fixer()
    except Exception as exc:  # noqa: BLE001 — never 500 out of the trust page
        logger.warning("remediation for %s failed: %s", control_key, exc)
        attempted, message = False, f"The fix did not run ({type(exc).__name__})."

    # Re-run the real check. This is the only thing that decides the outcome.
    after = next(
        (r for r in _security_baseline() if r.get("key") == control_key), None
    )

    audit_action(
        action="trust.remediate",
        resource_type="security_control",
        resource_id=control_key,
        actor=getattr(user, "id", None) or "local",
        workspace_id=current_workspace_id(request),
        details={"attempted": attempted, "status_after": (after or {}).get("status")},
    )

    return {"control_key": control_key, "attempted": attempted,
            "message": message, "control": after}


@router.get("/supported-models")
def supported_models() -> dict[str, Any]:
    """Return the supported-models policy as JSON. Mirrors
    docs/supported-models.md (the human-readable copy)."""
    return _SUPPORTED_MODELS


@router.get("/eval-summary")
def eval_summary() -> dict[str, Any]:
    """Return the most recent eval-harness output, or a never-run sentinel.

    The harness writes its results to `data/eval/latest.json`. The trust
    page renders this so prospects see the empirical pass rate per
    category — Gate 4 evidence rather than aspiration.
    """
    candidates = [
        Path(os.environ.get("REVOLUZEN_FLOW_DATA_DIR", "data")) / "eval" / "latest.json",
        Path("data") / "eval" / "latest.json",
    ]
    for path in candidates:
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                payload.setdefault("source_path", str(path))
                return payload
            except Exception as exc:  # noqa: BLE001
                logger.warning("trust eval-summary: failed to read %s: %s", path, exc)
                continue
    return {
        "ran": False,
        "message": (
            "Eval harness has not been run on this install. "
            "Run `python -m revoluzen_flow.eval.run` to generate a baseline."
        ),
    }
