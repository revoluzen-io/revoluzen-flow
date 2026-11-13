# Database drivers

Most connectors work as soon as Revoluzen Flow is installed: their Python
drivers ship with it (`backend/requirements.txt`, mirrored in the
`pyproject.toml` dependencies). A few need something more:

* **An add-on driver.** Db2 and Informix, SAP HANA, Teradata, Kafka, Trino and
  Presto use drivers that come under the vendors' own licences or are native
  code, so a standard install leaves them out. An admin adds one on
  **Settings > Add-ons** (below). Until then the New Connection page marks the
  connector "Needs an add-on".
* **A driver installed on the computer itself.** SQL Server, Azure SQL and
  Azure Synapse need Microsoft's ODBC driver. Oracle needs Oracle Instant
  Client only in thick mode. See the sections further down.
* **Any other database** that has an ODBC driver: install the vendor's ODBC
  driver on the computer running Revoluzen Flow and use the **ODBC database**
  connection (below).

## What ships, and what does not

| Database or service | Driver ships with Revoluzen Flow? | Also needed on the computer |
|---|---|---|
| PostgreSQL, MySQL / MariaDB, SQLite | Yes | No |
| SQL Server / Azure SQL / Azure Synapse | Yes (pyodbc) | **Microsoft ODBC Driver 17 or 18** |
| Oracle | Yes (oracledb, thin mode) | No (Instant Client only for thick mode) |
| Snowflake, BigQuery, Databricks, ClickHouse | Yes | No |
| Amazon Redshift | Yes (psycopg2; Redshift speaks PostgreSQL's protocol) | No |
| MongoDB, Redis | Yes | No |
| Elasticsearch / OpenSearch | No driver needed (plain HTTP) | No |
| S3 / MinIO, Azure Blob, ADLS Gen2, Google Cloud Storage | Yes | No |
| SFTP, Windows file shares (SMB), Delta Lake | Yes | No |
| IBM Db2 / Informix | **Add-on** (`ibm_db`) | Sometimes, see below |
| SAP HANA | **Add-on** (`hdbcli`) | Sometimes, see below |
| Teradata | **Add-on** (`teradatasql`) | No |
| Kafka | **Add-on** (`confluent-kafka`) | No |
| Trino / Presto (browsing only) | **Add-on** (`trino` / `presto-python-client`) | No |
| Any other database with an ODBC driver | Yes (pyodbc) | That database's ODBC driver |

`redshift_connector` is used for Redshift instead of psycopg2 when it is installed.

## Adding a driver: Settings > Add-ons

An admin opens **Settings > Add-ons**, reads the vendor's licence terms for
the driver (each card links to them), ticks that they accept them, and clicks
**Install**. The connector works straight away, with no restart.

* Only the drivers listed there can be installed, each under a fixed package
  name. Installing runs `pip` with `--only-binary=:all:`, so no build scripts run.
* Drivers go into the data folder, under
  `addons/py<version>-<system>-<machine>` (for example
  `addons/py311-linux-x86_64`). They stay there across upgrades, and a data
  folder moved to another kind of computer does not load drivers built for the
  old one.
* That folder is searched **after** everything Revoluzen Flow ships, so an
  add-on can never replace one of its packages.
* Installs come from the package index pip is set up to use. A company mirror
  works through pip's usual settings (`PIP_INDEX_URL` and so on).
* **No internet on the server?** Download the driver's `.whl` files (and any it
  needs) on another computer, for the server's operating system and Python
  version. Then click **Upload wheel files…** and **Install from uploaded files**.
* Every install is written to the audit log.
* To remove a driver, delete its folder under `addons/` and restart.
* To turn installing off on a locked-down server, set
  `REVOLUZEN_FLOW_ADDON_INSTALLS=off`. The screen then only shows what is installed.

With pip you can also install a driver into Revoluzen Flow's own environment:
`pip install "revoluzen-flow[db2]"`, `[hana]`, `[teradata]` or `[kafka]`.

## ODBC database (any database with an ODBC driver)

1. Install the database vendor's ODBC driver on the computer running
   Revoluzen Flow (on Linux, the driver and unixODBC).
2. Add an **ODBC database** connection:
   * **ODBC Driver**: the driver's name as the computer lists it, for example
     `IBM INFORMIX ODBC DRIVER`. If the name is wrong, **Test** lists the
     drivers the computer has. Alternatively, give a data source name (DSN) set
     up on that computer.
   * **More Settings**: anything else the driver needs, as `name=value` pairs
     separated by semicolons, for example `Server=db1;Port=9088;Database=sales`.
   * **Username** and **Password**: put them here, not in More Settings. They
     are stored encrypted and passed to the driver escaped.
