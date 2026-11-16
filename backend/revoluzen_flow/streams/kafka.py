"""Reading from and writing to Kafka with either client library (moved here 2026-11-13).

The Kafka Source, the Kafka Sink and the Change Data Capture step's Debezium
mode share this code. It used to live in ``nodes/sources.py`` and
``nodes/sinks.py``; what it does is unchanged (see
``nodes/_kafka_positions.py`` and ``connections/kafka_settings.py``), and it
now also hands over each message's key, headers and time.

Reading takes every partition of the topic with ``assign()``, starts each at
the step's saved offset, joins no consumer group and commits nothing to Kafka.
confluent-kafka is tried first, then kafka-python.
"""

from __future__ import annotations

import time
from typing import Any

from revoluzen_flow.streams.kafka_formats import Message

COMPRESSION = ("none", "gzip", "snappy", "lz4", "zstd")
# kafka-python compresses with gzip by itself; the others need a package each.
KAFKA_PYTHON_CODECS = {
    "snappy": ("has_snappy", "python-snappy"),
    "lz4": ("has_lz4", "lz4"),
    "zstd": ("has_zstd", "zstandard"),
}


def error_text(error: Any) -> str:
    """The readable part of a Kafka client error."""
    inner = error.args[0] if isinstance(error, Exception) and error.args else error
    text = getattr(inner, "str", None)
    return str(text() if callable(text) else inner)


def _call(obj: Any, name: str, default: Any = None) -> Any:
    """``obj.name()`` (confluent) or ``obj.name`` (kafka-python), or ``default``."""
    found = getattr(obj, name, None)
    if found is None:
        return default
    try:
        return found() if callable(found) else found
    except Exception:  # noqa: BLE001 — a missing extra is never worth failing a run
        return default


def read(settings: Any, topic: str, group_id: str, max_messages: int, timeout_seconds: int,
         start: dict[int, int], start_from: str = "oldest", who: str = "Kafka Source",
         ) -> tuple[list[Message], dict[int, int]]:
    """Up to ``max_messages`` messages after ``start``, and each partition's next offset."""
    try:
        return _read_confluent(topic, settings, group_id, max_messages, timeout_seconds, start, start_from, who)
    except ImportError:
        pass
    try:
        return _read_kafka_python(topic, settings, group_id, max_messages, timeout_seconds, start, start_from, who)
    except ImportError:
        from revoluzen_flow.connections.addons import addon_message

        raise ValueError(f"{who}: {addon_message('kafka')}") from None


