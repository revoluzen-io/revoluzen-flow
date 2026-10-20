# Revoluzen Flow 1.0.0 — release notes

Draft text for the GitHub release tag + announcement post. Keep this
short, sober, and verifiable. No marketing absolutes. Every claim
maps to shipped code.

---

## Revoluzen Flow 1.0.0 — local-first data pipeline engine

**One command. Loopback by default. Your data stays on your host.**

Revoluzen Flow 1.0 is the first stable release of a single-binary,
local-first data pipeline engine. It's designed for the data
engineer who wants the productivity of a real pipeline orchestrator
without the operational cost of Docker, Kubernetes, or a managed
SaaS bill.

### What's in 1.0

- **`revoluzen open`** — one-command launch. Backend boots on a free
  port (defaults to 8001, falls back if taken), browser opens
  automatically. Detects WSL / Docker / SSH / headless Linux and
  falls back to a printed URL.
- **Loopback by default.** Backend binds to `127.0.0.1`; LAN
  exposure requires explicit `REVOLUZEN_FLOW_BIND_HOST=0.0.0.0` or
  `REVOLUZEN_FLOW_ALLOW_LAN=1` opt-in. DNS-rebinding defense via Host
  header allowlist + Origin/Referer pinning. The hardening rationale
  is in [`docs/install/security-hardening.md`](../install/security-hardening.md).
- **DuckDB-powered execution.** Vectorised in-process transforms.
  Joins, group-bys, pivots, aggregates run column-at-a-time. Spills
  to disk for bigger-than-RAM datasets.
- **40 node types** across sources, transforms, sinks, control flow,
  AI assistance, and operational utilities.
- **33 first-party connectors visible by default** — 4 database
  dialects (PostgreSQL / MySQL / MS SQL Server / SQLite) + 2
  bulk-load dialects (Postgres `COPY FROM STDIN`, Snowflake
  `PUT` + `COPY INTO`) + 27 SaaS REST manifests. 10 additional
  consumer-marketing / SMB-CRM manifests ship Hidden by tier flag.
- **5-tier honest connector classification** —
  Production / Verified / Beta / Experimental / Hidden, reported
  **per connector**, never as a total. The bar for Verified is a
  recorded-fixture acceptance run that replays green, re-checked
  automatically; Production additionally requires a live-vendor
  smoke test in CI. Each connector states what it has cleared and
  when, and a connector that has cleared nothing says nothing —
  rather than soft-labelling everything "Certified."

  We publish no "N of M verified" figure, because it cannot be read
  correctly. A connector is unattested when nobody has recorded
  fixtures for it, which is not the same as a connector that failed
  its tests — and a ratio renders both identically.
- **Avro + ORC file readers** alongside the existing CSV / JSON /
  Parquet / Excel / XML support, in both the File source and all
  5 Cloud Files sources (S3 / Azure Blob / GCS / SharePoint / SFTP).
- **REST framework upgrade** — proper HTTP method + body support +
  pagination aliases + deep template substitution. The previous
  GET-only framework silently dropped POST bodies; this is now
  fixed with 18 contract tests pinning the behaviour.
- **32 optional dependency extras** in `pyproject.toml` for
  database drivers. From PyPI: `pip install "revoluzen-flow[postgres]"`,
  `pip install "revoluzen-flow[oracle]"`, `pip install "revoluzen-flow[all]"`, etc.
  From a source checkout, use the editable equivalents:
  `pip install -e ".[postgres]"`, `pip install -e ".[oracle]"`,
  `pip install -e ".[all]"`.
  Full per-database install + OS-driver instructions in
  [`docs/install/database-drivers.md`](../install/database-drivers.md).
- **Embedded AI assistance** — Copilot chat + inline helpers (SQL,
  transform, diagnose-error, post-run summary, anomaly detect).
  Local Ollama is the default; cloud LLM providers are opt-in
  bring-your-own-key, no proxying through us.
- **Plus license design specified** — named-user seat model with
  Ed25519-signed license file. Implementation is a separate workstream,
  expected to land incrementally.

### How to install

Revoluzen Flow 1.0 ships on PyPI, as source, and as a Docker image you build
locally. The Docker Hub image is reserved but **not published yet**:

```bash
# PyPI
python -m pip install --upgrade revoluzen-flow
python -m revoluzen_flow open

# Docker (recommended) — builds locally, serves http://localhost:8001
git clone https://github.com/revoluzen-technologies/revoluzen-flow.git
cd revoluzen-flow
docker compose up -d

# or from a source checkout
pip install -e .
python -m revoluzen_flow open
```

For database connectors, add the matching extra to the source install:

```bash
pip install "revoluzen-flow[postgres]"
pip install "revoluzen-flow[oracle]"      # thin mode, no Instant Client
pip install "revoluzen-flow[snowflake]"
pip install "revoluzen-flow[all]"          # everything pip-only (no OS drivers)
```

MS SQL Server, Oracle thick mode, and IBM Db2 also need OS-level
drivers — see [`docs/install/database-drivers.md`](../install/database-drivers.md).

When running from a source checkout, use `pip install -e ".[postgres]"`
from the repo root instead.

### What 1.0 deliberately does NOT promise

- **No "AI generates your pipeline end-to-end" claim.** Inline AI
  helpers exist; the headline "describe → working pipeline" flow
  is on the 1.1 roadmap, intentionally deferred until reliable.
- **No Verified-tier connectors yet.** The verification harness
  ships; the first wave of Verified rows lands as live-smoke CI
  + fixtures land per connector. Estimated first 10 within 4-6
  weeks post-launch.
- **No desktop binary.** `revoluzen open` opens the system default
  browser. A native packaged desktop app is on the 1.1 roadmap.
  Two candidate paths under evaluation — see
  [`docs/roadmap/oss-1-1.md`](../roadmap/oss-1-1.md).
- **No Plus license enforcement.** The named-user seat model is
  specified; implementation comes when first customer signs.

### Tested with

| Component | Version |
|---|---|
| Python | 3.11.7, 3.12.1 |
| DuckDB | 1.1.3 |
| Docker Engine | 25.0+ |
| Ollama (optional, for local AI) | 0.5.7 |
| Node.js (build only) | 20.10 LTS |

### Acknowledgements

This release reflects feedback from multiple rounds of independent
review on architecture, security posture, connector certification,
and product positioning. Specific corrections from those reviews
are credited in the
[`changelog.md`](../../CHANGELOG.md) under the 2026-06-02 entry.

### License

Apache License 2.0. See [`LICENSE`](../../LICENSE) and
[`NOTICE`](../../NOTICE).

Built by [Revoluzen](https://revoluzen.io).
