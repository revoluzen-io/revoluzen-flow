"""Edition gate — the single place that answers "is this instance Plus?".

Why this module exists
----------------------
`edition-matrix.md` declares three node families Plus-only: CDC sources,
Vector DB sinks, and the JDBC dialect registry. None of them was enforced at
runtime — the declaration lived only in that table, in
`ai/session_context.py`, and in anti-pattern rule 25, while the nodes
themselves registered and ran on any instance (found 2026-10-21).

There was also no node-level gate anywhere to copy, so this module *is* the
pattern rather than a one-off for CDC.

How tier is determined
----------------------
Per `edition-matrix.md` ("How tier is determined at runtime"): Revoluzen Flow
has **no environment variable** that declares itself Plus. Plus injects a
`LicenseManager` into `app_state["license_manager"]` at startup. If it is
absent, the instance is free. That single check is the source of truth, and
deliberately so — an env var would make the edition spoofable by anyone who
can set one.

This gate lives in the OSS repository and that is correct under Boundary Rule
2 ("Plus features never live in this repo"). The gate is not a Plus feature;
it is the OSS side of the contract, and it is what makes the boundary real
rather than documentary. Plus supplies the licence manager that opens it.

Note there are two older copies of this detection — `api/reports.py` and
`ai/session_context.py` each define a private `_detect_tier`. They are left
alone here to keep this change narrow; consolidating them onto `is_plus()` is
a sensible follow-up, since three implementations of one rule is how editions
drift apart.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class PlusRequiredError(RuntimeError):
    """A Plus-only capability was used on an instance without a licence.

    Its own type rather than a bare RuntimeError for two reasons: the UI can
    render an upgrade path instead of a generic failure, and
    `engine/failure_class.py` maps it to USER_INPUT so the retry policy does
    not attempt it five more times — no amount of retrying adds a licence.
    """


def is_plus() -> bool:
    """True when a Plus licence manager is present and active.

    Fails closed. Any import problem, missing app_state, or partially
    initialised licence manager means free — an edition check that defaults
    to "paid" when something goes wrong is not a check.
    """
    try:
        from revoluzen_flow.main import app_state

        lm = app_state.get("license_manager")
        return bool(lm and getattr(lm, "is_plus", False))
    except Exception:  # noqa: BLE001 — see docstring: fail closed
        return False


def require_plus(feature: str) -> None:
    """Refuse `feature` unless this instance has a Plus licence.

    The message names the feature, because the operator seeing it is usually
    someone who opened a shared pipeline rather than the person who built it,
    and "this requires Plus" without a subject is not actionable.
    """
    if is_plus():
        return
    raise PlusRequiredError(
        f"{feature} requires Revoluzen Flow Enterprise. This instance is "
        f"running the free edition. See edition-matrix.md for what each "
        f"edition includes."
    )
