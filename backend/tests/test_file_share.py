"""Windows file shares (SMB) for the File step, Test connection and browsing.

No SMB server is needed: ``FakeSmbClient`` stands in for the ``smbclient``
module with the same function names and keyword arguments, keeping each
``\\\\server\\share`` as a folder under pytest's tmp_path. A check against
the real library (closed local port, 2026-11-11) showed that it accepts the
same keyword arguments and that its connection failure reads "Could not reach
...".
"""
from __future__ import annotations

import fnmatch
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from revoluzen_flow.connections import file_share
from revoluzen_flow.connections.file_share import ShareError, inner_path, location
from revoluzen_flow.nodes.base import ExecutionContext

PASSWORD = "Sup3r-Secret!pw"
CONFIG = {
    "host": "fileserver01", "share": "mainframe", "base_folder": "outbound",
    "domain": "CORP", "username": "svc_flow", "password": PASSWORD,
}
COPYBOOK = "01 R.\n   05 ID PIC 9(2).\n   05 AMOUNT PIC S9(3)V99 COMP-3."
RECORDS = "01".encode("cp037") + bytes.fromhex("01050c") + "02".encode("cp037") + bytes.fromhex("00325d")


class FakeShareError(OSError):
    """What smbclient raises: an OSError carrying the server's NTSTATUS."""

    def __init__(self, ntstatus: int, path: str = "") -> None:
        super().__init__(0, f"NtStatus 0x{ntstatus:08x}", path)
        self.ntstatus = ntstatus


class FakeDirEntry:
    """What smbclient's scandir yields. Like the real ``SMBDirEntry``, the
    size, last-write time and attributes the server sent with the listing are
    in ``smb_info``, and ``stat()`` asks the server again (counted)."""

    def __init__(self, client: FakeSmbClient, entry: os.DirEntry) -> None:
        self._client = client
        self._path = entry.path
        self.name = entry.name
        self._is_dir = entry.is_dir()
        self._is_link = entry.is_symlink()
        info = entry.stat()
        self.smb_info = SimpleNamespace(
            end_of_file=0 if self._is_dir else info.st_size,
            last_write_time=datetime.fromtimestamp(info.st_mtime, tz=timezone.utc),
            file_attributes=0x10 if self._is_dir else 0x80,  # DIRECTORY / NORMAL
        )

    def is_dir(self, follow_symlinks=True):
        return self._is_dir

    def is_symlink(self):
        return self._is_link

    def stat(self, follow_symlinks=True):
        self._client.entry_stats += 1
        return os.stat(self._path)


