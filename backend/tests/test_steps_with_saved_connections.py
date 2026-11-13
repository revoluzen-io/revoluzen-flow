"""Send Email and File steps work when they use a saved connection.

Until November 2026 both steps called ``.get("config")`` on the Connection
object the store returns, which has no ``.get``, so either step failed as
soon as a saved connection was picked. They now read a connection the way
every other step does: secrets decrypted, a linked credential merged, and
the field names the Connections form saves.
"""

from __future__ import annotations

import duckdb
import pytest

from revoluzen_flow.connections.models import Connection
from revoluzen_flow.connections.store import ConnectionStore
from revoluzen_flow.credentials.models import Credential
from revoluzen_flow.credentials.store import CredentialStore
from revoluzen_flow.nodes import flow_control
from revoluzen_flow.nodes.base import ExecutionContext
from revoluzen_flow.nodes.file_node import FileSourceNode, _s3_settings
from revoluzen_flow.nodes.flow_control import SendEmailNode
from revoluzen_flow.security.encryptor import fresh_encryptor_for_tests
from revoluzen_flow.storage.database import Database

SMTP_PASSWORD = "synthetic-smtp-password-9w"
S3_SECRET = "synthetic-s3-secret-with-a-quote-'-in-it"


@pytest.fixture
def state(tmp_path):
    db = Database(str(tmp_path / "revoluzen_flow.db"))
    encryptor = fresh_encryptor_for_tests()
    connections = ConnectionStore(db=db)
    credentials = CredentialStore(db=db)
    connections._get_encryptor = lambda: encryptor
    credentials._get_encryptor = lambda: encryptor
    yield {"connection_store": connections, "credential_store": credentials, "db": db}
    db.close()


def _ctx(state, conn=None, data_dir="."):
    return ExecutionContext(conn or duckdb.connect(), data_dir=data_dir, full_run=True, app_state=state)


class _FakeSMTP:
    """Records what Send Email does instead of talking to a mail server."""

    opened: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.calls = host, port, []
        _FakeSMTP.opened.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def sendmail(self, from_addr, recipients, _message):
        self.calls.append(("sendmail", from_addr, recipients))