def _read_confluent(topic, settings, group_id, max_messages, timeout_seconds, start, start_from, who):
    """Read using confluent_kafka.

    With no saved position at all, every partition starts at its oldest
    message, or with ``start_from="newest"`` where it ends now. A partition
    the saved position does not know (added since) starts at its oldest message.
    """
    from confluent_kafka import (  # type: ignore
        OFFSET_BEGINNING, Consumer, KafkaError, KafkaException, TopicPartition,
    )

    from revoluzen_flow.connections.kafka_settings import ClientErrors

    bootstrap_servers = settings.brokers
    client_errors = ClientErrors(settings)
    conf = {
        **settings.confluent(),
        "error_cb": client_errors,
        "group.id": group_id,
        # Used only when a saved offset is no longer in the topic because the
        # broker has deleted it: carry on at the oldest message kept.
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
        # Report the end of each partition, so a run that has caught up stops
        # instead of waiting out the timeout.
        "enable.partition.eof": True,
    }
    try:
        consumer = Consumer(conf)
    except KafkaException as exc:
        raise ValueError(f"{who}: the Kafka client could not start: {settings.hide(error_text(exc))}") from None

    messages: list[Message] = []
    end = dict(start)
    try:
        try:
            metadata = consumer.list_topics(topic, timeout=timeout_seconds)
        except KafkaException as exc:
            raise ValueError(
                f"{who}: could not reach Kafka at {bootstrap_servers}: "
                f"{client_errors.explain(error_text(exc))}"
            ) from None
        found = metadata.topics.get(topic)
        error = getattr(found, "error", None)
        if found is None or not found.partitions or (
            error is not None and error.code() == KafkaError.UNKNOWN_TOPIC_OR_PART
        ):
            raise ValueError(f"{who}: topic '{topic}' was not found at {bootstrap_servers}.")
        if error is not None:
            raise ValueError(f"{who}: topic '{topic}' could not be read: {client_errors.explain(error_text(error))}")
        partitions = sorted(found.partitions)
        if not start and start_from == "newest":
            for p in partitions:
                try:
                    _oldest, after_newest = consumer.get_watermark_offsets(
                        TopicPartition(topic, p), timeout=timeout_seconds,
                    )
                except KafkaException as exc:
                    raise ValueError(
                        f"{who}: could not find where topic '{topic}' ends: "
                        f"{client_errors.explain(error_text(exc))}"
                    ) from None
                end[p] = after_newest
        consumer.assign([TopicPartition(topic, p, end.get(p, OFFSET_BEGINNING)) for p in partitions])

        caught_up: set[int] = set()
        deadline = time.monotonic() + timeout_seconds
        while len(messages) < max_messages and time.monotonic() < deadline:
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                continue
            msg_error = msg.error()
            if msg_error is not None:
                if msg_error.code() == KafkaError._PARTITION_EOF:
                    caught_up.add(msg.partition())
                    if len(caught_up) == len(partitions):
                        break
                continue
            partition = msg.partition()
            caught_up.discard(partition)
            end[partition] = msg.offset() + 1
            stamp = _call(msg, "timestamp")
            messages.append(Message(
                topic=msg.topic(), partition=partition, offset=msg.offset(), value=msg.value(),
                key=_call(msg, "key"), headers=list(_call(msg, "headers") or []),
                timestamp_ms=stamp[1] if isinstance(stamp, tuple) and len(stamp) == 2 and stamp[0] else None,
            ))
    finally:
        consumer.close()
    return messages, end


def _read_kafka_python(topic, settings, group_id, max_messages, timeout_seconds, start, start_from, who):
    """Read using kafka-python, the same way as with confluent_kafka."""
    from kafka import KafkaConsumer, TopicPartition  # type: ignore

    bootstrap_servers = settings.brokers
    try:
        consumer = KafkaConsumer(
            **settings.kafka_python(),
            group_id=group_id,
            auto_offset_reset="earliest",
            enable_auto_commit=False,
            consumer_timeout_ms=timeout_seconds * 1000,
        )
    except Exception as exc:  # noqa: BLE001 — kafka-python raises its own error types
        raise ValueError(
            f"{who}: could not reach Kafka at {bootstrap_servers}: {settings.hide(error_text(exc))}"
        ) from None

    messages: list[Message] = []
    end = dict(start)
    try:
        try:
            partitions = sorted(consumer.partitions_for_topic(topic) or ())
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"{who}: could not reach Kafka at {bootstrap_servers}: {settings.hide(error_text(exc))}"
            ) from None
        if not partitions:
            raise ValueError(f"{who}: topic '{topic}' was not found at {bootstrap_servers}.")
        assigned = [TopicPartition(topic, p) for p in partitions]
        consumer.assign(assigned)
        if not start and start_from == "newest":
            try:
                ends = consumer.end_offsets(assigned)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(
                    f"{who}: could not find where topic '{topic}' ends: {settings.hide(error_text(exc))}"
                ) from None
            for tp in assigned:
                end[tp.partition] = ends[tp]
        for tp in assigned:
            if tp.partition in end:
                consumer.seek(tp, end[tp.partition])
            else:
                consumer.seek_to_beginning(tp)
        for msg in consumer:
            end[msg.partition] = msg.offset + 1
            if msg.value is None:
                # A delete marker gives no row (it used to stop the read).
                continue
            stamp = getattr(msg, "timestamp", None)
            messages.append(Message(
                topic=msg.topic, partition=msg.partition, offset=msg.offset, value=msg.value,
                key=getattr(msg, "key", None), headers=list(getattr(msg, "headers", None) or []),
                timestamp_ms=stamp if isinstance(stamp, int) and stamp >= 0 else None,
            ))
            if len(messages) >= max_messages:
                break
    finally:
        consumer.close()
    return messages, end