class FakeSmbClient:
    """Stands in for the smbclient module. ``\\\\server\\share\\x`` is the
    folder ``root/server/share/x``; one account may sign in. A file named in
    ``writing`` is open in another program that is writing it."""

    def __init__(self, root: Path, account=("CORP\\svc_flow", PASSWORD)) -> None:
        self.root = root
        self.account = account
        self.servers = {"fileserver01"}
        self.sign_ins: list[dict] = []
        self.calls: list[tuple] = []
        self.closed = 0
        self.lenient_patterns = False
        self.fail_replace = False
        self.writing: set[str] = set()  # names of files still being written
        self.entry_stats = 0

    def share(self, server: str = "fileserver01", share: str = "mainframe") -> Path:
        folder = self.root / server / share
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _local(self, unc: str, kwargs: dict) -> Path:
        # Every call runs inside a session Flow opened, with its own cache.
        assert kwargs.get("connection_cache"), "a call outside Flow's own session"
        assert kwargs.get("username") == self.account[0]
        parts = [p for p in unc.split("\\") if p]
        share_dir = self.root / parts[0].lower() / parts[1].lower()
        if not share_dir.is_dir():
            raise FakeShareError(0xC00000CC, unc)  # BAD_NETWORK_NAME
        return share_dir.joinpath(*parts[2:])

    def register_session(self, server, username=None, password=None, port=445, encrypt=None,
                         connection_timeout=60, connection_cache=None, **_):
        self.sign_ins.append({"server": server, "username": username, "port": port,
                              "encrypt": encrypt, "timeout": connection_timeout})
        if server.lower() not in self.servers:
            raise ValueError(f"Failed to connect to '{server}:{port}': [Errno 11001] getaddrinfo failed")
        if (username, password) != self.account:
            raise FakeShareError(0xC000006D)  # LOGON_FAILURE
        connection_cache[f"{server.lower()}:{port}"] = "connected"

    def reset_connection_cache(self, fail_on_error=True, connection_cache=None):
        assert connection_cache is not None, "the process-wide pool must never be used"
        connection_cache.clear()
        self.closed += 1

    def scandir(self, path, search_pattern="*", **kwargs):
        folder = self._local(path, kwargs)
        if not folder.is_dir():
            raise FakeShareError(0xC000003A, path)  # OBJECT_PATH_NOT_FOUND
        entries = list(os.scandir(folder))
        if not self.lenient_patterns:  # a lenient server also matches 8.3 short names
            entries = [e for e in entries if fnmatch.fnmatch(e.name.lower(), search_pattern.lower())]
        return iter([FakeDirEntry(self, e) for e in entries])

    def open_file(self, path, mode="r", share_access=None, **kwargs):
        local = self._local(path, kwargs)
        self.calls.append(("open", path, mode, share_access))
        if "r" in mode:
            if local.is_dir():
                raise FakeShareError(0xC00000BA, path)  # FILE_IS_A_DIRECTORY
            if not local.exists():
                raise FakeShareError(0xC0000034, path)  # OBJECT_NAME_NOT_FOUND
            if local.name in self.writing and "w" not in (share_access or ""):
                raise FakeShareError(0xC0000043, path)  # SHARING_VIOLATION: a writer has it open
            return open(local, "rb")
        if not local.parent.is_dir():
            raise FakeShareError(0xC000003A, path)
        return open(local, "wb")

    def stat(self, path, **kwargs):
        local = self._local(path, kwargs)
        if not local.exists():
            raise FakeShareError(0xC0000034, path)
        return os.stat(local)

    def makedirs(self, path, exist_ok=False, **kwargs):
        self.calls.append(("makedirs", path))
        self._local(path, kwargs).mkdir(parents=True, exist_ok=exist_ok)

    def replace(self, src, dst, **kwargs):
        self.calls.append(("replace", src, dst))
        if self.fail_replace:
            raise FakeShareError(0xC0000043, dst)  # SHARING_VIOLATION
        os.replace(self._local(src, kwargs), self._local(dst, kwargs))

    def remove(self, path, **kwargs):
        self.calls.append(("remove", path))
        os.remove(self._local(path, kwargs))


class Connections:
    def __init__(self, config: dict) -> None:
        self.config = config

    def get(self, connection_id, workspace_id=None):
        if connection_id != "share1":
            return None
        return SimpleNamespace(config=dict(self.config), type="smb", credential_id=None)


class Credentials:
    def __init__(self, config: dict) -> None:
        self.config = config

    def get_raw(self, credential_id, workspace_id=None):
        return SimpleNamespace(config=dict(self.config)) if credential_id == "pgp1" else None


@pytest.fixture
def fake(tmp_path, monkeypatch):
    client = FakeSmbClient(tmp_path / "servers")
    client.share()
    monkeypatch.setattr(file_share, "_client", lambda: client)
    return client


def _ctx(tmp_path, config=None, credentials=None):
    state = {"connection_store": Connections(config or CONFIG)}
    if credentials is not None:
        state["credential_store"] = Credentials(credentials)
    return ExecutionContext(conn=duckdb.connect(), data_dir=str(tmp_path / "data"), full_run=True, app_state=state)


# ── where a path points ──

def test_location_reads_the_form_and_a_path_copied_from_explorer():
    loc = location({"host": "\\\\fileserver01", "share": "mainframe\\incoming", "base_folder": "/daily/", "port": ""})
    assert (loc.server, loc.share, loc.folder, loc.port) == ("fileserver01", "mainframe", "incoming\\daily", 445)
    assert loc.unc("a.dat") == "\\\\fileserver01\\mainframe\\a.dat"
    pasted = location({"host": "\\\\fs01\\finance\\in", "port": "1445"})
    assert (pasted.server, pasted.share, pasted.folder, pasted.port) == ("fs01", "finance", "in", 1445)
    for config, message in [
        ({"share": "x"}, "Give the server's name"),
        ({"host": "fs01"}, "Give the share's name"),
        ({"host": "fs01\\a", "share": "b"}, "Server takes only the server's name"),
        ({"host": "fs01", "share": "b", "port": "smb"}, "is not a number"),
        ({"host": "fs01", "share": "b", "base_folder": "..\\.."}, "goes above the top of the share"),
    ]:
        with pytest.raises(ShareError, match=message):
            location(config)


