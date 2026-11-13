"""Where a Kafka client connects, how it signs in and which certificates it uses.

A saved Kafka connection holds the broker list, the sign-in and, for SSL, the
certificates (Connections page): ``brokers``, ``security_protocol``,
``sasl_mechanism``, ``username``, ``password``, ``ssl_ca_pem``,
``ssl_certificate_pem``, ``ssl_key_pem`` and ``ssl_key_password``. A Kafka step
with no saved connection can hold the brokers and the sign-in itself:
``bootstrap_servers``, ``security_protocol``, ``sasl_mechanism``,
``sasl_username`` and ``sasl_password``. Until November 2026 only the broker
list reached a Kafka client, so the Kafka Source, the Kafka Sink, Test
connection and topic browsing could not use a cluster that asks for a sign-in
(fixed 2026-11-11), nor one whose certificates a company signs itself or that
asks for a client certificate (fixed 2026-11-12).

``kafka_settings()`` reads them, from the connection when the step has one, and
checks them. ``KafkaSettings`` then gives each client library the names it
takes:

====================  =====================  ===================
setting               confluent-kafka        kafka-python
====================  =====================  ===================
brokers               bootstrap.servers      bootstrap_servers
security protocol     security.protocol      security_protocol
SASL mechanism        sasl.mechanism         sasl_mechanism
username              sasl.username          sasl_plain_username
password              sasl.password          sasl_plain_password
CA certificate        ssl.ca.pem             ssl_context
client certificate    ssl.certificate.pem    ssl_context
client private key    ssl.key.pem            ssl_context
private key password  ssl.key.password       ssl_context
====================  =====================  ===================

* The SASL settings reach the client only with ``SASL_PLAINTEXT`` or
  ``SASL_SSL``. With ``PLAINTEXT`` or ``SSL`` a username and password are not
  used, as before, so a connection that worked keeps working.
* ``PLAIN``, ``SCRAM-SHA-256`` and ``SCRAM-SHA-512`` are the mechanisms the
  forms offer, and the ones both libraries take with a username and password.
* The certificates reach the client only with ``SSL`` or ``SASL_SSL``, and only
  from a saved connection. They are pasted as PEM text (the founders' choice,
  2026-11-12), so there is no file to copy to the Flow server. With no CA
  certificate, the broker's certificate is checked against the certificates the
  computer trusts, as before. A client certificate goes with its private key and,
  if the key is locked, the key's password. ``kafka_settings()`` reads all of
  them before any client is made, so a certificate that cannot be read, a wrong
  key password or a key that does not belong to the certificate stops at once
  with a plain message.
* kafka-python takes certificates only as files or as a ready ``SSLContext``.
  ``ssl_context()`` builds that context; see it for how the private key is
  loaded without being written to disk in clear.
* With a saved connection, the connection decides how to sign in: a step's own
  sign-in fields are not used. The step's broker list is used only when the
  connection has none.
* The password, the private key and the key's password never go into a message
  or a log line. ``hide()`` takes them out of any text a Kafka client hands
  back, and ``repr()`` leaves them out. The connection keeps the key and its
  password encrypted and never sends them back to the browser
  (``inline_secrets.py``).
"""

from __future__ import annotations

import os
import re
import secrets
import ssl
import tempfile
from dataclasses import dataclass, field
from typing import Any, Mapping

DEFAULT_BROKERS = "localhost:9092"
PROTOCOLS = ("PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL")
SIGN_IN_PROTOCOLS = ("SASL_PLAINTEXT", "SASL_SSL")
TLS_PROTOCOLS = ("SSL", "SASL_SSL")
MECHANISMS = ("PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512")
# What a password becomes in a message; the same eight dots Flow shows for any
# stored secret.
HIDDEN = "\N{BULLET}" * 8

# A PEM private key of any kind: PKCS#8 (also locked), PKCS#1 RSA, SEC1 EC.
_PRIVATE_KEY = re.compile(r"-----BEGIN ([A-Z0-9 ]*?)PRIVATE KEY-----[\s\S]*?-----END \1PRIVATE KEY-----")
# A key that needs a password: locked PKCS#8, or an older OpenSSL key with a
# "Proc-Type: 4,ENCRYPTED" header.
_LOCKED_KEY = re.compile(r"-----BEGIN ENCRYPTED PRIVATE KEY-----|^Proc-Type:\s*4,\s*ENCRYPTED", re.MULTILINE)
_WHOLE_CERTIFICATE = "from -----BEGIN CERTIFICATE----- to -----END CERTIFICATE-----"


