/**
 * Non-component exports extracted from ModulesPanel.tsx so React Fast
 * Refresh can hot-reload ModulesPanel cleanly. Vite's Fast Refresh
 * requires component-only exports per file; mixing constants/hooks/types
 * with React components forces full HMR invalidation, which wipes
 * module-level state (e.g. the license cache in api/client.ts) on every
 * file change → backend log spam + brief tier flicker.
 *
 * Anything imported by both ModulesPanel and other surfaces (ActivitiesRibbon)
 * lives here. Components stay in ModulesPanel.tsx.
 */
import { useEffect, useMemo, useState } from 'react';
import { HIDDEN_TYPES } from './hiddenNodeTypes';

/**
 * Progressive disclosure level for the palette (D11/12/13, 2026-05-18).
 *   - 'beginner'     — must-have nodes everyone needs from day one.
 *   - 'intermediate' — common transforms most users reach for within
 *                     a couple of pipelines.
 *   - 'advanced'     — specialist primitives (retry_handler, materialize,
 *                     conditional_split, SCD2, semantic_router, etc.).
 *                     Hidden by default; revealed when the user picks
 *                     "All" in the palette level filter.
 *
 * Items without an explicit level default to 'intermediate' so adding
 * a new node doesn't accidentally bury it in Advanced.
 */
export type ModuleLevel = 'beginner' | 'intermediate' | 'advanced';

export interface ModuleItem {
  type: string;
  label: string;
  description?: string;
  level?: ModuleLevel;
}

export interface ModuleCategory {
  name: string;
  items: ModuleItem[];
}

/**
 * C1 — User-intent categorization (2026-05-18).
 *
 * The engine-categorization (Data Movement / Transform / Combine /
 * Control Flow / Action / AI / Semantic) is internally accurate but
 * mentally awkward for first-time users — "is HTTP Request a transform
 * or an action?" Reviewer 1 strongly recommended re-grouping the
 * palette by what the user is TRYING TO DO instead:
 *
 *   Import   — read data in
 *   Prepare  — clean, shape, validate
 *   Analyze  — combine, aggregate, classify
 *   Automate — call APIs, send messages, branch on conditions
 *   Publish  — write data out
 *
 * The Basic + Standard palette levels use this grouping; All keeps the
 * engine grouping for power users who think in primitives.
 */
export type UserIntent = 'Import' | 'Prepare' | 'Analyze' | 'Automate' | 'Publish';

export const INTENT_FOR_STEP_TYPE: Record<string, UserIntent> = {
  // ── Import (sources) ──
  source: 'Import', csv_source: 'Import', json_source: 'Import',
  parquet_source: 'Import', excel_source: 'Import', xml_source: 'Import',
  db_source: 'Import', api_source: 'Import', s3_source: 'Import',
  kafka_source: 'Import', ftp_source: 'Import', gsheet_source: 'Import',
  delta_source: 'Import', sharepoint_source: 'Import', onedrive_source: 'Import',
  gdrive_source: 'Import', dropbox_source: 'Import', box_source: 'Import',
  adls_gen2_source: 'Import', azure_blob_source: 'Import', gcs_source: 'Import',
  webhook_trigger: 'Import',
  // 2026-06-10: managed local-table nodes were missing here, so the
  // intent regrouper dropped them into the default 'Automate' bucket —
  // a table READER filed under Automate while every other reader sits
  // under Import.
  local_table_source: 'Import', saas_connector: 'Import',
  // ── Prepare (single-relation transforms + quality) ──
  data_wrangler: 'Prepare', filter: 'Prepare', derived_column: 'Prepare',
  schema_mapper: 'Prepare', sort: 'Prepare', deduplicate: 'Prepare',
  sample: 'Prepare', data_quality: 'Prepare', flatten_explode: 'Prepare', split_out: 'Prepare',
  upsert: 'Prepare', rename: 'Prepare', typecast: 'Prepare',
  validate: 'Prepare', llm_guardrail: 'Prepare',
  // ── Analyze (multi-relation + semantic classification) ──
  transform: 'Analyze', join: 'Analyze', lookup: 'Analyze', union: 'Analyze',
  aggregate: 'Analyze', pivot: 'Analyze', unpivot: 'Analyze', window: 'Analyze',
  embedder: 'Analyze', semantic_router: 'Analyze',
  scd2: 'Analyze',
  // ── Automate (calls, messages, control flow) ──
  http_request: 'Automate', send_email: 'Automate', slack_notify: 'Automate',
  code_script: 'Automate', get_metadata: 'Automate', copy_data: 'Automate',
  execute_pipeline: 'Automate', execute_sql_task: 'Automate',
  file_system: 'Automate', delete_data: 'Automate',
  set_variable: 'Automate', append_variable: 'Automate', filter_array: 'Automate',
  if_condition: 'Automate', switch_case: 'Automate', lookup_activity: 'Automate',
  conditional_split: 'Automate',
  foreach_loop: 'Automate', foreach_pipeline: 'Automate', until_loop: 'Automate', wait_delay: 'Automate',
  retry_handler: 'Automate', fail: 'Automate', validation: 'Automate',
  // ── Publish (sinks) ──
  destination: 'Publish', csv_sink: 'Publish', json_sink: 'Publish',
  excel_sink: 'Publish', parquet_sink: 'Publish', db_sink: 'Publish',
  s3_sink: 'Publish', kafka_sink: 'Publish', api_sink: 'Publish',
  email_sink: 'Publish', delta_sink: 'Publish', warehouse_sink: 'Publish',
  file_sink: 'Publish', sharepoint_sink: 'Publish', onedrive_sink: 'Publish',
  gdrive_sink: 'Publish', dropbox_sink: 'Publish', box_sink: 'Publish',
  adls_gen2_sink: 'Publish', azure_blob_sink: 'Publish', gcs_sink: 'Publish',
  output: 'Publish',
  local_table_sink: 'Publish',  // 2026-06-10 — see local_table_source note above
};