def test_step_paths_stay_inside_the_connections_share():
    loc = location(CONFIG)
    assert inner_path(loc, "daily/CUSTMAST.DAT") == "outbound\\daily\\CUSTMAST.DAT"
    assert inner_path(loc, "\\inbound\\x.dat") == "inbound\\x.dat"  # from the top of the share
    assert inner_path(loc, "//FILESERVER01/MainFrame/a/b.csv") == "a\\b.csv"
    assert inner_path(loc, "../inbound/x.dat") == "inbound\\x.dat"
    for path, message in [
        ("", "Give the file's path on the share"),
        ("C:\\data\\x.csv", "is a path on a computer, not on the share"),
        ("\\\\elsewhere\\mainframe\\x.csv", "is not on \\\\\\\\fileserver01\\\\mainframe"),
        ("\\\\fileserver01\\other\\x.csv", "is not on"),
        ("..\\..\\x.csv", "goes above the top of the share"),
        ("..", "is a folder"),
    ]:
        with pytest.raises(ShareError, match=message):
            inner_path(loc, path)


# ── reading ──

def test_file_step_reads_a_copybook_file_from_the_share(tmp_path, fake):
    from revoluzen_flow.nodes.generic import GenericSourceNode

    (fake.share() / "outbound").mkdir()
    (fake.share() / "outbound" / "AMOUNTS.DAT").write_bytes(RECORDS)
    rel = GenericSourceNode({"connector_type": "file", "connection_id": "share1", "file_path": "AMOUNTS.DAT",
                             "format": "copybook", "copybook": COPYBOOK}).execute(_ctx(tmp_path))
    assert [(r[0], str(r[1])) for r in rel.fetchall()] == [(1, "10.50"), (2, "-3.25")]
    assert fake.sign_ins == [{"server": "fileserver01", "username": "CORP\\svc_flow", "port": 445,
                              "encrypt": None, "timeout": file_share.RUN_TIMEOUT}]
    # Read while others may read it too, but not while another program writes it.
    assert fake.calls == [("open", "\\\\fileserver01\\mainframe\\outbound\\AMOUNTS.DAT", "rb", "r")]
    assert fake.closed == 1


def test_a_name_pattern_reads_every_matching_file_in_name_order(tmp_path, fake):
    from revoluzen_flow.nodes.file_node import FileSourceNode

    folder = fake.share() / "outbound" / "daily"
    folder.mkdir(parents=True)
    (folder / "orders_02.csv").write_text("id,amount\n2,20\n", encoding="utf-8")
    (folder / "ORDERS_01.CSV").write_text("id,amount\n1,10\n", encoding="utf-8")
    (folder / "orders_03.txt").write_text("id,amount\n3,30\n", encoding="utf-8")
    (folder / "orders_04.csv").mkdir()  # a folder is never read
    fake.lenient_patterns = True  # a server may return more than the pattern asks for
    rel = FileSourceNode({"connection_id": "share1", "file_path": "daily/orders_*.csv"}).execute(_ctx(tmp_path))
    assert sorted(rel.fetchall()) == [(1, 10), (2, 20)]
    assert [c[1].rsplit("\\", 1)[1] for c in fake.calls] == ["ORDERS_01.CSV", "orders_02.csv"]

    with pytest.raises(ValueError, match=r"File: No file in .*daily matches 'invoices_\*.csv'"):
        FileSourceNode({"connection_id": "share1", "file_path": "daily/invoices_*.csv"}).execute(_ctx(tmp_path))
    with pytest.raises(ValueError, match="work in the file's name only"):
        FileSourceNode({"connection_id": "share1", "file_path": "d*/orders.csv"}).execute(_ctx(tmp_path))


def test_reading_problems_are_explained_and_never_show_the_password(tmp_path, fake, caplog):
    from revoluzen_flow.nodes.file_node import FileSourceNode

    caplog.set_level("DEBUG")

    def read(path, **config):
        return FileSourceNode({"connection_id": "share1", "file_path": path}).execute(
            _ctx(tmp_path, {**CONFIG, **config}))

    with pytest.raises(ValueError, match=r"File: \\\\fileserver01\\mainframe\\outbound\\missing.csv was not found"):
        read("missing.csv")
    (fake.share() / "outbound" / "folder.csv").mkdir(parents=True)
    with pytest.raises(ValueError, match="is a folder"):
        read("folder.csv")
    with pytest.raises(ValueError, match="fileserver01 refused the sign-in. Check the domain, user name and password"):
        read("x.csv", password="wrong")
    with pytest.raises(ValueError, match="fileserver01 has no share called 'payroll'"):
        read("x.csv", share="payroll")
    with pytest.raises(ValueError, match="Could not reach nowhere01 on port 445"):
        read("x.csv", host="nowhere01")
    with pytest.raises(ValueError, match="Give the user name and password"):
        read("x.csv", username="")
    assert fake.closed == 5  # every connection is closed, failed sign-ins included
    assert PASSWORD not in caplog.text


