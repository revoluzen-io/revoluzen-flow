# Contributing to Revoluzen Flow

Thanks for your interest. This guide covers how to get set up and our
conventions — and, up front, what we can and can't accept right now.

## Code contributions are paused

**We are not merging external pull requests yet.** Not because we don't want
them — because accepting one requires a Contributor License Agreement, and
ours ([CLA.md](CLA.md)) is still a draft awaiting legal review. Asking you to
sign an unreviewed agreement would be unfair to you, and merging without one
would leave the provenance of your code unclear. Neither is acceptable, so we
wait.

This is a paperwork problem, not a judgement about your code, and it is the
whole of the reason.

**How you'll know when it changes.** We'll delete this section and say so in
the [CHANGELOG](CHANGELOG.md) under the release that carries it. To be told
without checking back, use **Watch → Custom → Releases** on the repository.
We won't announce a date we aren't certain of; [CLA.md](CLA.md) lists exactly
what is still outstanding, so you can judge for yourself how close it is.

**Everything else is open, and genuinely useful:**

- **Bug reports** — see below. These are the most valuable thing you can send
  us today.
- **Connector and node requests** — [open an
  issue](https://github.com/revoluzen-io/revoluzen-flow/issues/new/choose).
- **Discussion, questions, design feedback** — very welcome.
- **Forks** — Revoluzen Flow is Apache 2.0. Fork it, change it, run it, ship it. The
  CLA is only about contributing *back* upstream.

If you've already written a fix, please open an issue describing it and link
your branch. We'll pick it up when the CLA lands, and credit you.

Why a CLA at all: it grants Revoluzen the right to relicense
contributions, including into commercial Revoluzen Flow Enterprise features. That's a real ask,
and it deserves a real agreement rather than a draft with a TODO list in it.

## Reporting bugs

Open an issue on GitHub with:

- Revoluzen Flow version (`revoluzen --version`)
- OS and Python version
- Minimal reproduction steps
- Actual vs expected behavior

Security vulnerabilities go through [SECURITY.md](SECURITY.md), not public issues.

## Proposing features

Open a GitHub Discussion first for anything non-trivial — we'd rather agree
on the approach before you spend hours on a PR. For small fixes, a PR is fine.

## Setting up the dev environment

```bash
TODO: dev setup instructions land here in Phase 2 of the repo split
# git clone, python venv, install deps, run tests
```

### Database-dialect tests

Most of the suite runs against SQLite and DuckDB. The database sink also
targets Postgres, MySQL and SQL Server, and those differ in ways SQLite
cannot reveal — identifier quoting and type compatibility above all.
`backend/tests/test_db_sink_merge_dialects.py` runs the sink's merge contract
against real servers and **skips** unless you point it at one:

```bash
docker run -d --name rflow-pg-test    -e POSTGRES_PASSWORD=flowtest \
    -e POSTGRES_DB=flowtest -p 55432:5432 postgres:16-alpine
docker run -d --name rflow-mysql-test -e MYSQL_ROOT_PASSWORD=flowtest \
    -e MYSQL_DATABASE=flowtest -p 53306:3306 mysql:8
docker run -d --name rflow-mssql-test -e 'ACCEPT_EULA=1' \
    -e 'MSSQL_SA_PASSWORD=FlowTest!2026x' -p 51433:1433 \
    mcr.microsoft.com/azure-sql-edge:latest

export REVOLUZEN_FLOW_TEST_PG=postgresql://postgres:flowtest@127.0.0.1:55432/flowtest
export REVOLUZEN_FLOW_TEST_MYSQL=mysql://root:flowtest@127.0.0.1:53306/flowtest
export REVOLUZEN_FLOW_TEST_MSSQL=mssql://sa:FlowTest!2026x@127.0.0.1:51433/master

cd backend
pytest tests/test_db_sink_merge_dialects.py -m external
```

SQL Server needs the Microsoft ODBC driver installed at OS level — pyodbc on
its own is not enough. The recipe uses `azure-sql-edge` because
`mcr.microsoft.com/mssql/server` refuses to start on a Docker VM under
2000 MB, which is the default on Windows where WSL2 is commonly capped at
2 GB; CI has the headroom and runs the full server image.

`-m external` is required — `pytest.ini` deselects that marker by default, so
without it the file collects and runs nothing.

CI runs this too (the `backend-pytest-dialects` job in `ci.yml`), with
Postgres and MySQL as service containers, so you do not have to remember to.
That job treats a **skip as a failure**: the tests skip themselves when a
server is unreachable, which is right on a laptop and worthless in CI, where
it would turn "the database was never tested" into a green tick.

Worth the setup. The first run against each new engine found a real bug:

- **MySQL** — the sink quoted every identifier with `"`, which MySQL reads as
  a string literal, so create, append *and* truncate were all syntax errors.
  It had never worked against MySQL, and a SQLite-only suite had stayed green
  for as long as the sink had existed.
- **SQL Server** — the untyped column fallback created `TEXT` columns, and
  SQL Server's deprecated `TEXT` cannot be used with `=` at all, so a merge's
  keyed `DELETE` failed with *"the data types text and nvarchar are
  incompatible in the equal to operator"*. It also pinned "ODBC Driver 17" in
  a hand-built connection string, so it could not connect on a host carrying
  only Driver 18 — which is what CI and current Linux images install.

## Conventions

- Python 3.11+, type hints required on public functions
- `ruff` for lint, `pytest` for tests
- One feature per PR; rebase on `main` before requesting review
- Tests required for all behavior changes
- No new dependencies without discussion

## What goes in Revoluzen Flow vs Revoluzen Flow Enterprise

This repository contains the open-source core. Commercial features
(team workspaces, RBAC enforcement, approval gates, audit, vault,
sandbox, SLA support) live in a separate proprietary repository and
are NOT accepted here.

If you're unsure where a feature belongs, ask in a GitHub Discussion first.
The current free / commercial split is documented in [edition-matrix.md](edition-matrix.md).

## Contributing a connector

Connectors are JSON manifests, not code. The contribution path:

1. Read [community-connectors/readme.md](community-connectors/readme.md).
2. Drop your manifest in `community-connectors/`.
3. Run `python -m revoluzen_flow.connectors.certify <connector_id>` locally — the
   PR cannot merge until it passes.
4. Open a PR using the connector template:
   add `?template=connector.md` to the compare URL.

Manifests live in `community-connectors/` until a maintainer promotes them
into the built-in catalog at `backend/revoluzen_flow/connectors/manifests/`.
Custom transform code is not yet accepted via this path — open a
Discussion first if your connector needs more than declarative config.

## Code of conduct

All participation is governed by [code-of-conduct.md](CODE_OF_CONDUCT.md).
