"""dbt project importer — compiled manifest.json → Revoluzen Flow pipeline.

Covers the pure converter (``revoluzen_flow.importers.dbt.manifest_to_pipeline``) and
the ``POST /api/workflows/import-dbt`` endpoint. The converter tests need no
app; the endpoint test uses the authed client.
"""
from __future__ import annotations

from revoluzen_flow.importers.dbt import manifest_to_pipeline
from revoluzen_flow.ir.schema import Workflow, Step, StepConnection, NodePosition

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


# A small but realistic compiled manifest: a source → staging model → mart
# model chain, plus a dbt test node (which must be ignored) and an
# incremental model (which must warn).
MANIFEST = {
    "metadata": {
        "project_name": "jaffle_shop",
        "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v11.json",
    },
    "nodes": {
        "model.jaffle_shop.stg_orders": {
            "resource_type": "model",
            "name": "stg_orders",
            "schema": "analytics",
            "compiled_code": "SELECT id, amount FROM raw.orders",
            "config": {"materialized": "view"},
            "depends_on": {"nodes": ["source.jaffle_shop.raw.orders"]},
        },
        "model.jaffle_shop.orders": {
            "resource_type": "model",
            "name": "orders",
            "schema": "analytics",
            "compiled_code": "SELECT * FROM stg_orders WHERE amount > 0",
            "config": {"materialized": "table"},
            "depends_on": {"nodes": ["model.jaffle_shop.stg_orders"]},
        },
        "model.jaffle_shop.orders_daily": {
            "resource_type": "model",
            "name": "orders_daily",
            "schema": "analytics",
            "compiled_code": "SELECT date_trunc('day', ts) d, count(*) FROM orders GROUP BY 1",
            "config": {"materialized": "incremental"},
            "depends_on": {"nodes": ["model.jaffle_shop.orders"]},
        },
        # Non-model resource types must be ignored entirely.
        "test.jaffle_shop.not_null_orders_id": {
            "resource_type": "test",
            "name": "not_null_orders_id",
        },
    },
    "sources": {
        "source.jaffle_shop.raw.orders": {
            "resource_type": "source",
            "name": "orders",
            "source_name": "raw",
        },
        # An UNREFERENCED source must not become a dangling node.
        "source.jaffle_shop.raw.customers": {
            "resource_type": "source",
            "name": "customers",
            "source_name": "raw",
        },
    },
}


