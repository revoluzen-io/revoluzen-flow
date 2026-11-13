"""Pipeline Output node: save a pipeline's result in Storage -> Pipeline Outputs.

The file sinks (CSV, JSON, Excel) write wherever their path points, so a
pipeline that only wants "my result, as a file I can open or download" had
to make up a path. This node needs only a name. Its file lives at

    outputs/{workspace}/{pipeline}/latest/{name}.{ext}

and every run replaces it, the way Managed Table Sink's replace mode
replaces a table. The Storage page lists it under the pipeline, stamped
with the run that last wrote it.

A run of a canvas that was never saved has no pipeline id; its output goes
under ``adhoc``.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

from revoluzen_flow.common.duckdb_compat import fetch_scalar
from revoluzen_flow.datastore.models import OBJECT_KIND_OUTPUT, StorageObject
from revoluzen_flow.datastore.paths import safe_schema_or_table_name, workspace_paths
from revoluzen_flow.ir.schema import StepType
from revoluzen_flow.nodes.base import BaseNode, ExecutionContext
from revoluzen_flow.nodes.local_table import (
    _ctx_workspace_id,
    _data_dir,
    _datastore_for,
    _table_write_lock,
)
from revoluzen_flow.nodes.registry import register

logger = logging.getLogger(__name__)

#: The formats people can pick: value -> (file extension, DuckDB COPY options).
FORMATS: dict[str, tuple[str, str]] = {
    "csv": ("csv", "FORMAT CSV, HEADER"),
    "parquet": ("parquet", "FORMAT PARQUET"),
    "json": ("json", "FORMAT JSON, ARRAY true"),
}
DEFAULT_FORMAT = "csv"
#: The folder these outputs live in: one file per name, replaced by every run.
LATEST_FOLDER = "latest"
#: Where outputs of a canvas that was never saved go.
ADHOC_FOLDER = "adhoc"


def output_name(params: dict[str, Any]) -> str:
    """The file name without extension: letters, digits and underscores."""
    return safe_schema_or_table_name(params.get("output_name") or "", fallback="")


def output_format(params: dict[str, Any]) -> str:
    fmt = str(params.get("format") or DEFAULT_FORMAT).lower()
    if fmt not in FORMATS:
        raise ValueError(
            f"pipeline_output: unknown format {fmt!r}; expected csv, parquet or json."
        )
    return fmt


def _replace_file(staging: str, final: str) -> None:
    """Move the finished file into place. On Windows a reader holding the old
    file open makes the rename fail for a moment, so it retries briefly."""
    for attempt in range(10):
        try:
            os.replace(staging, final)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.2)


@register(StepType.PIPELINE_OUTPUT)
class PipelineOutputNode(BaseNode):
    """Write the upstream rows to one file in Storage -> Pipeline Outputs,
    replacing that file on every run."""

    display_name = "Pipeline Output"
    category = "destination"
    description = "Save the result as a file in Storage, Pipeline Outputs, replaced on every run"

    @staticmethod
    def preview_message(params: dict[str, Any], input_row_count: int) -> str:
        name = output_name(params) or "<name>"
        try:
            ext = FORMATS[output_format(params)][0]
        except ValueError:
            ext = "?"
        rows = f"{input_row_count} row{'' if input_row_count == 1 else 's'}"
        return f"would replace the pipeline output {name}.{ext} with {rows}"

    def execute(self, ctx: ExecutionContext) -> "duckdb.DuckDBPyRelation":
        name = output_name(self.params)
        if not name:
            raise ValueError("pipeline_output: an output name is required")
        fmt = output_format(self.params)
        ext, copy_options = FORMATS[fmt]

        input_step_ids = self.params.get("_input_step_ids") or []
        inputs = ctx.get_inputs(input_step_ids) if input_step_ids else []
        if not inputs:
            raise ValueError("pipeline_output: no upstream relation.")
        upstream = inputs[0]

        workspace_id = _ctx_workspace_id(ctx)
        paths = workspace_paths(_data_dir(ctx), workspace_id).ensure()
        pipeline_id = getattr(ctx, "workflow_id", None) or ""
        out_dir = paths.output_dir(pipeline_id or ADHOC_FOLDER, LATEST_FOLDER)
        os.makedirs(out_dir, exist_ok=True)
        final = os.path.join(out_dir, f"{name}.{ext}")

        alias = f"__pipeline_output_{uuid.uuid4().hex[:8]}"
        # One writer per file, as for managed tables: two runs of the same
        # pipeline must not interleave their writes.
        with _table_write_lock(final):
            staging = os.path.join(out_dir, f".staging-{uuid.uuid4().hex}.{ext}")
            ctx.conn.register(alias, upstream)
            try:
                target = staging.replace("\\", "/")
                ctx.conn.sql(f"COPY (SELECT * FROM {alias}) TO '{target}' ({copy_options})")
                rows = fetch_scalar(ctx.conn.sql(f"SELECT COUNT(*) FROM {alias}"))
                _replace_file(staging, final)
            finally:
                try:
                    ctx.conn.unregister(alias)
                except Exception:  # noqa: BLE001 - the view is scratch
                    pass
                if os.path.exists(staging):
                    os.remove(staging)
            _record_output(ctx, paths, workspace_id, pipeline_id, final, f"{name}.{ext}", fmt, rows)
        return upstream

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"output_name": "", "format": DEFAULT_FORMAT}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {
                "name": "output_name", "type": "string", "label": "Output name",
                "required": True, "placeholder": "daily_sales",
                "help": (
                    "Saved as <name>.<format> in Storage, Pipeline Outputs, under this "
                    "pipeline. Each run replaces the file. Letters, digits and underscores."
                ),
            },
            {
                "name": "format", "type": "string", "label": "Format",
                "default": DEFAULT_FORMAT,
                "options": [
                    {"value": "csv", "label": "CSV"},
                    {"value": "parquet", "label": "Parquet"},
                    {"value": "json", "label": "JSON"},
                ],
            },
        ]


def _record_output(
    ctx: ExecutionContext,
    paths,
    workspace_id: str,
    pipeline_id: str,
    final: str,
    file_name: str,
    fmt: str,
    rows: Any,
) -> None:
    """List the file under Storage -> Pipeline Outputs, or refresh its entry.

    The file is the same one on every run, so its entry is too: size, rows,
    time and the run that wrote it are brought up to date instead of a new
    entry being added per run. Best effort: the file is already written, and
    the Storage page's next scan of outputs/ picks it up if this fails.
    """
    try:
        store = _datastore_for(ctx)
        rel_path = paths.relative_to_data_dir(final)
        now = datetime.now(timezone.utc)
        run_id = getattr(ctx, "run_id", None)
        try:
            size = os.path.getsize(final)
        except OSError:
            size = 0
        row_count = int(rows) if rows is not None else None
        existing = next(
            (
                o for o in store.list_objects(
                    workspace_id, kind=OBJECT_KIND_OUTPUT, include_deleted=False,
                )
                if o.path == rel_path
            ),
            None,
        )
        if existing is not None:
            existing.size_bytes = size
            existing.row_count = row_count
            existing.run_id = run_id or existing.run_id
            existing.pipeline_id = pipeline_id or existing.pipeline_id
            # Newest first on the Storage page: the file was written anew.
            existing.created_at = now
            existing.updated_at = now
            store.save_object(existing)
            return
        store.save_object(StorageObject(
            workspace_id=workspace_id,
            kind=OBJECT_KIND_OUTPUT,
            name=file_name,
            path=rel_path,
            format=fmt,
            size_bytes=size,
            row_count=row_count,
            pipeline_id=pipeline_id or None,
            run_id=run_id,
            description="Written by Pipeline Output; each run replaces it.",
            created_at=now,
            updated_at=now,
        ))
    except Exception:  # noqa: BLE001 - see docstring
        logger.debug("pipeline_output: storage listing failed", exc_info=True)


__all__ = ["PipelineOutputNode", "FORMATS", "output_format", "output_name"]
