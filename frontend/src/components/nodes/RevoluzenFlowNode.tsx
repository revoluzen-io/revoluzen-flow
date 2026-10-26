import { memo, useState, useRef, useEffect, useLayoutEffect } from 'react';
import { createPortal } from 'react-dom';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { useWorkflowStore } from '../../stores/workflowStore';
import { branchPortsFor } from '../../utils/branchPorts';
import { getEditorPreferences, useEditorPreferences } from '../../hooks/useEditorPreferences';
import ConnectorIcon from '../shared/ConnectorIcons';

/* 2026-10-24 — square-node geometry (n8n model).
   The card is a fixed BOX_SIZE x BOX_SIZE square holding the icon and
   nothing else. The node NAME renders in an absolutely positioned caption
   BELOW the square; row counts and schema deltas render on the edge.

   Keep BOX_SIZE in sync with `.revoluzen-flow-node { width/height }` in
   globals.css — the CSS draws the box, this constant places the handles
   on it, and Canvas.tsx's auto-layout spacing is derived from it.

   Branch nodes are the one exception to "every node the same size": a
   node with N output ports needs N handles down its right edge, so it
   grows vertically in PORT_PITCH steps. n8n does the same. */
const BOX_SIZE = 96;
const PORT_PITCH = 26;
/** Height of the square for a node with `portCount` output ports. */
function boxHeightFor(portCount: number): number {
  if (portCount <= 1) return BOX_SIZE;
  return Math.max(BOX_SIZE, portCount * PORT_PITCH + 24);
}

/* 2026-06-02 Phase 2 — connector types that ConnectorIcons.tsx ships
   a brand icon for. When a source/sink node's params.connector_type
   matches one of these, we render the real brand mark instead of the
   generic gradient glyph. The list mirrors ICON_MAP keys in
   ConnectorIcons.tsx — keep them in sync when adding new connectors. */
const BRAND_ICON_TYPES = new Set([
  // Databases
  'postgresql','mysql','mssql','oracle','sqlite','mariadb','cockroachdb','db2','sap_hana','teradata',
  'mongodb','cassandra','couchbase','dynamodb','cosmosdb','neo4j','firebase',
  // Warehouses + lakes
  'snowflake','bigquery','redshift','databricks','synapse','clickhouse','trino','presto','athena',
  // Search + cache
  'elasticsearch','opensearch','redis',
  // Object stores
  's3','azure_blob','adls_gen2','gcs','minio',
  // SaaS docs / files
  'sharepoint','onedrive','gdrive','dropbox','ftp','gsheet',
  // API protocols
  'rest_api','graphql','odata','microsoft_graph','oracle_api','oracle_fusion','oracle_bip',
  // Messaging
  'kafka','rabbitmq','pulsar','eventhub','kinesis',
  // CRM / ERP / ITSM
  'salesforce','dynamics365','sap','sap_s4hana','sap_successfactors','servicenow','jira','workday',
  'hubspot','zendesk','netsuite',
  // DevTools + SaaS
  'github','shopify','stripe','notion','asana',
  // Notification + ops
  'smtp','sendgrid','slack','twilio','datadog','pagerduty','splunk',
  // Vector DBs
  'pinecone','weaviate','qdrant','chroma','pgvector',
]);
import { uiConfirm } from '../../ui/dialog';
import { toast } from '../Toast';
import { hasSideEffect, sideEffectLabel } from '../../utils/nodeArity';
import { formatNodeDocsTooltip } from '../../utils/nodeDocs';
import { classifyIdempotency, IDEMPOTENCY_TONE_CLASSES } from '../../utils/idempotency';
import { askCopilot } from '../../hooks/useAgentChatStore';

/* ── Category accent bar — a NEUTRAL ramp, not brand colour.
 *
 * 2026-10-25. This used to be the saturated mid-tone of a Tailwind
 * family per category: blue sources, purple sinks, orange joins, amber
 * control flow, emerald transforms. On the square node that put a
 * second colour system on the same tile as the first: gold, red and
 * green already mean RUN STATE there — the running ring, the status
 * badge, the error ring — and two colour languages competing for one
 * glance means neither is read quickly.
 *
 * Category has not lost its colour; it moved to where it was always
 * strongest. The icon tile is still brand-coloured per node type (see
 * NodeIcon's gradients, and the real brand marks for ~70 connectors),
 * and that is a 48px block rather than a 4px strip. The bar is now a
 * neutral foot that orders the pipeline by lightness — darkest at the
 * ends where data enters and leaves, lightest for the transforms in
 * between — so it still groups at zoom-out without shouting.
 *
 * Same instinct as the card-colour standard: neutral surface, colour
 * reserved for the one thing that carries meaning.
 */
function categoryAccent(stepType: string): string {
  // Sources — graphite. Darkest: where data enters.
  if (stepType === 'source' || stepType.endsWith('_source')) return '#334155';
  // Destinations / sinks / outputs — the other end of the pipeline.
  if (stepType === 'destination' || stepType === 'output' || stepType === 'db_sink' || stepType.endsWith('_sink')) return '#475569';
  // Combine (multi-input joins).
  if (stepType === 'join' || stepType === 'lookup' || stepType === 'union' || stepType === 'copy_data') return '#64748b';
  // AI / Semantic.
  if (stepType === 'embedder' || stepType === 'llm_guardrail' || stepType === 'semantic_router') return '#64748b';
  // Control flow.
  if (stepType === 'conditional_split' || stepType === 'validation' || stepType === 'fail' ||
      stepType === 'execute_sql_task' || stepType === 'get_metadata' || stepType === 'delete_data' ||
      stepType === 'file_system' || stepType === 'append_variable' || stepType === 'filter_array' ||
      stepType === 'lookup_activity') return '#94a3b8';
  // Transform / shape / quality — the default, and the largest
  // category, so it gets the quietest stop.
  return '#cbd5e1';
}