# ── Writing ────────────────────────────────────────────────────────────────


def write(settings: Any, topic: str, messages: list[tuple[Any, bytes, list]], batch_size: int,
          compression: str = "none", who: str = "Kafka Sink") -> None:
    """Send ``(key, value, headers)`` messages and check every delivery."""
    if not messages:
        return
    try:
        return _write_confluent(settings, topic, messages, batch_size, compression, who)
    except ImportError:
        pass
    try:
        return _write_kafka_python(settings, topic, messages, batch_size, compression, who)
    except ImportError:
        from revoluzen_flow.connections.addons import addon_message

        raise ValueError(f"{who}: {addon_message('kafka')}") from None


def _write_confluent(settings, topic, messages, batch_size, compression, who):
    """Kafka is asked about the topic first, so a refused sign-in or a broker
    that cannot be reached stops the run at once. Then each message's delivery
    report is checked: a message Kafka did not take stops the run."""
    from confluent_kafka import KafkaException, Producer  # type: ignore

    from revoluzen_flow.connections.kafka_settings import ClientErrors

    client_errors = ClientErrors(settings)
    try:
        producer = Producer({**settings.confluent(), "error_cb": client_errors, "compression.type": compression})
    except KafkaException as exc:
        raise ValueError(f"{who}: the Kafka client could not start: {settings.hide(error_text(exc))}") from None
    try:
        producer.list_topics(topic, timeout=10)
    except KafkaException as exc:
        raise ValueError(
            f"{who}: could not reach Kafka at {settings.brokers}: {client_errors.explain(error_text(exc))}"
        ) from None

    written = 0
    failed: list[str] = []

    def delivered(error, _message) -> None:
        nonlocal written
        if error is None:
            written += 1
        else:
            failed.append(error_text(error))

    for count, (key, value, headers) in enumerate(messages, start=1):
        kwargs = {"value": value, "key": key, "on_delivery": delivered}
        if headers:
            kwargs["headers"] = headers
        producer.produce(topic, **kwargs)
        if count % batch_size == 0:
            producer.flush()
            if failed:
                break
    producer.flush()
    if written < len(messages):
        reason = client_errors.explain(failed[0] if failed else "Kafka did not confirm them")
        raise ValueError(
            f"{who}: {len(messages) - written} of {len(messages)} messages were not written to topic "
            f"'{topic}' at {settings.brokers}: {reason}"
        )


def _write_kafka_python(settings, topic, messages, batch_size, compression, who):
    from kafka import KafkaProducer  # type: ignore

    if compression in KAFKA_PYTHON_CODECS:
        check, package = KAFKA_PYTHON_CODECS[compression]
        try:
            from kafka import codec  # type: ignore
        except ImportError:
            codec = None
        if codec is not None and not getattr(codec, check)():
            raise ValueError(
                f"{who}: {compression} compression needs the {package} package, which this install "
                f"does not have. Choose gzip, or ask whoever set up Revoluzen Flow to add {package}."
            )
    try:
        producer = KafkaProducer(
            **settings.kafka_python(),
            compression_type=None if compression == "none" else compression,
        )
    except Exception as exc:  # noqa: BLE001 — kafka-python raises its own error types
        raise ValueError(f"{who}: could not reach Kafka at {settings.brokers}: {settings.hide(error_text(exc))}") from None

    sent = []
    try:
        for key, value, headers in messages:
            kwargs = {"value": value, "key": key}
            if headers:
                kwargs["headers"] = headers
            sent.append(producer.send(topic, **kwargs))
            if len(sent) % batch_size == 0:
                producer.flush()
        producer.flush()
    except Exception as exc:  # noqa: BLE001
        raise ValueError(
            f"{who}: messages could not be written to topic '{topic}' at {settings.brokers}: "
            f"{settings.hide(error_text(exc))}"
        ) from None
    finally:
        producer.close()
    failed = [future.exception for future in sent if future.failed()]
    if failed:
        raise ValueError(
            f"{who}: {len(failed)} of {len(sent)} messages were not written to topic '{topic}' at "
            f"{settings.brokers}: {settings.hide(error_text(failed[0]))}"
        )
