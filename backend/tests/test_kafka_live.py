"""Kafka Source, Kafka Sink and Debezium events against a real Kafka broker (2026-11-13).

Needs a running broker and the client libraries (confluent-kafka and
kafka-python; each test runs once per library). Tell the tests where the
broker's listeners are::

    REVOLUZEN_FLOW_TEST_KAFKA="plain=127.0.0.1:19092,sasl=127.0.0.1:19094,tls=127.0.0.1:19095,mtls=127.0.0.1:19096"
    REVOLUZEN_FLOW_TEST_KAFKA_CERTS=<folder with ca.pem, client.pem, client-key.pem>

The SASL listener must accept PLAIN, SCRAM-SHA-256 and SCRAM-SHA-512 for the
user ``alice`` with password ``alice-secret``; the mtls listener must ask for a
client certificate signed by ca.pem. Without the variables every test is
skipped. A Schema Registry is played by a small local HTTP server that speaks
the registry's API.
"""
from __future__ import annotations

import csv
import datetime as dt
import decimal
import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from revoluzen_flow.connections.models import Connection
from revoluzen_flow.engine import sync_state_store as position_store
from revoluzen_flow.engine.executor import WorkflowExecutor
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow

LISTENERS = dict(
    part.split("=", 1) for part in os.environ.get("REVOLUZEN_FLOW_TEST_KAFKA", "").split(",") if "=" in part
)
CERTS = os.environ.get("REVOLUZEN_FLOW_TEST_KAFKA_CERTS", "")
confluent_kafka = pytest.importorskip("confluent_kafka") if LISTENERS else None
# The tests' own producer, consumer and admin client always use confluent-kafka,
# held here because the kafka-python runs hide the module from Flow.
if confluent_kafka is not None:
    import confluent_kafka.admin as _ck_admin
    _Producer, _Consumer = confluent_kafka.Producer, confluent_kafka.Consumer
    _AdminClient, _NewTopic = _ck_admin.AdminClient, _ck_admin.NewTopic
pytestmark = [
    pytest.mark.external,
    pytest.mark.skipif(not LISTENERS.get("plain"), reason="REVOLUZEN_FLOW_TEST_KAFKA is not set"),
]


# ── A local Schema Registry ─────────────────────────────────────────────────

class _Registry(BaseHTTPRequestHandler):
    schemas: list[str] = []
    subjects: dict[str, list[int]] = {}

    def log_message(self, *args):  # quiet
        pass

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/vnd.schemaregistry.v1+json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.headers.get("Authorization") != "Basic cmVnOnJlZy1zZWNyZXQ=":
            return self._send(401, {"error_code": 401, "message": "Unauthorized"})
        if self.path.startswith("/schemas/ids/"):
            schema_id = int(self.path.rsplit("/", 1)[1])
            return self._send(200, {"schema": self.schemas[schema_id - 1]})
        return self._send(404, {"error_code": 40403, "message": "Schema not found"})

    def do_POST(self):  # noqa: N802
        if self.headers.get("Authorization") != "Basic cmVnOnJlZy1zZWNyZXQ=":
            return self._send(401, {"error_code": 401, "message": "Unauthorized"})
        subject = self.path.split("/")[2]
        schema = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["schema"]
        if schema not in self.schemas:
            self.schemas.append(schema)
        schema_id = self.schemas.index(schema) + 1
        self.subjects.setdefault(subject, []).append(schema_id)
        return self._send(200, {"id": schema_id})


@pytest.fixture(scope="module")
def registry():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Registry)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


# ── Setup ───────────────────────────────────────────────────────────────────

@pytest.fixture(params=["confluent-kafka", "kafka-python"])
def library(request, monkeypatch):
    if request.param == "kafka-python":
        pytest.importorskip("kafka")
        monkeypatch.setitem(sys.modules, "confluent_kafka", None)  # forces kafka-python
    return request.param


@pytest.fixture
def kafka(tmp_path, connection_store, _revoluzen_flow_test_db, monkeypatch, registry):
    from revoluzen_flow.connectors import cdc
    from revoluzen_flow.main import app_state

    monkeypatch.setitem(app_state, "connection_store", connection_store)
    monkeypatch.setitem(app_state, "credential_store", None)
    monkeypatch.setattr(position_store.sync_state_store, "_db", _revoluzen_flow_test_db)
    monkeypatch.setattr(cdc, "require_plus", lambda feature: None)

    def save(cid: str, **config):
        connection_store.create(Connection(id=cid, name=cid, type="kafka", workspace_id="default", config=config))

    save("c_plain", brokers=LISTENERS["plain"], security_protocol="PLAINTEXT",
         schema_registry_url=registry, schema_registry_username="reg", schema_registry_password="reg-secret")
    return {"root": tmp_path, "save": save}


