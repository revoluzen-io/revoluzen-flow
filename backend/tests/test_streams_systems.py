"""Kinesis, SQS, Pub/Sub, Event Hubs, MQTT, RabbitMQ, NATS and Pulsar steps (2026-11-13).

Every test runs the real executor through the Source and Destination steps.
Kinesis and SQS use botocore's Stubber, which checks each call against AWS's
own API definitions. Pub/Sub talks to a local HTTP server that checks the
signed sign-in token with the service account's public key. RabbitMQ, MQTT,
Event Hubs, NATS and Pulsar use stand-in client modules written from each
library's documented API; no real broker is used here.
"""
from __future__ import annotations

import base64
import csv
import datetime as dt
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from revoluzen_flow.connections.models import Connection
from revoluzen_flow.engine import sync_state_store as position_store
from revoluzen_flow.engine.executor import WorkflowExecutor
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow


@pytest.fixture
def env(tmp_path, connection_store, _revoluzen_flow_test_db, monkeypatch):
    from revoluzen_flow.main import app_state

    monkeypatch.setitem(app_state, "connection_store", connection_store)
    monkeypatch.setitem(app_state, "credential_store", None)
    monkeypatch.setattr(position_store.sync_state_store, "_db", _revoluzen_flow_test_db)

    def save(cid, ctype, **config):
        connection_store.create(Connection(id=cid, name=cid, type=ctype, workspace_id="default", config=config))

    return {"root": tmp_path, "save": save}


def source_run(env, connector, wf_id="wf-1", fail=False, preview=False, **params):
    steps = [Step(id="src", type=StepType.SOURCE, params={"connector_type": connector, "connection_id": "c",
                                                        **params})]
    links, last = [], "src"
    if fail:
        steps.append(Step(id="bad", type=StepType.FILTER, params={"condition": "no_such_column > 1"}))
        links.append(StepConnection(from_step="src", to_step="bad"))
        last = "bad"
    steps.append(Step(id="out", type=StepType.DESTINATION,
                      params={"connector_type": "csv", "file_path": str(env["root"] / f"{wf_id}.csv")}))
    links.append(StepConnection(from_step=last, to_step="out"))
    wf = Workflow(id=wf_id, name=wf_id, steps=steps, connections=links)
    result = WorkflowExecutor(data_dir=str(env["root"])).execute_workflow(wf, preview_mode=preview)
    path = env["root"] / f"{wf_id}.csv"
    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8"))) if path.exists() and not preview else []
    path.unlink(missing_ok=True)
    return result, rows


def sink_run(env, connector, data: list[dict], **params):
    path = env["root"] / "in.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(data[0]))
        writer.writeheader()
        writer.writerows(data)
    wf = Workflow(id="wf-sink", name="s", steps=[
        Step(id="in", type=StepType.SOURCE, params={"connector_type": "csv", "file_path": str(path)}),
        Step(id="to", type=StepType.DESTINATION, params={"connector_type": connector, "connection_id": "c", **params}),
    ], connections=[StepConnection(from_step="in", to_step="to")])
    return WorkflowExecutor(data_dir=str(env["root"])).execute_workflow(wf)


def errors(result):
    return {k: v.error for k, v in result.step_results.items() if v.error}


def saved(wf_id="wf-1"):
    state = position_store.sync_state_store.get(wf_id, "src")
    return None if state is None else (state.cursor_column, json.loads(state.last_cursor))


# ── Kinesis (botocore Stubber) ─────────────────────────────────────────────

@pytest.fixture
def kinesis(env, monkeypatch):
    import boto3
    from botocore.stub import Stubber

    api = boto3.client("kinesis", region_name="eu-west-1", aws_access_key_id="AKIAEXAMPLE",
                       aws_secret_access_key="secret")
    stub = Stubber(api)
    stub.activate()
    from revoluzen_flow.streams import aws
    monkeypatch.setattr(aws, "client", lambda service, config, who: api)
    env["save"]("c", "kinesis", region="eu-west-1", access_key="AKIAEXAMPLE", secret_key="secret",
                stream_name="clicks")
    yield stub
    stub.deactivate()


