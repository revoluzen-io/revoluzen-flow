"""Google Cloud Pub/Sub over its HTTPS API (2026-11-13).

No Google client library is needed: Flow signs in with the connection's
service-account key (a signed token, RS256, made with ``cryptography``, which
ships with Flow) and calls Pub/Sub's REST API.

A Pub/Sub source reads a subscription. Pub/Sub hands each message to one
reader and redelivers it unless it is acknowledged before its deadline. Flow
pulls up to Max Messages, extends their deadline to the step's Hold Time (at
most 600 seconds, Pub/Sub's limit), acknowledges them only after the whole run
has succeeded, and after a failed, stopped or Sample run sets their deadline to
zero so Pub/Sub delivers them again at once (``holds.py``).

A Pub/Sub destination publishes to a topic, 1,000 messages per request, with
the attributes column's fields as message attributes.

The Pub/Sub emulator works too: put its address (http://localhost:8085) in
the connection's API endpoint; it needs no key.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from revoluzen_flow.streams.messages import Received

SCOPE = "https://www.googleapis.com/auth/pubsub"
DEFAULT_ENDPOINT = "https://pubsub.googleapis.com"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class PubSub:
    """A signed-in Pub/Sub project."""

    def __init__(self, config: dict, who: str, opener=None) -> None:
        self.who = who
        self.endpoint = str(config.get("api_endpoint") or DEFAULT_ENDPOINT).rstrip("/")
        self.emulator = self.endpoint != DEFAULT_ENDPOINT and not config.get("credentials_json")
        key = config.get("credentials_json") or config.get("service_account_json") or ""
        self.key = None
        if not self.emulator:
            try:
                self.key = key if isinstance(key, dict) else json.loads(key)
                self.key["client_email"], self.key["private_key"]
            except (TypeError, ValueError, KeyError):
                raise ValueError(
                    f"{who}: the connection needs a service account key (the JSON file Google Cloud "
                    f"gives when you create a key for a service account)."
                ) from None
        self.project = str(config.get("project_id") or (self.key or {}).get("project_id") or "").strip()
        if not self.project:
            raise ValueError(f"{who}: add the Google Cloud project ID to the connection.")
        self._open = opener or urllib.request.urlopen
        self._token = ""
        self._token_ends = 0.0

    # ── Sign-in ─────────────────────────────────────────────────────

    def token(self) -> str:
        if self.emulator:
            return ""
        if self._token and time.time() < self._token_ends - 60:
            return self._token
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        now = int(time.time())
        token_uri = self.key.get("token_uri") or "https://oauth2.googleapis.com/token"
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = _b64url(json.dumps({"iss": self.key["client_email"], "scope": SCOPE, "aud": token_uri,
                                     "iat": now, "exp": now + 3600}).encode())
        try:
            private = serialization.load_pem_private_key(self.key["private_key"].encode(), None)
        except (ValueError, TypeError):
            raise ValueError(f"{self.who}: the service account key's private_key could not be read.") from None
        signature = private.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
        assertion = f"{header}.{claims}.{_b64url(signature)}"
        body = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                       "assertion": assertion}).encode()
        reply = self._request("POST", token_uri, body, {"Content-Type": "application/x-www-form-urlencoded"},
                              signing_in=True)
        self._token = reply["access_token"]
        self._token_ends = time.time() + int(reply.get("expires_in", 3600))
        return self._token

    # ── HTTP ────────────────────────────────────────────────────────

    def _request(self, method: str, url: str, body: bytes | None, headers: dict, timeout: float = 30,
                 signing_in: bool = False) -> Any:
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self._open(request, timeout=timeout) as reply:
                text = reply.read().decode("utf-8")
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
                message = (detail.get("error") or {}).get("message") or detail.get("error_description") or ""
            except Exception:  # noqa: BLE001
                message = ""
            if signing_in:
                raise ValueError(f"{self.who}: Google refused the service account key: {message or exc.code}.") from None
            if exc.code == 404:
                raise ValueError(f"{self.who}: not found: {message or url.rsplit('/', 1)[-1]}.") from None
            if exc.code == 403:
                raise ValueError(
                    f"{self.who}: the service account may not do this ({message}). Give it the Pub/Sub "
                    f"Subscriber role to read, or Publisher to write."
                ) from None
            raise ValueError(f"{self.who}: Pub/Sub answered HTTP {exc.code}: {message}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise ValueError(f"{self.who}: could not reach Pub/Sub: {getattr(exc, 'reason', exc)}") from None

    def call(self, path: str, payload: dict | None = None, method: str = "POST", timeout: float = 30) -> Any:
        headers = {"Content-Type": "application/json"}
        token = self.token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = json.dumps(payload).encode() if payload is not None else None
        return self._request(method, f"{self.endpoint}/v1/{path}", body, headers, timeout)

    def name(self, kind: str, value: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError(f"{self.who}: name the {kind[:-1]}.")
        return value if value.startswith("projects/") else f"projects/{self.project}/{kind}/{value}"

    # ── Reading ─────────────────────────────────────────────────────

    def pull(self, subscription: str, max_messages: int, timeout_seconds: int,
             hold_seconds: int) -> tuple[list[Received], list[str]]:
        path = self.name("subscriptions", subscription)
        out: list[Received] = []
        ack_ids: list[str] = []
        deadline = time.monotonic() + timeout_seconds
        while len(out) < max_messages and time.monotonic() < deadline:
            wait = max(1.0, deadline - time.monotonic())
            try:
                reply = self.call(f"{path}:pull", {"maxMessages": min(1000, max_messages - len(out))},
                                  timeout=wait)
            except ValueError as exc:
                if "timed out" in str(exc).lower():
                    break
                raise
            received = reply.get("receivedMessages") or []
            if not received:
                break
            batch = []
            for item in received:
                msg = item.get("message") or {}
                stamp = msg.get("publishTime")
                out.append(Received(
                    body=base64.b64decode(msg.get("data") or ""), source=subscription,
                    message_id=msg.get("messageId"), key=msg.get("orderingKey") or None,
                    time=_dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None,
                    attributes=dict(msg.get("attributes") or {}),
                ))
                batch.append(item["ackId"])
            ack_ids.extend(batch)
            self.call(f"{path}:modifyAckDeadline", {"ackIds": batch, "ackDeadlineSeconds": hold_seconds})
        return out, ack_ids

    def acknowledge(self, subscription: str, ack_ids: list[str]) -> None:
        path = self.name("subscriptions", subscription)
        for start in range(0, len(ack_ids), 1000):
            self.call(f"{path}:acknowledge", {"ackIds": ack_ids[start:start + 1000]})

    def give_back(self, subscription: str, ack_ids: list[str]) -> None:
        path = self.name("subscriptions", subscription)
        for start in range(0, len(ack_ids), 1000):
            self.call(f"{path}:modifyAckDeadline", {"ackIds": ack_ids[start:start + 1000], "ackDeadlineSeconds": 0})

    # ── Writing ─────────────────────────────────────────────────────

    def publish(self, topic: str, items: list[tuple[bytes, str | None, dict]]) -> None:
        path = self.name("topics", topic)
        for start in range(0, len(items), 1000):
            messages = []
            for body, key, attrs in items[start:start + 1000]:
                message: dict[str, Any] = {"data": base64.b64encode(body).decode("ascii")}
                attributes = dict(attrs)
                if key:
                    attributes.setdefault("key", key)
                if attributes:
                    message["attributes"] = attributes
                messages.append(message)
            reply = self.call(f"{path}:publish", {"messages": messages})
            if len(reply.get("messageIds") or []) != len(messages):
                raise ValueError(f"{self.who}: Pub/Sub accepted {len(reply.get('messageIds') or [])} of "
                                 f"{len(messages)} messages.")

    def check(self, subscription: str = "", topic: str = "") -> str:
        if subscription:
            self.call(self.name("subscriptions", subscription), method="GET")
            return f"subscription {subscription}"
        if topic:
            self.call(self.name("topics", topic), method="GET")
            return f"topic {topic}"
        self.call(f"projects/{self.project}/topics?pageSize=1", method="GET")
        return f"project {self.project}"
