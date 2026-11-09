"""OpenAPI/Swagger -> connector manifest generator.

Pins that a pasted spec yields a draft manifest compatible with the
rest_framework runtime (id / base_url / auth shape / GET streams), for both
OpenAPI 3 and Swagger 2, with each supported security scheme mapping to the
exact auth block _build_auth_headers expects.
"""

from __future__ import annotations

from revoluzen_flow.connectors.openapi_import import manifest_from_openapi


def _openapi3(security_schemes):
    return {
        "openapi": "3.0.0",
        "info": {"title": "Acme API", "description": "Acme things"},
        "servers": [{"url": "https://api.acme.com/v1"}],
        "components": {"securitySchemes": security_schemes},
        "security": [{next(iter(security_schemes)): []}] if security_schemes else [],
        "paths": {
            "/customers": {"get": {"operationId": "listCustomers", "summary": "List customers"}},
            "/orders": {"get": {"summary": "List orders"}},
            "/orders/{id}": {"post": {"summary": "create"}},  # non-GET → skipped
        },
    }


def _reply_spec(schema, schemas=None):
    return {
        "openapi": "3.0.0",
        "info": {"title": "Acme API"},
        "servers": [{"url": "https://api.acme.com"}],
        "components": {"schemas": schemas or {}},
        "paths": {"/things": {"get": {"responses": {"200": {"description": "ok", "content": {"application/json": {"schema": schema}}}}}}},
    }


def _rows_path_of(spec):
    return manifest_from_openapi(spec)["streams"][0].get("data_path")


_ROWS = {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}}}


def test_a_wrapped_list_gets_its_data_path():
    assert _rows_path_of(_reply_spec({"type": "object", "properties": {"items": _ROWS, "total": {"type": "integer"}}})) == "items"


def test_any_name_counts_when_the_rest_is_paging():
    assert _rows_path_of(_reply_spec({"type": "object", "properties": {"prices": _ROWS, "next_cursor": {"type": "string"}, "has_more": {"type": "boolean"}}})) == "prices"


def test_a_list_defined_by_reference_is_found():
    spec = _reply_spec({"$ref": "#/components/schemas/Page"}, {
        "Page": {"type": "object", "properties": {"items": {"$ref": "#/components/schemas/Things"}, "total": {"type": "integer"}}},
        "Things": _ROWS,
    })
    assert _rows_path_of(spec) == "items"


def test_a_nested_wrapper_gives_a_dotted_path():
    schema = {"type": "object", "properties": {"data": {"type": "object", "properties": {"results": _ROWS, "count": {"type": "integer"}}}, "status": {"type": "string"}}}
    assert _rows_path_of(_reply_spec(schema)) == "data.results"


def test_a_plain_array_needs_no_path():
    assert _rows_path_of(_reply_spec(_ROWS)) is None


def test_one_record_holding_a_list_stays_one_row():
    schema = {"type": "object", "properties": {"id": {"type": "string"}, "lines": _ROWS}}
    assert _rows_path_of(_reply_spec(schema)) is None


def test_two_lists_are_left_for_a_person_to_choose():
    schema = {"type": "object", "properties": {"orders": _ROWS, "refunds": _ROWS}}
    assert _rows_path_of(_reply_spec(schema)) is None


def test_a_swagger2_reply_is_read_too():
    spec = {
        "swagger": "2.0", "info": {"title": "Old"}, "host": "api.acme.com",
        "paths": {"/things": {"get": {"responses": {"200": {"schema": {"type": "object", "properties": {"items": _ROWS, "total": {"type": "integer"}}}}}}}},
    }
    assert _rows_path_of(spec) == "items"


def test_a_spec_that_says_nothing_about_replies_is_fine():
    assert _rows_path_of(_openapi3({})) is None


def test_openapi3_bearer_basic_shape():
    m = manifest_from_openapi(_openapi3({"BearerAuth": {"type": "http", "scheme": "bearer"}}))
    assert m["id"] == "acme_api"
    assert m["name"] == "Acme API"
    assert m["base_url"] == "https://api.acme.com/v1"
    assert m["auth"]["alternatives"][0]["schemes"][0]["type"] == "bearer"
    assert any(p["name"].endswith("_token") and p["secret"] for p in m["params"])
    names = {s["name"] for s in m["streams"]}
    assert names == {"listcustomers", "orders"}  # only GETs, slugged
    assert all(s["method"] == "GET" for s in m["streams"])
    assert m["tier"] == "generated"


def test_openapi3_apikey_header():
    m = manifest_from_openapi(_openapi3({"ApiKey": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}}))
    binding = m["auth"]["alternatives"][0]["schemes"][0]
    assert binding["type"] == "api_key"
    assert (binding["location"], binding["key_name"]) == ("header", "X-Api-Key")


def test_openapi3_apikey_query():
    m = manifest_from_openapi(_openapi3({"ApiKey": {"type": "apiKey", "in": "query", "name": "apikey"}}))
    binding = m["auth"]["alternatives"][0]["schemes"][0]
    assert (binding["location"], binding["key_name"]) == ("query", "apikey")


def test_openapi3_oauth2_token_url():
    m = manifest_from_openapi(_openapi3({
        "OAuth": {"type": "oauth2", "flows": {"clientCredentials": {"tokenUrl": "https://api.acme.com/oauth/token"}}},
    }))
    option = m["auth"]["alternatives"][0]
    assert option["supported"] is False
    assert "saved-connection" in option["schemes"][0]["reason"]


def test_swagger2_host_basepath_and_basic():
    spec = {
        "swagger": "2.0",
        "info": {"title": "Legacy API"},
        "host": "legacy.example.com",
        "basePath": "/api",
        "schemes": ["https"],
        "securityDefinitions": {"basic": {"type": "basic"}},
        "security": [{"basic": []}],
        "paths": {"/things": {"get": {"summary": "things"}}},
    }
    m = manifest_from_openapi(spec)
    assert m["base_url"] == "https://legacy.example.com/api"
    binding = m["auth"]["alternatives"][0]["schemes"][0]
    assert binding["type"] == "basic"
    assert set(binding["fields"]) == {"username", "password"}
    assert [s["path"] for s in m["streams"]] == ["/things"]


def test_no_security_is_anonymous():
    spec = {"openapi": "3.0.0", "info": {"title": "X"}, "servers": [{"url": "https://x.io"}], "paths": {}}
    m = manifest_from_openapi(spec)
    assert m["auth"]["alternatives"] == [{"schemes": [], "supported": True}]
    assert m["streams"] == []


def test_connector_id_override_and_bad_spec():
    m = manifest_from_openapi({"info": {"title": "Y"}, "paths": {}}, connector_id="My Custom ID")
    assert m["id"] == "my_custom_id"
    import pytest
    with pytest.raises(ValueError):
        manifest_from_openapi("not a dict")