// D11/12/13 — Progressive disclosure (2026-05-18).
// `level` per item drives the palette's Basic / Standard / All filter.
//   beginner     → always visible (Sources, Wrangler, SQL, Join, Aggregate, Output)
//   intermediate → default-visible (most common transforms)
//   advanced     → hidden in Basic + Standard; visible in All only
//
// 2026-10-28: moved here from ModulesPanel.tsx. Two reasons. It is data,
// and this file is where data lives so Fast Refresh sees the panel as
// component-only. And VALID_GHOST_TYPES below has to be derived from it —
// it used to be a second hand-maintained list and had drifted in both
// directions.
/** A node type's name as the palette shows it, e.g. local_table_sink is "Managed Table Sink". */
export const nodeTypeLabel = (type: string): string =>
  MODULES.flatMap((category) => category.items).find((item) => item.type === type)?.label
    ?? type.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());

export const MODULES: ModuleCategory[] = [
  {
    name: 'Data Movement',
    items: [
      // Generic Source & Destination — pick connector inside config.
      // Microsoft Graph / Salesforce / SAP / Oracle / Postgres etc. are
      // all surfaced via these two nodes; the connector type is selected
      // in the node config and the saved connection (Connections page)
      // carries auth + endpoint config.
      { type: 'source', label: 'Source', level: 'beginner' },
      { type: 'destination', label: 'Destination', level: 'beginner' },
      { type: 'copy_data', label: 'Copy Data', level: 'intermediate' },
      // Z11 (2026-05-23) — managed workspace Parquet tables (Storage
      // page → Managed Tables). These are NOT generic connectors —
      // they're the workspace's internal materialised storage, so they
      // earn dedicated nodes. Before this fix they fell through to an
      // orphan "Destination" bottom-of-palette category with no icon.
      { type: 'local_table_source', label: 'Managed Table Source', level: 'intermediate', description: 'Read from a workspace-managed Parquet table by schema.name' },
      { type: 'local_table_sink', label: 'Managed Table Sink', level: 'intermediate', description: 'Write to a workspace-managed Parquet table (replace / append / merge)' },
      { type: 'saas_connector', label: 'SaaS Connector', level: 'intermediate', description: 'Read from a SaaS application, using a built-in connector or one you generated in the API Explorer' },
    ],
  },
  {
    name: 'Transform',
    items: [
      { type: 'data_wrangler', label: 'Data Wrangler', level: 'beginner', description: 'Ordered list of inline sub-steps (filter / rename / cast / derive / group) with per-step preview' },
      { type: 'transform', label: 'SQL Transform', level: 'beginner' },
      { type: 'filter', label: 'Filter', level: 'intermediate' },
      { type: 'derived_column', label: 'Derived Column', level: 'intermediate' },
      { type: 'sort', label: 'Sort', level: 'intermediate' },
      { type: 'deduplicate', label: 'Deduplicate', level: 'intermediate' },
      { type: 'sample', label: 'Sample', level: 'intermediate' },
      { type: 'schema_mapper', label: 'Schema Mapper', level: 'advanced' },
      { type: 'data_quality', label: 'Data Quality', level: 'advanced' },
      { type: 'flatten_explode', label: 'Flatten', level: 'advanced' },
      // 2026-06-15 (control-flow alignment): "Split Out" tile removed — it was a duplicate
      // preset of Flatten (mode=explode); use Flatten in explode mode.
      // "Keep Latest" (upsert) likewise removed — Deduplicate (keep last)
      // covers it. Both backend types stay for back-compat (HIDDEN_TYPES).
    ],
  },
  {
    name: 'Combine',
    items: [
      { type: 'join', label: 'Join', level: 'beginner' },
      { type: 'aggregate', label: 'Aggregate', level: 'beginner' },
      { type: 'lookup', label: 'Lookup Join', level: 'intermediate' },
      { type: 'union', label: 'Union', level: 'intermediate' },
      { type: 'pivot', label: 'Pivot', level: 'advanced' },
      { type: 'unpivot', label: 'Unpivot', level: 'advanced' },
      { type: 'window', label: 'Window', level: 'advanced' },
    ],
  },
  {
    name: 'Control Flow',
    // 2026-06-15 (control-flow alignment): standard control-flow names + structure.
    //  • Switch = conditional_split (the real multi-output brancher).
    //  • The old single-case "switch_case" filter is hidden (HIDDEN_TYPES).
    //  • ForEach = foreach_pipeline (per-row sub-pipeline); the batch chunker
    //    foreach_loop is relabeled "Batch Rows" and demoted to advanced.
    //  • Lookup = lookup_activity (Lookup activity); the enrichment join
    //    is "Lookup Join" under Combine.
    items: [
      { type: 'if_condition', label: 'If Condition', level: 'intermediate' },
      { type: 'conditional_split', label: 'Switch', level: 'intermediate' },
      { type: 'foreach_pipeline', label: 'ForEach', level: 'intermediate' },
      { type: 'execute_pipeline', label: 'Execute Pipeline', level: 'intermediate' },
      { type: 'lookup_activity', label: 'Lookup', level: 'intermediate' },
      { type: 'set_variable', label: 'Set Variable', level: 'intermediate' },
      { type: 'wait_delay', label: 'Wait', level: 'intermediate' },
      { type: 'fail', label: 'Fail', level: 'intermediate' },
      { type: 'retry_handler', label: 'Retry', level: 'advanced' },
      { type: 'foreach_loop', label: 'Batch Rows', level: 'advanced' },
    ],
  },
  {
    name: 'Action',
    items: [
      { type: 'http_request', label: 'HTTP Request', level: 'intermediate' },
      { type: 'send_email', label: 'Send Email', level: 'intermediate' },
      { type: 'slack_notify', label: 'Slack / Teams', level: 'intermediate' },
      // 2026-05-22: `webhook_trigger` removed from the palette. Per the
      // 2026-05-22 node audit it has a StepType enum value but no backend
      // registration, so every user who added one ended up with a node
      // that couldn't execute. Inbound-webhook receiver infrastructure
      // (URL routing, signature verification, replay protection) isn't
      // ready for v1.0; promote `api_source` for pull-style integration.
      // Legacy workflows that still carry this type are remapped at load
      // time via revoluzen_flow/ir/migrations.py:migrate_legacy_node_types.
      // 2026-06-15: Code / Script gated to Plus (runs user Python in-process,
      // not a sandbox) — hidden from OSS via HIDDEN_TYPES.
      { type: 'get_metadata', label: 'Get Metadata', level: 'advanced' },
    ],
  },
  {
    name: 'AI / Semantic',
    items: [
      { type: 'embedder', label: 'Embedder', level: 'intermediate' },
      { type: 'llm_guardrail', label: 'LLM Guardrail', level: 'advanced' },
      { type: 'semantic_router', label: 'Semantic Router', level: 'advanced' },
    ],
  },
];

