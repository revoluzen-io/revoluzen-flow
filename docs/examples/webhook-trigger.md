# Example: a webhook-triggered pipeline

Fire a pipeline the instant an external system POSTs to it, and use the JSON
body as pipeline inputs. This is the event-driven ("near-real-time") trigger
in Revoluzen Flow — an upstream app calls a URL, Revoluzen Flow runs the pipeline right then.

**How it works, end to end:**

```
POST /api/published/orders-webhook          (external system, with an API key)
  → gateway validates the API key + rate limit
  → JSON body becomes the run's parameter_values
  → ${param.<name>} placeholders in the pipeline resolve to those values
  → the pipeline executes; rows land in a managed table
```

The body-to-pipeline binding is covered by a test that runs this exact file: a
body of `{"country":"US","min_quantity":2}` resolves the filter to
`country = 'US' AND quantity >= 2`, and the run returns `status: success`.

---

## 1. The pipeline

Three nodes: a **Source** reads the sample orders file, a **Filter** keeps the
rows the caller asked for, and a **Local Table Sink** writes them to a managed
table. It needs no connection, because the file ships with the repository.

Save this as `webhook-order-pipeline.json` and import it (Pipelines →
Import), or build the three nodes in the editor and copy the params.

```json
{
  "id": "webhook-order-demo",
  "name": "Webhook: Orders for one country → Managed Table",
  "description": "Fired by POST /api/published/orders-webhook; the JSON body binds to ${param.*}.",
  "parameters": [
    { "name": "country", "type": "string", "required": true, "description": "Two-letter country code to keep" },
    { "name": "min_quantity", "type": "int", "default": 1, "description": "Smallest order quantity to keep" }
  ],
  "steps": [
    {
      "id": "src",
      "type": "source",
      "label": "Orders file",
      "params": {
        "connector_type": "csv",
        "file_path": "samples/free-api-pipelines/data/orders.csv",
        "format": "csv",
        "delimiter": ",",
        "header": true
      }
    },
    {
      "id": "flt",
      "type": "filter",
      "label": "Keep one country",
      "params": {
        "mode": "expression",
        "condition": "country = '${param.country}' AND quantity >= ${param.min_quantity}"
      }
    },
    {
      "id": "sink",
      "type": "local_table_sink",
      "label": "Replace webhook_orders",
      "params": { "schema_name": "default", "table_name": "webhook_orders", "mode": "replace" }
    }
  ],
  "connections": [
    { "from_step": "src", "to_step": "flt" },
    { "from_step": "flt", "to_step": "sink" }
  ]
}
```

> **Parameters** are the contract with the webhook. Each `${param.name}` in a
> node's config is replaced at run time by the matching key from the JSON body
> (or its `default` if absent). Types are coerced: `string` / `int` / `float`
> / `bool` / `json`. An unknown key, a `required` parameter that's missing or a
> value of the wrong type gets a `400` naming it, and nothing runs. Defaults may
> also use system placeholders: `${utcnow}`, `${utcnow:%Y-%m-%d}`, `${run_id}`.

---

## 2. Publish the pipeline and create an API key

The quickest way is the **Workflows → Triggers** page: **New endpoint**, pick
the pipeline, set the path to `orders-webhook`, then **New API key**. The page
gives you the finished command under **Ready-to-use command**.

To script it instead, these calls need your session token (`Authorization: Bearer
<token>` — grab it from the browser DevTools → Application → Local Storage →
`revoluzen_flow_token`, or from the login API). Assume the server is on the default
`http://127.0.0.1:8001`.

```bash
TOKEN="<your session JWT>"

curl -sX POST http://127.0.0.1:8001/api/gateway/keys \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"orders-webhook","scopes":["execute"],"rate_limit_rpm":120}'
# → {"id":"...", "api_key":"fpk_...", ...}   ← copy `api_key`; it is shown once

curl -sX POST http://127.0.0.1:8001/api/gateway/endpoints \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "workflow_id": "webhook-order-demo",
        "path": "orders-webhook",
        "method": "POST",
        "require_api_key": true,
        "rate_limit_rpm": 120
      }'
```

The pipeline is now live at `POST /api/published/orders-webhook`.

## 3. Fire the webhook

This is the only call the **external system** makes — it needs just the API
key (no user login):

```bash
APIKEY="fpk_..."   # from step 2

curl -sX POST http://127.0.0.1:8001/api/published/orders-webhook \
  -H "X-API-Key: $APIKEY" \
  -H "Content-Type: application/json" \
  -d '{"country":"US","min_quantity":2}'
```

The call waits for the run to finish and answers with the outcome, never the data:

```json
{
  "status": "success",
  "execution_id": "3a2f2b088479",
  "pipeline_id": "webhook-order-demo",
  "parameter_values": { "country": "US", "min_quantity": 2 },
  "duration_ms": 180.4,
  "steps": [
    { "step_id": "src", "name": "Orders file", "status": "success", "row_count": 400 },
    { "step_id": "flt", "name": "Keep one country", "status": "success", "row_count": 57 },
    { "step_id": "sink", "name": "Replace webhook_orders", "status": "success", "row_count": 57 }
  ]
}
```

Each call replaces the `webhook_orders` managed table (see the **Storage** page),
and the run appears on the **Executions** page with the trigger **API**. On a
local install the API key can also be passed as `?api_key=...` if a header is
inconvenient for the caller. A server install accepts the header only.

---

## How this fits the "runs without stopping" model

Revoluzen Flow's engine is batch/micro-batch, not a continuous stream processor. The
**server + scheduler stay up 24/7** (run it as a service with `revoluzen-flow
install-service`, or in Docker with a restart policy). "Real-time" comes from
two triggers:

- **Webhook (this example)** — the upstream system pushes an event and the
  pipeline fires immediately. Best when you control the source. An endpoint can
  also require signed calls instead of a key, for senders that sign webhooks.
- **File arriving** — for files landing in a folder or bucket, a file-arrival
  trigger starts one run per new file, with its path as a parameter. See
  [Scheduling and triggers](../user-guides/pipelines.md#7-scheduling-and-triggers).
- **Interval schedule** — for sources you must poll (a table or a queue),
  schedule the pipeline every N seconds/minutes for micro-batch.

## Notes for production

- **Security:** keep `require_api_key: true`. The gateway also supports
  per-key `rate_limit_rpm` and an `ip_allowlist`. Because parameter values are
  substituted as text, treat a webhook whose caller you don't control as
  untrusted input — prefer typed parameters (`int`/`float`/`bool`) and avoid
  interpolating free-text straight into SQL, as this example's `country` does.
  An endpoint with no key is refused if its pipeline does that. The DB Source
  also blocks `;` and validates identifiers, but validate business fields yourself.
- **Idempotency:** if the caller may retry, use `mode: "merge"` with
  `merge_on: ["order_id"]` on the sink so repeats upsert instead of duplicating.
- **Persisting elsewhere:** swap `local_table_sink` for `db_sink`, `s3_sink`,
  `api_sink`, etc. to land the event in your warehouse/lake instead of a
  managed table.