def admin():
    return _AdminClient({"bootstrap.servers": LISTENERS["plain"]})


def new_topic(partitions: int = 1) -> str:
    name = f"flow-test-{uuid.uuid4().hex[:10]}"
    client = admin()  # kept until Kafka answers; a dropped client cancels the request
    futures = client.create_topics([_NewTopic(name, num_partitions=partitions, replication_factor=1)])
    futures[name].result(30)
    return name


def produce(topic: str, *values, key: bytes | None = None, headers=None, partition: int | None = None):
    producer = _Producer({"bootstrap.servers": LISTENERS["plain"]})
    for value in values:
        data = value if isinstance(value, (bytes, type(None))) else json.dumps(value).encode()
        kwargs = {"value": data, "key": key, "headers": headers}
        if partition is not None:
            kwargs["partition"] = partition
        producer.produce(topic, **kwargs)
    assert producer.flush(30) == 0


def consume_all(topic: str) -> list:
    consumer = _Consumer({"bootstrap.servers": LISTENERS["plain"], "group.id": f"check-{uuid.uuid4().hex}",
                         "auto.offset.reset": "earliest", "enable.partition.eof": True})
    consumer.subscribe([topic])
    out, eof = [], 0
    while eof < 1:
        msg = consumer.poll(10)
        if msg is None:
            break
        if msg.error():
            eof += 1
            continue
        out.append(msg)
    consumer.close()
    return out


def source_pipeline(kafka, topic: str, conn: str = "c_plain", **params) -> Workflow:
    wf_id = f"wf-{uuid.uuid4().hex[:8]}"
    steps = [
        Step(id="src", type=StepType.SOURCE, params={"connector_type": "kafka", "connection_id": conn,
                                                    "topic": topic, "max_messages": 50,
                                                    "timeout_seconds": 15, **params}),
        Step(id="out", type=StepType.DESTINATION,
             params={"connector_type": "csv", "file_path": str(kafka["root"] / f"{wf_id}.csv")}),
    ]
    return Workflow(id=wf_id, name=wf_id, steps=steps, connections=[StepConnection(from_step="src", to_step="out")])


def run(kafka, wf: Workflow):
    return WorkflowExecutor(data_dir=str(kafka["root"]), app_state={"data_dir": str(kafka["root"])}).execute_workflow(wf)


def rows(kafka, wf: Workflow) -> list[dict]:
    path = kafka["root"] / f"{wf.id}.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def errors(result) -> dict:
    return {k: v.error for k, v in result.step_results.items() if v.error}


# ── Reading ─────────────────────────────────────────────────────────────────

def test_each_run_carries_on_after_the_last_successful_run(kafka, library):
    topic = new_topic(partitions=3)
    for p in range(3):
        produce(topic, *({"order": f"{p}-{n}"} for n in range(4)), partition=p)
    wf = source_pipeline(kafka, topic, max_messages=7)
    first = run(kafka, wf)
    assert first.status == "success", errors(first)
    seen = [r["order"] for r in rows(kafka, wf)]
    assert len(seen) == 7
    second = run(kafka, wf)
    assert second.status == "success", errors(second)
    rest = [r["order"] for r in rows(kafka, wf)]
    assert sorted(seen + rest) == sorted(f"{p}-{n}" for p in range(3) for n in range(4)), "lost or repeated"
    assert run(kafka, wf).status == "success" and rows(kafka, wf) == []


