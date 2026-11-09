/**
 * Helpers for Insights → Activity, the view of what the AI did.
 *
 * The page reads `/api/v1/activity?scope=ai`: one event per AI request —
 * the Copilot's, or another feature's such as drafting a pipeline in the
 * Editor (what was asked, where, the model, tokens, how it ended) — plus
 * `summary`, the totals for the whole time window. These helpers put a
 * run's recorded outcome into plain words, say where a request came from,
 * price tokens with the workspace's rate table (Settings → AI Pricing) and
 * build the CSV export, so the page and its tests share one definition.
 */

import { computeCostUsd, type CostRates } from './cost';

export interface ActivityEvent {
  kind: 'audit' | 'agent' | 'execution';
  timestamp: string;
  actor: string;
  subject: string;
  summary: string;
  severity: 'info' | 'warning' | 'error';
  details: Record<string, unknown>;
}

/** What the backend records for one AI request (kind "agent"). */
export interface AiRunDetails {
  /** "copilot", or the id of the feature that asked the model. */
  source?: string | null;
  /** The feature's plain name, e.g. "Draft a pipeline". */
  feature?: string | null;
  user_intent?: string | null;
  model?: string | null;
  provider?: string | null;
  tokens_in?: number;
  tokens_out?: number;
  tokens?: number;
  elapsed_ms?: number | null;
  step_count?: number;
  page?: string | null;
  outcome?: string;
  failure_reason?: string | null;
}

export interface AiUsageByModel {
  provider: string | null;
  model: string | null;
  requests: number;
  tokens_in: number;
  tokens_out: number;
}

/** Totals over every request in the window, not only the rows returned. */
export interface AiUsageSummary {
  requests: number;
  tokens_in: number;
  tokens_out: number;
  failed: number;
  blocked: number;
  by_model: AiUsageByModel[];
  /** Requests per source, the Copilot's under "copilot". */
  by_source?: { source: string; requests: number }[];
}

export interface AiActivityResponse {
  events: ActivityEvent[];
  count: number;
  kind_counts: Record<string, number>;
  severity_counts: Record<string, number>;
  filter: Record<string, unknown>;
  summary?: AiUsageSummary;
}

export const AI_ACTIVITY_URL = '/api/v1/activity?scope=ai&limit=200';

/** Days the endpoint looks back when no `since` is given. */
export const WINDOW_DAYS = 7;

export type AiResult = 'answered' | 'failed' | 'blocked';

export const AI_RESULTS: readonly AiResult[] = ['answered', 'failed', 'blocked'];

export const AI_RESULT_LABELS: Record<AiResult, string> = {
  answered: 'Answered',
  failed: 'Failed',
  blocked: 'Blocked',
};

export function runDetails(ev: ActivityEvent): AiRunDetails {
  return (ev.details || {}) as AiRunDetails;
}

/**
 * Group a recorded outcome. A request failed when the model call errored,
 * the run broke, or it ran out of time; it was blocked when a limit (such
 * as a daily token cap) stopped it before the model was asked.
 */
export function aiResult(outcome?: string | null): AiResult {
  if (!outcome || outcome === 'success') return 'answered';
  if (outcome === 'policy_block') return 'blocked';
  return 'failed';
}

/** The recorded outcome in the words the page shows. */
export function outcomeLabel(outcome?: string | null): string {
  switch (outcome || 'success') {
    case 'success':
      return 'Answered';
    case 'llm_failure':
      return 'Model error';
    case 'timeout':
      return 'Took too long';
    case 'tool_failure':
      return 'Stopped by an error';
    case 'policy_block':
      return 'Blocked by a limit';
    default:
      return 'Did not finish';
  }
}

/** The recorded failure reason without the trace's internal `llm_call:` tag. */
export function failureText(reason?: string | null): string {
  return (reason || '').replace(/^llm_call:/i, '').trim();
}

export function questionOf(ev: ActivityEvent): string {
  const d = runDetails(ev);
  return d.user_intent || ev.summary || d.feature || 'AI request';
}

/** The Copilot's own runs, and the model calls made inside them. */
export function isCopilot(ev: ActivityEvent): boolean {
  const source = runDetails(ev).source || 'copilot';
  return source === 'copilot' || source.startsWith('copilot.');
}

/**
 * Where a request came from: "Copilot", or the page and the feature, such
 * as "Editor · Draft a pipeline". The feature is left out when it is
 * already the request's text.
 */
export function whereOf(ev: ActivityEvent): string {
  const d = runDetails(ev);
  const source = d.source || 'copilot';
  if (source === 'copilot') return 'Copilot';
  const feature = d.feature || 'Other AI feature';
  const page = pageLabel(d.page);
  if (!page) return feature;
  return questionOf(ev) === feature ? page : `${page} · ${feature}`;
}

/** How many requests came from the Copilot and how many from other features. */
export function sourceSplit(summary: AiUsageSummary | undefined): { copilot: number; other: number } {
  let copilot = 0;
  let other = 0;
  for (const s of summary?.by_source || []) {
    if (s.source === 'copilot' || s.source.startsWith('copilot.')) copilot += s.requests;
    else other += s.requests;
  }
  return { copilot, other };
}

export function tokensOf(ev: ActivityEvent): { tokensIn: number; tokensOut: number; total: number } {
  const d = runDetails(ev);
  const tokensIn = Number(d.tokens_in || 0);
  const tokensOut = Number(d.tokens_out || 0);
  const total = tokensIn || tokensOut ? tokensIn + tokensOut : Number(d.tokens || 0);
  return { tokensIn, tokensOut, total };
}

