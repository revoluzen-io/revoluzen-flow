"""Source and destination steps for streams and queues (2026-11-13).

Amazon Kinesis, Amazon SQS, Google Pub/Sub, Azure Event Hubs, MQTT, RabbitMQ,
NATS JetStream and Apache Pulsar. Each is reached through the Source or
Destination step with a saved connection of that type (``nodes/generic.py``),
and reads in batches like the Kafka Source: up to Max Messages within the
Timeout, carrying on after the last successful run. How each system keeps its
place, and when messages are confirmed, is in ``streams/aws.py``,
``streams/pubsub.py`` and ``streams/brokers.py``; the shared rules are in
``streams/positions.py`` and ``streams/holds.py``.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

from revoluzen_flow.ir.schema import StepType
from revoluzen_flow.nodes.base import BaseNode, ExecutionContext
from revoluzen_flow.nodes.registry import register
from revoluzen_flow.streams import messages as msgs
from revoluzen_flow.streams import positions
from revoluzen_flow.streams.holds import HeldMessages, hold

_COMMON_SOURCE = [
    {"name": "message_format", "type": "select", "label": "Message Format", "options": ["json", "text"],
     "default": "json",
     "description": "json: a JSON object's fields become columns. text: the whole message in one column."},
    {"name": "include_details", "type": "boolean", "label": "Include message details", "default": True,
     "description": "Adds _source, _message_id, _message_key, _message_time and _message_attributes."},
    {"name": "max_messages", "type": "number", "label": "Max Messages", "default": 100,
     "description": "The most messages one run reads."},
    {"name": "timeout_seconds", "type": "number", "label": "Timeout (seconds)", "default": 10,
     "description": "The longest a run waits for messages."},
]
_START = {"name": "start_from", "type": "select", "label": "First run starts at", "options": ["oldest", "newest"],
          "default": "oldest",
          "description": "oldest: the oldest message kept. newest: only messages that arrive after the "
                         "first run. Later runs carry on after the last message the previous successful run read."}
_HOLD = {"name": "hold_seconds", "type": "number", "label": "Hold Time (seconds)", "default": 300,
         "description": "How long the queue keeps the run's messages from other readers. They are "
                        "confirmed when the run succeeds and given back when it fails; a run longer "
                        "than this lets the queue deliver them again."}
_COMMON_SINK = [
    {"name": "message_format", "type": "select", "label": "Message Format", "options": ["json", "text"],
     "default": "json", "description": "json: each row as a JSON object. text: one column's text."},
    {"name": "text_column", "type": "text", "label": "Text Column",
     "show_when": {"message_format": ["text"]},
     "description": "With text: the column whose text is each message."},
    {"name": "key_column", "type": "text", "label": "Key Column",
     "description": "Optional. The message key (partition key, routing key, ordering)."},
    {"name": "attributes_column", "type": "text", "label": "Attributes Column",
     "description": "Optional. A column holding a JSON object; its fields become message "
                    "attributes. The column is not part of the message."},
]


def _connection_param(label: str) -> dict:
    return {"name": "connection_id", "type": "connection_picker", "label": "Connection", "required": True,
            "description": f"A saved {label} connection."}


class _StreamSource(BaseNode):
    system = ""
    label = ""
    target = "topic"          # the step setting that names what is read
    target_label = "Topic"
    model = "log"             # "log": Flow keeps the place; "queue": messages are held
    category = "source"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        from revoluzen_flow.streams.connection import saved_connection

        who = f"{self.label} Source"
        config, conn_type = saved_connection(ctx, self.params.get("connection_id"), who)
        if conn_type != self.system:
            raise ValueError(f"{who}: the connection is a {conn_type} connection; pick a {self.label} connection.")
        fmt = msgs.check_format(self.params.get("message_format"), who)
        include = bool(self.params.get("include_details"))
        max_messages = msgs.whole_number(self.params.get("max_messages"), 100, 100000)
        timeout = msgs.whole_number(self.params.get("timeout_seconds"), 10, 3600)
        target = str(self.params.get(self.target) or config.get(self.target) or self._default_target(config)).strip()
        if not target:
            raise ValueError(f"{who}: name the {self.target_label.lower()} to read.")
        step_id = str(self.params.get("_step_id") or "")
        if self.model == "log":
            start_from = str(self.params.get("start_from") or "oldest").lower()
            if start_from not in ("oldest", "newest"):
                raise ValueError(f"{who}: 'First run starts at' is '{start_from}'. Choose oldest or newest.")
            saved = positions.load(ctx, step_id, f"{self.system}:{target}", label=who)
            received, place = self.read_log(config, target, saved.value, start_from, max_messages, timeout, who)
            positions.keep(ctx, step_id, saved, place, len(received), label=who)
        else:
            hold_seconds = msgs.whole_number(self.params.get("hold_seconds"), 300, 43200)
            received, held = self.read_queue(ctx, config, target, max_messages, timeout, hold_seconds, who, step_id)
            if held is not None:
                hold(ctx, held)
        return msgs.relation(ctx, received, fmt, include, f"__{self.system}_source")

    def _default_target(self, config: dict) -> str:
        return ""

    def read_log(self, config, target, saved, start_from, max_messages, timeout, who):  # pragma: no cover
        raise NotImplementedError

    def read_queue(self, ctx, config, target, max_messages, timeout, hold_seconds, who, step_id):  # pragma: no cover
        raise NotImplementedError

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        out = {"connection_id": "", cls.target: "", "message_format": "json", "include_details": True,
               "max_messages": 100, "timeout_seconds": 10}
        out.update({"start_from": "oldest"} if cls.model == "log" else {"hold_seconds": 300})
        return out

    @classmethod
    def param_schema(cls) -> list[dict]:
        return [_connection_param(cls.label),
                {"name": cls.target, "type": "text", "label": cls.target_label, "required": True},
                *_COMMON_SOURCE, _START if cls.model == "log" else _HOLD]


class _StreamSink(BaseNode):
    system = ""
    label = ""
    target = "topic"
    target_label = "Topic"
    category = "output"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        from revoluzen_flow.nodes.sinks import _get_input, _relation_to_dicts
        from revoluzen_flow.streams.connection import saved_connection

        who = f"{self.label} Destination"
        source = _get_input(ctx, self.params)
        config, conn_type = saved_connection(ctx, self.params.get("connection_id"), who)
        if conn_type != self.system:
            raise ValueError(f"{who}: the connection is a {conn_type} connection; pick a {self.label} connection.")
        target = str(self.params.get(self.target) or config.get(self.target) or "").strip()
        if not target and self.target_required:
            raise ValueError(f"{who}: name the {self.target_label.lower()} to write to.")
        records = _relation_to_dicts(ctx.conn, source, f"__{self.system}_sink_export")
        if not records:
            return source
        fmt = msgs.check_format(self.params.get("message_format"), who)
        out = msgs.Outgoing(fmt, list(source.columns), str(self.params.get("key_column") or "").strip(),
                            str(self.params.get("attributes_column") or "").strip(),
                            str(self.params.get("text_column") or "").strip(), who)
        items = [(out.body(r), out.key(r), out.attributes(r)) for r in records]
        self.write(ctx, config, target, items, who)
        return source

    target_required = True

    def write(self, ctx, config, target, items, who):  # pragma: no cover
        raise NotImplementedError

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"connection_id": "", cls.target: "", "message_format": "json", "key_column": "",
                "attributes_column": ""}

    @classmethod
    def param_schema(cls) -> list[dict]:
        return [_connection_param(cls.label),
                {"name": cls.target, "type": "text", "label": cls.target_label, "required": cls.target_required},
                *_COMMON_SINK, *cls.extra_sink_params()]

    @classmethod
    def extra_sink_params(cls) -> list[dict]:
        return []


# ── Amazon Kinesis ─────────────────────────────────────────────────────────

@register(StepType.KINESIS_SOURCE)
class KinesisSourceNode(_StreamSource):
    system, label, target, target_label = "kinesis", "Kinesis", "stream_name", "Stream"
    display_name = "Kinesis Source"
    description = "Read records from an Amazon Kinesis data stream"

    def read_log(self, config, target, saved, start_from, max_messages, timeout, who):
        from revoluzen_flow.streams.aws import client, kinesis_read

        return kinesis_read(client("kinesis", config, who), target, saved, start_from, max_messages, timeout, who)


@register(StepType.KINESIS_SINK)
class KinesisSinkNode(_StreamSink):
    system, label, target, target_label = "kinesis", "Kinesis", "stream_name", "Stream"
    display_name = "Kinesis Destination"
    description = "Put records on an Amazon Kinesis data stream"

    def write(self, ctx, config, target, items, who):
        from revoluzen_flow.streams.aws import client, kinesis_write

        kinesis_write(client("kinesis", config, who), target, [(body, key) for body, key, _a in items], who)


# ── Amazon SQS ─────────────────────────────────────────────────────────────

@register(StepType.SQS_SOURCE)
class SqsSourceNode(_StreamSource):
    system, label, target, target_label, model = "sqs", "SQS", "queue", "Queue", "queue"
    display_name = "SQS Source"
    description = "Read messages from an Amazon SQS queue"

    def _default_target(self, config: dict) -> str:
        return str(config.get("queue_url") or "")

    def read_queue(self, ctx, config, target, max_messages, timeout, hold_seconds, who, step_id):
        from revoluzen_flow.streams.aws import client, sqs_confirm, sqs_give_back, sqs_receive, sqs_url

        sqs = client("sqs", config, who)
        url = sqs_url(sqs, target, who)
        received, handles = sqs_receive(sqs, url, max_messages, timeout, hold_seconds, who)
        if not handles:
            return received, None
        return received, HeldMessages("SQS Source", step_id, len(handles),
                                      confirm=lambda: sqs_confirm(sqs, url, handles),
                                      give_back=lambda: sqs_give_back(sqs, url, handles))


@register(StepType.SQS_SINK)
class SqsSinkNode(_StreamSink):
    system, label, target, target_label = "sqs", "SQS", "queue", "Queue"
    display_name = "SQS Destination"
    description = "Send messages to an Amazon SQS queue"

    def write(self, ctx, config, target, items, who):
        from revoluzen_flow.streams.aws import client, sqs_send, sqs_url

        sqs = client("sqs", config, who)
        sqs_send(sqs, sqs_url(sqs, target or config.get("queue_url"), who), items, who)


# ── Google Pub/Sub ─────────────────────────────────────────────────────────

@register(StepType.PUBSUB_SOURCE)
class PubSubSourceNode(_StreamSource):
    system, label, target, target_label, model = "pubsub", "Pub/Sub", "subscription", "Subscription", "queue"
    display_name = "Pub/Sub Source"
    description = "Read messages from a Google Cloud Pub/Sub subscription"

    def read_queue(self, ctx, config, target, max_messages, timeout, hold_seconds, who, step_id):
        from revoluzen_flow.streams.pubsub import PubSub

        project = PubSub(config, who)
        received, ack_ids = project.pull(target, max_messages, timeout, min(hold_seconds, 600))
        if not ack_ids:
            return received, None
        return received, HeldMessages("Pub/Sub Source", step_id, len(ack_ids),
                                      confirm=lambda: project.acknowledge(target, ack_ids),
                                      give_back=lambda: project.give_back(target, ack_ids))


@register(StepType.PUBSUB_SINK)
class PubSubSinkNode(_StreamSink):
    system, label, target, target_label = "pubsub", "Pub/Sub", "topic", "Topic"
    display_name = "Pub/Sub Destination"
    description = "Publish messages to a Google Cloud Pub/Sub topic"

    def write(self, ctx, config, target, items, who):
        from revoluzen_flow.streams.pubsub import PubSub

        PubSub(config, who).publish(target, items)


# ── Azure Event Hubs ───────────────────────────────────────────────────────

@register(StepType.EVENTHUB_SOURCE)
class EventHubSourceNode(_StreamSource):
    system, label, target, target_label = "eventhub", "Event Hubs", "event_hub_name", "Event Hub"
    display_name = "Event Hubs Source"
    description = "Read events from Azure Event Hubs"

    def read_log(self, config, target, saved, start_from, max_messages, timeout, who):
        from revoluzen_flow.streams.brokers import eventhub_read

        return eventhub_read({**config, "event_hub_name": target}, saved, start_from, max_messages, timeout, who)


@register(StepType.EVENTHUB_SINK)
class EventHubSinkNode(_StreamSink):
    system, label, target, target_label = "eventhub", "Event Hubs", "event_hub_name", "Event Hub"
    display_name = "Event Hubs Destination"
    description = "Send events to Azure Event Hubs"

    def write(self, ctx, config, target, items, who):
        from revoluzen_flow.streams.brokers import eventhub_write

        eventhub_write({**config, "event_hub_name": target}, items, who)


# ── MQTT ───────────────────────────────────────────────────────────────────

@register(StepType.MQTT_SOURCE)
class MqttSourceNode(_StreamSource):
    system, label, target, target_label, model = "mqtt", "MQTT", "topic", "Topic filter", "queue"
    display_name = "MQTT Source"
    description = "Read messages from an MQTT broker"

    def read_queue(self, ctx, config, target, max_messages, timeout, hold_seconds, who, step_id):
        import uuid

        from revoluzen_flow.streams.brokers import client_id, mqtt_close, mqtt_confirm, mqtt_read

        who_owns = positions.owner(ctx, step_id)
        cid = client_id(*who_owns) if who_owns else f"flow-try-{uuid.uuid4().hex[:12]}"
        received, client, raw = mqtt_read(config, target, cid, max_messages, timeout, who,
                                          persistent=who_owns is not None)
        return received, HeldMessages("MQTT Source", step_id, len(raw),
                                      confirm=lambda: mqtt_confirm(client, raw),
                                      close=lambda: mqtt_close(client))


@register(StepType.MQTT_SINK)
class MqttSinkNode(_StreamSink):
    system, label, target, target_label = "mqtt", "MQTT", "topic", "Topic"
    display_name = "MQTT Destination"
    description = "Publish messages to an MQTT broker"

    @classmethod
    def extra_sink_params(cls) -> list[dict]:
        return [{"name": "qos", "type": "select", "label": "QoS", "options": ["0", "1", "2"], "default": "1",
                 "description": "1: the broker confirms each message (recommended)."},
                {"name": "retain", "type": "boolean", "label": "Retain", "default": False,
                 "description": "The broker keeps the last message for new subscribers."}]

    def write(self, ctx, config, target, items, who):
        import uuid

        from revoluzen_flow.streams.brokers import mqtt_write

        qos = int(str(self.params.get("qos") or "1"))
        if qos not in (0, 1, 2):
            raise ValueError(f"{who}: QoS is 0, 1 or 2.")
        mqtt_write(config, target, items, qos, bool(self.params.get("retain")), f"flow-{uuid.uuid4().hex[:12]}", who)


# ── RabbitMQ ───────────────────────────────────────────────────────────────

@register(StepType.RABBITMQ_SOURCE)
class RabbitMqSourceNode(_StreamSource):
    system, label, target, target_label, model = "rabbitmq", "RabbitMQ", "queue", "Queue", "queue"
    display_name = "RabbitMQ Source"
    description = "Read messages from a RabbitMQ queue"

    def read_queue(self, ctx, config, target, max_messages, timeout, hold_seconds, who, step_id):
        from revoluzen_flow.streams.brokers import rabbit_read

        received, connection, channel, last_tag = rabbit_read(config, target, max_messages, timeout, who)
        if last_tag is None:
            connection.close()
            return received, None
        return received, HeldMessages(
            "RabbitMQ Source", step_id, len(received),
            confirm=lambda: channel.basic_ack(delivery_tag=last_tag, multiple=True),
            give_back=lambda: channel.basic_nack(delivery_tag=last_tag, multiple=True, requeue=True),
            close=connection.close,
        )


@register(StepType.RABBITMQ_SINK)
class RabbitMqSinkNode(_StreamSink):
    system, label, target, target_label = "rabbitmq", "RabbitMQ", "routing_key", "Routing Key"
    display_name = "RabbitMQ Destination"
    description = "Publish messages to RabbitMQ"
    target_required = False

    @classmethod
    def extra_sink_params(cls) -> list[dict]:
        return [{"name": "exchange", "type": "text", "label": "Exchange",
                 "description": "Empty: the default exchange, where the routing key is the queue's name."}]

    def write(self, ctx, config, target, items, who):
        from revoluzen_flow.streams.brokers import rabbit_write

        exchange = str(self.params.get("exchange") or "")
        if not exchange and not target and not any(key for _b, key, _a in items):
            raise ValueError(f"{who}: name the queue (Routing Key), or an exchange.")
        rabbit_write(config, exchange, target, items, who)


# ── NATS JetStream ─────────────────────────────────────────────────────────

@register(StepType.NATS_SOURCE)
class NatsSourceNode(_StreamSource):
    system, label, target, target_label = "nats", "NATS", "subject", "Subject"
    display_name = "NATS Source"
    description = "Read messages from a NATS JetStream stream"

    def read_log(self, config, target, saved, start_from, max_messages, timeout, who):
        from revoluzen_flow.streams.brokers import nats_read

        return nats_read(config, target, saved, start_from, max_messages, timeout, who)


@register(StepType.NATS_SINK)
class NatsSinkNode(_StreamSink):
    system, label, target, target_label = "nats", "NATS", "subject", "Subject"
    display_name = "NATS Destination"
    description = "Publish messages to NATS JetStream"

    def write(self, ctx, config, target, items, who):
        from revoluzen_flow.streams.brokers import nats_write

        nats_write(config, target, items, who)


# ── Apache Pulsar ──────────────────────────────────────────────────────────

@register(StepType.PULSAR_SOURCE)
class PulsarSourceNode(_StreamSource):
    system, label, target, target_label = "pulsar", "Pulsar", "topic", "Topic"
    display_name = "Pulsar Source"
    description = "Read messages from an Apache Pulsar topic"

    def read_log(self, config, target, saved, start_from, max_messages, timeout, who):
        from revoluzen_flow.streams.brokers import pulsar_read

        return pulsar_read(config, target, saved, start_from, max_messages, timeout, who)


@register(StepType.PULSAR_SINK)
class PulsarSinkNode(_StreamSink):
    system, label, target, target_label = "pulsar", "Pulsar", "topic", "Topic"
    display_name = "Pulsar Destination"
    description = "Send messages to an Apache Pulsar topic"

    def write(self, ctx, config, target, items, who):
        from revoluzen_flow.streams.brokers import pulsar_write

        pulsar_write(config, target, items, who)
