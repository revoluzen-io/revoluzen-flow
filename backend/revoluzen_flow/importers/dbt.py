"""dbt project importer — turn a compiled dbt ``manifest.json`` into an
Revoluzen Flow pipeline, to onboard dbt shops without a rewrite.

MVP contract (deliberate): we read the **compiled** manifest — the artifact
dbt writes to ``target/manifest.json`` after ``dbt compile`` / ``dbt docs
generate`` — and never the raw ``models/*.sql``. dbt has already resolved
``{{ ref }}`` / ``{{ source }}`` / macros / ``is_incremental()`` into
``compiled_code`` plus an explicit ``depends_on`` DAG, so we only read
*structure* + *finished SQL*. That keeps the importer to the standard library
(``json`` is done by the caller) with **no new dependency** and sidesteps the
entire Jinja/adapter-dialect problem, which is the hard part of dbt.

Mapping to the Revoluzen Flow IR (see ``revoluzen_flow.ir.schema``):

    dbt model node        → SQL Transform step (``params.expression`` = SQL)
    dbt source            → Source placeholder step (bind a connection later)
    ``ref()`` edge        → StepConnection(alias = upstream model name)
    ``source()`` edge     → StepConnection(alias = source table name)
    ``materialized:``     → Destination step after the Transform, for the
    ``table``/``incremental``  materializations that actually write a relation
                          (see ``_MATERIALIZED_TO_WRITE_MODE``). ``view`` and
                          ``ephemeral`` persist nothing and get no sink.

The ``alias`` on each edge is what makes the SQL runnable: Revoluzen Flow's SQL
Transform registers every incoming relation under its edge alias, so a model
whose compiled SQL says ``FROM stg_orders`` finds ``stg_orders`` because the
edge from that upstream model carries ``alias="stg_orders"``.

``manifest_to_pipeline`` returns the same **portable pipeline dict** that
``POST /api/workflows/import`` already accepts, plus an honest ``report`` of
caveats (unbound sources, incremental models, missing/raw SQL, DuckDB dialect
risk) so the import surface can warn rather than silently mislead.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

# dbt resource types that are REAL RELATIONS in the warehouse and so can be
# read by a model: seeds are materialised as tables from CSV, snapshots as
# slowly-changing-dimension tables. They are not models, so they get the same
# placeholder treatment as a source — but they must still be imported, because
# a model that reads one and finds no upstream node produces SQL referencing a
# relation that does not exist. (Tests, analyses, exposures and metrics are
# not relations and stay ignored.)
_RELATION_RESOURCE_TYPES = {"seed", "snapshot"}

# How to label each placeholder kind in the UI and the warning text.
_PLACEHOLDER_NOUN = {"source": "source", "seed": "seed", "snapshot": "snapshot"}

# dbt materializations that WRITE A RELATION, mapped to the write mode of the
# Revoluzen Flow sink contract (db_sink: create | append | truncate).
#
#   table       → dbt drops and rebuilds the table each run  → create
#                 ("CREATE OR REPLACE TABLE", the same semantics).
#   incremental → dbt inserts only new rows into the existing table → append,
#                 or, when the model declares a `unique_key`, dbt upserts →
#                 merge on those key columns (see `merge_on` below).
#
# Deliberately absent: `view` (re-evaluated on read, nothing to persist) and
# `ephemeral` (inlined as a CTE in its consumers, never a relation of its own).
# Anything unrecognised is treated as non-materializing, so an unknown custom
# materialization imports as a plain Transform instead of inventing a write.
_MATERIALIZED_TO_WRITE_MODE = {
    "table": "create",
    "incremental": "append",
}


def _safe_id(unique_id: str) -> str:
    """dbt ``unique_id`` (``model.project.name``) → a stable, valid step id.

    Deterministic so the same manifest always yields the same ids (re-import
    is diffable) and so edges resolve without a side lookup table.
    """
    s = re.sub(r"[^A-Za-z0-9]+", "_", unique_id or "").strip("_")
    return s or "node"


def _relation_pattern(relation_name: str) -> re.Pattern[str] | None:
    """A regex matching a dbt ``relation_name`` however it is quoted.

    ``relation_name`` in the manifest is the adapter's own rendering —
    ``"db"."schema"."tbl"`` on Postgres/Snowflake, `` `db`.`schema`.`tbl` ``
    on BigQuery, sometimes unquoted. The compiled SQL uses the same
    rendering, but matching on the literal string alone is brittle, so this
    matches the identifier PARTS with any of those quoting styles and
    optional whitespace around the dots.
    """
    parts = [p for p in re.split(r'[.]', relation_name or "") if p.strip()]
    cleaned = [p.strip().strip('"').strip('`').strip("[]").strip() for p in parts]
    cleaned = [p for p in cleaned if p]
    if not cleaned:
        return None
    q = r'["`\[]?'          # optional opening quote
    e = r'["`\]]?'          # optional closing quote
    body = r'\s*\.\s*'.join(f'{q}{re.escape(p)}{e}' for p in cleaned)
    # Require a non-identifier boundary so `orders` doesn't match inside
    # `raw_orders`.
    return re.compile(rf'(?<![A-Za-z0-9_."`\]]){body}(?![A-Za-z0-9_])', re.IGNORECASE)


def _rewrite_relations(sql: str, rewrites: list[tuple[str, str]]) -> tuple[str, int]:
    """Point compiled SQL at the names Revoluzen Flow will register.

    `dbt compile` resolves ``{{ ref('stg_orders') }}`` to the relation's
    FULLY-QUALIFIED name — ``"jaffle_shop"."analytics"."stg_orders"`` — not
    to ``stg_orders``. Revoluzen Flow registers each upstream under its edge
    alias, which is the bare model/source name, so an un-rewritten import
    produces SQL that cannot resolve a single one of its inputs. It only
    appears to work on a hand-written manifest with bare table names, which
    is exactly what the first fixture for this importer contained.

    Longest relation first, so a two-part name can't eat part of a
    three-part one.
    """
    count = 0
    for relation_name, alias in sorted(rewrites, key=lambda r: -len(r[0])):
        pat = _relation_pattern(relation_name)
        if pat is None:
            continue
        sql, n = pat.subn(alias, sql)
        count += n
    return sql, count


def manifest_to_pipeline(
    manifest: dict[str, Any],
    *,
    name: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Convert a compiled dbt ``manifest.json`` (parsed) into
    ``(pipeline_dict, report)``.

    ``pipeline_dict`` is import-ready (the shape ``POST /workflows/import``
    consumes). ``report`` carries counts + human-readable warnings so the
    caller can be honest about what will and won't run unchanged.

    Raises ``ValueError`` if ``manifest`` is not a dict.
    """
    if not isinstance(manifest, dict):
        raise ValueError("dbt manifest must be a JSON object")

    nodes = manifest.get("nodes")
    sources = manifest.get("sources")
    nodes = nodes if isinstance(nodes, dict) else {}
    sources = sources if isinstance(sources, dict) else {}

    models: dict[str, dict] = {
        uid: n for uid, n in nodes.items()
        if isinstance(n, dict) and n.get("resource_type") == "model"
    }
    # Seeds and snapshots live in `nodes` alongside models but are relations,
    # not transforms. Keyed the same way so a depends_on entry resolves.
    relations: dict[str, dict] = {
        uid: n for uid, n in nodes.items()
        if isinstance(n, dict) and n.get("resource_type") in _RELATION_RESOURCE_TYPES
    }

    warnings: list[str] = []
    incremental_models: list[str] = []
    missing_sql: list[str] = []
    ephemeral_models: list[str] = []
    unresolved_deps: list[str] = []
    materialized_models: list[str] = []
    merge_key_models: list[str] = []

    # Stable id per model + every upstream relation (edges reference these).
    id_of: dict[str, str] = {}
    for uid in models:
        id_of[uid] = _safe_id(uid)
    for uid in sources:
        id_of[uid] = _safe_id(uid)
    for uid in relations:
        id_of[uid] = _safe_id(uid)

    # Only materialise upstream nodes a model actually reads — an unreferenced
    # source or seed would be a dangling node on the canvas.
    referenced_sources: set[str] = set()
    referenced_relations: set[str] = set()
    for node in models.values():
        for dep in (node.get("depends_on") or {}).get("nodes") or []:
            if dep in sources:
                referenced_sources.add(dep)
            elif dep in relations:
                referenced_relations.add(dep)
            elif dep not in models:
                # Neither a model, a source, a seed nor a snapshot. Nothing
                # can be drawn for it, so the model's SQL will reference a
                # relation with no upstream — say so rather than importing a
                # DAG that merely looks complete.
                unresolved_deps.append(dep)

    steps: list[dict] = []
    connections: list[dict] = []

    # ── Placeholder nodes: sources, seeds, snapshots ─────────────────────
    def _add_placeholder(uid: str, kind: str, label: str, table: str, source_name: str = "") -> None:
        steps.append({
            "id": id_of[uid],
            "type": "source",
            "label": label,
            "params": {
                # Placeholders the UI surfaces so the operator binds a real
                # Revoluzen Flow connection before running. Prefixed to avoid
                # colliding with any real source-node param.
                "_dbt_kind": kind,
                "_needs_connection": True,
                "table": table,
                "source_name": source_name,
            },
            "position": {},  # filled by _layout
        })

    for uid in sorted(referenced_sources):
        src = sources.get(uid) or {}
        sname = src.get("name") or uid
        label = ".".join(x for x in (src.get("source_name"), sname) if x) or sname
        _add_placeholder(uid, "source", label, sname, src.get("source_name", ""))

    for uid in sorted(referenced_relations):
        rel = relations.get(uid) or {}
        kind = rel.get("resource_type") or "seed"
        rname = rel.get("name") or uid
        _add_placeholder(uid, kind, rname, rname, rel.get("schema", ""))

    placeholder_total = len(referenced_sources) + len(referenced_relations)
    if placeholder_total:
        parts = []
        for kind, count in (
            ("source", len(referenced_sources)),
            ("seed", sum(1 for u in referenced_relations
                         if (relations[u].get("resource_type") == "seed"))),
            ("snapshot", sum(1 for u in referenced_relations
                             if (relations[u].get("resource_type") == "snapshot"))),
        ):
            if count:
                parts.append(f"{count} {_PLACEHOLDER_NOUN[kind]}{'' if count == 1 else 's'}")
        listed = (
            parts[0] if len(parts) == 1
            else f"{', '.join(parts[:-1])} and {parts[-1]}"
        )
        warnings.append(
            f"{listed} imported as placeholder nodes — bind each to a real "
            "Revoluzen Flow connection before running. Each one reports a "
            "validation error until you do."
        )

    # ── Model nodes + edges ──────────────────────────────────────────────
    # What each upstream will be called once Revoluzen Flow registers it:
    # the edge alias. Used to rewrite the qualified names dbt compiled in.
    alias_of: dict[str, str] = {}
    for uid, n in models.items():
        alias_of[uid] = n.get("name") or _safe_id(uid)
    for uid in referenced_sources:
        alias_of[uid] = (sources[uid].get("name") or _safe_id(uid))
    for uid in referenced_relations:
        alias_of[uid] = (relations[uid].get("name") or _safe_id(uid))

    def _relation_name_of(uid: str) -> str:
        node = models.get(uid) or sources.get(uid) or relations.get(uid) or {}
        explicit = node.get("relation_name")
        if explicit:
            return str(explicit)
        # Older manifests omit relation_name; reconstruct from the parts dbt
        # would have used.
        parts = [node.get("database"), node.get("schema"),
                 node.get("alias") or node.get("identifier") or node.get("name")]
        return ".".join(str(p) for p in parts if p)

    rewritten_total = 0
    raw_sql_seen = False
    for uid, node in models.items():
        compiled = node.get("compiled_code") or node.get("compiled_sql")
        raw = node.get("raw_code") or node.get("raw_sql")
        sql = compiled or raw or ""
        if not compiled and raw:
            raw_sql_seen = True
        if not sql:
            missing_sql.append(node.get("name", uid))

        # Repoint the compiled, fully-qualified relation names at the
        # aliases this pipeline will actually register.
        deps = (node.get("depends_on") or {}).get("nodes") or []
        rewrites = [
            (_relation_name_of(d), alias_of[d])
            for d in deps
            if d in alias_of and _relation_name_of(d)
        ]
        if rewrites:
            sql, n = _rewrite_relations(sql, rewrites)
            rewritten_total += n

        mat = (
            (node.get("config") or {}).get("materialized")
            or node.get("materialized")
            or "view"
        )
        if mat == "incremental":
            incremental_models.append(node.get("name", uid))
        if mat == "ephemeral":
            ephemeral_models.append(node.get("name", uid))

        steps.append({
            "id": id_of[uid],
            "type": "transform",
            "label": node.get("name", uid),
            "params": {
                "expression": sql,
                "_dbt_kind": "model",
                "_dbt_materialized": mat,
                "_dbt_schema": node.get("schema", ""),
            },
            "position": {},
        })

        # ── Materialization → a real write ───────────────────────────────
        # A dbt model is not just a SELECT: `table` and `incremental` models
        # WRITE A RELATION into the warehouse, and the rest of the project
        # (and everything downstream of dbt) reads those tables. Importing
        # them as a bare Transform produced a pipeline that recomputed the
        # DAG and threw every result away — it ran green and left the
        # warehouse untouched, which is the most expensive kind of wrong.
        # `view` and `ephemeral` legitimately persist nothing: a view is
        # re-evaluated on read, and dbt inlines an ephemeral model as a CTE
        # in its consumers.
        if mat in _MATERIALIZED_TO_WRITE_MODE:
            materialized_models.append(node.get("name", uid))
            sink_params: dict[str, Any] = {
                "_dbt_kind": "materialization",
                "_dbt_materialized": mat,
                # No `connector_type`. The operator must pick one, exactly as
                # for the imported sources, and the validator reports each
                # unbound node until they do. Pre-setting "database" would
                # look complete while `connection_id` stayed empty — and an
                # empty connection on db_sink means "DuckDB in-memory", so
                # the write would silently go nowhere on every run.
                "_needs_connection": True,
                "table": node.get("alias") or node.get("name", uid),
                "schema": node.get("schema", ""),
                "mode": _MATERIALIZED_TO_WRITE_MODE[mat],
            }
            unique_key = (node.get("config") or {}).get("unique_key")
            if mat == "incremental" and unique_key:
                # dbt upserts an incremental model that declares a
                # unique_key, so this maps to the sink's merge mode on the
                # same key(s). dbt allows a single column or a list.
                keys = (
                    [unique_key] if isinstance(unique_key, str)
                    else [str(k) for k in unique_key]
                )
                keys = [k.strip() for k in keys if str(k).strip()]
                if keys:
                    merge_key_models.append(node.get("name", uid))
                    sink_params["mode"] = "merge"
                    sink_params["merge_on"] = keys
                    sink_params["_dbt_unique_key"] = unique_key
            steps.append({
                "id": f"{id_of[uid]}__write",
                "type": "destination",
                "label": f"write {node.get('name', uid)}",
                "params": sink_params,
                "position": {},
            })
            connections.append({
                "from_step": id_of[uid],
                "to_step": f"{id_of[uid]}__write",
                "alias": node.get("name") or _safe_id(uid),
            })

        for dep in (node.get("depends_on") or {}).get("nodes") or []:
            if dep in models:
                connections.append({
                    "from_step": id_of[dep],
                    "to_step": id_of[uid],
                    "alias": models[dep].get("name") or _safe_id(dep),
                })
            elif dep in referenced_sources:
                connections.append({
                    "from_step": id_of[dep],
                    "to_step": id_of[uid],
                    "alias": sources[dep].get("name") or _safe_id(dep),
                })
            elif dep in referenced_relations:
                connections.append({
                    "from_step": id_of[dep],
                    "to_step": id_of[uid],
                    "alias": relations[dep].get("name") or _safe_id(dep),
                })

    # ── Honest caveats ───────────────────────────────────────────────────
    if raw_sql_seen:
        warnings.append(
            "Some models had no compiled SQL, so their raw SQL was used — it "
            "may still contain unresolved {{ }} templating. Run `dbt compile` "
            "first for a clean import."
        )
    if materialized_models:
        warnings.append(
            f"{len(materialized_models)} model(s) materialize a relation in dbt "
            "(table/incremental) and were imported as Transform → Destination. "
            "Each Destination needs a connector and connection chosen before "
            "running — until then it reports a validation error, and nothing "
            "is written."
        )
    if incremental_models:
        warnings.append(
            f"{len(incremental_models)} incremental model(s) write in append "
            "mode. dbt's is_incremental() filter is already baked into the "
            "compiled SQL, but the run-state it depends on is not — review the "
            "WHERE clause before scheduling."
        )
    if merge_key_models:
        shown = ", ".join(sorted(merge_key_models)[:5])
        warnings.append(
            f"{len(merge_key_models)} incremental model(s) declare a unique_key "
            f"({shown}) and were imported as a merge (upsert) on those key "
            "columns, matching dbt. Re-running replaces the matching rows "
            "rather than duplicating them."
        )
    if missing_sql:
        warnings.append(
            f"{len(missing_sql)} model(s) had no SQL body and were imported empty."
        )
    if ephemeral_models:
        warnings.append(
            f"{len(ephemeral_models)} ephemeral model(s) imported as their own "
            "nodes — dbt inlines ephemeral models as CTEs, so their SQL is "
            "already embedded in each consumer. The node is redundant and can "
            "be deleted."
        )
    if unresolved_deps:
        # The case that used to pass silently: a model reading something the
        # importer cannot draw. Its SQL names a relation with no upstream, so
        # the pipeline LOOKS complete and fails at run time with "table not
        # found". Naming it is the whole point of this report.
        shown = ", ".join(sorted(set(unresolved_deps))[:5])
        warnings.append(
            f"{len(set(unresolved_deps))} dependenc(y/ies) could not be mapped "
            f"to a node ({shown}) — the model SQL references them but nothing "
            "upstream provides them. Add a source for each before running."
        )
    if models:
        warnings.append(
            "Model SQL is warehouse-dialect (from `dbt compile`); Revoluzen Flow runs "
            "SQL Transforms on DuckDB. Some functions may need dialect tweaks — "
            "every node is editable after import."
        )

    _layout(steps, connections)

    meta = manifest.get("metadata") or {}
    proj_name = meta.get("project_name") or ""
    pipeline_name = name or (
        f"{proj_name} (dbt import)" if proj_name else "dbt project import"
    )

    pipeline = {
        "name": pipeline_name,
        "description": (
            "Imported from dbt project"
            + (f" '{proj_name}'" if proj_name else "")
            + f" — {len(models)} model(s), {placeholder_total} upstream relation(s)."
        ),
        "metadata": {
            "source": "dbt",
            "dbt_project": proj_name,
            "dbt_schema_version": meta.get("dbt_schema_version", ""),
        },
        "steps": steps,
        "connections": connections,
    }
    report = {
        "models": len(models),
        "sources": len(referenced_sources),
        "seeds": sum(1 for u in referenced_relations
                     if relations[u].get("resource_type") == "seed"),
        "snapshots": sum(1 for u in referenced_relations
                         if relations[u].get("resource_type") == "snapshot"),
        "connections": len(connections),
        "materialized_models": materialized_models,
        "merge_key_models": merge_key_models,
        "incremental_models": incremental_models,
        "ephemeral_models": ephemeral_models,
        "unresolved_dependencies": sorted(set(unresolved_deps)),
        "relation_references_rewritten": rewritten_total,
        "warnings": warnings,
    }
    return pipeline, report


def _layout(steps: list[dict], connections: list[dict]) -> None:
    """Assign a left→right layered position to each step (in place).

    Column = longest dependency path to the node (its depth); rows stack
    within a column. Cheap, deterministic, and gives a readable DAG instead
    of every node piled at (0, 0).
    """
    ids = [s["id"] for s in steps]
    id_set = set(ids)
    preds: dict[str, list[str]] = {i: [] for i in ids}
    for c in connections:
        if c["to_step"] in id_set and c["from_step"] in id_set:
            preds[c["to_step"]].append(c["from_step"])

    depth: dict[str, int] = {}

    def _depth(nid: str, seen: frozenset[str]) -> int:
        if nid in depth:
            return depth[nid]
        if nid in seen:  # cycle guard — a real dbt DAG is acyclic, but be safe
            return 0
        parents = preds.get(nid, [])
        d = 0 if not parents else 1 + max(
            _depth(p, seen | {nid}) for p in parents
        )
        depth[nid] = d
        return d

    for i in ids:
        _depth(i, frozenset())

    per_column: dict[int, int] = defaultdict(int)
    for s in steps:
        col = depth.get(s["id"], 0)
        row = per_column[col]
        per_column[col] += 1
        s["position"] = {"x": col * 280, "y": row * 140}
