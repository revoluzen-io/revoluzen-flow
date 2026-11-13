"""Kafka Source: each run carries on where the last successful run stopped (2026-11-11).

Both Kafka readers turned off Kafka's own position saving and never saved a
position themselves, so every run started again at the oldest message the topic
still kept. A scheduled pipeline read the same messages on every run and an
append destination filled with copies. Every step also shared one default
consumer group, so once positions were saved in Kafka, two pipelines reading one
topic would have split its messages between them.

Flow now keeps each step's read position itself and saves it only after the
whole run succeeds (``nodes/_kafka_positions.py``). These tests run the real
executor and the real position store against a stand-in Kafka client: there is
no broker and no network. The stand-in behaves like a consumer group if a step
subscribes or commits, so going back to a shared group would show up here.
"""
from __future__ import annotations

import csv
import json
import sys
import types
from collections import namedtuple
from types import SimpleNamespace

import pytest

from revoluzen_flow.connections.models import Connection
from revoluzen_flow.engine import sync_state_store as position_store
from revoluzen_flow.engine.executor import WorkflowExecutor
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow

STEP = "src"
TOPIC = "orders"


# ── A stand-in for a Kafka cluster ──────────────────────────────────────────

class FakeKafka:
    """Topics held in memory, and a record of what the clients asked for."""

    def __init__(self) -> None:
        self.topics: dict[str, list[list[bytes | None]]] = {}
        self.group_offsets: dict[tuple[str, str, int], int] = {}
        self.subscribed: list[tuple[str, list[str]]] = []
        self.commits: list[str] = []
        self.configs: list[dict] = []
        self.polls = 0
        self.reachable = True

    def produce(self, topic: str, *values, partition: int = 0, partitions: int = 1) -> None:
        parts = self.topics.setdefault(topic, [[] for _ in range(partitions)])
        for value in values:
            parts[partition].append(None if value is None else json.dumps(value).encode())

    def size(self, topic: str, partition: int) -> int:
        return len(self.topics[topic][partition])

    def start_at(self, topic: str, partition: int, offset: int) -> int:
        """Where a client asking for ``offset`` starts. Out of range means the oldest message."""
        return offset if 0 <= offset <= self.size(topic, partition) else 0

    def take(self, positions: dict[int, int], topic: str):
        """The next message after ``positions``, as (partition, offset, value), or None."""
        for partition in sorted(positions):
            offset = positions[partition]
            if offset < self.size(topic, partition):
                positions[partition] = offset + 1
                return partition, offset, self.topics[topic][partition][offset]
        return None

    def join_group(self, client, group: str, topics: list[str]) -> None:
        """What a consumer group does: start where the group last committed."""
        self.subscribed.append((group, list(topics)))
        client.topic = topics[0]
        for partition in range(len(self.topics.get(client.topic, []))):
            client.positions[partition] = self.group_offsets.get((group, client.topic, partition), 0)

    def commit_group(self, client, group: str) -> None:
        self.commits.append(group)
        for partition, offset in client.positions.items():
            self.group_offsets[(group, client.topic, partition)] = offset


