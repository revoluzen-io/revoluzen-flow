# Editor E2E (Playwright)

Functional + smoke tests that open every visible node in the editor, render its
config, and (functionally) run nodes through the real backend/DuckDB engine.

This is the durable answer to "check each node's frontend / each functionality
with a sample connection" — it drives the **real** input pipeline, so React's
`onChange` fires (which programmatic `page.evaluate` value-setting can't do).

## What it covers
- **`node-configs.spec.ts`** — opens the config of every visible palette node
  (~40), asserts the config body mounts, a Parameters tab exists, and there's no
  React error boundary. Catches missing dispatches, crashing configs, and
  registry/palette drift.
- **`functional.spec.ts`** — runs the full FE → backend → engine path:
  - DB Source `SELECT 1` against the in-memory engine (no connection) → expects a row.
  - Source via a **sample DB connection** (skips if none exists) → Test Node → expects rows.

## Prerequisites
- A running Revoluzen Flow app (frontend + backend). The config defaults to
  `http://localhost:5174` and **reuses** an already-running dev server.
- Chromium for Playwright: `npm run test:e2e:install` (first time only).

## Run it
```powershell
# point at your app + provide login creds (nothing is committed)
$env:REVOLUZEN_FLOW_URL = "http://localhost:5174"
$env:REVOLUZEN_FLOW_E2E_EMAIL = "admin@revoluzen.local"
$env:REVOLUZEN_FLOW_E2E_PASSWORD = "<your password>"

npm run test:e2e            # headless
npm run test:e2e:ui         # interactive UI mode
npx playwright show-report  # last HTML report
```

`global-setup.ts` logs in once and caches the session in `e2e/.auth/state.json`
(gitignored); specs reuse it. Set `NO_SERVER=1` to skip the auto dev-server.

## Popover / drawer placement

Two layers, because they answer different questions.

- **`popover-placement.spec.ts`** (part of `npm run test:e2e`) — the standing
  regression guard. Opens a handful of named controls at **900px** and asserts
  the panel that appears is inside the viewport.
- **`npm run check:popovers`** — the breadth net. Opens *every* menu-ish trigger
  on all 11 pages at 1280px and 900px, plus the drawers, and exits non-zero if
  any panel escapes the viewport. Run it after changing the global nav, the
  editor toolbar, or anything anchored with `absolute right-0`.

```powershell
$env:REVOLUZEN_FLOW_URL = "http://localhost:5174"   # default
npm run check:popovers
# REVOLUZEN_FLOW_WIDTHS="1600,1280,900,768"  to sweep other widths
# REVOLUZEN_FLOW_E2E_STATE=<path>            to use a different saved login
```

It reuses `e2e/.auth/state.json`, so run the E2E suite once first to create it.

**Why both.** `absolute right-0` pins a panel's right edge to its *trigger*, which
is only safe while that trigger sits near the right edge of the window — a
property of the layout, not the component, so it breaks whenever the nav
changes. The Steward findings panel was correct at 1920px and had its first
~230px off-screen at 900px, unreachable, and nothing caught it: the layout
checks that existed only measured default page states, so no menu was ever
opened.

The sweep starts with a **self-test** that injects a panel 233px off the left
edge and aborts if the detector misses it. That is not ceremony — three earlier
versions of this check could not have failed and all reported success (they
measured the largest floating box rather than the panel under test; returned
`undefined` from a templated `evaluate` string via semicolon insertion; and let
a destructive-word blocklist skip the one broken panel because its tooltip
contained "duplicate"). Treat a green run as meaningless unless the self-test
line is present, and read the "triggers NOT opened" list at the end — a menu
hiding in there is a menu nobody checked.

## Extending
Add a node to the `NODES` list in `node-configs.spec.ts` to include it in the
smoke net. Add deterministic runs (fill required fields → **Test Node** →
assert output) to `functional.spec.ts` for nodes whose execution doesn't need
external state. Helpers (`addNode`, `openConfig`, …) are in `helpers.ts`.