class TestConverter:
    def test_shapes_and_counts(self):
        pipeline, report = manifest_to_pipeline(MANIFEST)
        assert pipeline["name"] == "jaffle_shop (dbt import)"
        # 3 models + 1 referenced source; the dbt test node and the
        # unreferenced 'customers' source are dropped.
        assert report["models"] == 3
        assert report["sources"] == 1
        types = sorted(s["type"] for s in pipeline["steps"])
        # 3 transforms (one per model) + 1 source placeholder, plus a
        # Destination for each model that MATERIALIZES a relation: `orders`
        # (table) and `orders_daily` (incremental). `stg_orders` is a view,
        # so it persists nothing and gets no sink.
        assert types == [
            "destination", "destination",
            "source",
            "transform", "transform", "transform",
        ]

    def test_source_is_placeholder(self):
        pipeline, _ = manifest_to_pipeline(MANIFEST)
        src = next(s for s in pipeline["steps"] if s["type"] == "source")
        assert src["params"]["_needs_connection"] is True
        assert src["params"]["table"] == "orders"
        assert src["label"] == "raw.orders"

    def test_edges_carry_ref_aliases(self):
        pipeline, _ = manifest_to_pipeline(MANIFEST)
        id_by_label = {s["label"]: s["id"] for s in pipeline["steps"]}
        edges = {
            (c["from_step"], c["to_step"]): c.get("alias")
            for c in pipeline["connections"]
        }
        # stg_orders → orders, aliased 'stg_orders' so `FROM stg_orders` resolves.
        assert edges[(id_by_label["stg_orders"], id_by_label["orders"])] == "stg_orders"
        # source → stg_orders, aliased with the source table name.
        assert edges[(id_by_label["raw.orders"], id_by_label["stg_orders"])] == "orders"

    def test_incremental_and_dialect_warnings(self):
        _, report = manifest_to_pipeline(MANIFEST)
        assert report["incremental_models"] == ["orders_daily"]
        joined = " ".join(report["warnings"]).lower()
        assert "incremental" in joined
        assert "duckdb" in joined
        assert "connection" in joined  # unbound-source warning

    def test_layout_is_left_to_right_by_depth(self):
        pipeline, _ = manifest_to_pipeline(MANIFEST)
        x = {s["label"]: s["position"]["x"] for s in pipeline["steps"]}
        # raw.orders(0) < stg_orders(1) < orders(2) < orders_daily(3)
        assert x["raw.orders"] < x["stg_orders"] < x["orders"] < x["orders_daily"]

    def test_output_is_importable_ir(self):
        # The dict must round-trip through the same IR the import endpoint builds.
        pipeline, _ = manifest_to_pipeline(MANIFEST)
        wf = Workflow(name="rt", project_id="default", workspace_id="default")
        for s in pipeline["steps"]:
            wf.steps.append(Step(
                id=s["id"], type=s["type"], label=s.get("label", ""),
                params=s.get("params", {}),
                position=NodePosition(**s.get("position", {})),
            ))
        for c in pipeline["connections"]:
            wf.connections.append(StepConnection(**c))
        # 4 graph nodes + 2 materialization sinks; 3 dependency edges + 2
        # transform→sink edges.
        assert len(wf.steps) == 6
        assert len(wf.connections) == 5

    def test_rejects_non_dict(self):
        import pytest
        with pytest.raises(ValueError):
            manifest_to_pipeline("not a manifest")  # type: ignore[arg-type]

    def test_empty_manifest_yields_no_steps(self):
        pipeline, report = manifest_to_pipeline({"nodes": {}, "sources": {}})
        assert pipeline["steps"] == []
        assert report["models"] == 0