def _record(seq: str, data: dict, key="k"):
    return {"SequenceNumber": seq, "Data": json.dumps(data).encode(), "PartitionKey": key,
            "ApproximateArrivalTimestamp": dt.datetime(2026, 11, 13, tzinfo=dt.timezone.utc)}


def test_kinesis_reads_every_shard_and_carries_on_after_the_saved_sequence(env, kinesis):
    shards = {"Shards": [{"ShardId": "shardId-0", "HashKeyRange": {"StartingHashKey": "0", "EndingHashKey": "1"},
                          "SequenceNumberRange": {"StartingSequenceNumber": "1"}}]}
    kinesis.add_response("list_shards", shards, {"StreamName": "clicks"})
    kinesis.add_response("get_shard_iterator", {"ShardIterator": "it-1"},
                         {"StreamName": "clicks", "ShardId": "shardId-0", "ShardIteratorType": "TRIM_HORIZON"})
    kinesis.add_response("get_records", {"Records": [_record("101", {"n": 1}), _record("102", {"n": 2})],
                                         "NextShardIterator": "it-2", "MillisBehindLatest": 0},
                         {"ShardIterator": "it-1", "Limit": 100})
    kinesis.add_response("get_records", {"Records": [], "NextShardIterator": "it-3", "MillisBehindLatest": 0},
                         {"ShardIterator": "it-2", "Limit": 98})
    result, rows = source_run(env, "kinesis", stream_name="clicks", include_details=True)
    assert result.status == "success", errors(result)
    assert [r["n"] for r in rows] == ["1", "2"] and rows[0]["_message_key"] == "k"
    assert saved() == ("kinesis:clicks", {"shards": {"shardId-0": {"after": "102"}}})

    kinesis.add_response("list_shards", shards, {"StreamName": "clicks"})
    kinesis.add_response("get_shard_iterator", {"ShardIterator": "it-9"},
                         {"StreamName": "clicks", "ShardId": "shardId-0",
                          "ShardIteratorType": "AFTER_SEQUENCE_NUMBER", "StartingSequenceNumber": "102"})
    kinesis.add_response("get_records", {"Records": [], "NextShardIterator": "it-10", "MillisBehindLatest": 0},
                         {"ShardIterator": "it-9", "Limit": 100})
    again, rows = source_run(env, "kinesis", stream_name="clicks")
    assert again.status == "success" and rows == []
    kinesis.assert_no_pending_responses()


def test_kinesis_retries_records_it_turned_down(env, kinesis):
    kinesis.add_response("put_records", {"FailedRecordCount": 1, "Records": [
        {"SequenceNumber": "1", "ShardId": "s"}, {"ErrorCode": "ProvisionedThroughputExceededException",
                                                  "ErrorMessage": "slow down"}]})
    kinesis.add_response("put_records", {"Records": [{"SequenceNumber": "2", "ShardId": "s"}]})
    result = sink_run(env, "kinesis", [{"id": "1"}, {"id": "2"}], stream_name="clicks", key_column="id")
    assert result.status == "success", errors(result)
    kinesis.assert_no_pending_responses()


# ── SQS (botocore Stubber) ─────────────────────────────────────────────────

URL = "https://sqs.eu-west-1.amazonaws.com/123456789012/orders"