3. **Test** connects and lists the tables. Browsing, the Database Source (query
   or table) and the Execute SQL step all work through the driver.

An ODBC database is **read only** as a pipeline destination: there is no one
way to create a table or load rows that every ODBC database accepts. Write to
it with an Execute SQL step instead. A sample run reads the first rows and stops.

## Docker

The image has unixODBC, so the ODBC database connection works with any ODBC
driver added to the image. It also has the DuckDB extensions pipelines use
for cloud files (`httpfs`, `azure`, `spatial`), so they load without internet
access.

Microsoft's ODBC driver for SQL Server comes under Microsoft's licence terms,
so the image leaves it out unless you build with it and accept those terms:

```bash
docker build --build-arg INSTALL_MSODBCSQL=yes -t revoluzen-flow .
```

Add-on drivers installed on Settings > Add-ons live in the data volume
(`/data/addons/...`), so they survive upgrading the image. To build them into an
image of your own instead:

```dockerfile
FROM revoluzen-flow
# Installs into the image's own environment, next to what it ships.
RUN python -m pip install --no-cache-dir --user hdbcli teradatasql
```

## What "OS-level driver" means

Some database client libraries are thin Python wrappers around a
native shared library. The Python wheel installs the wrapper; the
shared library has to be installed at the OS level, usually via the
system package manager.

If you skip the OS install, `pip install` succeeds but the connection
fails at runtime with an error like `Can't open lib 'ODBC Driver 17
for SQL Server' : file not found`. This page tells you the OS install
to pair with each `pip install`.

---

## MS SQL Server / Azure SQL / Synapse / Fabric SQL endpoint

Python install:
```bash
pip install revoluzen-flow[mssql]
```

OS-level driver — **Microsoft ODBC Driver 17 or 18 for SQL Server**:

### Windows
Download + install: <https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server>

Quick check:
```powershell
odbcad32.exe   # GUI confirms the driver appears under "Drivers" tab
```

### Linux (Ubuntu / Debian)
```bash
curl https://packages.microsoft.com/keys/microsoft.asc | sudo tee /etc/apt/trusted.gpg.d/microsoft.asc
curl https://packages.microsoft.com/config/ubuntu/22.04/prod.list | sudo tee /etc/apt/sources.list.d/mssql-release.list
sudo apt-get update
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev
```

### macOS
```bash
brew tap microsoft/mssql-release https://github.com/Microsoft/homebrew-mssql-release
brew update
HOMEBREW_ACCEPT_EULA=Y brew install msodbcsql18
```

### Verify
```bash
python -c "import pyodbc; print([d for d in pyodbc.drivers() if 'SQL Server' in d])"
```
Expected output includes `ODBC Driver 17 for SQL Server` or
`ODBC Driver 18 for SQL Server`.

---

## Oracle Database

Python install:
```bash
pip install revoluzen-flow[oracle]
```

The `oracledb` library has **two modes**:

### Thin mode (default, no extra install)
Pure Python — no Oracle Instant Client required. Supports the common
case: read/write rows, parameterised queries, basic types. **This is
what Revoluzen Flow uses unless you explicitly switch to thick.** No further
action needed.

### Thick mode (for AQ, sharding, advanced types)
Requires **Oracle Instant Client** installed at the OS level + a
config flag in the connection.

#### Windows
1. Download Oracle Instant Client Basic Lite (smallest, ~50 MB) from
   <https://www.oracle.com/database/technologies/instant-client/winx64-64-downloads.html>
2. Unzip to e.g. `C:\oracle\instantclient_21_13`
3. Add that folder to `PATH`:
   ```powershell
   [Environment]::SetEnvironmentVariable("PATH", "C:\oracle\instantclient_21_13;$env:PATH", "User")
   ```

#### Linux
```bash
# Ubuntu/Debian
sudo apt-get install libaio1   # transitive C dep
# Download Instant Client Basic Lite RPM/ZIP from Oracle
sudo unzip instantclient-basiclite-linux.x64-21.13.0.0.0dbru.zip -d /opt/oracle
echo /opt/oracle/instantclient_21_13 | sudo tee /etc/ld.so.conf.d/oracle.conf
sudo ldconfig
```