export function isLocalProvider(provider?: string | null): boolean {
  return (provider || '').toLowerCase() === 'ollama';
}

export function runCostUsd(ev: ActivityEvent, rates: CostRates): number {
  const d = runDetails(ev);
  const { tokensIn, tokensOut } = tokensOf(ev);
  return computeCostUsd(rates, d.provider, d.model, tokensIn, tokensOut);
}

export function fmtUsd(value: number): string {
  return value === 0 ? '$0.00' : `$${value.toFixed(4)}`;
}

/**
 * Totals worked out from the rows themselves, for a backend that sends no
 * `summary` (one older than the AI-only scope). They cover only the rows
 * returned, not the whole window.
 */
export function summaryFromEvents(events: ActivityEvent[]): AiUsageSummary {
  const byModel = new Map<string, AiUsageByModel>();
  const bySource = new Map<string, number>();
  const s: AiUsageSummary = { requests: 0, tokens_in: 0, tokens_out: 0, failed: 0, blocked: 0, by_model: [] };
  for (const ev of events) {
    const source = runDetails(ev).source || 'copilot';
    bySource.set(source, (bySource.get(source) ?? 0) + 1);
    const d = runDetails(ev);
    const t = tokensOf(ev);
    // Rows from before the input/output split carry one combined count,
    // which is priced as input.
    const tokensIn = t.tokensIn || t.tokensOut ? t.tokensIn : t.total;
    s.requests += 1;
    s.tokens_in += tokensIn;
    s.tokens_out += t.tokensOut;
    const result = aiResult(d.outcome);
    if (result === 'failed') s.failed += 1;
    if (result === 'blocked') s.blocked += 1;
    const key = `${d.provider ?? ''}\u0000${d.model ?? ''}`;
    const m = byModel.get(key) ?? { provider: d.provider ?? null, model: d.model ?? null, requests: 0, tokens_in: 0, tokens_out: 0 };
    m.requests += 1;
    m.tokens_in += tokensIn;
    m.tokens_out += t.tokensOut;
    byModel.set(key, m);
  }
  s.by_model = [...byModel.values()];
  s.by_source = [...bySource.entries()].map(([source, requests]) => ({ source, requests }));
  return s;
}

export interface UsageTotals {
  requests: number;
  tokensIn: number;
  tokensOut: number;
  costUsd: number;
  /** Every request whose provider is known ran on a local model. */
  allLocal: boolean;
  failed: number;
  blocked: number;
}

/** Totals for the cards, priced per provider and model. */
export function usageTotals(summary: AiUsageSummary | undefined, rates: CostRates): UsageTotals {
  const s = summary ?? { requests: 0, tokens_in: 0, tokens_out: 0, failed: 0, blocked: 0, by_model: [] };
  let costUsd = 0;
  let anyLocal = false;
  let anyPriced = false;
  for (const m of s.by_model || []) {
    costUsd += computeCostUsd(rates, m.provider, m.model, m.tokens_in, m.tokens_out);
    // A request with no recorded provider proves nothing either way: it is
    // priced at the fallback rate and does not make the total "local".
    if (isLocalProvider(m.provider)) anyLocal = true;
    else if (m.provider) anyPriced = true;
  }
  return {
    requests: s.requests,
    tokensIn: s.tokens_in,
    tokensOut: s.tokens_out,
    costUsd,
    allLocal: anyLocal && !anyPriced,
    failed: s.failed,
    blocked: s.blocked,
  };
}

const PAGE_LABELS: Record<string, string> = {
  dashboard: 'Dashboard',
  projects: 'Projects',
  pipelines: 'Pipelines',
  editor: 'Editor',
  executions: 'Executions',
  triggers: 'Triggers',
  templates: 'Templates',
  connections: 'Connections',
  credentials: 'Credentials',
  pool: 'Pool',
  storage: 'Storage',
  ai: 'Insights',
  reports: 'Insights',
  settings: 'Settings',
  trust: 'Settings',
  help: 'Help',
  account: 'Account',
  notifications: 'Notifications',
  copilot: 'Copilot',
};

/** The page a request was asked from, as the sidebar names it. */
export function pageLabel(page?: string | null): string | null {
  if (!page) return null;
  const head = page.split('.')[0].toLowerCase();
  return PAGE_LABELS[head] ?? page;
}

export const AI_CSV_HEADERS = [
  'when',
  'question',
  'where',
  'model',
  'provider',
  'tokens_in',
  'tokens_out',
  'tokens_total',
  'est_cost_usd',
  'result',
  'failure_reason',
  'run_id',
] as const;

function csvCell(value: unknown): string {
  let s = value == null ? '' : String(value);
  // A question typed to the AI is free text. Opened in a spreadsheet, a cell
  // starting with = + - @ would run as a formula, so it is kept as text.
  if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** The rows on screen as CSV, one line per AI request. */
export function aiActivityCsv(events: ActivityEvent[], rates: CostRates): string {
  const lines = [AI_CSV_HEADERS.join(',')];
  for (const ev of events) {
    const d = runDetails(ev);
    const t = tokensOf(ev);
    lines.push([
      ev.timestamp,
      questionOf(ev),
      whereOf(ev),
      d.model || '',
      d.provider || '',
      t.tokensIn,
      t.tokensOut,
      t.total,
      runCostUsd(ev, rates).toFixed(6),
      outcomeLabel(d.outcome),
      d.failure_reason || '',
      ev.subject || '',
    ].map(csvCell).join(','));
  }
  return lines.join('\n');
}