@pytest.fixture
def sqs(env, monkeypatch):
    import boto3
    from botocore.stub import ANY, Stubber

    api = boto3.client("sqs", region_name="eu-west-1", aws_access_key_id="AKIAEXAMPLE", aws_secret_access_key="s")
    stub = Stubber(api)
    stub.activate()
    from revoluzen_flow.streams import aws
    monkeypatch.setattr(aws, "client", lambda service, config, who: api)
    env["save"]("c", "sqs", region="eu-west-1", access_key="AKIAEXAMPLE", secret_key="s")

    def receive(*bodies):
        stub.add_response("receive_message", {"Messages": [
            {"MessageId": f"m{i}", "ReceiptHandle": f"h{i}", "Body": json.dumps(b),
             "Attributes": {"SentTimestamp": "1763020000000"}} for i, b in enumerate(bodies)]},
            {"QueueUrl": URL, "MaxNumberOfMessages": ANY, "WaitTimeSeconds": ANY, "VisibilityTimeout": 120,
             "MessageAttributeNames": ["All"], "AttributeNames": ["All"]})
        stub.add_response("receive_message", {"Messages": []},
                          {"QueueUrl": URL, "MaxNumberOfMessages": ANY, "WaitTimeSeconds": ANY,
                           "VisibilityTimeout": 120, "MessageAttributeNames": ["All"], "AttributeNames": ["All"]})

    env["receive"] = receive
    yield stub
    stub.deactivate()


def test_sqs_deletes_messages_only_after_the_run_succeeds(env, sqs):
    env["receive"]({"id": 1}, {"id": 2})
    sqs.add_response("delete_message_batch", {"Successful": [{"Id": "0"}, {"Id": "1"}], "Failed": []},
                     {"QueueUrl": URL, "Entries": [{"Id": "0", "ReceiptHandle": "h0"}, {"Id": "1", "ReceiptHandle": "h1"}]})
    result, rows = source_run(env, "sqs", queue=URL, hold_seconds=120, timeout_seconds=1)
    assert result.status == "success", errors(result)
    assert [r["id"] for r in rows] == ["1", "2"]
    sqs.assert_no_pending_responses()


def test_sqs_gives_messages_back_when_the_run_fails(env, sqs):
    env["receive"]({"id": 1})
    sqs.add_response("change_message_visibility_batch", {"Successful": [{"Id": "0"}], "Failed": []},
                     {"QueueUrl": URL, "Entries": [{"Id": "0", "ReceiptHandle": "h0", "VisibilityTimeout": 0}]})
    result, _rows = source_run(env, "sqs", fail=True, queue=URL, hold_seconds=120, timeout_seconds=1)
    assert result.status == "error"
    sqs.assert_no_pending_responses()


def test_sqs_fifo_destination_sets_group_and_deduplication(env, sqs):
    from botocore.stub import ANY

    fifo = URL + ".fifo"
    sqs.add_response("send_message_batch", {"Successful": [{"Id": "0", "MessageId": "a", "MD5OfMessageBody": "x"}],
                                            "Failed": []},
                     {"QueueUrl": fifo, "Entries": [{"Id": "0", "MessageBody": '{"id": 7, "name": "a"}',
                                                     "MessageGroupId": "7", "MessageDeduplicationId": ANY}]})
    result = sink_run(env, "sqs", [{"id": "7", "name": "a"}], queue=fifo, key_column="id")
    assert result.status == "success", errors(result)
    sqs.assert_no_pending_responses()


# ── Pub/Sub (a local HTTP stand-in that checks the signed token) ──────────

