/**
 * ActivityPage — Insights → Activity: what the AI did.
 *
 * One row per AI request from `/api/v1/activity?scope=ai` — the Copilot's,
 * or another feature's such as drafting a pipeline in the Editor: what was
 * asked, where it came from, the model that answered, tokens, the estimated
 * cost and how it ended. The four cards total the whole window (the
 * endpoint's `summary`), not only the rows in the table. Click a row to see
 * its steps.
 *
 * There is no "who asked": this edition has one user. Pipeline runs are
 * not repeated here either: their history is on Workflows → Executions.
 */

import { Fragment, useEffect, useMemo, useState } from 'react';
import { useDarkMode } from '../../hooks/useDarkMode';
import { usePageContext } from '../../hooks/usePageContext';
import HeroCard from '../shared/HeroCard';
import { api } from '../../api/client';
import { navigateTo } from '../../router';
import { DEFAULT_RATES, type CostRates } from '../../lib/cost';
import {
  AI_ACTIVITY_URL,
  AI_RESULTS,
  AI_RESULT_LABELS,
  WINDOW_DAYS,
  aiActivityCsv,
  aiResult,
  failureText,
  fmtUsd,
  outcomeLabel,
  pageLabel,
  questionOf,
  runCostUsd,
  runDetails,
  sourceSplit,
  summaryFromEvents,
  tokensOf,
  usageTotals,
  whereOf,
  type AiActivityResponse,
  type AiResult,
} from '../../lib/aiActivity';

interface TraceStep {
  step_id: string;
  tool_name: string;
  tool_tier: string;
  outcome: string;
  decision_reason: string;
  latency_ms: number;
  redactions_applied?: Record<string, number>;
  policy_rules_fired?: string[];
}

interface TraceDetail {
  run_id: string;
  user_intent?: string;
  outcome: string;
  iterations: number;
  total_tokens_in: number;
  total_tokens_out: number;
  elapsed_ms: number;
  steps: TraceStep[];
  final_text?: string;
}

const RESULT_DOT: Record<AiResult, string> = {
  answered: 'bg-emerald-500',
  failed: 'bg-red-500',
  blocked: 'bg-amber-500',
};

function fmtTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  } catch {
    return iso;
  }
}

function fmtRelative(iso: string): string {
  const t = new Date(iso).getTime();
  if (!t) return '';
  const sec = Math.max(0, (Date.now() - t) / 1000);
  if (sec < 60) return `${Math.round(sec)}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h ago`;
  return `${Math.round(sec / 86400)}d ago`;
}

function fmtSeconds(ms?: number | null): string | null {
  if (ms == null || !Number.isFinite(Number(ms))) return null;
  const s = Number(ms) / 1000;
  return s < 10 ? `${s.toFixed(1)} s` : `${Math.round(s)} s`;
}

