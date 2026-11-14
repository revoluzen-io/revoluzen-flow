"""One connect path for warehouses and cloud storage (2026-11-11).

Browsing, Test connection and runs used to open these connections each their
own way, and they had drifted: BigQuery runs ignored the saved service account
key, Azure browsing read a field the form never saves, ClickHouse runs ignored
SSL, Redshift needed a driver no install ships. The drivers are replaced by
stand-ins here; nothing reaches a real service.
"""

from __future__ import annotations

import json
import sys

import httpx
import pytest

from revoluzen_flow.connections import warehouse_clients as wc
from revoluzen_flow.connections.tester import ConnectionTester


class _Recorder:
    """Callable that records its keyword arguments and returns a stand-in."""

    def __init__(self, result=None):
        self.calls: list[dict] = []
        self.result = result if result is not None else object()

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        return self.result


# ── Google ──────────────────────────────────────────────────────────────────


KEY = {"type": "service_account", "project_id": "key-project", "client_email": "x@y"}


def test_bigquery_client_uses_the_saved_key_and_location(monkeypatch):
    from google.cloud import bigquery
    from google.oauth2 import service_account

    made = _Recorder()
    monkeypatch.setattr(bigquery, "Client", made)
    monkeypatch.setattr(service_account.Credentials, "from_service_account_info",
                        staticmethod(lambda info: ("creds-for", info["project_id"])))
    wc.bigquery_client({"project": "p1", "credentials_json": json.dumps(KEY), "location": "EU"})
    call = made.calls[0]
    assert call["project"] == "p1"
    assert call["credentials"] == ("creds-for", "key-project")
    assert call["location"] == "EU"


def test_bigquery_client_takes_the_project_from_the_key_when_none_is_typed(monkeypatch):
    from google.cloud import bigquery
    from google.oauth2 import service_account

    made = _Recorder()
    monkeypatch.setattr(bigquery, "Client", made)
    monkeypatch.setattr(service_account.Credentials, "from_service_account_info", staticmethod(lambda info: "c"))
    wc.bigquery_client({"credentials_json": KEY})
    assert made.calls[0]["project"] == "key-project"


def test_a_key_that_is_not_json_is_refused_plainly():
    with pytest.raises(ValueError, match="not valid JSON"):
        wc.service_account_info({"credentials_json": "{not json"})


def test_gcs_client_leaves_the_project_out_when_unknown(monkeypatch):
    from google.cloud import storage

    made = _Recorder()
    monkeypatch.setattr(storage, "Client", made)
    wc.gcs_client({"bucket": "b"})
    assert "project" not in made.calls[0]
    assert made.calls[0]["credentials"] is None


def test_gcs_hmac_is_recognised():
    assert wc.gcs_uses_hmac({"auth_method": "hmac"})
    assert wc.gcs_uses_hmac({"hmac_key_id": "GOOG1"})
    assert not wc.gcs_uses_hmac({"auth_method": "service_account", "hmac_key_id": "GOOG1"})


# ── Redshift, ClickHouse, Databricks, Oracle, Snowflake ─────────────────────


def test_redshift_connects_through_psycopg2_when_its_own_driver_is_missing(monkeypatch):
    import psycopg2

    monkeypatch.setitem(sys.modules, "redshift_connector", None)
    made = _Recorder()
    monkeypatch.setattr(psycopg2, "connect", made)
    wc.redshift_connect({"host": "c.redshift.amazonaws.com", "database": "dev", "username": "u", "password": "p"})
    call = made.calls[0]
    assert call["port"] == 5439 and call["dbname"] == "dev" and call["user"] == "u"
    assert call["sslmode"] == "require"  # the form's SSL box is ticked by default

    wc.redshift_connect({"host": "h", "ssl": "false", "username": "u"})
    assert made.calls[1]["sslmode"] == "prefer"


def test_redshift_uses_its_own_driver_when_added(monkeypatch):
    fake = type(sys)("redshift_connector")
    fake.connect = _Recorder()
    monkeypatch.setitem(sys.modules, "redshift_connector", fake)
    wc.redshift_connect({"host": "h", "username": "u", "ssl": True})
    assert fake.connect.calls[0]["ssl"] is True