/**
 * Node types an AI ghost suggestion is allowed to propose — the curated
 * palette, and nothing else.
 *
 * DERIVED, not hand-written. This was a second literal list in
 * hiddenNodeTypes.ts carrying a "keep in sync" comment, and by 2026-10-28
 * it had drifted in both directions: 11 entries the backend did not
 * register at all (so the AI could suggest a node the user then could not
 * add — the exact failure the whitelist exists to prevent) and 10 real
 * palette nodes missing (so valid suggestions were silently dropped).
 * Counting that list is also what produced the "40 node types" figure that
 * had spread into eight documents.
 *
 * A type that is in MODULES but also in HIDDEN_TYPES is excluded, so
 * hiding a node is enough to stop it being suggested.
 *
 * NOTE: this covers the curated palette only. The palette additionally
 * renders backend types that HIDDEN_TYPES does not hide (see
 * makeReconciledModulesHook); callers that want to match the rendered
 * palette exactly should also accept those — Canvas does.
 */
export const VALID_GHOST_TYPES: ReadonlySet<string> = new Set(
  MODULES.flatMap((c) => c.items.map((i) => i.type))
    .filter((t) => !HIDDEN_TYPES.has(t)),
);

/** Re-groups a list of engine-categorized modules into user-intent groups. */
export function regroupByIntent(modules: ModuleCategory[]): ModuleCategory[] {
  const order: UserIntent[] = ['Import', 'Prepare', 'Analyze', 'Automate', 'Publish'];
  const buckets = new Map<UserIntent, ModuleItem[]>();
  for (const intent of order) buckets.set(intent, []);
  for (const cat of modules) {
    for (const item of cat.items) {
      const intent = INTENT_FOR_STEP_TYPE[item.type] || 'Automate';
      buckets.get(intent)!.push(item);
    }
  }
  return order
    .map((name) => ({ name, items: buckets.get(name) || [] }))
    .filter((c) => c.items.length > 0);
}

