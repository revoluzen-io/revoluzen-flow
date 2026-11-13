# Triggering pipelines from external systems

This guide is for integrators wiring Revoluzen Flow pipelines into CI jobs, peer services, scheduled tasks in other tools, or public webhooks. If you just want to "run a pipeline by hand from the UI", that's the **Run** button on the Pipelines page — this guide isn't for you.

There are two ways to invoke a Revoluzen Flow pipeline from outside the product:

1. **Published endpoint** — for other applications, CI jobs and webhooks. Callers prove who they are with an API key, a signature made with a shared secret, or both. Set it up on the **Workflows → Triggers** page.
2. **Authenticated API** — for tools that already hold a signed-in user's session token.

To start a pipeline when a file lands in a folder, or when another pipeline finishes, no outside call is needed: see [Scheduling and triggers](pipelines.md#7-scheduling-and-triggers).

Both can pass parameter values that the pipeline uses at runtime. Both run through the same path as the **Run** button, so the run is admitted to the pool, recorded on the Executions page and followed by the usual alerts.

---

## 1. Published endpoint with an API key

The caller needs no user login. It calls a stable URL and proves it may do so with an API key.

### Set it up

1. Open **Workflows → Triggers**, or choose **Run from another app** in a pipeline's row menu on the Pipelines page.
2. Click **New endpoint**, pick a saved pipeline, check the URL path, and choose how callers prove who they are: **An API key**, **A signature (for webhooks)**, or **No check**.
3. For a key, click **New API key**. For a signature, Flow makes the secret when you create the endpoint. Either is shown once, so copy it before you close the dialog.
4. Copy the command under **Ready-to-use command**. It holds the real URL and a body built from the pipeline's parameters, and for a signed endpoint the lines that sign the call. Choose **curl** for macOS, Linux and Git Bash (the signing needs `openssl`), or **PowerShell** for Windows.

### Call it

**Endpoint:** `POST /api/published/{your-path}`

The request body **is** the parameter values, with no wrapper.

```bash
curl -X POST 'https://your-revoluzen-flow.example.com/api/published/customer-onboarding' \
  -H 'X-API-Key: YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
    "customer_id": "cust_42",
    "tier": "enterprise"
  }'
```

An empty body is fine for a pipeline with no required parameters. In server mode the key must be in the `X-API-Key` header. On a local install `?api_key=` also works.

### Response

The call waits for the run to finish, then returns the outcome. It never returns your data.

```json
{
  "status": "success",
  "execution_id": "3a2f2b088479",
  "pipeline_id": "d5c145430ad9",
  "parameter_values": { "customer_id": "cust_42", "tier": "enterprise" },
  "duration_ms": 412.6,
  "steps": [
    { "step_id": "src", "name": "Customers", "status": "success", "row_count": 1200 },
    { "step_id": "flt", "name": "Keep this customer", "status": "success", "row_count": 1 }
  ]
}
```

`parameter_values` shows what the run really used, with defaults filled in. Open the run on the Executions page, by its `execution_id`, for logs and captured samples.

A run that starts and then fails still returns `200`. Its `status` is `error`, the failed step is marked, and `message` says where to look. The error text stays on the run record, because it can name files, tables or values. If the pipeline's overlap policy turns the call away, `status` is `skipped` or `queued` and `running_execution_id` names the run in the way.

### Status codes

| Code | Meaning |
|---|---|
| `200` | The call was handled. Read `status` in the body for the outcome of the run. |
| `400` | The body is not a JSON object, or a parameter is unknown, missing or of the wrong type. The message names it, and nothing runs. |
| `401` | No API key was sent, or a signed call's headers are missing, its signature does not match, or it is too old. |
| `403` | The key is wrong, revoked, expired or from another workspace, or the caller's address is not on the key's allowlist. |
| `404` | No endpoint has this path, or its pipeline was deleted. |
| `405` | The endpoint accepts a different HTTP method. |
| `409` | A signed call's `webhook-id` was already accepted, so no second run started. |
| `423` | The pipeline is deactivated or archived. |
| `429` | Too many calls. The limit is per key and per endpoint, and per endpoint when no key is required. |
| `503` | The endpoint is turned off. |

### Signed calls (webhooks)

A signed endpoint needs no key. The sender signs each call with the secret Flow made, in the open Standard Webhooks format, which has libraries for most languages: a `webhook-id` for the message, a `webhook-timestamp`, and a `webhook-signature` over both and the body. Flow refuses a call whose signature does not match (wrong secret, or the body was changed on the way), one more than 5 minutes from its clock, and a message id it has already accepted, so a captured call cannot be sent again. The run shows as **Webhook** on the Executions page.

The secret is kept encrypted and shown only when it is made. **Make a new secret**, under the command for that endpoint, replaces it at once: calls signed with the old one are refused from then on. The exact format, with a Python example, is in the [API reference](../api.md#signed-calls).

### Endpoints without a key or signature

Choosing **No check** lets anyone who can reach the server start the pipeline. Parameter values are substituted as plain text, so a value placed in SQL could change the query. For that reason an endpoint with neither a key nor a signature is refused, when you publish it and again on every call, if its pipeline has a `${param.x}` or `{{ }}` expression in a SQL field. The API's `allow_templated_sql_public` flag overrides this, and that is not recommended.

---

## 2. Authenticated API

**Endpoint:** `POST /api/execute/workflow/{workflow_id}`

The request body is optional. The UI's **Run** button uses this route. Callers add `parameter_values` to set the pipeline's parameters for that one run.

```bash
curl -X POST 'https://your-revoluzen-flow.example.com/api/execute/workflow/wf_abc123' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer YOUR_SESSION_TOKEN' \
  -H 'X-Workspace-Id: my-workspace' \
  -d '{
    "parameter_values": {
      "region": "eu-west-1",
      "run_date": "2026-05-12"
    }
  }'
```

### Query-string knobs (compatible with the UI)

These remain in the URL, not the body:

| Knob | Default | Effect |
|---|---|---|
| `preview_limit` | `50` | Rows to retain in each step's preview slot. |
| `safety_mode` | `live` | `sample`, `dry_run` or `validate_only` to run without writing to real destinations. |

### Response

Same shape as a UI-triggered run — see [API reference: Execution Response](../api.md#execution-response). This response includes sample rows, so do not pass it on to anyone who should not see the data.

---

## When to use which

| Question | Use this path |
|---|---|
| Caller is another application, a CI job or a SaaS that only sends webhooks | Published endpoint |
| Caller should not hold a user's login | Published endpoint |
| Need rate limiting on the trigger | Published endpoint (per key and per endpoint) |
| Need a stable URL like `/api/published/customer-onboarding` | Published endpoint |
| Caller already has a signed-in user's session token and wants the step samples back | Authenticated API |

---

## How values reach the pipeline

Declare a parameter in the Editor with the **Parameters** button, then reference it in any step field as `${param.NAME}`:

```text
filter:
  condition: region = '${param.region}' AND order_date = '${param.run_date}'
```

For each declared parameter the run uses the first of these that exists:

1. **The value in the call** (this run only, recorded on the run but never saved to the pipeline)
2. **The parameter's declared default**, which may use `${utcnow:%Y-%m-%d}` or `${run_id}`

A `required` parameter with neither is an error before any step runs. Values are coerced to the declared type: `string`, `int`, `float`, `bool` or `json`.

---

## Declaring parameters in a pipeline file

```json
{
  "name": "Regional sales pipeline",
  "parameters": [
    { "name": "region",     "type": "string", "required": true, "description": "Region to load" },
    { "name": "batch_size", "type": "int",    "default": 1000 },
    { "name": "dry_run",    "type": "bool",   "default": false }
  ],
  "steps": [ /* ... */ ],
  "connections": [ /* ... */ ]
}
```

A value for a parameter the pipeline does not declare is an error, so a typo surfaces at once instead of being ignored. A published endpoint answers with `400`. The authenticated API answers `200` with this body:

```json
{
  "workflow_id": "wf_abc123",
  "status": "error",
  "step_results": {
    "parameter_resolution": {
      "step_id": "parameter_resolution",
      "status": "error",
      "error": "Parameter resolution failed: Required parameter(s) missing: region"
    }
  }
}
```

---

## Idempotency and overlap

If two callers trigger the same pipeline at the same time, the overlap policy (set in the pipeline's Execution settings) decides what happens:

| Policy | Behavior |
|---|---|
| `parallel` (default) | Both runs proceed. |
| `skip` | The second caller gets `status: "skipped"` with the running run's ID. |
| `queue` | The second caller gets `status: "queued"` with the running run's ID. No run starts, so call again later. |
| `cancel_previous` | The first run is cancelled; the new caller proceeds. |

For external triggers, `skip` is usually safer than `parallel`.

---

## Common patterns

### CI: trigger nightly with a date

```yaml
# Example CI step
- name: Run Revoluzen Flow nightly pipeline
  run: |
    curl -fsS -X POST 'https://your-revoluzen-flow.example.com/api/published/nightly' \
      -H 'Content-Type: application/json' \
      -H 'X-API-Key: ${{ secrets.REVOLUZEN_FLOW_API_KEY }}' \
      -d '{"run_date": "${{ github.event.repository.updated_at }}"}'
```

`curl -f` only fails on an HTTP error. A run that started and failed returns `200` with `status: "error"`, so check the body if the job should fail with the pipeline.

### Long-running pipelines

Both routes are synchronous: the response returns when the run completes, so give the caller a timeout longer than the pipeline takes. While it runs, the Execution Pool page shows it.

### Multi-tenant pipelines

Same pipeline definition, different tenant per invocation:

```bash
curl -X POST '.../api/published/tenant-sync' \
  -H 'X-API-Key: YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id": "acme-corp"}'
```

---

## What this is NOT

- **Not a queueing system.** Revoluzen Flow runs the pipeline immediately when you POST. If you want long-running asynchronous workflows with retries, put your own queue in front of it and use the endpoint's rate limit.
- **Not for high-frequency calls.** Source nodes spin up DuckDB; this is heavyweight per request. Don't call this from a per-event hot path.
- **Not for storing per-call state.** Parameter values are recorded on the run but not kept for the next one. If you need to remember a value across runs, write it from inside the pipeline.

---

## See also

- [Example: a webhook-triggered pipeline](../examples/webhook-trigger.md) — a pipeline you can import and call
- [API reference: Execution](../api.md#execution) — every endpoint flag and the full response schema
- [Pipelines guide](pipelines.md) — building and publishing pipelines
