# Revoluzen Flow — User Guide

A complete path from installing Revoluzen Flow to running scheduled, verified
pipelines. Read it once in order; after that, use it as a map.

Two companions sit beside it. [`NODE_REFERENCE.md`](NODE_REFERENCE.md) is the
per-node specification — every parameter of every node. [`sop/user-operations.md`](sop/user-operations.md)
is the checklist for routine jobs: backup, restore, upgrade, diagnosis. This
guide teaches; those two are looked up.

**Contents**

1. [What Revoluzen Flow is](#1-what-revoluzen-flow-is)
2. [Concepts](#2-concepts)
3. [Installing and first run](#3-installing-and-first-run)
4. [The canvas and the node families](#4-the-canvas-and-the-node-families)
5. [Your first pipeline](#5-your-first-pipeline)
6. [Making pipelines reusable](#6-making-pipelines-reusable)
7. [Branching and routing](#7-branching-and-routing)
8. [Connections](#8-connections)
9. [Running pipelines](#9-running-pipelines)
10. [Reading a run](#10-reading-a-run)
11. [Execution Contracts](#11-execution-contracts)
12. [Storage and managed tables](#12-storage-and-managed-tables)
13. [Projects](#13-projects)
14. [Users, roles and access](#14-users-roles-and-access)
15. [Keeping it healthy](#15-keeping-it-healthy)
16. [Troubleshooting](#16-troubleshooting)
17. [Glossary](#17-glossary)

---

## 1. What Revoluzen Flow is

Revoluzen Flow is a data pipeline engine you run yourself. You draw a pipeline
on a canvas, point it at your data, and it moves and transforms that data on a
schedule.

Three things follow from "you run it yourself", and they shape everything else:

**Your data stays where you put it.** There is no Revoluzen cloud in the path.
The engine runs on your machine or your server, reads your sources directly,
and writes to your destinations. Nothing is relayed through us, because there
is nothing to relay through.

**It is a single binary with batteries included.** Install it and you have the
engine, the browser UI, the connectors and the scheduler. There is no separate
database to provision before it will start, no message broker, no agent to
deploy on the machines it talks to.

**You are the administrator.** On first run there is no account and no default
password — you create the first one and it owns the instance. That is a
deliberate trade: nothing is reachable before you have decided who you are.

### What it is not

It is not a hosted service, and it is not a replacement for a data warehouse.
It moves data into and out of the systems you already have and transforms it on
the way. The warehouse stays yours.

It is also not a general job runner. Everything is built around tabular data
moving through a graph. If your task has no rows in it, this is the wrong tool.

---

## 2. Concepts

Six ideas. Everything in the UI is one of them.

| Concept | What it is |
| --- | --- |
| **Project** | A folder for related work. Pipelines and connections can be scoped to one, so a team's things stay together. |
| **Connection** | A saved, tested way to reach a system — a database, a warehouse, an API, a file store. Credentials live here, encrypted, never in a pipeline. |
| **Pipeline** | The unit you build and run: a graph of nodes describing how data moves from source to destination. |
| **Node** | One step in a pipeline. A node reads, transforms, routes, acts, or writes. |
| **Run** | One execution of a pipeline, with its own status, row counts, timings and logs. |
| **Managed table** | Storage Revoluzen Flow owns, so a pipeline can land results without you provisioning a database first. |

They nest the way you would expect. A **project** holds **pipelines**. A
pipeline is a graph of **nodes**. Nodes reach the outside world through
**connections**. Executing a pipeline produces a **run**. A run may write to a
**managed table** or to any system you have a connection for.

### How a pipeline actually executes

Worth understanding early, because it explains most surprises:

- Nodes run in **topological order** — a node runs once everything feeding it
  has finished. Independent branches may run in any order relative to each
  other.
- Each node receives the rows its upstream node produced and emits rows of its
  own. The row count on the canvas after a run is what that node *emitted*.
- A node's configuration is resolved **just before it runs**, so it can refer to
  values produced by nodes upstream of it (§6).
- If a node fails, the nodes downstream of it do not run. Independent branches
  are unaffected.

---

## 3. Installing and first run

### Choosing an install method

| Situation | Use |
| --- | --- |
| Any OS, least that can go wrong | Docker Compose |
| Python 3.11+ already, want it on the host | `pip install revoluzen-flow` |
| Linux without Docker | the `.deb`, `.rpm` or `.AppImage` from the release |

There is no signed Windows `.exe` or macOS `.pkg`. An unsigned installer trips
SmartScreen and Gatekeeper, which is a worse first experience than Docker.

### Starting it

```bash
revoluzen serve
```

Default port 8001, bound to `127.0.0.1` — **loopback only**, so nothing outside
the machine can reach it until you choose otherwise. Open the printed URL and
create the first account; it becomes the operator.

```bash
revoluzen health      # confirm it is up
revoluzen --help      # every command
```

Exposing it to a network, running it as a service, and the corporate-TLS-proxy
case are in [`sop/user-operations.md`](sop/user-operations.md) §§ SOP-U1–U2.

---

## 4. The canvas and the node families

The editor has three parts: the **palette** on the left, the **canvas** in the
middle, and the **config panel** on the right for whichever node is selected.

Drag a node onto the canvas, drag from one node's output handle to another's
input to connect them. The palette has a search box — use it rather than
hunting through categories once you know what you want.

Nodes are grouped into six families.

### Data Movement

Getting data in and out. **Source** and **Destination** are the generic
connection-backed pair; **Copy Data** moves between two systems directly;
**Managed Table Source** and **Managed Table Sink** read and write Revoluzen
Flow's own storage (§12); **SaaS Connector** reads a SaaS application, through a
built-in connector or one you saved from Insights → API Explorer.

To upload a file over FTP or SFTP, pick that connection on a Destination, then
set **Remote Path**, such as `/uploads/orders.csv`. The folder must already exist
on the server, and SFTP needs the `paramiko` package.

A destination has a **write mode** — typically *replace*, *append* or *merge* —
and this is the single most consequential setting in a pipeline. *Replace*
discards what was there. §11 exists largely because of it.

### Transform

Reshaping rows: filtering, selecting and renaming columns, aggregating,
deriving new values, cleaning, pivoting, deduplicating, and the **Data
Wrangler** for multi-step shaping in one node. This is the largest family.

### Combine

Putting streams together — joins, unions, merges. These take more than one
input, so the node has several input handles.

### Control Flow

Deciding what happens rather than transforming rows: conditional routing,
loops, waiting, and failing deliberately when something is not right.

### Action

Side effects outside the data path — sending a notification, calling an HTTP
endpoint, executing SQL as a task, reading file metadata. An action node is
how a pipeline tells the outside world something happened.

**File System** copies, moves, renames or deletes files on the machine running
Revoluzen Flow. A relative path means inside the data folder, and missing folders
are created.

### AI / Semantic

Embedding, semantic routing, and guardrail steps for pipelines that involve a
model.

> **The palette is the authority on what exists in your version.** Node counts
> in documentation go stale; the palette cannot. Search it.
> [`NODE_REFERENCE.md`](NODE_REFERENCE.md) documents every node's parameters.

---

## 5. Your first pipeline

Build something real: read a table, keep the rows you care about, summarise
them, and store the result.

### 5.1 Create a connection

**Connections → New.** Choose the system, enter credentials, and — the step
people skip — **press Test before saving**. A connection that saves but cannot
connect fails later, inside a run, where the error is further from the cause.

If the secret is already on **Connections → Credentials**, pick it under **Saved
credential**. The fields it holds fill themselves, so you type only the rest.

### 5.2 Lay out the nodes

**Pipelines → New Pipeline.** A minimal useful shape:

```
Source  ->  Filter  ->  Aggregate  ->  Destination
```

Click each node and fill in the config panel on the right. Required fields are
marked; a node with an incomplete required field will fail at run time rather
than silently doing nothing.

You can also describe the pipeline to the Copilot. It asks where the data comes
from and where it goes, offering your real connections, files and tables, then
drafts the pipeline for you to review. Nothing is saved until you accept it.

### 5.3 Run it and watch

Press **Run**. Nodes light up in order. When it finishes, each shows the number
of rows it emitted — reading those left to right tells you where data went.

### 5.4 Check the result before trusting it

Open the run from **Executions** and look at the sample rows for the step before
your destination. Confirming the shape of what you were about to write is
faster than inspecting the destination afterwards, and it is the habit that
catches the "wrong column, right row count" class of mistake.

To make the check part of every run, add a **Data Quality** step: rules such as
"amount is 0 or more" or "email is not empty", and what to do with a row that
breaks one (drop it, stop the run, tag it, or send it down a separate path).
Rules that several pipelines need belong in a **shared rule set**: in the
step's settings, **Save this step's rules as a shared set**, then pick the set
in the other pipelines' Data Quality steps. A fix to the set (**Edit set**)
reaches every pipeline that uses it from its next run, published ones
included, and the screen says how many that is before it saves. Each save
keeps the rules it replaced as an earlier version, a run's error names the set
and the version it checked, and a set that a saved pipeline uses cannot be
deleted.

### 5.5 Schedule it

Once it runs correctly by hand, give it a schedule from the pipeline row.
Scheduled runs behave exactly like manual ones and appear in the same history.

---

## 6. Making pipelines reusable

A pipeline with values typed into it works once. There are three mechanisms for
making one work for many cases, and they resolve at different times — which is
the thing to understand.

### Parameters — `${param.NAME}`

**Typed inputs declared once per pipeline**, in Editor → Parameters. Each has a
name, a type (string, int, float, bool, json), an optional default, and whether
it is required.

Parameters resolve **before the run starts**. Use them for what differs between
runs of the same pipeline: a date, a batch size, a dataset name, an
environment.

```
"${param.batch_size}"            # whole-string: keeps the declared type
"/data/${param.dataset}.csv"     # embedded: substituted as a string
```

That distinction matters. A whole-string reference to an int parameter stays an
int; the same parameter inside a longer string becomes text.

Values can come from the Run dialog (which prompts when a pipeline declares
parameters), the Pipelines page, or the API — so the same pipeline can be run
by hand today and by another system tomorrow.

System placeholders resolve the same way anywhere: `${utcnow}`,
`${utcnow:%Y-%m-%d}`, `${run_id}`.

### Expressions — `{{ ... }}`

Where parameters are fixed before the run, **expressions are evaluated per step,
in order, immediately before that step executes** — so an expression can use
values that earlier steps produced.

The helpers available:

| Expression | Gives you |
| --- | --- |
| `{{ $json.field }}` | a field of the current row |
| `{{ $now }}`, `{{ $today }}` | date helpers with `.startOf` `.endOf` `.plus` `.minus` `.toFormat` |
| `{{ $vars.NAME }}` | a runtime variable (below) |
| `{{ $('Node Name').first().col }}` | a value from a named earlier node — also `.all()`, `.item(i)` |
| `{{ $itemIndex }}` | the row's index |
| `{{ $env.REVOLUZEN_FLOW_* }}` | an environment variable |

Object literals accept quoted or unquoted keys, so `{{ $now.minus({ days: 7 }) }}`
and `{{ $now.minus({'days': 7}) }}` are the same.

### Runtime variables — `$vars`

A dictionary scoped to one run. Written by **Set Variable** (a scalar per
entry), and by **Lookup**, which stores `{ firstRow, rows, count, isEmpty }`.
Read anywhere with `{{ $vars.NAME }}`, including into the fields of later nodes.

The classic use is an incremental load: look up `MAX(updated_at)` from the
destination into a variable, then use that variable in the source query's
`WHERE` clause.

### Which to reach for

- Differs per run, known before it starts → **parameter**
- Depends on data the pipeline just read → **expression**
- Computed once and used by several later nodes → **variable**

---

## 7. Branching and routing

Some nodes send different rows down different paths. The distinction that
confuses people is between a **brancher** and a **filter**:

- A **brancher** has more than one output. Every row leaves by one of them.
- A **filter** has one output. Rows that do not match are dropped.

The branchers are **Conditional Split**, **Data Quality** in reject mode (`pass`
and `reject` outputs), and **Deduplicate** when emitting duplicates (`unique`
and `duplicate`).

**If** and **Switch** are single-output filters, not branchers — a common
surprise. If you want the rows that failed the test, you need a brancher, not
an If.

Branching is how you build "load the good rows, quarantine the bad ones"
without a second pipeline: Data Quality in reject mode, `pass` to the real
destination, `reject` to a managed table you can inspect later.

---

## 8. Connections

A connection is a saved, tested route to a system. It holds the credentials —
encrypted at rest — so pipelines never carry them.

Key points:

- **Test before saving.** Always.
- **Read and write are separate capabilities.** A connection may be usable as a
  source but not a destination, which is a feature: it lets you hand someone a
  read-only route to production.
- **Connections can be global or scoped to a project.** Scope them when a team
  should not see another team's credentials.
- Rotating a credential means editing the connection, not the pipelines — which
  is the point of keeping them separate.
- **A saved credential can serve many connections.** Keep it on **Connections →
  Credentials** and pick it under **Saved credential**; change it once and they
  all pick up the new value. Give it an expiry date and administrators are warned
  on the bell and by email a week ahead, a day ahead and when it expires.

Per-system detail is in [`connectors.md`](connectors.md); the full procedure is
[`user-guides/connections.md`](user-guides/connections.md).

---

## 9. Running pipelines

**By hand** — the Run button in the editor or on the Pipelines row. If the
pipeline declares parameters, you are prompted for them.

**On a schedule** — set from the pipeline row. Scheduled runs are ordinary runs
and share the same history.

**From another system** — on **Workflows → Triggers**, give a pipeline an endpoint
and create an API key, then copy the ready-to-use command. The caller passes
parameter values in the request, and the run shows on Executions with the trigger
API. Use this when something else owns the timing: a data-arrival event, an
upstream job finishing, a deployment. See
[`user-guides/external-triggers.md`](user-guides/external-triggers.md).

**To pause a pipeline**, open its row's **⋯** menu on the Pipelines page and click
**Deactivate**. Its schedules are skipped and its endpoints answer 423 until you
click **Activate**. You can still run it by hand, so you can test a fix first.

Pipelines also have a lifecycle — draft, published, and so on — which governs
what may be scheduled or deployed. See
[`user-guides/pipelines.md`](user-guides/pipelines.md).

---

## 10. Reading a run

Open any run from **Executions**.

| What you see | What it means |
| --- | --- |
| **Status** | Success, error, or running. Note that success means every step completed, not that the data was right — §11. |
| **Per-step status** | Which step failed, and which never ran because it was downstream of the failure. |
| **Row counts** | What each step *emitted*. Reading them in order shows where rows disappeared. |
| **Duration** | Per step and total. A step far slower than usual is often a source query, not the engine. |
| **Peak memory / CPU** | What this run actually cost. Rolled up per pipeline under Pool → Configuration (§15). |
| **Sample rows** | For steps that produced data — the fastest way to confirm a transform did what you meant. |
| **Logs** | The detail, when the above is not enough. |

### When a run fails

1. **Open the run.** The failure summary names the failed step and its error.
2. **Check whether a contract stopped it** (§11). A run marked *Stopped by
   Execution Contract* did not break — it was refused, and your destination is
   untouched.
3. **Look upstream.** A destination reporting "no rows" is almost always
   describing a filter's behaviour, not its own. Row counts show where.
4. **Check the host, not the pipeline,** if several failed at once —
   `revoluzen doctor`.

### Incidents

When a scheduled, triggered or API run fails, **Executions → Incidents** opens
an incident for that pipeline. Later failures add to it, and the next
successful unattended run closes it. Runs started from the editor do not
count, because they may be samples of a draft.

An incident shows what you would otherwise piece together by hand:

- the kind of failure: a passing problem (timeout, rate limit), an outside
  system that was unreachable or refused the login, the data, the
  configuration, or a crash;
- the step that failed, what it reads from and what it feeds;
- when the pipeline last worked, and every version saved since;
- a timeline of failures, notes and anything Flow tried.

**Ask the Copilot** starts a diagnosis from the incident: the Copilot can read
it, including what changed since the pipeline last worked.

When several pipelines fail for one reason, the tab says so above the list:
the failed steps use the same connection and fail the same way (a warehouse
that refused its login), or they stop with the same error once names and
numbers are set aside (a column that an upstream table dropped). Fix the cause
once instead of opening each incident.

The Steward looks across pipelines too: the same step (a SQL transform, a
filter, a join, a set of Data Quality rules) copied into several pipelines is
reported as **Repeated logic**, with what to make it into: a shared rule set,
or one pipeline the others call with a Run Pipeline step.

### Resuming failed runs by policy

An admin can turn on **Settings → Resume failed runs automatically**. A
failure a retry can plausibly fix (a passing problem or an outside system,
and optionally unclassified failures) is then resumed from where it stopped,
after a wait, up to the number of attempts set, and each attempt is recorded
on the incident. The pipeline is never changed, and data, configuration and
crash failures are never resumed, because they would fail the same way. Off
by default.

---

## 11. Execution Contracts

A pipeline can succeed and still be wrong. Every step reports success, the
output is empty, and a destination set to *replace* is replaced with nothing.
The run is green. The data is gone.

An **Execution Contract** is how you stop that. You declare, before the run,
what must be true of the result. Revoluzen Flow then checks it.

### The part that matters: when it checks

Guarantees that can be judged from the rows **about to be written** are checked
**before the write happens**. A run that would break one is refused, and the
destination is left exactly as it was. Those are the ones worth having:

- never write an empty result
- at least N rows / at most N rows
- within a percentage of an expected count
- these columns must be present

Everything else — every step succeeded, no duplicate keys, the destination
acknowledged the write, the newest row is recent enough — can only be judged
once the run is over. Those are verified afterwards and recorded. Useful, but
they report damage rather than prevent it. Duplicate keys and the age of the
newest row are measured from the output itself at the end of the run.

The authoring dialog separates the two, because choosing between them is the
whole decision.

### Receipts

Every verified run produces a receipt: the verdict, what each guarantee did,
what the run did to its destination (*"replaced with 0 rows, was 3"*), and which
step the rows went missing at. Receipts are hash-chained, so a removed or
edited receipt is detectable.

### Start from the spec

**Pipelines → the Contract column → + Spec** opens the **Requirement** tab: what
the pipeline is for, in the words of whoever asked for it. The goal (the
pipeline's business purpose), the business owner, who uses the output, where the
data comes from, what should change when it works, and the acceptance criteria:
how to tell it worked.

Each criterion is a sentence plus how it is checked. Pick a check — never
empty, at least or at most N rows, close to an expected count, has these
columns, one row per key, newest row no older than — or leave it to a person.
**Save and make contract** turns every criterion with a check into a guarantee.
Criteria left to a person are listed as such; a criterion missing a setting is
reported and left out, because a clause without its number fails every run.

The spec is saved with every version of the pipeline, and with the copy in your
Git repository when you publish. Editing it does not count as a change to
publish and does not undo a passing test, because it changes what the pipeline
is held to, not what it runs. Every receipt names the spec version it was
judged against.

### Adding a guarantee by hand

The **Contract** tab adds guarantees directly. When it asks you to say what the
guarantee means, use your own words — the receipt shows your wording first, and
*"a regional rollup with no regions is not a result"* is something you can act
on at 3am in a way that `no_empty_output` is not. Making the contract from the
spec again replaces only the guarantees that came from the spec; the ones you
added here stay.

Start with one guarantee on your most destructive pipeline — the one whose
destination is set to replace. That is where a contract earns its keep.

### Publishing a version that broke its contract

Publishing needs a passing test, but a test runs on a sample, and a sample
cannot be held to a row count. So if this exact version's last real run broke
its contract, **Publish is refused** and says which promise broke. Fix the
pipeline, or change the contract, and run it again. A failure on an earlier
version does not block a fix, and a verdict against a contract you have since
changed no longer counts.

---

## 12. Storage and managed tables

Not every result deserves a warehouse table. Revoluzen Flow can own storage
itself, so a pipeline can land output immediately.

Managed tables appear under **Storage**, can be previewed in the UI, and can be
read back by other pipelines through **Managed Table Source**. They are a real
destination, not a scratch area: row counts, write modes and contracts apply to
them exactly as to an external database.

Before building on a file or table, open its **Profile** tab: for each column,
how many values are empty, how many are distinct (and whether every value is
unique, which makes it a candidate key), the smallest and largest, the most
common value, and the average for numbers. These are the statistics the Data
Profile step produces. Anything over 100,000 rows is profiled on a random
sample, and the tab says how many rows it read.

A table's **Tests** tab holds checks on its data: no empty values, no
duplicates, only allowed values, every value present in another table, loaded
recently enough, and a row count within bounds or close to the previous load.
They can run on their own, not only when you press **Run tests**: after every
load, in the background, and every 15 minutes, hour, 6 hours or day. The
periodic check is what notices a table that stopped being loaded at all. A
failed test becomes a Steward finding and a notification to the admins, unless
its severity is *warn*, which keeps it on the table. The details are in
[Storage](storage.md#tests-and-monitors).

Reach for them for intermediate results between pipelines, for quarantined rows
from a Data Quality branch, for outputs only Revoluzen Flow consumes, and while
you are still deciding where something ought to live.

---

## 13. Projects

A project groups related pipelines, connections and folders. Create one per
team, per domain, or per customer — whatever the unit is that you would want to
hand to somebody whole.

There is a Default project, and small installations happily stay in it.
Scope connections to a project when credentials should not be shared across
teams. Full detail: [`user-guides/projects.md`](user-guides/projects.md).

---

## 14. Users, roles and access

Revoluzen Flow has five roles — **super_admin**, **admin**, **developer**,
**analyst** and **viewer** — and access can additionally be scoped by
environment, so someone who may change things in DEV need not be able to in
PROD.

Self-registration is **off** by default. Accounts are created deliberately,
which is the right default for something holding credentials to your databases.

Adding users, changing roles, the password policy and session limits:
[`user-guides/users-and-access.md`](user-guides/users-and-access.md).

---

## 15. Keeping it healthy

**Pool → Overview** shows what is running now: workers, queue, current load.

**Pool → Configuration** shows what your pipelines actually cost. The
per-pipeline footprint gives memory and runtime per pipeline, and how many
copies of each this host could hold. The capacity line compares the configured
per-worker memory ceiling against both your RAM and your measured peak — and if
it reports the ceiling is many times larger than anything you have ever used,
**lower the ceiling rather than reducing concurrency**. Serialising a machine
that could run everything at once is a real cost paid for a theoretical risk.

These are environment variables read at startup, so changing them means a
restart. They are deliberately not editable in the UI: a setting that can change
while work is in flight is a setting that changes mid-run.

**Back up before every upgrade.** `revoluzen backup` writes pipelines, history,
credentials and managed tables into one archive. A backup you have never
restored is a hypothesis — restore one into a throwaway directory once. The
procedure is [`sop/user-operations.md`](sop/user-operations.md) §§ SOP-U3–U5.

---

## 16. Troubleshooting

| Symptom | Usual cause | What to do |
| --- | --- | --- |
| `pip install` fails with `CERTIFICATE_VERIFY_FAILED` | A TLS-inspecting corporate proxy — affects all pip, not this project | Trust your org's root CA, or use a prebuilt image |
| Destination is empty after a successful run | A filter upstream matched nothing, and the destination replaced its contents | Read row counts along the canvas; add a contract (§11) so it cannot recur |
| "No rows" at the destination step | Same as above — the sink is reporting, not causing | Look at the first step whose count dropped |
| A pipeline is far slower than usual | Usually the source query, not the engine | Compare per-step durations against a previous run |
| Several pipelines fail at once | The host, not the pipelines | `revoluzen doctor` |
| Cannot sign in on a fresh install | By design — there is no default account | Create the first account; it becomes the operator |
| Changed a credential and runs still fail | Pipelines use the connection; the connection was not updated | Edit the connection and press Test |
| A schedule stopped firing, or another application gets a 423 | The pipeline is deactivated: its row shows **Paused** | Row's **⋯** menu → **Activate** |
| Expression returns nothing | It referenced a node that had not run yet, or a misspelled node name | Expressions see only upstream nodes; check the name in `$('Node Name')` |

---

## 17. Glossary

**Brancher** — a node with more than one output; every row leaves by one of
them. Contrast a filter, which has one output and drops non-matching rows.

**Connection** — a saved, tested, credentialed route to an external system.

**Execution Contract** — a declaration of what must be true about a run's
result, checked before the write where possible.

**Managed table** — storage Revoluzen Flow owns, usable as a destination and as
a source.

**Node** — one step in a pipeline.

**Parameter** — a typed pipeline input, resolved before the run (`${param.X}`).

**Pipeline** — a graph of nodes describing how data moves.

**Project** — a grouping of pipelines, connections and folders.

**Receipt** — the hash-chained record of a contract verdict for one run.

**Run** — one execution of a pipeline.

**Runtime variable** — a value written during a run and read with
`{{ $vars.NAME }}`.

**Write mode** — how a destination applies rows: replace, append or merge.

---

## Where to go next

| You want to | Read |
| --- | --- |
| Every parameter of every node | [`NODE_REFERENCE.md`](NODE_REFERENCE.md) |
| Build more complex pipelines | [`user-guides/pipelines.md`](user-guides/pipelines.md) |
| Connect to a specific system | [`user-guides/connections.md`](user-guides/connections.md), [`connectors.md`](connectors.md) |
| Organise work across teams | [`user-guides/projects.md`](user-guides/projects.md) |
| Add people | [`user-guides/users-and-access.md`](user-guides/users-and-access.md) |
| Trigger runs from elsewhere | [`user-guides/external-triggers.md`](user-guides/external-triggers.md) |
| Run it properly on a server | [`deployment.md`](deployment.md) |
| Understand how it works inside | [`architecture.md`](architecture.md) |
| Do a routine operational task | [`sop/user-operations.md`](sop/user-operations.md) |
| Ask a question | [`faq.md`](faq.md) |

**Found a bug?** Open an issue with your version, OS and reproduction steps —
[`../CONTRIBUTING.md`](../CONTRIBUTING.md).

**Found a security problem?** Do not open an issue. Follow
[`../SECURITY.md`](../SECURITY.md).