@pytest.fixture
def smtp(monkeypatch):
    _FakeSMTP.opened = []
    monkeypatch.setattr(flow_control.smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP


def _smtp_connection(state, **config):
    return state["connection_store"].create(Connection(
        name="Mail", type="smtp", config={
            "host": "smtp.example.test", "port": "587", "username": "mailer@example.test",
            "password": SMTP_PASSWORD, "tls": "true", "from_address": "noreply@example.test", **config,
        },
    ))


# ── Send Email ──────────────────────────────────────────────────────────


def test_send_email_sends_through_the_saved_connection(state, smtp):
    connection = _smtp_connection(state)
    ctx = _ctx(state)
    ctx.set_result("rows", ctx.conn.sql("SELECT 'done' AS status"))
    node = SendEmailNode({
        "connection_id": connection.id, "to": "ops@example.test",
        "subject": "Run {status}", "body": "The run is {status}.", "_input_step_ids": ["rows"],
    })
    node.execute(ctx)
    [server] = smtp.opened
    assert (server.host, server.port) == ("smtp.example.test", 587)
    assert server.calls == [
        "starttls",
        ("login", "mailer@example.test", SMTP_PASSWORD),
        ("sendmail", "noreply@example.test", ["ops@example.test"]),
    ]


def test_send_email_uses_a_credential_linked_to_the_connection(state, smtp):
    credential = state["credential_store"].create(Credential(
        name="Mailer login", type="email", config={"username": "mailer", "password": SMTP_PASSWORD},
    ))
    connection = state["connection_store"].create(Connection(
        name="Mail", type="smtp", credential_id=credential.id, config={
            "host": "smtp.example.test", "port": 587, "from_address": "noreply@example.test",
            "credential_field_map": {"username": "username", "password": "password"},
        },
    ))
    settings = SendEmailNode({"connection_id": connection.id})._resolve_smtp(_ctx(state))
    assert (settings["user"], settings["password"]) == ("mailer", SMTP_PASSWORD)


def test_send_email_fails_clearly_when_the_connection_is_gone(state):
    with pytest.raises(ValueError, match="could not load the SMTP connection"):
        SendEmailNode({"connection_id": "deleted"})._resolve_smtp(_ctx(state))


@pytest.mark.parametrize(("config", "security"), [
    ({"tls": "true", "port": 587}, "tls"),
    ({"tls": True}, "tls"),
    ({}, "tls"),
    ({"tls": "false", "port": 25}, "none"),
    ({"tls": "true", "port": 465}, "ssl"),
    ({"security": "ssl", "port": 2465}, "ssl"),
])
def test_smtp_security_follows_the_connection(config, security):
    assert SendEmailNode._smtp_from_connection({"host": "h", **config})["security"] == security


def test_smtp_fields_saved_through_the_api_are_read_too():
    settings = SendEmailNode._smtp_from_connection({"host": "h", "user": "u", "sender": "s@example.test"})
    assert (settings["user"], settings["from"], settings["port"]) == ("u", "s@example.test", 587)


# ── File ────────────────────────────────────────────────────────────────


def test_file_reads_through_a_saved_share_connection(state, tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    (share / "orders.csv").write_text("id,amount\n1,10\n2,20\n", encoding="utf-8")
    connection = state["connection_store"].create(Connection(
        name="Share", type="local_file", config={"base_path": str(share)},
    ))
    node = FileSourceNode({"connection_id": connection.id, "file_path": "orders.csv"})
    rows = node.execute(_ctx(state, data_dir=str(tmp_path / "data"))).fetchall()
    assert rows == [(1, 10), (2, 20)]


class _RecordingConn:
    def __init__(self):
        self.statements: list[str] = []

    def sql(self, statement):
        self.statements.append(statement)


def test_file_gives_duckdb_the_real_s3_keys_from_a_linked_credential(state):
    credential = state["credential_store"].create(Credential(
        name="Lake keys", type="cloud", config={"access_key": "AKIASYNTHETIC", "secret_key": S3_SECRET},
    ))
    connection = state["connection_store"].create(Connection(
        name="Lake", type="minio", credential_id=credential.id, config={
            "bucket": "lake", "prefix": "/raw/", "region": "eu-west-1",
            "endpoint_url": "http://minio.example.test:9000",
            "credential_field_map": {"access_key": "access_key", "secret_key": "secret_key"},
        },
    ))
    conn = _RecordingConn()
    path, from_storage = FileSourceNode({})._resolve_via_connection(_ctx(state, conn=conn), connection.id, "/orders.csv")
    assert (path, from_storage) == ("s3://lake/raw/orders.csv", True)
    assert set(conn.statements) == {
        "SET s3_access_key_id='AKIASYNTHETIC'",
        "SET s3_secret_access_key='" + S3_SECRET.replace("'", "''") + "'",
        "SET s3_region='eu-west-1'",
        "SET s3_endpoint='minio.example.test:9000'",
        "SET s3_url_style='path'",
        "SET s3_use_ssl=false",
    }


def test_s3_endpoint_without_a_scheme_follows_the_ssl_switch():
    assert _s3_settings({"endpoint_url": "minio:9000", "ssl": "false"})["s3_use_ssl"] is False
    assert "s3_use_ssl" not in _s3_settings({"endpoint_url": "s3.example.test"})


def test_file_fails_clearly_when_the_connection_is_gone(state):
    with pytest.raises(ValueError, match="connection 'deleted' not found"):
        FileSourceNode({"connection_id": "deleted", "file_path": "orders.csv"}).execute(_ctx(state))


def test_a_url_typed_into_the_step_is_not_fetched(state, tmp_path):
    """Only an S3 connection hands DuckDB a URL; a typed one stays a local path."""
    node = FileSourceNode({"file_path": "http://169.254.169.254/latest/meta-data.csv"})
    with pytest.raises(ValueError, match="File: not found"):
        node.execute(_ctx(state, data_dir=str(tmp_path)))