class TestMaterializationBecomesAWrite:
    """A dbt model is not just a SELECT.

    `table` and `incremental` models write a relation into the warehouse —
    that is the point of dbt. Importing them as a bare Transform produced a
    pipeline that recomputed the whole DAG and discarded every result: it ran
    green and left the warehouse untouched. These tests pin the write back on.
    """

    def _sinks(self, pipeline):
        return {
            s["label"]: s for s in pipeline["steps"] if s["type"] == "destination"
        }

    def test_table_model_writes_with_create_mode(self):
        pipeline, _ = manifest_to_pipeline(MANIFEST)
        sink = self._sinks(pipeline)["write orders"]
        # dbt's `table` drops and rebuilds; db_sink's `create` is
        # CREATE OR REPLACE TABLE — the same semantics.
        assert sink["params"]["mode"] == "create"
        assert sink["params"]["table"] == "orders"
        assert sink["params"]["schema"] == "analytics"

    def test_incremental_model_writes_with_append_mode(self):
        pipeline, _ = manifest_to_pipeline(MANIFEST)
        sink = self._sinks(pipeline)["write orders_daily"]
        assert sink["params"]["mode"] == "append"

    def test_view_model_gets_no_sink(self):
        # A view is re-evaluated on read; there is nothing to persist, so
        # inventing a write here would be worse than omitting one.
        pipeline, _ = manifest_to_pipeline(MANIFEST)
        assert "write stg_orders" not in self._sinks(pipeline)

    def test_sink_is_wired_to_its_model(self):
        pipeline, _ = manifest_to_pipeline(MANIFEST)
        ids = {s["label"]: s["id"] for s in pipeline["steps"]}
        edge = next(
            c for c in pipeline["connections"]
            if c["to_step"] == ids["write orders"]
        )
        assert edge["from_step"] == ids["orders"]

    def test_sink_demands_a_connection_rather_than_defaulting(self):
        # No connector_type on purpose. Pre-setting "database" would look
        # complete while connection_id stayed empty — and an empty connection
        # on db_sink means "DuckDB in-memory", so every run would write
        # somewhere that silently disappears.
        pipeline, _ = manifest_to_pipeline(MANIFEST)
        sink = self._sinks(pipeline)["write orders"]
        assert sink["params"]["_needs_connection"] is True
        assert "connector_type" not in sink["params"]

    def test_report_lists_materialized_models(self):
        _, report = manifest_to_pipeline(MANIFEST)
        assert sorted(report["materialized_models"]) == ["orders", "orders_daily"]

    def test_unique_key_incremental_becomes_a_merge(self):
        # dbt upserts an incremental model that declares a unique_key, so it
        # maps to the sink's merge mode on the same key. Importing it as a
        # plain append would duplicate every row on the second run.
        manifest = {
            "metadata": {"project_name": "shop"},
            "nodes": {
                "model.shop.users": {
                    "resource_type": "model",
                    "name": "users",
                    "schema": "analytics",
                    "compiled_code": "SELECT * FROM raw_users",
                    "config": {"materialized": "incremental", "unique_key": "user_id"},
                    "depends_on": {"nodes": []},
                },
            },
            "sources": {},
        }
        pipeline, report = manifest_to_pipeline(manifest)
        sink = self._sinks(pipeline)["write users"]
        assert sink["params"]["mode"] == "merge"
        assert sink["params"]["merge_on"] == ["user_id"]
        assert sink["params"]["_dbt_unique_key"] == "user_id"
        assert report["merge_key_models"] == ["users"]
        joined = " ".join(report["warnings"]).lower()
        assert "merge" in joined

    def test_composite_unique_key_becomes_multi_column_merge(self):
        # dbt allows unique_key to be a list.
        manifest = {
            "metadata": {"project_name": "shop"},
            "nodes": {
                "model.shop.daily": {
                    "resource_type": "model",
                    "name": "daily",
                    "compiled_code": "SELECT * FROM raw",
                    "config": {
                        "materialized": "incremental",
                        "unique_key": ["tenant_id", "day"],
                    },
                    "depends_on": {"nodes": []},
                },
            },
            "sources": {},
        }
        pipeline, _ = manifest_to_pipeline(manifest)
        sink = self._sinks(pipeline)["write daily"]
        assert sink["params"]["mode"] == "merge"
        assert sink["params"]["merge_on"] == ["tenant_id", "day"]

    def test_incremental_without_unique_key_stays_append(self):
        # dbt only upserts when a unique_key is declared; a plain incremental
        # is insert-only, so append is the faithful mapping.
        pipeline, _ = manifest_to_pipeline(MANIFEST)
        sink = self._sinks(pipeline)["write orders_daily"]
        assert sink["params"]["mode"] == "append"
        assert "merge_on" not in sink["params"]

    def test_unknown_materialization_gets_no_sink(self):
        # A custom materialization from a dbt package. We cannot know what it
        # writes, so import it as a plain Transform rather than invent a write.
        manifest = {
            "metadata": {"project_name": "shop"},
            "nodes": {
                "model.shop.weird": {
                    "resource_type": "model",
                    "name": "weird",
                    "compiled_code": "SELECT 1",
                    "config": {"materialized": "materialized_view"},
                    "depends_on": {"nodes": []},
                },
            },
            "sources": {},
        }
        pipeline, report = manifest_to_pipeline(manifest)
        assert [s["type"] for s in pipeline["steps"]] == ["transform"]
        assert report["materialized_models"] == []