def test_a_path_on_another_server_is_refused_before_signing_in(tmp_path, fake):
    from revoluzen_flow.nodes.file_node import FileSourceNode

    with pytest.raises(ValueError, match="is not on"):
        FileSourceNode({"connection_id": "share1", "file_path": "\\\\collector.example\\loot\\x.csv"}).execute(
            _ctx(tmp_path))
    assert fake.sign_ins == []


def test_port_and_required_encryption_reach_the_client(tmp_path, fake):
    from revoluzen_flow.nodes.file_node import FileSourceNode

    (fake.share() / "outbound").mkdir()
    (fake.share() / "outbound" / "a.csv").write_text("n\n1\n", encoding="utf-8")
    FileSourceNode({"connection_id": "share1", "file_path": "a.csv"}).execute(
        _ctx(tmp_path, {**CONFIG, "port": "1445", "encrypt": "true", "domain": "", "username": "CORP\\svc_flow"}))
    assert fake.sign_ins[-1]["port"] == 1445 and fake.sign_ins[-1]["encrypt"] is True
    assert fake.sign_ins[-1]["username"] == "CORP\\svc_flow"


# ── writing ──

def _upstream(ctx, sql="SELECT 1 AS id, 'Köln' AS city UNION ALL SELECT 2, 'Zürich' ORDER BY id"):
    ctx.set_result("up", ctx.conn.sql(sql))
    return ctx


def test_destination_writes_a_whole_file_to_the_share(tmp_path, fake):
    from revoluzen_flow.nodes.generic import GenericDestinationNode

    ctx = _upstream(_ctx(tmp_path))
    params = {"connector_type": "file", "connection_id": "share1", "file_path": "results\\2026\\cities.csv",
              "_input_step_ids": ["up"]}
    GenericDestinationNode(params).execute(ctx)
    written = fake.share() / "outbound" / "results" / "2026" / "cities.csv"
    assert written.read_text(encoding="utf-8").splitlines() == ["id,city", "1,Köln", "2,Zürich"]
    replaced = [c for c in fake.calls if c[0] == "replace"]
    assert len(replaced) == 1 and replaced[0][1].endswith(".partial")
    assert replaced[0][2] == "\\\\fileserver01\\mainframe\\outbound\\results\\2026\\cities.csv"
    assert ("makedirs", "\\\\fileserver01\\mainframe\\outbound\\results\\2026") in fake.calls

    # A second run replaces the file; the folder is there now.
    fake.calls.clear()
    GenericDestinationNode(params).execute(_upstream(ctx, "SELECT 3 AS id, 'Pune' AS city"))
    assert written.read_text(encoding="utf-8").splitlines() == ["id,city", "3,Pune"]
    assert not [c for c in fake.calls if c[0] == "makedirs"]
    assert sorted(p.name for p in written.parent.iterdir()) == ["cities.csv"]
    assert not (tmp_path / "data").exists()  # nothing was written on the Flow server


def test_a_failed_upload_leaves_no_half_file(tmp_path, fake):
    from revoluzen_flow.nodes.file_node import FileSinkNode

    (fake.share() / "outbound").mkdir()
    fake.fail_replace = True
    with pytest.raises(ValueError, match=r"File Sink: .*out.csv is open in another program"):
        FileSinkNode({"connection_id": "share1", "file_path": "out.csv", "_input_step_ids": ["up"]}).execute(
            _upstream(_ctx(tmp_path)))
    assert list((fake.share() / "outbound").iterdir()) == []
    assert fake.calls[-1][0] == "remove" and fake.calls[-1][1].endswith(".partial")
    with pytest.raises(ValueError, match="cannot be part of the name of a file to write"):
        FileSinkNode({"connection_id": "share1", "file_path": "out_*.csv", "_input_step_ids": ["up"]}).execute(
            _upstream(_ctx(tmp_path)))


