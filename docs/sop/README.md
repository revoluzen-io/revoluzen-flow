# Standard operating procedures

Operating procedures for Revoluzen Flow, maintained by Revoluzen Private
Limited.

These describe **what the project does** — how we triage, respond, release and
launch. They are deliberately separate from the documentation that tells a user
or a contributor what *they* should do, which already exists and is linked
below rather than repeated here.

## If you run Revoluzen Flow

**[user-operations.md](user-operations.md)** — install, first run, back up,
restore, upgrade, diagnose a failed run, size the host, report a problem.

New to Revoluzen Flow? Read the **[User Guide](../user-guide.md)** first — it
explains the concepts and walks you from install to a scheduled, verified
pipeline. This SOP is the checklist you come back to afterwards.

Reference material it builds on:

- [Quickstart](../quickstart.md) — install and first run
- [FAQ](../faq.md) and [Customer FAQ](../customer-faq.md)
- [Deployment](../deployment.md) — running it properly
- [Architecture](../architecture.md) — how it fits together
- [`SECURITY.md`](../../SECURITY.md) — reporting a vulnerability
- [`PRIVACY.md`](../../PRIVACY.md) — what the software does and does not send

## If you develop it

**[developer-operations.md](developer-operations.md)** — set up, run locally,
the three test suites, staging the UI, verifying a change, building a wheel,
adding a node or connector, and the conventions that fail the build.

- [`CONTRIBUTING.md`](../../CONTRIBUTING.md) — **read this first.** It states
  the current position on external pull requests, which may be that they are
  paused.
- [Developer guide](../dev-guide.md) — how the system is put together
- [`CODE_OF_CONDUCT.md`](../../CODE_OF_CONDUCT.md)
- [`CLA.md`](../../CLA.md) — the contributor licence agreement and its status
- [Connector authoring](../connector-authoring.md) — adding a connector

## If you maintain or release it

| Document | When you need it |
| --- | --- |
| [go-live.md](go-live.md) | Taking the repository public and publishing to PyPI. Read the blockers first. |
| [maintainer-operations.md](maintainer-operations.md) | Day to day: issue triage, pull requests, support questions, releases, dependencies. |
| [security-response.md](security-response.md) | A security report has arrived. Our side of the policy in `SECURITY.md`. |
| [`RELEASING.md`](../../RELEASING.md) | The release mechanics themselves. |

## How these differ from the rest of the docs

The reference documentation explains what Revoluzen Flow is and how it is put
together. These are procedural: for a given task, the steps in order, and how
to tell whether each one worked. Where they overlap, the reference material is
the explanation and the SOP is the checklist.

---

## A note on scope

Anything in this directory is public, because the repository is public. Keep
out of it:

- names on a duty rota
- who holds publishing credentials for PyPI or a registry
- billing and account details
- anything that is a control rather than a procedure

Those belong in the company's internal records. A procedure can say "the duty
maintainer acknowledges within three business days" in public; the roster of who
that is on a given week should not be.