class TestUpstreamsThatAreNotSources:
    """A model can read a SEED or a SNAPSHOT, not just a source or a model.

    Both are real relations in the warehouse — dbt materialises seeds from
    CSV and snapshots as SCD tables — but neither is a `model`, so an
    importer that only looks at models and sources drops the edge. That was
    the behaviour: the model's compiled SQL said `FROM country_codes` while
    the canvas showed no upstream and the report said nothing, so the import
    looked clean and failed at run time with "table not found". An honest
    report is the entire value of this importer; a silently incomplete DAG
    is the one outcome it must not produce.
    """

    SEEDED = {
        "metadata": {"project_name": "seeded"},
        "nodes": {
            "seed.seeded.country_codes": {"resource_type": "seed", "name": "country_codes"},
            "snapshot.seeded.cust_hist": {"resource_type": "snapshot", "name": "cust_hist"},
            "model.seeded.enriched": {
                "resource_type": "model", "name": "enriched",
                "compiled_code": "SELECT * FROM country_codes JOIN cust_hist USING (id)",
                "config": {"materialized": "table"},
                "depends_on": {"nodes": [
                    "seed.seeded.country_codes", "snapshot.seeded.cust_hist",
                ]},
            },
        },
        "sources": {},
    }

    def test_seed_and_snapshot_become_upstream_nodes(self):
        pipeline, report = manifest_to_pipeline(self.SEEDED)
        # `enriched` is materialized:table, so it also gets a Destination —
        # not what this test is about, so scope to the graph nodes.
        kinds = sorted(
            s["params"].get("_dbt_kind") for s in pipeline["steps"]
            if s["type"] != "destination"
        )
        assert kinds == ["model", "seed", "snapshot"]
        assert report["seeds"] == 1
        assert report["snapshots"] == 1

    def test_their_edges_are_aliased_so_the_sql_resolves(self):
        pipeline, _ = manifest_to_pipeline(self.SEEDED)
        sink_ids = {
            s["id"] for s in pipeline["steps"] if s["type"] == "destination"
        }
        aliases = sorted(
            c["alias"] for c in pipeline["connections"]
            if c["to_step"] not in sink_ids
        )
        # The compiled SQL says FROM country_codes / JOIN cust_hist; those are
        # exactly the names the edges must register.
        assert aliases == ["country_codes", "cust_hist"]

    def test_they_need_binding_like_any_source(self):
        pipeline, report = manifest_to_pipeline(self.SEEDED)
        placeholders = [
            s for s in pipeline["steps"]
            if s["params"].get("_dbt_kind") in ("seed", "snapshot")
        ]
        assert len(placeholders) == 2
        assert all(s["params"]["_needs_connection"] for s in placeholders)
        joined = " ".join(report["warnings"]).lower()
        assert "seed" in joined and "snapshot" in joined

    def test_a_dependency_that_cannot_be_mapped_is_reported(self):
        """Belt and braces: anything still unmappable must be named, not dropped."""
        manifest = {
            "metadata": {"project_name": "odd"},
            "nodes": {
                "model.odd.m": {
                    "resource_type": "model", "name": "m",
                    "compiled_code": "SELECT * FROM mystery",
                    "depends_on": {"nodes": ["metric.odd.mystery"]},
                },
            },
            "sources": {},
        }
        pipeline, report = manifest_to_pipeline(manifest)
        assert pipeline["connections"] == []
        assert report["unresolved_dependencies"] == ["metric.odd.mystery"]
        assert any("could not be mapped" in w for w in report["warnings"])


