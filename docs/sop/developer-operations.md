# SOP — Developing Revoluzen Flow

**Audience:** anyone changing the code, in this repository or a fork.
**Scope:** set up, run, test, verify and package — the procedures you repeat.

[`../dev-guide.md`](../dev-guide.md) explains how the system is put together.
This tells you the exact commands and, for each one, how to tell whether it
worked. Every command here has been run against this repository.

> **Before you write code intended for upstream:** read
> [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md). External pull requests are
> currently not being merged while the contributor licence agreement is in
> legal review. Forking and shipping your own build is unaffected — Revoluzen
> Flow is Apache 2.0.

---

## SOP-D1 — Set up

Python 3.11, 3.12 or 3.13. Node 20+ for the frontend.

```bash
git clone https://github.com/revoluzen-io/revoluzen-flow.git
cd revoluzen-flow

python -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cd frontend && npm ci && cd ..
```

**Verify both halves independently** — a half-installed tree produces confusing
failures later:

```bash
python -c "import revoluzen_flow; print('backend ok')"
npm --prefix frontend run build
```

### Point it at a scratch data directory

Do this before you run anything. It keeps experiments away from real pipelines:

```bash
export REVOLUZEN_FLOW_DATA_DIR=/tmp/rzdev      # Windows: $env:REVOLUZEN_FLOW_DATA_DIR='C:\temp\rzdev'
export REVOLUZEN_FLOW_HOME=$REVOLUZEN_FLOW_DATA_DIR
```

---

## SOP-D2 — Run it locally

Two modes. Know which you are in, because they fail differently.

### Against the built UI (what users get)

```bash
python -m uvicorn revoluzen_flow.main:app --host 127.0.0.1 --port 8001
```

Serves the API and the **staged** frontend bundle. If you changed frontend
code, this will not show it until you restage (SOP-D4).

### With frontend hot-reload (what you develop against)

```bash
cd frontend && npm run dev
```

Vite serves the UI and proxies the API to the backend. Changes appear
immediately. **Run only one project's dev server at a time** — two instances
cross-wire, and you end up debugging a frontend talking to the wrong backend.

**Verify:**

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/api/health
```

---

## SOP-D3 — Run the tests

Run the suite that covers what you touched; run all three before proposing a
change.

### Backend

```bash
PYTHONPATH=backend python -m pytest backend/tests -q
```

PowerShell:

```powershell
$env:PYTHONPATH='backend'; python -m pytest backend\tests -q
```

Narrow while iterating, then run the whole thing:

```bash
PYTHONPATH=backend python -m pytest backend/tests/test_execution_contract.py -q
PYTHONPATH=backend python -m pytest backend/tests -q -k "contract or pool"
PYTHONPATH=backend python -m pytest backend/tests -q -m "not slow and not external"
```

Markers are declared in `backend/pytest.ini`: `unit`, `integration`, `e2e`,
`slow`, `stress`, `external`, `security`.

Tests marked `external` need a real service and **skip** otherwise — a skip
there is not a pass. Database-dialect tests need Postgres, MySQL or SQL Server;
`CONTRIBUTING.md` has the containers to start.

### Frontend unit

```bash
npm --prefix frontend run test
```

### Frontend end-to-end

These drive a real browser against a running instance. Point them at one you
started yourself:

```bash
npm --prefix frontend run test:e2e:install     # once — fetches Chromium