def test_key_headers_and_time_come_along_when_asked(kafka, library):
    topic = new_topic()
    produce(topic, {"id": 1}, key=b"customer-7", headers=[("source", b"shop"), ("trace", b"abc")])
    wf = source_pipeline(kafka, topic, include_metadata=True)
    result = run(kafka, wf)
    assert result.status == "success", errors(result)
    [row] = rows(kafka, wf)
    assert row["_kafka_key"] == "customer-7"
    assert json.loads(row["_kafka_headers"]) == {"source": "shop", "trace": "abc"}
    stamp = dt.datetime.fromisoformat(row["_kafka_timestamp"]).replace(tzinfo=dt.timezone.utc)  # kept in UTC
    assert abs((dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()) < 600


def test_text_messages_stay_whole(kafka, library):
    topic = new_topic()
    produce(topic, b'{"looks": "like json"}', b"plain words")
    wf = source_pipeline(kafka, topic, message_format="text")
    assert run(kafka, wf).status == "success"
    assert [r["value"] for r in rows(kafka, wf)] == ['{"looks": "like json"}', "plain words"]


def test_a_topic_that_does_not_exist_fails_plainly(kafka, library):
    result = run(kafka, source_pipeline(kafka, f"missing-{uuid.uuid4().hex[:6]}"))
    assert result.status == "error" and "was not found" in errors(result)["src"]


# ── Writing ─────────────────────────────────────────────────────────────────

def sink_pipeline(kafka, topic: str, csv_rows: list[dict], **params) -> Workflow:
    wf_id = f"wf-{uuid.uuid4().hex[:8]}"
    path = kafka["root"] / f"{wf_id}-in.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(csv_rows[0]))
        writer.writeheader()
        writer.writerows(csv_rows)
    steps = [
        Step(id="in", type=StepType.SOURCE, params={"connector_type": "csv", "file_path": str(path)}),
        Step(id="to_kafka", type=StepType.DESTINATION,
             params={"connector_type": "kafka", "connection_id": "c_plain", "topic": topic, **params}),
    ]
    return Workflow(id=wf_id, name=wf_id, steps=steps, connections=[StepConnection(from_step="in", to_step="to_kafka")])


def test_the_sink_writes_keys_headers_and_json(kafka, library):
    topic = new_topic()
    wf = sink_pipeline(kafka, topic, [{"id": "1", "name": "a", "meta": '{"origin": "flow"}'},
                                      {"id": "2", "name": "b", "meta": ""}],
                       key_column="id", headers_column="meta")
    result = run(kafka, wf)
    assert result.status == "success", errors(result)
    got = consume_all(topic)
    assert [m.key() for m in got] == [b"1", b"2"]
    assert json.loads(got[0].value()) == {"id": 1, "name": "a"}
    assert got[0].headers() == [("origin", b"flow")]
    assert not got[1].headers()


def test_avro_round_trip_through_the_schema_registry(kafka, library):
    pytest.importorskip("fastavro")  # Avro needs it; an install without it says so plainly
    topic = new_topic()
    wf = sink_pipeline(kafka, topic, [{"id": "1", "price": "10.50", "day": "2026-11-13"},
                                      {"id": "2", "price": "7.25", "day": "2026-11-14"}],
                       message_format="avro")
    result = run(kafka, wf)
    assert result.status == "success", errors(result)
    raw = consume_all(topic)
    assert raw[0].value()[0] == 0, "not in the registry's wire format"
    assert f"{topic}-value" in _Registry.subjects

    back = source_pipeline(kafka, topic, message_format="avro")
    read = run(kafka, back)
    assert read.status == "success", errors(read)
    got = rows(kafka, back)
    assert [(r["id"], r["price"], r["day"]) for r in got] == [("1", "10.5", "2026-11-13"), ("2", "7.25", "2026-11-14")]


# ── Debezium change events ─────────────────────────────────────────────────

def _debezium(op: str, before, after, ts: int = 1763020000000) -> dict:
    decimal_field = {"type": "bytes", "optional": True, "name": "org.apache.kafka.connect.data.Decimal",
                     "parameters": {"scale": "2"}, "field": "amount"}
    row_schema = {"type": "struct", "fields": [{"type": "int32", "field": "id"}, decimal_field,
                                               {"type": "int32", "optional": True, "name": "io.debezium.time.Date",
                                                "field": "placed"}]}
    return {
        "schema": {"type": "struct", "fields": [dict(row_schema, field="before"), dict(row_schema, field="after"),
                                               {"type": "struct", "field": "source"}]},
        "payload": {"before": before, "after": after, "op": op,
                    "source": {"db": "shop", "schema": "dbo", "table": "orders", "ts_ms": ts}},
    }


def _amount(value: str) -> str:
    unscaled = int(decimal.Decimal(value).scaleb(2))
    import base64
    return base64.b64encode(unscaled.to_bytes(4, "big", signed=True)).decode()


def test_debezium_events_become_change_rows(kafka, library):
    topic = new_topic()
    produce(
        topic,
        _debezium("r", None, {"id": 1, "amount": _amount("10.50"), "placed": 20405}),
        _debezium("c", None, {"id": 2, "amount": _amount("3.00"), "placed": 20406}),
        _debezium("u", {"id": 1, "amount": _amount("10.50"), "placed": 20405},
                  {"id": 1, "amount": _amount("12.75"), "placed": 20405}),
        _debezium("d", {"id": 2, "amount": _amount("3.00"), "placed": 20406}, None),
        None,  # the tombstone Debezium sends after a delete
    )
    wf_id = f"wf-{uuid.uuid4().hex[:8]}"
    wf = Workflow(id=wf_id, name=wf_id, steps=[
        Step(id="src", type=StepType.CDC_SOURCE, params={"connection_id": "c_plain", "topic": topic,
                                                        "start_from": "copy", "timeout_seconds": 15}),
        Step(id="out", type=StepType.DESTINATION,
             params={"connector_type": "csv", "file_path": str(kafka["root"] / f"{wf_id}.csv")}),
    ], connections=[StepConnection(from_step="src", to_step="out")])
    result = run(kafka, wf)
    assert result.status == "success", errors(result)
    got = rows(kafka, wf)
    assert [(r["_op"], r["id"], r["amount"]) for r in got] == [
        ("snapshot", "1", "10.50"), ("insert", "2", "3.00"), ("update", "1", "12.75"), ("delete", "2", "3.00")]
    assert got[0]["placed"] == "2025-11-13" and got[0]["_table"] == "dbo.orders"
    again = run(kafka, wf)
    assert again.status == "success" and rows(kafka, wf) == []


# ── Signing in for real ────────────────────────────────────────────────────

@pytest.mark.parametrize("mechanism", ["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"])
def test_sasl_sign_in(kafka, library, mechanism):
    topic = new_topic()
    produce(topic, {"secured": True})
    kafka["save"]("c_sasl", brokers=LISTENERS["sasl"], security_protocol="SASL_PLAINTEXT",
                  sasl_mechanism=mechanism, username="alice", password="alice-secret")
    wf = source_pipeline(kafka, topic, conn="c_sasl")
    result = run(kafka, wf)
    assert result.status == "success", errors(result)
    assert rows(kafka, wf)[0]["secured"] in ("true", "True")


def test_a_wrong_password_fails_without_showing_it(kafka, library):
    topic = new_topic()
    kafka["save"]("c_bad", brokers=LISTENERS["sasl"], security_protocol="SASL_PLAINTEXT",
                  sasl_mechanism="SCRAM-SHA-256", username="alice", password="not-the-password")
    result = run(kafka, source_pipeline(kafka, topic, conn="c_bad", timeout_seconds=8))
    assert result.status == "error"
    assert "not-the-password" not in errors(result)["src"]


@pytest.mark.skipif(not CERTS, reason="REVOLUZEN_FLOW_TEST_KAFKA_CERTS is not set")
def test_tls_with_a_pasted_company_ca(kafka, library):
    topic = new_topic()
    produce(topic, {"over": "tls"})
    ca = open(os.path.join(CERTS, "ca.pem"), encoding="ascii").read()
    kafka["save"]("c_tls", brokers=LISTENERS["tls"], security_protocol="SSL", ssl_ca_pem=ca)
    wf = source_pipeline(kafka, topic, conn="c_tls")
    result = run(kafka, wf)
    assert result.status == "success", errors(result)
    assert rows(kafka, wf)[0]["over"] == "tls"


@pytest.mark.skipif(not CERTS, reason="REVOLUZEN_FLOW_TEST_KAFKA_CERTS is not set")
def test_tls_with_a_client_certificate(kafka, library):
    topic = new_topic()
    produce(topic, {"over": "mtls"})
    read = lambda name: open(os.path.join(CERTS, name), encoding="ascii").read()  # noqa: E731
    kafka["save"]("c_mtls", brokers=LISTENERS["mtls"], security_protocol="SSL", ssl_ca_pem=read("ca.pem"),
                  ssl_certificate_pem=read("client.pem"), ssl_key_pem=read("client-key.pem"))
    wf = source_pipeline(kafka, topic, conn="c_mtls")
    result = run(kafka, wf)
    assert result.status == "success", errors(result)
    kafka["save"]("c_nocert", brokers=LISTENERS["mtls"], security_protocol="SSL", ssl_ca_pem=read("ca.pem"))
    refused = run(kafka, source_pipeline(kafka, topic, conn="c_nocert", timeout_seconds=8))
    assert refused.status == "error"