/* ── Mini SVG icons matching the ModulesPanel icons ── */
function NodeIcon({ type, color, connectorType }: { type: string; color: string; connectorType?: string }) {
  // 2026-06-02 Phase 2 — if the node points at a real branded
  // connector (Postgres, Salesforce, Slack, etc.), render the brand
  // mark instead of the generic gradient glyph. ConnectorIcon ships
  // its own white card + brand SVG, so we return it directly without
  // wrapping. This gives the "I recognize this immediately"
  // moment for ~70 known connector types; everything else falls
  // through to the gradient glyph branch below.
  if (connectorType && BRAND_ICON_TYPES.has(connectorType)) {
    return <ConnectorIcon type={connectorType} size={44} />;
  }
  // 2026-06-02 — inner SVG bumped 16 → 18 to fill the larger 32px
  // icon container without going flush to the edge.
  // 2026-10-24 — 18 → 26 to match the 48px tile on the square node.
  const s = 26;
  const icons: Record<string, React.ReactNode> = {
    // Generic Source — connector chosen inside config
    source: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <ellipse cx="12" cy="5" rx="9" ry="3" />
        <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
        <path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3" />
      </svg>
    ),
    // Generic Destination — connector chosen inside config
    destination: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
        <polyline points="7 10 12 15 17 10" />
        <line x1="12" y1="15" x2="12" y2="3" />
      </svg>
    ),
    csv_source: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <polyline points="14 2 14 8 20 8" />
        <line x1="8" y1="13" x2="16" y2="13" />
        <line x1="8" y1="17" x2="16" y2="17" />
      </svg>
    ),
    db_source: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <ellipse cx="12" cy="5" rx="9" ry="3" />
        <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" />
        <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
      </svg>
    ),
    api_source: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="10" />
        <line x1="2" y1="12" x2="22" y2="12" />
        <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
      </svg>
    ),
    filter: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
      </svg>
    ),
    transform: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
      </svg>
    ),
    derived_column: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
      </svg>
    ),
    rename: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
      </svg>
    ),
    typecast: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="23 4 23 10 17 10" /><polyline points="1 20 1 14 7 14" />
        <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
      </svg>
    ),
    schema_mapper: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="7" height="7" rx="1" />
        <rect x="14" y="14" width="7" height="7" rx="1" />
        <path d="M10 7h4l3 3v4" />
        <path d="M14 17h-4l-3-3v-4" />
      </svg>
    ),
    data_quality: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
        <polyline points="9 12 11 14 15 10" />
      </svg>
    ),
    upsert: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <ellipse cx="12" cy="5" rx="9" ry="3" />
        <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
        <path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3" />
        <path d="M16 8v6M13 11h6" />
      </svg>
    ),
    embedder: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 7h16M4 12h10M4 17h7" />
        <circle cx="18" cy="15" r="3" />
      </svg>
    ),
    llm_guardrail: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
        <line x1="9" y1="9" x2="15" y2="15" />
        <line x1="15" y1="9" x2="9" y2="15" />
      </svg>
    ),
    semantic_router: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="6" cy="6" r="2" />
        <circle cx="18" cy="6" r="2" />
        <circle cx="18" cy="18" r="2" />
        <circle cx="6" cy="18" r="2" />
        <circle cx="12" cy="12" r="2" />
        <path d="M8 6h8M12 8v8M16 18h-8" />
      </svg>
    ),
    flatten_explode: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <line x1="14" y1="6" x2="20" y2="6" />
        <line x1="14" y1="12" x2="20" y2="12" />
        <line x1="14" y1="18" x2="20" y2="18" />
        <circle cx="9" cy="6" r="1.5" fill="white" />
        <circle cx="9" cy="12" r="1.5" fill="white" />
        <circle cx="9" cy="18" r="1.5" fill="white" />
        <path d="M4 6v12" />
      </svg>
    ),
    aggregate: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <line x1="4" y1="20" x2="4" y2="10" />
        <line x1="10" y1="20" x2="10" y2="4" />
        <line x1="16" y1="20" x2="16" y2="14" />
        <line x1="22" y1="20" x2="22" y2="8" />
      </svg>
    ),
    pivot: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="18" height="18" rx="2" />
        <line x1="3" y1="9" x2="21" y2="9" />
        <line x1="9" y1="9" x2="9" y2="21" />
      </svg>
    ),
    unpivot: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="18" height="18" rx="2" />
        <line x1="3" y1="9" x2="21" y2="9" />
        <line x1="3" y1="15" x2="21" y2="15" />
      </svg>
    ),
    window: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="18" height="18" rx="2" />
        <line x1="3" y1="9" x2="21" y2="9" />
      </svg>
    ),
    sample: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
      </svg>
    ),
    validate: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
        <polyline points="22 4 12 14.01 9 11.01" />
      </svg>
    ),
    conditional_split: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 3v6m0 0l-4 4m4-4l4 4" />
        <path d="M8 13v6m8-6v6" />
      </svg>
    ),
    lookup: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="11" cy="11" r="8" />
        <line x1="21" y1="21" x2="16.65" y2="16.65" />
      </svg>
    ),
    // Lookup activity — magnifier with a filled "fetched value" dot in
    // the lens, distinct from the plain Lookup Join magnifier above.
    lookup_activity: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="10" cy="10" r="6.5" />
        <line x1="20" y1="20" x2="14.5" y2="14.5" />
        <circle cx="10" cy="10" r="1.8" fill="white" stroke="none" />
      </svg>
    ),
    union: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 4v8a8 8 0 0 0 16 0V4" />
        <line x1="2" y1="20" x2="22" y2="20" />
      </svg>
    ),
    sort: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <line x1="12" y1="5" x2="12" y2="19" /><polyline points="19 12 12 19 5 12" /><line x1="4" y1="3" x2="20" y2="3" />
      </svg>
    ),
    deduplicate: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" opacity="0.5" />
        <path d="M10 14l4 4m0-4l-4 4" />
      </svg>
    ),
    join: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="8" cy="12" r="5" /><circle cx="16" cy="12" r="5" />
      </svg>
    ),
    // (lookup / union / aggregate / pivot / unpivot / window / sample /
    // validate / conditional_split definitions live earlier in this same
    // map — pre-existing duplicate block removed May 4 2026 to silence
    // TS1117. The earlier definitions take effect at runtime per JS
    // last-wins semantics, but TS rejects the duplication outright.)
    output: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
        <polyline points="17 21 17 13 7 13 7 21" /><polyline points="7 3 7 8 15 8" />
      </svg>
    ),
    db_sink: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <ellipse cx="12" cy="5" rx="9" ry="3" />
        <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" />
        <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
        <polyline points="15 13 17 15 21 11" strokeWidth="2.5" />
      </svg>
    ),
    // ── Control flow & integration primitives ──
    append_variable: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="6" width="14" height="3" rx="0.5" />
        <rect x="3" y="11" width="11" height="3" rx="0.5" />
        <rect x="3" y="16" width="8" height="3" rx="0.5" />
        <path d="M19 16h3M20.5 14.5v3" />
      </svg>
    ),
    filter_array: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 4h18l-7 9v6l-4 2v-8z" />
      </svg>
    ),
    validation: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="9" />
        <path d="M8 12l3 3 5-6" />
      </svg>
    ),
    fail: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="9" />
        <path d="M9 9l6 6M15 9l-6 6" />
      </svg>
    ),
    file_system: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
      </svg>
    ),
    execute_sql_task: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <ellipse cx="12" cy="6" rx="8" ry="3" />
        <path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6" />
        <path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" />
      </svg>
    ),
    copy_data: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="9" y="9" width="11" height="11" rx="2" />
        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
      </svg>
    ),
    delete_data: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="3 6 5 6 21 6" />
        <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
        <path d="M10 11v6M14 11v6" />
      </svg>
    ),
    get_metadata: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="9" />
        <path d="M12 8h.01M11 12h1v4h1" />
      </svg>
    ),
    // ── Cloud object storage (Azure / GCP) ──
    adls_gen2_source: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 18h13a4 4 0 0 0 0-8 6 6 0 0 0-11.7-1.5A4 4 0 0 0 3 18z" />
      </svg>
    ),
    adls_gen2_sink: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 18h13a4 4 0 0 0 0-8 6 6 0 0 0-11.7-1.5A4 4 0 0 0 3 18z" />
        <path d="M9 11v6M6 14l3 3 3-3" />
      </svg>
    ),
    azure_blob_source: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 18h13a4 4 0 0 0 0-8 6 6 0 0 0-11.7-1.5A4 4 0 0 0 3 18z" />
      </svg>
    ),
    azure_blob_sink: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 18h13a4 4 0 0 0 0-8 6 6 0 0 0-11.7-1.5A4 4 0 0 0 3 18z" />
        <path d="M10 13v3M8 14.5l2 2 2-2" />
      </svg>
    ),
    gcs_source: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 17h14a4 4 0 0 0 0-8 6 6 0 0 0-11.66-1.5A4 4 0 0 0 3 17z" />
      </svg>
    ),
    gcs_sink: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 17h14a4 4 0 0 0 0-8 6 6 0 0 0-11.66-1.5A4 4 0 0 0 3 17z" />
        <path d="M10 11v4M8 13l2 2 2-2" />
      </svg>
    ),
    // ── Universal File (auto-detect format from extension) ──
    file_source: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <polyline points="14 2 14 8 20 8" />
        <line x1="9" y1="13" x2="15" y2="13" />
        <line x1="9" y1="17" x2="15" y2="17" />
      </svg>
    ),
    file_sink: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <polyline points="14 2 14 8 20 8" />
        <polyline points="9 14 12 17 15 14" />
      </svg>
    ),
    // ── SaaS document storage ──
    sharepoint_source: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="9" cy="9" r="5" />
        <circle cx="16" cy="14" r="4" />
        <circle cx="11" cy="18" r="3" />
      </svg>
    ),
    sharepoint_sink: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="9" cy="9" r="5" />
        <circle cx="16" cy="14" r="4" />
        <circle cx="11" cy="18" r="3" />
      </svg>
    ),
    onedrive_source: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 16a4 4 0 0 1 2-7.4A6 6 0 0 1 16 8a4 4 0 0 1 4 8H5a2 2 0 0 1-2-2z" />
      </svg>
    ),
    onedrive_sink: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 16a4 4 0 0 1 2-7.4A6 6 0 0 1 16 8a4 4 0 0 1 4 8H5a2 2 0 0 1-2-2z" />
      </svg>
    ),
    gdrive_source: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M9 3l-6 11 3 5h12l3-5L15 3z" />
        <line x1="9" y1="3" x2="15" y2="14" />
        <line x1="3" y1="14" x2="18" y2="14" />
      </svg>
    ),
    gdrive_sink: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M9 3l-6 11 3 5h12l3-5L15 3z" />
        <line x1="9" y1="3" x2="15" y2="14" />
        <line x1="3" y1="14" x2="18" y2="14" />
      </svg>
    ),
    dropbox_source: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M6 4l6 4-6 4-6-4z" transform="translate(2,2)" />
        <path d="M18 4l6 4-6 4-6-4z" transform="translate(-2,2)" />
        <path d="M6 12l6 4-6 4-6-4z" transform="translate(2,2)" />
        <path d="M18 12l6 4-6 4-6-4z" transform="translate(-2,2)" />
      </svg>
    ),
    dropbox_sink: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M6 4l6 4-6 4-6-4z" transform="translate(2,2)" />
        <path d="M18 4l6 4-6 4-6-4z" transform="translate(-2,2)" />
        <path d="M6 12l6 4-6 4-6-4z" transform="translate(2,2)" />
        <path d="M18 12l6 4-6 4-6-4z" transform="translate(-2,2)" />
      </svg>
    ),
    box_source: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
      </svg>
    ),
    box_sink: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
      </svg>
    ),
    // Data Wrangler — ordered list of inline sub-steps (design-data-wrangler-node.md)
    data_wrangler: (
      <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <line x1="3" y1="6" x2="5" y2="6" />
        <line x1="8" y1="6" x2="21" y2="6" />
        <line x1="3" y1="12" x2="5" y2="12" />
        <line x1="8" y1="12" x2="21" y2="12" />
        <line x1="3" y1="18" x2="5" y2="18" />
        <line x1="8" y1="18" x2="21" y2="18" />
      </svg>
    ),
  };

  const gradients: Record<string, string> = {
    csv_source: 'linear-gradient(135deg, #3b82f6, #2563eb)',
    db_source: 'linear-gradient(135deg, #8b5cf6, #7c3aed)',
    api_source: 'linear-gradient(135deg, #0ea5e9, #0284c7)',
    filter: 'linear-gradient(135deg, #f59e0b, #d97706)',
    transform: 'linear-gradient(135deg, #10b981, #059669)',
    derived_column: 'linear-gradient(135deg, #059669, #047857)',
    rename: 'linear-gradient(135deg, #14b8a6, #0d9488)',
    typecast: 'linear-gradient(135deg, #a855f7, #9333ea)',
    schema_mapper: 'linear-gradient(135deg, #0d9488, #0f766e)',
    data_quality: 'linear-gradient(135deg, #22c55e, #16a34a)',
    upsert: 'linear-gradient(135deg, #6366f1, #4f46e5)',
    embedder: 'linear-gradient(135deg, #8b5cf6, #6d28d9)',
    llm_guardrail: 'linear-gradient(135deg, #ef4444, #dc2626)',
    semantic_router: 'linear-gradient(135deg, #6366f1, #4338ca)',
    flatten_explode: 'linear-gradient(135deg, #f59e0b, #d97706)',
    sort: 'linear-gradient(135deg, #8b5cf6, #7c3aed)',
    deduplicate: 'linear-gradient(135deg, #ec4899, #db2777)',
    join: 'linear-gradient(135deg, #f97316, #ea580c)',
    lookup: 'linear-gradient(135deg, #ea580c, #c2410c)',
    union: 'linear-gradient(135deg, #d946ef, #c026d3)',
    aggregate: 'linear-gradient(135deg, #06b6d4, #0891b2)',
    pivot: 'linear-gradient(135deg, #0891b2, #0e7490)',
    unpivot: 'linear-gradient(135deg, #0e7490, #155e75)',
    window: 'linear-gradient(135deg, #7c3aed, #6d28d9)',
    sample: 'linear-gradient(135deg, #84cc16, #65a30d)',
    validate: 'linear-gradient(135deg, #22c55e, #16a34a)',
    conditional_split: 'linear-gradient(135deg, #eab308, #ca8a04)',
    lookup_activity: 'linear-gradient(135deg, #f59e0b, #d97706)',
    output: 'linear-gradient(135deg, #6366f1, #4f46e5)',
    db_sink: 'linear-gradient(135deg, #4f46e5, #4338ca)',
    copy_data: 'linear-gradient(135deg, #6366f1, #4338ca)',
    delete_data: 'linear-gradient(135deg, #ef4444, #dc2626)',
    get_metadata: 'linear-gradient(135deg, #06b6d4, #0891b2)',
    adls_gen2_source: 'linear-gradient(135deg, #0078d4, #005a9e)',
    adls_gen2_sink: 'linear-gradient(135deg, #005a9e, #003e6b)',
    azure_blob_source: 'linear-gradient(135deg, #00bcf2, #0078d4)',
    azure_blob_sink: 'linear-gradient(135deg, #0078d4, #00557a)',
    gcs_source: 'linear-gradient(135deg, #4285f4, #34a853)',
    gcs_sink: 'linear-gradient(135deg, #34a853, #0f9d58)',
    file_source: 'linear-gradient(135deg, #6366f1, #4338ca)',
    file_sink: 'linear-gradient(135deg, #6366f1, #4338ca)',
    sharepoint_source: 'linear-gradient(135deg, #036ac4, #024a8f)',
    sharepoint_sink: 'linear-gradient(135deg, #036ac4, #024a8f)',
    onedrive_source: 'linear-gradient(135deg, #0364b8, #0078d4)',
    onedrive_sink: 'linear-gradient(135deg, #0364b8, #0078d4)',
    gdrive_source: 'linear-gradient(135deg, #1fa463, #0f9d58)',
    gdrive_sink: 'linear-gradient(135deg, #1fa463, #0f9d58)',
    dropbox_source: 'linear-gradient(135deg, #0061ff, #0050d3)',
    dropbox_sink: 'linear-gradient(135deg, #0061ff, #0050d3)',
    box_source: 'linear-gradient(135deg, #0061d5, #003a7a)',
    box_sink: 'linear-gradient(135deg, #0061d5, #003a7a)',
    data_wrangler: 'linear-gradient(135deg, #10b981, #047857)',
  };

  return (
    <div
      // 2026-06-02 — bumped from w-7 h-7 (28px) to w-8 h-8 (32px) so
      // the type signal survives zoom-out. Icon-first clarity: the icon
      // is the primary recognition cue, not the text.
      // 2026-10-24 — 32px -> 48px. On the square node the icon IS the
      // node: the name moved to a caption below, so the tile has to
      // carry recognition on its own at zoom-out.
      className="w-12 h-12 rounded-xl flex items-center justify-center shadow-sm shrink-0"
      style={{ background: gradients[type] || color }}
    >
      {icons[type] || (
        // Fallback: generic node glyph instead of "?". Used when a node type
        // ships without a registered canvas icon — the node still looks
        // intentional, branded, and not broken.
        <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="3" />
          <line x1="9" y1="9" x2="15" y2="9" />
          <line x1="9" y1="13" x2="15" y2="13" />
          <line x1="9" y1="17" x2="13" y2="17" />
        </svg>
      )}
    </div>
  );
}

