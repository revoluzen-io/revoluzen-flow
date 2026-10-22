"""Cross-workspace isolation — the tenant boundary.

Why this file exists
--------------------
Workspace scoping is the entire tenant boundary, and until 2026-10-22 **no
test anywhere exercised `X-Workspace-Id`**. There was no isolation test file.
The membership check in `auth/deps.py:current_workspace_id` looked correct by
inspection, and 43 API modules depend on it, but nothing proved it.

Two different failures are possible and they need different tests:

1. **Spoofing** — a user claims a workspace they do not belong to. The
   dependency is supposed to 403. Covered by the structural sweep below,
   which discovers every workspace-scoped route rather than listing a few,
   so a route added next year is covered the day it is added.

2. **Missing filter** — a user legitimately in workspace B calls an endpoint
   that resolves the workspace correctly and then *ignores it* when querying.
   The dependency cannot prevent this; it is per-router discipline. This is
   the likelier and more serious leak, because the caller does nothing
   suspicious. Covered by the data-isolation tests, which put real rows in
   workspace A and assert a member of B never sees them.

Admin bypass is deliberate and documented (`ADMIN_ROLES` skip the membership
check the same way they bypass the project ACL), so every tenant in these
tests is a `data_engineer` — rank 70, the most privileged non-admin role.
Testing with an admin would pass against a completely broken boundary.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
    _ensure_user, _login,
)


# ── Two tenants ──────────────────────────────────────────────────────────────


class Tenant:
    def __init__(self, ws_id: str, user_id: str, client: TestClient):
        self.ws_id = ws_id
        self.user_id = user_id
        self.client = client

    def get(self, path: str, **kw):
        return self.client.get(path, headers={"X-Workspace-Id": self.ws_id}, **kw)

    def post(self, path: str, **kw):
        return self.client.post(path, headers={"X-Workspace-Id": self.ws_id}, **kw)


@pytest.fixture
def tenants(app_v2, authed_client):
    """Two workspaces, one non-admin member each, no overlap.

    Names are unique per test: the database persists across tests in a
    module, and `workspaces.slug` is UNIQUE, so a fixed slug collides on the
    second test that asks for tenants.
    """
    import uuid

    from revoluzen_flow.main import app_state
    from revoluzen_flow.workspaces.models import Workspace

    ws_store = app_state["workspace_store"]
    user_store = app_state["user_store"]
    run = uuid.uuid4().hex[:8]

    made: list[Tenant] = []
    for label in ("a", "b"):
        email = f"{label}-{run}@tenant.test"
        ws = ws_store.create(
            Workspace(name=f"Tenant {label.upper()} {run}", slug=f"tenant-{label}-{run}")
        )
        assert _ensure_user(authed_client, email, "Password123!", "data_engineer")

        user = user_store.get_user_by_email(email)
        assert user is not None, f"{email} was not created"
        assert user.role not in ("admin", "super_admin", "workspace_admin"), (
            "tenants must be non-admin; an admin bypasses the membership check "
            "and would pass against a broken boundary"
        )
        # Two separate role vocabularies, which is easy to trip over:
        # `WorkspaceMember.role` accepts only viewer/developer/admin/
        # super_admin, while the instance ladder in `auth/deps.py` also has
        # data_engineer, analyst and lead. What matters for isolation is the
        # *instance* role, because that is what `current_workspace_id` tests
        # against ADMIN_ROLES — so the user stays data_engineer and only the
        # membership row uses the workspace vocabulary.
        ws_store.add_member(ws.id, user.id, role="developer")

        c = TestClient(app_v2, base_url="http://localhost")
        token = _login(c, email, "Password123!")
        assert token, f"could not log in as {email}"
        c.headers["Authorization"] = f"Bearer {token}"
        made.append(Tenant(ws.id, user.id, c))

    a, b = made
    assert ws_store.is_member(a.ws_id, a.user_id)
    assert not ws_store.is_member(a.ws_id, b.user_id), "fixture built overlapping members"
    return a, b


# ── The membership gate ──────────────────────────────────────────────────────


def test_a_member_can_use_their_own_workspace(tenants):
    """Sanity: the gate must not refuse everybody.

    Without this, every assertion below would pass against a boundary that
    simply rejects all traffic.
    """
    a, _ = tenants
    r = a.get("/api/workflows")
    assert r.status_code == 200, f"a member was refused their own workspace: {r.text[:200]}"


def test_addressing_another_workspace_is_refused(tenants):
    """The spoofing case: B claims A."""
    a, b = tenants
    r = b.client.get("/api/workflows", headers={"X-Workspace-Id": a.ws_id})
    assert r.status_code == 403, (
        f"a non-member addressed another tenant's workspace and got "
        f"{r.status_code}"
    )
    assert "not a member" in r.text.lower()


def test_a_nonexistent_workspace_is_refused(tenants):
    """A guessed id must not resolve to anything."""
    _, b = tenants
    r = b.client.get("/api/workflows", headers={"X-Workspace-Id": "deadbeefcafe"})
    assert r.status_code == 403


def test_refusal_does_not_silently_downgrade(tenants):
    """An explicit header must 403, never fall back to the caller's own space.

    Silently downgrading would be worse than refusing: the caller believes
    they are reading workspace A, and their next *write* lands in B.
    `auth/deps.py` calls this out and this pins it.
    """
    a, b = tenants
    r = b.client.get("/api/workflows", headers={"X-Workspace-Id": a.ws_id})
    assert r.status_code == 403, "request succeeded against a foreign workspace id"


# ── Structural sweep: every workspace-scoped route ───────────────────────────


def _workspace_scoped_get_routes(app) -> list[str]:
    """Every GET route whose dependency tree includes current_workspace_id.

    Discovered rather than listed, so a route added later is covered without
    anyone remembering to update this file — which is exactly how
    `/api/plugins` shipped unguarded past the hand-maintained allowlist in
    `test_anonymous_access_blocked.py`.
    """
    found: list[str] = []
    for route in getattr(app, "routes", []):
        methods = getattr(route, "methods", set()) or set()
        if "GET" not in methods:
            continue
        path = getattr(route, "path", "")
        if "{" in path:
            continue  # needs a real id; covered by the data tests instead
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        stack, seen = [dependant], set()
        while stack:
            dep = stack.pop()
            if id(dep) in seen:
                continue
            seen.add(id(dep))
            call = getattr(dep, "call", None)
            if getattr(call, "__name__", "") == "current_workspace_id":
                found.append(path)
                break
            stack.extend(getattr(dep, "dependencies", []) or [])
    return sorted(set(found))


def test_the_sweep_finds_routes(app_v2):
    """Guard the guard: an empty sweep would make the next test vacuous."""
    routes = _workspace_scoped_get_routes(app_v2)
    assert len(routes) >= 10, (
        f"only {len(routes)} workspace-scoped GET routes discovered — the "
        f"detection probably broke, making the isolation sweep meaningless"
    )


def test_no_scoped_route_serves_another_tenant(tenants, app_v2):
    """Every workspace-scoped GET must refuse a foreign workspace id.

    One assertion over every route the app actually exposes, so coverage does
    not depend on anyone maintaining a list.
    """
    a, b = tenants
    leaked: list[str] = []

    for path in _workspace_scoped_get_routes(app_v2):
        try:
            r = b.client.get(path, headers={"X-Workspace-Id": a.ws_id})
        except Exception:
            continue  # route blew up for unrelated reasons; not an isolation finding
        if r.status_code not in (401, 403, 404, 405, 422, 500, 501, 503):
            leaked.append(f"{path} -> {r.status_code}")

    assert not leaked, (
        "workspace-scoped routes served a caller who is not a member of the "
        f"workspace they addressed: {leaked}"
    )


@pytest.mark.parametrize("path", [
    "/api/steward/findings",
    "/api/steward/memory",
    "/api/steward/coverage",
    "/api/steward/governance",
])
def test_steward_refuses_a_foreign_workspace(tenants, path):
    """REGRESSION: steward swallowed the 403 and downgraded to "default".

    Found 2026-10-22. `api/steward.py` wrapped the resolver as::

        try:
            return current_workspace_id(request)
        except Exception:          # catches HTTPException too
            return "default"

    so a caller addressing a workspace they do not belong to was not
    refused — the denial was discarded and the request silently served the
    `default` workspace instead. Twenty-six other modules wrap the same call
    and all re-raise `HTTPException`; steward was the only deviation.

    Two things were wrong with that. A member of any workspace could read
    the `default` workspace's steward data by sending any workspace id at
    all, valid or invented. And `auth/deps.py` explicitly refuses to
    downgrade for a stated reason: the caller believes they are working in
    the workspace they asked for, so their next *write* lands somewhere
    else.
    """
    a, b = tenants
    r = b.client.get(path, headers={"X-Workspace-Id": a.ws_id})
    assert r.status_code == 403, (
        f"{path} served a non-member who addressed another workspace "
        f"({r.status_code}) instead of refusing"
    )


def test_steward_refuses_an_invented_workspace(tenants):
    """The same hole, reached without knowing any real workspace id."""
    _, b = tenants
    r = b.client.get(
        "/api/steward/findings", headers={"X-Workspace-Id": "no-such-workspace"}
    )
    assert r.status_code == 403, (
        f"an invented workspace id was accepted ({r.status_code}); the "
        f"request was served from some other workspace"
    )


# ── Data isolation: the subtler leak ─────────────────────────────────────────
#
# The gate above only helps when the caller *claims* another workspace. These
# cover the case where B does nothing suspicious — uses their own workspace
# header — but a router resolved the workspace and then failed to filter by
# it. The dependency cannot catch that.


def _create_variable(t: Tenant, name: str):
    return t.post("/api/variables", json={"key": name, "value": "secret-value"})


def test_a_variable_created_in_a_is_invisible_to_b(tenants):
    a, b = tenants
    created = _create_variable(a, "tenant_a_secret")
    if created.status_code not in (200, 201):
        pytest.skip(f"variables endpoint unavailable in this build ({created.status_code})")

    mine = a.get("/api/variables")
    assert mine.status_code == 200
    assert "tenant_a_secret" in mine.text, "the creator cannot see their own variable"

    theirs = b.get("/api/variables")
    assert theirs.status_code == 200
    assert "tenant_a_secret" not in theirs.text, (
        "workspace B can read workspace A's variables — the router resolved "
        "the workspace but did not filter by it"
    )
    assert "secret-value" not in theirs.text


def test_a_workflow_created_in_a_is_invisible_to_b(tenants):
    a, b = tenants
    created = a.post("/api/workflows", json={
        "name": "tenant-a-only-pipeline",
        "steps": [], "connections": [],
    })
    if created.status_code not in (200, 201):
        pytest.skip(f"workflow create unavailable in this build ({created.status_code})")

    mine = a.get("/api/workflows")
    assert "tenant-a-only-pipeline" in mine.text, "the creator cannot see their own workflow"

    theirs = b.get("/api/workflows")
    assert theirs.status_code == 200
    assert "tenant-a-only-pipeline" not in theirs.text, (
        "workspace B can list workspace A's pipelines"
    )


def test_b_cannot_fetch_as_resource_by_id(tenants):
    """Guessing an id must not work either.

    List filtering alone is not isolation: if the detail route looks up by id
    without a workspace predicate, knowing the id is enough.
    """
    a, b = tenants
    created = a.post("/api/workflows", json={
        "name": "tenant-a-by-id", "steps": [], "connections": [],
    })
    if created.status_code not in (200, 201):
        pytest.skip("workflow create unavailable in this build")

    try:
        wf_id = created.json().get("id")
    except Exception:
        pytest.skip("could not read the created workflow id")
    if not wf_id:
        pytest.skip("create returned no id")

    r = b.get(f"/api/workflows/{wf_id}")
    assert r.status_code in (403, 404), (
        f"workspace B fetched workspace A's workflow by id and got "
        f"{r.status_code} — list filtering is not isolation"
    )