class KafkaSettingsError(ValueError):
    """A broker or sign-in setting that cannot work, said so a person can fix it."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def broker_list(value: Any) -> str:
    """``"a:9092, b:9092"`` or ``["a:9092", "b:9092"]`` as ``"a:9092,b:9092"``."""
    if isinstance(value, (list, tuple)):
        value = ",".join(str(item) for item in value)
    return ",".join(part.strip() for part in _text(value).split(",") if part.strip())


@dataclass(frozen=True)
class KafkaSettings:
    """The brokers, the sign-in and the certificates, ready for either client library."""

    brokers: str
    security_protocol: str = "PLAINTEXT"
    sasl_mechanism: str = ""
    username: str = ""
    password: str = field(default="", repr=False)
    # PEM text, checked by kafka_settings(): the CA certificate(s) to trust, the
    # client certificate (and any certificates that came with it), its private
    # key, and the key's password when the key is locked.
    ca_pem: str = field(default="", repr=False)
    certificate_pem: str = field(default="", repr=False)
    key_pem: str = field(default="", repr=False)
    key_password: str = field(default="", repr=False)

    @property
    def signs_in(self) -> bool:
        """Whether the client signs in with a username and password."""
        return self.security_protocol in SIGN_IN_PROTOCOLS

    @property
    def uses_tls(self) -> bool:
        """Whether the connection to the brokers is encrypted (SSL or SASL_SSL)."""
        return self.security_protocol in TLS_PROTOCOLS

    def confluent(self) -> dict[str, Any]:
        """Settings for confluent-kafka (``Consumer``, ``Producer``, ``AdminClient``)."""
        conf: dict[str, Any] = {
            "bootstrap.servers": self.brokers,
            "security.protocol": self.security_protocol,
        }
        if self.signs_in:
            conf.update({
                "sasl.mechanism": self.sasl_mechanism,
                "sasl.username": self.username,
                "sasl.password": self.password,
            })
        if self.uses_tls:
            if self.ca_pem:
                conf["ssl.ca.pem"] = self.ca_pem
            if self.certificate_pem:
                conf["ssl.certificate.pem"] = self.certificate_pem
                conf["ssl.key.pem"] = self.key_pem
                if self.key_password:
                    conf["ssl.key.password"] = self.key_password
        return conf

    def kafka_python(self) -> dict[str, Any]:
        """Keyword arguments for kafka-python (``KafkaConsumer``, ``KafkaProducer``, ``KafkaAdminClient``)."""
        conf: dict[str, Any] = {
            "bootstrap_servers": self.brokers.split(","),
            "security_protocol": self.security_protocol,
        }
        if self.signs_in:
            conf.update({
                "sasl_mechanism": self.sasl_mechanism,
                "sasl_plain_username": self.username,
                "sasl_plain_password": self.password,
            })
        if self.uses_tls and (self.ca_pem or self.certificate_pem):
            # Without certificates kafka-python makes its own context, as before.
            conf["ssl_context"] = self.ssl_context()
        return conf

    def ssl_context(self) -> ssl.SSLContext:
        """The certificates as kafka-python takes them when they are not files.

        The context trusts ``ca_pem`` or, with none, the certificates the
        computer trusts, and checks the broker's name against its certificate,
        as kafka-python's own context does. Python's ``ssl`` module reads a
        client certificate and its key only from a file, so they go through a
        temporary file that is deleted as soon as they are loaded. The key in
        that file is locked with a one-time password that only this process
        holds: the clear key is never written to disk, and a file left behind
        by a crash is of no use to anyone.
        """
        try:
            context = ssl.create_default_context(cadata=self.ca_pem or None)
            if self.certificate_pem:
                _load_client_certificate(context, self.certificate_pem, self.key_pem, self.key_password)
        except Exception as exc:  # noqa: BLE001 — ssl and cryptography raise several types
            # kafka_settings() has read every certificate already, so this is
            # the computer (no temporary folder, say), not what was pasted.
            raise KafkaSettingsError(
                f"Kafka: the certificates could not be prepared for kafka-python: {self.hide(exc)}"
            ) from None
        return context

    def hide(self, text: Any) -> str:
        """``text`` with the password, the private key and the key's password
        taken out, for messages and logs."""
        text = str(text)
        for secret in self._secrets():
            text = text.replace(secret, HIDDEN)
        return text

    def _secrets(self) -> list[str]:
        found = {self.password, self.key_password, self.key_pem.strip()}
        # A client that repeats part of the key repeats whole lines of it.
        found.update(
            line.strip() for line in self.key_pem.splitlines()
            if len(line.strip()) >= 16 and not line.startswith("-----")
        )
        # Longest first, so the whole key goes before any line of it.
        return sorted((s for s in found if s), key=len, reverse=True)


def _load_client_certificate(context: ssl.SSLContext, certificate_pem: str, key_pem: str, key_password: str) -> None:
    """Load the client certificate and its key into ``context``; see ``KafkaSettings.ssl_context``."""
    from cryptography.hazmat.primitives.serialization import (
        BestAvailableEncryption, Encoding, PrivateFormat, load_pem_private_key,
    )

    password = key_password.encode("utf-8") if key_password and _LOCKED_KEY.search(key_pem) else None
    key = load_pem_private_key(key_pem.encode("ascii"), password)
    one_time = secrets.token_hex(32).encode("ascii")
    locked = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, BestAvailableEncryption(one_time))
    handle, path = tempfile.mkstemp(prefix="flow-kafka-", suffix=".pem")
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(certificate_pem.encode("ascii") + locked)
        context.load_cert_chain(path, password=one_time)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def error_text(error: Any) -> str:
    """The readable part of a Kafka client error.

    confluent-kafka raises a ``KafkaException`` around a ``KafkaError``, whose
    text is ``.str()``; kafka-python raises ordinary exceptions.
    """
    inner = error.args[0] if isinstance(error, Exception) and error.args else error
    text = getattr(inner, "str", None)
    return str(text() if callable(text) else inner)


class ClientErrors:
    """The last error a confluent-kafka client reported in the background.

    librdkafka reports a refused sign-in or a broker it cannot reach to the
    ``error_cb`` callback, while the call that waits for the broker often only
    says it timed out. Pass an instance as ``error_cb``; ``explain()`` adds what
    it heard to the message a person sees.
    """

    def __init__(self, settings: KafkaSettings) -> None:
        self._settings = settings
        self.last = ""

    def __call__(self, error: Any) -> None:
        self.last = self._settings.hide(error_text(error))

    def explain(self, text: Any) -> str:
        """``text`` without the password, followed by the last background error."""
        text = self._settings.hide(text)
        if self.last and self.last not in text:
            return f"{text} (Kafka said: {self.last})"
        return text


def kafka_settings(
    connection: Mapping[str, Any] | None,
    step: Mapping[str, Any] | None = None,
    *,
    who: str = "Kafka",
) -> KafkaSettings:
    """The brokers, sign-in and certificates to use.

    ``connection`` is the saved connection's settings, or ``None`` when the
    step has no connection; then the step's own fields are read, and there are
    no certificates (only a connection holds them). ``who`` starts every error
    message ("Kafka Source", "Kafka Sink").
    """
    step = step or {}
    if connection is not None:
        where = "the Kafka connection"
        brokers = (
            connection.get("brokers") or connection.get("bootstrap_servers")
            or connection.get("host") or step.get("bootstrap_servers")
        )
        protocol = connection.get("security_protocol")
        mechanism = connection.get("sasl_mechanism")
        username = connection.get("username") or connection.get("sasl_username")
        password = connection.get("password") or connection.get("sasl_password")
    else:
        where = "the step"
        brokers = step.get("bootstrap_servers") or step.get("brokers")
        protocol = step.get("security_protocol")
        mechanism = step.get("sasl_mechanism")
        username = step.get("sasl_username")
        password = step.get("sasl_password")

    protocol = _text(protocol).upper().replace("-", "_") or "PLAINTEXT"
    if protocol not in PROTOCOLS:
        raise KafkaSettingsError(
            f"{who}: the security protocol '{protocol}' is not one Kafka offers. "
            "Use PLAINTEXT, SSL, SASL_PLAINTEXT or SASL_SSL."
        )
    sign_in: dict[str, str] = {}
    if protocol in SIGN_IN_PROTOCOLS:
        mechanism = _text(mechanism).upper().replace("_", "-")
        if not mechanism:
            raise KafkaSettingsError(
                f"{who}: {protocol} signs in with a username and password, so it needs a SASL "
                f"mechanism. Choose PLAIN, SCRAM-SHA-256 or SCRAM-SHA-512 in {where}, the one "
                "your Kafka cluster uses."
            )
        if mechanism not in MECHANISMS:
            raise KafkaSettingsError(
                f"{who}: the SASL mechanism '{mechanism}' is not supported. Flow signs in to "
                "Kafka with PLAIN, SCRAM-SHA-256 or SCRAM-SHA-512."
            )
        username, password = _text(username), "" if password is None else str(password)
        missing = [name for name, value in (("a username", username), ("a password", password)) if not value]
        if missing:
            raise KafkaSettingsError(
                f"{who}: signing in with {mechanism} needs a username and a password. "
                f"Add {' and '.join(missing)} to {where}."
            )
        sign_in = {"sasl_mechanism": mechanism, "username": username, "password": password}
    # Certificates matter only on an encrypted connection; on any other they
    # are left alone, as a username and password are without SASL.
    certificates = (
        _certificates(connection, who=who, where=where)
        if connection is not None and protocol in TLS_PROTOCOLS else {}
    )
    return KafkaSettings(
        brokers=broker_list(brokers) or DEFAULT_BROKERS,
        security_protocol=protocol,
        **sign_in,
        **certificates,
    )


# ── Certificates (SSL and SASL_SSL) ──────────────────────────────────────


def _pem_text(value: Any) -> str:
    """Pasted PEM text, with Windows line ends made plain and blank edges trimmed."""
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _read_certificates(text: str, what: str, who: str, where: str) -> list[Any]:
    """Every certificate in ``text``, or a plain message when there is none."""
    from cryptography import x509

    try:
        return x509.load_pem_x509_certificates(text.encode("utf-8"))
    except ValueError:
        raise KafkaSettingsError(
            f"{who}: the {what} in {where} could not be read. Paste the whole certificate, "
            f"{_WHOLE_CERTIFICATE}."
        ) from None


def _as_pem(certificates: list[Any]) -> str:
    """Certificates as clean PEM text: no notes around them, one line end style."""
    from cryptography.hazmat.primitives.serialization import Encoding

    return "".join(certificate.public_bytes(Encoding.PEM).decode("ascii") for certificate in certificates)


def _read_private_key(text: str, password: str, who: str, where: str) -> tuple[str, str, Any]:
    """The key's PEM block, the password it needs ("" when the key is not
    locked) and the key itself, or a plain message."""
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    found = _PRIVATE_KEY.search(text)
    block = found.group(0) if found else ""
    locked = bool(block and _LOCKED_KEY.search(block))
    if locked and not password:
        raise KafkaSettingsError(
            f"{who}: the client private key in {where} is locked with a password. "
            f"Add the private key password to {where}."
        )
    try:
        if not block:
            raise ValueError("no private key")
        key = load_pem_private_key(block.encode("ascii"), password.encode("utf-8") if locked else None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        if locked:
            raise KafkaSettingsError(
                f"{who}: the private key password in {where} does not unlock the client private "
                "key. Check the password, or paste the key again."
            ) from None
        raise KafkaSettingsError(
            f"{who}: the client private key in {where} could not be read. Paste the whole key, "
            "from the line that starts -----BEGIN to the line that starts -----END."
        ) from None
    # A password typed for a key that has none is not needed, and not sent.
    return block + "\n", password if locked else "", key


def _belongs_to(key: Any, certificate: Any) -> bool:
    """Whether ``key`` is the private half of ``certificate``'s public key."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    def public(value: Any) -> bytes:
        return value.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)

    return public(key.public_key()) == public(certificate.public_key())


