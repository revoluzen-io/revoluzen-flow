"""List the files in one folder, wherever the folder is.

A file-arrival trigger looks at a folder on this server, on an SFTP server, on
a Windows file share, or under a key prefix in Amazon S3 (or anything that
speaks S3, such as MinIO), Google Cloud Storage or Azure Blob Storage. Only
files directly in the folder count, not those in folders inside it.

Each listing returns :class:`FoundFile` records. ``path`` is what the pipeline
gets as its parameter, in the form that location's Source step expects: a full
path on this server, a path on the SFTP server, a full ``\\\\server\\share\\...``
path on the share, or the object's key inside the connection's bucket or
container. ``version`` tells two versions of the same file apart: the object's
content tag where the store has one, otherwise its size and modified time.

Credentials come from the saved connection, read the same way the Source steps
read them, and are never logged or put in an error message.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import posixpath
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

#: Seconds to wait for a server before a listing gives up.
TIMEOUT_SECONDS = 30
#: A folder with more files than this is listed only this far, oldest pages first.
MAX_FILES = 20_000

#: Connection types each location accepts. An SFTP server can also be saved as
#: an FTP connection with SFTP chosen as its protocol.
CONNECTION_TYPES = {
    "sftp": ("sftp", "ftp"),
    "smb": ("smb",),
    "s3": ("s3", "minio"),
    "gcs": ("gcs",),
    "azure_blob": ("azure_blob",),
}

LOCATION_LABELS = {
    "local": "a folder on this server",
    "sftp": "SFTP",
    "smb": "a Windows file share",
    "s3": "Amazon S3",
    "gcs": "Google Cloud Storage",
    "azure_blob": "Azure Blob Storage",
}


class ListingError(Exception):
    """The folder could not be listed. The message is safe to show to a person."""


@dataclass(frozen=True)
class FoundFile:
    path: str
    name: str
    size: int
    modified: datetime | None
    tag: str = ""

    @property
    def version(self) -> str:
        if self.tag:
            return f"tag:{self.tag}"
        millis = int(self.modified.timestamp() * 1000) if self.modified else 0
        return f"{self.size}:{millis}"

    def describe(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "size": self.size,
            "modified": self.modified.isoformat() if self.modified else None,
        }


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _prefix(folder: str) -> str:
    """A key prefix for a folder in a bucket: no leading slash, one trailing."""
    folder = (folder or "").strip().replace("\\", "/").lstrip("/")
    return f"{folder.rstrip('/')}/" if folder.strip("/") else ""


# ── This server ──────────────────────────────────────────────────────────


def local_folder(folder: str, data_dir: str) -> str:
    """The folder on this server. A relative folder is inside the data folder,
    as it is for every File step."""
    folder = (folder or "").strip()
    if not folder:
        raise ListingError("Choose the folder to watch.")
    path = folder if os.path.isabs(folder) else os.path.join(data_dir or ".", folder)
    return os.path.abspath(path)


def list_local(folder: str, data_dir: str) -> list[FoundFile]:
    root = local_folder(folder, data_dir)
    if not os.path.isdir(root):
        raise ListingError(f"The folder {folder} does not exist on this server.")
    found: list[FoundFile] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                # A link is left alone: it could lead anywhere on the server.
                if not entry.is_file(follow_symlinks=False):
                    continue
                info = entry.stat(follow_symlinks=False)
                found.append(FoundFile(
                    path=os.path.join(root, entry.name),
                    name=entry.name,
                    size=int(info.st_size),
                    modified=_utc(info.st_mtime),
                ))
                if len(found) >= MAX_FILES:
                    break
    except PermissionError as exc:
        raise ListingError(f"Flow may not read the folder {folder}.") from exc
    return found


# ── SFTP ─────────────────────────────────────────────────────────────────


def list_sftp(folder: str, config: dict[str, Any], *, client_factory: Callable | None = None) -> list[FoundFile]:
    """List a folder over SFTP, checking the server's host key as the SFTP
    steps do (see nodes/_sftp.py)."""
    host = str(config.get("host") or "").strip()
    if not host:
        raise ListingError("The SFTP connection has no host.")
    try:
        port = int(config.get("port") or 22)
    except (TypeError, ValueError):
        port = 22
    if port == 21:
        port = 22  # an FTP connection left at the FTP port but switched to SFTP
    username = str(config.get("username") or config.get("user") or "").strip()
    remote = (folder or config.get("remote_path") or ".").strip() or "."

    if client_factory is None:
        try:
            import paramiko  # noqa: F401
        except ImportError as exc:
            raise ListingError(
                "Watching an SFTP folder needs the paramiko package: pip install revoluzen-flow[sftp]"
            ) from exc
        from revoluzen_flow.nodes._sftp import build_ssh_client

        def client_factory(cfg):  # noqa: E306
            return build_ssh_client(cfg)

    ssh = client_factory(config)
    kwargs: dict[str, Any] = {"hostname": host, "port": port, "username": username, "timeout": TIMEOUT_SECONDS}
    private_key = config.get("private_key") or ""
    if private_key:
        kwargs["pkey"] = _private_key(private_key)
    elif config.get("password"):
        kwargs["password"] = config.get("password")
    try:
        ssh.connect(**kwargs)
        sftp = ssh.open_sftp()
        try:
            entries = sftp.listdir_attr(remote)
        finally:
            sftp.close()
    except ListingError:
        raise
    except FileNotFoundError as exc:
        raise ListingError(f"The folder {remote} does not exist on {host}.") from exc
    except Exception as exc:  # noqa: BLE001 — the server's own words are safe; credentials are not in them
        raise ListingError(f"Cannot list {remote} on {host}: {_reason(exc)}") from exc
    finally:
        try:
            ssh.close()
        except Exception:  # noqa: BLE001
            pass

    found: list[FoundFile] = []
    for entry in entries:
        mode = getattr(entry, "st_mode", None)
        if mode is not None and not stat.S_ISREG(mode):
            continue
        name = getattr(entry, "filename", "")
        if not name:
            continue
        found.append(FoundFile(
            path=posixpath.join(remote, name) if remote not in ("", ".") else name,
            name=name,
            size=int(getattr(entry, "st_size", 0) or 0),
            modified=_utc(getattr(entry, "st_mtime", None)),
        ))
        if len(found) >= MAX_FILES:
            break
    return found


def _private_key(text: str):
    from io import StringIO

    import paramiko

    for kind in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
        try:
            return kind.from_private_key(StringIO(str(text)))
        except Exception:  # noqa: BLE001 — try the next kind of key
            continue
    raise ListingError("The SFTP connection's private key could not be read.")


# ── A Windows file share ─────────────────────────────────────────────────


def list_smb(folder: str, config: dict[str, Any]) -> list[FoundFile]:
    """List a folder on a Windows file share (``connections/file_share.py``).

    The folder is read like the File step's path: inside the connection's
    folder, from the top of the share when it starts with ``\\``, or a full
    path on the connection's own share; empty is the connection's folder.
    Each file's path is its full ``\\\\server\\share\\...`` path. The File
    step reads it with the same connection and refuses it with a connection
    to any other share, so a run cannot read a file from the wrong place.
    """
    from revoluzen_flow.connections import file_share

    try:
        _where, files = file_share.list_files(config, folder, timeout=TIMEOUT_SECONDS, limit=MAX_FILES)
    except file_share.ShareError as exc:
        raise ListingError(str(exc)) from None
    return [
        FoundFile(path=f["path"], name=f["name"], size=int(f["size"] or 0), modified=_utc(f["modified"]))
        for f in files
    ]


def smb_in_use(path: str, config: dict[str, Any]) -> bool:
    """Whether a program still has the file on the share open to write it."""
    from revoluzen_flow.connections import file_share

    try:
        return file_share.in_use(config, path, timeout=TIMEOUT_SECONDS)
    except file_share.ShareError:
        return False  # the run reads the file and says what is wrong


# ── S3 and anything that speaks it ───────────────────────────────────────


def _s3_client(config: dict[str, Any], *, endpoint_url: str | None = None, access_key: str = "", secret_key: str = ""):
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise ListingError("Watching a bucket needs the boto3 package: pip install revoluzen-flow[aws]") from exc
    kwargs: dict[str, Any] = {
        "region_name": config.get("region") or config.get("region_name") or "us-east-1",
        "config": Config(connect_timeout=TIMEOUT_SECONDS, read_timeout=TIMEOUT_SECONDS, retries={"max_attempts": 2}),
    }
    endpoint = endpoint_url or config.get("endpoint_url") or config.get("endpoint")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    access = access_key or config.get("access_key") or config.get("access_key_id") or config.get("aws_access_key_id")
    secret = secret_key or config.get("secret_key") or config.get("secret_access_key") or config.get("aws_secret_access_key")
    if access and secret:
        kwargs["aws_access_key_id"] = access
        kwargs["aws_secret_access_key"] = secret
    return boto3.client("s3", **kwargs)


def list_s3(folder: str, config: dict[str, Any], *, client: Any = None, v1: bool = False) -> list[FoundFile]:
    bucket = str(config.get("bucket") or "").strip()
    if not bucket:
        raise ListingError("The connection names no bucket.")
    prefix = _prefix(folder)
    client = client or _s3_client(config)
    found: list[FoundFile] = []
    try:
        if v1:
            # Google Cloud Storage's S3-compatible API is sure to answer the
            # original listing call.
            pages = client.get_paginator("list_objects").paginate(Bucket=bucket, Prefix=prefix, Delimiter="/")
        else:
            pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, Delimiter="/")
        for page in pages:
            for item in page.get("Contents") or []:
                key = item.get("Key") or ""
                name = key[len(prefix):]
                if not name or name.endswith("/"):
                    continue
                found.append(FoundFile(
                    path=key,
                    name=name,
                    size=int(item.get("Size") or 0),
                    modified=_utc(item.get("LastModified")),
                    tag=str(item.get("ETag") or "").strip('"'),
                ))
                if len(found) >= MAX_FILES:
                    return found
    except ListingError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ListingError(f"Cannot list s3://{bucket}/{prefix}: {_reason(exc)}") from exc
    return found


# ── Google Cloud Storage ─────────────────────────────────────────────────


def list_gcs(folder: str, config: dict[str, Any], *, client: Any = None) -> list[FoundFile]:
    bucket = str(config.get("bucket") or "").strip()
    if not bucket:
        raise ListingError("The connection names no bucket.")
    key_id = config.get("hmac_key_id") or ""
    secret = config.get("hmac_secret") or ""
    if (config.get("auth_method") or ("hmac" if key_id else "service_account")) == "hmac" or (key_id and secret):
        if not (key_id and secret):
            raise ListingError("The connection's HMAC key is incomplete.")
        s3 = client
        if s3 is None:
            s3 = _s3_client(
                {"region": config.get("region") or "auto"},
                endpoint_url="https://storage.googleapis.com", access_key=key_id, secret_key=secret,
            )
            # boto3 asks for URL-encoded keys on every listing, a parameter
            # Google's S3-compatible API does not take. Keys then come back as
            # they are, which is what the pipeline needs anyway.
            try:
                from botocore.handlers import set_list_objects_encoding_type_url

                s3.meta.events.unregister(
                    "before-parameter-build.s3.ListObjects", set_list_objects_encoding_type_url,
                )
            except Exception:  # noqa: BLE001 — an older botocore without the hook
                pass
        return list_s3(folder, {**config, "bucket": bucket}, client=s3, v1=True)

    try:
        import json

        from google.cloud import storage  # type: ignore
        from google.oauth2 import service_account  # type: ignore
    except ImportError as exc:
        raise ListingError(
            "Watching a Google Cloud Storage folder with a service account needs the "
            "google-cloud-storage package (pip install revoluzen-flow[google]), or use HMAC keys."
        ) from exc
    prefix = _prefix(folder)
    try:
        if client is None:
            info = config.get("credentials_json")
            if isinstance(info, str) and info.strip():
                info = json.loads(info)
            credentials = service_account.Credentials.from_service_account_info(info) if info else None
            client = storage.Client(project=config.get("project_id") or None, credentials=credentials)
        found: list[FoundFile] = []
        for blob in client.list_blobs(bucket, prefix=prefix, delimiter="/", timeout=TIMEOUT_SECONDS):
            name = (blob.name or "")[len(prefix):]
            if not name or name.endswith("/"):
                continue
            found.append(FoundFile(
                path=blob.name, name=name, size=int(blob.size or 0),
                modified=_utc(blob.updated), tag=str(blob.generation or ""),
            ))
            if len(found) >= MAX_FILES:
                break
        return found
    except ListingError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ListingError(f"Cannot list gs://{bucket}/{prefix}: {_reason(exc)}") from exc


# ── Azure Blob Storage, over its REST API ────────────────────────────────

_AZURE_VERSION = "2021-08-06"


def _connection_string_parts(text: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for piece in (text or "").split(";"):
        if "=" in piece:
            key, _, value = piece.partition("=")
            parts[key.strip()] = value.strip()
    return parts


def _azure_account(config: dict[str, Any]) -> tuple[str, str, str, str]:
    """(account, endpoint, account key, SAS token) from the saved connection."""
    parts = _connection_string_parts(config.get("connection_string") or "")
    account = str(config.get("account_name") or parts.get("AccountName") or "").strip()
    endpoint = parts.get("BlobEndpoint") or ""
    if not endpoint and account:
        protocol = parts.get("DefaultEndpointsProtocol") or "https"
        suffix = parts.get("EndpointSuffix") or "core.windows.net"
        endpoint = f"{protocol}://{account}.blob.{suffix}"
    key = str(config.get("account_key") or parts.get("AccountKey") or "").strip()
    sas = str(config.get("sas_token") or parts.get("SharedAccessSignature") or "").strip().lstrip("?")
    method = (config.get("auth_method") or "").strip()
    if method == "sas_token":
        key = ""
    elif method == "account_key":
        sas = ""
    return account, endpoint.rstrip("/"), key, sas


def shared_key_authorization(
    *, account: str, key: str, method: str, container: str, query: dict[str, str], headers: dict[str, str],
    endpoint_path: str = "",
) -> str:
    """The ``SharedKey`` Authorization header for a Blob service request with no body.

    The string signed is Azure's: the verb, eleven standard headers (all empty
    here, as the request carries no body), the ``x-ms-`` headers in order, then
    the account, the resource's path and each query parameter on its own line.
    ``endpoint_path`` is the path part of an endpoint that has one, such as a
    storage emulator's ``/devstoreaccount1``.
    """
    canonical_headers = "".join(
        f"{name}:{value.strip()}\n"
        for name, value in sorted((k.lower(), v) for k, v in headers.items() if k.lower().startswith("x-ms-"))
    )
    canonical_resource = f"/{account}{endpoint_path.rstrip('/')}/{container}" + "".join(
        f"\n{name.lower()}:{value}" for name, value in sorted(query.items(), key=lambda kv: kv[0].lower())
    )
    to_sign = method + "\n" * 12 + canonical_headers + canonical_resource
    digest = hmac.new(base64.b64decode(key), to_sign.encode("utf-8"), hashlib.sha256).digest()
    return f"SharedKey {account}:{base64.b64encode(digest).decode('ascii')}"


def _azure_bearer(config: dict[str, Any], client) -> str:
    tenant = str(config.get("tenant_id") or "").strip()
    client_id = str(config.get("client_id") or "").strip()
    client_secret = str(config.get("client_secret") or "").strip()
    if not (tenant and client_id and client_secret):
        raise ListingError("The connection's service principal is incomplete.")
    reply = client.post(
        f"https://login.microsoftonline.com/{quote(tenant, safe='')}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret,
            "scope": "https://storage.azure.com/.default",
        },
    )
    if reply.status_code != 200:
        raise ListingError(f"Azure did not accept the service principal (HTTP {reply.status_code}).")
    token = (reply.json() or {}).get("access_token")
    if not token:
        raise ListingError("Azure sent no access token for the service principal.")
    return token


def list_azure_blob(folder: str, config: dict[str, Any], *, client: Any = None) -> list[FoundFile]:
    import httpx

    from revoluzen_flow.common.safe_xml import XmlError, local_name, parse_xml

    container = str(config.get("container") or "").strip()
    if not container:
        raise ListingError("The connection names no container.")
    account, endpoint, key, sas = _azure_account(config)
    if not endpoint:
        raise ListingError("The connection names no storage account.")
    service_principal = (config.get("auth_method") or "") == "service_principal"
    if not (key or sas or service_principal):
        raise ListingError("The connection has no account key, SAS token or service principal to sign in with.")

    prefix = _prefix(folder)
    owns_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False)
    found: list[FoundFile] = []
    marker = ""
    try:
        bearer = _azure_bearer(config, client) if service_principal else ""
        while True:
            query = {"restype": "container", "comp": "list", "delimiter": "/", "maxresults": "5000"}
            if prefix:
                query["prefix"] = prefix
            if marker:
                query["marker"] = marker
            headers = {"x-ms-date": formatdate(usegmt=True), "x-ms-version": _AZURE_VERSION}
            url_query = urlencode(query)
            if bearer:
                headers["Authorization"] = f"Bearer {bearer}"
            elif key and not sas:
                headers["Authorization"] = shared_key_authorization(
                    account=account, key=key, method="GET", container=container, query=query, headers=headers,
                    endpoint_path=urlsplit(endpoint).path,
                )
            else:
                sas_pairs = [p for p in parse_qsl(sas, keep_blank_values=True) if p[0] not in query]
                url_query = urlencode(list(query.items()) + sas_pairs)
            reply = client.get(f"{endpoint}/{quote(container, safe='')}?{url_query}", headers=headers)
            if reply.status_code != 200:
                raise ListingError(
                    f"Azure answered HTTP {reply.status_code} when listing {container}/{prefix}"
                    + (": the container does not exist." if reply.status_code == 404 else ".")
                )
            try:
                root = parse_xml(reply.content)
            except XmlError as exc:
                raise ListingError(f"Azure's listing could not be read: {exc}") from exc
            blobs = next((child for child in root if local_name(child.tag) == "Blobs"), None)
            for blob in list(blobs) if blobs is not None else []:
                if local_name(blob.tag) != "Blob":
                    continue
                fields = {local_name(child.tag): child for child in blob}
                name = (fields["Name"].text or "") if "Name" in fields else ""
                short = name[len(prefix):]
                if not short or short.endswith("/"):
                    continue
                props = {local_name(child.tag): (child.text or "") for child in fields.get("Properties", [])}
                modified = None
                if props.get("Last-Modified"):
                    try:
                        modified = parsedate_to_datetime(props["Last-Modified"])
                    except (TypeError, ValueError):
                        modified = None
                found.append(FoundFile(
                    path=name, name=short, size=int(props.get("Content-Length") or 0),
                    modified=_utc(modified), tag=props.get("Etag", "").strip('"'),
                ))
                if len(found) >= MAX_FILES:
                    return found
            next_marker = next((child.text or "" for child in root if local_name(child.tag) == "NextMarker"), "")
            if not next_marker:
                return found
            marker = next_marker
    except ListingError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ListingError(f"Cannot list {container}/{prefix} in Azure: {_reason(exc)}") from exc
    finally:
        if owns_client:
            client.close()


# ── One entry point ──────────────────────────────────────────────────────


def _reason(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text.splitlines()[0][:300]


def connection_config(app_state: dict, connection_id: str, workspace_id: str, location: str) -> dict[str, Any]:
    """The saved connection's settings, with its secrets readable, if it is in
    this workspace and of a kind this location can use."""
    if not connection_id:
        raise ListingError("Choose the connection to watch.")
    from revoluzen_flow.nodes.db_source import _get_connection_config

    resolved = _get_connection_config(connection_id, app_state, workspace_id=workspace_id)
    if not resolved:
        raise ListingError("The connection was not found in this workspace.")
    config, kind = resolved
    allowed = CONNECTION_TYPES.get(location, ())
    if kind not in allowed:
        raise ListingError(f"A {kind} connection cannot be used to watch {LOCATION_LABELS.get(location, location)}.")
    if location == "sftp" and kind == "ftp" and str(config.get("protocol") or "").lower() != "sftp":
        raise ListingError("This FTP connection does not use SFTP. Only SFTP folders can be watched.")
    return config


def list_folder(
    location: str,
    folder: str,
    *,
    app_state: dict,
    workspace_id: str,
    connection_id: str = "",
) -> list[FoundFile]:
    """The files directly in the folder. Raises :class:`ListingError`."""
    location = (location or "").strip().lower()
    if location == "local":
        return list_local(folder, app_state.get("data_dir") or "")
    config = connection_config(app_state, connection_id, workspace_id, location)
    if location == "sftp":
        return list_sftp(folder, config)
    if location == "smb":
        return list_smb(folder, config)
    if location == "s3":
        return list_s3(folder, config)
    if location == "gcs":
        return list_gcs(folder, config)
    if location == "azure_blob":
        return list_azure_blob(folder, config)
    raise ListingError(f"Flow cannot watch a folder of kind {location!r}.")


def still_being_written(
    location: str,
    path: str,
    *,
    app_state: dict,
    workspace_id: str,
    connection_id: str = "",
) -> bool:
    """Whether a file that looks complete is still open in the program writing
    it, where the location can tell.

    Only a Windows file share is asked. A Windows server's folder listing may
    show a file's final size and time while it is still being copied in, so
    "unchanged for a while" is not enough there; the File step would then find
    the file in use and the run would fail. Objects in S3, Google Cloud Storage
    and Azure appear only when complete, and an SFTP listing shows a file grow.
    """
    if (location or "").strip().lower() != "smb":
        return False
    config = connection_config(app_state, connection_id, workspace_id, "smb")
    return smb_in_use(path, config)