export NO_SERVER=1
export REVOLUZEN_FLOW_URL=http://127.0.0.1:8001
export REVOLUZEN_FLOW_E2E_EMAIL=<your dev account>
export REVOLUZEN_FLOW_E2E_PASSWORD=<its password>
npm --prefix frontend run test:e2e
```

Without `NO_SERVER=1` Playwright starts its own dev server on port 5174.

**A green e2e run against a stale instance proves nothing.** If you changed
frontend code, restage or use the dev server first.

### Types and build

```bash
npx --prefix frontend tsc --noEmit -p frontend/tsconfig.json
npm --prefix frontend run build
```

`build` also runs a CSS guard that fails if Tailwind's utilities are missing
from the output — a build that "succeeds" with an unstyled bundle is the
failure it exists to catch.

---

## SOP-D4 — Change the frontend

The backend serves a **staged copy** of the built frontend, not
`frontend/dist`. Three steps, in order:

```bash
npm --prefix frontend run build      # 1. build
python scripts/stage_frontend.py     # 2. copy into the package
python tools/package_preflight.py    # 3. confirm it is current
```

Skip step 2 and the server keeps serving the previous bundle, with no error to
tell you. Step 3 reports `frontend_dist is STALE` when that has happened.

---

## SOP-D5 — Verify a change before proposing it

Run all of this. It is what CI runs, and finding a failure locally costs
minutes rather than a review cycle.

```bash
PYTHONPATH=backend python -m pytest backend/tests -q      # backend
npm --prefix frontend run test                            # frontend unit
npx --prefix frontend tsc --noEmit -p frontend/tsconfig.json
npm --prefix frontend run build                           # build + CSS guard
python scripts/check_repo_coordinates.py                  # repo + PyPI coordinates
python tools/package_preflight.py                         # packaging
```

Then — and this is the step people skip — **run the thing you changed.** Start
the app, do the action in the UI, and look at it. A passing test suite tells
you nothing broke that was already covered; it does not tell you your change
works.

---

## SOP-D6 — Build and check a wheel

```bash
python scripts/stage_frontend.py
python scripts/stage_docs.py
python tools/package_preflight.py --strict
python -m pip wheel --no-deps -w dist .
python tools/verify_wheel.py dist/revoluzen_flow-*.whl
```

**Then install and run it**, in a clean environment:

```bash
python -m venv /tmp/rzwheel
/tmp/rzwheel/bin/pip install dist/revoluzen_flow-*.whl
/tmp/rzwheel/bin/revoluzen selftest
/tmp/rzwheel/bin/revoluzen serve --port 8020
```

Build-smoke is not run-smoke. A previous release shipped an image that built
perfectly and died on start, because nothing had ever run it.

---

## SOP-D7 — Add a node or a connector

- **Connector:** follow [`../connector-authoring.md`](../connector-authoring.md).
  Most connectors are a JSON manifest under
  `backend/revoluzen_flow/connectors/manifests/`, not Python. There is a
  dedicated issue template and PR template for connectors.
- **Node:** implement `BaseNode.execute(ctx)`, register the type, and add it to
  the palette (`MODULES` in `frontend/src/components/modulesPanelData.ts`). Node
  configuration is rendered from metadata, so a node that works but has no config
  schema is not finished.
- **Then re-derive the node count** from `MODULES` plus the backend types
  `HIDDEN_TYPES` does not hide, and update Settings → About and the documents that
  state it.

**Verify a new node renders**, not just that it imports — there is an e2e test
that opens the config panel for every visible node, and a node missing from it
is a node users cannot configure.

---

## SOP-D8 — Conventions that are enforced

These fail the build, so knowing them saves a round trip:

- **Writes must commit.** `db.execute()` does **not** commit. Use
  `db.execute_write()`, `db.execute_write_with_retry()` or
  `with db.transaction()`. An architecture test fails on any new
  uncommitted mutating statement — a write the caller was told succeeded and
  that vanishes on restart is the failure mode it exists to stop.
- **Self-referential GitHub URLs** must match `app_meta.py`, and the documented
  PyPI Trusted Publisher fields must match `pyproject.toml`.
  `scripts/check_repo_coordinates.py` enforces both.
- **The staged UI must be current** when packaging — `package_preflight.py
  --strict`.

---

## SOP-D9 — When local behaviour makes no sense

In this order:

1. **Are you running two servers?** Two dev servers, or a stale background
   backend, is the most common cause of "my change did nothing". Stop
   everything and start one.
2. **Is the staged UI stale?** `python tools/package_preflight.py` (SOP-D4).
3. **Is it your data, not your code?** Point `REVOLUZEN_FLOW_DATA_DIR` at an
   empty directory and try again on a clean first-run.
4. **Is the install half-broken?**

   ```bash
   revoluzen selftest     # imports the full server stack
   revoluzen doctor       # ports, service, datastore, LLM reachability
   ```