/** Backend node registry — fetched once, cached on window for the app lifetime. */
export function useBackendNodeTypes(): { type: string; label: string; category: string; description?: string }[] | null {
  const [types, setTypes] = useState<{ type: string; label: string; category: string; description?: string }[] | null>(
    () => (window as any).__revoluzen_flow_node_types ?? null
  );
  useEffect(() => {
    if (types) return;
    fetch('/api/node-types')
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (Array.isArray(data)) {
          (window as any).__revoluzen_flow_node_types = data;
          setTypes(data);
        }
      })
      .catch(() => {});
  }, [types]);
  return types;
}

// `icon` values are IconName strings from `shared/Icon.tsx`. Consumers
// render them as line-art SVGs so palette categories render identically
// across OS / browser fonts (the previous emoji values degraded to
// monochrome boxes on systems without an emoji font).
import type { IconName } from './shared/Icon';

export const CATEGORY_ICONS: Record<string, { icon: IconName; color: string; }> = {
  'Data Movement': { icon: 'package', color: '#3b82f6' },
  Transform: { icon: 'zap', color: '#10b981' },
  Combine: { icon: 'link', color: '#f97316' },
  'Control Flow': { icon: 'shuffle', color: '#eab308' },
  Action: { icon: 'globe', color: '#0ea5e9' },
  'AI / Semantic': { icon: 'activity', color: '#8b5cf6' },
  // Legacy keys — kept for reconciliation fallback
  Sources: { icon: 'download', color: '#3b82f6' },
  Outputs: { icon: 'upload', color: '#6366f1' },
  Flow: { icon: 'shuffle', color: '#eab308' },
  SaaS: { icon: 'globe', color: '#a855f7' },
};

const CATEGORY_ALIAS: Record<string, string> = {
  source: 'Data Movement',
  sources: 'Data Movement',
  output: 'Data Movement',
  outputs: 'Data Movement',
  transform: 'Transform',
  combine: 'Combine',
  flow: 'Control Flow',
  flow_control: 'Control Flow',
  action: 'Action',
  saas: 'Action',
  ai: 'AI / Semantic',
  semantic: 'AI / Semantic',
  general: 'Transform',
};

const aliasFor = (raw: string) => {
  const key = (raw || '').toLowerCase();
  return CATEGORY_ALIAS[key] || raw.replace(/^./, (c) => c.toUpperCase());
};

/** Reconciles the hardcoded MODULES palette against the backend node
 *  registry. Lifted out of ModulesPanel so ActivitiesRibbon (and any
 *  future picker surface) reuses the exact same source-of-truth.
 *  MODULES is passed in to avoid a circular import — ModulesPanel.tsx
 *  owns the canonical MODULES array and re-exports useReconciledModules
 *  bound to its own MODULES via a tiny wrapper. */
export function makeReconciledModulesHook(modules: ModuleCategory[]) {
  return function useReconciledModules(): ModuleCategory[] {
    const backendTypes = useBackendNodeTypes();
    return useMemo<ModuleCategory[]>(() => {
      if (!backendTypes) return modules;

      const descByType = new Map(
        backendTypes.map((t) => [t.type, t.description || ''])
      );

      const filtered: ModuleCategory[] = modules.map((cat) => ({
        ...cat,
        items: cat.items
          .filter((item) => !HIDDEN_TYPES.has(item.type))
          .map((item): ModuleItem => ({
            ...item,
            description: descByType.get(item.type) || item.description,
          })),
      })).filter((cat) => cat.items.length > 0);

      const knownTypes = new Set(modules.flatMap((c) => c.items.map((i) => i.type)));
      const extras = backendTypes.filter(
        (t) => !knownTypes.has(t.type) && !HIDDEN_TYPES.has(t.type)
      );
      if (extras.length > 0) {
        const byCat: Record<string, ModuleItem[]> = {};
        for (const t of extras) {
          const cat = aliasFor(t.category || 'More');
          (byCat[cat] ||= []).push({ type: t.type, label: t.label || t.type, description: t.description });
        }
        for (const [name, items] of Object.entries(byCat)) {
          const existing = filtered.find((c) => c.name === name);
          if (existing) existing.items.push(...items);
          else filtered.push({ name, items });
        }
      }
      return filtered;
    }, [backendTypes]);
  };
}