class TestCompiledSqlIsRepointed:
    """`dbt compile` emits FULLY-QUALIFIED relation names.

    `{{ ref('stg_orders') }}` becomes `"jaffle_shop"."analytics"."stg_orders"`,
    not `stg_orders`. Revoluzen Flow registers each upstream under its edge
    alias — the bare name — so without a rewrite the imported SQL cannot
    resolve a single input and every model fails at run time.

    This went unnoticed because the original fixture was hand-written with
    bare table names, which no real `dbt compile` produces. These tests use
    the qualified form on purpose.
    """

    QUALIFIED = {
        "metadata": {"project_name": "jaffle_shop"},
        "nodes": {
            "model.jaffle_shop.stg_orders": {
                "resource_type": "model", "name": "stg_orders",
                "schema": "analytics", "database": "jaffle_shop",
                "relation_name": '"jaffle_shop"."analytics"."stg_orders"',
                "compiled_code": 'SELECT id, amount FROM "jaffle_shop"."raw"."orders"',
                "depends_on": {"nodes": ["source.jaffle_shop.raw.orders"]},
            },
            "model.jaffle_shop.orders": {
                "resource_type": "model", "name": "orders",
                "schema": "analytics", "database": "jaffle_shop",
                "relation_name": '"jaffle_shop"."analytics"."orders"',
                "compiled_code": (
                    'SELECT * FROM "jaffle_shop"."analytics"."stg_orders" WHERE amount > 0'
                ),
                "depends_on": {"nodes": ["model.jaffle_shop.stg_orders"]},
            },
        },
        "sources": {
            "source.jaffle_shop.raw.orders": {
                "resource_type": "source", "name": "orders", "source_name": "raw",
                "database": "jaffle_shop", "schema": "raw",
                "relation_name": '"jaffle_shop"."raw"."orders"',
            },
        },
    }

    def _sql_by_label(self, manifest):
        pipeline, report = manifest_to_pipeline(manifest)
        return (
            {s["label"]: s["params"].get("expression", "") for s in pipeline["steps"]},
            report,
        )

    def test_qualified_names_become_the_edge_alias(self):
        sql, report = self._sql_by_label(self.QUALIFIED)
        assert sql["stg_orders"] == "SELECT id, amount FROM orders"
        assert sql["orders"] == "SELECT * FROM stg_orders WHERE amount > 0"
        assert report["relation_references_rewritten"] == 2

    def test_no_qualified_relation_survives(self):
        sql, _ = self._sql_by_label(self.QUALIFIED)
        for label, expr in sql.items():
            assert '"jaffle_shop"' not in expr, f"{label} kept a qualified name"

    def test_backtick_quoting_is_handled(self):
        """BigQuery renders relations with backticks."""
        manifest = {
            "nodes": {
                "model.p.a": {
                    "resource_type": "model", "name": "a",
                    "relation_name": "`proj`.`ds`.`a`",
                    "compiled_code": "SELECT 1",
                },
                "model.p.b": {
                    "resource_type": "model", "name": "b",
                    "relation_name": "`proj`.`ds`.`b`",
                    "compiled_code": "SELECT * FROM `proj`.`ds`.`a`",
                    "depends_on": {"nodes": ["model.p.a"]},
                },
            },
            "sources": {},
        }
        sql, _ = self._sql_by_label(manifest)
        assert sql["b"] == "SELECT * FROM a"

    def test_manifest_without_relation_name_falls_back_to_parts(self):
        """Older manifests omit relation_name; database/schema/name still
        reconstruct the reference."""
        manifest = {
            "nodes": {
                "model.p.a": {
                    "resource_type": "model", "name": "a",
                    "database": "db", "schema": "sc", "compiled_code": "SELECT 1",
                },
                "model.p.b": {
                    "resource_type": "model", "name": "b",
                    "database": "db", "schema": "sc",
                    "compiled_code": "SELECT * FROM db.sc.a",
                    "depends_on": {"nodes": ["model.p.a"]},
                },
            },
            "sources": {},
        }
        sql, _ = self._sql_by_label(manifest)
        assert sql["b"] == "SELECT * FROM a"

    def test_rewrite_does_not_match_inside_a_longer_identifier(self):
        """`orders` must not be rewritten inside `raw_orders_archive`."""
        manifest = {
            "nodes": {
                "model.p.m": {
                    "resource_type": "model", "name": "m",
                    "compiled_code": 'SELECT * FROM "d"."s"."orders" JOIN raw_orders_archive USING (id)',
                    "depends_on": {"nodes": ["source.p.s.orders"]},
                },
            },
            "sources": {
                "source.p.s.orders": {
                    "resource_type": "source", "name": "orders", "source_name": "s",
                    "relation_name": '"d"."s"."orders"',
                },
            },
        }
        sql, _ = self._sql_by_label(manifest)
        assert sql["m"] == "SELECT * FROM orders JOIN raw_orders_archive USING (id)"

    def test_bare_names_are_left_alone(self):
        """The original hand-written style must keep working unchanged."""
        _, report = manifest_to_pipeline(MANIFEST)
        assert report["relation_references_rewritten"] == 0


