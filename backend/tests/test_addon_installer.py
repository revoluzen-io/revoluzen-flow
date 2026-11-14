"""Settings > Add-ons: installing driver add-ons from the app (2026-11-11).

pip never runs here: ``subprocess.run`` is replaced by a stand-in that writes
what pip would have written into the target folder.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from revoluzen_flow.connections import addon_installer as installer


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(installer, "_jobs", {})
    monkeypatch.setattr(installer, "_active_folder", None)
    monkeypatch.delenv("REVOLUZEN_FLOW_ADDON_INSTALLS", raising=False)
    yield


def _wait(package: str, seconds: float = 10.0) -> dict:
    deadline = time.time() + seconds
    while time.time() < deadline:
        job = installer._jobs.get(package) or {}
        if job.get("state") != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("the install did not finish")


def test_every_add_on_package_serves_a_connection_type():
    for package in installer.PACKAGES:
        assert installer.types_for(package), package
    assert installer.types_for("ibm_db") == ["db2", "informix"]


def test_the_folder_goes_last_on_the_import_path(tmp_path):
    folder = installer.activate(tmp_path)
    assert folder == tmp_path / "addons" / installer.folder_name()
    assert folder.is_dir()
    assert sys.path[-1] == str(folder)
    installer.activate(tmp_path)
    assert sys.path.count(str(folder)) == 1


def test_the_pip_command_is_fixed_and_wheels_only(tmp_path):
    command = installer.pip_command("hdbcli", tmp_path / "t", None)
    assert command[:4] == [sys.executable, "-m", "pip", "install"]
    assert "--only-binary=:all:" in command and "--no-input" in command
    assert command[command.index("--target") + 1] == str(tmp_path / "t")
    assert command[-1] == "hdbcli>=2.19"
    offline = installer.pip_command("hdbcli", tmp_path / "t", tmp_path / "w")
    assert "--no-index" in offline and offline[offline.index("--find-links") + 1] == str(tmp_path / "w")


@pytest.mark.parametrize("kwargs, message", [
    ({"package": "requests", "accepted": True}, "not one of the add-ons"),
    ({"package": "hdbcli", "accepted": False}, "Accept the licence"),
    ({"package": "hdbcli", "accepted": True, "from_uploads": True}, "Upload the driver's wheel files first"),
])
def test_refusals(tmp_path, kwargs, message):
    with pytest.raises(installer.AddonError, match=message):
        installer.start_install(tmp_path, **kwargs)


def test_installs_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv("REVOLUZEN_FLOW_ADDON_INSTALLS", "off")
    with pytest.raises(installer.AddonError, match="turned off"):
        installer.start_install(tmp_path, "hdbcli", accepted=True)
    assert installer.status(tmp_path)["installs_allowed"] is False


def test_one_install_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setitem(installer._jobs, "teradatasql", {"state": "running"})
    with pytest.raises(installer.AddonError, match="Another add-on"):
        installer.start_install(tmp_path, "hdbcli", accepted=True)


def test_an_install_makes_the_driver_importable_without_a_restart(tmp_path, monkeypatch):
    monkeypatch.setitem(installer.PACKAGES, "flow-test-addon", {
        "label": "Test driver", "spec": "flow-test-addon>=1.0", "licence": "MIT.", "url": "https://example.test/",
    })
    monkeypatch.setattr(installer, "modules_for", lambda package: ("flow_test_addon_mod",))
    seen: list[list[str]] = []

    def fake_pip(command, **kwargs):
        seen.append(command)
        target = Path(command[command.index("--target") + 1])
        (target / "flow_test_addon_mod").mkdir(parents=True, exist_ok=True)
        (target / "flow_test_addon_mod" / "__init__.py").write_text("VALUE = 42\n", encoding="utf-8")
        dist = target / "flow_test_addon-1.2.3.dist-info"
        dist.mkdir(exist_ok=True)
        (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: flow-test-addon\nVersion: 1.2.3\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "Successfully installed flow-test-addon-1.2.3", "")

    monkeypatch.setattr(installer.subprocess, "run", fake_pip)
    monkeypatch.setattr(installer, "pip_available", lambda: True)
    installer.activate(tmp_path)
    finished: list = []
    installer.start_install(tmp_path, "flow-test-addon", accepted=True, on_done=lambda p, j: finished.append(j))
    job = _wait("flow-test-addon")
    assert job["state"] == "done", job
    assert job["version"] == "1.2.3"
    assert "is installed (version 1.2.3)" in job["message"]
    assert finished and finished[0]["state"] == "done"
    import flow_test_addon_mod  # noqa: F401 — importable now, no restart
    assert flow_test_addon_mod.VALUE == 42
    sys.modules.pop("flow_test_addon_mod", None)


def test_a_failed_install_keeps_the_log_without_passwords(tmp_path, monkeypatch):
    def failing_pip(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 1, "", "Looking in indexes: https://reader:s3cret@pypi.example.com/simple\nERROR: No matching distribution",
        )

    monkeypatch.setattr(installer.subprocess, "run", failing_pip)
    monkeypatch.setattr(installer, "pip_available", lambda: True)
    installer.start_install(tmp_path, "hdbcli", accepted=True)
    job = _wait("hdbcli")
    assert job["state"] == "failed"
    assert "was not installed" in job["message"]
    assert "s3cret" not in job["log"] and "****@pypi.example.com" in job["log"]


def test_wheel_uploads_are_checked(tmp_path):
    with pytest.raises(installer.AddonError, match="not a Python wheel"):
        installer.save_wheel(tmp_path, "hdbcli", "setup.py", b"x")
    with pytest.raises(installer.AddonError, match="not a Python wheel"):
        installer.save_wheel(tmp_path, "hdbcli", "../../evil.whl", b"x")
    with pytest.raises(installer.AddonError, match="not one of the add-ons"):
        installer.save_wheel(tmp_path, "requests", "requests-2.0-py3-none-any.whl", b"x")
    name = installer.save_wheel(tmp_path, "hdbcli", "hdbcli-2.30.1-cp38-abi3-win_amd64.whl", b"PK")
    assert (installer.wheels_folder(tmp_path, "hdbcli") / name).read_bytes() == b"PK"
    assert installer.status(tmp_path)["packages"][1]["uploaded_wheels"] == [name]
    installer.clear_wheels(tmp_path, "hdbcli")
    assert not installer.wheels_folder(tmp_path, "hdbcli").exists()


def test_status_lists_every_driver(tmp_path):
    status = installer.status(tmp_path)
    assert [p["package"] for p in status["packages"]] == list(installer.PACKAGES)
    assert status["folder"].endswith(installer.folder_name())
    assert all(p["licence"] and p["licence_url"].startswith("https://") for p in status["packages"])


# ── The routes ──────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    from revoluzen_flow.api import addons as route
    from revoluzen_flow.auth.deps import require_admin

    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[require_admin] = lambda: None
    monkeypatch.setattr(route, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(route, "_audit", lambda *a, **k: None)
    return TestClient(app)


def test_status_route(client):
    reply = client.get("/api/addons")
    assert reply.status_code == 200
    assert {p["package"] for p in reply.json()["packages"]} == set(installer.PACKAGES)


def test_install_route_refuses_without_the_licence(client):
    reply = client.post("/api/addons/hdbcli/install", json={"accept_licence": False})
    assert reply.status_code == 400 and "Accept the licence" in reply.json()["detail"]


def test_upload_route_keeps_wheels(client, tmp_path):
    reply = client.post("/api/addons/teradatasql/wheels", files=[
        ("files", ("teradatasql-20.0.0.20-py3-none-win_amd64.whl", b"PK", "application/octet-stream")),
    ])
    assert reply.status_code == 200 and reply.json()["saved"] == ["teradatasql-20.0.0.20-py3-none-win_amd64.whl"]
    bad = client.post("/api/addons/teradatasql/wheels", files=[("files", ("notes.txt", b"x", "text/plain"))])
    assert bad.status_code == 400


def test_the_routes_need_an_admin():
    from revoluzen_flow.api import addons as route
    from revoluzen_flow.auth.deps import require_admin

    for r in route.router.routes:
        names = {getattr(d.call, "__name__", "") for d in r.dependant.dependencies}
        assert require_admin.__name__ in names, r.path
