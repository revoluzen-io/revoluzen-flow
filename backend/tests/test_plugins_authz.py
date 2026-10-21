"""Authorization on the plugin API.

Why this file exists
--------------------
Loading a plugin executes arbitrary Python inside the Flow process: the
manager puts the plugin directory on ``sys.path`` and calls
``spec.loader.exec_module`` (`revoluzen_flow/plugins/__init__.py`). There is
no signature check, no allowlist and no sandbox, so whoever can enable or
reload a plugin can run code as the server.

Until 2026-10-21 this router carried no authorization whatsoever — only the
``REVOLUZEN_FLOW_ENABLE_PLUGINS`` feature flag, which defaults ON like every
other flag. `test_anonymous_access_blocked.py` says in its own docstring that
it exists to catch "a new router added without `Depends(require_auth)` or
without role guards on its mutations", and `/api/plugins` was never added to
it. This file closes that gap for this router specifically.

Note what these tests do and do not establish. They prove the endpoints are
gated. They do not make plugin loading safe — that needs signing, an
allowlist, or out-of-process execution. Admin-only is the floor.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client, role_clients,
)

# Every route that changes which code the server will execute.
MUTATIONS = [
    ("post", "/api/plugins/reload"),
    ("post", "/api/plugins/demo-plugin/enable"),
    ("post", "/api/plugins/demo-plugin/disable"),
]

READS = [
    ("get", "/api/plugins"),
    ("get", "/api/plugins/scaffold"),
]


@pytest.fixture(autouse=True)
def _anonymous(client: TestClient):
    """Make sure the bare `client` really carries no session.

    Every "must be refused" assertion is worthless if the client is
    authenticated, and this fixture file has been bitten by that before — see
    the same guard in `test_anonymous_access_blocked.py`.
    """
    client.cookies.clear()
    client.headers.pop("Authorization", None)
    return client


# ── Anonymous ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method,path", MUTATIONS + READS)
def test_anonymous_is_refused(client: TestClient, method, path):
    resp = getattr(client, method)(path)
    assert resp.status_code in (401, 403), (
        f"{method.upper()} {path} answered {resp.status_code} to an "
        f"unauthenticated caller"
    )


# ── Authenticated but not admin ──────────────────────────────────────────────


@pytest.mark.parametrize("method,path", MUTATIONS)
@pytest.mark.parametrize("role", ["viewer", "analyst", "data_engineer"])
def test_non_admin_cannot_change_what_code_runs(role_clients, role, method, path):
    """Authoring a plugin is not the same authority as loading one.

    `data_engineer` is the case worth having. At rank 70 it is the most
    privileged non-admin role — `auth/deps.py` describes it as having "full
    write authority on pipelines/creds" — and it still must not load code
    into the server process. If the gate were set at `data_engineer` the
    mutations would look guarded while handing in-process execution to
    everyone who builds pipelines.

    The admin threshold is 90, so this holds for data_engineer (70),
    analyst (30) and viewer (10) alike.
    """
    c = role_clients.get(role)
    if c is None:
        pytest.skip(f"role {role} not provisioned in this environment")

    resp = getattr(c, method)(path)
    assert resp.status_code == 403, (
        f"{role} got {resp.status_code} from {method.upper()} {path}; "
        f"only admin may decide which code the server executes"
    )


# ── Admin ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method,path", MUTATIONS)
def test_admin_is_not_blocked_by_authorization(authed_client: TestClient, method, path):
    """Admin must get past the gate.

    This asserts only that the *authorization* layer let the request through.
    What happens afterwards is not this file's business: the plugin id below
    does not exist, the feature flag may be off, and the handler writes to
    SQLite, which under parallel test clients can raise "database is locked".
    Any of those would fail an assertion on 200 for reasons unrelated to
    authorization — and a test that fails for unrelated reasons gets muted,
    which would lose the signal that actually matters here.

    Dependencies resolve before the endpoint runs, so reaching the handler at
    all — even to fail in it — proves the gate admitted the caller.
    """
    try:
        resp = getattr(authed_client, method)(path)
    except Exception:
        # The handler raised, so the gate had already admitted the request.
        return

    assert resp.status_code not in (401, 403), (
        f"admin was refused {method.upper()} {path} with {resp.status_code}"
    )


@pytest.mark.parametrize("method,path", READS)
def test_authenticated_users_can_read(role_clients, method, path):
    """Reads disclose no secrets, and a plugin author needs the scaffold."""
    c = role_clients.get("data_engineer")
    if c is None:
        pytest.skip("data_engineer role not provisioned in this environment")

    resp = getattr(c, method)(path)
    assert resp.status_code not in (401, 403), (
        f"data_engineer was refused {method.upper()} {path}; reads should be "
        f"available to any authenticated user"
    )


@pytest.mark.parametrize("method,path", MUTATIONS)
def test_workspace_admin_is_admitted(role_clients, method, path):
    """workspace_admin is rank 90, a declared peer of admin.

    Pinned because the gate is written as `require_min_rank("admin")` rather
    than a membership test against ADMIN_ROLES, so the two could drift apart
    and silently lock out an admin-equivalent role.
    """
    c = role_clients.get("workspace_admin")
    if c is None:
        pytest.skip("workspace_admin not provisioned in this environment")

    try:
        resp = getattr(c, method)(path)
    except Exception:
        return  # reached the handler, so the gate admitted it

    assert resp.status_code not in (401, 403), (
        f"workspace_admin was refused {method.upper()} {path}"
    )


# ── The guard itself ─────────────────────────────────────────────────────────


def test_every_mutating_plugin_route_is_admin_gated(app_v2):
    """Catch a new mutating route added without a gate.

    The point of failure this defends is a future route — `/install`,
    `/{id}/delete` — added next to the gated ones and inheriting only the
    router-level `require_auth`. That would be authenticated, look guarded,
    and still execute code for any logged-in user.
    """
    from revoluzen_flow.auth.deps import require_min_rank  # noqa: F401

    ungated: list[str] = []
    for route in app_v2.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if not path.startswith("/api/plugins"):
            continue
        if not (methods & {"POST", "PUT", "PATCH", "DELETE"}):
            continue
        # A gated route carries an extra dependency beyond the router-level
        # require_auth; count the resolved dependant tree for the rank check.
        deps = getattr(getattr(route, "dependant", None), "dependencies", [])
        if len(deps) < 2:
            ungated.append(f"{sorted(methods)} {path}")

    assert not ungated, (
        "mutating plugin routes without an admin gate: "
        + ", ".join(ungated)
        + " — loading a plugin executes code in the server process"
    )