def fake_confluent_kafka(broker: FakeKafka) -> types.ModuleType:
    module = types.ModuleType("confluent_kafka")

    class KafkaError:
        _PARTITION_EOF = -191
        UNKNOWN_TOPIC_OR_PART = 3

        def __init__(self, code: int, text: str = "") -> None:
            self._code, self._text = code, text

        def code(self) -> int:
            return self._code

        def str(self) -> str:
            return self._text

    class KafkaException(Exception):
        pass

    class TopicPartition:
        def __init__(self, topic: str, partition: int = -1, offset: int = -1001) -> None:
            self.topic, self.partition, self.offset = topic, partition, offset

    class Message:
        def __init__(self, topic, partition, offset, value, error=None) -> None:
            self._fields = (topic, partition, offset, value, error)

        def topic(self):
            return self._fields[0]

        def partition(self):
            return self._fields[1]

        def offset(self):
            return self._fields[2]

        def value(self):
            return self._fields[3]

        def error(self):
            return self._fields[4]

    class Consumer:
        def __init__(self, conf: dict) -> None:
            broker.configs.append(dict(conf))
            self.conf = conf
            self.topic: str | None = None
            self.positions: dict[int, int] = {}
            self.at_end: set[int] = set()

        def list_topics(self, topic=None, timeout=-1):
            if not broker.reachable:
                raise KafkaException(KafkaError(-185, "Local: Timed out"))
            parts = broker.topics.get(topic)
            if parts is None:
                found = SimpleNamespace(partitions={}, error=KafkaError(3, "Broker: Unknown topic or partition"))
            else:
                found = SimpleNamespace(partitions={p: SimpleNamespace(id=p) for p in range(len(parts))}, error=None)
            return SimpleNamespace(topics={topic: found})

        def subscribe(self, topics, **_kwargs):
            broker.join_group(self, self.conf.get("group.id"), topics)

        def assign(self, partitions):
            for tp in partitions:
                self.topic = tp.topic
                offset = 0 if tp.offset == module.OFFSET_BEGINNING else tp.offset
                self.positions[tp.partition] = broker.start_at(tp.topic, tp.partition, offset)

        def get_watermark_offsets(self, partition, timeout=None, cached=False):
            """(oldest offset kept, offset after the newest message)."""
            return 0, broker.size(partition.topic, partition.partition)

        def poll(self, timeout=None):
            broker.polls += 1
            taken = broker.take(self.positions, self.topic)
            if taken is not None:
                self.at_end.discard(taken[0])
                return Message(self.topic, *taken)
            if self.conf.get("enable.partition.eof"):
                for partition in sorted(self.positions):
                    if partition not in self.at_end:
                        self.at_end.add(partition)
                        return Message(self.topic, partition, self.positions[partition], None,
                                       KafkaError(KafkaError._PARTITION_EOF, "Broker: No more messages"))
            return None

        def commit(self, *_args, **_kwargs):
            broker.commit_group(self, self.conf.get("group.id"))

        def close(self):
            if self.conf.get("enable.auto.commit", True) and self.positions:
                broker.commit_group(self, self.conf.get("group.id"))

    module.OFFSET_BEGINNING = -2
    module.OFFSET_END = -1
    module.KafkaError = KafkaError
    module.KafkaException = KafkaException
    module.TopicPartition = TopicPartition
    module.Consumer = Consumer
    return module


def fake_kafka_python(broker: FakeKafka) -> types.ModuleType:
    module = types.ModuleType("kafka")
    TopicPartition = namedtuple("TopicPartition", "topic partition")
    Record = namedtuple("Record", "topic partition offset value")

    class KafkaConsumer:
        def __init__(self, *topics, **config) -> None:
            broker.configs.append(dict(config))
            self.config = config
            self.topic: str | None = None
            self.positions: dict[int, int] = {}
            if topics:
                broker.join_group(self, config.get("group_id"), list(topics))

        def partitions_for_topic(self, topic):
            parts = broker.topics.get(topic)
            return set(range(len(parts))) if parts is not None else None

        def subscribe(self, topics=(), **_kwargs):
            broker.join_group(self, self.config.get("group_id"), list(topics))

        def assign(self, partitions):
            for tp in partitions:
                self.topic = tp.topic
                self.positions[tp.partition] = 0

        def seek(self, tp, offset):
            self.positions[tp.partition] = broker.start_at(tp.topic, tp.partition, offset)

        def seek_to_beginning(self, *partitions):
            for tp in partitions:
                self.positions[tp.partition] = 0

        def end_offsets(self, partitions):
            return {tp: broker.size(tp.topic, tp.partition) for tp in partitions}

        def __iter__(self):
            # Stops once nothing new arrives, as consumer_timeout_ms does.
            deserialize = self.config.get("value_deserializer")
            while True:
                taken = broker.take(self.positions, self.topic)
                if taken is None:
                    return
                partition, offset, value = taken
                # kafka-python hands every value to the deserializer, None included.
                yield Record(self.topic, partition, offset, deserialize(value) if deserialize else value)

        def commit(self, *_args, **_kwargs):
            broker.commit_group(self, self.config.get("group_id"))

        def close(self, autocommit=True):
            if autocommit and self.config.get("enable_auto_commit", True) and self.config.get("group_id"):
                broker.commit_group(self, self.config.get("group_id"))

    module.TopicPartition = TopicPartition
    module.KafkaConsumer = KafkaConsumer
    return module