def test_clickhouse_honours_the_ssl_box(monkeypatch):
    import clickhouse_connect

    made = _Recorder()
    monkeypatch.setattr(clickhouse_connect, "get_client", made)
    wc.clickhouse_client({"host": "ch", "username": "reader", "ssl": "true"})
    call = made.calls[0]
    assert call["secure"] is True and call["port"] == 8443 and call["username"] == "reader"

    wc.clickhouse_client({"host": "ch", "port": "8123"})
    assert made.calls[1]["secure"] is False and made.calls[1]["port"] == 8123
    assert made.calls[1]["username"] == "default"


def test_databricks_strips_the_scheme_and_uses_catalog_and_schema(monkeypatch):
    import databricks.sql

    made = _Recorder()
    monkeypatch.setattr(databricks.sql, "connect", made)
    wc.databricks_connect({
        "host": "https://adb-1.azuredatabricks.net/", "http_path": "/sql/1.0/warehouses/abc",
        "token": "dapi1", "catalog": "main", "schema": "sales",
    })
    call = made.calls[0]
    assert call["server_hostname"] == "adb-1.azuredatabricks.net"
    assert call["access_token"] == "dapi1"
    assert call["catalog"] == "main" and call["schema"] == "sales"


def test_databricks_without_an_http_path_is_refused():
    with pytest.raises(ValueError, match="HTTP path"):
        wc.databricks_connect({"host": "h", "token": "t"})


def test_oracle_tries_the_name_as_a_sid_when_the_listener_has_no_such_service(monkeypatch):
    import oracledb

    dsns: list[str] = []

    def connect(**kwargs):
        dsns.append(kwargs["dsn"])
        if len(dsns) == 1:
            raise oracledb.DatabaseError("DPY-6001: cannot connect to database. Service \"ORCL\" is not registered")
        return "conn"

    monkeypatch.setattr(oracledb, "connect", connect)
    assert wc.oracle_connect({"host": "db", "service_name": "ORCL", "username": "u"}) == "conn"
    assert "SERVICE_NAME=ORCL" in dsns[0] and "SID=ORCL" in dsns[1]


def test_oracle_does_not_retry_other_errors(monkeypatch):
    import oracledb

    def connect(**kwargs):
        raise oracledb.DatabaseError("ORA-01017: invalid username/password")

    monkeypatch.setattr(oracledb, "connect", connect)
    with pytest.raises(oracledb.DatabaseError, match="ORA-01017"):
        wc.oracle_connect({"host": "db", "service_name": "ORCL"})


def test_snowflake_can_leave_the_warehouse_out(monkeypatch):
    import snowflake.connector

    made = _Recorder()
    monkeypatch.setattr(snowflake.connector, "connect", made)
    cfg = {"account": "acme-x1", "username": "u", "password": "p", "warehouse": "WH", "role": "R"}
    wc.snowflake_connect(cfg, use_warehouse=False)
    assert "warehouse" not in made.calls[0] and made.calls[0]["role"] == "R"
    wc.snowflake_connect(cfg)
    assert made.calls[1]["warehouse"] == "WH"


# ── Azure ───────────────────────────────────────────────────────────────────


class _FakeService:
    made: list = []

    def __init__(self, account_url=None, credential=None, **kwargs):
        self.account_url, self.credential = account_url, credential
        _FakeService.made.append(self)

    @classmethod
    def from_connection_string(cls, text):
        obj = cls(account_url="from-connection-string", credential=None)
        obj.connection_string = text
        return obj


@pytest.fixture
def fake_azure(monkeypatch):
    from azure.storage import blob, filedatalake

    _FakeService.made = []
    monkeypatch.setattr(blob, "BlobServiceClient", _FakeService)
    monkeypatch.setattr(filedatalake, "DataLakeServiceClient", _FakeService)
    return _FakeService


def test_azure_account_key_builds_the_address_from_the_account_name(fake_azure):
    service = wc.azure_blob_service({"account_name": "acct", "auth_method": "account_key", "account_key": "a2V5"})
    assert service.account_url == "https://acct.blob.core.windows.net"
    assert service.credential == {"account_name": "acct", "account_key": "a2V5"}


def test_azure_sas_and_connection_string(fake_azure):
    service = wc.azure_blob_service({"account_name": "acct", "auth_method": "sas_token", "sas_token": "?sv=1&sig=x"})
    assert service.credential == "sv=1&sig=x"
    service = wc.azure_blob_service({"auth_method": "connection_string",
                                     "connection_string": "DefaultEndpointsProtocol=https;AccountName=a;AccountKey=k"})
    assert service.account_url == "from-connection-string"