@pytest.fixture
def pubsub(env):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    state = {"queue": [], "acked": [], "deadlines": [], "published": [], "tokens": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path == "/token":
                assertion = dict(p.split("=", 1) for p in raw.decode().split("&"))["assertion"]
                head, claims, sig = assertion.split(".")
                pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
                key.public_key().verify(pad(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
                assert json.loads(pad(claims))["scope"].endswith("/pubsub")
                state["tokens"] += 1
                return self._reply({"access_token": "tok", "expires_in": 3600})
            assert self.headers["Authorization"] == "Bearer tok"
            body = json.loads(raw)
            if self.path.endswith(":pull"):
                batch, state["queue"] = state["queue"], []
                return self._reply({"receivedMessages": batch})
            if self.path.endswith(":acknowledge"):
                state["acked"] += body["ackIds"]
            elif self.path.endswith(":modifyAckDeadline"):
                state["deadlines"].append((body["ackIds"], body["ackDeadlineSeconds"]))
            elif self.path.endswith(":publish"):
                state["published"] += body["messages"]
                return self._reply({"messageIds": [str(i) for i, _ in enumerate(body["messages"])]})
            return self._reply({})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    env["save"]("c", "pubsub", project_id="proj", api_endpoint=base,
                credentials_json=json.dumps({"client_email": "flow@proj.iam.gserviceaccount.com",
                                             "private_key": pem, "token_uri": f"{base}/token"}))

    def add(*bodies):
        for i, b in enumerate(bodies):
            state["queue"].append({"ackId": f"a{len(state['acked'])}-{i}", "message": {
                "data": base64.b64encode(json.dumps(b).encode()).decode(), "messageId": str(i),
                "publishTime": "2026-11-13T10:00:00.123Z", "attributes": {"origin": "test"}}})

    state["add"] = add
    yield state
    server.shutdown()


def test_pubsub_acknowledges_after_success_and_gives_back_after_failure(env, pubsub):
    pubsub["add"]({"id": 1}, {"id": 2})
    result, rows = source_run(env, "pubsub", subscription="orders-sub", timeout_seconds=2, include_details=True)
    assert result.status == "success", errors(result)
    assert [r["id"] for r in rows] == ["1", "2"] and json.loads(rows[0]["_message_attributes"]) == {"origin": "test"}
    assert sorted(pubsub["acked"]) == ["a0-0", "a0-1"]
    assert pubsub["deadlines"][0][1] == 300
    pubsub["add"]({"id": 3})
    failed, _ = source_run(env, "pubsub", fail=True, subscription="orders-sub", timeout_seconds=2)
    assert failed.status == "error"
    assert pubsub["deadlines"][-1][1] == 0, "the failed run's messages were not given back"
    assert len(pubsub["acked"]) == 2


def test_pubsub_destination_publishes_with_attributes(env, pubsub):
    result = sink_run(env, "pubsub", [{"id": "1", "meta": '{"kind": "order"}'}], topic="orders",
                      attributes_column="meta")
    assert result.status == "success", errors(result)
    [message] = pubsub["published"]
    assert json.loads(base64.b64decode(message["data"])) == {"id": 1}
    assert message["attributes"] == {"kind": "order"}


# ── RabbitMQ, MQTT, Event Hubs, NATS, Pulsar (stand-in client modules) ────

class FakeRabbit:
    def __init__(self):
        self.queue = []
        self.unacked = []
        self.acked = []
        self.published = []

    def module(self):
        broker = self
        mod = types.ModuleType("pika")

        class Method:
            def __init__(self, tag):
                self.delivery_tag, self.routing_key = tag, "orders"

        class Channel:
            def basic_get(self, queue, auto_ack):
                assert auto_ack is False
                if not broker.queue:
                    return None, None, None
                body = broker.queue.pop(0)
                broker.unacked.append(body)
                return Method(len(broker.unacked)), types.SimpleNamespace(headers={}, message_id=None,
                                                                         timestamp=None), body

            def basic_ack(self, delivery_tag, multiple):
                assert multiple
                broker.acked += broker.unacked[:delivery_tag]
                broker.unacked = broker.unacked[delivery_tag:]

            def basic_nack(self, delivery_tag, multiple, requeue):
                assert multiple and requeue
                broker.queue = broker.unacked[:delivery_tag] + broker.queue
                broker.unacked = broker.unacked[delivery_tag:]

            def confirm_delivery(self):
                pass

            def basic_publish(self, exchange, routing_key, body, mandatory, properties):
                broker.published.append((exchange, routing_key, body, properties.headers))

        class Connection:
            def __init__(self, params):
                self.closed = False

            def channel(self):
                return Channel()

            def sleep(self, seconds):
                pass

            def close(self):
                self.closed = True

        mod.BlockingConnection = Connection
        mod.ConnectionParameters = lambda **kw: kw
        mod.PlainCredentials = lambda u, p: (u, p)
        mod.BasicProperties = lambda **kw: types.SimpleNamespace(**kw)
        mod.SSLOptions = lambda ctx, host: (ctx, host)
        return mod


def test_rabbitmq_acknowledges_after_success_and_requeues_after_failure(env, monkeypatch):
    rabbit = FakeRabbit()
    monkeypatch.setitem(sys.modules, "pika", rabbit.module())
    env["save"]("c", "rabbitmq", host="mq", username="u", password="p")
    rabbit.queue = [b'{"id": 1}', b'{"id": 2}']
    result, rows = source_run(env, "rabbitmq", queue="orders", timeout_seconds=1)
    assert result.status == "success", errors(result)
    assert [r["id"] for r in rows] == ["1", "2"] and len(rabbit.acked) == 2 and rabbit.unacked == []
    rabbit.queue = [b'{"id": 3}']
    failed, _ = source_run(env, "rabbitmq", fail=True, queue="orders", timeout_seconds=1)
    assert failed.status == "error" and rabbit.queue == [b'{"id": 3}'] and len(rabbit.acked) == 2
    sent = sink_run(env, "rabbitmq", [{"id": "1"}], routing_key="out")
    assert sent.status == "success", errors(sent)
    assert rabbit.published[0][:3] == ("", "out", b'{"id": 1}')


class FakeMqtt:
    def __init__(self):
        self.pending = []      # (mid, payload) the broker holds for the session
        self.acked = []
        self.subscribes = 0
        self.sessions = set()
        self.published = []

    def module(self):
        broker = self
        client_mod = types.ModuleType("paho.mqtt.client")
        client_mod.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
        client_mod.MQTTv311 = 4

        class Client:
            def __init__(self, callback_api_version, client_id, clean_session, protocol, manual_ack):
                self.cid, self.clean, self.manual = client_id, clean_session, manual_ack
                self.on_connect = self.on_message = None

            def username_pw_set(self, u, p):
                pass

            def tls_set_context(self, ctx):
                pass

            def connect(self, host, port, keepalive):
                present = self.cid in broker.sessions and not self.clean
                broker.sessions.add(self.cid)
                if self.on_connect:  # as paho: a callback that is not set is skipped
                    self.on_connect(self, None, types.SimpleNamespace(session_present=present),
                                    types.SimpleNamespace(is_failure=False), None)
                if present:
                    self._deliver()

            def _deliver(self):
                for mid, payload in list(broker.pending):
                    self.on_message(self, None, types.SimpleNamespace(mid=mid, payload=payload, topic="t/1",
                                                                      qos=1, retain=False))

            def loop_start(self):
                pass

            def loop_stop(self):
                pass

            def subscribe(self, topic, qos):
                broker.subscribes += 1
                self._deliver()

            def ack(self, mid, qos):
                broker.acked.append(mid)
                broker.pending = [p for p in broker.pending if p[0] != mid]

            def disconnect(self):
                pass

            def is_connected(self):
                return True

            def publish(self, topic, payload, qos, retain):
                broker.published.append((topic, payload, qos, retain))
                return types.SimpleNamespace(wait_for_publish=lambda timeout: None, is_published=lambda: True, rc=0)

        client_mod.Client = Client
        paho = types.ModuleType("paho")
        mqtt = types.ModuleType("paho.mqtt")
        paho.mqtt, mqtt.client = mqtt, client_mod
        return {"paho": paho, "paho.mqtt": mqtt, "paho.mqtt.client": client_mod}


def test_mqtt_keeps_a_session_and_acknowledges_only_after_success(env, monkeypatch):
    broker = FakeMqtt()
    for name, mod in broker.module().items():
        monkeypatch.setitem(sys.modules, name, mod)
    env["save"]("c", "mqtt", host="broker", port=1883)
    broker.pending = [(1, b'{"t": 20}'), (2, b'{"t": 21}')]
    failed, _ = source_run(env, "mqtt", fail=True, topic="t/#", timeout_seconds=1)
    assert failed.status == "error" and broker.acked == [] and len(broker.pending) == 2
    result, rows = source_run(env, "mqtt", topic="t/#", timeout_seconds=1)
    assert result.status == "success", errors(result)
    assert [r["t"] for r in rows] == ["20", "21"] and broker.acked == [1, 2]
    assert broker.subscribes == 1, "a kept session subscribed again (retained messages would repeat)"
    sent = sink_run(env, "mqtt", [{"t": "5"}], topic="out/t", qos="1")
    assert sent.status == "success" and broker.published[0][0] == "out/t"


def test_event_hubs_keeps_each_partitions_sequence_number(env, monkeypatch):
    events = {"0": [(5, b'{"n": 1}'), (6, b'{"n": 2}')], "1": [(9, b'{"n": 3}')]}
    seen_starts = []
    mod = types.ModuleType("azure.eventhub")

    class Consumer:
        @classmethod
        def from_connection_string(cls, conn_str, consumer_group, eventhub_name):
            return cls()

        def get_partition_ids(self):
            return ["0", "1"]

        def receive(self, on_event, partition_id, starting_position, starting_position_inclusive, max_wait_time):
            seen_starts.append((partition_id, starting_position))
            for seq, body in events[partition_id]:
                if starting_position == "-1" or (isinstance(starting_position, int) and seq > starting_position):
                    on_event(None, types.SimpleNamespace(body=body, sequence_number=seq, partition_key=None,
                                                         enqueued_time=None, properties={}))
            on_event(None, None)

        def close(self):
            pass

    mod.EventHubConsumerClient = Consumer
    azure = types.ModuleType("azure")
    azure.eventhub = mod
    monkeypatch.setitem(sys.modules, "azure", azure)
    monkeypatch.setitem(sys.modules, "azure.eventhub", mod)
    env["save"]("c", "eventhub", connection_string="Endpoint=sb://ns.servicebus.windows.net/;x", event_hub_name="hub")
    result, rows = source_run(env, "eventhub", event_hub_name="hub", timeout_seconds=2)
    assert result.status == "success", errors(result)
    assert sorted(r["n"] for r in rows) == ["1", "2", "3"]
    assert saved() == ("eventhub:hub", {"partitions": {"0": {"seq": 6}, "1": {"seq": 9}}})
    events["0"].append((7, b'{"n": 4}'))
    again, rows = source_run(env, "eventhub", event_hub_name="hub", timeout_seconds=2)
    assert [r["n"] for r in rows] == ["4"] and ("0", 6) in seen_starts


def test_nats_reads_from_the_saved_stream_sequence(env, monkeypatch):
    stored = [(1, b'{"n": 1}'), (2, b'{"n": 2}'), (3, b'{"n": 3}')]
    asked = []

    class Sub:
        def __init__(self, start):
            self.start, self.done = start, False

        async def fetch(self, batch, timeout):
            if self.done:
                raise TimeoutError()
            self.done = True
            meta = lambda seq: types.SimpleNamespace(sequence=types.SimpleNamespace(stream=seq), timestamp=None)  # noqa: E731
            return [types.SimpleNamespace(data=d, subject="orders.new", headers=None, metadata=meta(s))
                    for s, d in stored if s >= self.start][:batch]

        async def unsubscribe(self):
            pass

    class JS:
        async def find_stream_name_by_subject(self, subject):
            return "ORDERS"

        async def pull_subscribe(self, subject, stream, config):
            asked.append(config.opt_start_seq)
            return Sub(config.opt_start_seq or 1)

    class NC:
        def jetstream(self):
            return JS()

        async def close(self):
            pass

    nats_mod = types.ModuleType("nats")
    nats_mod.__path__ = []  # a package, as the real one is
    js_mod, api_mod = types.ModuleType("nats.js"), types.ModuleType("nats.js.api")
    js_mod.__path__ = []
    api_mod.ConsumerConfig = lambda **kw: types.SimpleNamespace(**kw)
    api_mod.DeliverPolicy = types.SimpleNamespace(BY_START_SEQUENCE="by_start_sequence", ALL="all")
    api_mod.AckPolicy = types.SimpleNamespace(EXPLICIT="explicit")

    async def connect(**kwargs):
        return NC()

    nats_mod.connect = connect
    for name, mod in (("nats", nats_mod), ("nats.js", js_mod), ("nats.js.api", api_mod)):
        monkeypatch.setitem(sys.modules, name, mod)
    env["save"]("c", "nats", servers="nats://n:4222")
    first, rows = source_run(env, "nats", subject="orders.new", max_messages=2, timeout_seconds=2)
    assert first.status == "success", errors(first)
    assert [r["n"] for r in rows] == ["1", "2"] and saved() == ("nats:orders.new", {"seq": 2, "stream": "ORDERS"})
    second, rows = source_run(env, "nats", subject="orders.new", timeout_seconds=2)
    assert [r["n"] for r in rows] == ["3"] and asked == [None, 3]


def test_pulsar_reads_after_the_saved_message_id(env, monkeypatch):
    log = [b'{"n": 1}', b'{"n": 2}']
    mod = types.ModuleType("pulsar")

    class MessageId:
        earliest = "earliest"

        def __init__(self, i):
            self.i = i

        def serialize(self):
            return str(self.i).encode()

        @staticmethod
        def deserialize(data):
            return MessageId(int(data))

    class Reader:
        def __init__(self, start):
            self.next = 0 if start == "earliest" else start.i + 1

        def read_next(self, timeout_millis):
            if self.next >= len(log):
                raise TimeoutError()
            i, self.next = self.next, self.next + 1
            return types.SimpleNamespace(data=lambda: log[i], message_id=lambda: MessageId(i),
                                         partition_key=lambda: "", properties=lambda: {},
                                         publish_timestamp=lambda: 0)

        def close(self):
            pass

    class Client:
        def __init__(self, url, **kw):
            pass

        def create_reader(self, topic, start):
            return Reader(start)

        def close(self):
            pass

    mod.Client, mod.MessageId, mod.AuthenticationToken = Client, MessageId, lambda t: t
    monkeypatch.setitem(sys.modules, "pulsar", mod)
    env["save"]("c", "pulsar", service_url="pulsar://p:6650")
    first, rows = source_run(env, "pulsar", topic="persistent://public/default/orders", timeout_seconds=1)
    assert first.status == "success", errors(first)
    assert [r["n"] for r in rows] == ["1", "2"]
    log.append(b'{"n": 3}')
    second, rows = source_run(env, "pulsar", topic="persistent://public/default/orders", timeout_seconds=1)
    assert [r["n"] for r in rows] == ["3"]


def test_a_missing_add_on_says_which_package(env, monkeypatch):
    monkeypatch.setitem(sys.modules, "pulsar", None)
    env["save"]("c", "pulsar", service_url="pulsar://p:6650")
    result, _ = source_run(env, "pulsar", topic="t", timeout_seconds=1)
    assert "needs an add-on" in errors(result)["src"] and "pulsar-client" in errors(result)["src"]


def test_a_connection_of_another_kind_is_refused(env):
    env["save"]("c", "sqs", region="eu-west-1")
    result, _ = source_run(env, "kinesis", stream_name="x")
    assert "pick a Kinesis connection" in errors(result)["src"]