class TestManifestSchemaVariants:
    def test_old_manifest_keys_still_read(self):
        """dbt renamed compiled_sql -> compiled_code and raw_sql -> raw_code.
        The importer reads both; nothing pinned the old spelling."""
        manifest = {
            "metadata": {"project_name": "legacy"},
            "nodes": {
                "model.legacy.a": {
                    "resource_type": "model", "name": "a",
                    "compiled_sql": "SELECT 1 AS x",
                    "config": {"materialized": "view"},
                },
            },
            "sources": {},
        }
        pipeline, _ = manifest_to_pipeline(manifest)
        assert pipeline["steps"][0]["params"]["expression"] == "SELECT 1 AS x"

    def test_raw_only_model_warns_to_compile_first(self):
        manifest = {
            "nodes": {
                "model.p.b": {
                    "resource_type": "model", "name": "b",
                    "raw_code": "SELECT * FROM {{ ref('a') }}",
                },
            },
            "sources": {},
        }
        pipeline, report = manifest_to_pipeline(manifest)
        assert "{{" in pipeline["steps"][0]["params"]["expression"]
        assert any("dbt compile" in w for w in report["warnings"])

    def test_ephemeral_models_are_flagged_as_redundant(self):
        manifest = {
            "nodes": {
                "model.e.helper": {
                    "resource_type": "model", "name": "helper",
                    "compiled_code": "SELECT 1",
                    "config": {"materialized": "ephemeral"},
                },
            },
            "sources": {},
        }
        _, report = manifest_to_pipeline(manifest)
        assert report["ephemeral_models"] == ["helper"]
        assert any("ephemeral" in w for w in report["warnings"])

    def test_a_cyclic_manifest_does_not_hang_or_recurse(self):
        """A real dbt DAG is acyclic, but a hand-edited manifest need not be
        and _layout recurses over predecessors."""
        manifest = {
            "nodes": {
                "model.c.a": {"resource_type": "model", "name": "a", "compiled_code": "SELECT 1",
                              "depends_on": {"nodes": ["model.c.b"]}},
                "model.c.b": {"resource_type": "model", "name": "b", "compiled_code": "SELECT 2",
                              "depends_on": {"nodes": ["model.c.a"]}},
            },
            "sources": {},
        }
        pipeline, _ = manifest_to_pipeline(manifest)
        assert len(pipeline["steps"]) == 2
        assert all("x" in s["position"] for s in pipeline["steps"])


class TestEndpoint:
    """Exercise the import-dbt handler's persist path deterministically.

    We call the async handler directly with ``workspace_id`` supplied rather
    than going over HTTP: auth is enforced by middleware above the route (so a
    dependency override can't bypass it) and the login-seed fixture skips on
    some workers — both would leave this new route unexercised. The ``client``
    fixture boots the app so ``app_state['db']`` (hence ``get_store()``) is
    wired; the handler then converts + saves exactly as a real request would.
    """

    async def test_import_creates_pipeline(self, client):
        from revoluzen_flow.api.workflows import import_dbt_project, DbtImportRequest
        result = await import_dbt_project(
            DbtImportRequest(manifest=MANIFEST), workspace_id="default",
        )
        # 3 models + 1 source + 2 materialization sinks (orders, orders_daily).
        assert result["steps_imported"] == 6
        assert result["connections_imported"] == 5
        assert result["report"]["models"] == 3
        assert result["report"]["incremental_models"] == ["orders_daily"]

    async def test_import_returns_its_validation_findings(self, client):
        """Import used to report nothing, so a freshly imported project looked
        clean until the operator opened the editor and the client re-ran the
        same checks. Every dbt import has findings by construction: each
        placeholder source and each materialization sink needs a connection."""
        from revoluzen_flow.api.workflows import import_dbt_project, DbtImportRequest
        result = await import_dbt_project(
            DbtImportRequest(manifest=MANIFEST), workspace_id="default",
        )
        assert "errors" in result and "warnings" in result
        blamed = {e["step_id"] for e in result["errors"]}
        # Both materialization sinks must be named, not just the source. If an
        # unbound Destination produced no finding it would fall through to
        # db_sink's empty-connection behaviour — a write to in-memory DuckDB
        # that disappears — which is precisely what this import must not do.
        assert "model_jaffle_shop_orders__write" in blamed
        assert "model_jaffle_shop_orders_daily__write" in blamed
        assert "source_jaffle_shop_raw_orders" in blamed

    async def test_empty_manifest_400(self, client):
        from revoluzen_flow.api.workflows import import_dbt_project, DbtImportRequest
        from fastapi import HTTPException
        import pytest
        with pytest.raises(HTTPException) as exc:
            await import_dbt_project(
                DbtImportRequest(manifest={"nodes": {}}), workspace_id="default",
            )
        assert exc.value.status_code == 400
        assert "dbt" in str(exc.value.detail).lower()
