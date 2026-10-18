# Changelog

All notable changes to Revoluzen Flow are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

API stability policy: see `docs/api.md`. The HTTP API surface under `/api/*`
follows the compatibility rules documented there.

## [Unreleased]

## [1.0.1] — 2026-10-18

First release published as **Revoluzen Flow**, by Revoluzen Technologies Pvt Ltd.

The engine, connectors and UI are unchanged from the 1.0.x line; this release
renames the project and its published artifacts.

### Changed

- Distribution is now `revoluzen-flow` on PyPI. The CLI entry point is
  `revoluzen` (alias `rz`); `python -m revoluzen_flow` works as well.
- Python package is `revoluzen_flow`.
- Environment variables use the `REVOLUZEN_FLOW_` prefix, and the config
  directory is `.revoluzen-flow`.
- Docker image is `revoluzen/flow`; the Compose service is `flow` and the
  data volume is `revoluzen_flow_data`.
- OpenLineage events are emitted with the `revoluzen-flow` namespace and a
  producer URI pointing at this repository.

### Added

- `scripts/check_repo_coordinates.py`, run by the `architecture-invariants`
  CI job. It fails the build when a self-referential GitHub slug disagrees
  with `backend/revoluzen_flow/app_meta.py`, or when documentation tells a
  reader to `cd` into a directory that `git clone` does not create.

### Notes for existing installs

There is no automatic migration. An instance created before this release
keeps its own data directory and environment variables; point the new
`REVOLUZEN_FLOW_DATA_DIR` at the existing data directory to carry it over.

Some identifiers deliberately keep their original spelling, because changing
them would break stored data rather than text: the `fpulseNode` node type
recorded inside saved workflows, the `fpulse.db` SQLite filename, session
cookie names, and Prometheus metric names.
