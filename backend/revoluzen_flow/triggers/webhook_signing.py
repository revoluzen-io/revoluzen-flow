"""Signed calls to a published endpoint.

An endpoint can require each call to be signed with a secret shared between
Flow and the sender, instead of (or as well as) carrying an API key. A signed
call proves who sent it and that nothing in it was changed, and a recorded call
cannot be sent again.

The format is the open Standard Webhooks one, so a sender can use any of the
libraries written for it:

* ``webhook-id``: a unique id for this message, chosen by the sender.
* ``webhook-timestamp``: when it was sent, in whole seconds since 1970 (UTC).
* ``webhook-signature``: ``v1,`` then the base64 HMAC-SHA256 of
  ``<id>.<timestamp>.<body>``, keyed with the secret's bytes (the part after
  ``whsec_``, base64-decoded). Several space-separated signatures may be sent;
  one that matches is enough.

Flow refuses a call whose timestamp is more than the endpoint's window (five
minutes unless set) from its own clock, and a message id it has already
accepted inside that window, so a captured call is useless soon after.

The secret is kept encrypted with the same key as connection passwords, shown
once when it is made, and never returned again.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Mapping

SECRET_PREFIX = "whsec_"
#: How far a call's time may be from the server's, either way.
DEFAULT_TOLERANCE_SECONDS = 300
MIN_TOLERANCE_SECONDS = 30
MAX_TOLERANCE_SECONDS = 3600
#: Longest message id accepted; the id is stored to refuse repeats.
MAX_MESSAGE_ID_LENGTH = 200

ID_HEADER = "webhook-id"
TIMESTAMP_HEADER = "webhook-timestamp"
SIGNATURE_HEADER = "webhook-signature"


class SignatureError(Exception):
    """A call that may not start the pipeline. ``status`` is the HTTP answer."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SignedCall:
    message_id: str
    timestamp: int


def new_secret() -> str:
    """A fresh secret: 32 random bytes, base64, after the ``whsec_`` prefix."""
    return SECRET_PREFIX + base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def secret_key(secret: str) -> bytes:
    """The HMAC key a secret stands for."""
    text = (secret or "").strip()
    if text.startswith(SECRET_PREFIX):
        text = text[len(SECRET_PREFIX):]
    try:
        key = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("This is not a signing secret Flow made.") from exc
    if len(key) < 16:
        raise ValueError("This signing secret is too short.")
    return key


def sign(secret: str, message_id: str, timestamp: int | str, body: bytes) -> str:
    """The ``webhook-signature`` value for one message."""
    to_sign = f"{message_id}.{timestamp}.".encode("utf-8") + (body or b"")
    digest = hmac.new(secret_key(secret), to_sign, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def _header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name)
    if value is None:
        value = headers.get(name.title())
    return (value or "").strip()


def verify(
    secret: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> SignedCall:
    """Check one call. Returns its id and time, or raises :class:`SignatureError`.

    Whether the id was seen before is the caller's to check, after this
    passes: only a call with a good signature may use up an id.
    """
    message_id = _header(headers, ID_HEADER)
    stamp = _header(headers, TIMESTAMP_HEADER)
    offered = _header(headers, SIGNATURE_HEADER)
    if not (message_id and stamp and offered):
        missing = [n for n, v in ((ID_HEADER, message_id), (TIMESTAMP_HEADER, stamp), (SIGNATURE_HEADER, offered)) if not v]
        raise SignatureError(
            f"This endpoint needs signed calls. Missing header: {', '.join(missing)}."
        )
    if len(message_id) > MAX_MESSAGE_ID_LENGTH or not message_id.isprintable():
        raise SignatureError(
            f"The {ID_HEADER} header must be printable text of at most {MAX_MESSAGE_ID_LENGTH} characters."
        )
    try:
        timestamp = int(stamp)
    except ValueError:
        raise SignatureError(f"The {TIMESTAMP_HEADER} header must be whole seconds since 1970 (UTC).") from None
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        raise SignatureError(
            f"The {TIMESTAMP_HEADER} is more than {tolerance_seconds} seconds from this server's clock, "
            "so the call was refused. Sign each call when you send it."
        )
    try:
        expected = sign(secret, message_id, timestamp, body)
    except ValueError:
        raise SignatureError("This endpoint's signing secret cannot be read. Make a new one.", status=503) from None
    for candidate in offered.split():
        if hmac.compare_digest(candidate.strip().encode("utf-8"), expected.encode("utf-8")):
            return SignedCall(message_id=message_id, timestamp=timestamp)
    raise SignatureError("The signature does not match. Check the secret, and sign the body exactly as it is sent.")