/* ── Param preview text ── */
function paramPreview(stepType: string, params: any): string {
  switch (stepType) {
    case 'csv_source': return params?.file_path || 'No file selected';
    case 'db_source': return params?.query?.slice(0, 50) || 'No query';
    case 'api_source': return params?.url?.slice(0, 50) || 'No URL';
    case 'filter': return params?.condition?.slice(0, 50) || 'No condition';
    case 'transform': return params?.expression?.slice(0, 50) || 'No expression';
    case 'deduplicate': return `Key: ${(params?.key || []).join(', ') || 'none'}`;
    case 'aggregate': return `Group: ${(params?.group_by || []).join(', ') || 'none'}`;
    case 'join': return `${params?.join_type || 'INNER'} JOIN on ${params?.join_key || '...'}`;
    case 'lookup': return `Lookup: ${params?.lookup_key || '...'}`;
    case 'lookup_activity': return `→ $vars.${params?.output_var || 'result'}`;
    case 'union': return `Mode: ${params?.union_type || 'all'}`;
    case 'sort': return `By: ${(params?.columns || params?.sort_by || []).join(', ') || 'none'}`;
    case 'rename': return `${Object.keys(params?.mappings || {}).length} columns`;
    case 'typecast': return `${Object.keys(params?.casts || {}).length} columns`;
    case 'pivot': return `Pivot: ${params?.pivot_column || '...'}`;
    case 'unpivot': return `Unpivot: ${(params?.value_columns || []).length} columns`;
    case 'window': return `${params?.function || 'ROW_NUMBER'} OVER ...`;
    case 'sample': return `${params?.count || params?.fraction || '...'} rows`;
    case 'validate': return `${(params?.rules || []).length} rules`;
    case 'derived_column': return `${(params?.columns || []).length} columns`;
    case 'conditional_split': return `${(params?.branches || []).length} branches`;
    case 'data_wrangler': {
      const steps = (params?.steps || []) as Array<{ enabled?: boolean }>;
      const enabled = steps.filter((s) => s.enabled !== false).length;
      return `${enabled} of ${steps.length} step${steps.length === 1 ? '' : 's'}`;
    }
    case 'output': return params?.format?.toUpperCase() || 'PARQUET';
    case 'db_sink': return params?.table_name || 'No table';
    default: return '';
  }
}

