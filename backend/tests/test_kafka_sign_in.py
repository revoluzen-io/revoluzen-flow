"""Kafka sign-in reaches every Kafka client Flow makes (2026-11-11).

A Kafka connection saves how to sign in: a security protocol, a SASL mechanism,
a username and a password. The Kafka step forms have the same fields for a step
with no saved connection. None of it reached a Kafka client, so the Kafka
Source, the Kafka Sink, Test connection and topic browsing all connected without
signing in, and a cluster that asks for a sign-in could not be used. The Kafka
Sink also read only the older ``bootstrap_servers`` key, so with a saved
connection it wrote to localhost:9092, and it finished as if every message had
gone even when Kafka took none.

The stand-in cluster here turns away any client that does not sign in the way
it expects, as a real broker would. There is no broker and no network. The
stand-in clients are the ones in test_kafka_source_positions.py, with a
producer and an admin client added.

2026-11-12: a Kafka connection can also trust a company's own certificate
authority (CA) and show the brokers a client certificate, pasted as PEM text.
The stand-in broker can present a certificate that CA signed and ask for a
client certificate; each client then goes through a real TLS handshake with
it, in memory, through Python's ssl module. The confluent-kafka stand-in builds
its side of the handshake from the ``ssl.*.pem`` settings the way librdkafka
reads them, the kafka-python one uses the ``ssl_context`` it is given.
"""
from __future__ import annotations

import csv
import datetime
import json
import logging
import ssl
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from revoluzen_flow.connections import addons
from revoluzen_flow.connections import kafka_settings as kafka_settings_module
from revoluzen_flow.connections.catalog import get_catalog
from revoluzen_flow.connections.inline_secrets import SECRET_PLACEHOLDER, connection_response
from revoluzen_flow.connections.kafka_settings import HIDDEN, KafkaSettingsError, kafka_settings
from revoluzen_flow.connections.models import Connection
from revoluzen_flow.connections.tester import ConnectionTester
from revoluzen_flow.engine import sync_state_store as position_store
from revoluzen_flow.engine.executor import WorkflowExecutor
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow
from revoluzen_flow.security.encryptor import fresh_encryptor_for_tests
from tests.test_kafka_source_positions import FakeKafka, fake_confluent_kafka, fake_kafka_python

USER = "flow-reader"
PASSWORD = "Tr0ub4dor&3-kafka"
TOPIC = "orders"
BROKERS = "broker-1:9093,broker-2:9093"
LIBRARIES = ("confluent-kafka", "kafka-python")
SIGN_INS = [
    (protocol, mechanism)
    for protocol in ("SASL_PLAINTEXT", "SASL_SSL")
    for mechanism in ("PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512")
]
KEY_PASSWORD = "Key-pa55word-for-flow"


# ── Certificates: a company CA, its broker and a client ────────────────────

def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _certificate(common_name: str, signer=None, *, ca: bool = False, hosts=()):
    """A key and a certificate. A CA signs itself; anything else is signed by
    ``signer`` (its key and certificate) and is a broker when it has ``hosts``."""
    key = ec.generate_private_key(ec.SECP256R1())
    signer_key, signer_certificate = signer or (key, None)
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(signer_certificate.subject if signer_certificate else _name(common_name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        # Python 3.13 verifies strictly (VERIFY_X509_STRICT) and refuses a
        # chain whose certificates lack key identifiers, as a real CA's have.
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signer_key.public_key()), critical=False,
        )
    )
    if ca:
        builder = builder.add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False,
        ), critical=True)
    else:
        use = ExtendedKeyUsageOID.SERVER_AUTH if hosts else ExtendedKeyUsageOID.CLIENT_AUTH
        builder = builder.add_extension(x509.ExtendedKeyUsage([use]), critical=False)
    if hosts:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(host) for host in hosts]), critical=False,
        )
    return key, builder.sign(signer_key, hashes.SHA256())


def _pem(certificate) -> str:
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _key_pem(key, password: str = "", *, older_format: bool = False) -> str:
    """A private key as PEM: PKCS#8, or the older OpenSSL format with a
    "Proc-Type: 4,ENCRYPTED" header when it is locked."""
    form = serialization.PrivateFormat.TraditionalOpenSSL if older_format else serialization.PrivateFormat.PKCS8
    lock = serialization.BestAvailableEncryption(password.encode()) if password else serialization.NoEncryption()
    return key.private_bytes(serialization.Encoding.PEM, form, lock).decode("ascii")


@pytest.fixture(scope="module")
def pki() -> SimpleNamespace:
    """A company CA that signed the brokers' certificate and Flow's client
    certificate, and another CA that signed neither."""
    company = _certificate("Company Kafka CA", ca=True)
    other = _certificate("Some Other CA", ca=True)
    broker = _certificate("broker-1", company, hosts=("broker-1", "broker-2"))
    client_key, client = _certificate("flow-reader", company)
    stray_key, _stray = _certificate("someone-else", company)
    return SimpleNamespace(
        ca=_pem(company[1]), other_ca=_pem(other[1]), broker=broker,
        certificate=_pem(client), key=_key_pem(client_key),
        locked_key=_key_pem(client_key, KEY_PASSWORD),
        older_locked_key=_key_pem(client_key, KEY_PASSWORD, older_format=True),
        stray_key=_key_pem(stray_key),
    )


def with_certificates(pki, protocol="SSL", *, ca=True, client=True, key=None, key_password="", **extra) -> dict:
    """A Kafka connection's settings with a CA certificate and a client
    certificate pasted in, as the Connections page saves them."""
    config = {"brokers": "broker-1:9093, broker-2:9093", "security_protocol": protocol, "group_id": ""}
    if protocol.startswith("SASL_"):
        config.update(sasl_mechanism="SCRAM-SHA-512", username=USER, password=PASSWORD)
    if ca:
        config["ssl_ca_pem"] = pki.ca
    if client:
        config.update(ssl_certificate_pem=pki.certificate, ssl_key_pem=key or pki.key, ssl_key_password=key_password)
    config.update(extra)
    return config


