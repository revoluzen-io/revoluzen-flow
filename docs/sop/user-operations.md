# SOP — Running Revoluzen Flow

**Audience:** anyone operating a Revoluzen Flow instance.
**Scope:** the procedures you repeat — installing, upgrading, backing up,
restoring, and working out why a run failed.

This is the procedural companion to the reference documentation. Where
[`../quickstart.md`](../quickstart.md) explains *what* Revoluzen Flow is and
[`../deployment.md`](../deployment.md) explains how it is put together, this
tells you the steps for a given task and how to confirm each one worked.

Every command below is the real CLI. Check what you have with:

```bash
revoluzen version
revoluzen --help
```

`rz` is a shorter alias for the same program.

---

## SOP-U1 — Install

### Decide the method first

| Situation | Use |
| --- | --- |
| Any OS, you want the least that can go wrong | Docker Compose |
| You have Python 3.11+ and want it on the host | `pip install revoluzen-flow` |
| Linux, no Docker | the `.deb`, `.rpm` or `.AppImage` on the GitHub release |

There is no signed Windows `.exe` or macOS `.pkg`. An unsigned installer trips
SmartScreen and Gatekeeper, which is a worse first experience than Docker.

### Steps — pip

```bash
python -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install revoluzen-flow
revoluzen version
```

**Verify:** `revoluzen version` prints a version. If `revoluzen` is not found,
the virtual environment is not active, or its `Scripts`/`bin` directory is not
on `PATH`.

### Steps — Docker Compose

```bash
docker compose up -d
docker compose ps
```

**Verify:** the container stays up for more than a few seconds. A container
that starts and immediately exits is a real failure — read `docker compose logs`
rather than retrying.

### If install fails behind a corporate network

```
SSL: CERTIFICATE_VERIFY_FAILED ... self-signed certificate in certificate chain
```

This is a TLS-inspecting proxy, not a fault in Revoluzen Flow, and it affects
every `pip install` on that network — including the one *inside* a Docker build.

Fix in order of preference:

1. Trust your organisation's root CA:
   `pip config set global.cert /path/to/corp-root.pem` and set
   `REQUESTS_CA_BUNDLE` to the same file.
2. Pull a prebuilt image instead of building one.
3. Last resort, if policy allows:
   `pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org revoluzen-flow`

---

## SOP-U2 — First run

```bash
revoluzen serve
```

Default port is 8001 and it binds to `127.0.0.1` — **loopback only**. Nothing
outside the machine can reach it until you choose otherwise.

Open the printed URL. The first screen offers **Create first account**: there is
no default password, and the account you create becomes the operator.

In server security mode (Docker, or `REVOLUZEN_FLOW_SECURITY_MODE=server`) the
form also asks for a one-time **setup token**, so a stranger who reaches the page
first cannot claim the install. The server prints it at startup (look for
`FIRST RUN` in the log or `docker compose logs`) and saves it as
`SETUP_TOKEN.txt` in the data folder. It stops working once the first account
exists.

**Verify:**

```bash
revoluzen health
```

### Exposing it to other machines

```bash
revoluzen serve --host 0.0.0.0 --port 8001
```

Do this deliberately. It puts the instance on your network, so put it behind a
reverse proxy with TLS rather than exposing it directly, and read
[`../deployment.md`](../deployment.md) first.

### Running it permanently

```bash
revoluzen install-service      # register as an OS-native background service
revoluzen service-status
revoluzen uninstall-service    # removes the registration, not your data
```

---

## SOP-U3 — Back up

**Do this before every upgrade, and on a schedule you actually keep.**

```bash
revoluzen backup --to /backups/revoluzen-$(date +%F).tar.gz
```

This snapshots `REVOLUZEN_FLOW_DATA_DIR` — pipelines, run history, credentials,
endpoints and API keys from the Triggers page, managed tables — into one archive.

**Verify** the archive is non-trivial in size and lists sensibly:

```bash
tar -tzf /backups/revoluzen-$(date +%F).tar.gz | head
```

A backup you have never restored is a hypothesis. Restore one into a throwaway
directory once, so you find out now rather than during an incident:

```bash
revoluzen restore /backups/revoluzen-2026-11-01.tar.gz --to /tmp/restore-test
```

---

## SOP-U4 — Restore

```bash
revoluzen restore <backup.tar.gz> --to /path/to/data-dir
```

Restoring **on top of an existing data directory requires `--force`, and is
destructive.** Prefer restoring into an empty directory and pointing the server
at it:

```bash
export REVOLUZEN_FLOW_DATA_DIR=/path/to/restored
revoluzen serve
```

**Verify:** your pipelines are listed and their run history is present.

```bash
revoluzen list
```

---

## SOP-U5 — Upgrade

1. **Back up** (SOP-U3). Not optional.
2. Read the [CHANGELOG](../../CHANGELOG.md) for the versions you are crossing.
3. Upgrade:

   ```bash
   pip install --upgrade revoluzen-flow     # or: docker compose pull && docker compose up -d
   ```

4. **Verify before trusting it:**

   ```bash
   revoluzen version
   revoluzen selftest
   revoluzen health
   ```

   `selftest` imports the full server stack and reports the route count. It
   catches a broken install before your pipelines do.

5. Run one known-good pipeline and confirm the output matches what it produced
   before the upgrade.

**If it goes wrong:** reinstall the previous version and restore the backup from
step 1.

---

## SOP-U6 — A run failed

Work in this order. Stop at the step that answers it.

1. **Read the Execution Summary** for that run in the UI. It names the failed
   step and shows the error.
2. **Check whether an Execution Contract stopped it.** A run marked *Stopped by
   Execution Contract* did not break — it was refused because the result would
   have broken a promise you declared. The banner says what was protected, and
   "Likely cause" points at the step where the rows went missing. **Your
   destination is unchanged.** Fix the cause, do not disable the contract.
3. **Look upstream of the failing step.** A sink reporting "no rows" is usually
   reporting a filter's behaviour, not its own.
4. **Check the logs:**

   ```bash
   revoluzen logs <execution-id>
   revoluzen status <pipeline-id>
   ```

5. **Check the host, not the pipeline**, if several pipelines failed at once:

   ```bash
   revoluzen doctor
   ```

   `doctor` checks ports, the service, the datastore and LLM reachability.
   `--repair` clears a stale instance record left by an unclean shutdown.

6. **If the run never started**, look for a **Paused** badge on the pipeline's row.
   While a pipeline is deactivated, its schedules are skipped and calls from other
   applications get a 423. Row's **⋯** menu → **Activate** switches it back on.

---

## SOP-U7 — Sizing the host

Go to **Pool → Configuration**.

- **Per-pipeline compute footprint** shows what each pipeline actually costs
  (p95 memory, runtime, CPU) and how many copies of it this host can hold.
- The **capacity line** compares the configured per-worker ceiling against both
  your RAM and your measured peak. If it reports the ceiling is many times your
  real peak, lower the ceiling rather than cutting concurrency — the suggested
  environment variable is in the message.

These are environment variables read at startup, so changing them means
restarting the server. They cannot be edited in the UI by design.

---

## SOP-U8 — Getting help, and reporting a problem

- **A question:** check [`../faq.md`](../faq.md) and
  [`../customer-faq.md`](../customer-faq.md) first.
- **A bug:** open an issue using the template. Include what
  [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md) asks for — version, OS,
  Python version, minimal reproduction, and actual versus expected behaviour. A
  report without a reproduction is usually unactionable.
- **A security vulnerability:** **do not open an issue.** Follow
  [`../../SECURITY.md`](../../SECURITY.md). Public disclosure before a fix
  exists puts every other operator at risk.