# ── Pipelines ───────────────────────────────────────────────────────────────

@pytest.fixture
def broker(tmp_path, connection_store, _revoluzen_flow_test_db, monkeypatch):
    """A stand-in Kafka behind a saved connection, and the real position store."""
    from revoluzen_flow.main import app_state

    monkeypatch.setitem(app_state, "connection_store", connection_store)
    monkeypatch.setitem(app_state, "credential_store", None)
    monkeypatch.setattr(position_store.sync_state_store, "_db", _revoluzen_flow_test_db)

    fake = FakeKafka()
    fake.root = tmp_path
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_confluent_kafka(fake))
    monkeypatch.setitem(sys.modules, "kafka", None)  # only one client at a time
    # Saved the way the Connections page saves it: the broker list under `brokers`,
    # and the Consumer Group left empty, so every step gets the same default group.
    connection_store.create(Connection(
        id="c_kafka", name="events", type="kafka", workspace_id="default",
        config={"brokers": "broker-1:9092, broker-2:9092", "security_protocol": "PLAINTEXT", "group_id": ""},
    ))
    return fake


def pipeline(broker: FakeKafka, workflow_id: str = "wf-orders", *, fail_after_read: bool = False,
             **source) -> Workflow:
    """Kafka -> (a step that fails) -> CSV file."""
    params = {"connector_type": "kafka", "connection_id": "c_kafka", "topic": TOPIC, "max_messages": 3, **source}
    steps = [Step(id=STEP, type=StepType.SOURCE, params=params)]
    connections = []
    upstream = STEP
    if fail_after_read:
        steps.append(Step(id="check", type=StepType.FILTER, params={"condition": "no_such_column > 1"}))
        connections.append(StepConnection(from_step=STEP, to_step="check"))
        upstream = "check"
    out = broker.root / f"{workflow_id}.csv"
    steps.append(Step(id="out", type=StepType.DESTINATION, params={"connector_type": "csv", "file_path": str(out)}))
    connections.append(StepConnection(from_step=upstream, to_step="out"))
    return Workflow(id=workflow_id, name=workflow_id, steps=steps, connections=connections)


def run(broker: FakeKafka, wf: Workflow, **kwargs):
    executor = WorkflowExecutor(data_dir=str(broker.root), app_state={"data_dir": str(broker.root)})
    return executor.execute_workflow(wf, **kwargs)


def delivered(broker: FakeKafka, workflow_id: str = "wf-orders") -> list[tuple[int, int]]:
    """(partition, offset) of every message the last run wrote to its CSV file."""
    with open(broker.root / f"{workflow_id}.csv", newline="", encoding="utf-8") as handle:
        return [(int(r["_kafka_partition"]), int(r["_kafka_offset"])) for r in csv.DictReader(handle)]


def saved(workflow_id: str = "wf-orders"):
    state = position_store.sync_state_store.get(workflow_id, STEP)
    return None if state is None else (state.cursor_column, json.loads(state.last_cursor)["next_offsets"])


def errors(result) -> dict:
    return {k: v.error for k, v in result.step_results.items() if v.error}


# ── A run carries on after the last message the previous run read ──────────

def test_the_next_run_starts_after_the_last_message_the_previous_run_read(broker):
    broker.produce(TOPIC, *({"order_id": n} for n in range(5)))
    wf = pipeline(broker)

    first = run(broker, wf)
    assert first.status == "success", errors(first)
    assert delivered(broker) == [(0, 0), (0, 1), (0, 2)]
    assert saved() == (f"kafka:{TOPIC}", {"0": 3})

    second = run(broker, wf)
    assert second.status == "success", errors(second)
    assert delivered(broker) == [(0, 3), (0, 4)], "the second run read messages the first had already read"
    assert saved() == (f"kafka:{TOPIC}", {"0": 5})

    broker.produce(TOPIC, {"order_id": 5}, {"order_id": 6})
    assert run(broker, wf).status == "success"
    assert delivered(broker) == [(0, 5), (0, 6)]