def handshake(client: ssl.SSLContext, broker: ssl.SSLContext, host: str) -> str:
    """Why a TLS handshake between a client and a broker fails, or "" when each
    accepts the other. Both ends run here, in memory: no socket is opened."""
    client_in, client_out, broker_in, broker_out = (ssl.MemoryBIO() for _ in range(4))
    client_end = client.wrap_bio(client_in, client_out, server_hostname=host)
    broker_end = broker.wrap_bio(broker_in, broker_out, server_side=True)
    client_done = broker_done = False
    for _ in range(20):
        if not client_done:
            try:
                client_end.do_handshake()
                client_done = True
            except ssl.SSLWantReadError:
                pass
            except ssl.SSLError as exc:
                return f"the client refused the broker: {exc}"
        broker_in.write(client_out.read())
        if not broker_done:
            try:
                broker_end.do_handshake()
                broker_done = True
            except ssl.SSLWantReadError:
                pass
            except ssl.SSLError as exc:
                return f"the broker refused the client: {exc}"
        client_in.write(broker_out.read())
        if client_done and broker_done:
            return ""
    return "the handshake did not finish"


class CompanyBroker:
    """A broker whose certificate the company CA signed. With
    ``client_certificate`` it asks each client for a certificate the company
    CA signed, too."""

    def __init__(self, pki, scratch: Path, *, client_certificate: bool = False) -> None:
        key, certificate = pki.broker
        own = scratch / "broker.pem"
        own.write_text(_pem(certificate) + _key_pem(key), encoding="ascii")
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(own)
        if client_certificate:
            self.context.verify_mode = ssl.CERT_REQUIRED
            self.context.load_verify_locations(cadata=pki.ca)

    def refusal(self, client: ssl.SSLContext, host: str) -> str:
        return handshake(client, self.context, host)


def librdkafka_tls(conf: dict, scratch: Path) -> ssl.SSLContext:
    """The trust and the certificate librdkafka takes from ``ssl.ca.pem``,
    ``ssl.certificate.pem``, ``ssl.key.pem`` and ``ssl.key.password``. With no
    CA it trusts the computer's certificates."""
    context = ssl.create_default_context(cadata=conf.get("ssl.ca.pem") or None)
    if conf.get("ssl.certificate.pem"):
        mine = scratch / "librdkafka-client.pem"
        mine.write_text(conf["ssl.certificate.pem"] + conf["ssl.key.pem"], encoding="ascii")
        try:
            context.load_cert_chain(mine, password=conf.get("ssl.key.password", ""))
        finally:
            mine.unlink()
    return context


def kafka_python_tls(config: dict) -> ssl.SSLContext:
    """The context kafka-python wraps its sockets in: the one it is given, or
    its own, which trusts the computer's certificates."""
    return config.get("ssl_context") or ssl.create_default_context()


def first_host(brokers) -> str:
    first = brokers[0] if isinstance(brokers, list) else str(brokers).split(",")[0]
    return first.strip().rsplit(":", 1)[0]


# ── A stand-in cluster that asks for a sign-in ─────────────────────────────

class SecuredKafka(FakeKafka):
    """Topics in memory, behind a broker that only lets in the right sign-in."""

    def __init__(self) -> None:
        super().__init__()
        self.protocol, self.mechanism = "SASL_SSL", "SCRAM-SHA-512"
        self.username, self.password = USER, PASSWORD
        self.refuse_writes = ""
        # A careless client that repeats the password it was given.
        self.repeats_password = False
        # With SSL or SASL_SSL: None is a broker whose certificate the
        # computer already trusts; a CompanyBroker is one the company CA signed.
        self.tls: CompanyBroker | None = None

    def refusal(self, protocol, mechanism, username, password, *, tls=None, host="") -> str:
        """Why the broker turns a client away, or "" when it lets it in.
        ``tls`` makes the client's side of a TLS handshake."""
        if (protocol or "PLAINTEXT") != self.protocol:
            return f"Disconnected while requesting ApiVersion: the broker expects {self.protocol}"
        if self.tls is not None and self.protocol in ("SSL", "SASL_SSL"):
            failed = self.tls.refusal(tls(), host)
            if failed:
                return f"SSL handshake failed: {failed}"
        if self.protocol.startswith("SASL_") and (mechanism, username, password) != (
            self.mechanism, self.username, self.password,
        ):
            said = f" (password {password!r})" if self.repeats_password else ""
            return (
                "SASL authentication error: Authentication failed during authentication "
                f"due to invalid credentials with SASL mechanism {mechanism}{said}"
            )
        return ""

    def append(self, topic: str, value: bytes) -> None:
        self.topics.setdefault(topic, [[]])[0].append(value)

    def messages(self, topic: str) -> list[dict]:
        return [json.loads(value) for value in self.topics.get(topic, [[]])[0]]


def secured_confluent(cluster: SecuredKafka) -> tuple[types.ModuleType, types.ModuleType]:
    """confluent-kafka: Consumer, Producer and admin.AdminClient."""
    module = fake_confluent_kafka(cluster)
    KafkaError, KafkaException = module.KafkaError, module.KafkaException

    def let_in(conf: dict) -> None:
        refusal = cluster.refusal(
            conf.get("security.protocol"), conf.get("sasl.mechanism"),
            conf.get("sasl.username"), conf.get("sasl.password"),
            tls=lambda: librdkafka_tls(conf, cluster.root), host=first_host(conf.get("bootstrap.servers")),
        )
        if refusal:
            # librdkafka tells error_cb why (-181 is its SSL error, -169 a
            # refused sign-in); the call that waits only times out.
            if conf.get("error_cb"):
                conf["error_cb"](KafkaError(-181 if refusal.startswith("SSL") else -169, refusal))
            raise KafkaException(KafkaError(-185, "Local: Timed out"))

    class Consumer(module.Consumer):
        def list_topics(self, topic=None, timeout=-1):
            let_in(self.conf)
            return super().list_topics(topic, timeout)

    class Producer:
        def __init__(self, conf: dict) -> None:
            cluster.configs.append(dict(conf))
            self.conf, self.queue = conf, []

        def list_topics(self, topic=None, timeout=-1):
            let_in(self.conf)
            return SimpleNamespace(topics={topic: SimpleNamespace(partitions={0: None}, error=None)})

        def produce(self, topic, value=None, key=None, on_delivery=None):
            self.queue.append((topic, value, on_delivery))

        def flush(self, timeout=None):
            for topic, value, on_delivery in self.queue:
                if cluster.refuse_writes:
                    on_delivery(KafkaError(29, cluster.refuse_writes), None)
                else:
                    cluster.append(topic, value)
                    on_delivery(None, None)
            self.queue = []
            return 0

    class AdminClient:
        def __init__(self, conf: dict) -> None:
            cluster.configs.append(dict(conf))
            self.conf = conf

        def list_topics(self, topic=None, timeout=-1):
            let_in(self.conf)
            return SimpleNamespace(topics={name: None for name in cluster.topics}, brokers={1: None})

    admin = types.ModuleType("confluent_kafka.admin")
    admin.AdminClient = AdminClient
    module.Consumer, module.Producer, module.admin = Consumer, Producer, admin
    return module, admin


