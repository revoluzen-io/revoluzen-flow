# Connections — User Guide

**Audience:** All users
**Prerequisites:** Optionally a project you can access

A **Connection** is a saved, reusable configuration for a data source or sink — database URL, S3 bucket, API endpoint, etc. Instead of typing credentials into every pipeline node, you create one connection and reference it by ID everywhere.

---

## 1. Key properties

| Field | Meaning |
|---|---|
| `id` | System-assigned |
| `name` | Human-readable (e.g. `orders-pg`) |
| `type` | One of 45 supported types (postgres, mysql, s3, snowflake, slack, ...) |
| `config` | Connection params: host, port, database, region, etc. |
| `credentials` | Username / password / API key — encrypted at rest |
| `capabilities` | `["read"]`, `["write"]`, or `["read","write"]` — **enforced** at save-time |
| `project_id` | If set, connection is project-scoped; otherwise global |
| `tags` | Free-form labels |

> **Revoluzen Flow Enterprise** adds a separate Secrets Manager that stores credentials in a vault with rotation, an `environment` field that scopes connections to DEV vs PROD, and tier-based access control. See [section 9](#9-revoluzen-flow-secrets-manager--environment-scoping).

---

## 2. Creating a connection

### 2.1 Via UI

1. Go to the **Connections** page.
2. Click **+ New Connection**.
3. Pick a connector type. The picker shows status badges:
   - **Certified** — production-grade, no badge
   - **Beta** — usable with gaps; first-use shows a one-time confirmation dialog
   - Hidden: Coming-soon (UI-only stubs) and Revoluzen Flow Enterprise enterprise connectors
4. Fill in config + credentials.
5. Click **Test** to verify. Click **Save**.

### 2.2 Via API

```bash
curl -X POST http://localhost:8001/api/connections/ \
  -H "Content-Type: application/json" \
  -d '{
    "name": "orders-pg",
    "type": "postgresql",
    "config": {"host": "localhost", "port": 5432, "database": "orders"},
    "credentials": {"username": "revoluzen-flow", "password": "..."},
    "capabilities": ["read"],
    "project_id": "proj_sales"
  }'
```

---

## 3. The read/write capability split

Every connection is tagged with what it can do:

| Capability | Symbol | Used by |
|---|---|---|
| `read` | ⬆️ | Source nodes |
| `write` | ⬇️ | Sink nodes |
| `read+write` | ⬆️⬇️ | Both (default for databases) |

Notification connectors (Slack, email, webhook) default to `["write"]` only. The validator **refuses to save** a pipeline that wires a source node to a write-only connection — this is the capability split.

In the connection picker, filters automatically narrow to the right capability. A source node only shows read-capable connections.

---

## 4. Testing a connection

Before saving or on demand:

```bash
curl -X POST http://localhost:8001/api/connections/{id}/test
```

Returns `{"ok": true, "duration_ms": 124}` or `{"ok": false, "error": "..."}`. The UI shows a green check or red error inline.

---

## 5. Project-scoped vs global connections

- **Global** (`project_id = null`) — every project can use it. Appropriate for shared warehouses, shared Slack endpoints.
- **Project-scoped** (`project_id = proj_xxx`) — only visible inside that project. Useful when different pipelines need different DB users for the same host.

---

## 6. Connector status & Beta connectors

The connector picker shows status badges so you know what to expect:

| Status | What works | What might not |
|---|---|---|
| **Certified** | Auth + reads + pagination + sinks + error handling — production-grade | — |
| **Beta** | Auth + reads work; pagination may be partial; sinks may use basic INSERT (no bulk-load) | Schema drift logged but not enforced; test fixtures incomplete |

The first time you click a Beta connector, Revoluzen Flow shows a one-time dialog listing typical limitations so you're not surprised. Acknowledged once per browser via localStorage.

See the [connector catalog](../connectors.md) for the full list.

---

## 7. Credentials at rest

### Reuse a saved credential

Instead of typing a password into each connection, save it once on **Connections → Credentials**, then pick it under **Saved credential** when you create or edit a connection. The fields it holds fill themselves. For example, a credential with a `username` and a `password` fills those two fields on a Postgres connection, and you type only the host and database. The fields show which credential they come from, and the secret stays in the credential. Change the credential once and every connection using it picks up the new value on its next test or run.

Give a credential an **expiry date** and administrators are warned on the notification bell and by email a week ahead, a day ahead and when it expires. See [the notifications guide](../product_facts/09_notifications.md).

### How it is stored

Revoluzen Flow encrypts saved credentials using **Fernet (AES-128-CBC + HMAC-SHA256)**. The master key lives in `<REVOLUZEN_FLOW_DATA_DIR>/secret.key` (Docker default: `/data/secret.key`; or override the path via `REVOLUZEN_FLOW_MASTER_KEY_FILE`). POSIX permissions are enforced to `0600` on startup — the server fails closed if the perms are wider.

The plaintext password / API key is **never** returned by any API endpoint after save — neither in `GET /api/connections/{id}` nor in exports. To rotate, edit the connection and provide a new credential.

> Revoluzen Flow Enterprise is a paid extension that adds team-oriented governance on top of these primitives.

---

## 8. Version history

Every change to a connection keeps a version: its name, type, settings, linked credential, project, environment, roles, description and tags, with who changed it and when. Saving the form unchanged, or testing the connection, keeps none.

Open a connection and choose **History** at the top of the page:

- The versions are listed newest first, each with what changed ("Host and password changed"), who changed it and when.
- Click a version to set it against the settings now, setting by setting. **Show every setting** lists the ones that are the same too.
- **Restore version N…** puts the version's settings back. The restore is saved as a new version, so nothing is lost and a restore can itself be undone.

**Passwords and keys are never kept in the history.** Each one is kept as a fingerprint, a one-way code made with your install's master key that only shows whether two passwords are the same. So the history can say a password changed, never what it was. When the version you restore had a different password, Flow asks for it: type it to use it again, or leave it blank to keep today's password.

Before restoring, Flow lists the pipelines that use the connection, and which of them are published or live in PROD. Their next runs use the restored settings.

When restoring, Flow keeps today's value for:

- a linked credential, project or environment that no longer exists or that you can no longer use;
- the name, when another connection has it now. The restored connection gets a number instead, as in "Orders DB (2)".

Flow keeps the newest 50 versions of each connection and always its first one. Set `REVOLUZEN_FLOW_CONNECTION_VERSION_RETENTION` to change the number (`0` keeps every version). Deleting a connection deletes its history.

A connection made before version history existed has none until it is next changed. That change keeps its settings as they were as version 1, and the change as version 2.

---

## 9. API reference (essentials)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/connections/` | List connections (filter by `type`, `project_id`, `scope`) |
| `GET` | `/api/connections/types` | Supported connection types |
| `GET` | `/api/connections/metadata` | Types + categories + storage formats |
| `POST` | `/api/connections/` | Create |
| `GET` | `/api/connections/{id}` | Fetch one (credentials redacted) |
| `PUT` | `/api/connections/{id}` | Update |
| `DELETE` | `/api/connections/{id}` | Delete |
| `POST` | `/api/connections/{id}/test` | Live test |
| `GET` | `/api/connections/{id}/versions` | Kept versions, newest first, with each one's settings (secrets as tags only), the settings now and the pipelines that use the connection |
| `POST` | `/api/connections/{id}/versions/{n}/restore` | Restore version `n`. Body `{"secrets": {"password": "..."}}` for old passwords typed in; any other secret keeps today's value |

---

## 10. Revoluzen Flow Enterprise: team-oriented governance for connections

Revoluzen Flow Enterprise is a paid extension that adds team-oriented governance on top of OSS connections — see [revoluzen.io/flow](https://revoluzen.io/flow).

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "capability mismatch: source uses write-only connection" | Sink connection wired to source node | Pick a read-capable connection, or change capabilities |
| Test fails "password authentication failed" | Credential is wrong | Edit connection, re-enter credential |
| Connection saved but pipeline can't see it | Project-scoped connection in wrong project | Edit connection, change `project_id` or set to global |
| "Connector not found" in picker | Connector is hidden (Coming soon, Revoluzen Flow Enterprise, or unsupported) | Use Generic Source/Destination + REST API, or upgrade to Revoluzen Flow Enterprise |
| Master key file refuses startup | `~/.revoluzen-flow/secret.key` has wrong permissions (must be `0600` on POSIX) | `chmod 600 ~/.revoluzen-flow/secret.key` and restart. There is deliberately **no override** — the POSIX check is fail-closed with no fallback, since the app owns the file it created and `chmod 600` essentially cannot fail. (On Windows the loader repairs the ACL in place instead of refusing, and logs if it cannot.) |

---

**Change log**

| Date | Change |
|---|---|
| 2026-11-10 | Section 8, version history: every change kept, compared with the settings now and restored; passwords and keys kept only as fingerprints. |
| 2026-05-03 | Rewritten for Revoluzen Flow audience. Vault references + DEV/PROD scoping consolidated into section 9 (Revoluzen Flow Enterprise section). Connector status badges section added. |
| 2026-04-22 | Initial publication. |
