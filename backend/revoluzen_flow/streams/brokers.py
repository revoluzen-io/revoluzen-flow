"""Azure Event Hubs, MQTT, RabbitMQ, NATS JetStream and Apache Pulsar (2026-11-13).

Each needs its own client library, an add-on (``connections/addons.py``):
azure-eventhub, paho-mqtt (2.0 or later), pika, nats-py and pulsar-client.

Logs, where Flow keeps the place (saved after the whole run succeeds):

* Event Hubs: each partition's sequence number. A first run starts at the
  oldest event kept, or at the moment of the first run ("newest").
* NATS JetStream: the stream sequence number, read through a short-lived
  consumer that the server removes after a minute; nothing is acknowledged.
* Pulsar: the last message id read, through a Reader (no subscription).

Queues, where messages are held until the run succeeds:

* RabbitMQ: messages are taken without acknowledging them; after success they
  are acknowledged, otherwise rejected back into the queue.
* MQTT: Flow keeps a persistent session per step (a fixed client id and
  QoS 1) and acknowledges messages only after success; a session closed
  without acknowledging gets them again from the broker on the next run.

The clients were written against each library's published API and checked
with stand-ins in Flow's tests, not against a real broker.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import hashlib
import ssl
import threading
import time
from typing import Any

from revoluzen_flow.streams.messages import Received


def _missing(system: str, who: str) -> ValueError:
    from revoluzen_flow.connections.addons import addon_message

    return ValueError(f"{who}: {addon_message(system)}")


def _context(config: dict) -> ssl.SSLContext | None:
    ca = str(config.get("ssl_ca_pem") or "").strip()
    use = str(config.get("ssl") or config.get("use_tls") or "").lower() in ("1", "true", "yes", "on")
    if not (use or ca):
        return None
    return ssl.create_default_context(cadata=ca or None)


def client_id(workflow_id: str, step_id: str) -> str:
    return "flow-" + hashlib.sha1(f"{workflow_id}:{step_id}".encode()).hexdigest()[:20]


# ── Azure Event Hubs ───────────────────────────────────────────────────────


def eventhub_read(config: dict, saved: dict, start_from: str, max_messages: int, timeout_seconds: int,
                  who: str) -> tuple[list[Received], dict]:
    try:
        from azure.eventhub import EventHubConsumerClient  # type: ignore
    except ImportError:
        raise _missing("eventhub", who) from None
    conn_str, hub, group = _eventhub_names(config, who)
    places = {k: dict(v) for k, v in (saved.get("partitions") or {}).items()}
    first_run = not places
    now = _dt.datetime.now(_dt.timezone.utc)
    probe = EventHubConsumerClient.from_connection_string(conn_str, consumer_group=group, eventhub_name=hub)
    try:
        partitions = sorted(probe.get_partition_ids())
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{who}: could not reach the event hub '{hub}': {exc}") from None
    finally:
        probe.close()
    out: list[Received] = []
    deadline = time.monotonic() + timeout_seconds
    for pid in partitions:
        place = places.get(pid)
        if place is None:
            place = {"at": now.isoformat()} if first_run and start_from == "newest" else {}
            places[pid] = place
        if len(out) >= max_messages or time.monotonic() > deadline:
            continue
        if place.get("seq") is not None:
            starting, inclusive = int(place["seq"]), False
        elif place.get("at"):
            starting, inclusive = _dt.datetime.fromisoformat(place["at"]), True
        else:
            starting, inclusive = "-1", False
        consumer = EventHubConsumerClient.from_connection_string(conn_str, consumer_group=group, eventhub_name=hub)
        done = threading.Event()
        got: list[Any] = []

        def on_event(_context, event, _got=got, _done=done):
            if event is None:  # max_wait_time passed with nothing new
                _done.set()
                return
            if len(out) + len(_got) < max_messages:
                _got.append(event)
            if len(out) + len(_got) >= max_messages:
                _done.set()

        worker = threading.Thread(target=consumer.receive, kwargs={
            "on_event": on_event, "partition_id": pid, "starting_position": starting,
            "starting_position_inclusive": inclusive, "max_wait_time": 2,
        }, daemon=True)
        worker.start()
        done.wait(max(0.5, deadline - time.monotonic()))
        consumer.close()
        worker.join(10)
        for event in got:
            props = {(k.decode() if isinstance(k, bytes) else str(k)): v for k, v in (event.properties or {}).items()}
            out.append(Received(
                body=b"".join(part if isinstance(part, bytes) else bytes(str(part), "utf-8") for part in event.body)
                if not isinstance(event.body, (bytes, str)) else event.body,
                source=hub, message_id=str(event.sequence_number), key=event.partition_key,
                time=event.enqueued_time, attributes={"partition": pid, **props},
            ))
            places[pid] = {"seq": int(event.sequence_number)}
    return out, {"partitions": places}


def _eventhub_names(config: dict, who: str) -> tuple[str, str, str]:
    conn_str = str(config.get("connection_string") or "").strip()
    if not conn_str:
        raise ValueError(f"{who}: the connection needs the Event Hubs connection string.")
    hub = str(config.get("event_hub_name") or config.get("eventhub_name") or "").strip()
    if not hub and "EntityPath=" not in conn_str:
        raise ValueError(f"{who}: name the event hub in the connection.")
    return conn_str, hub or None, str(config.get("consumer_group") or "$Default")


def eventhub_write(config: dict, items: list[tuple[bytes, str | None, dict]], who: str) -> None:
    try:
        from azure.eventhub import EventData, EventHubProducerClient  # type: ignore
    except ImportError:
        raise _missing("eventhub", who) from None
    conn_str, hub, _group = _eventhub_names(config, who)
    producer = EventHubProducerClient.from_connection_string(conn_str, eventhub_name=hub)
    try:
        by_key: dict[str | None, list[tuple[bytes, dict]]] = {}
        for body, key, attrs in items:
            by_key.setdefault(key, []).append((body, attrs))
        for key, bodies in by_key.items():
            batch = producer.create_batch(partition_key=key) if key else producer.create_batch()
            for body, attrs in bodies:
                event = EventData(body)
                if attrs:
                    event.properties = dict(attrs)
                try:
                    batch.add(event)
                except ValueError:  # the batch is full
                    producer.send_batch(batch)
                    batch = producer.create_batch(partition_key=key) if key else producer.create_batch()
                    batch.add(event)
            producer.send_batch(batch)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{who}: could not send to the event hub: {exc}") from None
    finally:
        producer.close()


# ── MQTT ───────────────────────────────────────────────────────────────────


def _mqtt_client(config: dict, cid: str, who: str, manual_ack: bool, persistent: bool = True):
    try:
        import paho.mqtt.client as mqtt  # type: ignore
    except ImportError:
        raise _missing("mqtt", who) from None
    if not hasattr(mqtt, "CallbackAPIVersion"):
        raise ValueError(f"{who}: the MQTT add-on must be paho-mqtt 2.0 or later.")
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=cid,
                         clean_session=not persistent, protocol=mqtt.MQTTv311, manual_ack=manual_ack)
    if config.get("username"):
        client.username_pw_set(str(config.get("username")), str(config.get("password") or ""))
    context = _context(config)
    if context is not None:
        client.tls_set_context(context)
    return mqtt, client


def mqtt_read(config: dict, topic: str, cid: str, max_messages: int, timeout_seconds: int, who: str,
              persistent: bool = True):
    """Messages for ``topic`` and the open client that must acknowledge them later.

    ``persistent`` is off only outside a saved pipeline (Test Node), so trying a
    step leaves no session behind on the broker.
    """
    mqtt, client = _mqtt_client(config, cid, who, manual_ack=True, persistent=persistent)
    received: list[Any] = []
    connected = threading.Event()
    state: dict[str, Any] = {}

    def on_connect(c, _u, flags, reason, _p):
        state["reason"] = reason
        state["present"] = bool(getattr(flags, "session_present", False))
        connected.set()

    def on_message(_c, _u, message):
        if len(received) < max_messages:
            received.append(message)

    client.on_connect = on_connect
    client.on_message = on_message
    host, port = str(config.get("host") or "localhost"), int(config.get("port") or 1883)
    try:
        client.connect(host, port, keepalive=60)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{who}: could not connect to the MQTT broker at {host}:{port}: {exc}") from None
    client.loop_start()
    if not connected.wait(15) or getattr(state.get("reason"), "is_failure", False):
        client.loop_stop()
        raise ValueError(f"{who}: the MQTT broker at {host}:{port} refused the connection: {state.get('reason')}")
    if not (persistent and state.get("present")):
        # A new session subscribes once; later runs keep the session, so a
        # retained message is not handed over again on every run.
        client.subscribe(topic, qos=1)
    deadline = time.monotonic() + timeout_seconds
    quiet_since = time.monotonic()
    seen = 0
    while len(received) < max_messages and time.monotonic() < deadline:
        time.sleep(0.2)
        if len(received) != seen:
            seen, quiet_since = len(received), time.monotonic()
        elif received and time.monotonic() - quiet_since > 2:
            break
    out = [Received(body=m.payload, source=m.topic, message_id=str(m.mid), key=m.topic,
                    attributes={"qos": m.qos, "retain": bool(m.retain)}) for m in received]
    return out, client, list(received)


def mqtt_confirm(client: Any, messages: list[Any]) -> None:
    for m in messages:
        client.ack(m.mid, m.qos)


def mqtt_close(client: Any) -> None:
    try:
        client.disconnect()
    finally:
        client.loop_stop()


def mqtt_write(config: dict, topic: str, items: list[tuple[bytes, str | None, dict]], qos: int, retain: bool,
               cid: str, who: str) -> None:
    _mqtt, client = _mqtt_client(config, cid + "-pub", who, manual_ack=False)
    host, port = str(config.get("host") or "localhost"), int(config.get("port") or 1883)
    try:
        client.connect(host, port, keepalive=60)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{who}: could not connect to the MQTT broker at {host}:{port}: {exc}") from None
    client.loop_start()
    try:
        infos = [client.publish(topic, payload=body, qos=qos, retain=retain) for body, _key, _attrs in items]
        for info in infos:
            info.wait_for_publish(timeout=30)
            if not info.is_published():
                raise ValueError(f"{who}: the MQTT broker did not confirm a message (code {info.rc}).")
    finally:
        mqtt_close(client)


# ── RabbitMQ ───────────────────────────────────────────────────────────────


def _rabbit(config: dict, who: str):
    try:
        import pika  # type: ignore
    except ImportError:
        raise _missing("rabbitmq", who) from None
    host, port = str(config.get("host") or "localhost"), int(config.get("port") or 5672)
    kwargs: dict[str, Any] = {
        "host": host, "port": port, "virtual_host": str(config.get("vhost") or "/"),
        # No heartbeats while the run holds unacknowledged messages, so the
        # broker does not drop the connection while later steps work.
        "heartbeat": 0, "blocked_connection_timeout": 300,
        "credentials": pika.PlainCredentials(str(config.get("username") or "guest"), str(config.get("password") or "guest")),
    }
    context = _context(config)
    if context is not None:
        kwargs["ssl_options"] = pika.SSLOptions(context, host)
    try:
        return pika, pika.BlockingConnection(pika.ConnectionParameters(**kwargs))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{who}: could not connect to RabbitMQ at {host}:{port}: {exc}") from None


def rabbit_read(config: dict, queue: str, max_messages: int, timeout_seconds: int, who: str):
    _pika, connection = _rabbit(config, who)
    channel = connection.channel()
    out: list[Received] = []
    last_tag = None
    deadline = time.monotonic() + timeout_seconds
    try:
        while len(out) < max_messages:
            method, props, body = channel.basic_get(queue=queue, auto_ack=False)
            if method is None:
                if out or time.monotonic() >= deadline:
                    break
                connection.sleep(0.5)
                continue
            last_tag = method.delivery_tag
            headers = dict(getattr(props, "headers", None) or {})
            stamp = getattr(props, "timestamp", None)
            out.append(Received(
                body=body, source=queue, message_id=getattr(props, "message_id", None) or str(method.delivery_tag),
                key=method.routing_key, attributes=headers,
                time=_dt.datetime.fromtimestamp(stamp, tz=_dt.timezone.utc) if stamp else None,
            ))
    except Exception as exc:  # noqa: BLE001
        connection.close()
        raise ValueError(f"{who}: could not read the queue '{queue}': {exc}") from None
    return out, connection, channel, last_tag


def rabbit_write(config: dict, exchange: str, routing_key: str, items: list[tuple[bytes, str | None, dict]],
                 who: str) -> None:
    pika, connection = _rabbit(config, who)
    try:
        channel = connection.channel()
        channel.confirm_delivery()
        for body, key, attrs in items:
            channel.basic_publish(
                exchange=exchange, routing_key=key or routing_key, body=body, mandatory=True,
                properties=pika.BasicProperties(content_type="application/json", delivery_mode=2,
                                                headers=attrs or None),
            )
    except Exception as exc:  # noqa: BLE001 — UnroutableError, NackError, closed channels
        raise ValueError(f"{who}: RabbitMQ did not take every message: {exc}") from None
    finally:
        connection.close()


# ── NATS JetStream ─────────────────────────────────────────────────────────


def _run(coro):
    """Run a coroutine to the end on its own event loop, in its own thread."""
    result: dict[str, Any] = {}

    def target():
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 — handed back to the caller
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


async def _nats_connect(config: dict, who: str):
    try:
        import nats  # type: ignore
    except ImportError:
        raise _missing("nats", who) from None
    servers = config.get("servers") or config.get("url") or f"nats://{config.get('host') or 'localhost'}:{int(config.get('port') or 4222)}"
    if isinstance(servers, str):
        servers = [s.strip() for s in servers.split(",") if s.strip()]
    kwargs: dict[str, Any] = {"servers": servers, "allow_reconnect": False, "connect_timeout": 10}
    if config.get("username"):
        kwargs.update(user=str(config["username"]), password=str(config.get("password") or ""))
    if config.get("token"):
        kwargs["token"] = str(config["token"])
    if config.get("nkey_seed"):
        kwargs["nkeys_seed_str"] = str(config["nkey_seed"])
    context = _context(config)
    if context is not None:
        kwargs["tls"] = context
    try:
        return await nats.connect(**kwargs)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{who}: could not connect to NATS at {', '.join(servers)}: {exc}") from None


def nats_read(config: dict, subject: str, saved: dict, start_from: str, max_messages: int,
              timeout_seconds: int, who: str) -> tuple[list[Received], dict]:
    async def go():
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy  # type: ignore

        nc = await _nats_connect(config, who)
        try:
            js = nc.jetstream()
            try:
                stream = await js.find_stream_name_by_subject(subject)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"{who}: no JetStream stream holds the subject '{subject}': {exc}") from None
            last = int(saved.get("seq") or 0)
            if not saved:
                if start_from == "newest":
                    info = await js.stream_info(stream)
                    last = int(info.state.last_seq)
                    return [], {"stream": stream, "seq": last}
            config_ = ConsumerConfig(
                deliver_policy=DeliverPolicy.BY_START_SEQUENCE if last else DeliverPolicy.ALL,
                opt_start_seq=last + 1 if last else None, ack_policy=AckPolicy.EXPLICIT,
                max_ack_pending=max(max_messages, 1), inactive_threshold=60.0, filter_subject=subject,
            )
            sub = await js.pull_subscribe(subject, stream=stream, config=config_)
            out: list[Received] = []
            deadline = time.monotonic() + timeout_seconds
            while len(out) < max_messages and time.monotonic() < deadline:
                try:
                    batch = await sub.fetch(min(256, max_messages - len(out)),
                                            timeout=max(1.0, min(5.0, deadline - time.monotonic())))
                except Exception:  # noqa: BLE001 — a fetch with nothing new times out
                    break
                for msg in batch:
                    meta = msg.metadata
                    last = int(meta.sequence.stream)
                    out.append(Received(body=msg.data, source=msg.subject, message_id=str(last), key=msg.subject,
                                        time=meta.timestamp, attributes=dict(msg.headers or {})))
            try:
                await sub.unsubscribe()
            except Exception:  # noqa: BLE001
                pass
            return out, {"stream": stream, "seq": last}
        finally:
            await nc.close()

    return _run(go())


def nats_write(config: dict, subject: str, items: list[tuple[bytes, str | None, dict]], who: str) -> None:
    async def go():
        nc = await _nats_connect(config, who)
        try:
            js = nc.jetstream()
            for body, key, attrs in items:
                try:
                    await js.publish(key or subject, body, headers=attrs or None)
                except Exception as exc:  # noqa: BLE001
                    raise ValueError(f"{who}: JetStream did not take a message for '{key or subject}': {exc}") from None
        finally:
            await nc.close()

    _run(go())


# ── Apache Pulsar ──────────────────────────────────────────────────────────


def _pulsar_client(config: dict, who: str):
    try:
        import pulsar  # type: ignore
    except ImportError:
        raise _missing("pulsar", who) from None
    url = str(config.get("service_url") or "").strip()
    if not url:
        raise ValueError(f"{who}: the connection needs the Pulsar service URL, for example pulsar://host:6650.")
    kwargs: dict[str, Any] = {"operation_timeout_seconds": 30}
    if config.get("token"):
        kwargs["authentication"] = pulsar.AuthenticationToken(str(config["token"]))
    return pulsar, pulsar.Client(url, **kwargs)


def pulsar_read(config: dict, topic: str, saved: dict, start_from: str, max_messages: int,
                timeout_seconds: int, who: str) -> tuple[list[Received], dict]:
    pulsar, client = _pulsar_client(config, who)
    try:
        if saved.get("id"):
            start = pulsar.MessageId.deserialize(base64.b64decode(saved["id"]))
        else:
            start = pulsar.MessageId.earliest
        try:
            reader = client.create_reader(topic, start)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"{who}: could not read the topic '{topic}': {exc}") from None
        if not saved and start_from == "newest":
            now = int(time.time() * 1000)
            reader.close()
            return [], {"at": now}
        if saved.get("at") and not saved.get("id"):
            reader.seek(int(saved["at"]))
        out: list[Received] = []
        last = saved.get("id")
        deadline = time.monotonic() + timeout_seconds
        while len(out) < max_messages and time.monotonic() < deadline:
            try:
                msg = reader.read_next(timeout_millis=int(max(200, min(2000, (deadline - time.monotonic()) * 1000))))
            except Exception:  # noqa: BLE001 — pulsar.Timeout: nothing new
                break
            last = base64.b64encode(msg.message_id().serialize()).decode("ascii")
            stamp = msg.publish_timestamp()
            out.append(Received(body=msg.data(), source=topic, message_id=last, key=msg.partition_key() or None,
                                time=_dt.datetime.fromtimestamp(stamp / 1000, tz=_dt.timezone.utc) if stamp else None,
                                attributes=dict(msg.properties() or {})))
        reader.close()
        return out, ({"id": last} if last else dict(saved))
    finally:
        client.close()


def pulsar_write(config: dict, topic: str, items: list[tuple[bytes, str | None, dict]], who: str) -> None:
    _pulsar, client = _pulsar_client(config, who)
    try:
        producer = client.create_producer(topic)
        for body, key, attrs in items:
            kwargs: dict[str, Any] = {}
            if key:
                kwargs["partition_key"] = key
            if attrs:
                kwargs["properties"] = attrs
            producer.send(body, **kwargs)
        producer.flush()
        producer.close()
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{who}: Pulsar did not take every message for '{topic}': {exc}") from None
    finally:
        client.close()