def secured_kafka_python(cluster: SecuredKafka) -> types.ModuleType:
    """kafka-python: KafkaConsumer, KafkaProducer and KafkaAdminClient."""
    module = fake_kafka_python(cluster)

    class NoBrokersAvailable(Exception):
        """What kafka-python raises when no broker lets it in."""

    def let_in(config: dict) -> None:
        refusal = cluster.refusal(
            config.get("security_protocol"), config.get("sasl_mechanism"),
            config.get("sasl_plain_username"), config.get("sasl_plain_password"),
            tls=lambda: kafka_python_tls(config), host=first_host(config.get("bootstrap_servers")),
        )
        if refusal:
            raise NoBrokersAvailable(refusal)

    class KafkaConsumer(module.KafkaConsumer):
        def __init__(self, *topics, **config) -> None:
            let_in(config)
            super().__init__(*topics, **config)

        def topics(self):
            return set(cluster.topics)

    class Sent:
        def __init__(self, exception=None) -> None:
            self.exception = exception

        def failed(self) -> bool:
            return self.exception is not None

    class KafkaProducer:
        def __init__(self, **config) -> None:
            cluster.configs.append(dict(config))
            let_in(config)
            self.config = config

        def send(self, topic, value=None, key=None):
            if cluster.refuse_writes:
                return Sent(RuntimeError(cluster.refuse_writes))
            cluster.append(topic, self.config["value_serializer"](value))
            return Sent()

        def flush(self, timeout=None):
            pass

        def close(self, timeout=None):
            pass

    class KafkaAdminClient:
        def __init__(self, **config) -> None:
            cluster.configs.append(dict(config))
            let_in(config)

        def list_topics(self):
            return list(cluster.topics)

        def close(self):
            pass

    module.KafkaConsumer, module.KafkaProducer, module.KafkaAdminClient = (
        KafkaConsumer, KafkaProducer, KafkaAdminClient,
    )
    return module


# ── Pipelines, connections and the client in use ──────────────────────────

@pytest.fixture
def cluster(tmp_path, connection_store, _revoluzen_flow_test_db, monkeypatch):
    from revoluzen_flow.main import app_state

    monkeypatch.setitem(app_state, "connection_store", connection_store)
    monkeypatch.setitem(app_state, "credential_store", None)
    monkeypatch.setattr(position_store.sync_state_store, "_db", _revoluzen_flow_test_db)
    # Test connection first asks whether a Kafka client is installed.
    monkeypatch.setattr(addons, "_importable", lambda module: sys.modules.get(module) is not None)
    found = SecuredKafka()
    found.root = tmp_path
    found.connections = connection_store
    return found


def use(monkeypatch, cluster: SecuredKafka, library: str) -> None:
    """Install one stand-in client library, as if it were the only one."""
    if library == "confluent-kafka":
        module, admin = secured_confluent(cluster)
        monkeypatch.setitem(sys.modules, "confluent_kafka", module)
        monkeypatch.setitem(sys.modules, "confluent_kafka.admin", admin)
        monkeypatch.setitem(sys.modules, "kafka", None)
    else:
        monkeypatch.setitem(sys.modules, "confluent_kafka", None)
        monkeypatch.setitem(sys.modules, "confluent_kafka.admin", None)
        monkeypatch.setitem(sys.modules, "kafka", secured_kafka_python(cluster))


def sign_in(protocol="SASL_SSL", mechanism="SCRAM-SHA-512", username=USER, password=PASSWORD) -> dict:
    """A Kafka connection's settings, as the Connections page saves them."""
    return {
        "brokers": "broker-1:9093, broker-2:9093", "security_protocol": protocol,
        "sasl_mechanism": mechanism, "username": username, "password": password, "group_id": "",
    }


def save_connection(cluster: SecuredKafka, config: dict, connection_id: str = "c_kafka") -> str:
    cluster.connections.create(Connection(
        id=connection_id, name=connection_id, type="kafka", workspace_id="default", config=config,
    ))
    return connection_id


def read_pipeline(cluster: SecuredKafka, step_type: StepType = StepType.SOURCE, **source) -> Workflow:
    """Kafka -> CSV file."""
    params = {"topic": TOPIC, "max_messages": 10, **source}
    if step_type == StepType.SOURCE:
        params["connector_type"] = "kafka"
    out = cluster.root / "read.csv"
    return Workflow(id="wf-read", name="wf-read", steps=[
        Step(id="src", type=step_type, params=params),
        Step(id="out", type=StepType.DESTINATION, params={"connector_type": "csv", "file_path": str(out)}),
    ], connections=[StepConnection(from_step="src", to_step="out")])


def write_pipeline(cluster: SecuredKafka, step_type: StepType = StepType.DESTINATION, **sink) -> Workflow:
    """CSV file -> Kafka."""
    data = cluster.root / "orders.csv"
    with open(data, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["order_id", "amount"])
        writer.writerows([(1, 10), (2, 20), (3, 30)])
    params = {"topic": "orders-out", **sink}
    if step_type == StepType.DESTINATION:
        params["connector_type"] = "kafka"
    return Workflow(id="wf-write", name="wf-write", steps=[
        Step(id="in", type=StepType.SOURCE, params={"connector_type": "csv", "file_path": str(data)}),
        Step(id="to_kafka", type=step_type, params=params),
    ], connections=[StepConnection(from_step="in", to_step="to_kafka")])


def run(cluster: SecuredKafka, wf: Workflow):
    executor = WorkflowExecutor(data_dir=str(cluster.root), app_state={"data_dir": str(cluster.root)})
    return executor.execute_workflow(wf)


def errors(result) -> dict:
    return {step: outcome.error for step, outcome in result.step_results.items() if outcome.error}