def _certificates(connection: Mapping[str, Any], *, who: str, where: str) -> dict[str, str]:
    """A connection's CA certificate, client certificate and private key, read
    and checked, as ``KafkaSettings`` fields."""
    ca_text = _pem_text(connection.get("ssl_ca_pem"))
    certificate_text = _pem_text(connection.get("ssl_certificate_pem"))
    key_text = _pem_text(connection.get("ssl_key_pem"))
    key_password = connection.get("ssl_key_password")
    key_password = "" if key_password is None else str(key_password)

    found: dict[str, str] = {}
    if ca_text:
        if _PRIVATE_KEY.search(ca_text):
            raise KafkaSettingsError(
                f"{who}: the CA certificate in {where} holds a private key too. Flow needs only "
                f"the certificate: paste just the part {_WHOLE_CERTIFICATE}."
            )
        found["ca_pem"] = _as_pem(_read_certificates(ca_text, "CA certificate", who, where))
    if certificate_text:
        certificates = _read_certificates(certificate_text, "client certificate", who, where)
        # A file that holds the certificate and its key together can be pasted
        # whole into the certificate box (the box is then kept secret too).
        if not key_text and _PRIVATE_KEY.search(certificate_text):
            key_text = certificate_text
        if not key_text:
            raise KafkaSettingsError(
                f"{who}: the client certificate in {where} needs its private key. "
                f"Add the client private key to {where}."
            )
        key_pem, key_password, key = _read_private_key(key_text, key_password, who, where)
        # The certificate the key belongs to goes first; any others (the
        # certificates that signed it) follow, as both libraries expect.
        own = next((c for c in certificates if _belongs_to(key, c)), None)
        if own is None:
            raise KafkaSettingsError(
                f"{who}: the client private key in {where} does not belong to the client "
                "certificate. Paste the key that was made with this certificate."
            )
        certificates = [own] + [c for c in certificates if c is not own]
        found.update(certificate_pem=_as_pem(certificates), key_pem=key_pem, key_password=key_password)
    elif key_text:
        raise KafkaSettingsError(
            f"{who}: {where} has a client private key but no client certificate. Add the "
            "certificate the key belongs to, or remove the key."
        )
    return found