function RevoluzenFlowNode({ id, data, selected }: NodeProps) {
  const { label, stepType, color, status, params, category } = data as any;
  const deleteNode = useWorkflowStore((s) => s.deleteNode);
  const runStep = useWorkflowStore((s) => s.runStep);
  const resumeFromStep = useWorkflowStore((s) => s.resumeFromStep);
  const ensureWorkflow = useWorkflowStore((s) => s.ensureWorkflow);
  const updateNodeParams = useWorkflowStore((s) => s.updateNodeParams);
  const setSelectedNode = useWorkflowStore((s) => s.setSelectedNode);
  const addNode = useWorkflowStore((s) => s.addNode);
  const stepResults = useWorkflowStore((s) => s.stepResults);
  const updateNodeLabel = useWorkflowStore((s) => s.updateNodeLabel);
  // N1 — pin / unpin selectors. Reading isPinned via the store snapshot
  // so the badge + menu label update when the pin state changes.
  const pinned = useWorkflowStore((s) => Boolean(s.pinnedResults?.[id]));
  const pinNode = useWorkflowStore((s) => s.pinNode);
  const unpinNode = useWorkflowStore((s) => s.unpinNode);
  const nodeErrors = useWorkflowStore((s) => s.validationErrors[id]);
  // C3 — the schema-delta chip used to live here, on the node. It now
  // renders on the EDGE (CustomEdge), which already computed the same
  // +N/~N/−N against the same pair of results — two surfaces describing
  // one transition. The `showSchemaDeltas` preference still gates it,
  // read by CustomEdge. Removed from the node on 2026-10-24 with the
  // square redesign: a 96px square has no room for it, and the edge is
  // where the before/after actually happens.
  //
  // B2: input row count from the single upstream's result, so the success
  // badge can show "in → out" — makes a Filter/Join/Dedup effect visible at
  // a glance. Sources (no input) and multi-input nodes just show output.
  const inputRowCount = (() => {
    const self = stepResults[id];
    if (self?.status !== 'success') return null;
    const { edges } = useWorkflowStore.getState();
    const incoming = edges.filter((e) => e.target === id);
    if (incoming.length !== 1) return null;
    const up = stepResults[incoming[0].source];
    if (up?.status !== 'success' || typeof up.row_count !== 'number') return null;
    return up.row_count;
  })();
  // Re-evaluate "blocked by upstream" whenever the graph changes so the
  // downstream grey-out follows the chain live as the user toggles
  // Deactivate on any ancestor. We subscribe to nodes+edges so a change
  // anywhere upstream re-runs the selector.
  const isBlockedByUpstream = useWorkflowStore((s) => {
    void s.nodes; void s.edges; // subscribe
    return s.isNodeBlockedByUpstream(id);
  });
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number } | null>(null);
  // Resolved menu position after measuring its size against the
  // viewport. Falls back to the requested coords on first paint, then
  // gets clamped/flipped on the next layout effect tick.
  const [menuPos, setMenuPos] = useState<{ left: number; top: number } | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [renamingValue, setRenamingValue] = useState('');
  const renameRef = useRef<HTMLInputElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const isDeactivated = params?._settings?.deactivated;

  // 2026-06-02 — Canvas label density now drives node text density,
  // not just edge labels. Three modes, same semantic as edges:
  //   - clean   icon + title only. Subtitle hidden (the icon already
  //             encodes type for icon-literate users). Best for big
  //             pipelines you want to scan structurally.
  //   - metrics icon + title + subtitle (the type label). Balanced
  //             default for users still learning the icon set.
  //   - verbose adds a third line — the param preview from
  //             paramPreview() — so power users see config at a glance
  //             without opening the side panel.
  // The toggle (CanvasDensityToggle, bottom-right of canvas) flips
  // this live; preference persists via setGeneralPreference.
  const labelDensity = useEditorPreferences().labelDensity;

  // Auto-focus rename input
  useEffect(() => {
    if (renaming && renameRef.current) {
      renameRef.current.focus();
      renameRef.current.select();
    }
  }, [renaming]);

  // 2026-06-02 — F2 to rename when this node is the selected one.
  // Listed in the context menu kbd hint since launch but never actually
  // bound; wiring it now that double-click no longer renames.
  // Scoped to `selected` so the listener only acts on the focused node
  // (multi-selection F2 stays a no-op for safety — renaming N nodes at
  // once isn't a meaningful action). Guarded against firing when the
  // user is already typing in another field (input/textarea/select/
  // contentEditable).
  useEffect(() => {
    if (!selected) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key !== 'F2') return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
      e.preventDefault();
      setRenamingValue(label);
      setRenaming(true);
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [selected, label]);

  const startRename = () => {
    setRenamingValue(label);
    setRenaming(true);
  };

  const finishRename = () => {
    const trimmed = renamingValue.trim();
    if (trimmed && trimmed !== label) {
      updateNodeLabel(id, trimmed);
    }
    setRenaming(false);
  };

  // N1 — when this node is pinned, the pinned snapshot wins over any
  // fresh stepResult. That's the whole point of pinning: keep the
  // working sample visible even after upstream changes. Backend
  // integration (don't re-execute pinned nodes on the next run) is a
  // follow-up; round 1 just preserves the visible badge on the canvas.
  const pinnedSnapshot = useWorkflowStore((s) => s.pinnedResults?.[id]);
  const result = pinnedSnapshot ?? stepResults[id];

  const statusClass =
    status === 'running' ? 'running' :
    status === 'success' ? 'success' :
    status === 'error' ? 'error' : '';

  // Universal nodes: every node can be wired anywhere. Category is kept only
  // for palette grouping and color. Topology is the user's responsibility.
  const isJoin = stepType === 'join' || stepType === 'lookup' || stepType === 'union';
  void category;

  const preview = paramPreview(stepType, params);

  const handleDelete = async (e: React.MouseEvent) => {
    e.stopPropagation();
    // Honor Settings → General → "Confirm before delete". Read fresh
    // from localStorage so a just-toggled preference takes effect even
    // if the canvas hasn't re-rendered.
    if (getEditorPreferences().confirmDelete) {
      const ok = await uiConfirm({
        title: 'Delete this node?',
        message: `Remove "${label || stepType}" from the canvas. Edges connected to this node will also be removed.`,
        confirmLabel: 'Delete',
        danger: true,
      });
      if (!ok) return;
    }
    deleteNode(id);
  };

  const handleContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    // Capture VIEWPORT coordinates (clientX/Y), not node-relative
    // (offsetX/Y). Right-clicking on the right edge of a node used to
    // open the menu off the canvas because offsetX placed it past the
    // node's bounds. Viewport coords + position:fixed (see render
    // below) lets the menu live in the document overlay layer where
    // we can flip it back into view if it would overflow.
    setContextMenu({ x: e.clientX, y: e.clientY });
  };

  const handleExecuteStep = async () => {
    setContextMenu(null);
    // ensureWorkflow() is update-only: never creates a draft silently
    // (memory rule 2026-05-09). Toast and bail if the canvas is unsaved.
    const wfId = await ensureWorkflow();
    if (!wfId) {
      toast.error(
        'Save the pipeline first',
        'Click Save and give the pipeline a name before running a single step.',
      );
      return;
    }
    await runStep(id);
  };

  const handleResumeFromStep = async () => {
    setContextMenu(null);
    const wfId = await ensureWorkflow();
    if (!wfId) {
      toast.error(
        'Save the pipeline first',
        'Click Save and give the pipeline a name before resuming from a step.',
      );
      return;
    }
    await resumeFromStep(id);
  };

  const handleDuplicate = () => {
    setContextMenu(null);
    // 2026-06-10: offset from the ORIGINAL node's position. The old
    // hard-coded {x:100, y:200} stacked every duplicate at the same
    // canvas spot regardless of where the source node sat.
    const original = useWorkflowStore.getState().nodes.find((n) => n.id === id);
    const pos = original
      ? { x: original.position.x + 48, y: original.position.y + 48 }
      : { x: 100, y: 200 };
    addNode(stepType, pos);
  };

  const handleDeactivate = () => {
    setContextMenu(null);
    updateNodeParams(id, { _settings: { ...(params?._settings || {}), deactivated: !isDeactivated } });
  };

  const handleRenameCtx = () => {
    setContextMenu(null);
    startRename();
  };

  // Position the context menu so it always stays inside the viewport.
  // Default open position is the click point; if the menu would
  // overflow the right edge we flip it left, and if it would overflow
  // the bottom we flip it up. Runs synchronously on layout so the
  // user never sees a one-frame flash of the off-screen position.
  useLayoutEffect(() => {
    if (!contextMenu) { setMenuPos(null); return; }
    // Initial guess — click point. The menu renders at this position,
    // we measure, then refine on the next paint if it overflows.
    setMenuPos({ left: contextMenu.x, top: contextMenu.y });
    const id = window.requestAnimationFrame(() => {
      const el = menuRef.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const pad = 8;
      let left = contextMenu.x;
      let top = contextMenu.y;
      if (left + r.width + pad > vw) left = Math.max(pad, contextMenu.x - r.width);
      if (top + r.height + pad > vh) top = Math.max(pad, contextMenu.y - r.height);
      // Final clamp so a menu larger than the viewport (rare but
      // possible at very small windows) at least sticks to the edge.
      left = Math.min(Math.max(pad, left), Math.max(pad, vw - r.width - pad));
      top = Math.min(Math.max(pad, top), Math.max(pad, vh - r.height - pad));
      setMenuPos({ left, top });
    });
    return () => window.cancelAnimationFrame(id);
  }, [contextMenu]);

  // Close context menu on outside click
  useEffect(() => {
    if (!contextMenu) return;
    const close = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setContextMenu(null);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [contextMenu]);

  // Output ports decide the square's height: one port keeps it at
  // BOX_SIZE, N ports grow it so each handle has room on the right edge.
  const outPorts = branchPortsFor(stepType, params);
  const boxH = boxHeightFor(outPorts.length);

  // The node's STANDARD name — what the node is, derived from its type.
  // It renders inside the square. `label` is the user's own name for this
  // instance and renders below; until they rename, the two are the same
  // string, and showing both printed "Destination" twice.
  const typeLabel = stepType
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c: string) => c.toUpperCase());
  const hasCustomName =
    String(label ?? '').trim().length > 0 &&
    String(label).trim().toLowerCase() !== typeLabel.toLowerCase();

  return (
    <div
      className={`revoluzen-flow-node ${statusClass} ${selected ? 'selected' : ''} ${isDeactivated ? 'deactivated' : isBlockedByUpstream ? 'blocked-upstream' : ''} ${nodeErrors?.length ? 'validation-error' : ''}`}
      style={boxH !== BOX_SIZE ? { height: boxH } : undefined}
      onContextMenu={handleContextMenu}
    >
      {/* Category accent bar — bottom edge, 4px tall, full width.
          Driven by stepType via the categoryAccent() helper above. This
          is the category-color clarity signal: at zoom-out where text is
          illegible, the colored bar still tells the eye whether this is a
          source (blue), transform (emerald), join (orange), control flow
          (amber), AI (violet) or sink (purple).

          2026-10-24 — was a 4px stripe down the LEFT edge, which read as
          a tab marker beside a wide pill. On a square with a centred icon
          a full-width bar under the icon reads as part of the tile.
          Pointer-events off so the bar never intercepts drag/click. */}
      <div
        className="absolute left-0 right-0 bottom-0 h-1 rounded-b-[12px] pointer-events-none"
        style={{ background: categoryAccent(stepType) }}
        aria-hidden="true"
      />

      {/* Input handle — LEFT side (always rendered; universal nodes).
          2026-06-02 — bumped 10px → 14px (!w-2.5 → !w-3.5) so the
          drop target is easier to see and grab. Border bumped to 2px
          to keep the white halo visible at the larger size.
          2026-10-24 — the square is a fixed 96px tall whatever the
          density, so the handle sits at its true vertical centre; it no
          longer has to be pinned to a header row to stop edges
          re-routing when the body grew. */}
      <Handle
        type="target"
        position={Position.Left}
        className="!w-3.5 !h-3.5 !border-2 !border-white !bg-slate-400"
        style={{ top: boxH / 2 }}
      />
      {isJoin && (
        <Handle
          type="target"
          position={Position.Top}
          id="input-2"
          className="!w-3.5 !h-3.5 !border-2 !border-white !bg-slate-400"
        />
      )}

      {/* N1 — Pin badge. Persistent (not hover-gated like delete) so
          the user always sees that this node's output is frozen. Click
          unpins. Top-LEFT corner — distinct from the delete button. */}
      {pinned && (
        <button
          onClick={(e) => { e.stopPropagation(); unpinNode(id); }}
          className="absolute -top-2 -left-2 w-5 h-5 rounded-full bg-amber-100 border border-amber-400 flex items-center justify-center hover:bg-amber-200 shadow-lg z-10 nodrag"
          title="Pinned — click to unpin"
        >
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#b45309" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="12" y1="17" x2="12" y2="22" />
            <path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1V2H8v4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V17z" />
          </svg>
        </button>
      )}

      {/* Delete button — top right, visible on hover */}
      <button
        onClick={handleDelete}
        className="node-delete-btn absolute -top-2 -right-2 w-5 h-5 rounded-full bg-white border border-red-300 flex items-center justify-center hover:bg-red-50 hover:border-red-400 shadow-lg z-10"
        title="Delete node"
      >
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#ef4444" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
          <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>

      {/* The square holds the icon and nothing else. Everything that used
          to sit beside or under it — name, subtitle, param preview,
          schema delta, validation text, idempotency badge, row counts —
          either moved to the caption below, became a corner badge, or
          moved onto the edge. */}
      {/* Side-effect badge — amber ⚠ for nodes that touch the outside
          world (writes, calls, deletes). Tooltip is per-action ("Writes a
          CSV file", "Calls an external API") via `sideEffectLabel` so the
          user sees specific consequences at a glance instead of generic
          "side-effect node" boilerplate.

          2026-10-24 — moved off the icon's corner onto the square's
          bottom-RIGHT. Sitting on the icon it read as a blemish on the
          artwork; the square now carries one badge per corner —
          validation top-left, run status top-right, re-run class
          bottom-left, side effect bottom-right — so none overlaps
          another or the icon. */}
      {hasSideEffect(stepType) && (
        <div
          className="absolute -bottom-1.5 -right-1.5 z-10 w-[18px] h-[18px] rounded-full bg-amber-400 border-2 border-white flex items-center justify-center shadow"
          title={
            (sideEffectLabel(stepType) || 'Has external side effects') +
            ' — preview / retry / resume may have real-world consequences.'
          }
        >
          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="#78350f" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round">
            <line x1="12" y1="9" x2="12" y2="13" />
            <line x1="12" y1="17" x2="12.01" y2="17" />
          </svg>
        </div>
      )}

      {/* Icon + the node's STANDARD name, both inside the square. The
          standard name is what the node IS ("Destination", "Derived
          Column") and never changes, so it belongs on the tile; the
          user's own name for this instance goes in the caption below.
          `break-words` + two lines covers the longest type labels
          ("Managed Table Source") without widening the square. */}
      <div className="flex flex-col items-center justify-center gap-1 px-1.5">
        <NodeIcon type={stepType} color={color} connectorType={params?.connector_type} />
        <div
          // 10px, not 9: `line-clamp-2` clips with `overflow: hidden`, so a
          // line box tighter than ~1.3 cuts the descenders off words like
          // "Aggregate" and "Derived Column". 10px also matches the size
          // this label had as a subtitle, which was picked for legibility.
          className="text-[10px] font-semibold text-slate-600 leading-[1.3] text-center line-clamp-2 break-words cursor-help"
          title={formatNodeDocsTooltip(stepType)}
        >
          {typeLabel}
        </div>
      </div>

      {/* Status badge — top-right corner of the square. Was a 16px dot in
          the header row; on a square there is no header row, and a corner
          badge is both more visible at zoom-out and in the same place on
          every node. */}
      {(status === 'success' || status === 'error' || status === 'skipped' || status === 'running') && (
        <div className="absolute -top-1.5 -right-1.5 z-10">
          {status === 'success' && (
            <div className="w-[18px] h-[18px] rounded-full bg-green-500 border-2 border-white shadow flex items-center justify-center" title="Succeeded">
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="20 6 9 17 4 12" />
              </svg>
            </div>
          )}
          {status === 'error' && (
            <div className="w-[18px] h-[18px] rounded-full bg-red-500 border-2 border-white shadow flex items-center justify-center" title={result?.error || 'Failed'}>
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round">
                <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </div>
          )}
          {status === 'skipped' && (
            <div
              className="w-[18px] h-[18px] rounded-full bg-slate-400 border-2 border-white shadow flex items-center justify-center"
              title="Skipped — upstream had no rows, or this node was elided in sample mode"
            >
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="13 17 18 12 13 7" />
                <polyline points="6 17 11 12 6 7" />
              </svg>
            </div>
          )}
          {status === 'running' && (
            <div className="w-[18px] h-[18px] rounded-full bg-white border-2 border-amber-400 border-t-transparent animate-spin shadow" title="Running" />
          )}
        </div>
      )}

      {/* Validation badge — top-left corner. The inline red error list
          used to grow the card (and was the main reason no two nodes were
          the same height). The full text is in the tooltip here and in
          the config panel; the red ring on the card comes from the
          `validation-error` class. */}
      {nodeErrors?.length > 0 && (
        <div
          className="absolute -top-1.5 -left-1.5 z-10 w-[18px] h-[18px] rounded-full bg-red-500 border-2 border-white shadow flex items-center justify-center"
          title={nodeErrors.join('\n')}
        >
          <span className="text-white text-[11px] font-black leading-none">!</span>
        </div>
      )}

      {/* Idempotency dot — bottom-left of the square, sinks only. Tells
          the user at a glance whether a re-run is safe (green), safe but
          destructive (amber), or risky/external (red). Label is in the
          tooltip; see src/utils/idempotency.ts for the classifier. */}
      {(() => {
        const idem = classifyIdempotency(stepType, params);
        if (!idem) return null;
        const tone = IDEMPOTENCY_TONE_CLASSES[idem.tone];
        return (
          <span
            className={`absolute -bottom-1 -left-1 z-10 w-3.5 h-3.5 rounded-full border-2 border-white shadow ${tone.dot}`}
            title={`${idem.label} — ${idem.detail}`}
          />
        );
      })()}

      {/* ── Caption, BELOW the square ──────────────────────────────────
          Absolutely positioned so React Flow still measures the node as
          the square alone: handles stay on the box edges and fitView is
          not thrown off by a tall name. Canvas auto-layout leaves room
          for it (see V_SPACING). Width is wider than the square so a
          two-word name wraps instead of truncating. */}
      <div className="absolute top-full left-1/2 -translate-x-1/2 mt-1.5 w-[140px] text-center pointer-events-none">
        {renaming ? (
          <input
            ref={renameRef}
            value={renamingValue}
            onChange={(e) => setRenamingValue(e.target.value)}
            onBlur={finishRename}
            onKeyDown={(e) => {
              if (e.key === 'Enter') finishRename();
              if (e.key === 'Escape') setRenaming(false);
            }}
            className="text-[12px] font-bold text-slate-800 w-full text-center bg-blue-50 border border-blue-300 rounded px-1 py-0 focus:outline-none focus:ring-1 focus:ring-blue-400 nodrag pointer-events-auto"
            onClick={(e) => e.stopPropagation()}
          />
        ) : (
            /* 2026-05-28 — `truncate` (single-line ellipsis) cut off
               common labels like "Local CSV: daily_sales" → "Local CSV:
               daily_s…" with no way for the user to see the full name.
               Now: up to TWO lines via `line-clamp-2`; full label still
               available via `title`.
               2026-06-02 — bumped title from text-xs (12px) to
               text-[13px]. The previous 12px was edge-of-legibility
               at React Flow's typical 80-100% zoom; 13px reads
               clearly down to ~70% zoom on standard 1080p displays.
               2026-06-02 (rebind) — double-click on title NO LONGER
               renames. Double-click anywhere on a node now opens it
               (double-click-to-open behavior, handled in Canvas.tsx onNodeDoubleClick).
               Rename moved to F2 (handled below) + right-click menu.
               Removing the stopPropagation lets the React Flow
               double-click handler fire as intended.
               2026-10-24 — this is now the USER'S OWN name only. The
               standard type name moved inside the square, so a node the
               user has not renamed shows nothing here rather than
               repeating what the tile already says. */
            hasCustomName && (
              <div
                className="text-[13px] font-bold text-slate-800 line-clamp-2 leading-tight cursor-default hover:text-blue-600 transition-colors break-words"
                title={`${label}\n\nDouble-click to open · F2 to rename`}
              >
                {label}
              </div>
            )
          )}
          {/* Verbose-only — param preview line. Surfaces the most
              salient config (group-by columns / join key / filter
              expression / etc.) without opening the side panel. Only
              renders when paramPreview() returned a non-empty string. */}
          {labelDensity === 'verbose' && preview && (
            <div
              className="text-[10px] text-slate-600 font-normal mt-0.5 truncate"
              title={preview}
            >
              {preview}
            </div>
          )}
        {/* Run time, under the name — n8n's placement. The row count it
            used to sit beside now renders on the OUTGOING EDGE, where
            one number describes one flow instead of being repeated on
            both endpoints. Duration belongs to the step, so it stays
            here. */}
        {result?.status === 'success' && (
          <div
            className="text-[10px] text-slate-500 font-medium mt-0.5 tabular-nums"
            title={
              inputRowCount != null
                ? `${inputRowCount.toLocaleString()} rows in → ${result.row_count.toLocaleString()} rows out in ${result.duration_ms} ms`
                : `${result.row_count.toLocaleString()} rows in ${result.duration_ms} ms`
            }
          >
            {result.duration_ms} ms
          </div>
        )}
        {result?.status === 'error' && (
          <div
            className="text-[10px] text-red-600 font-semibold mt-0.5 line-clamp-2 leading-tight break-words"
            title={result.error || 'Error'}
          >
            {result.error || 'Error'}
          </div>
        )}
      </div>

      {/* Output handle(s) — RIGHT side. Ordinary nodes render the single
          'output' handle (pinned to the header row so edges stay stable
          across density changes). Branch nodes (conditional_split) render
          one labeled source handle per output port, distributed down the
          right edge — handle id = port name → edge.sourceHandle →
          connection.from_port → executor branch routing (2026-06-11). */}
      {(() => {
        if (outPorts.length <= 1) {
          return (
            <Handle
              type="source"
              position={Position.Right}
              id={outPorts[0]?.id || 'output'}
              className="!w-3.5 !h-3.5 !border-2 !border-white !rounded-full"
              style={{ background: outPorts[0]?.color || color, top: boxH / 2 }}
            />
          );
        }
        // Branch node: one handle per port, evenly down the right edge of
        // the (taller) square. Tops are PIXELS, not percentages — the
        // square's height is known here and a percentage would be read
        // against the measured element, which is the square only.
        return outPorts.flatMap((p, idx) => {
          const top = Math.round(((idx + 1) / (outPorts.length + 1)) * boxH);
          return [
            // Port label sits OUTSIDE the square, in the layout gap, lifted
            // clear of the edge that leaves this handle. There is no room
            // for it inside a 96px square next to a 44px icon.
            <span
              key={`${p.id}-lbl`}
              className="absolute left-full ml-2 text-[10px] font-semibold text-slate-500 pointer-events-none max-w-[80px] truncate"
              style={{ top: top - 16 }}
              title={p.label}
            >{p.label}</span>,
            <Handle
              key={`${p.id}-h`}
              type="source"
              position={Position.Right}
              id={p.id}
              className="!w-3 !h-3 !border-2 !border-white !rounded-full"
              style={{ background: p.color || color, top }}
            />,
          ];
        });
      })()}

      {/* Deactivated overlay — amber pill, centered, impossible to miss */}
      {isDeactivated && (
        <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
          <span className="text-[10px] font-bold text-amber-800 bg-amber-100 px-2.5 py-1 rounded-full uppercase tracking-wider border border-amber-300 shadow-sm flex items-center gap-1">
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="10" />
              <line x1="4.93" y1="4.93" x2="19.07" y2="19.07" />
            </svg>
            Disabled
          </span>
        </div>
      )}

      {/* Blocked-by-upstream indicator — this node will be skipped
          because one of its ancestors is deactivated. The executor
          already handles this (engine/executor.py:382), but the
          canvas needs to show it so the user understands why nothing
          ran here at execution time. */}
      {!isDeactivated && isBlockedByUpstream && (
        <div className="absolute -top-2 left-1/2 -translate-x-1/2 pointer-events-none">
          <span className="text-[10px] font-bold text-slate-500 bg-white px-2 py-0.5 rounded-full uppercase tracking-wider border border-slate-300 shadow-sm flex items-center gap-1 whitespace-nowrap">
            <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="13 17 18 12 13 7" />
              <polyline points="6 17 11 12 6 7" />
            </svg>
            Will Skip
          </span>
        </div>
      )}

      {/* Right-click context menu.
          Rendered into a portal on document.body because ReactFlow
          nodes use CSS `transform`, which breaks `position: fixed` for
          descendants (fixed elements anchor to the transformed parent
          instead of the viewport). The portal escapes the transform
          context so viewport coords + auto-flip work as intended. */}
      {contextMenu && createPortal(
        <div
          ref={menuRef}
          className="fixed bg-white border border-slate-200 rounded-xl shadow-2xl py-1 w-48 z-[1000] nodrag nopan"
          style={{ left: menuPos?.left ?? contextMenu.x, top: menuPos?.top ?? contextMenu.y }}
          onClick={(e) => e.stopPropagation()}
          onMouseDown={(e) => e.stopPropagation()}
          onContextMenu={(e) => e.preventDefault()}
        >
          {/* #10 deeper — when this node failed, surface the three
              recovery actions at the TOP of the context menu so the
              user sees them first. Maps to the V10 punchlist:
              Fix configuration / Retry from here / Ask Copilot. The
              "Retry from here" item below already exists (Rerun from
              Here); we reuse its handler. */}
          {status === 'error' && (
            <>
              <div className="px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider text-red-700 bg-red-50 border-b border-red-100">
                This step failed
              </div>
              <button
                onClick={() => {
                  // 2026-06-03 — select-vs-open separation: setSelectedNode alone no
                  // longer opens the modal (single-click select-only
                  // pattern). "Fix configuration" is an explicit
                  // "open this for editing" intent → dispatch the
                  // event the modal listens for.
                  setContextMenu(null);
                  setSelectedNode(id);
                  try { window.dispatchEvent(new CustomEvent('revoluzen-flow-node-opened', { detail: { id } })); } catch { /* ignore */ }
                }}
                className="w-full flex items-center gap-2.5 px-3 py-2 text-xs text-red-700 hover:bg-red-50 font-semibold"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-red-500"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" /></svg>
                Fix configuration
              </button>
              <button
                onClick={handleResumeFromStep}
                className="w-full flex items-center gap-2.5 px-3 py-2 text-xs text-red-700 hover:bg-red-50 font-semibold"
                title="Re-run starting from this node (uses cached upstream results)"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-red-500"><polyline points="1 4 1 10 7 10" /><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" /></svg>
                Retry from here
              </button>
              <button
                onClick={() => {
                  setContextMenu(null);
                  const err = result?.error || 'Unknown error';
                  const prompt = `The "${label || stepType}" step (${stepType}) failed with: ${err}. What's wrong and how do I fix it?`;
                  askCopilot(prompt);
                }}
                className="w-full flex items-center gap-2.5 px-3 py-2 text-xs text-red-700 hover:bg-red-50 font-semibold"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-red-500"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>
                Ask Copilot
              </button>
              <div className="border-t border-slate-100 my-1" />
            </>
          )}
          <button onClick={handleExecuteStep} className="w-full flex items-center gap-2.5 px-3 py-2 text-xs text-slate-600 hover:bg-slate-50 hover:text-slate-800">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" stroke="none" className="text-blue-500"><polygon points="5 3 19 12 5 21 5 3" /></svg>
            Execute Step
          </button>
          <button onClick={handleResumeFromStep} className="w-full flex items-center gap-2.5 px-3 py-2 text-xs text-slate-600 hover:bg-slate-50 hover:text-slate-800" title="Reuses cached upstream outputs where params are unchanged">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-emerald-500"><polyline points="1 4 1 10 7 10" /><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" /></svg>
            Rerun from Here
            <span className="ml-auto text-[10px] text-emerald-500 font-medium">CACHE</span>
          </button>
          {/* N1 — pin / unpin this node's output. Only meaningful when
              the node already has a result to pin (status === 'success'
              or 'error' with a result body); offering it on a never-run
              node would create a confusing no-op. Pinning freezes the
              preview so upstream re-runs don't replace it. */}
          {(result || pinned) && (
            <button
              onClick={() => {
                setContextMenu(null);
                if (pinned) unpinNode(id);
                else pinNode(id);
              }}
              className="w-full flex items-center gap-2.5 px-3 py-2 text-xs text-slate-600 hover:bg-slate-50 hover:text-slate-800"
              title={pinned
                ? 'Drop the pinned sample — node will refresh on next run.'
                : 'Freeze this node\'s output so it survives upstream edits + re-runs.'}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className={pinned ? 'text-amber-500' : 'text-slate-400'}>
                <line x1="12" y1="17" x2="12" y2="22" />
                <path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1V2H8v4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V17z" />
              </svg>
              {pinned ? 'Unpin output' : 'Pin output'}
            </button>
          )}
          <button onClick={handleRenameCtx} className="w-full flex items-center gap-2.5 px-3 py-2 text-xs text-slate-600 hover:bg-slate-50 hover:text-slate-800">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-slate-400"><path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" /></svg>
            Rename
            <kbd className="ml-auto text-[10px] text-slate-500 bg-slate-100 px-1 py-0.5 rounded">F2</kbd>
          </button>
          <button
            onClick={() => {
              // 2026-06-03 — explicit "Open Settings" intent must
              // dispatch the modal-open event (single-click no longer
              // opens the modal under the select-vs-open rebind).
              setContextMenu(null);
              setSelectedNode(id);
              try { window.dispatchEvent(new CustomEvent('revoluzen-flow-node-opened', { detail: { id } })); } catch { /* ignore */ }
            }}
            className="w-full flex items-center gap-2.5 px-3 py-2 text-xs text-slate-600 hover:bg-slate-50 hover:text-slate-800"
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-slate-400"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>
            Open Settings
            <kbd className="ml-auto text-[10px] text-slate-500 bg-slate-100 px-1 py-0.5 rounded">Space</kbd>
          </button>
          <button onClick={handleDeactivate} className="w-full flex items-center gap-2.5 px-3 py-2 text-xs text-slate-600 hover:bg-slate-50 hover:text-slate-800">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-slate-400"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>
            {isDeactivated ? 'Activate' : 'Deactivate'}
            <kbd className="ml-auto text-[10px] text-slate-500 bg-slate-100 px-1 py-0.5 rounded">D</kbd>
          </button>
          <button onClick={handleDuplicate} className="w-full flex items-center gap-2.5 px-3 py-2 text-xs text-slate-600 hover:bg-slate-50 hover:text-slate-800">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-slate-400"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            Duplicate
            <kbd className="ml-auto text-[10px] text-slate-500 bg-slate-100 px-1 py-0.5 rounded">Ctrl+D</kbd>
          </button>
          <div className="border-t border-slate-100 my-1" />
          <button onClick={async () => {
            setContextMenu(null);
            if (getEditorPreferences().confirmDelete) {
              const ok = await uiConfirm({
                title: 'Delete this node?',
                message: `Remove "${label || stepType}" from the canvas. Edges connected to this node will also be removed.`,
                confirmLabel: 'Delete',
                danger: true,
              });
              if (!ok) return;
            }
            deleteNode(id);
          }} className="w-full flex items-center gap-2.5 px-3 py-2 text-xs text-red-500 hover:bg-red-50 hover:text-red-600">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>
            Delete
            <kbd className="ml-auto text-[10px] text-red-300 bg-red-50 px-1 py-0.5 rounded">Del</kbd>
          </button>
        </div>,
        document.body,
      )}
    </div>
  );
}

export default memo(RevoluzenFlowNode);
