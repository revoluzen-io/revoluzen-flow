"""Fetching one file from an FTP, FTPS or SFTP connection for the File step.

The File step reads a saved FTP / SFTP connection by downloading the file to a
private temporary folder and reading it there with any format, so mainframe
files (fixed-width, copybook records, PGP-encrypted) that arrive by SFTP need
no separate step. Host keys are checked the same way as the FTP / SFTP
Source step (``_sftp.build_ssh_client``).
"""

from __future__ import annotations

import posixpath
from typing import Any

REMOTE_TYPES = ("ftp", "sftp", "ftps")


def protocol_of(config: dict[str, Any], conn_type: str) -> str:
    chosen = str(config.get("protocol") or "").strip().lower()
    if chosen in ("ftp", "ftps", "sftp"):
        return chosen
    if conn_type == "sftp":
        return "sftp"
    return "ftps" if str(config.get("use_tls") or "").lower() in ("1", "true", "yes", "on") else "ftp"


def remote_path(config: dict[str, Any], path: str) -> str:
    """The step's path on the server: as typed when it starts with ``/``,
    otherwise under the connection's Remote Path."""
    path = (path or "").replace("\\", "/").strip()
    if not path:
        raise ValueError("File: give the file's path on the server.")
    if path.startswith("/"):
        return path
    base = str(config.get("remote_path") or "").replace("\\", "/").strip()
    return posixpath.join(base, path) if base else path


def _port(config: dict[str, Any], protocol: str) -> int:
    try:
        port = int(config.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if port == 0:
        return 22 if protocol == "sftp" else 21
    if protocol == "sftp" and port == 21:
        return 22
    return port


def download(config: dict[str, Any], conn_type: str, path: str, target: str) -> str:
    """Download ``path`` from the connection into ``target``. Returns the
    remote path that was read."""
    protocol = protocol_of(config, conn_type)
    host = str(config.get("host") or "").strip()
    if not host:
        raise ValueError("File: the FTP / SFTP connection has no host.")
    remote = remote_path(config, path)
    username = str(config.get("username") or "anonymous")
    password = str(config.get("password") or "")
    port = _port(config, protocol)
    if protocol == "sftp":
        try:
            import paramiko
        except ImportError as exc:
            raise ValueError(
                "SFTP needs the 'paramiko' package. Install it with `pip install paramiko`, then retry."
            ) from exc
        from io import StringIO

        from revoluzen_flow.nodes._sftp import build_ssh_client

        client = build_ssh_client(config)
        options: dict[str, Any] = {"hostname": host, "port": port, "username": username, "timeout": 30}
        private_key = str(config.get("private_key") or "")
        key = None
        if private_key:
            for kind in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
                try:
                    key = kind.from_private_key(StringIO(private_key))
                    break
                except Exception:  # noqa: BLE001 - try the next key type
                    continue
        if key is not None:
            options["pkey"] = key
        elif password:
            options["password"] = password
        client.connect(**options)
        try:
            client.open_sftp().get(remote, target)
        except FileNotFoundError:
            raise ValueError(f"File: '{remote}' was not found on {host}.") from None
        finally:
            client.close()
        return remote
    import ftplib

    ftp = ftplib.FTP_TLS() if protocol == "ftps" else ftplib.FTP()
    ftp.connect(host, port, timeout=30)
    try:
        ftp.login(username, password)
        if protocol == "ftps" and isinstance(ftp, ftplib.FTP_TLS):
            ftp.prot_p()
        with open(target, "wb") as out:
            try:
                ftp.retrbinary(f"RETR {remote}", out.write)
            except ftplib.error_perm as exc:
                raise ValueError(f"File: could not read '{remote}' from {host}: {exc}") from None
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001 - the transfer result already stands
            ftp.close()
    return remote
