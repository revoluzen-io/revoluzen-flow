# Contributing to Revoluzen Flow

Thanks for your interest. This guide covers how to get set up and our
conventions — and, up front, what we can and can't accept right now.

## Code contributions are paused for v1.0.0

**We are not merging external pull requests yet.** Not because we don't want
them — because accepting one requires a Contributor License Agreement, and
ours ([CLA.md](CLA.md)) is still a draft awaiting legal review. Asking you to
sign an unreviewed agreement would be unfair to you, and merging without one
would leave the provenance of your code unclear. Neither is acceptable, so we
wait.

We'll remove this notice as soon as the CLA is final, and we'll say so in the
CHANGELOG.

**Everything else is open, and genuinely useful:**

- **Bug reports** — see below. These are the most valuable thing you can send
  us today.
- **Connector and node requests** — [open an
  issue](https://github.com/revoluzen-data-systems/revoluzen-flow/issues/new/choose).
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

Security vulnerabilities go through [security.md](SECURITY.md), not public issues.

## Proposing features

Open a GitHub Discussion first for anything non-trivial — we'd rather agree
on the approach before you spend hours on a PR. For small fixes, a PR is fine.

## Setting up the dev environment

```bash
TODO: dev setup instructions land here in Phase 2 of the repo split
# git clone, python venv, install deps, run tests
```

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
