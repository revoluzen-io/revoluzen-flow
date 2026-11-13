"""PGP decryption, encryption and signatures for the File step.

The messages in tests/fixtures/openpgp were made with OpenPGP.js 6.3.2, an
independent implementation, so these tests check Flow against something other
than itself. The fixture keys are throwaway test keys; their passphrase is
"test passphrase". A wider check (RSA, Curve25519, NIST P-256/384/521,
X25519, signed, compressed, armored, in both directions) was run against
OpenPGP.js on 2026-11-11 and passed 57 of 57.

tests/fixtures/openpgp/rfc9580 holds the test vectors published in RFC 9580
Appendix A (version 6 keys, SEIPD v2 with OCB, GCM and EAX, version 6
signatures, Argon2) and in draft-ietf-openpgp-rfc4880bis-10 Appendix A (the
OCB packet GnuPG 2.3+ writes). Signatures and SEIPD v2 were also checked
against OpenPGP.js in both directions on 2026-11-11 (see the guide).
"""
from __future__ import annotations

import io
import os
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from revoluzen_flow.openpgp import PgpError, Signer, decrypt_file, encrypt_file, read_keys, summary
from revoluzen_flow.openpgp import algorithms as alg
from revoluzen_flow.openpgp.armor import armor_bytes, dearmor, dearmor_stream
from revoluzen_flow.openpgp.credentials import (
    decryption_keys, encryption_keys, load_credential, signing_keys, verification_keys,
)
from revoluzen_flow.openpgp.keygen import generate_key
from revoluzen_flow.openpgp.keys import Key, _parse_public, format_fingerprint, secret_key_usable, unlock
from revoluzen_flow.openpgp.message import decrypt_stream, encrypt_stream
from revoluzen_flow.openpgp.packets import LITERAL, PUBLIC_SUBKEY, SIGNATURE, iter_packets, mpi, packet
from revoluzen_flow.openpgp.signatures import check_detached, signing_key

FIXTURES = Path(__file__).parent / "fixtures" / "openpgp"
RFC = FIXTURES / "rfc9580"
PASSPHRASE = "test passphrase"
# The encrypted fixtures hold LF line ends; a Windows checkout may turn the
# plain copy into CRLF, so compare against LF.
PAYLOAD = (FIXTURES / "payload.csv").read_bytes().replace(b"\r\n", b"\n")


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def curve_key():
    return generate_key("Flow Test", "test@example.com", kind="curve25519", passphrase="flow pass")


@pytest.fixture(scope="module")
def rsa_key():
    return generate_key("Flow RSA", "rsa@example.com", kind="rsa3072", passphrase="flow pass")


# ── files made by another implementation ──

@pytest.mark.parametrize("prefix, compression", [("cv", "ZIP"), ("rsa", "ZLIB")])
def test_decrypts_files_made_by_openpgp_js(tmp_path, prefix, compression):
    source = tmp_path / "in.asc"
    source.write_text(fixture(f"{prefix}.msg.asc"), encoding="utf-8")
    result = decrypt_file(str(source), str(tmp_path / "out.csv"), read_keys(fixture(f"{prefix}.priv.asc")), PASSPHRASE)
    assert (tmp_path / "out.csv").read_bytes() == PAYLOAD
    assert result.cipher == "AES-256"
    assert result.compression == compression
    assert result.filename == "payload.bin"
    if prefix == "cv":
        assert result.signed_by, "the signer's key ID should be noted"


def test_reads_openpgp_js_keys():
    keys = read_keys(fixture("rsa.pub.asc"))
    described = summary(keys)[0]
    assert described["algorithm"] == "RSA 2048"
    assert described["encrypts_with"].replace(" ", "").endswith("4E408FE113FA1D8B")
    assert not described["has_private_key"]
    assert summary(read_keys(fixture("cv.priv.asc")))[0]["has_private_key"]


# ── round trips ──

@pytest.mark.parametrize("armor", [False, True])
@pytest.mark.parametrize("compress", [True, False])
def test_round_trip_with_a_curve25519_key(tmp_path, curve_key, armor, compress):
    plain = tmp_path / "data.bin"
    plain.write_bytes(os.urandom(150_000) + PAYLOAD)  # more than two 64 KiB partial chunks
    encrypted = tmp_path / ("data.asc" if armor else "data.pgp")
    encrypt_file(str(plain), str(encrypted), read_keys(curve_key.public_key), armor=armor, compress=compress)
    assert encrypted.read_bytes().startswith(b"-----BEGIN PGP MESSAGE-----") == armor
    out = tmp_path / "out.bin"
    result = decrypt_file(str(encrypted), str(out), read_keys(curve_key.private_key), "flow pass")
    assert out.read_bytes() == plain.read_bytes()
    assert result.filename == "data.bin"


def test_round_trip_with_rsa_and_two_recipients(tmp_path, curve_key, rsa_key):
    plain = tmp_path / "orders.csv"
    plain.write_bytes(PAYLOAD)
    encrypted = tmp_path / "orders.csv.pgp"
    used = encrypt_file(str(plain), str(encrypted), read_keys(curve_key.public_key) + read_keys(rsa_key.public_key))
    assert len(used) == 2
    for key in (curve_key, rsa_key):
        out = tmp_path / "out.csv"
        decrypt_file(str(encrypted), str(out), read_keys(key.private_key), "flow pass")
        assert out.read_bytes() == PAYLOAD