def test_pgp_encrypted_files_go_to_the_share_and_come_back(tmp_path, fake):
    from revoluzen_flow.nodes.file_node import FileSinkNode, FileSourceNode
    from revoluzen_flow.openpgp.keygen import generate_key

    key = generate_key("Flow Share Test", "share@example.com", kind="curve25519", passphrase="flow pass")
    ctx = _upstream(_ctx(tmp_path, credentials={"public_key": key.public_key}))
    FileSinkNode({"connection_id": "share1", "file_path": "partner/cities.csv", "pgp_encrypt": True,
                  "pgp_credential_id": "pgp1", "_input_step_ids": ["up"]}).execute(ctx)
    folder = fake.share() / "outbound" / "partner"
    assert sorted(p.name for p in folder.iterdir()) == ["cities.csv.pgp"]
    assert "Zürich".encode("utf-8") not in (folder / "cities.csv.pgp").read_bytes()

    rel = FileSourceNode({"connection_id": "share1", "file_path": "partner/cities.csv.pgp", "pgp_decrypt": True,
                          "pgp_credential_id": "pgp1"}).execute(
        _ctx(tmp_path, credentials={"private_key": key.private_key, "passphrase": "flow pass"}))
    assert rel.fetchall() == [(1, "Köln"), (2, "Zürich")]


def test_writing_on_the_flow_server_is_unchanged(tmp_path, fake):
    from revoluzen_flow.nodes.file_node import FileSinkNode

    ctx = _upstream(_ctx(tmp_path))
    FileSinkNode({"file_path": "out/cities.csv", "_input_step_ids": ["up"]}).execute(ctx)
    assert (tmp_path / "data" / "out" / "cities.csv").is_file()
    with pytest.raises(ValueError, match="connection 'gone' not found"):
        FileSinkNode({"connection_id": "gone", "file_path": "x.csv", "_input_step_ids": ["up"]}).execute(ctx)
    assert fake.sign_ins == []


# ── Test connection, browsing, and where the type is registered ──

def test_test_connection_and_browsing(fake):
    from revoluzen_flow.connections.catalog import get_catalog
    from revoluzen_flow.connections.tester import ConnectionTester

    outbound = fake.share() / "outbound"
    (outbound / "daily").mkdir(parents=True)
    (outbound / "CUSTMAST.DAT").write_bytes(RECORDS)
    result = ConnectionTester().test_connection("smb", CONFIG)
    assert result["success"] is True
    assert result["message"] == "Opened \\\\fileserver01\\mainframe\\outbound (2 files and folders)"
    assert result["details"]["files_in_folder"] == 2
    assert fake.sign_ins[-1]["timeout"] == file_share.TEST_TIMEOUT

    failed = ConnectionTester().test_connection("smb", {**CONFIG, "password": "nope"})
    assert failed["success"] is False and "refused the sign-in" in failed["error"]
    assert PASSWORD not in str(failed)

    catalog = get_catalog("smb", CONFIG)
    assert catalog.supported is True
    assert [(i.name, i.kind) for i in catalog.items] == [("daily", "directory"), ("CUSTMAST.DAT", "file")]
    assert catalog.items[1].metadata == {"size": len(RECORDS)}
    assert get_catalog("smb", {**CONFIG, "share": "payroll"}).supported is False


def test_a_missing_package_is_said_plainly(monkeypatch):
    monkeypatch.setitem(sys.modules, "smbclient", None)
    with pytest.raises(ShareError, match=r"needs an add-on .*\(Python package: smbprotocol\)"):
        file_share.download(CONFIG, "x.csv", ".")


def test_smb_is_a_file_location_everywhere_it_is_listed():
    from revoluzen_flow.api.catalog import _CONNECTOR_CATEGORIES
    from revoluzen_flow.connections.capability_levels import capability_levels, maturity_label
    from revoluzen_flow.connections.models import CONNECTION_CATEGORIES, CONNECTION_TYPES, STORAGE_TYPES

    assert "smb" in CONNECTION_TYPES and "smb" in CONNECTION_CATEGORIES["File Systems"] and "smb" in STORAGE_TYPES
    assert _CONNECTOR_CATEGORIES["smb"] == "filesystem"
    levels = capability_levels("smb")
    assert levels["form"] and levels["testable"] and levels["catalog"]
    assert levels["source_runtime"] and levels["sink_runtime"]
    assert maturity_label(levels) == "production"