def test_a_run_with_nothing_new_reads_nothing_and_stops_at_the_end_of_the_topic(broker):
    broker.produce(TOPIC, {"order_id": 1}, {"order_id": 2})
    wf = pipeline(broker, timeout_seconds=30)
    run(broker, wf)

    broker.polls = 0
    quiet = run(broker, wf)
    assert quiet.status == "success", errors(quiet)
    assert quiet.step_results[STEP].row_count == 0
    assert delivered(broker) == []
    assert saved() == (f"kafka:{TOPIC}", {"0": 2})
    # It stopped when the partition said it had no more messages, instead of
    # polling for the whole 30 seconds.
    assert broker.polls < 10


def test_each_partition_carries_on_from_its_own_offset(broker):
    broker.produce(TOPIC, {"n": "a0"}, {"n": "a1"}, {"n": "a2"}, partition=0, partitions=2)
    broker.produce(TOPIC, {"n": "b0"}, {"n": "b1"}, partition=1, partitions=2)
    wf = pipeline(broker, max_messages=4)

    run(broker, wf)
    assert delivered(broker) == [(0, 0), (0, 1), (0, 2), (1, 0)]
    assert saved() == (f"kafka:{TOPIC}", {"0": 3, "1": 1})

    run(broker, wf)
    assert delivered(broker) == [(1, 1)]
    assert saved() == (f"kafka:{TOPIC}", {"0": 3, "1": 2})


# ── The position moves only when the whole run succeeds ────────────────────

def test_a_failed_run_does_not_move_the_position(broker):
    broker.produce(TOPIC, *({"order_id": n} for n in range(5)))
    assert run(broker, pipeline(broker)).status == "success"
    assert saved() == (f"kafka:{TOPIC}", {"0": 3})

    failed = run(broker, pipeline(broker, fail_after_read=True))
    assert failed.status == "error"
    assert failed.step_results[STEP].row_count == 2, "the failed run should have read offsets 3 and 4"
    assert saved() == (f"kafka:{TOPIC}", {"0": 3}), "a failed run moved the position past messages never delivered"

    # So the next run delivers the messages the failed run never did.
    assert run(broker, pipeline(broker)).status == "success"
    assert delivered(broker) == [(0, 3), (0, 4)]


def test_a_first_run_that_fails_saves_nothing(broker):
    broker.produce(TOPIC, *({"order_id": n} for n in range(3)))
    assert run(broker, pipeline(broker, fail_after_read=True)).status == "error"
    assert saved() is None
    assert run(broker, pipeline(broker)).status == "success"
    assert delivered(broker) == [(0, 0), (0, 1), (0, 2)]


def test_a_sample_run_and_test_node_leave_the_position_alone(broker):
    broker.produce(TOPIC, *({"order_id": n} for n in range(5)))
    wf = pipeline(broker)
    run(broker, wf)
    assert saved() == (f"kafka:{TOPIC}", {"0": 3})

    sample = run(broker, wf, preview_mode=True, sample_row_limit=10)
    assert sample.status == "success", errors(sample)
    assert sample.step_results[STEP].row_count == 2, "a sample shows what the next run would read"
    assert saved() == (f"kafka:{TOPIC}", {"0": 3})

    executor = WorkflowExecutor(data_dir=str(broker.root), app_state={"data_dir": str(broker.root)})
    tested = executor.execute_step(wf, STEP)
    assert tested.status == "success", tested.error
    assert saved() == (f"kafka:{TOPIC}", {"0": 3})

    assert run(broker, wf).status == "success"
    assert delivered(broker) == [(0, 3), (0, 4)]


# ── Two pipelines on one topic each read every message ─────────────────────

