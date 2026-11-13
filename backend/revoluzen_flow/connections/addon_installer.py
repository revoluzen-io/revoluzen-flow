"""Installing driver add-ons from inside Revoluzen Flow (2026-11-11).

Some connectors need a driver that a standard install leaves out: the
vendors' own licences (Db2, SAP HANA, Teradata) or native code (Kafka). The
only way to add one used to be ``pip install`` on the server, which a packaged
desktop install or a Docker container has no place for. An admin now adds one
on Settings > Add-ons.

The rules:

* Only the drivers listed in ``connections/addons.py``, each under a fixed
  package name. Nothing typed by the person on screen reaches pip.
* Wheels only (``--only-binary=:all:``), so installing runs no build scripts.
* Into ``<data folder>/addons/py<version>-<system>-<machine>``. The drivers live
  with the data, so a Docker volume keeps them across upgrades, and a data
  folder moved to another kind of machine does not load drivers built for the
  old one.
* That folder goes at the END of ``sys.path``: a package in it can never
  replace one Revoluzen Flow ships. A newly installed driver works at once,
  with no restart.
* From the package index pip is set up to use (``PIP_INDEX_URL`` and pip's
  other settings work, for a company mirror), or from wheel files an admin
  uploads, for a machine without internet access.
* The admin must accept the vendor's licence, which the screen shows.
* ``REVOLUZEN_FLOW_ADDON_INSTALLS=off`` turns installing off; the screen then
  only shows what is installed.

One install runs at a time, in the background. Each is audited.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from revoluzen_flow.connections.addons import ADDONS

logger = logging.getLogger(__name__)

# Package -> what installs it and what it says about the vendor's terms. The
# summaries are deliberately short; the link is the package's own page, which
# states the licence in full.
PACKAGES: dict[str, dict[str, Any]] = {
    "ibm_db": {
        "label": "IBM Db2 and Informix",
        "spec": "ibm_db>=3.2",
        "licence": "Apache-2.0 for the Python driver. The IBM Data Server Driver it brings comes under IBM's own licence terms.",
        "url": "https://pypi.org/project/ibm-db/",
    },
    "hdbcli": {
        "label": "SAP HANA",
        "spec": "hdbcli>=2.19",
        "licence": "SAP's own licence (SAP Developer License Agreement).",
        "url": "https://pypi.org/project/hdbcli/",
    },
    "teradatasql": {
        "label": "Teradata",
        "spec": "teradatasql>=20.0",
        "licence": "Teradata's own licence.",
        "url": "https://pypi.org/project/teradatasql/",
    },
    "confluent-kafka": {
        "label": "Kafka",
        "spec": "confluent-kafka>=2.3",
        "licence": "Apache-2.0.",
        "url": "https://pypi.org/project/confluent-kafka/",
    },
    "trino": {
        "label": "Trino",
        "spec": "trino>=0.328",
        "licence": "Apache-2.0.",
        "url": "https://pypi.org/project/trino/",
    },
    "presto-python-client": {
        "label": "Presto",
        "spec": "presto-python-client>=0.8",
        "licence": "Apache-2.0.",
        "url": "https://pypi.org/project/presto-python-client/",
    },
}

INSTALL_TIMEOUT_SECONDS = 900
MAX_WHEEL_BYTES = 300 * 1024 * 1024
_WHEEL_NAME = re.compile(r"^[A-Za-z0-9_.]+-[A-Za-z0-9_.!+]+(-[0-9][A-Za-z0-9_.]*)?-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+\.whl$")
_URL_CREDENTIALS = re.compile(r"(//)[^/@\s:]+:[^/@\s]+@")

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_active_folder: Path | None = None


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def types_for(package: str) -> list[str]:
    """The connection types the package serves, from the one add-on list."""
    return sorted(t for t, entry in ADDONS.items() if _normalise(entry[2]) == _normalise(package))


def modules_for(package: str) -> tuple[str, ...]:
    for entry in ADDONS.values():
        if _normalise(entry[2]) == _normalise(package):
            return entry[1]
    return ()


def folder_name() -> str:
    """``py311-windows-amd64``: Python version and the kind of machine."""
    machine = platform.machine().lower() or "unknown"
    return f"py{sys.version_info.major}{sys.version_info.minor}-{platform.system().lower()}-{machine}"


def addons_root(data_dir: str | os.PathLike) -> Path:
    return Path(data_dir) / "addons"


def addons_folder(data_dir: str | os.PathLike) -> Path:
    return addons_root(data_dir) / folder_name()


def wheels_folder(data_dir: str | os.PathLike, package: str) -> Path:
    return addons_root(data_dir) / "wheels" / _normalise(package)


def activate(data_dir: str | os.PathLike) -> Path:
    """Make the add-ons folder importable, after everything Revoluzen Flow ships."""
    global _active_folder
    folder = addons_folder(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    entry = str(folder)
    if entry not in sys.path:
        sys.path.append(entry)
    importlib.invalidate_caches()
    _active_folder = folder
    return folder


def active_folder() -> Path | None:
    return _active_folder


def installs_allowed() -> bool:
    return os.environ.get("REVOLUZEN_FLOW_ADDON_INSTALLS", "on").strip().lower() not in ("off", "0", "false", "no")


def pip_available() -> bool:
    try:
        return importlib.util.find_spec("pip") is not None
    except (ImportError, ValueError):
        return False


def _installed_version(package: str) -> str | None:
    """The version importable now, wherever it is installed, or None."""
    from revoluzen_flow.connections.addons import _importable

    if not any(_importable(m) for m in modules_for(package)):
        return None
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "installed"


def status(data_dir: str | os.PathLike) -> dict[str, Any]:
    """What the Add-ons screen shows."""
    importlib.invalidate_caches()
    folder = addons_folder(data_dir)
    packages = []
    for package, info in PACKAGES.items():
        wheels = wheels_folder(data_dir, package)
        job = dict(_jobs.get(package) or {})
        packages.append({
            "package": package,
            "label": info["label"],
            "types": types_for(package),
            "licence": info["licence"],
            "licence_url": info["url"],
            "version": _installed_version(package),
            "uploaded_wheels": sorted(p.name for p in wheels.glob("*.whl")) if wheels.is_dir() else [],
            "job": job or None,
        })
    running = next((p for p, j in _jobs.items() if j.get("state") == "running"), None)
    return {
        "installs_allowed": installs_allowed(),
        "pip_available": pip_available(),
        "folder": str(folder),
        "folder_active": _active_folder == folder,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "packages": packages,
        "running": running,
    }


class AddonError(ValueError):
    """A request the installer refuses, with a sentence for the screen."""


def save_wheel(data_dir: str | os.PathLike, package: str, filename: str, data: bytes) -> str:
    """Keep an uploaded wheel for an offline install of ``package``."""
    if package not in PACKAGES:
        raise AddonError("That is not one of the add-ons Revoluzen Flow can install.")
    name = os.path.basename(str(filename or "")).strip()
    if not name.lower().endswith(".whl") or not _WHEEL_NAME.match(name):
        raise AddonError(f"{name or 'The file'} is not a Python wheel (a .whl file).")
    if len(data) > MAX_WHEEL_BYTES:
        raise AddonError(f"{name} is larger than {MAX_WHEEL_BYTES // (1024 * 1024)} MB.")
    folder = wheels_folder(data_dir, package)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    return name


def clear_wheels(data_dir: str | os.PathLike, package: str) -> None:
    if package not in PACKAGES:
        raise AddonError("That is not one of the add-ons Revoluzen Flow can install.")
    shutil.rmtree(wheels_folder(data_dir, package), ignore_errors=True)


def start_install(data_dir: str | os.PathLike, package: str, *, accepted: bool,
                  from_uploads: bool = False, actor: str = "", on_done=None) -> dict[str, Any]:
    """Start installing ``package`` in the background. Returns the job."""
    if package not in PACKAGES:
        raise AddonError("That is not one of the add-ons Revoluzen Flow can install.")
    if not installs_allowed():
        raise AddonError("Installing add-ons is turned off on this server.")
    if not pip_available():
        raise AddonError("This install has no package installer (pip), so add-ons cannot be added from here.")
    if not accepted:
        raise AddonError(f"Accept the licence terms for the {PACKAGES[package]['label']} driver first.")
    wheels = wheels_folder(data_dir, package)
    if from_uploads and not any(wheels.glob("*.whl")):
        raise AddonError("Upload the driver's wheel files first.")
    with _lock:
        if any(j.get("state") == "running" for j in _jobs.values()):
            raise AddonError("Another add-on is being installed. Try again when it has finished.")
        job = {
            "state": "running", "source": "uploaded files" if from_uploads else "package index",
            "started_at": time.time(), "finished_at": None, "message": "", "log": "", "actor": actor,
        }
        _jobs[package] = job
    target = addons_folder(data_dir)
    thread = threading.Thread(
        target=_run, args=(package, target, wheels if from_uploads else None, job, on_done),
        name=f"addon-install-{package}", daemon=True,
    )
    thread.start()
    return dict(job)


def pip_command(package: str, target: Path, wheels: Path | None) -> list[str]:
    """The pip command line for one add-on. Every part of it is fixed here."""
    command = [
        sys.executable, "-m", "pip", "install",
        "--target", str(target), "--upgrade",
        "--only-binary=:all:", "--disable-pip-version-check", "--no-input",
        "--no-warn-script-location", "--progress-bar", "off",
    ]
    if wheels is not None:
        command += ["--no-index", "--find-links", str(wheels)]
    command.append(PACKAGES[package]["spec"])
    return command


def _clean_log(text: str) -> str:
    return _URL_CREDENTIALS.sub(r"\1****@", text or "")[-4000:]


def _run(package: str, target: Path, wheels: Path | None, job: dict[str, Any], on_done) -> None:
    target.mkdir(parents=True, exist_ok=True)
    already_loaded = any(m in sys.modules for m in modules_for(package))
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            pip_command(package, target, wheels), capture_output=True, text=True,
            timeout=INSTALL_TIMEOUT_SECONDS, env={**os.environ, "PIP_NO_INPUT": "1"}, **kwargs,
        )
        log = _clean_log((result.stdout or "") + "\n" + (result.stderr or ""))
        ok = result.returncode == 0
    except subprocess.TimeoutExpired:
        log, ok = f"The install took longer than {INSTALL_TIMEOUT_SECONDS // 60} minutes and was stopped.", False
    except Exception as exc:  # noqa: BLE001 — reported on the screen, never raised
        log, ok = f"The installer could not start: {exc}", False
    importlib.invalidate_caches()
    version = _installed_version(package) if ok else None
    label = PACKAGES[package]["label"]
    if ok and version:
        message = f"The {label} driver is installed (version {version})."
        if already_loaded:
            message += " Restart Revoluzen Flow to use the new version."
    elif ok:
        ok = False
        message = f"The installer finished, but the {label} driver still cannot be loaded. See the details."
    else:
        message = f"The {label} driver was not installed. See the details."
    job.update(state="done" if ok else "failed", finished_at=time.time(), message=message, log=log, version=version)
    logger.info("Add-on install %s: %s", package, message)
    if on_done is not None:
        try:
            on_done(package, dict(job))
        except Exception:  # noqa: BLE001
            logger.warning("Add-on install %s: the audit record could not be written", package, exc_info=True)
