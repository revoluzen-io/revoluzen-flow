# Projects — User Guide

**Audience:** Revoluzen Flow users
**Prerequisites:** An Revoluzen Flow install up and running (see the [Quickstart](../quickstart.md))

A **Project** in Revoluzen Flow is a logical container that groups related pipelines and connections. Projects are how you keep work organized once you have more than a handful of pipelines on the canvas.

> ℹ️ **Revoluzen Flow is single-user.** Membership, role-based access control, and approval workflows are Revoluzen Flow Enterprise features. This guide covers the OSS Free experience; Plus extras are summarised at the end.

---

## Table of contents

1. [What a project is](#1-what-a-project-is)
2. [When to create a new project](#2-when-to-create-a-new-project)
3. [The Default project](#3-the-default-project)
4. [Creating a project](#4-creating-a-project)
5. [Updating a project](#5-updating-a-project)
6. [Deleting a project](#6-deleting-a-project)
7. [API reference](#7-api-reference)
8. [Troubleshooting](#8-troubleshooting)
9. [What's next](#9-whats-next)
10. [Multi-user features in Revoluzen Flow Enterprise](#10-multi-user-features-in-revoluzen-flow)

---

## 1. What a project is

A project is a named grouping of related work. Every pipeline in Revoluzen Flow belongs to exactly one project, and every connection is either scoped to a project or shared globally across the workspace.

A project has:

| Field | Meaning |
|---|---|
| `id` | System-assigned unique identifier (hex) |
| `name` | Human-readable name — shown in the project panel on the Pipelines page and in the editor's project ribbon |
| `description` | Free-text explanation of what the project is for |
| `color` / `icon` | Visual accent used by the project chip throughout the UI |
| `workspace_id` | The workspace the project belongs to (a project cannot move between workspaces) |
| `metadata` | Free-form JSON for tags and labels |

Projects live for the lifetime of the workspace. Archiving a pipeline does not archive its project; projects are only removed when you explicitly delete them.

> ℹ️ **Why projects exist.** Without projects, every pipeline and every connection sits in one flat list — impossible to scan, impossible to scope. Projects give you room to grow without losing track of what belongs where.

---

## 2. When to create a new project

Create a new project when:

- **A new domain of work starts** — e.g. "Sales Analytics", "Customer 360", "Finance Close". Each domain typically has its own pipelines and connections.
- **A short-lived experiment starts** — a project is a cheap container; it's easier to delete a project than to untangle 40 experimental pipelines from your main project.

Don't create a new project when:

- It's a single pipeline in an existing domain. Add it to the existing project.
- It's a reusable connection that several pipelines will use. Use a **global-scope connection** instead (see the [Connections Guide](connections.md)).

---

## 3. The Default project

Every Revoluzen Flow installation ships with a built-in project named **Default**. It cannot be deleted.

The Default project exists for two reasons:

1. **Any pipeline created without an explicit `project_id` lands here.** This keeps API calls and migrations from failing when the caller doesn't know which project they want.
2. **It gives you a working space on day one.** You can use Revoluzen Flow for weeks without ever creating a project — everything just goes to Default.

Most users outgrow Default quickly. The moment you have two or more distinct bodies of work, create real projects and move pipelines into them.

> ⚠️ **Default cannot be deleted.** The API returns HTTP 400 if you try. This is intentional: deleting Default would orphan any pipeline still pointing to it.

---

## 4. Creating a project

### 4.1 Via the UI

1. Open **Pipelines** in the sidebar. The project panel is on the left of the list; on a narrow screen, click **Projects** above the list to open it.
2. Click **New** at the top of the panel.
3. Fill in:
   - **Name** (required) — e.g. `Sales Analytics`
   - **Description** (recommended) — one sentence explaining the scope
   - **Color** — one of the preset accent colors
   - **Icon** — one of the preset icons
4. Click **Create**.

The project appears in the panel immediately. Click it to list its pipelines. Its **⋯** menu adds a folder, edits or exports the project, pins it to the top of the panel, or archives it.

### 4.2 Via the API

```bash
curl -X POST http://localhost:8001/api/projects/ \
  -H "Content-Type: application/json" \
  -H "Cookie: session=..." \
  -d '{
    "name": "Sales Analytics",
    "description": "Revenue, pipeline, quota",
    "color": "#7C3AED",
    "icon": "bar-chart"
  }'
```

Response:

```json
{
  "id": "proj_4f7c...",
  "name": "Sales Analytics",
  "workspace_id": "ws_default",
  "created_at": "2026-04-22T10:30:00Z",
  ...
}
```

### 4.3 What happens on creation

- A row is written to the `projects` table in the workspace's SQLite store.
- The project is immediately visible in the project panel on the Pipelines page.
- A pipeline started with **New Pipeline** while the project (or one of its folders) is picked in the panel is saved there. Pipelines can also be assigned to it from the Editor's Save dialog, or moved in with **Move** on their row.

---

## 5. Updating a project

Editable fields: `name`, `description`, `color`, `icon`, `metadata`.
Read-only fields: `id`, `workspace_id`, `created_at`.

**Via UI:** on the Pipelines page, pick the project in the panel and click **Edit project** above the list (or choose **Edit project** in its **⋯** menu), change the fields, click **Save**.

**Via API:**

```bash
curl -X PUT http://localhost:8001/api/projects/proj_4f7c... \
  -H "Content-Type: application/json" \
  -H "Cookie: session=..." \
  -d '{
    "name": "Sales Analytics (Q2)",
    "description": "Revenue, pipeline, quota — refocused for Q2"
  }'
```

Response: the full updated project.

> ⚠️ **Renames are not historised in OSS.** The audit log of past names is an Revoluzen Flow Enterprise feature.

---

## 6. Deleting a project

**What gets deleted:** the project row. Pipelines and connections scoped to the project are **not** cascade-deleted — they become orphaned. Handle them first.

### 6.1 Pre-delete checklist

Before deleting a project:

1. **List its pipelines.** `GET /api/projects/{id}/pipelines`. Archive or move each one.
2. **List its connections.** `GET /api/connections/?project_id={id}`. Delete or re-scope each one.

### 6.2 Delete

**Via UI:** in the project panel on the Pipelines page, open the project's **⋯** menu and choose **Archive project**. Archived projects are listed under **Archived projects** at the bottom of the panel: **Restore** brings one back, and **Delete now** removes it for good after you confirm.

**Via API:**

```bash
curl -X DELETE http://localhost:8001/api/projects/proj_4f7c... \
  -H "Cookie: session=..."
```

Response:

```json
{"deleted": true}
```

### 6.3 What happens to orphaned pipelines

Pipelines that still reference the deleted project's ID will:

- Still execute (the project ID is stored as a string; the pipeline doesn't need the project to exist).
- Are listed under **Unassigned** in the project panel (their project no longer exists).
- Continue to appear on the Pipelines page.

Recover them by editing each pipeline and assigning it to a valid project, or by cloning into a new project.

---

## 7. API reference

Base path: `http://localhost:8001/api/projects`

### List projects

```
GET /api/projects/
```

Returns every project in the current workspace, enriched with `pipeline_count` per project.

**Query params:** none.
**Response:** `200 OK` → array of project objects.

### Create a project

```
POST /api/projects/
```

**Body:**
```json
{
  "name": "string",
  "description": "string",
  "color": "#hex",
  "icon": "string",
  "metadata": {}
}
```

**Response:** `200 OK` → full project object.

### Get a project

```
GET /api/projects/{project_id}
```

**Response:** `200 OK` → project object, or `404 Not Found` if the id is unknown.

### Update a project

```
PUT /api/projects/{project_id}
```

**Body:** any subset of editable fields (partial update).
**Response:** `200 OK` → updated project object.

### Delete a project

```
DELETE /api/projects/{project_id}
```

**Response:** `200 OK` → `{"deleted": true}`, or `400 Bad Request` for the Default project.

### List pipelines in a project

```
GET /api/projects/{project_id}/pipelines
```

**Response:** `200 OK` → array of workflow objects.

> The membership and approval endpoints (`/submit-for-approval`, `/approve`, `/reject`, `/pending-approvals`) are Revoluzen Flow Enterprise only; they return 404 on OSS.

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "Cannot delete this project" | Tried to delete the Default project | Default cannot be deleted. If you want an empty fallback, archive its pipelines instead. |
| `POST /projects/` returns 400 | Missing `name` field | `name` is required; send a non-empty string. |
| Project vanishes after workspace switch | Projects are workspace-scoped by design | Switch back to the workspace where the project lives. |
| A pipeline I expected is missing from a project | Pipeline was assigned to a different project | Open the Pipelines page (admins see all), find it, and reassign via Save dialog. |

---

## 9. What's next

Now that you have a project:

- **Build your first pipeline** in it → [Pipelines Guide](pipelines.md)
- **Set up connections** for the pipeline's sources and sinks → [Connections Guide](connections.md)

---

## 10. Multi-user features in Revoluzen Flow Enterprise

OSS is built for a single developer on a laptop. When you need several people working in the same install, Revoluzen Flow Enterprise adds:

- **Project members and ownership** — grant per-project access; everyone outside the member list cannot see the project.
- **5-tier role-based access control** — `super_admin`, `admin`, `lead`, `data_engineer`, `analyst`, `viewer`. Read/write rules vary by role.
- **Approval workflow** — submit a project for approval before it becomes visible to non-admins; admins approve or reject with notes.
- **Audit log of project changes** — historised renames, member edits, approval decisions.
- **Per-environment scoping (DEV/PROD)** — projects in PROD inherit stricter approval and read-only defaults.

See `edition-matrix.md` at the repository root for the canonical capability split, and the [Revoluzen Flow vs Revoluzen Flow Enterprise](../editions.md) doc for a user-facing summary.

---

**Document change log**

| Date | Change |
|---|---|
| 2026-11-10 | Projects moved into the project panel on the Pipelines page; the separate Projects page is gone, and old `#projects` links open the panel. |
| 2026-05-09 | Rewritten for OSS-only audience; Plus features (members, approval workflow, RBAC) consolidated to section 10. |
| 2026-04-22 | Initial publication against Revoluzen Flow schema v19. |