def test_elgamal_keys_still_work(tmp_path):
    p = int("fc071e0477d73aacc41c8e62d61f112277907b93e09f3d1383f4a4e990da6addaabe568b4f21e594c4a92bf6ea435888f212c43b48e9ccd5d542dfe11f94bc77", 16)
    g, x = 2, 0x1234567890ABCDEF1234567890ABCDEF
    body = bytes([4]) + (1_700_000_000).to_bytes(4, "big") + bytes([alg.ELGAMAL]) + mpi(p) + mpi(g) + mpi(pow(g, x, p))
    public, end = _parse_public(body, is_subkey=False)
    secret_mpis = mpi(x)
    private = _parse_public(body, is_subkey=False)[0]
    private.secret_body = b"\x00" + secret_mpis + alg.checksum(secret_mpis)
    plain = tmp_path / "x.txt"
    plain.write_bytes(PAYLOAD)
    encrypt_file(str(plain), str(tmp_path / "x.pgp"), [Key(public)])
    decrypt_file(str(tmp_path / "x.pgp"), str(tmp_path / "x.out"), [Key(private)])
    assert (tmp_path / "x.out").read_bytes() == PAYLOAD


# ── what is refused, and how ──

def _encrypted(tmp_path, key) -> Path:
    plain = tmp_path / "p.csv"
    plain.write_bytes(PAYLOAD)
    target = tmp_path / "p.csv.pgp"
    encrypt_file(str(plain), str(target), read_keys(key.public_key))
    return target


def test_wrong_passphrase_wrong_key_and_tampering(tmp_path, curve_key, rsa_key):
    encrypted = _encrypted(tmp_path, curve_key)
    out = tmp_path / "out.csv"
    with pytest.raises(PgpError, match="passphrase is wrong") as wrong:
        decrypt_file(str(encrypted), str(out), read_keys(curve_key.private_key), "not it")
    assert "not it" not in str(wrong.value)
    with pytest.raises(PgpError, match="encrypted for key ID .* Choose the credential with the matching private key"):
        decrypt_file(str(encrypted), str(out), read_keys(rsa_key.private_key), "flow pass")
    data = bytearray(encrypted.read_bytes())
    data[-30] ^= 0x01
    tampered = tmp_path / "tampered.pgp"
    tampered.write_bytes(bytes(data))
    with pytest.raises(PgpError, match="integrity check"):
        decrypt_file(str(tampered), str(out), read_keys(curve_key.private_key), "flow pass")
    assert not out.exists(), "a file that failed its check must not be left behind"


def test_missing_passphrase_is_explained(tmp_path, curve_key):
    encrypted = _encrypted(tmp_path, curve_key)
    with pytest.raises(PgpError, match="Add the passphrase to the credential"):
        decrypt_file(str(encrypted), str(tmp_path / "o"), read_keys(curve_key.private_key), "")


@pytest.mark.parametrize("data, message", [
    (packet(9, os.urandom(40)), "without an integrity check"),
    (packet(20, b"\x02" + os.urandom(40)), "version 2, which is not supported"),
    (packet(18, b"\x03" + os.urandom(40)), "version 3, which is not supported"),
    (packet(18, b"\x02\x07\x02\x06" + os.urandom(80)), "no session key that this credential can open"),
    (packet(11, b"b\x00\x00\x00\x00\x00hello"), "not encrypted .* turn on 'Check the sender's signature'"),
    (b"plain text, not pgp", "not PGP data"),
])
def test_refusals_say_why(tmp_path, data, message):
    with pytest.raises(PgpError, match=message):
        decrypt_stream(io.BytesIO(data), io.BytesIO(), [], None)


def test_a_key_given_as_the_file_is_explained(tmp_path):
    source = tmp_path / "key.asc"
    source.write_text(fixture("rsa.pub.asc"), encoding="utf-8")
    with pytest.raises(PgpError, match="armored|is a PGP key"):
        decrypt_file(str(source), str(tmp_path / "o"), [], None)


def test_a_stub_primary_key_is_skipped(tmp_path, rsa_key):
    keys = read_keys(rsa_key.private_key)
    keys[0].primary.secret_body = b"\xff\x00\x65\x00GNU\x01"  # exported with --export-secret-subkeys
    assert not secret_key_usable(keys[0].primary)
    encrypted = _encrypted(tmp_path, rsa_key)
    decrypt_file(str(encrypted), str(tmp_path / "o.csv"), keys, "flow pass")
    assert (tmp_path / "o.csv").read_bytes() == PAYLOAD


def test_expired_and_signing_only_keys_cannot_be_encrypted_to(curve_key):
    keys = read_keys(curve_key.public_key)
    for subkey in keys[0].subkeys:
        subkey.expires = 1
    with pytest.raises(PgpError, match="has expired"):
        keys[0].encryption_key()


# ── armor ──

def test_armor_checksum_and_lost_line_breaks():
    blob = os.urandom(300)
    text = armor_bytes("MESSAGE", blob)
    assert dearmor(text) == [("MESSAGE", blob)]
    damaged = text.replace(text.splitlines()[2][:4], "AAAA" if not text.splitlines()[2].startswith("AAAA") else "BBBB", 1)
    with pytest.raises(PgpError, match="checksum"):
        dearmor(damaged)
    one_line = text.replace("\n", "")  # pasted into a one-line box
    assert dearmor(one_line)[0][1] == blob


def test_streamed_dearmor(tmp_path):
    import io

    blob = os.urandom(5000)
    out = io.BytesIO()
    assert dearmor_stream(io.BytesIO(armor_bytes("MESSAGE", blob).encode()), out) == "MESSAGE"
    assert out.getvalue() == blob


# ── credentials ──