#### Tell `oracledb` to use thick mode
In your Revoluzen Flow connection JSON or via env var before starting the
backend:
```bash
export ORACLE_THICK_MODE=1
```
(Revoluzen Flow's `connections/catalog_extensions.py` honours this and calls
`oracledb.init_oracle_client()` once at startup.)

### Verify
```bash
python -c "import oracledb; oracledb.connect('user/pwd@localhost:1521/free')"
```

---

## IBM Db2 / Informix

Python install:
```bash
pip install revoluzen-flow[db2]
```

OS-level driver — bundled with `ibm_db` on most platforms; **manual
install required on Linux ARM64 and recent Apple Silicon macOS**.

### Windows / Linux x86_64
`pip install` is usually sufficient — the wheel bundles the CLI driver.

### macOS (Apple Silicon) / Linux ARM64
Download IBM Data Server Driver Package (DSDriver):
<https://www.ibm.com/support/pages/download-initial-version-11510-clients-and-drivers>

Set environment:
```bash
export IBM_DB_HOME=/opt/ibm/dsdriver
export DYLD_LIBRARY_PATH=$IBM_DB_HOME/lib:$DYLD_LIBRARY_PATH   # macOS
export LD_LIBRARY_PATH=$IBM_DB_HOME/lib:$LD_LIBRARY_PATH       # Linux
```

---

## SAP HANA

Python install:
```bash
pip install revoluzen-flow[hana]
```

The `hdbcli` wheel is self-contained on Windows + modern Linux +
macOS. On glibc < 2.27 systems (older CentOS / RHEL 7), you'll need
to install the SAP HANA Client manually — see SAP Note 2399566.

---

## Snowflake

Python install:
```bash
pip install revoluzen-flow[snowflake]
```

No OS-level driver. The wheel includes pure-Python + C extensions.

Optional perf bonus: install Apache Arrow extras for faster fetch:
```bash
pip install "snowflake-connector-python[pandas]"
```

---

## Google BigQuery

Python install:
```bash
pip install revoluzen-flow[bigquery]
```

No OS-level driver. Authentication is via a service-account JSON key
file (or Application Default Credentials when running on GCE / Cloud
Run / GKE).

---

## Databricks SQL

Python install:
```bash
pip install revoluzen-flow[databricks]
```

No OS-level driver. Authentication is via Personal Access Token (PAT)
or OAuth M2M, both passed in the Revoluzen Flow connection config.

---

## ClickHouse

Ships with Revoluzen Flow (`clickhouse-connect`). Reading, writing, browsing
and Test connection all use ClickHouse's HTTP interface: port 8123, or 8443
with **Use SSL** ticked.

---

## Convenience: install everything at once

For a dev laptop where you want every connector to "just work":

```bash
# All databases where pip-only works (no OS drivers needed)
pip install revoluzen-flow[all-databases-no-os-deps]

# All databases + cloud SDKs (AWS, Azure, GCP)
pip install revoluzen-flow[all]
```

Then add MSSQL / Oracle (thick) / Db2 separately if you need them —
those each require the OS-level driver install documented above.

---

## Troubleshooting

| Error | Likely cause | Fix |
|---|---|---|
| "The X connector needs an add-on that isn't included in this Revoluzen Flow install" | The driver is an add-on, or a shipped driver was removed | An admin installs it on Settings > Add-ons, or reinstall Revoluzen Flow |
| "The ODBC driver or data source ... isn't set up on the computer" | The ODBC Driver name does not match an installed driver | Use one of the names the message lists, or install the driver |
| `Can't open lib 'ODBC Driver 17 for SQL Server'` | Python wheel installed; OS driver missing | Install Microsoft ODBC Driver (see above) |
| `DPI-1047: Cannot locate a 64-bit Oracle Client library` | `ORACLE_THICK_MODE=1` set but Instant Client missing | Install Instant Client + add to PATH/`LD_LIBRARY_PATH` |
| `cffi.api.CDefError: ...` on Linux ARM64 (db2) | ARM64 wheel needs DSDriver | Install DSDriver, set `IBM_DB_HOME` |
| `RuntimeError: snowflake-connector-python ... requires Python 3.8+` | Older Python on host | Upgrade to Python 3.11 (Revoluzen Flow's recommended runtime) |

If the connection still fails after the right install, open an issue
with the full traceback + your OS / Python version — the team will
add the case to this page.