def read_rows(cluster: SecuredKafka) -> list[dict]:
    with open(cluster.root / "read.csv", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# ── The helper gives each library its own names ────────────────────────────

@pytest.mark.parametrize("protocol, mechanism", SIGN_INS)
def test_every_offered_sign_in_is_turned_into_each_librarys_settings(protocol, mechanism):
    settings = kafka_settings(sign_in(protocol, mechanism))
    assert settings.confluent() == {
        "bootstrap.servers": BROKERS, "security.protocol": protocol,
        "sasl.mechanism": mechanism, "sasl.username": USER, "sasl.password": PASSWORD,
    }
    assert settings.kafka_python() == {
        "bootstrap_servers": BROKERS.split(","), "security_protocol": protocol,
        "sasl_mechanism": mechanism, "sasl_plain_username": USER, "sasl_plain_password": PASSWORD,
    }


def test_values_typed_in_other_cases_are_understood():
    settings = kafka_settings(sign_in("sasl_ssl", "scram_sha_256"))
    assert (settings.security_protocol, settings.sasl_mechanism) == ("SASL_SSL", "SCRAM-SHA-256")


@pytest.mark.parametrize("protocol", ["PLAINTEXT", "SSL"])
def test_without_sasl_a_saved_username_and_password_are_not_sent(protocol):
    # A connection that works today keeps working: these fields were never used.
    settings = kafka_settings(sign_in(protocol, "PLAIN"))
    assert settings.confluent() == {"bootstrap.servers": BROKERS, "security.protocol": protocol}
    assert settings.kafka_python() == {"bootstrap_servers": BROKERS.split(","), "security_protocol": protocol}


def test_a_connection_saved_before_sign_in_existed_still_works():
    settings = kafka_settings({"bootstrap_servers": "old-broker:9092"})
    assert settings.confluent() == {"bootstrap.servers": "old-broker:9092", "security.protocol": "PLAINTEXT"}
    assert kafka_settings(None, {}).brokers == "localhost:9092"


def test_the_step_fields_are_used_only_without_a_connection():
    step = {
        "bootstrap_servers": "step-broker:9093", "security_protocol": "SASL_PLAINTEXT",
        "sasl_mechanism": "PLAIN", "sasl_username": "step-user", "sasl_password": "step-password",
    }
    own = kafka_settings(None, step)
    assert (own.brokers, own.security_protocol, own.username, own.password) == (
        "step-broker:9093", "SASL_PLAINTEXT", "step-user", "step-password",
    )
    # With a connection, the connection decides how to sign in.
    linked = kafka_settings(sign_in(), step)
    assert (linked.brokers, linked.security_protocol, linked.username, linked.password) == (
        BROKERS, "SASL_SSL", USER, PASSWORD,
    )


@pytest.mark.parametrize("config, expected", [
    (sign_in(mechanism=""), "needs a SASL mechanism. Choose PLAIN, SCRAM-SHA-256 or SCRAM-SHA-512"),
    (sign_in(mechanism="GSSAPI"), "the SASL mechanism 'GSSAPI' is not supported"),
    (sign_in(password=""), "needs a username and a password. Add a password to the Kafka connection."),
    (sign_in(username="", password=""), "Add a username and a password to the Kafka connection."),
    (sign_in(protocol="TLS"), "the security protocol 'TLS' is not one Kafka offers"),
])
def test_a_sign_in_that_cannot_work_is_named_plainly(config, expected):
    with pytest.raises(KafkaSettingsError) as caught:
        kafka_settings(config, who="Kafka Source")
    assert str(caught.value).startswith("Kafka Source: ")
    assert expected in str(caught.value)
    assert PASSWORD not in str(caught.value)


def test_the_password_stays_out_of_repr_and_messages():
    settings = kafka_settings(sign_in())
    assert PASSWORD not in repr(settings)
    assert settings.hide(f"login {PASSWORD} refused") == f"login {HIDDEN} refused"


# ── Each place that makes a Kafka client signs in ─────────────────────────

@pytest.mark.parametrize("library", LIBRARIES)
@pytest.mark.parametrize("protocol, mechanism", SIGN_INS)
def test_the_kafka_source_signs_in_with_the_connection(cluster, monkeypatch, library, protocol, mechanism):
    use(monkeypatch, cluster, library)
    cluster.protocol, cluster.mechanism = protocol, mechanism
    cluster.produce(TOPIC, {"order_id": 1}, {"order_id": 2})
    connection_id = save_connection(cluster, sign_in(protocol, mechanism))

    result = run(cluster, read_pipeline(cluster, connection_id=connection_id))
    assert result.status == "success", errors(result)
    assert [row["order_id"] for row in read_rows(cluster)] == ["1", "2"]


@pytest.mark.parametrize("library", LIBRARIES)
@pytest.mark.parametrize("protocol, mechanism", SIGN_INS)
def test_the_kafka_sink_signs_in_and_writes_to_the_connections_brokers(
    cluster, monkeypatch, library, protocol, mechanism,
):
    use(monkeypatch, cluster, library)
    cluster.protocol, cluster.mechanism = protocol, mechanism
    connection_id = save_connection(cluster, sign_in(protocol, mechanism))

    result = run(cluster, write_pipeline(cluster, connection_id=connection_id))
    assert result.status == "success", errors(result)
    assert [m["order_id"] for m in cluster.messages("orders-out")] == [1, 2, 3]
    # The connection's `brokers`, not localhost:9092.
    brokers = cluster.configs[-1].get("bootstrap.servers") or cluster.configs[-1].get("bootstrap_servers")
    assert brokers in (BROKERS, BROKERS.split(","))


@pytest.mark.parametrize("library", LIBRARIES)
@pytest.mark.parametrize("protocol, mechanism", SIGN_INS)
def test_test_connection_signs_in(cluster, monkeypatch, library, protocol, mechanism):
    use(monkeypatch, cluster, library)
    cluster.protocol, cluster.mechanism = protocol, mechanism
    cluster.produce(TOPIC, {"order_id": 1})

    result = ConnectionTester().test_connection("kafka", sign_in(protocol, mechanism))
    assert result["success"] is True, result
    assert result["message"] == f"Kafka connected at {BROKERS}"
    assert TOPIC in result["details"]["topics"]


@pytest.mark.parametrize("library", LIBRARIES)
def test_browsing_a_connections_topics_signs_in(cluster, monkeypatch, library):
    use(monkeypatch, cluster, library)
    cluster.produce(TOPIC, {"order_id": 1})
    cluster.produce("__consumer_offsets", {"internal": True})

    catalog = get_catalog("kafka", sign_in())
    assert catalog.supported, catalog.reason
    assert [item.name for item in catalog.items] == [TOPIC]


@pytest.mark.parametrize("library", LIBRARIES)
def test_a_step_with_no_connection_signs_in_with_its_own_fields(cluster, monkeypatch, library):
    use(monkeypatch, cluster, library)
    cluster.protocol, cluster.mechanism = "SASL_PLAINTEXT", "SCRAM-SHA-256"
    cluster.produce(TOPIC, {"order_id": 7})
    own = {
        "bootstrap_servers": BROKERS, "security_protocol": "SASL_PLAINTEXT",
        "sasl_mechanism": "SCRAM-SHA-256", "sasl_username": USER, "sasl_password": PASSWORD,
    }

    read = run(cluster, read_pipeline(cluster, StepType.KAFKA_SOURCE, **own))
    assert read.status == "success", errors(read)
    assert [row["order_id"] for row in read_rows(cluster)] == ["7"]

    written = run(cluster, write_pipeline(cluster, StepType.KAFKA_SINK, **own))
    assert written.status == "success", errors(written)
    assert len(cluster.messages("orders-out")) == 3


def test_with_a_connection_the_steps_own_sign_in_is_not_used(cluster, monkeypatch):
    use(monkeypatch, cluster, "confluent-kafka")
    cluster.produce(TOPIC, {"order_id": 1})
    connection_id = save_connection(cluster, sign_in())
    leftover = {"security_protocol": "SASL_PLAINTEXT", "sasl_mechanism": "PLAIN",
                "sasl_username": "someone-else", "sasl_password": "old-password"}

    result = run(cluster, read_pipeline(cluster, connection_id=connection_id, **leftover))
    assert result.status == "success", errors(result)


# ── A refused sign-in stops the run plainly, without the password ─────────

@pytest.mark.parametrize("library", LIBRARIES)
def test_a_wrong_password_stops_the_source_and_says_why(cluster, monkeypatch, caplog, library):
    use(monkeypatch, cluster, library)
    cluster.repeats_password = True
    cluster.produce(TOPIC, {"order_id": 1})
    wrong = "not-the-password-123"
    connection_id = save_connection(cluster, sign_in(password=wrong))
    caplog.set_level(logging.DEBUG)

    result = run(cluster, read_pipeline(cluster, connection_id=connection_id))
    assert result.status == "error"
    message = result.step_results["src"].error
    assert f"could not reach Kafka at {BROKERS}" in message
    assert "SASL authentication error" in message
    assert wrong not in message and HIDDEN in message
    assert wrong not in caplog.text


@pytest.mark.parametrize("library", LIBRARIES)
def test_a_wrong_password_stops_the_sink_instead_of_finishing_quietly(cluster, monkeypatch, caplog, library):
    use(monkeypatch, cluster, library)
    cluster.repeats_password = True
    wrong = "not-the-password-123"
    connection_id = save_connection(cluster, sign_in(password=wrong))
    caplog.set_level(logging.DEBUG)

    result = run(cluster, write_pipeline(cluster, connection_id=connection_id))
    assert result.status == "error"
    message = result.step_results["to_kafka"].error
    assert f"could not reach Kafka at {BROKERS}" in message
    assert "SASL authentication error" in message
    assert wrong not in message and HIDDEN in message
    assert wrong not in caplog.text
    assert cluster.messages("orders-out") == []


@pytest.mark.parametrize("library", LIBRARIES)
def test_messages_kafka_refuses_fail_the_sink(cluster, monkeypatch, library):
    use(monkeypatch, cluster, library)
    cluster.refuse_writes = "Broker: Topic authorization failed"
    connection_id = save_connection(cluster, sign_in())

    result = run(cluster, write_pipeline(cluster, connection_id=connection_id))
    assert result.status == "error", "the Sink finished as if Kafka had taken the messages"
    assert "3 of 3 messages were not written to topic 'orders-out'" in result.step_results["to_kafka"].error
    assert "Topic authorization failed" in result.step_results["to_kafka"].error


@pytest.mark.parametrize("library", LIBRARIES)
def test_a_wrong_password_fails_test_connection_without_sending_it_back(cluster, monkeypatch, library):
    use(monkeypatch, cluster, library)
    cluster.repeats_password = True
    wrong = "not-the-password-123"

    result = ConnectionTester().test_connection("kafka", sign_in(password=wrong))
    assert result["success"] is False
    assert "SASL authentication error" in result["error"]
    assert result["suggestion"] == "Check your username and password. Ensure the user has the correct permissions."
    assert wrong not in json.dumps(result)


@pytest.mark.parametrize("library", LIBRARIES)
def test_a_wrong_password_while_browsing_is_not_shown(cluster, monkeypatch, library):
    use(monkeypatch, cluster, library)
    cluster.repeats_password = True
    wrong = "not-the-password-123"

    catalog = get_catalog("kafka", sign_in(password=wrong))
    assert not catalog.supported
    assert "SASL authentication error" in catalog.reason
    assert wrong not in catalog.reason


# ── The Kafka Sink's Compression reaches the client ───────────────────────

@pytest.mark.parametrize("library", LIBRARIES)
@pytest.mark.parametrize("compression", ["none", "gzip", "snappy", "lz4", "zstd"])
def test_the_sinks_compression_reaches_the_client(cluster, monkeypatch, library, compression):
    use(monkeypatch, cluster, library)
    connection_id = save_connection(cluster, sign_in())

    result = run(cluster, write_pipeline(cluster, connection_id=connection_id, compression=compression))
    assert result.status == "success", errors(result)
    producer = cluster.configs[-1]
    if library == "confluent-kafka":
        assert producer["compression.type"] == compression
    else:
        assert producer["compression_type"] == (None if compression == "none" else compression)
    assert len(cluster.messages("orders-out")) == 3


def test_kafka_python_says_which_package_a_compression_needs(cluster, monkeypatch):
    use(monkeypatch, cluster, "kafka-python")
    sys.modules["kafka"].codec = SimpleNamespace(
        has_snappy=lambda: False, has_lz4=lambda: True, has_zstd=lambda: True,
    )
    connection_id = save_connection(cluster, sign_in())

    result = run(cluster, write_pipeline(cluster, connection_id=connection_id, compression="snappy"))
    assert result.status == "error"
    assert "snappy compression needs the python-snappy package" in result.step_results["to_kafka"].error
    assert cluster.messages("orders-out") == []

    lz4 = run(cluster, write_pipeline(cluster, connection_id=connection_id, compression="lz4"))
    assert lz4.status == "success", errors(lz4)


def test_a_compression_kafka_does_not_offer_is_named(cluster, monkeypatch):
    use(monkeypatch, cluster, "confluent-kafka")
    connection_id = save_connection(cluster, sign_in())

    result = run(cluster, write_pipeline(cluster, connection_id=connection_id, compression="brotli"))
    assert result.status == "error"
    assert "compression 'brotli' is not one Kafka offers" in result.step_results["to_kafka"].error


def test_a_sign_in_that_cannot_work_stops_before_any_client_is_made(cluster, monkeypatch):
    use(monkeypatch, cluster, "confluent-kafka")
    connection_id = save_connection(cluster, sign_in(mechanism=""))

    result = run(cluster, read_pipeline(cluster, connection_id=connection_id))
    assert result.status == "error"
    assert "needs a SASL mechanism" in result.step_results["src"].error
    tested = ConnectionTester().test_connection("kafka", sign_in(mechanism=""))
    assert tested["success"] is False and "needs a SASL mechanism" in tested["message"]
    assert cluster.configs == []


# ── Certificates: the helper gives each library its own names ──────────────

KEY_KINDS = ["unlocked", "locked", "older format, locked"]


def pasted_key(pki, kind: str) -> tuple[str, str]:
    """A client private key as pasted, and the password it needs."""
    return {
        "unlocked": (pki.key, ""),
        "locked": (pki.locked_key, KEY_PASSWORD),
        "older format, locked": (pki.older_locked_key, KEY_PASSWORD),
    }[kind]


def key_lines(*keys: str) -> list[str]:
    """The lines of PEM keys that carry the key itself."""
    return [line for key in keys for line in key.splitlines() if line and not line.startswith(("-----", "Proc-Type", "DEK-Info"))]


@pytest.mark.parametrize("protocol", ["SSL", "SASL_SSL"])
@pytest.mark.parametrize("kind", KEY_KINDS)
def test_pasted_certificates_are_turned_into_each_librarys_settings(pki, protocol, kind):
    key, key_password = pasted_key(pki, kind)
    settings = kafka_settings(with_certificates(pki, protocol, key=key, key_password=key_password))

    conf = settings.confluent()
    assert conf["ssl.ca.pem"] == pki.ca
    assert conf["ssl.certificate.pem"] == pki.certificate
    assert conf["ssl.key.pem"].strip() == key.strip()
    assert conf.get("ssl.key.password", "") == key_password
    assert ("sasl.password" in conf) == (protocol == "SASL_SSL")

    # kafka-python takes certificates only as files or as a context.
    python = settings.kafka_python()
    context = python["ssl_context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert [ca["subject"] for ca in context.get_ca_certs()] == [((("commonName", "Company Kafka CA"),),)]
    assert not any(name.startswith("ssl_") and name != "ssl_context" for name in python)


def test_a_ca_alone_is_enough_when_the_brokers_ask_for_no_client_certificate(pki):
    settings = kafka_settings(with_certificates(pki, client=False))
    assert {k: v for k, v in settings.confluent().items() if k.startswith("ssl.")} == {"ssl.ca.pem": pki.ca}
    assert settings.kafka_python()["ssl_context"].get_ca_certs()


def test_a_client_certificate_alone_keeps_trusting_the_computers_certificates(pki):
    settings = kafka_settings(with_certificates(pki, ca=False))
    assert "ssl.ca.pem" not in settings.confluent()
    context = settings.kafka_python()["ssl_context"]
    assert "Company Kafka CA" not in str(context.get_ca_certs())


@pytest.mark.parametrize("protocol", ["PLAINTEXT", "SASL_PLAINTEXT"])
def test_certificates_are_not_used_on_a_connection_that_is_not_encrypted(pki, protocol):
    # Leftovers from an earlier SSL set-up, even broken ones, change nothing.
    settings = kafka_settings(with_certificates(pki, protocol, ssl_ca_pem="not a certificate"))
    assert not any(name.startswith("ssl.") for name in settings.confluent())
    assert "ssl_context" not in settings.kafka_python()


def test_a_step_with_no_connection_has_no_certificates(pki):
    own = {"bootstrap_servers": BROKERS, "security_protocol": "SSL", "ssl_ca_pem": pki.ca}
    settings = kafka_settings(None, own)
    assert settings.confluent() == {"bootstrap.servers": BROKERS, "security.protocol": "SSL"}


def test_a_certificate_file_with_its_key_can_be_pasted_whole(pki):
    settings = kafka_settings(with_certificates(pki, ssl_certificate_pem=pki.certificate + pki.key, ssl_key_pem=""))
    conf = settings.confluent()
    assert conf["ssl.certificate.pem"] == pki.certificate  # the key is not sent as a certificate
    assert conf["ssl.key.pem"].strip() == pki.key.strip()


@pytest.mark.parametrize("change, expected", [
    ({"ssl_ca_pem": "MIIBnot-a-certificate"},
     "the CA certificate in the Kafka connection could not be read. Paste the whole certificate"),
    ({"ssl_ca_pem": "{ca}{key}"}, "the CA certificate in the Kafka connection holds a private key too"),
    ({"ssl_certificate_pem": "{key}"}, "the client certificate in the Kafka connection could not be read"),
    ({"ssl_key_pem": ""}, "the client certificate in the Kafka connection needs its private key"),
    ({"ssl_certificate_pem": ""}, "the Kafka connection has a client private key but no client certificate"),
    ({"ssl_key_pem": "{locked_key}"},
     "the client private key in the Kafka connection is locked with a password. Add the private key password"),
    ({"ssl_key_pem": "{older_locked_key}"}, "is locked with a password"),
    ({"ssl_key_pem": "{locked_key}", "ssl_key_password": "not-the-key-password"},
     "the private key password in the Kafka connection does not unlock the client private key"),
    ({"ssl_key_pem": "{older_locked_key}", "ssl_key_password": "not-the-key-password"}, "does not unlock"),
    ({"ssl_key_pem": "{stray_key}"},
     "the client private key in the Kafka connection does not belong to the client certificate"),
    ({"ssl_key_pem": "-----BEGIN PRIVATE KEY-----\nbm90IGEga2V5\n-----END PRIVATE KEY-----"},
     "the client private key in the Kafka connection could not be read. Paste the whole key"),
])
def test_certificates_that_cannot_work_are_named_plainly(pki, change, expected):
    config = with_certificates(pki, **{name: value.format(**vars(pki)) for name, value in change.items()})
    with pytest.raises(KafkaSettingsError) as caught:
        kafka_settings(config, who="Kafka Source")
    message = str(caught.value)
    assert message.startswith("Kafka Source: ")
    assert expected in message
    for secret in (KEY_PASSWORD, "not-the-key-password", *key_lines(pki.key, pki.locked_key, pki.stray_key)):
        assert secret not in message


def test_the_key_and_its_password_stay_out_of_repr_and_messages(pki):
    settings = kafka_settings(with_certificates(pki, key=pki.locked_key, key_password=KEY_PASSWORD))
    shown = repr(settings)
    assert KEY_PASSWORD not in shown and "PRIVATE KEY" not in shown
    line = key_lines(pki.locked_key)[0]
    said = settings.hide(f"key {pki.locked_key} line {line} password {KEY_PASSWORD}")
    assert KEY_PASSWORD not in said and line not in said and "PRIVATE KEY" not in said
    assert said.count(HIDDEN) == 3


def test_kafka_python_never_finds_the_key_in_clear_on_disk(pki, tmp_path, monkeypatch):
    """Python's ssl module reads a client key only from a file. That file holds
    the key locked with a one-time password, and is gone once it is read."""
    monkeypatch.setattr(kafka_settings_module.tempfile, "tempdir", str(tmp_path))
    written: list[str] = []
    load = ssl.SSLContext.load_cert_chain

    def spy(self, certfile, keyfile=None, password=None):
        written.append(Path(certfile).read_text(encoding="ascii"))
        return load(self, certfile, keyfile, password)

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", spy)
    settings = kafka_settings(with_certificates(pki, key=pki.key))

    settings.kafka_python()
    assert len(written) == 1
    assert pki.certificate in written[0]
    assert "-----BEGIN ENCRYPTED PRIVATE KEY-----" in written[0]
    assert not any(line in written[0] for line in key_lines(pki.key))
    assert list(tmp_path.iterdir()) == []


# ── Certificates: every place that makes a Kafka client uses them ──────────

CERTIFICATE_CASES = [
    pytest.param(library, protocol, client_certificate,
                 id=f"{library}-{protocol}-{'client certificate' if client_certificate else 'CA only'}")
    for library in LIBRARIES
    for protocol in ("SSL", "SASL_SSL")
    for client_certificate in (False, True)
]


def company_cluster(cluster: SecuredKafka, pki, protocol: str, client_certificate: bool, **config) -> dict:
    """Brokers whose certificate the company CA signed, asking for a client
    certificate or not; returns a connection set up for them."""
    cluster.protocol = protocol
    cluster.tls = CompanyBroker(pki, cluster.root, client_certificate=client_certificate)
    return with_certificates(pki, protocol, client=client_certificate, **config)


@pytest.mark.parametrize("library, protocol, client_certificate", CERTIFICATE_CASES)
def test_the_kafka_source_reaches_brokers_signed_by_the_company_ca(
    cluster, monkeypatch, pki, library, protocol, client_certificate,
):
    use(monkeypatch, cluster, library)
    connection_id = save_connection(cluster, company_cluster(cluster, pki, protocol, client_certificate))
    cluster.produce(TOPIC, {"order_id": 1}, {"order_id": 2})

    result = run(cluster, read_pipeline(cluster, connection_id=connection_id))
    assert result.status == "success", errors(result)
    assert [row["order_id"] for row in read_rows(cluster)] == ["1", "2"]


@pytest.mark.parametrize("library, protocol, client_certificate", CERTIFICATE_CASES)
def test_the_kafka_sink_reaches_brokers_signed_by_the_company_ca(
    cluster, monkeypatch, pki, library, protocol, client_certificate,
):
    use(monkeypatch, cluster, library)
    connection_id = save_connection(cluster, company_cluster(cluster, pki, protocol, client_certificate))

    result = run(cluster, write_pipeline(cluster, connection_id=connection_id))
    assert result.status == "success", errors(result)
    assert [m["order_id"] for m in cluster.messages("orders-out")] == [1, 2, 3]


@pytest.mark.parametrize("library, protocol, client_certificate", CERTIFICATE_CASES)
def test_test_connection_reaches_brokers_signed_by_the_company_ca(
    cluster, monkeypatch, pki, library, protocol, client_certificate,
):
    use(monkeypatch, cluster, library)
    config = company_cluster(cluster, pki, protocol, client_certificate)
    cluster.produce(TOPIC, {"order_id": 1})

    result = ConnectionTester().test_connection("kafka", config)
    assert result["success"] is True, result
    assert TOPIC in result["details"]["topics"]


@pytest.mark.parametrize("library, protocol, client_certificate", CERTIFICATE_CASES)
def test_browsing_reaches_brokers_signed_by_the_company_ca(
    cluster, monkeypatch, pki, library, protocol, client_certificate,
):
    use(monkeypatch, cluster, library)
    config = company_cluster(cluster, pki, protocol, client_certificate)
    cluster.produce(TOPIC, {"order_id": 1})

    catalog = get_catalog("kafka", config)
    assert catalog.supported, catalog.reason
    assert [item.name for item in catalog.items] == [TOPIC]


@pytest.mark.parametrize("library", LIBRARIES)
@pytest.mark.parametrize("kind", KEY_KINDS)
def test_a_locked_client_key_is_opened_with_its_password(cluster, monkeypatch, pki, library, kind):
    use(monkeypatch, cluster, library)
    key, key_password = pasted_key(pki, kind)
    config = company_cluster(cluster, pki, "SSL", True, ssl_key_pem=key, ssl_key_password=key_password)
    cluster.produce(TOPIC, {"order_id": 9})

    result = run(cluster, read_pipeline(cluster, connection_id=save_connection(cluster, config)))
    assert result.status == "success", errors(result)


@pytest.mark.parametrize("library", LIBRARIES)
@pytest.mark.parametrize("trusted", ["the computer's certificates", "another CA"])
def test_brokers_signed_by_a_ca_the_connection_does_not_trust_are_refused(cluster, monkeypatch, pki, library, trusted):
    # Without the company CA, as every SSL connection was before, the run stops.
    use(monkeypatch, cluster, library)
    config = company_cluster(cluster, pki, "SSL", False)
    if trusted == "another CA":
        config["ssl_ca_pem"] = pki.other_ca
    else:
        del config["ssl_ca_pem"]
    cluster.produce(TOPIC, {"order_id": 1})

    result = run(cluster, read_pipeline(cluster, connection_id=save_connection(cluster, config)))
    assert result.status == "error"
    message = result.step_results["src"].error
    assert f"could not reach Kafka at {BROKERS}" in message
    assert "certificate verify failed" in message

    tested = ConnectionTester().test_connection("kafka", config)
    assert tested["success"] is False and "certificate verify failed" in tested["error"]
    assert "CA certificate" in tested["suggestion"]


@pytest.mark.parametrize("library", LIBRARIES)
def test_brokers_that_ask_for_a_client_certificate_turn_away_a_client_without_one(cluster, monkeypatch, pki, library):
    use(monkeypatch, cluster, library)
    company_cluster(cluster, pki, "SSL", True)
    config = with_certificates(pki, "SSL", client=False)

    tested = ConnectionTester().test_connection("kafka", config)
    assert tested["success"] is False
    assert "the broker refused the client" in tested["error"]
    assert "client certificate" in tested["suggestion"]

    catalog = get_catalog("kafka", config)
    assert not catalog.supported and "the broker refused the client" in catalog.reason


def test_a_certificate_problem_stops_before_any_client_is_made(cluster, monkeypatch, pki):
    use(monkeypatch, cluster, "confluent-kafka")
    config = company_cluster(cluster, pki, "SSL", True, ssl_key_pem=pki.locked_key)
    connection_id = save_connection(cluster, config)

    result = run(cluster, read_pipeline(cluster, connection_id=connection_id))
    assert result.status == "error"
    assert "is locked with a password" in result.step_results["src"].error
    written = run(cluster, write_pipeline(cluster, connection_id=connection_id))
    assert "is locked with a password" in written.step_results["to_kafka"].error
    tested = ConnectionTester().test_connection("kafka", config)
    assert tested["success"] is False and "is locked with a password" in tested["message"]
    assert "is locked with a password" in get_catalog("kafka", config).reason
    assert cluster.configs == []


# ── Certificates: the key and its password stay secret ─────────────────────

@pytest.fixture
def encrypted(monkeypatch):
    """Secrets encrypted at rest, as on a real install."""
    from revoluzen_flow.main import app_state

    encryptor = fresh_encryptor_for_tests()
    monkeypatch.setitem(app_state, "encryptor", encryptor)
    return encryptor


def stored_row(cluster: SecuredKafka, connection_id: str) -> dict:
    row = cluster.connections._db.fetchone("SELECT data FROM connections WHERE id = ?", (connection_id,))
    return json.loads(row["data"])["config"]


@pytest.mark.parametrize("library", LIBRARIES)
def test_the_private_key_and_its_password_are_encrypted_and_never_sent_back(
    cluster, monkeypatch, pki, encrypted, caplog, library,
):
    use(monkeypatch, cluster, library)
    caplog.set_level(logging.DEBUG)
    config = company_cluster(cluster, pki, "SASL_SSL", True, ssl_key_pem=pki.locked_key, ssl_key_password=KEY_PASSWORD)
    connection_id = save_connection(cluster, config)

    stored = stored_row(cluster, connection_id)
    for name in ("ssl_key_pem", "ssl_key_password", "password"):
        assert stored[name].startswith("ENC:v1:"), name
    # The certificates are public: they stay readable, as a host name does.
    assert (stored["ssl_ca_pem"], stored["ssl_certificate_pem"]) == (pki.ca, pki.certificate)

    shown = connection_response(cluster.connections.get(connection_id))["config"]
    listed = cluster.connections.list_all()[0]["config"]
    for answer in (shown, listed):
        assert answer["ssl_key_pem"] == answer["ssl_key_password"] == SECRET_PLACEHOLDER
        assert answer["ssl_certificate_pem"] == pki.certificate

    # The run decrypts them on the server, and they reach the client.
    cluster.produce(TOPIC, {"order_id": 4})
    result = run(cluster, read_pipeline(cluster, connection_id=connection_id))
    assert result.status == "success", errors(result)
    everything = json.dumps([shown, listed]) + caplog.text
    for secret in (KEY_PASSWORD, *key_lines(pki.locked_key)):
        assert secret not in everything


def test_a_key_pasted_into_the_certificate_box_is_kept_secret_too(cluster, monkeypatch, pki, encrypted):
    use(monkeypatch, cluster, "kafka-python")
    config = company_cluster(cluster, pki, "SSL", True, ssl_certificate_pem=pki.certificate + pki.key, ssl_key_pem="")
    connection_id = save_connection(cluster, config)

    assert stored_row(cluster, connection_id)["ssl_certificate_pem"].startswith("ENC:v1:")
    shown = connection_response(cluster.connections.get(connection_id))["config"]
    assert shown["ssl_certificate_pem"] == SECRET_PLACEHOLDER
    cluster.produce(TOPIC, {"order_id": 5})
    result = run(cluster, read_pipeline(cluster, connection_id=connection_id))
    assert result.status == "success", errors(result)


@pytest.mark.parametrize("library", LIBRARIES)
def test_a_wrong_key_password_is_not_repeated_anywhere(cluster, monkeypatch, pki, caplog, library):
    use(monkeypatch, cluster, library)
    caplog.set_level(logging.DEBUG)
    wrong = "not-the-key-password"
    config = company_cluster(cluster, pki, "SSL", True, ssl_key_pem=pki.locked_key, ssl_key_password=wrong)
    connection_id = save_connection(cluster, config)

    result = run(cluster, read_pipeline(cluster, connection_id=connection_id))
    tested = ConnectionTester().test_connection("kafka", config)
    assert result.status == "error" and tested["success"] is False
    assert "does not unlock the client private key" in result.step_results["src"].error
    said = result.step_results["src"].error + json.dumps(tested) + caplog.text
    for secret in (wrong, KEY_PASSWORD, *key_lines(pki.locked_key)):
        assert secret not in said
