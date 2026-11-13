"""Business glossary: the workspace's terms and metrics, read by the Copilot."""

from __future__ import annotations

import asyncio
import json

from revoluzen_flow.glossary.store import GlossaryTerm, match_terms, render_for_model

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


def _t(name, synonyms=(), **kw):
    return GlossaryTerm(name=name, definition=kw.pop("definition", f"{name} defined"), synonyms=list(synonyms), **kw)


def test_a_request_finds_the_terms_it_uses_by_any_name_and_spelling():
    terms = [
        _t("Net revenue", ["NR", "net sales"], kind="metric", sql="SUM(amount) - SUM(refund_amount)"),
        _t("Active customer", ["active client"]),
        _t("Revenue"),
    ]
    found = match_terms(terms, "Show net_sales by Active-Customer for last month")
    assert [t.name for t in found] == ["Net revenue", "Active customer"]
    # A word inside another word is not a mention; a plural is.
    assert match_terms(terms, "the nrt column") == []
    assert [t.name for t in match_terms(terms, "count active customers")] == ["Active customer"]
    assert [t.name for t in match_terms(terms, "total revenue")] == ["Revenue"]

    block = render_for_model(found)
    assert "Net revenue (metric)" in block and "SUM(amount) - SUM(refund_amount)" in block
    assert "Also called: NR, net sales" in block
    assert render_for_model([]) == ""


def test_the_glossary_is_kept_per_workspace_and_reaches_every_pipeline_draft(authed_client):
    bad = authed_client.post("/api/glossary", json={"name": "Churn", "definition": ""})
    assert bad.status_code == 400 and "definition" in json.dumps(bad.json())

    made = authed_client.post("/api/glossary", json={
        "name": "Active customer", "definition": "A customer with an order in the last 90 days.",
        "synonyms": ["active client", "Active Client"], "sql": "last_order_at >= current_date - INTERVAL 90 DAY",
        "where": "crm.customers.last_order_at",
    })
    assert made.status_code == 201, made.text
    term = made.json()
    assert term["synonyms"] == ["active client"]  # the same name twice is kept once
    clash = authed_client.post("/api/glossary", json={"name": "ACTIVE CUSTOMER", "definition": "x"})
    assert clash.status_code == 400 and "already" in json.dumps(clash.json())

    listed = authed_client.get("/api/glossary", params={"q": "count active clients by region"}).json()["terms"]
    assert [t["name"] for t in listed] == ["Active customer"]

    changed = authed_client.put(f"/api/glossary/{term['id']}", json={"definition": "A customer with an order in the last 60 days."})
    assert changed.status_code == 200 and "60 days" in changed.json()["definition"]

    # Every pipeline draft passes through the planner, which adds the terms a request uses.
    from revoluzen_flow.planner.ai_client import _with_glossary
    ws = term["workspace_id"]
    out = _with_glossary([{"role": "user", "content": "list active customers"}], ws)
    assert "BUSINESS TERMS DEFINED IN THIS WORKSPACE" in out[0]["content"]
    assert "60 days" in out[0]["content"]
    untouched = [{"role": "user", "content": "copy orders.csv to a table"}]
    assert _with_glossary(untouched, ws) == untouched

    # And the Copilot can look a term up by another wording.
    from revoluzen_flow.ai.tools.base import ToolContext
    from revoluzen_flow.ai.tools.lookup_business_terms import DEFINITION
    res = asyncio.run(DEFINITION.handler({"query": "active client"}, ToolContext(
        tenant_id=ws, user_id="u", workspace_id=ws, environment="dev",
    )))
    assert [t["name"] for t in res["terms"]] == ["Active customer"]

    # The lanes that answer without tools carry the terms too: asked "what is an
    # active customer?", the single-shot lane once said there was no glossary.
    from revoluzen_flow.ai.context import PageContext
    from revoluzen_flow.ai.hybrid import _build_hybrid_system_prompt
    from revoluzen_flow.ai.single_shot import _build_system_prompt
    from revoluzen_flow.main import app_state
    page = PageContext(page="insights", user_id="u", tenant_id=ws, workspace_id=ws)
    for build in (_build_system_prompt, _build_hybrid_system_prompt):
        system = build(page, app_state, "What does our glossary say an active customer is?")
        assert "BUSINESS TERMS DEFINED IN THIS WORKSPACE" in system and "60 days" in system, build.__module__
        assert "BUSINESS TERMS" not in build(page, app_state, "copy orders.csv to a table")

    assert authed_client.delete(f"/api/glossary/{term['id']}").status_code == 200
    assert authed_client.get("/api/glossary").json()["terms"] == []
