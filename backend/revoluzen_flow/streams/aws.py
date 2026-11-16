"""Amazon Kinesis Data Streams and Amazon SQS (2026-11-13), through boto3.

Kinesis is a log. Flow keeps each shard's place (the sequence number after
which the next run reads) for the step, and saves it only after the whole run
has succeeded (``positions.py``). A first run starts at the oldest record the
stream keeps, or at the moment of the first run ("newest"). When the stream is
resharded, the new shards are read from their start, so no record is skipped;
records of a parent and its child shards are not put in one order.

SQS is a queue. A run takes up to Max Messages, which SQS then hides from other
readers for the step's Hold Time; after the whole run has succeeded Flow
deletes them, and after a failed, stopped or Sample run it makes them visible
again at once (``holds.py``). A run that outlasts the Hold Time lets SQS hand
those messages out again. FIFO queues keep their order per message group; the
destination sets the group from the Key Column.

Connection settings: region, access key ID and secret access key (or none, to
use the AWS role of the computer Flow runs on), optional session token and
endpoint (for LocalStack or a VPC endpoint).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import time
import uuid
from typing import Any

from revoluzen_flow.streams.messages import Received


def _first(config: dict, *names: str) -> str:
    for name in names:
        value = config.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def client(service: str, config: dict, who: str):
    try:
        import boto3  # type: ignore
    except ImportError:
        from revoluzen_flow.connections.addons import driver_message

        raise ValueError(f"{who}: {driver_message('AWS', 'boto3')}") from None
    kwargs: dict[str, Any] = {}
    region = _first(config, "region", "aws_region", "region_name")
    if region:
        kwargs["region_name"] = region
    key_id = _first(config, "access_key", "access_key_id", "aws_access_key_id")
    secret = _first(config, "secret_key", "secret_access_key", "aws_secret_access_key")
    if bool(key_id) != bool(secret):
        raise ValueError(f"{who}: the connection needs both the access key ID and the secret access key, or neither.")
    if key_id:
        kwargs.update(aws_access_key_id=key_id, aws_secret_access_key=secret)
        token = _first(config, "session_token", "aws_session_token")
        if token:
            kwargs["aws_session_token"] = token
    endpoint = _first(config, "endpoint_url", "endpoint")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    if not region and not endpoint:
        raise ValueError(f"{who}: add the AWS region to the connection, for example eu-west-1.")
    return boto3.client(service, **kwargs)


def _error(exc: Exception) -> str:
    response = getattr(exc, "response", None) or {}
    err = response.get("Error") or {}
    if err:
        return f"{err.get('Code', '')}: {err.get('Message', '')}".strip(": ")
    return str(exc)


def _code(exc: Exception) -> str:
    return str(((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code") or "")


# ── Kinesis ────────────────────────────────────────────────────────────────


def kinesis_read(kinesis: Any, stream: str, saved: dict, start_from: str, max_messages: int,
                 timeout_seconds: int, who: str) -> tuple[list[Received], dict]:
    """Records after the saved places, and the new places ``{"shards": {...}}``."""
    places = {k: dict(v) for k, v in (saved.get("shards") or {}).items()}
    first_run = not places
    try:
        shards, token = [], None
        while True:
            reply = kinesis.list_shards(NextToken=token) if token else kinesis.list_shards(StreamName=stream)
            shards.extend(reply.get("Shards") or [])
            token = reply.get("NextToken")
            if not token:
                break
    except Exception as exc:  # noqa: BLE001
        if _code(exc) == "ResourceNotFoundException":
            raise ValueError(f"{who}: the stream '{stream}' was not found.") from None
        raise ValueError(f"{who}: could not list the shards of '{stream}': {_error(exc)}") from None

    now = _dt.datetime.now(_dt.timezone.utc)
    out: list[Received] = []
    deadline = time.monotonic() + timeout_seconds
    for shard in sorted(shards, key=lambda s: s["ShardId"]):
        shard_id = shard["ShardId"]
        place = places.get(shard_id)
        if place is None:
            if first_run and start_from == "newest":
                place = {"at": now.isoformat()}
            else:
                place = {}
            places[shard_id] = place
        if len(out) >= max_messages or time.monotonic() > deadline:
            continue
        args: dict[str, Any] = {"StreamName": stream, "ShardId": shard_id}
        if place.get("after"):
            args.update(ShardIteratorType="AFTER_SEQUENCE_NUMBER", StartingSequenceNumber=place["after"])
        elif place.get("at"):
            args.update(ShardIteratorType="AT_TIMESTAMP", Timestamp=_dt.datetime.fromisoformat(place["at"]))
        else:
            args["ShardIteratorType"] = "TRIM_HORIZON"
        try:
            iterator = kinesis.get_shard_iterator(**args)["ShardIterator"]
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"{who}: could not start reading shard {shard_id} of '{stream}': {_error(exc)}") from None
        while iterator and len(out) < max_messages and time.monotonic() < deadline:
            try:
                reply = kinesis.get_records(ShardIterator=iterator, Limit=min(10000, max_messages - len(out)))
            except Exception as exc:  # noqa: BLE001
                if _code(exc) in ("ProvisionedThroughputExceededException", "LimitExceededException"):
                    time.sleep(1)
                    continue
                raise ValueError(f"{who}: reading shard {shard_id} of '{stream}' failed: {_error(exc)}") from None
            records = reply.get("Records") or []
            for record in records:
                out.append(Received(
                    body=record.get("Data"), source=stream, message_id=record.get("SequenceNumber"),
                    key=record.get("PartitionKey"), time=record.get("ApproximateArrivalTimestamp"),
                    attributes={"shard": shard_id},
                ))
                places[shard_id] = {"after": record["SequenceNumber"]}
                if len(out) >= max_messages:
                    break
            iterator = reply.get("NextShardIterator")
            if not records and not reply.get("MillisBehindLatest"):
                break  # caught up with this shard
    return out, {"shards": places}


def kinesis_write(kinesis: Any, stream: str, items: list[tuple[bytes, str | None]], who: str) -> None:
    """Put ``(data, partition key)`` records, 500 at a time, retrying the ones Kinesis turns down."""
    for start in range(0, len(items), 500):
        batch = [{"Data": data, "PartitionKey": key or uuid.uuid4().hex} for data, key in items[start:start + 500]]
        for attempt in range(4):
            try:
                reply = kinesis.put_records(StreamName=stream, Records=batch)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"{who}: could not write to the stream '{stream}': {_error(exc)}") from None
            failed = [(r, res) for r, res in zip(batch, reply.get("Records") or []) if res.get("ErrorCode")]
            if not failed:
                break
            if attempt == 3:
                code = failed[0][1]
                raise ValueError(
                    f"{who}: {len(failed)} of {len(batch)} records were not written to '{stream}': "
                    f"{code.get('ErrorCode')} {code.get('ErrorMessage', '')}".strip()
                )
            batch = [r for r, _res in failed]
            time.sleep(0.5 * (attempt + 1))


# ── SQS ────────────────────────────────────────────────────────────────────


def sqs_url(sqs: Any, queue: str, who: str) -> str:
    queue = str(queue or "").strip()
    if not queue:
        raise ValueError(f"{who}: name the queue (its name or URL).")
    if queue.startswith(("https://", "http://")):
        return queue
    try:
        return sqs.get_queue_url(QueueName=queue)["QueueUrl"]
    except Exception as exc:  # noqa: BLE001
        if "NonExistentQueue" in _code(exc) or "QueueDoesNotExist" in _code(exc):
            raise ValueError(f"{who}: the queue '{queue}' was not found.") from None
        raise ValueError(f"{who}: could not find the queue '{queue}': {_error(exc)}") from None


def sqs_receive(sqs: Any, url: str, max_messages: int, timeout_seconds: int, hold_seconds: int,
                who: str) -> tuple[list[Received], list[str]]:
    out: list[Received] = []
    handles: list[str] = []
    name = url.rstrip("/").rsplit("/", 1)[-1]
    deadline = time.monotonic() + timeout_seconds
    while len(out) < max_messages:
        wait = max(0, min(20, int(deadline - time.monotonic())))
        try:
            reply = sqs.receive_message(
                QueueUrl=url, MaxNumberOfMessages=min(10, max_messages - len(out)),
                WaitTimeSeconds=wait, VisibilityTimeout=hold_seconds,
                MessageAttributeNames=["All"], AttributeNames=["All"],
            )
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"{who}: could not read from the queue '{name}': {_error(exc)}") from None
        messages = reply.get("Messages") or []
        if not messages:
            break
        for m in messages:
            attrs = {k: v.get("StringValue", v.get("BinaryValue")) for k, v in (m.get("MessageAttributes") or {}).items()}
            system = m.get("Attributes") or {}
            sent = system.get("SentTimestamp")
            out.append(Received(
                body=m.get("Body"), source=name, message_id=m.get("MessageId"), key=system.get("MessageGroupId"),
                time=_dt.datetime.fromtimestamp(int(sent) / 1000, tz=_dt.timezone.utc) if sent else None,
                attributes=attrs,
            ))
            handles.append(m["ReceiptHandle"])
        if time.monotonic() >= deadline:
            break
    return out, handles


def sqs_confirm(sqs: Any, url: str, handles: list[str]) -> None:
    for start in range(0, len(handles), 10):
        chunk = handles[start:start + 10]
        reply = sqs.delete_message_batch(
            QueueUrl=url, Entries=[{"Id": str(i), "ReceiptHandle": h} for i, h in enumerate(chunk)])
        if reply.get("Failed"):
            raise RuntimeError(f"SQS did not delete {len(reply['Failed'])} message(s): {reply['Failed'][0].get('Message')}")


def sqs_give_back(sqs: Any, url: str, handles: list[str]) -> None:
    for start in range(0, len(handles), 10):
        chunk = handles[start:start + 10]
        sqs.change_message_visibility_batch(
            QueueUrl=url, Entries=[{"Id": str(i), "ReceiptHandle": h, "VisibilityTimeout": 0}
                                   for i, h in enumerate(chunk)])


def sqs_send(sqs: Any, url: str, items: list[tuple[bytes, str | None, dict]], who: str) -> None:
    fifo = url.endswith(".fifo")
    for start in range(0, len(items), 10):
        entries = []
        for i, (body, key, attrs) in enumerate(items[start:start + 10]):
            entry: dict[str, Any] = {"Id": str(i), "MessageBody": body.decode("utf-8")}
            if attrs:
                entry["MessageAttributes"] = {k: {"DataType": "String", "StringValue": v} for k, v in attrs.items()}
            if fifo:
                entry["MessageGroupId"] = key or "flow"
                entry["MessageDeduplicationId"] = hashlib.sha256(body).hexdigest()
            entries.append(entry)
        try:
            reply = sqs.send_message_batch(QueueUrl=url, Entries=entries)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"{who}: could not send to the queue: {_error(exc)}") from None
        if reply.get("Failed"):
            first = reply["Failed"][0]
            raise ValueError(
                f"{who}: {len(reply['Failed'])} of {len(entries)} messages were not sent: "
                f"{first.get('Code')} {first.get('Message', '')}".strip()
            )
