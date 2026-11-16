"""Test connection for streams and queues (2026-11-13).

Each check signs in and asks for something small that proves the connection
works, without reading or sending any message.
"""

from __future__ import annotations

import time
from typing import Any

LABELS = {
    "kinesis": "Amazon Kinesis", "sqs": "Amazon SQS", "pubsub": "Google Pub/Sub",
    "eventhub": "Azure Event Hubs", "mqtt": "MQTT", "rabbitmq": "RabbitMQ",
    "nats": "NATS JetStream", "pulsar": "Apache Pulsar",
}


def _ok(message: str, started: float, **details: Any) -> dict:
    return {"success": True, "message": message, "error": None, "suggestion": None,
            "details": {"latency_ms": round((time.time() - started) * 1000, 1), **details}}


def _fail(label: str, exc: Exception) -> dict:
    text = str(exc)
    return {"success": False, "message": f"{label} connection failed", "details": {}, "error": text,
            "suggestion": "Check the address, the sign-in and that this server may reach it."}


def test_stream(conn_type: str, config: dict) -> dict:
    label = LABELS.get(conn_type, conn_type)
    who = label
    started = time.time()
    try:
        if conn_type in ("kinesis", "sqs"):
            from revoluzen_flow.streams.aws import client, sqs_url

            api = client(conn_type, config, who)
            if conn_type == "kinesis":
                stream = str(config.get("stream_name") or "").strip()
                if stream:
                    summary = api.describe_stream_summary(StreamName=stream)["StreamDescriptionSummary"]
                    return _ok(f"Reached stream {stream} ({summary.get('StreamStatus')}, "
                               f"{summary.get('OpenShardCount')} open shards)", started)
                api.list_streams(Limit=1)
                return _ok("Signed in to Kinesis", started)
            queue = str(config.get("queue_url") or "").strip()
            if queue:
                url = sqs_url(api, queue, who)
                api.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])
                return _ok(f"Reached queue {url.rsplit('/', 1)[-1]}", started)
            api.list_queues(MaxResults=1)
            return _ok("Signed in to SQS", started)
        if conn_type == "pubsub":
            from revoluzen_flow.streams.pubsub import PubSub

            what = PubSub(config, who).check(str(config.get("subscription") or ""), str(config.get("topic") or ""))
            return _ok(f"Reached Pub/Sub {what}", started)
        if conn_type == "eventhub":
            try:
                from azure.eventhub import EventHubConsumerClient  # type: ignore
            except ImportError:
                from revoluzen_flow.connections.addons import addon_message

                raise ValueError(addon_message("eventhub")) from None
            from revoluzen_flow.streams.brokers import _eventhub_names

            conn_str, hub, group = _eventhub_names(config, who)
            consumer = EventHubConsumerClient.from_connection_string(conn_str, consumer_group=group, eventhub_name=hub)
            try:
                props = consumer.get_eventhub_properties()
            finally:
                consumer.close()
            return _ok(f"Reached event hub {props.get('eventhub_name', hub)} "
                       f"({len(props.get('partition_ids') or [])} partitions)", started)
        if conn_type == "mqtt":
            import uuid

            from revoluzen_flow.streams.brokers import _mqtt_client, mqtt_close

            _mqtt, client = _mqtt_client(config, f"flow-test-{uuid.uuid4().hex[:10]}", who, manual_ack=False,
                                         persistent=False)
            host, port = str(config.get("host") or "localhost"), int(config.get("port") or 1883)
            client.connect(host, port, keepalive=30)
            client.loop_start()
            time.sleep(0.5)
            connected = client.is_connected()
            mqtt_close(client)
            if not connected:
                raise ValueError(f"the broker at {host}:{port} did not accept the connection")
            return _ok(f"Connected to the MQTT broker at {host}:{port}", started)
        if conn_type == "rabbitmq":
            from revoluzen_flow.streams.brokers import _rabbit

            _pika, connection = _rabbit(config, who)
            try:
                queue = str(config.get("queue") or "").strip()
                if queue:
                    connection.channel().queue_declare(queue=queue, passive=True)
            finally:
                connection.close()
            return _ok(f"Connected to RabbitMQ at {config.get('host')}", started)
        if conn_type == "nats":
            from revoluzen_flow.streams.brokers import _nats_connect, _run

            async def check():
                nc = await _nats_connect(config, who)
                try:
                    info = await nc.jetstream().account_info()
                    return getattr(info, "streams", None)
                finally:
                    await nc.close()

            streams = _run(check())
            return _ok("Connected to NATS JetStream" + (f" ({streams} streams)" if streams is not None else ""),
                       started)
        if conn_type == "pulsar":
            from revoluzen_flow.streams.brokers import _pulsar_client

            _pulsar, client = _pulsar_client(config, who)
            try:
                topic = str(config.get("topic") or "").strip()
                if topic:
                    client.get_topic_partitions(topic)
            finally:
                client.close()
            return _ok(f"Connected to Pulsar at {config.get('service_url')}", started)
    except Exception as exc:  # noqa: BLE001
        return _fail(label, exc)
    return {"success": False, "message": f"No tester for {conn_type}", "details": {},
            "error": f"Unsupported connection type: {conn_type}", "suggestion": None}