export default function ActivityPage({ embedded = false }: { embedded?: boolean } = {}) {
  const dark = useDarkMode();
  const [data, setData] = useState<AiActivityResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeResults, setActiveResults] = useState<Set<AiResult>>(new Set(AI_RESULTS));
  const [searchTerm, setSearchTerm] = useState('');
  // Open rows, keyed by run id so a filter change does not open another row.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [traceDetails, setTraceDetails] = useState<Record<string, TraceDetail | 'loading' | 'error'>>({});
  const [rates, setRates] = useState<CostRates>(DEFAULT_RATES);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    try {
      const json = await api.get<AiActivityResponse>(AI_ACTIVITY_URL);
      setData(json);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load AI activity');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  // AI cost-rate table. Workspace-scoped so admin overrides apply, and
  // re-fetched when Settings → AI Pricing saves, so the cost updates
  // without a reload.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await api.get<{ rates: CostRates }>('/api/v1/ai/cost-rates');
        if (!cancelled && r?.rates) setRates(r.rates);
      } catch {
        /* fall back to DEFAULT_RATES already in state */
      }
    };
    load();
    const onChange = () => { load(); };
    window.addEventListener('revoluzen-flow-settings-changed', onChange);
    return () => {
      cancelled = true;
      window.removeEventListener('revoluzen-flow-settings-changed', onChange);
    };
  }, []);

  // Only AI requests are asked for; the kind check keeps the page honest if
  // an older backend ignores `scope` and sends the full timeline.
  const requests = useMemo(
    () => (data?.events || []).filter((e) => e.kind === 'agent'),
    [data],
  );

  const resultCounts = useMemo(() => {
    const counts: Record<AiResult, number> = { answered: 0, failed: 0, blocked: 0 };
    for (const ev of requests) counts[aiResult(runDetails(ev).outcome)] += 1;
    return counts;
  }, [requests]);

  const filtered = useMemo(() => {
    const q = searchTerm.trim().toLowerCase();
    return requests.filter((ev) => {
      const d = runDetails(ev);
      if (!activeResults.has(aiResult(d.outcome))) return false;
      if (!q) return true;
      const hay = `${questionOf(ev)} ${whereOf(ev)} ${d.model || ''} ${d.provider || ''}`.toLowerCase();
      return hay.includes(q);
    });
  }, [requests, activeResults, searchTerm]);

  const summary = useMemo(() => data?.summary ?? summaryFromEvents(requests), [data, requests]);
  const totals = useMemo(() => usageTotals(summary, rates), [summary, rates]);
  const split = sourceSplit(summary);
  const totalInWindow = data?.summary?.requests ?? requests.length;

  // Tell the Copilot what this tab shows: the AI requests on screen, so a
  // question like "which requests failed?" or "what did the AI cost?" can
  // be answered from the page.
  usePageContext({
    page: 'ai.activity',
    visible_ids: filtered.map((ev) => ev.subject).filter(Boolean),
    filters: {
      tab: 'activity',
      results: AI_RESULTS.filter((r) => activeResults.has(r)),
      search: searchTerm.trim() || null,
    },
    visible_items: filtered.slice(0, 50).map((ev) => {
      const d = runDetails(ev);
      return {
        id: ev.subject,
        name: questionOf(ev),
        kind: 'ai_request',
        status: aiResult(d.outcome),
        // The Copilot's prompt shows the first four of these.
        meta: {
          where: whereOf(ev),
          tokens: tokensOf(ev).total,
          est_cost_usd: Number(runCostUsd(ev, rates).toFixed(6)),
          model: d.model || d.provider || null,
          when: ev.timestamp,
        },
      };
    }),
  });

  function exportCsv() {
    const blob = new Blob([aiActivityCsv(filtered, rates)], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    const a = document.createElement('a');
    a.href = url;
    a.download = `revoluzen-flow-ai-activity-${stamp}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  // Fetch the steps of a request when its row is opened.
  async function fetchTraceDetail(runId: string) {
    setTraceDetails((cur) => ({ ...cur, [runId]: 'loading' }));
    try {
      const trace = await api.get<TraceDetail>(`/api/ai/agent/trace/${encodeURIComponent(runId)}`);
      setTraceDetails((cur) => ({ ...cur, [runId]: trace }));
    } catch {
      setTraceDetails((cur) => ({ ...cur, [runId]: 'error' }));
    }
  }

  const toggleResult = (r: AiResult) => {
    setActiveResults((cur) => {
      const n = new Set(cur);
      if (n.has(r)) n.delete(r); else n.add(r);
      // Keep at least one selected.
      return n.size === 0 ? cur : n;
    });
  };

  const muted = 'text-slate-500';
  const dash = <span className={dark ? 'text-slate-600' : 'text-slate-500'}>—</span>;

  // Body content — used both for the standalone page and embedded in the
  // Insights hub (AIPage), which provides the header and tab bar.
  const Body = (
    <>
      <div className={embedded ? '' : 'w-full px-8 pt-4'}>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <HeroCard
            gradient="from-violet-400 to-indigo-500"
            icon={
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 2l2.09 6.26L20 10l-5.91 1.74L12 18l-2.09-6.26L4 10l5.91-1.74z" />
              </svg>
            }
            label="AI requests"
            value={totals.requests.toLocaleString()}
            footer={
              split.other > 0
                ? `${split.copilot.toLocaleString()} Copilot · ${split.other.toLocaleString()} other features`
                : `Last ${WINDOW_DAYS} days`
            }
          />
          <HeroCard
            gradient="from-sky-400 to-blue-500"
            icon={
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="4" y1="9" x2="20" y2="9" /><line x1="4" y1="15" x2="20" y2="15" />
                <line x1="10" y1="3" x2="8" y2="21" /><line x1="16" y1="3" x2="14" y2="21" />
              </svg>
            }
            label="Tokens"
            value={(totals.tokensIn + totals.tokensOut).toLocaleString()}
            footer={`Input ${totals.tokensIn.toLocaleString()} · Output ${totals.tokensOut.toLocaleString()}`}
          />
          <HeroCard
            gradient="from-emerald-400 to-teal-500"
            icon={
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="12" y1="1" x2="12" y2="23" /><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" />
              </svg>
            }
            label="Est. cost"
            value={totals.allLocal ? '$0.00' : fmtUsd(totals.costUsd)}
            footer={totals.allLocal ? 'Local model, no per-token cost' : 'Per-model rates, set in Settings'}
          />
          <HeroCard
            gradient={totals.failed > 0 ? 'from-red-400 to-rose-500' : 'from-slate-400 to-slate-500'}
            icon={
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10" /><line x1="15" y1="9" x2="9" y2="15" /><line x1="9" y1="9" x2="15" y2="15" />
              </svg>
            }
            label="Failed AI calls"
            value={totals.failed.toLocaleString()}
            footer={
              totals.blocked > 0
                ? `Model errors and time-outs · ${totals.blocked} blocked by a limit`
                : 'Model errors and time-outs'
            }
          />
        </div>
      </div>

      <div className={embedded ? 'pt-4 space-y-3' : 'w-full px-8 py-4 space-y-3'}>
        {/* Filter toolbar — same card style as the KPI tiles. */}
        <div
          className={`rounded-xl border shadow-sm px-3 py-2.5 flex items-center gap-2 flex-wrap ${
            dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'
          }`}
        >
          <span className={`text-xs font-bold uppercase tracking-wider ${muted}`}>
            Result:
          </span>
          {AI_RESULTS.map((r) => {
            const active = activeResults.has(r);
            return (
              <button
                key={r}
                type="button"
                aria-pressed={active}
                onClick={() => toggleResult(r)}
                className={`inline-flex items-center gap-1 px-2 py-1 text-xs font-semibold rounded-md ring-1 transition-colors ${
                  active
                    ? (dark ? 'bg-white/[0.06] text-slate-200 ring-white/[0.1]' : 'bg-white text-slate-700 ring-slate-300')
                    : (dark ? 'text-slate-500 ring-white/[0.05]' : 'text-slate-500 ring-slate-200')
                }`}
              >
                <span className={`inline-block w-2 h-2 rounded-full ${RESULT_DOT[r]}`} />
                {AI_RESULT_LABELS[r]}
                <span className="ml-0.5 opacity-70">{resultCounts[r]}</span>
              </button>
            );
          })}
          <div className="ml-auto flex items-center gap-1.5">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={dark ? 'text-slate-500' : 'text-slate-400'}>
              <circle cx="11" cy="11" r="7" />
              <line x1="21" y1="21" x2="16.65" y2="16.65" />
            </svg>
            <input
              type="text"
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              placeholder="Search question, where, model…"
              aria-label="Search AI requests"
              className={`px-2 py-1 text-xs rounded-md ring-1 outline-none focus:ring-2 w-60 ${
                dark
                  ? 'bg-white/[0.04] text-slate-200 ring-white/[0.08] focus:ring-amber-400/60 placeholder:text-slate-500'
                  : 'bg-white text-slate-700 ring-slate-200 focus:ring-amber-400 placeholder:text-slate-500'
              }`}
            />
            {searchTerm && (
              <button
                type="button"
                onClick={() => setSearchTerm('')}
                className={`text-xs px-1.5 py-0.5 rounded ${dark ? 'text-slate-400 hover:text-slate-200' : 'text-slate-500 hover:text-slate-800'}`}
                aria-label="Clear search"
              >
                ✕
              </button>
            )}
          </div>
        </div>

        {error && (
          <div role="alert" className={`rounded-lg border p-3 text-[12px] ${dark ? 'bg-red-500/10 border-red-500/20 text-red-300' : 'bg-red-50 border-red-200 text-red-700'}`}>
            Could not load AI activity: {error}
          </div>
        )}

        {!loading && !error && filtered.length === 0 && (
          <div className={`rounded-lg border p-6 text-center text-sm ${dark ? 'bg-[#111827] border-white/[0.08] text-slate-400' : 'bg-white border-slate-200 text-slate-500'}`}>
            {requests.length === 0
              ? `No AI requests in the last ${WINDOW_DAYS} days. When you ask the Copilot or use another AI feature, such as drafting a pipeline in the Editor, each request shows up here, with the model, tokens and estimated cost.`
              : 'No requests match these filters.'}
          </div>
        )}

        {/* Request table — canonical thead-dev tokens so it matches every
            other table in the app. */}
        <div className="rounded-lg border-2 border-thead-dev-border overflow-hidden shadow-sm bg-white">
          <div className="bg-thead-dev-bg px-4 py-2.5 flex items-center justify-between gap-3 flex-wrap">
            <div className="text-[12px] text-white/85 font-medium">
              {data
                ? `${filtered.length} of ${requests.length} request${requests.length === 1 ? '' : 's'}`
                : 'Loading…'}
              {data && totalInWindow > requests.length && (
                <span className="text-white/60"> (latest {requests.length} of {totalInWindow.toLocaleString()})</span>
              )}
              <span className="text-white/40 mx-1.5">·</span>
              <span className="text-white/60">last {WINDOW_DAYS} days</span>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={exportCsv}
                disabled={filtered.length === 0}
                title={filtered.length === 0 ? 'Nothing to export' : `Export ${filtered.length} request${filtered.length === 1 ? '' : 's'} as CSV`}
                className="px-2.5 py-1 text-xs font-semibold text-white bg-white/10 hover:bg-white/20 rounded ring-1 ring-white/15 transition-colors disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center gap-1.5"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                  <polyline points="7 10 12 15 17 10" />
                  <line x1="12" y1="15" x2="12" y2="3" />
                </svg>
                Export CSV
              </button>
              <button
                type="button"
                onClick={refresh}
                disabled={loading}
                className="px-2.5 py-1 text-xs font-semibold text-white bg-white/10 hover:bg-white/20 rounded ring-1 ring-white/15 transition-colors disabled:opacity-50"
              >
                {loading ? 'Loading…' : 'Refresh'}
              </button>
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-[12px] table-fixed">
              <colgroup>
                <col className="w-[140px]" />
                <col />
                <col className="w-[150px]" />
                <col className="w-[160px]" />
                <col className="w-[90px]" />
                <col className="w-[130px]" />
              </colgroup>
              <thead className="bg-thead-dev-bg text-thead-dev-text border-b-2 border-thead-dev-border">
                <tr className="text-xs font-bold uppercase tracking-wider">
                  <th className="text-left px-3 py-2">When</th>
                  <th className="text-left px-3 py-2">Question</th>
                  <th className="text-left px-3 py-2">Model</th>
                  <th className="text-left px-3 py-2">Tokens</th>
                  <th className="text-left px-3 py-2">Est. cost</th>
                  <th className="text-left px-3 py-2">Result</th>
                </tr>
              </thead>
              <tbody className={dark ? 'divide-y divide-white/[0.04]' : 'divide-y divide-slate-100'}>
                {filtered.map((ev, i) => {
                  const rowKey = ev.subject || `row-${i}`;
                  const isExpanded = expanded.has(rowKey);
                  const d = runDetails(ev);
                  const t = tokensOf(ev);
                  const result = aiResult(d.outcome);
                  const modelLabel = d.model || d.provider;
                  const asked = pageLabel(d.page);
                  const took = fmtSeconds(d.elapsed_ms);
                  return (
                    <Fragment key={rowKey}>
                      <tr
                        className={`cursor-pointer transition-colors ${
                          dark ? 'hover:bg-white/[0.02]' : 'hover:bg-slate-50/60'
                        }`}
                        onClick={() => {
                          setExpanded((s) => {
                            const n = new Set(s);
                            if (n.has(rowKey)) n.delete(rowKey); else n.add(rowKey);
                            return n;
                          });
                          if (ev.subject && !traceDetails[ev.subject]) {
                            fetchTraceDetail(ev.subject);
                          }
                        }}
                      >
                        <td className={`px-3 py-2 text-xs tabular-nums ${muted}`}>
                          <div>{fmtRelative(ev.timestamp)}</div>
                          <div className={`text-xs ${dark ? 'text-slate-600' : 'text-slate-500'}`}>{fmtTime(ev.timestamp)}</div>
                        </td>
                        <td className="px-3 py-2" title={`${questionOf(ev)} (${whereOf(ev)})`}>
                          <div className={`truncate ${dark ? 'text-slate-200' : 'text-slate-800'}`}>{questionOf(ev)}</div>
                          <div className={`truncate text-xs ${muted}`}>{whereOf(ev)}</div>
                        </td>
                        <td className="px-3 py-2">
                          {modelLabel ? (
                            <code
                              title={d.model && d.provider ? `${d.provider} · ${d.model}` : modelLabel}
                              className={`font-mono text-xs truncate block ${dark ? 'text-slate-300' : 'text-slate-700'}`}
                            >
                              {modelLabel}
                            </code>
                          ) : dash}
                        </td>
                        <td className="px-3 py-2">
                          {t.total ? (
                            <div className="leading-tight">
                              <div className={`text-xs font-semibold tabular-nums ${dark ? 'text-slate-200' : 'text-slate-800'}`}>
                                {t.total.toLocaleString()}
                              </div>
                              <div className={`text-xs tabular-nums ${muted}`}>
                                in {t.tokensIn.toLocaleString()} · out {t.tokensOut.toLocaleString()}
                              </div>
                            </div>
                          ) : dash}
                        </td>
                        <td className={`px-3 py-2 text-xs tabular-nums ${dark ? 'text-slate-300' : 'text-slate-700'}`}>
                          {t.total ? fmtUsd(runCostUsd(ev, rates)) : dash}
                        </td>
                        <td className="px-3 py-2">
                          <span className="inline-flex items-center gap-1.5">
                            <span className={`inline-block w-2 h-2 rounded-full ${RESULT_DOT[result]}`} />
                            <span className={`text-xs ${dark ? 'text-slate-300' : 'text-slate-600'}`}>
                              {outcomeLabel(d.outcome)}
                            </span>
                          </span>
                        </td>
                      </tr>
                      {isExpanded && (
                        <tr className={dark ? 'bg-white/[0.02]' : 'bg-slate-50/40'}>
                          <td colSpan={6} className="px-3 py-3">
                            <div className="space-y-2">
                              <div className={`text-xs italic ${dark ? 'text-slate-400' : 'text-slate-600'}`}>
                                "{questionOf(ev)}"
                              </div>
                              <div className={`flex flex-wrap items-center gap-x-3 gap-y-1 text-xs ${muted}`}>
                                {asked && <span>From <b className={dark ? 'text-slate-200' : 'text-slate-700'}>{asked}</b></span>}
                                {took && <span>took <b className={dark ? 'text-slate-200' : 'text-slate-700'}>{took}</b></span>}
                                {typeof d.step_count === 'number' && (
                                  <span><b className={dark ? 'text-slate-200' : 'text-slate-700'}>{d.step_count}</b> step{d.step_count === 1 ? '' : 's'}</span>
                                )}
                              </div>
                              {result !== 'answered' && failureText(d.failure_reason) && (
                                <div className={`text-xs ${dark ? 'text-red-300' : 'text-red-700'}`}>
                                  Why it failed: {failureText(d.failure_reason)}
                                </div>
                              )}
                              {(() => {
                                const td = ev.subject ? traceDetails[ev.subject] : undefined;
                                if (td === 'loading') {
                                  return <div className={`text-xs ${muted}`}>Loading the steps…</div>;
                                }
                                if (td === 'error') {
                                  return <div className={`text-xs ${dark ? 'text-amber-300' : 'text-amber-700'}`}>Could not load the steps for this request.</div>;
                                }
                                if (!td) return null;
                                return (
                                  <>
                                    {td.steps && td.steps.length > 0 && (
                                      <div className={`rounded border ${dark ? 'border-white/[0.05]' : 'border-slate-200'} divide-y ${dark ? 'divide-white/[0.04]' : 'divide-slate-100'}`}>
                                        {td.steps.map((st) => (
                                          <div key={st.step_id} className="px-2 py-1.5 flex items-center gap-2 flex-wrap">
                                            <code className={`font-mono text-xs ${dark ? 'text-indigo-300' : 'text-indigo-700'}`}>{st.tool_name}</code>
                                            <span className={`text-[10px] uppercase font-bold tracking-wider px-1.5 py-0.5 rounded ${st.tool_tier === 'read' ? (dark ? 'bg-slate-700/40 text-slate-300' : 'bg-slate-100 text-slate-600') : (dark ? 'bg-amber-500/20 text-amber-300' : 'bg-amber-100 text-amber-700')}`}>{st.tool_tier}</span>
                                            <span className={`text-xs font-semibold ${st.outcome === 'success' ? (dark ? 'text-emerald-300' : 'text-emerald-700') : (dark ? 'text-red-300' : 'text-red-600')}`}>{st.outcome}</span>
                                            <span className={`text-xs ${muted}`}>{st.latency_ms}ms</span>
                                            {st.policy_rules_fired && st.policy_rules_fired.length > 0 && (
                                              <span className={`text-xs ${dark ? 'text-amber-300' : 'text-amber-700'}`}>
                                                policy: {st.policy_rules_fired.join(', ')}
                                              </span>
                                            )}
                                            <span className={`flex-1 truncate text-xs ${muted}`} title={st.decision_reason}>
                                              {st.decision_reason}
                                            </span>
                                          </div>
                                        ))}
                                      </div>
                                    )}
                                    {td.final_text && (
                                      <div className={`rounded p-2 text-xs ${dark ? 'bg-slate-900 text-slate-300' : 'bg-slate-50 text-slate-700 border border-slate-200'}`}>
                                        {td.final_text.slice(0, 800)}
                                        {td.final_text.length > 800 && '…'}
                                      </div>
                                    )}
                                  </>
                                );
                              })()}
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        <div className={`text-center text-xs pt-2 ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
          Pipeline runs are listed under{' '}
          <button
            type="button"
            onClick={() => navigateTo('executions')}
            className={`font-semibold underline underline-offset-2 ${dark ? 'text-slate-300 hover:text-slate-100' : 'text-slate-700 hover:text-slate-900'}`}
          >
            Workflows → Executions
          </button>
          .
        </div>
      </div>
    </>
  );

  // Embedded in the Insights hub: the parent provides the header and tabs.
  if (embedded) {
    return <div className="space-y-3">{Body}</div>;
  }

  // Standalone page with its own header.
  return (
    <div className={`flex-1 overflow-auto ${dark ? 'bg-[#0b1120]' : 'bg-canvas-bg'}`}>
      <div className={`border-b ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
        <div className="w-full max-w-[1500px] mx-auto px-8 py-5">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div>
              <h1 className={`text-xl font-bold ${dark ? 'text-slate-100' : 'text-slate-800'}`}>AI activity</h1>
              <p className={`text-xs mt-1 max-w-2xl ${dark ? 'text-slate-400' : 'text-slate-600'}`}>
                Every AI request, from the Copilot or another AI feature: which model answered, the tokens it used, the estimated cost, and any that failed.
              </p>
            </div>
            <button
              type="button"
              onClick={refresh}
              disabled={loading}
              className={`px-3 py-1.5 text-xs font-semibold rounded-lg ring-1 ${
                dark
                  ? 'bg-white/[0.06] hover:bg-white/[0.1] text-slate-200 ring-white/[0.08]'
                  : 'bg-white hover:bg-slate-100 text-slate-700 ring-slate-300'
              } ${loading ? 'opacity-50' : ''}`}
            >
              {loading ? 'Loading…' : 'Refresh'}
            </button>
          </div>
        </div>
      </div>
      {Body}
    </div>
  );
}