def test_azure_service_principal_and_missing_sign_in(fake_azure):
    service = wc.azure_blob_service({"account_name": "acct", "auth_method": "service_principal",
                                     "tenant_id": "t", "client_id": "c", "client_secret": "s"})
    assert isinstance(service.credential, wc.ServicePrincipalCredential)
    with pytest.raises(ValueError, match="no account key, SAS token or service principal"):
        wc.azure_blob_service({"account_name": "acct", "auth_method": "account_key"})


def test_adls_uses_the_dfs_address(fake_azure):
    service = wc.datalake_service({"account_name": "lake", "auth_method": "account_key", "account_key": "a2V5"})
    assert service.account_url == "https://lake.dfs.core.windows.net"


def test_service_principal_fetches_and_reuses_a_token(monkeypatch):
    posted: list[dict] = []

    def post(url, data=None, timeout=None):
        posted.append({"url": url, **data})
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})

    monkeypatch.setattr(httpx, "post", post)
    credential = wc.ServicePrincipalCredential("tenant-1", "client-1", "secret-1")
    assert credential.get_token("https://storage.azure.com/.default").token == "tok"
    credential.get_token("https://storage.azure.com/.default")
    assert len(posted) == 1
    assert posted[0]["url"].endswith("/tenant-1/oauth2/v2.0/token")
    assert posted[0]["scope"] == "https://storage.azure.com/.default"


# ── Test connection ─────────────────────────────────────────────────────────


def test_every_warehouse_and_storage_type_has_a_real_test():
    for conn_type in ("snowflake", "bigquery", "redshift", "databricks", "clickhouse", "oracle",
                      "synapse", "azure_blob", "adls_gen2", "gcs"):
        assert conn_type in ConnectionTester._TESTERS, conn_type


def test_databricks_test_asks_the_api_and_starts_nothing(monkeypatch):
    import databricks.sql

    def no_sql_session(**kwargs):
        raise AssertionError("a test must not open a SQL session")

    monkeypatch.setattr(databricks.sql, "connect", no_sql_session)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer dapi1"
        return httpx.Response(200, json={"name": "Shared", "state": "STOPPED"})

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))
    result = ConnectionTester().test_connection("databricks", {
        "host": "adb-1.azuredatabricks.net", "http_path": "/sql/1.0/warehouses/abc123", "token": "dapi1",
    })
    assert result["success"], result
    assert seen == ["https://adb-1.azuredatabricks.net/api/2.0/sql/warehouses/abc123"]
    assert "is stopped" in result["message"]


def test_databricks_test_reports_a_rejected_token(monkeypatch):
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real(
        *a, transport=httpx.MockTransport(lambda r: httpx.Response(403)), **k))
    result = ConnectionTester().test_connection("databricks", {
        "host": "h", "http_path": "/sql/1.0/warehouses/abc", "token": "bad"})
    assert not result["success"] and "access token" in result["message"]


def test_a_missing_driver_gives_the_add_on_message(monkeypatch):
    from revoluzen_flow.connections.addons import driver_message

    def missing(*args, **kwargs):
        raise RuntimeError(driver_message("Snowflake", "snowflake-connector-python"))

    monkeypatch.setattr(wc, "snowflake_connect", missing)
    result = ConnectionTester().test_connection("snowflake", {"account": "a", "username": "u"})
    assert not result["success"]
    assert "isn't available in this install" in result["message"]


def test_synapse_uses_the_sql_server_test_with_its_own_name(monkeypatch):
    monkeypatch.setattr(ConnectionTester, "_test_mssql", lambda self, cfg: {
        "success": True, "message": "Connected to SQL Server at ws.sql.azuresynapse.net:1433/pool",
        "details": {}, "error": None, "suggestion": None})
    result = ConnectionTester().test_connection("synapse", {"host": "ws.sql.azuresynapse.net"})
    assert result["message"] == "Connected to Azure Synapse at ws.sql.azuresynapse.net:1433/pool"


