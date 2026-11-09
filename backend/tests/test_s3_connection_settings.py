"""S3 steps honour what the connection form and the step's settings save.

The form saves `endpoint_url` and the step saves `file_format`. The nodes read
`endpoint` and `format`, so a MinIO or other S3-compatible store was ignored and
the upload went to Amazon, and the chosen format was ignored too.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import duckdb
import pytest

from revoluzen_flow.nodes.base import ExecutionContext
from revoluzen_flow.nodes.sinks import S3SinkNode
from revoluzen_flow.nodes.sources import S3SourceNode

CONFIG = {"bucket": "lake", "endpoint_url": "http://store.example.test:9000", "access_key": "synthetic-key", "secret_key": "synthetic-secret", "region": "ap-south-1"}


@pytest.fixture
def uploads(monkeypatch):
    calls = SimpleNamespace(client=None, files=[])

    class _Client:
        def upload_file(self, path, bucket, key):
            con = duckdb.connect(":memory:")
            calls.files.append((bucket, key, path.rsplit(".", 1)[-1], open(path, "rb").read(4)))
            con.close()

    def client(service, **kwargs):
        calls.client = kwargs
        return _Client()

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=client))
    monkeypatch.setattr("revoluzen_flow.nodes.db_source._get_connection_config", lambda cid: (dict(CONFIG), "s3"))
    return calls


def _write(**params):
    ctx = ExecutionContext(conn=duckdb.connect(":memory:"))
    ctx.set_result("in", ctx.conn.sql("SELECT 1 AS id, 'a' AS name"))
    S3SinkNode(params={"_input_step_ids": ["in"], "connection_id": "c1", "key": "out/data", **params}).execute(ctx)


def test_the_upload_goes_to_the_connections_own_endpoint(uploads):
    _write()
    assert uploads.client["endpoint_url"] == CONFIG["endpoint_url"]
    assert uploads.client["aws_access_key_id"] == CONFIG["access_key"]
    assert uploads.client["region_name"] == "ap-south-1"
    assert uploads.files[0][0] == "lake"


def test_with_no_endpoint_it_is_plain_s3(uploads, monkeypatch):
    plain = {k: v for k, v in CONFIG.items() if k != "endpoint_url"}
    monkeypatch.setattr("revoluzen_flow.nodes.db_source._get_connection_config", lambda cid: (plain, "s3"))
    _write()
    assert "endpoint_url" not in uploads.client


def test_an_older_connection_saved_as_endpoint_still_works(uploads, monkeypatch):
    older = {**{k: v for k, v in CONFIG.items() if k != "endpoint_url"}, "endpoint": "http://old.example.test"}
    monkeypatch.setattr("revoluzen_flow.nodes.db_source._get_connection_config", lambda cid: (older, "s3"))
    _write()
    assert uploads.client["endpoint_url"] == "http://old.example.test"


@pytest.mark.parametrize("chosen,suffix,start", [
    ("parquet", "parquet", b"PAR1"),
    ("csv", "csv", b"id,n"),
    ("json", "json", b"[\n\t{"),
    ("jsonl", "jsonl", b"{\"id"),
])
def test_the_chosen_format_is_the_one_written(uploads, chosen, suffix, start):
    _write(file_format=chosen)
    assert uploads.files[0][2] == suffix
    assert uploads.files[0][3] == start


def test_the_older_format_setting_still_works(uploads):
    _write(format="csv")
    assert uploads.files[0][2] == "csv"


def test_the_source_reads_from_the_connections_own_endpoint(uploads, monkeypatch):
    seen = {}

    def read(self, ctx, bucket, key, endpoint, access_key, secret_key, region, file_format):
        seen.update(endpoint=endpoint, file_format=file_format, bucket=bucket)
        return ctx.conn.sql("SELECT 1 AS id")

    monkeypatch.setattr(S3SourceNode, "_read_with_boto3", read)
    ctx = ExecutionContext(conn=duckdb.connect(":memory:"))
    S3SourceNode(params={"connection_id": "c1", "key": "in/data.csv", "file_format": "csv"}).execute(ctx)
    assert seen == {"endpoint": CONFIG["endpoint_url"], "file_format": "csv", "bucket": "lake"}