def test_credential_fields_and_messages(curve_key):
    with pytest.raises(PgpError, match="has no private key"):
        decryption_keys({"public_key": curve_key.public_key})
    with pytest.raises(PgpError, match="holds a public key only"):
        decryption_keys({"private_key": curve_key.public_key})
    keys, passphrase = decryption_keys({"private_key": curve_key.private_key, "passphrase": "flow pass"})
    assert keys and passphrase == "flow pass"
    assert encryption_keys({"private_key": curve_key.private_key})  # to your own key
    with pytest.raises(PgpError, match="has no public key"):
        encryption_keys({"username": "x"})


class _Store:
    def __init__(self, config, workspace="default"):
        self.config, self.workspace = config, workspace

    def get_raw(self, credential_id, workspace_id=None):
        if credential_id != "cred1" or (workspace_id or "default") != self.workspace:
            return None
        return SimpleNamespace(config=dict(self.config))


def test_credentials_stay_in_their_workspace(curve_key):
    state = {"credential_store": _Store({"private_key": curve_key.private_key}, workspace="other")}
    with pytest.raises(PgpError, match="no longer exists"):
        load_credential(state, "cred1", "default")
    assert load_credential(state, "cred1", "other")["private_key"]
    with pytest.raises(PgpError, match="Choose the PGP key credential"):
        load_credential(state, "", "default")


# ── the File step ──

def _ctx(tmp_path, store):
    from revoluzen_flow.nodes.base import ExecutionContext

    return ExecutionContext(conn=duckdb.connect(), data_dir=str(tmp_path), full_run=True,
                            app_state={"credential_store": store})


def test_file_step_decrypts_then_reads_csv_and_copybook(tmp_path, curve_key):
    from revoluzen_flow.nodes.file_node import FileSourceNode

    store = _Store({"private_key": curve_key.private_key, "passphrase": "flow pass"})
    (tmp_path / "orders.csv").write_bytes(PAYLOAD)
    encrypt_file(str(tmp_path / "orders.csv"), str(tmp_path / "orders.csv.pgp"), read_keys(curve_key.public_key))
    os.remove(tmp_path / "orders.csv")
    rel = FileSourceNode({"file_path": "orders.csv.pgp", "pgp_decrypt": True, "pgp_credential_id": "cred1"}).execute(
        _ctx(tmp_path, store))
    assert rel.fetchall() == [(1, 10.5), (2, -3.25)]

    copybook = "01 R.\n   05 ID PIC 9(2).\n   05 AMOUNT PIC S9(3)V99 COMP-3."
    record = "01".encode("cp037") + bytes.fromhex("01050c") + "02".encode("cp037") + bytes.fromhex("00325d")
    (tmp_path / "amounts.dat").write_bytes(record)
    encrypt_file(str(tmp_path / "amounts.dat"), str(tmp_path / "amounts.dat.gpg"), read_keys(curve_key.public_key), armor=True)
    rel = FileSourceNode({
        "file_path": "amounts.dat.gpg", "format": "copybook", "copybook": copybook,
        "pgp_decrypt": True, "pgp_credential_id": "cred1",
    }).execute(_ctx(tmp_path, store))
    assert [(r[0], str(r[1])) for r in rel.fetchall()] == [(1, "10.50"), (2, "-3.25")]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["amounts.dat", "amounts.dat.gpg", "orders.csv.pgp"]


def test_file_step_decrypt_errors_never_show_the_passphrase(tmp_path, curve_key):
    from revoluzen_flow.nodes.file_node import FileSourceNode

    (tmp_path / "x.csv").write_bytes(PAYLOAD)
    encrypt_file(str(tmp_path / "x.csv"), str(tmp_path / "x.csv.pgp"), read_keys(curve_key.public_key))
    store = _Store({"private_key": curve_key.private_key, "passphrase": "Secret-Phrase-123"})
    with pytest.raises(ValueError) as caught:
        FileSourceNode({"file_path": "x.csv.pgp", "pgp_decrypt": True, "pgp_credential_id": "cred1"}).execute(
            _ctx(tmp_path, _Store({"private_key": curve_key.private_key, "passphrase": "Secret-Phrase-123"})))
    assert "Secret-Phrase-123" not in str(caught.value)
    assert "passphrase is wrong" in str(caught.value)
    assert store