def test_two_pipelines_on_one_topic_do_not_take_each_others_messages(broker):
    broker.produce(TOPIC, *({"order_id": n} for n in range(5)))
    billing, audit = pipeline(broker, "wf-billing"), pipeline(broker, "wf-audit")

    assert run(broker, billing).status == "success"
    assert run(broker, audit).status == "success"
    assert delivered(broker, "wf-billing") == [(0, 0), (0, 1), (0, 2)]
    assert delivered(broker, "wf-audit") == [(0, 0), (0, 1), (0, 2)], "the second pipeline started after the first one's messages"

    assert run(broker, billing).status == "success"
    assert run(broker, audit).status == "success"
    assert delivered(broker, "wf-billing") == [(0, 3), (0, 4)]
    assert delivered(broker, "wf-audit") == [(0, 3), (0, 4)]

    # Both used the same default group name, yet neither joined or committed
    # to a group, which is how they could have split or moved each other.
    assert {config["group.id"] for config in broker.configs} == {"revoluzen_flow-consumer"}
    assert broker.subscribed == []
    assert broker.commits == []
    assert saved("wf-billing") == saved("wf-audit") == (f"kafka:{TOPIC}", {"0": 5})


def test_a_group_name_typed_into_both_pipelines_changes_nothing(broker):
    broker.produce(TOPIC, *({"order_id": n} for n in range(4)))
    first = pipeline(broker, "wf-one", group_id="shared-team-group", max_messages=10)
    second = pipeline(broker, "wf-two", group_id="shared-team-group", max_messages=10)
    run(broker, first)
    run(broker, second)
    assert delivered(broker, "wf-one") == delivered(broker, "wf-two") == [(0, n) for n in range(4)]


# ── The saved position belongs to one topic and one connection's brokers ───

def test_a_new_topic_starts_at_its_oldest_message(broker):
    broker.produce(TOPIC, *({"order_id": n} for n in range(4)))
    broker.produce("refunds", {"refund_id": 1}, {"refund_id": 2})
    run(broker, pipeline(broker))
    assert saved() == (f"kafka:{TOPIC}", {"0": 3})

    switched = run(broker, pipeline(broker, topic="refunds"))
    assert switched.status == "success", errors(switched)
    assert delivered(broker) == [(0, 0), (0, 1)], "the old topic's offsets were used for the new topic"
    assert saved() == ("kafka:refunds", {"0": 2})


def test_the_run_uses_the_brokers_saved_in_the_connection(broker):
    broker.produce(TOPIC, {"order_id": 1})
    assert run(broker, pipeline(broker)).status == "success"
    assert broker.configs[-1]["bootstrap.servers"] == "broker-1:9092,broker-2:9092"
    assert broker.configs[-1]["enable.auto.commit"] is False


def test_a_topic_that_does_not_exist_stops_the_run_plainly(broker):
    result = run(broker, pipeline(broker, topic="no-such-topic"))
    assert result.status == "error"
    assert "topic 'no-such-topic' was not found" in result.step_results[STEP].error
    assert saved() is None


def test_kafka_out_of_reach_stops_the_run_instead_of_reading_nothing(broker):
    broker.produce(TOPIC, {"order_id": 1})
    broker.reachable = False
    result = run(broker, pipeline(broker))
    assert result.status == "error"
    assert "could not reach Kafka at broker-1:9092,broker-2:9092" in result.step_results[STEP].error
    assert "Timed out" in result.step_results[STEP].error


# ── Where a first run starts ("First run starts at") ──────────────────────

def use_kafka_python(broker: FakeKafka, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "confluent_kafka", None)
    monkeypatch.setitem(sys.modules, "kafka", fake_kafka_python(broker))


@pytest.mark.parametrize("library", ["confluent-kafka", "kafka-python"])
def test_a_first_run_set_to_newest_reads_only_messages_that_arrive_after_it(broker, monkeypatch, library):
    if library == "kafka-python":
        use_kafka_python(broker, monkeypatch)
    broker.produce(TOPIC, {"n": "a0"}, {"n": "a1"}, partition=0, partitions=2)
    broker.produce(TOPIC, {"n": "b0"}, partition=1, partitions=2)
    wf = pipeline(broker, start_from="newest", max_messages=10)

    first = run(broker, wf)
    assert first.status == "success", errors(first)
    assert delivered(broker) == [], "the first run read messages that were there before it"
    # Where the topic ended is kept, though nothing was read.
    assert saved() == (f"kafka:{TOPIC}", {"0": 2, "1": 1})

    broker.produce(TOPIC, {"n": "a2"}, partition=0, partitions=2)
    broker.produce(TOPIC, {"n": "b1"}, partition=1, partitions=2)
    second = run(broker, wf)
    assert second.status == "success", errors(second)
    assert delivered(broker) == [(0, 2), (1, 1)]


