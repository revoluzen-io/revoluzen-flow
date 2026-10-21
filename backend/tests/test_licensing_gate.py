"""The edition gate, and the CDC node that uses it.

`edition-matrix.md` declares CDC sources, Vector DB sinks and the JDBC
dialect registry as Plus-only. Until 2026-10-21 none of them was enforced:
the declaration lived in that table, in `ai/session_context.py` and in
anti-pattern rule 25, while the nodes registered and ran on any instance.
There was no node-level gate anywhere to copy, so `revoluzen_flow/licensing.py`
is the pattern rather than a one-off.

These tests pin three things that are easy to get wrong in an edition gate:

1. **It fails closed.** A gate that opens when detection breaks is not a gate.
2. **It is not retried.** `engine/failure_class.py` must classify the refusal
   as USER_INPUT, because no number of attempts produces a licence.
3. **It gates execution, not registration.** The node stays visible and
   configurable on a free instance so the catalog does not differ between
   editions; the refusal happens when it runs.
"""

from __future__ import annotations

import pytest

from revoluzen_flow.licensing import PlusRequiredError, is_plus, require_plus


# ── Detection ────────────────────────────────────────────────────────────────


class FakeLicenseManager:
    def __init__(self, plus: bool):
        self.is_plus = plus


@pytest.fixture
def app_state(monkeypatch):
    """Patch the app_state dict that licence detection reads."""
    state: dict = {}
    monkeypatch.setattr("revoluzen_flow.main.app_state", state, raising=False)
    return state


def test_no_license_manager_means_free(app_state):
    """The OSS case: Plus injects the manager, so absence is free."""
    assert is_plus() is False


def test_an_active_license_manager_means_plus(app_state):
    app_state["license_manager"] = FakeLicenseManager(plus=True)
    assert is_plus() is True


def test_an_inactive_license_manager_means_free(app_state):
    """A manager present but reporting not-plus (expired, invalid) is free."""
    app_state["license_manager"] = FakeLicenseManager(plus=False)
    assert is_plus() is False


def test_a_manager_without_the_attribute_means_free(app_state):
    """A partially initialised manager must not read as Plus."""
    app_state["license_manager"] = object()
    assert is_plus() is False


def test_detection_fails_closed_when_app_state_is_unavailable(monkeypatch):
    """REGRESSION GUARD: an edition check that defaults to paid is not a check.

    If importing app_state raises — during early startup, in a trimmed
    deployment, or in a test harness — the answer must be free, never Plus.
    """
    import builtins

    real = builtins.__import__

    def boom(name, *a, **kw):
        if name == "revoluzen_flow.main":
            raise ImportError("not available")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert is_plus() is False


# ── require_plus ─────────────────────────────────────────────────────────────


def test_require_plus_passes_on_plus(app_state):
    app_state["license_manager"] = FakeLicenseManager(plus=True)
    require_plus("Some feature")  # must not raise


def test_require_plus_refuses_on_free_and_names_the_feature(app_state):
    """The person who hits this is often not the person who built the pipeline.

    "This requires Plus" with no subject is not actionable, so the feature
    name has to be in the message.
    """
    with pytest.raises(PlusRequiredError) as exc:
        require_plus("CDC Source (change data capture)")

    msg = str(exc.value)
    assert "CDC Source" in msg
    assert "Enterprise" in msg
    assert "edition-matrix.md" in msg, "point the reader at the authority"


def test_plus_required_is_its_own_type():
    """So the UI can offer an upgrade path, not a generic failure."""
    assert issubclass(PlusRequiredError, RuntimeError)


# ── Retry classification ─────────────────────────────────────────────────────


def test_a_licence_refusal_is_not_retried():
    """Retrying cannot produce a licence, so it must classify as USER_INPUT.

    `_EXCEPTION_TYPE_RULES` is keyed on class *name*, not isinstance, so
    subclassing RuntimeError does not get this for free — the entry has to be
    registered explicitly, and this test is what notices if it is removed.
    """
    from revoluzen_flow.engine.failure_class import (
        FailureClass, classify_error, retry_advisable,
    )

    result = classify_error(PlusRequiredError("CDC Source requires Enterprise"))
    assert result == FailureClass.USER_INPUT
    assert retry_advisable(result) is False


# ── The CDC node ─────────────────────────────────────────────────────────────


def test_cdc_node_is_registered_on_free_instances():
    """Gate execution, not registration.

    A free user should be able to see the node and build a pipeline against
    it; hiding it would make the catalog differ between editions, which the
    open-core contract avoids.
    """
    from revoluzen_flow.ir.schema import StepType
    from revoluzen_flow.nodes.registry import NodeRegistry

    assert NodeRegistry.get(StepType.CDC_SOURCE) is not None


def test_cdc_refuses_to_execute_without_a_licence(app_state):
    """The actual hole this change closes: CDC used to run on free."""
    from revoluzen_flow.connectors.cdc import CdcSourceNode

    node = CdcSourceNode.__new__(CdcSourceNode)
    node.params = {"mode": "kafka_topic", "kafka_topic": "t"}

    with pytest.raises(PlusRequiredError, match="CDC Source"):
        node.execute(ctx=None)  # type: ignore[arg-type]


def test_cdc_checks_the_licence_before_touching_params(app_state):
    """Order matters: the licence error must not be masked by a config error.

    With no `kafka_topic`, the old code raised ValueError("kafka_topic is
    required"). If the gate sat after that, a free user would get a confusing
    config complaint about a node they are not licensed to run at all.
    """
    from revoluzen_flow.connectors.cdc import CdcSourceNode

    node = CdcSourceNode.__new__(CdcSourceNode)
    node.params = {}  # no mode, no topic — would be a ValueError if reached

    with pytest.raises(PlusRequiredError):
        node.execute(ctx=None)  # type: ignore[arg-type]