def test_file_sink_encrypts_and_keeps_only_the_encrypted_file(tmp_path, curve_key):
    from revoluzen_flow.nodes.file_node import FileSinkNode

    conn = duckdb.connect()
    ctx = _ctx(tmp_path, _Store({"public_key": curve_key.public_key}))
    ctx.conn = conn
    ctx.set_result("up", conn.sql("SELECT 1 AS id, 'Köln' AS city UNION ALL SELECT 2, 'Zürich'"))
    FileSinkNode({"_input_step_ids": ["up"], "file_path": "out/cities.csv", "pgp_encrypt": True,
                  "pgp_credential_id": "cred1"}).execute(ctx)
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["cities.csv.pgp"]
    decrypt_file(str(tmp_path / "out" / "cities.csv.pgp"), str(tmp_path / "back.csv"),
                 read_keys(curve_key.private_key), "flow pass")
    lines = (tmp_path / "back.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "id,city" and sorted(lines[1:]) == ["1,Köln", "2,Zürich"]

    FileSinkNode({"_input_step_ids": ["up"], "file_path": "out/cities.json", "pgp_encrypt": True,
                  "pgp_armor": True, "pgp_credential_id": "cred1"}).execute(ctx)
    assert (tmp_path / "out" / "cities.json.asc").read_text().startswith("-----BEGIN PGP MESSAGE-----")


def test_file_sink_needs_a_public_key(tmp_path):
    from revoluzen_flow.nodes.file_node import FileSinkNode

    conn = duckdb.connect()
    ctx = _ctx(tmp_path, _Store({"username": "nobody"}))
    ctx.conn = conn
    ctx.set_result("up", conn.sql("SELECT 1 AS id"))
    with pytest.raises(ValueError, match="File Sink: The credential has no public key"):
        FileSinkNode({"_input_step_ids": ["up"], "file_path": "o.csv", "pgp_encrypt": True,
                      "pgp_credential_id": "cred1"}).execute(ctx)
    assert not list(tmp_path.glob("o.csv*"))


# ── the settings screen's helpers ──

def test_copybook_layout_endpoint():
    import asyncio

    from revoluzen_flow.api.legacy_files import copybook_layout

    ok = asyncio.run(copybook_layout({"copybook": "01 R.\n   05 A PIC X(2).\n   05 B PIC S9(5) COMP-3."}, None))
    assert ok["ok"] and ok["record_length"] == 5 and [f["column"] for f in ok["fields"]] == ["A", "B"]
    bad = asyncio.run(copybook_layout({"copybook": "01 R.\n   05 A PIC X(2) WIBBLE."}, None))
    assert bad == {"ok": False, "error": "Copybook line 2: did not understand 'WIBBLE' in the definition of A."}


def test_pgp_key_check_endpoint_never_returns_secrets(monkeypatch, curve_key):
    import asyncio

    import revoluzen_flow.main as main
    from revoluzen_flow.api.legacy_files import pgp_key_check

    monkeypatch.setitem(main.app_state, "credential_store",
                        _Store({"private_key": curve_key.private_key, "passphrase": "flow pass",
                                "public_key": curve_key.public_key}))
    result = asyncio.run(pgp_key_check({"credential_id": "cred1", "purpose": "decrypt"}, None, "default"))
    assert result["ok"] and result["unlocks"] is True
    text = repr(result)
    assert "flow pass" not in text and "PRIVATE KEY" not in text
    assert result["keys"][0]["user_ids"] == ["Flow Test <test@example.com>"]
    wrong = _Store({"private_key": curve_key.private_key, "passphrase": "nope"})
    monkeypatch.setitem(main.app_state, "credential_store", wrong)
    assert asyncio.run(pgp_key_check({"credential_id": "cred1"}, None, "default")) == {
        "ok": False, "error": "The passphrase is wrong (the private key did not unlock).",
    }
    encrypt = asyncio.run(pgp_key_check({"credential_id": "cred1", "purpose": "encrypt"}, None, "default"))
    assert encrypt["ok"] and encrypt["keys"][0]["encrypts_with"]


def test_pgp_key_check_for_signatures(monkeypatch, curve_key):
    import asyncio

    import revoluzen_flow.main as main
    from revoluzen_flow.api.legacy_files import pgp_key_check

    monkeypatch.setitem(main.app_state, "credential_store", _Store({"public_key": curve_key.public_key}))
    verify = asyncio.run(pgp_key_check({"credential_id": "cred1", "purpose": "verify"}, None, "default"))
    assert verify["ok"] and verify["keys"][0]["signs_with"] == verify["keys"][0]["fingerprint"]
    assert asyncio.run(pgp_key_check({"credential_id": "cred1", "purpose": "sign"}, None, "default")) == {
        "ok": False, "error": "The credential has no private key. To sign files, add the field 'private_key' with "
                              "your armored private key (-----BEGIN PGP PRIVATE KEY BLOCK-----) and its passphrase.",
    }
    monkeypatch.setitem(main.app_state, "credential_store",
                        _Store({"private_key": curve_key.private_key, "passphrase": "flow pass"}))
    sign = asyncio.run(pgp_key_check({"credential_id": "cred1", "purpose": "sign"}, None, "default"))
    assert sign["ok"] and sign["unlocks"] is True and "flow pass" not in repr(sign)
    monkeypatch.setitem(main.app_state, "credential_store",
                        _Store({"private_key": curve_key.private_key, "passphrase": "nope"}))
    assert asyncio.run(pgp_key_check({"credential_id": "cred1", "purpose": "sign"}, None, "default"))["error"] == (
        "The passphrase is wrong (the private key did not unlock)."
    )


# ── published test vectors: RFC 9580 Appendix A and rfc4880bis-10 Appendix A ──

def rfc(name: str, index: int = 0) -> bytes:
    return dearmor((RFC / name).read_text(encoding="utf-8"))[index][1]


def rfc_keys(name: str) -> list[Key]:
    return read_keys((RFC / name).read_text(encoding="utf-8"))


def test_rfc9580_ed25519_legacy_signature():
    key = rfc_keys("a1_ed25519_legacy.pub.asc")[0]
    assert format_fingerprint(key.primary.fingerprint) == "C959 BDBA FA32 A2F8 9A15 3B67 8CFD E121 9796 5A9A"
    signature = next(body for tag, body in iter_packets(rfc("a2_signature.asc")) if tag == SIGNATURE)
    good = check_detached(b"OpenPGP", signature, [key])
    assert good.hash_name == "SHA-256" and good.created == 0x55F95F95
    with pytest.raises(PgpError, match="does not match the file"):
        check_detached(b"OpenPGQ", signature, [key])


def test_rfc9580_version_6_key_and_x25519_ocb_message():
    public = rfc_keys("a3_v6.pub.asc")[0]
    assert public.primary.fingerprint.hex().upper() == "CB186C4F0609A697E4D52DFA6C722B0C1F1E27C18A56708F6525EC27BAD9ACC9"
    assert public.subkeys[0].fingerprint.hex().upper() == "12C83F1E706F6308FE151A417743A1F033790E93E9978488D1DB378DA9930885"
    assert signing_key(public) is public.primary
    described = summary([public])[0]
    assert described["version"] == 6 and "version 6 key" in described["problem"]
    out = io.BytesIO()
    result = decrypt_stream(io.BytesIO(rfc("a8_x25519_ocb.asc")), out, rfc_keys("a4_v6.sec.asc"))
    assert out.getvalue() == b"Hello, world!"
    assert (result.cipher, result.integrity, result.key_id) == ("AES-128", "AEAD OCB", "12C83F1E706F6308")


@pytest.mark.parametrize("name, mode", [("a9_eax.asc", "EAX"), ("a10_ocb.asc", "OCB"), ("a11_gcm.asc", "GCM")])
def test_rfc9580_passphrase_messages(name, mode):
    out = io.BytesIO()
    result = decrypt_stream(io.BytesIO(rfc(name)), out, [], "password")
    assert out.getvalue() == b"Hello, world!" and result.integrity == f"AEAD {mode}"
    with pytest.raises(PgpError, match="passphrase does not open this file"):
        decrypt_stream(io.BytesIO(rfc(name)), io.BytesIO(), [], "Password")


@pytest.mark.parametrize("label", ["eax", "ocb"])
def test_gnupg_ocb_packets(label):
    lines = (RFC / "bis10_aead.hex").read_text(encoding="utf-8").splitlines()
    start = lines.index(f"{label}:") + 1
    hexed = []
    for line in lines[start:]:
        if not line.strip():
            break
        hexed.append(line)
    data = bytes.fromhex("".join(hexed).replace(" ", ""))
    out = io.BytesIO()
    result = decrypt_stream(io.BytesIO(data), out, [], "password")
    assert out.getvalue() == b"Hello, world!\n" and result.integrity == f"AEAD {label.upper()}"


def test_rfc9580_inline_signed_text_message():
    sender = rfc_keys("a3_v6.pub.asc")
    out = io.BytesIO()
    result = decrypt_stream(io.BytesIO(rfc("a7_inline_signed.asc")), out, [], verify_keys=sender)
    assert out.getvalue().startswith(b"What we need from the grocery store:\n\n- tofu\n")
    assert not result.encrypted and result.signature.hash_name == "SHA-512"
    with pytest.raises(PgpError, match="not encrypted"):
        decrypt_stream(io.BytesIO(rfc("a7_inline_signed.asc")), io.BytesIO(), [])
    changed = rfc("a7_inline_signed.asc").replace(b"tofu", b"tofU")
    with pytest.raises(PgpError, match="does not match the file"):
        decrypt_stream(io.BytesIO(changed), io.BytesIO(), [], verify_keys=sender)


def test_rfc9580_locked_version_6_key(monkeypatch):
    """A.5: AEAD (OCB) key protection. The RFC's Argon2 step needs 2 GiB, so
    its published output is fed in; HKDF and the AEAD unlock are Flow's."""
    derived = [bytes.fromhex("832bd2662a5c2b251ee3fc82aec349a766ca539015880133002e5a21960b3bcf"),
               bytes.fromhex("f74a6ce873a089ef13a3da9ac059777bb22340d15eaa6c9dc0f8ef09035c67cd")]
    locked = rfc_keys("a5_v6_locked.sec.asc")[0]
    reference = rfc_keys("a4_v6.sec.asc")[0]
    for index, part in enumerate(locked.all_packets):
        monkeypatch.setattr(alg, "derive_key", lambda s2k, passphrase, size, value=derived[index]: value)
        assert unlock(part, "correct horse battery staple")["raw"] == unlock(reference.all_packets[index], None)["raw"]
        monkeypatch.setattr(alg, "derive_key", lambda s2k, passphrase, size: bytes(32))
        with pytest.raises(PgpError, match="passphrase is wrong"):
            unlock(part, "not it")


@pytest.mark.slow
def test_rfc9580_argon2_messages_and_locked_key():
    """A.12 and A.5 with the real Argon2 (2 GiB each)."""
    for index in range(3):
        out = io.BytesIO()
        decrypt_stream(io.BytesIO(rfc("a12_argon2.asc", index)), out, [], "password")
        assert out.getvalue() == b"Hello, world!"
    out = io.BytesIO()
    decrypt_stream(io.BytesIO(rfc("a8_x25519_ocb.asc")), out, rfc_keys("a5_v6_locked.sec.asc"),
                   "correct horse battery staple")
    assert out.getvalue() == b"Hello, world!"


# ── AEAD chunks: several chunks, and every way of cutting or reordering them ──

def _aead_message(content: bytes, *, version: int, mode: int, mutate=None) -> bytes:
    """A message for the passphrase "pw" in SEIPD v2 (version 2) or GnuPG's
    OCB packet (version 1), with 64-byte chunks, built here so its chunks
    can be changed."""
    from revoluzen_flow.openpgp import aead

    cipher, session = alg.AES128, os.urandom(16)
    s2k_salt = os.urandom(8)
    s2k = bytes([3, alg.SHA256]) + s2k_salt + b"\x00"  # 1024 octets hashed
    kek = alg.derive_key(alg.S2K(3, alg.SHA256, s2k_salt, 1024), b"pw", 16)
    size = aead.NONCE_SIZES[mode]
    sk_nonce = os.urandom(size)
    if version == 2:
        info = bytes([0xC3, 6, cipher, mode])
        params = bytes([cipher, mode, len(s2k)]) + s2k + sk_nonce
        skesk = bytes([6, len(params)]) + params + aead.seal(mode, cipher, aead.hkdf_sha256(kek, info, 16),
                                                             sk_nonce, session, info)
        prefix = bytes([0xD2, 2, cipher, mode, 0])
        salt = os.urandom(32)
        derived = aead.hkdf_sha256(session, prefix, 16 + size - 8, salt=salt)
        key, iv = derived[:16], derived[16:]
        head = bytes([2, cipher, mode, 0]) + salt

        def nonce(i):
            return iv + i.to_bytes(8, "big")

        def associated(i):
            return prefix
    else:
        info = bytes([0xC3, 5, cipher, mode])
        skesk = bytes([5, cipher, mode]) + s2k + sk_nonce + aead.seal(mode, cipher, kek, sk_nonce, session, info)
        prefix = bytes([0xD4, 1, cipher, mode, 0])
        key, iv = session, os.urandom(size)
        head = bytes([1, cipher, mode, 0]) + iv

        def nonce(i):
            return iv[:-8] + bytes(a ^ b for a, b in zip(iv[-8:], i.to_bytes(8, "big")))

        def associated(i):
            return prefix + i.to_bytes(8, "big")
    literal = packet(LITERAL, b"b\x00" + bytes(4) + content)
    pieces = [literal[i:i + 64] for i in range(0, len(literal), 64)]
    sealed = [aead.seal(mode, cipher, key, nonce(i), piece, associated(i)) for i, piece in enumerate(pieces)]
    final = aead.seal(mode, cipher, key, nonce(len(pieces)), b"",
                      associated(len(pieces)) + len(literal).to_bytes(8, "big"))
    if mutate is not None:
        sealed, final = mutate(sealed, final)
    return packet(3, skesk) + packet(18 if version == 2 else 20, head + b"".join(sealed) + final)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("mode", [1, 2, 3])
def test_aead_chunks(version, mode):
    if version == 1 and mode == 3:
        pytest.skip("GnuPG's OCB packet has no GCM mode")
    content = os.urandom(1000)
    out = io.BytesIO()
    decrypt_stream(io.BytesIO(_aead_message(content, version=version, mode=mode)), out, [], "pw")
    assert out.getvalue() == content
    for bad in (
        lambda s, f: (s, b""),                         # final tag cut off
        lambda s, f: (s[:-1], f),                      # last chunk cut off
        lambda s, f: (s[:3] + s[4:], f),               # a chunk taken out
        lambda s, f: ([s[1], s[0], *s[2:]], f),        # two chunks swapped
        lambda s, f: (s, bytes([f[0] ^ 1]) + f[1:]),   # final tag changed
    ):
        out = io.BytesIO()
        with pytest.raises(PgpError, match="integrity check|cut short"):
            decrypt_stream(io.BytesIO(_aead_message(content, version=version, mode=mode, mutate=bad)), out, [], "pw")


# ── signatures ──

def _signed_only(content: bytes, signer: Signer) -> bytes:
    """A file signed by ``signer`` and not encrypted (one-pass signed)."""
    head = signer.one_pass_packet()
    signer.update(content)
    return head + packet(LITERAL, b"b\x00" + bytes(4) + content) + signer.signature_packet()


def _raw_signed(content: bytes, part, ed_private, *, hash_algorithm=alg.SHA256, extra_hashed=b"") -> bytes:
    """A file signed directly with an Ed25519 key, bypassing Flow's own
    checks on the signing key, to test what the reader accepts."""
    import hashlib
    import time

    from revoluzen_flow.openpgp.signatures import _subpacket

    hashed = (_subpacket(2, int(time.time()).to_bytes(4, "big")) + _subpacket(33, b"\x04" + part.fingerprint)
              + extra_hashed)
    body = bytes([4, 0, alg.EDDSA_LEGACY, hash_algorithm]) + len(hashed).to_bytes(2, "big") + hashed
    digest = hashlib.new(alg.HASH_NAMES[hash_algorithm], content + body + b"\x04\xff"
                         + len(body).to_bytes(4, "big")).digest()
    raw = ed_private.sign(digest)
    one_pass = bytes([3, 0, hash_algorithm, alg.EDDSA_LEGACY]) + part.key_id + b"\x01"
    signature = body + b"\x00\x00" + digest[:2] + mpi(raw[:32]) + mpi(raw[32:])
    return packet(4, one_pass) + packet(LITERAL, b"b\x00" + bytes(4) + content) + packet(SIGNATURE, signature)


def _ed25519_part(created: int):
    """A new Ed25519 (EdDSA) key packet and its private key."""
    from cryptography.hazmat.primitives.asymmetric import ed25519

    private = ed25519.Ed25519PrivateKey.generate()
    oid = alg.ED25519_LEGACY.oid
    body = (bytes([4]) + created.to_bytes(4, "big") + bytes([alg.EDDSA_LEGACY, len(oid)]) + oid
            + mpi(b"\x40" + private.public_key().public_bytes_raw()))
    return _parse_public(body, is_subkey=True)[0], body, private


def _main_signer(generated, passphrase="flow pass"):
    """The Ed25519 private key behind a Flow-made Curve25519 key's main key."""
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = read_keys(generated.private_key)[0]
    seed = unlock(key.primary, passphrase)["secret_raw"].rjust(32, b"\x00")
    return key, ed25519.Ed25519PrivateKey.from_private_bytes(seed)


def _rebuild(armored: str, *, after: dict[int, bytes] | None = None, end: bytes = b"") -> list[Key]:
    """The key with extra packets put after the packet at each index."""
    items = list(iter_packets(dearmor(armored)[0][1]))
    blob = b""
    for index, (tag, body) in enumerate(items):
        blob += packet(tag, body) + (after or {}).get(index, b"")
    return read_keys(blob + end)


def test_openpgp_js_signed_file_is_checked(tmp_path):
    source = tmp_path / "in.asc"
    source.write_text(fixture("cv.msg.asc"), encoding="utf-8")
    sender = read_keys(fixture("cv.pub.asc"))
    result = decrypt_file(str(source), str(tmp_path / "out.csv"), read_keys(fixture("cv.priv.asc")), PASSPHRASE,
                          verify_keys=sender)
    assert (tmp_path / "out.csv").read_bytes() == PAYLOAD
    assert result.signature.fingerprint == format_fingerprint(sender[0].primary.fingerprint)
    assert result.signature.hash_name == "SHA-512" and result.integrity == "MDC"
    with pytest.raises(PgpError, match="which is not the sender's key in the credential"):
        decrypt_file(str(source), str(tmp_path / "o.csv"), read_keys(fixture("cv.priv.asc")), PASSPHRASE,
                     verify_keys=read_keys(fixture("rsa.pub.asc")))
    assert not (tmp_path / "o.csv").exists()
    unsigned = tmp_path / "rsa.asc"
    unsigned.write_text(fixture("rsa.msg.asc"), encoding="utf-8")
    with pytest.raises(PgpError, match="not signed, but this step requires the sender's signature"):
        decrypt_file(str(unsigned), str(tmp_path / "o.csv"), read_keys(fixture("rsa.priv.asc")), PASSPHRASE,
                     verify_keys=read_keys(fixture("rsa.pub.asc")))


@pytest.mark.parametrize("armor", [False, True])
def test_sign_and_encrypt_round_trip(tmp_path, curve_key, rsa_key, armor):
    plain = tmp_path / "orders.csv"
    plain.write_bytes(os.urandom(70_000) + PAYLOAD)
    target = tmp_path / ("orders.asc" if armor else "orders.pgp")
    signer = Signer(read_keys(rsa_key.private_key), "flow pass")
    encrypt_file(str(plain), str(target), read_keys(curve_key.public_key), armor=armor, signer=signer)
    result = decrypt_file(str(target), str(tmp_path / "out"), read_keys(curve_key.private_key), "flow pass",
                          verify_keys=read_keys(rsa_key.public_key))
    assert (tmp_path / "out").read_bytes() == plain.read_bytes()
    assert result.signature.key_id == signer.key_id and result.signature.signer.startswith("'Flow RSA")
    assert result.signed_by == [signer.key_id]
    # The same signer signs a second file correctly (each file starts afresh).
    encrypt_file(str(plain), str(tmp_path / "again.pgp"), read_keys(curve_key.public_key), signer=signer)
    assert decrypt_file(str(tmp_path / "again.pgp"), str(tmp_path / "out2"), read_keys(curve_key.private_key),
                        "flow pass", verify_keys=read_keys(rsa_key.public_key)).signature is not None


def test_changed_signed_file_is_refused(curve_key):
    content = b"id,amount\n1,10.50\n"
    message = _signed_only(content, Signer(read_keys(curve_key.private_key), "flow pass"))
    sender = read_keys(curve_key.public_key)
    out = io.BytesIO()
    decrypt_stream(io.BytesIO(message), out, [], verify_keys=sender)
    assert out.getvalue() == content
    with pytest.raises(PgpError, match="does not match the file"):
        decrypt_stream(io.BytesIO(message.replace(b"10.50", b"99.50")), io.BytesIO(), [], verify_keys=sender)


def test_signature_rules(curve_key):
    """Weak hashes, unknown critical parts, revoked and expired keys, and
    subkeys the main key never bound or that never signed back."""
    from revoluzen_flow.openpgp import keygen
    from revoluzen_flow.openpgp.signatures import _subpacket

    key, main = _main_signer(curve_key)
    primary = key.primary
    content = b"payload"

    def check(keys, message, expected):
        with pytest.raises(PgpError, match=expected):
            decrypt_stream(io.BytesIO(message), io.BytesIO(), [], verify_keys=keys)

    sender = read_keys(curve_key.public_key)
    good = _raw_signed(content, primary, main)
    decrypt_stream(io.BytesIO(good), io.BytesIO(), [], verify_keys=sender)
    check(sender, _raw_signed(content, primary, main, hash_algorithm=2), "SHA-1 hash, which is no longer safe")
    check(sender, _raw_signed(content, primary, main, extra_hashed=_subpacket(100, b"x", critical=True)),
          "marked critical")

    revocation = keygen._signature(alg.EDDSA_LEGACY, main, primary.fingerprint, 0x20, _subpacket(29, b"\x02"),
                                   keygen._key_hash_part(primary.public_body), primary.created + 5)
    check(_rebuild(curve_key.public_key, after={0: packet(SIGNATURE, revocation)}), good, "has been revoked")

    user_id = key.user_ids[0].encode("utf-8")
    expiring = keygen._signature(
        alg.EDDSA_LEGACY, main, primary.fingerprint, 0x13, _subpacket(27, b"\x03") + _subpacket(9, (1).to_bytes(4, "big")),
        keygen._key_hash_part(primary.public_body) + b"\xb4" + len(user_id).to_bytes(4, "big") + user_id,
        primary.created + 1,
    )
    check(_rebuild(curve_key.public_key, after={1: packet(SIGNATURE, expiring)}), good, "had expired")

    # A signing subkey bound by the main key but with no back-signature.
    part, body, private = _ed25519_part(primary.created)
    binding = keygen._signature(alg.EDDSA_LEGACY, main, primary.fingerprint, 0x18, _subpacket(27, b"\x02"),
                                keygen._key_hash_part(primary.public_body) + keygen._key_hash_part(body),
                                primary.created)
    tampered = _rebuild(curve_key.public_key, end=packet(PUBLIC_SUBKEY, body) + packet(SIGNATURE, binding))
    check(tampered, _raw_signed(content, part, private), "no valid back-signature")

    # A subkey someone else attached, with a binding made by their own key.
    rogue_part, rogue_body, rogue = _ed25519_part(primary.created)
    forged = keygen._signature(alg.EDDSA_LEGACY, rogue, rogue_part.fingerprint, 0x18, _subpacket(27, b"\x02"),
                               keygen._key_hash_part(primary.public_body) + keygen._key_hash_part(rogue_body),
                               primary.created)
    attacked = _rebuild(curve_key.public_key, end=packet(PUBLIC_SUBKEY, rogue_body) + packet(SIGNATURE, forged))
    check(attacked, _raw_signed(content, rogue_part, rogue), "no valid binding signature")

    # The encryption subkey is not a signing key.
    check(sender, _raw_signed(content, key.subkeys[0], main), "not the sender's key|does not match|not a signing key")


def test_flow_keys_sign_and_say_with_what(curve_key, rsa_key):
    for generated in (curve_key, rsa_key):
        public = read_keys(generated.public_key)[0]
        assert signing_key(public) is public.primary  # Flow's main keys certify and sign
        assert summary([public])[0]["signs_with"] == generated.fingerprint
    with pytest.raises(PgpError, match="not a public key"):
        Signer(read_keys(curve_key.public_key), None)
    with pytest.raises(PgpError, match="passphrase is wrong"):
        Signer(read_keys(curve_key.private_key), "nope")


def test_signature_credentials(curve_key):
    assert verification_keys({"public_key": curve_key.public_key})
    assert verification_keys({"private_key": curve_key.private_key})  # checking your own archive copy
    with pytest.raises(PgpError, match="To check a sender's signature"):
        verification_keys({"username": "x"})
    keys, passphrase = signing_keys({"private_key": curve_key.private_key, "passphrase": "flow pass"})
    assert keys and passphrase == "flow pass"
    with pytest.raises(PgpError, match="holds a public key only"):
        signing_keys({"private_key": curve_key.public_key})


class _Stores:
    """Several credentials by id, all in the default workspace."""

    def __init__(self, configs: dict[str, dict]):
        self.configs = configs

    def get_raw(self, credential_id, workspace_id=None):
        config = self.configs.get(credential_id)
        return SimpleNamespace(config=dict(config)) if config is not None else None


def test_file_step_checks_the_senders_signature(tmp_path, curve_key, rsa_key):
    from revoluzen_flow.nodes.file_node import FileSourceNode

    store = _Stores({
        "ours": {"private_key": curve_key.private_key, "passphrase": "flow pass"},
        "sender": {"public_key": rsa_key.public_key},
    })
    (tmp_path / "orders.csv").write_bytes(PAYLOAD)
    encrypt_file(str(tmp_path / "orders.csv"), str(tmp_path / "signed.csv.pgp"), read_keys(curve_key.public_key),
                 signer=Signer(read_keys(rsa_key.private_key), "flow pass"))
    encrypt_file(str(tmp_path / "orders.csv"), str(tmp_path / "unsigned.csv.pgp"), read_keys(curve_key.public_key))
    params = {"pgp_decrypt": True, "pgp_credential_id": "ours", "pgp_verify": True,
              "pgp_verify_credential_id": "sender"}
    rel = FileSourceNode({"file_path": "signed.csv.pgp", **params}).execute(_ctx(tmp_path, store))
    assert rel.fetchall() == [(1, 10.5), (2, -3.25)]
    with pytest.raises(ValueError, match="'unsigned.csv.pgp': The file is not signed"):
        FileSourceNode({"file_path": "unsigned.csv.pgp", **params}).execute(_ctx(tmp_path, store))
    with pytest.raises(ValueError, match="choose the credential with the sender's public key"):
        FileSourceNode({"file_path": "signed.csv.pgp", **params, "pgp_verify_credential_id": ""}).execute(
            _ctx(tmp_path, store))
    # With the check off, the signature is only noted, as before.
    rel = FileSourceNode({"file_path": "unsigned.csv.pgp", **params, "pgp_verify": False}).execute(
        _ctx(tmp_path, store))
    assert len(rel.fetchall()) == 2


def test_file_sink_signs_what_it_encrypts(tmp_path, curve_key, rsa_key):
    from revoluzen_flow.nodes.file_node import FileSinkNode

    conn = duckdb.connect()
    ctx = _ctx(tmp_path, _Stores({
        "partner": {"public_key": curve_key.public_key},
        "ours": {"private_key": rsa_key.private_key, "passphrase": "flow pass"},
    }))
    ctx.conn = conn
    ctx.set_result("up", conn.sql("SELECT 1 AS id"))
    FileSinkNode({"_input_step_ids": ["up"], "file_path": "out/ids.csv", "pgp_encrypt": True,
                  "pgp_credential_id": "partner", "pgp_sign": True, "pgp_sign_credential_id": "ours"}).execute(ctx)
    result = decrypt_file(str(tmp_path / "out" / "ids.csv.pgp"), str(tmp_path / "back.csv"),
                          read_keys(curve_key.private_key), "flow pass", verify_keys=read_keys(rsa_key.public_key))
    assert result.signature is not None and result.filename == "ids.csv"
    with pytest.raises(ValueError, match="choose the credential with your private key for signing"):
        FileSinkNode({"_input_step_ids": ["up"], "file_path": "out/x.csv", "pgp_encrypt": True,
                      "pgp_credential_id": "partner", "pgp_sign": True}).execute(ctx)
    with pytest.raises(ValueError, match="your signing key: The credential has no private key"):
        FileSinkNode({"_input_step_ids": ["up"], "file_path": "out/x.csv", "pgp_encrypt": True,
                      "pgp_credential_id": "partner", "pgp_sign": True,
                      "pgp_sign_credential_id": "partner"}).execute(ctx)
    assert not list((tmp_path / "out").glob("x.csv*"))
