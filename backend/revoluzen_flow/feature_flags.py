"""
Revoluzen Flow feature flags — Stage 2 (2026-04-19).

Purpose
─────────────────────────────────────────────────────────────────────
Explicit, operator-visible on/off switches for the enterprise feature
surface (marketplace, lineage, collaboration, plugins). A disabled
feature must:

  1. NOT instantiate its store in lifespan (zero memory footprint).
  2. Fail LOUDLY at the route boundary with FeatureDisabledError,
     never silent None / AttributeError / KeyError.
  3. Be inspectable via /api/health/memory so operators can verify.

This is reviewer 2's "never silent None" discipline. The whole point
is that when marketplace is off, the operator SEES it's off — in the
health endpoint, in the logs, and if a request happens to hit a
gated route, in a 503 response with a clear reason.

Environment
─────────────────────────────────────────────────────────────────────
All flags default to ON ("1") so upgrading to Stage 2 does NOT silently
disable anything. To turn a feature off, set the env var to "0":

    REVOLUZEN_FLOW_ENABLE_MARKETPLACE=0
    REVOLUZEN_FLOW_ENABLE_LINEAGE=0
    REVOLUZEN_FLOW_ENABLE_COLLABORATION=0
    REVOLUZEN_FLOW_ENABLE_PLUGINS=0

Why these four? They are the heaviest optional stores in main.py and
they are each opt-in enterprise features — a Free-tier single-user
install may legitimately want them off to shrink idle RSS.

Why NOT gate workflows/projects/schedules/credentials? Those are the
core product surface — turning them off wouldn't make Revoluzen Flow a
"lighter Revoluzen Flow," it would make it "not Revoluzen Flow."

Usage
─────────────────────────────────────────────────────────────────────

Lifespan startup (main.py):

    if flags.is_enabled("marketplace"):
        app_state["marketplace_store"] = MarketplaceStore(db=db)

Router (api/marketplace.py):

    from revoluzen_flow.feature_flags import require

    @router.get("/items")
    async def list_items():
        require("marketplace")       # raises FeatureDisabledError if off
        return app_state["marketplace_store"].list()

Exception is mapped to HTTP 503 in main.py's global exception handler
(the existing handler already returns 500 for unhandled errors; we
add a narrow handler for FeatureDisabledError specifically so the
operator-visible 503 message is clean).

Stability promise
─────────────────────────────────────────────────────────────────────
Flag names are STABLE API. Operators set them in env files and
docker-compose. Do not rename without a deprecation window.
"""

from __future__ import annotations

import os
from typing import Final


class FeatureDisabledError(RuntimeError):
    """Raised when a request hits a route guarded by a disabled feature.

    Carries the feature key so the global exception handler can include
    it in the 503 response and operators can see immediately which
    REVOLUZEN_FLOW_ENABLE_* var needs to change.
    """

    def __init__(self, feature: str) -> None:
        self.feature = feature
        super().__init__(
            f"Feature '{feature}' is disabled "
            f"(set REVOLUZEN_FLOW_ENABLE_{feature.upper()}=1 to enable)."
        )


# Canonical flag list. Adding a flag here is the only place new flags
# should be declared — routers and lifespan both read through is_enabled()
# so they cannot disagree on what's defined.
_FLAG_KEYS: Final[tuple[str, ...]] = (
    "marketplace",
    "lineage",
    "collaboration",
    "plugins",
    # 2026-10-21: lets an instance serve the API without running the
    # scheduler loop (`REVOLUZEN_FLOW_ENABLE_SCHEDULER=0`), which is what makes
    # more than one instance possible at all. Previously every instance
    # started its own scheduler unconditionally, so a second one behind a
    # load balancer fired every schedule twice.
    #
    # Defaults ON like every other flag, so a single-node install is
    # unaffected. Turning it off is the deliberate act of designating a node
    # API-only; the durable lease in `scheduling/lease.py` is the safety net
    # for when someone forgets.
    "scheduler",
)


def _read_env(key: str, default: str = "1") -> bool:
    """Parse an REVOLUZEN_FLOW_ENABLE_<KEY> env var. Accepts 1/0, true/false,
    yes/no, on/off. Anything else is treated as the default so a typo
    doesn't silently flip a feature off."""
    raw = os.environ.get(f"REVOLUZEN_FLOW_ENABLE_{key.upper()}", default).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    # Unknown value → fall back to default rather than silently disabling.
    return default.lower() in {"1", "true", "yes", "on"}


# Snapshot is computed ONCE at import and frozen for the life of the
# process. Operators change flags by restarting the process — this is
# the same contract every sane feature-flag system offers, and it
# eliminates the "did the flag change between request start and store
# lookup?" race entirely.
FLAGS: Final[dict[str, bool]] = {key: _read_env(key) for key in _FLAG_KEYS}


def is_enabled(feature: str) -> bool:
    """Return True if the feature is enabled. Unknown feature → False,
    because an unknown key almost certainly means a typo at a call site
    and we'd rather fail closed than silently allow it."""
    return FLAGS.get(feature, False)


def require(feature: str) -> None:
    """Raise FeatureDisabledError if the feature is disabled.

    This is the guard called at the top of a gated route handler:

        @router.get("/items")
        async def list_items():
            require("marketplace")
            ...

    The exception propagates to FastAPI, which returns a 503 via the
    handler registered in main.py.
    """
    if not is_enabled(feature):
        raise FeatureDisabledError(feature)


def snapshot() -> dict[str, bool]:
    """Return a copy of the current flag state for observability.

    Surfaced by /api/health/memory so operators can verify their
    REVOLUZEN_FLOW_ENABLE_* settings actually took effect. Returned as a new
    dict each call so callers can't mutate internal state.
    """
    return dict(FLAGS)


def all_known_flags() -> tuple[str, ...]:
    """Return the canonical flag names. Used by docs and admin UI."""
    return _FLAG_KEYS