def test_newest_matters_only_for_the_first_run(broker):
    broker.produce(TOPIC, {"order_id": 0})
    run(broker, pipeline(broker, max_messages=10))
    assert saved() == (f"kafka:{TOPIC}", {"0": 1})

    # Switched to newest later: the run still carries on from the saved position.
    broker.produce(TOPIC, {"order_id": 1}, {"order_id": 2})
    assert run(broker, pipeline(broker, start_from="newest", max_messages=10)).status == "success"
    assert delivered(broker) == [(0, 1), (0, 2)]


@pytest.mark.parametrize("start_from, first_read", [("oldest", [(0, 0), (0, 1), (0, 2), (0, 3)]), ("newest", [])])
def test_reset_starts_the_next_run_where_the_setting_says(broker, start_from, first_read):
    broker.produce(TOPIC, *({"order_id": n} for n in range(3)))
    wf = pipeline(broker, start_from=start_from, max_messages=10)
    run(broker, wf)
    broker.produce(TOPIC, {"order_id": 3})
    run(broker, wf)

    # What the Reset button in the step's form does.
    position_store.sync_state_store.reset("wf-orders", STEP)
    assert saved() is None
    assert run(broker, wf).status == "success"
    assert delivered(broker) == first_read


def test_a_step_saved_with_the_old_operation_setting_keeps_its_choice(broker):
    # Before November 2026 the form kept this choice in Operation and Auto
    # Offset Reset, and runs ignored it.
    broker.produce(TOPIC, {"order_id": 1}, {"order_id": 2})
    chose_latest = pipeline(broker, "wf-latest", operation="consume_from_latest", auto_offset_reset="latest")
    chose_earliest = pipeline(broker, "wf-earliest", operation="consume_from_earliest")
    only_reset = pipeline(broker, "wf-reset", auto_offset_reset="latest")

    for wf in (chose_latest, chose_earliest, only_reset):
        assert run(broker, wf).status == "success"
    assert delivered(broker, "wf-latest") == []
    assert delivered(broker, "wf-earliest") == [(0, 0), (0, 1)]
    assert delivered(broker, "wf-reset") == []


def test_an_unknown_first_run_setting_stops_the_run_plainly(broker):
    broker.produce(TOPIC, {"order_id": 1})
    result = run(broker, pipeline(broker, start_from="middle"))
    assert result.status == "error"
    assert "'First run starts at' is 'middle'. Choose oldest or newest." in result.step_results[STEP].error
    assert saved() is None


# ── kafka-python, used when confluent-kafka is not installed ───────────────

def test_kafka_python_carries_on_the_same_way(broker, monkeypatch):
    monkeypatch.setitem(sys.modules, "confluent_kafka", None)
    monkeypatch.setitem(sys.modules, "kafka", fake_kafka_python(broker))
    # Offset 1 is a delete marker: no row, but the position moves past it.
    broker.produce(TOPIC, {"order_id": 0}, None, {"order_id": 2}, {"order_id": 3}, {"order_id": 4})
    wf = pipeline(broker, max_messages=2)

    first = run(broker, wf)
    assert first.status == "success", errors(first)
    assert delivered(broker) == [(0, 0), (0, 2)]
    assert saved() == (f"kafka:{TOPIC}", {"0": 3})

    second = run(broker, wf)
    assert second.status == "success", errors(second)
    assert delivered(broker) == [(0, 3), (0, 4)]

    broker.produce(TOPIC, {"order_id": 5})
    failed = run(broker, pipeline(broker, fail_after_read=True))
    assert failed.status == "error"
    assert failed.step_results[STEP].row_count == 1
    assert saved() == (f"kafka:{TOPIC}", {"0": 5})
    assert run(broker, wf).status == "success"
    assert delivered(broker) == [(0, 5)]

    assert run(broker, pipeline(broker, "wf-other", max_messages=10)).status == "success"
    assert delivered(broker, "wf-other") == [(0, 0), (0, 2), (0, 3), (0, 4), (0, 5)]
    assert broker.subscribed == []
    assert broker.commits == []