def test_bigquery_test_lists_one_dataset_and_checks_the_default_one(monkeypatch):
    calls: list = []

    class FakeClient:
        project = "p1"

        def list_datasets(self, max_results=None, timeout=None):
            calls.append(("list", max_results))
            return iter([object()])

        def get_dataset(self, name, timeout=None):
            calls.append(("get", name))

    monkeypatch.setattr(wc, "bigquery_client", lambda cfg: FakeClient())
    result = ConnectionTester().test_connection("bigquery", {"project": "p1", "dataset": "analytics"})
    assert result["success"], result
    assert calls == [("list", 1), ("get", "analytics")]


def test_azure_blob_test_reports_a_missing_container(monkeypatch):
    class ResourceNotFoundError(Exception):
        pass

    class Service:
        def get_container_client(self, name):
            class Container:
                def list_blobs(self, **kwargs):
                    raise ResourceNotFoundError("ContainerNotFound")
            return Container()

    monkeypatch.setattr(wc, "azure_blob_service", lambda cfg: Service())
    result = ConnectionTester().test_connection("azure_blob", {"account_name": "a", "container": "raw"})
    assert not result["success"] and "container raw was not found" in result["message"]


# ── Browsing and runs use the same code ─────────────────────────────────────


def test_azure_browsing_works_with_an_account_key(fake_azure, monkeypatch):
    from revoluzen_flow.connections.catalog_extensions import _azure_blob_catalog

    class Blob:
        def __init__(self, name):
            self.name = name

    class Container:
        def walk_blobs(self, name_starts_with="", delimiter="/"):
            return [Blob("orders.csv"), Blob("2026/")]

    monkeypatch.setattr(_FakeService, "get_container_client", lambda self, name: Container(), raising=False)
    catalog = _azure_blob_catalog({"account_name": "acct", "container": "raw",
                                   "auth_method": "account_key", "account_key": "a2V5"})
    names = [item.name for item in catalog.items]
    assert names == ["orders.csv", "2026/"]


def test_clickhouse_browsing_uses_the_http_client(monkeypatch):
    from revoluzen_flow.connections.catalog import _clickhouse_catalog

    class Result:
        result_rows = [("default", "events", "MergeTree"), ("default", "daily", "View")]

    class Client:
        closed = False

        def query(self, sql):
            return Result()

        def close(self):
            Client.closed = True

    monkeypatch.setattr(wc, "clickhouse_client", lambda cfg, timeout=None: Client())
    catalog = _clickhouse_catalog({"host": "ch", "port": "8123"})
    assert [(i.name, i.kind) for i in catalog.items] == [("events", "table"), ("daily", "view")]
    assert Client.closed


def test_redshift_reads_without_its_own_driver(monkeypatch):
    import psycopg2

    from revoluzen_flow.connectors.jdbc_dialects import get_dialect

    class Cursor:
        description = [("id",), ("name",)]

        def execute(self, sql):
            self.sql = sql

        def fetchall(self):
            return [(1, "a")]

    class Conn:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "redshift_connector", None)
    monkeypatch.setattr(psycopg2, "connect", lambda **kwargs: Conn())
    cols, rows = get_dialect("redshift").reader({"host": "h", "username": "u"}, None, "public.t", None)
    assert cols == ["id", "name"] and rows == [(1, "a")]


def test_bigquery_reads_with_the_saved_key(monkeypatch):
    from revoluzen_flow.connectors.jdbc_dialects import get_dialect

    seen: list = []

    class Field:
        def __init__(self, name):
            self.name = name

    class Job:
        schema = [Field("n")]

        def result(self):
            return [{"n": 1}]

    class Client:
        project = "p1"

        def query(self, sql):
            seen.append(sql)
            return Job()

    monkeypatch.setattr(wc, "bigquery_client", lambda cfg: seen.append(cfg.get("credentials_json")) or Client())
    cols, rows = get_dialect("bigquery").reader({"credentials_json": "{}", "dataset": "d"}, None, "t", 5)
    assert seen[0] == "{}"
    assert "`p1.d.t`" in seen[1]
    assert cols == ["n"] and rows == [(1,)]


def test_database_source_no_longer_offers_cdc():
    from revoluzen_flow.nodes.db_source import DbSourceNode

    sync = next(p for p in DbSourceNode.param_schema() if p["name"] == "sync_mode")
    assert sync["options"] == ["full_refresh", "incremental"]
