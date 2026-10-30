/**
 * Execution Contract authoring (2026-10-30).
 *
 * The verifier, the pre-commit gate, the receipt chain and the whole
 * Contract panel on the execution page were all built and reachable —
 * over HTTP. Declaring a contract meant hand-writing JSON and PUTting it
 * with curl, so in practice nobody had one, and every surface built on
 * top of contracts sat empty. This is the missing half.
 *
 * Two things this dialog exists to get right, beyond "a form":
 *
 * 1. WHICH GUARANTEES ACTUALLY PROTECT YOU. Five of the ten can be
 *    evaluated against the rows about to be written, so they REFUSE the
 *    write. The rest can only be judged once the run is over, by which
 *    time a replace-mode sink has already truncated the destination.
 *    "We refused to destroy your table" and "we told you we destroyed
 *    your table" are different products, and an author choosing between
 *    them should be told which one they are picking.
 *
 * 2. THE DESCRIPTION IS THE PRODUCT. The receipt renders the author's
 *    own words first and falls back to the raw enum. A contract written
 *    without descriptions produces a receipt that reads "no_empty_output
 *    — output is empty", which is useless to the person who has to act
 *    on it at 3am. So the field is prominent, example-led, and the
 *    dialog shows the receipt line as it will actually read.
 */

import { useEffect, useMemo, useState } from 'react';
import { api } from '../../api/client';
import { toast } from '../Toast';

type FieldName =
  | 'min_rows' | 'max_rows' | 'expected_rows' | 'variance_pct'
  | 'columns' | 'key' | 'max_age_seconds';

interface GuaranteeSpec {
  type: string;
  label: string;
  /** What it means, in the author's terms rather than the enum's. */
  blurb: string;
  /** Checked against the pending write, so a violation BLOCKS it. */
  precommit: boolean;
  fields: FieldName[];
  /** Seeds the description box — a good one, not a restatement. */
  examplePrompt: string;
}

/**
 * Mirrors `contracts/precommit.PRECOMMIT_CHECKABLE` and the ten members
 * of `GuaranteeType`. `no_duplicate_key` is answerable before a write
 * but deliberately excluded there: it needs a full scan, and a gate that
 * silently doubles the cost of every write is a gate people switch off.
 */
const CATALOG: GuaranteeSpec[] = [
  {
    type: 'no_empty_output',
    label: 'Never write an empty result',
    blurb: 'The most common silent disaster: every step succeeds, the output is empty, and a replace-mode sink wipes the destination.',
    precommit: true,
    fields: [],
    examplePrompt: 'a regional rollup with no regions is not a result',
  },
  {
    type: 'row_count_min',
    label: 'At least N rows',
    blurb: 'Catches a partial load that is not empty enough to look broken.',
    precommit: true,
    fields: ['min_rows'],
    examplePrompt: 'we sell in at least 3 regions',
  },
  {
    type: 'row_count_max',
    label: 'At most N rows',
    blurb: 'Catches a runaway join that multiplies rows.',
    precommit: true,
    fields: ['max_rows'],
    examplePrompt: 'one row per customer, and we have under 50,000',
  },
  {
    type: 'row_count_variance',
    label: 'Within ±% of an expected count',
    blurb: 'For a steady daily volume — flags both a collapse and a spike.',
    precommit: true,
    fields: ['expected_rows', 'variance_pct'],
    examplePrompt: 'daily volume never moves more than 10% overnight',
  },
  {
    type: 'schema_contains',
    label: 'Output has these columns',
    blurb: 'Stops a rename upstream from quietly breaking what reads this.',
    precommit: true,
    fields: ['columns'],
    examplePrompt: 'the dashboard reads region and revenue',
  },
  {
    type: 'all_steps_succeeded',
    label: 'Every step succeeded',
    blurb: 'Cannot be judged early — later steps have not run yet.',
    precommit: false,
    fields: [],
    examplePrompt: 'no step is allowed to fail silently',
  },
  {
    type: 'no_duplicate_key',
    label: 'No duplicate keys',
    blurb: 'Checked after the run: answering it early needs a full scan of every write.',
    precommit: false,
    fields: ['key'],
    examplePrompt: 'one row per order id',
  },
  {
    type: 'destination_verified',
    label: 'Destination commit confirmed',
    blurb: 'Needs the commit to have happened, so it can only be checked afterwards.',
    precommit: false,
    fields: [],
    examplePrompt: 'the warehouse acknowledged the write',
  },
  {
    type: 'checkpoint_enabled',
    label: 'Run was resumable',
    blurb: 'A property of the run, not of the data.',
    precommit: false,
    fields: [],
    examplePrompt: 'a long load must be restartable',
  },
  {
    type: 'freshness_max_age',
    label: 'Source no older than',
    blurb: 'An out-of-band fact about the source, supplied by the engine after the run.',
    precommit: false,
    fields: ['max_age_seconds'],
    examplePrompt: 'we never report on yesterday’s extract',
  },
];

const SPEC = new Map(CATALOG.map((g) => [g.type, g]));

interface DraftGuarantee {
  type: string;
  description: string;
  output_step: string;
  min_rows: string;
  max_rows: string;
  expected_rows: string;
  variance_pct: string;
  columns: string;
  key: string;
  max_age_seconds: string;
}

function blank(type: string): DraftGuarantee {
  return {
    type, description: '', output_step: '',
    min_rows: '', max_rows: '', expected_rows: '', variance_pct: '10',
    columns: '', key: '', max_age_seconds: '',
  };
}

/** Draft -> the API's Guarantee, dropping everything the type ignores. */
function toApi(g: DraftGuarantee) {
  const spec = SPEC.get(g.type);
  const out: Record<string, unknown> = { type: g.type };
  if (g.description.trim()) out.description = g.description.trim();
  if (g.output_step) out.output_step = g.output_step;
  for (const f of spec?.fields || []) {
    const raw = g[f];
    if (raw === '' || raw == null) continue;
    if (f === 'columns') {
      const cols = String(raw).split(',').map((c) => c.trim()).filter(Boolean);
      if (cols.length) out.columns = cols;
    } else if (f === 'variance_pct') {
      // Authored as a percentage because that is how people say it;
      // the model stores a fraction (0.05 == ±5%).
      const pct = Number(raw);
      if (!Number.isNaN(pct)) out.variance_pct = pct / 100;
    } else if (f === 'key') {
      out.key = String(raw).trim();
    } else {
      const n = Number(raw);
      if (!Number.isNaN(n)) out[f] = n;
    }
  }
  return out;
}

function fromApi(g: any): DraftGuarantee {
  const d = blank(g.type);
  d.description = g.description || '';
  d.output_step = g.output_step || '';
  if (g.min_rows != null) d.min_rows = String(g.min_rows);
  if (g.max_rows != null) d.max_rows = String(g.max_rows);
  if (g.expected_rows != null) d.expected_rows = String(g.expected_rows);
  if (g.variance_pct != null) d.variance_pct = String(Math.round(g.variance_pct * 100));
  if (g.columns) d.columns = (g.columns as string[]).join(', ');
  if (g.key) d.key = g.key;
  if (g.max_age_seconds != null) d.max_age_seconds = String(g.max_age_seconds);
  return d;
}

/** The problems that make a clause a no-op, named where they happen. */
function problemsWith(g: DraftGuarantee): string[] {
  const out: string[] = [];
  const spec = SPEC.get(g.type);
  if (!spec) return ['unknown guarantee type'];
  for (const f of spec.fields) {
    if (String(g[f] ?? '').trim() === '') {
      out.push(`${f.replace(/_/g, ' ')} is required`);
    }
  }
  if (g.type === 'row_count_variance' && g.variance_pct !== '') {
    const p = Number(g.variance_pct);
    if (Number.isNaN(p) || p < 0) out.push('variance must be a positive percentage');
  }
  return out;
}

export default function ContractDialog({ pipelineId, pipelineName, onClose, onSaved }: {
  pipelineId: string;
  pipelineName: string;
  onClose: () => void;
  onSaved?: () => void;
}) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [existed, setExisted] = useState(false);
  const [name, setName] = useState('');
  const [defaultOutputStep, setDefaultOutputStep] = useState('');
  const [guarantees, setGuarantees] = useState<DraftGuarantee[]>([]);
  const [steps, setSteps] = useState<{ id: string; type: string }[]>([]);
  const [adding, setAdding] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      // The steps come from the workflow, not the contract: a guarantee
      // can be scoped to one step's output, and typing a step id from
      // memory is how you get a clause that silently measures the wrong
      // thing.
      const [contractRes, wfRes] = await Promise.allSettled([
        api.getExecutionContract(pipelineId),
        api.getWorkflow(pipelineId),
      ]);
      if (cancelled) return;

      if (wfRes.status === 'fulfilled') {
        const wf = (wfRes.value as any)?.workflow || wfRes.value;
        setSteps(((wf?.steps || []) as any[]).map((s) => ({
          id: s.id, type: s.type,
        })));
      }
      const c = contractRes.status === 'fulfilled'
        ? (contractRes.value as any)?.contract : null;
      if (c) {
        setExisted(true);
        setName(c.name || '');
        setDefaultOutputStep(c.default_output_step || '');
        setGuarantees((c.guarantees || []).map(fromApi));
      } else {
        setName(`${pipelineName} contract`);
      }
      setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [pipelineId, pipelineName]);

  const gated = guarantees.filter((g) => SPEC.get(g.type)?.precommit).length;
  const undescribed = guarantees.filter((g) => !g.description.trim()).length;
  const broken = useMemo(
    () => guarantees.flatMap((g, i) => problemsWith(g).map((p) => `#${i + 1}: ${p}`)),
    [guarantees],
  );

  const update = (i: number, patch: Partial<DraftGuarantee>) =>
    setGuarantees((prev) => prev.map((g, j) => (j === i ? { ...g, ...patch } : g)));

  const save = async () => {
    if (guarantees.length === 0) {
      // The backend 400s on this too; saying why here is kinder than a
      // round trip that returns "a contract needs at least one guarantee".
      toast.error('Add at least one guarantee — an empty contract would report every run as verified.');
      return;
    }
    if (broken.length) {
      toast.error(`Finish the incomplete guarantees: ${broken[0]}`);
      return;
    }
    setSaving(true);
    try {
      await api.putExecutionContract(pipelineId, {
        id: `${pipelineId}-contract`,
        name: name.trim() || pipelineName,
        default_output_step: defaultOutputStep || null,
        guarantees: guarantees.map(toApi),
      });
      toast.success(
        gated > 0
          ? `Contract saved — ${gated} guarantee${gated === 1 ? '' : 's'} will block a bad write on the next run.`
          : 'Contract saved — it will be verified after each run.',
      );
      onSaved?.();
      onClose();
    } catch (e) {
      toast.error(`Could not save: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    setSaving(true);
    try {
      await api.deleteExecutionContract(pipelineId);
      toast.success('Contract removed — runs will no longer be verified.');
      onSaved?.();
      onClose();
    } catch (e) {
      toast.error(`Could not remove: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSaving(false);
    }
  };

  const field = (g: DraftGuarantee, i: number, f: FieldName) => {
    const common = 'px-2 py-1 text-xs border border-slate-300 rounded-md focus:outline-none focus:ring-2 focus:ring-violet-300';
    if (f === 'columns') {
      return (
        <label key={f} className="flex items-center gap-2">
          <span className="text-xs text-slate-600 w-28 shrink-0">Columns</span>
          <input
            className={`${common} flex-1`}
            placeholder="region, revenue"
            value={g.columns}
            onChange={(e) => update(i, { columns: e.target.value })}
          />
        </label>
      );
    }
    if (f === 'key') {
      return (
        <label key={f} className="flex items-center gap-2">
          <span className="text-xs text-slate-600 w-28 shrink-0">Key column</span>
          <input
            className={`${common} flex-1`}
            placeholder="order_id"
            value={g.key}
            onChange={(e) => update(i, { key: e.target.value })}
          />
        </label>
      );
    }
    const labels: Record<string, string> = {
      min_rows: 'Minimum rows',
      max_rows: 'Maximum rows',
      expected_rows: 'Expected rows',
      variance_pct: 'Tolerance (%)',
      max_age_seconds: 'Max age (seconds)',
    };
    return (
      <label key={f} className="flex items-center gap-2">
        <span className="text-xs text-slate-600 w-28 shrink-0">{labels[f]}</span>
        <input
          type="number"
          min="0"
          className={`${common} w-32`}
          value={g[f]}
          onChange={(e) => update(i, { [f]: e.target.value } as Partial<DraftGuarantee>)}
        />
      </label>
    );
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="w-[720px] max-w-[95vw] max-h-[92vh] bg-white rounded-xl border border-slate-200 shadow-2xl flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-start gap-3 px-6 py-4 border-b border-slate-200 bg-gradient-to-r from-violet-50/60 via-white to-slate-50">
          <div className="w-9 h-9 rounded-lg bg-gradient-to-br from-violet-100 to-violet-50 border border-violet-200/70 flex items-center justify-center shrink-0 shadow-sm">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-violet-600">
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
              <polyline points="9 12 11 14 15 10" />
            </svg>
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-base font-semibold text-slate-900">Execution Contract</div>
            <div className="text-xs text-slate-500 mt-0.5 truncate">
              What must be true about <span className="font-semibold">{pipelineName}</span>, declared before it runs
            </div>
          </div>
          <button
            onClick={onClose}
            className="w-7 h-7 rounded-lg flex items-center justify-center text-slate-400 hover:text-slate-700 hover:bg-slate-100"
            aria-label="Close"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
          </button>
        </div>

        {loading ? (
          <div className="px-6 py-10 text-sm text-slate-500">Loading contract…</div>
        ) : (
          <>
            <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
              {/* What this will actually do. Stated up front, because the
                  difference between blocking a write and reporting on one
                  afterwards is the whole reason to choose one guarantee
                  over another, and it is invisible from the names. */}
              <div className={`rounded-lg border px-3.5 py-2.5 text-xs ${
                gated > 0
                  ? 'bg-emerald-50 border-emerald-200 text-emerald-900'
                  : 'bg-amber-50 border-amber-200 text-amber-900'
              }`}>
                {guarantees.length === 0 ? (
                  <>No guarantees yet. A contract with none would report every run as verified — which is worse than having no contract, because it looks like assurance.</>
                ) : gated > 0 ? (
                  <>
                    <span className="font-bold">{gated} of {guarantees.length}</span> can be checked
                    before the write, so a run that breaks {gated === 1 ? 'it' : 'them'} is
                    <span className="font-bold"> refused and your destination is left untouched</span>.
                    The {guarantees.length - gated === 0 ? 'rest' : `other ${guarantees.length - gated}`} {guarantees.length - gated === 1 ? 'is' : 'are'} verified after the run.
                  </>
                ) : (
                  <>
                    None of these can be checked before the write. The run will be
                    <span className="font-bold"> verified afterwards</span> and a receipt recorded —
                    but a bad write will already have happened. Add one of the shield-marked
                    guarantees below to refuse it instead.
                  </>
                )}
              </div>

              <div className="grid grid-cols-2 gap-3">
                <label className="block">
                  <span className="text-xs font-semibold text-slate-600">Contract name</span>
                  <input
                    className="mt-1 w-full px-2.5 py-1.5 text-sm border border-slate-300 rounded-md focus:outline-none focus:ring-2 focus:ring-violet-300"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                  />
                </label>
                <label className="block">
                  <span className="text-xs font-semibold text-slate-600">Measure which step by default</span>
                  <select
                    className="mt-1 w-full px-2.5 py-1.5 text-sm border border-slate-300 rounded-md bg-white focus:outline-none focus:ring-2 focus:ring-violet-300"
                    value={defaultOutputStep}
                    onChange={(e) => setDefaultOutputStep(e.target.value)}
                  >
                    <option value="">Last step in the pipeline</option>
                    {steps.map((s) => (
                      <option key={s.id} value={s.id}>{s.id} ({s.type})</option>
                    ))}
                  </select>
                </label>
              </div>

              {/* Guarantees */}
              <div>
                <div className="flex items-center justify-between mb-2">
                  <span className="text-[10px] font-bold uppercase tracking-wider text-slate-500">
                    Guarantees
                  </span>
                  {undescribed > 0 && (
                    <span className="text-[11px] text-amber-700">
                      {undescribed} without a description — {undescribed === 1 ? 'it' : 'they'} will read as the raw rule name on the receipt
                    </span>
                  )}
                </div>

                {guarantees.length === 0 && (
                  <div className="rounded-lg border border-dashed border-slate-300 px-4 py-6 text-center text-sm text-slate-500">
                    Nothing promised yet.
                  </div>
                )}

                <div className="space-y-3">
                  {guarantees.map((g, i) => {
                    const spec = SPEC.get(g.type)!;
                    const issues = problemsWith(g);
                    return (
                      <div key={i} className="rounded-lg border border-slate-200 p-3.5">
                        <div className="flex items-start gap-2">
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className="text-sm font-semibold text-slate-800">{spec.label}</span>
                              {spec.precommit ? (
                                <span
                                  className="inline-flex items-center gap-1 text-[10px] font-bold uppercase tracking-wider text-emerald-700 bg-emerald-100 px-1.5 py-0.5 rounded"
                                  title="Checked against the rows about to be written — a violation refuses the write"
                                >
                                  <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /></svg>
                                  Blocks the write
                                </span>
                              ) : (
                                <span
                                  className="text-[10px] font-bold uppercase tracking-wider text-slate-500 bg-slate-100 px-1.5 py-0.5 rounded"
                                  title="Can only be judged once the run has finished"
                                >
                                  Checked after
                                </span>
                              )}
                              <code className="text-[10px] font-mono text-slate-400">{g.type}</code>
                            </div>
                            <div className="text-xs text-slate-500 mt-0.5">{spec.blurb}</div>
                          </div>
                          <button
                            onClick={() => setGuarantees((p) => p.filter((_, j) => j !== i))}
                            className="w-6 h-6 rounded flex items-center justify-center text-slate-400 hover:text-red-600 hover:bg-red-50 shrink-0"
                            title="Remove this guarantee"
                          >
                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="3 6 5 6 21 6" /><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" /></svg>
                          </button>
                        </div>

                        {/* The author's own words. First on the receipt,
                            so first here too. */}
                        <div className="mt-3">
                          <input
                            className="w-full px-2.5 py-1.5 text-sm border border-slate-300 rounded-md focus:outline-none focus:ring-2 focus:ring-violet-300"
                            placeholder={`Say it in your own words — e.g. “${spec.examplePrompt}”`}
                            value={g.description}
                            onChange={(e) => update(i, { description: e.target.value })}
                          />
                          <div className="text-[11px] text-slate-400 mt-1">
                            On the receipt this reads:{' '}
                            <span className={g.description.trim() ? 'text-slate-600 font-medium' : 'font-mono text-amber-700'}>
                              ✗ {g.description.trim() || g.type}
                            </span>
                          </div>
                        </div>

                        {(spec.fields.length > 0 || steps.length > 0) && (
                          <div className="mt-3 flex flex-wrap gap-x-6 gap-y-2">
                            {spec.fields.map((f) => field(g, i, f))}
                            <label className="flex items-center gap-2">
                              <span className="text-xs text-slate-600 w-28 shrink-0">Measure step</span>
                              <select
                                className="px-2 py-1 text-xs border border-slate-300 rounded-md bg-white focus:outline-none focus:ring-2 focus:ring-violet-300"
                                value={g.output_step}
                                onChange={(e) => update(i, { output_step: e.target.value })}
                              >
                                <option value="">Use the default</option>
                                {steps.map((s) => (
                                  <option key={s.id} value={s.id}>{s.id}</option>
                                ))}
                              </select>
                            </label>
                          </div>
                        )}

                        {issues.length > 0 && (
                          <div className="mt-2 text-[11px] font-semibold text-amber-700">
                            {/* A clause missing its number is not a weaker
                                clause — the verifier reports it as failed
                                (fail-closed), so every run would fail. */}
                            Incomplete: {issues.join(', ')} — until then every run fails this clause.
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>

                {/* Add */}
                <div className="mt-3">
                  {adding ? (
                    <div className="rounded-lg border border-violet-200 bg-violet-50/40 p-3">
                      <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500 mb-2">
                        Refuses a bad write
                      </div>
                      <div className="grid grid-cols-2 gap-2">
                        {CATALOG.filter((s) => s.precommit).map((s) => (
                          <button
                            key={s.type}
                            onClick={() => { setGuarantees((p) => [...p, blank(s.type)]); setAdding(false); }}
                            className="text-left px-3 py-2 rounded-md border border-emerald-200 bg-white hover:bg-emerald-50 transition-colors"
                          >
                            <div className="text-xs font-semibold text-slate-800">{s.label}</div>
                            <div className="text-[11px] text-slate-500 mt-0.5 leading-snug">{s.blurb}</div>
                          </button>
                        ))}
                      </div>
                      <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500 mt-3 mb-2">
                        Verified after the run
                      </div>
                      <div className="grid grid-cols-2 gap-2">
                        {CATALOG.filter((s) => !s.precommit).map((s) => (
                          <button
                            key={s.type}
                            onClick={() => { setGuarantees((p) => [...p, blank(s.type)]); setAdding(false); }}
                            className="text-left px-3 py-2 rounded-md border border-slate-200 bg-white hover:bg-slate-50 transition-colors"
                          >
                            <div className="text-xs font-semibold text-slate-800">{s.label}</div>
                            <div className="text-[11px] text-slate-500 mt-0.5 leading-snug">{s.blurb}</div>
                          </button>
                        ))}
                      </div>
                      <button
                        onClick={() => setAdding(false)}
                        className="mt-3 text-xs font-semibold text-slate-500 hover:text-slate-700"
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={() => setAdding(true)}
                      className="px-3 py-1.5 text-sm font-semibold text-violet-700 bg-violet-50 hover:bg-violet-100 border border-violet-200 rounded-lg transition-colors"
                    >
                      + Add a guarantee
                    </button>
                  )}
                </div>
              </div>
            </div>

            {/* Footer */}
            <div className="flex items-center justify-between gap-3 px-6 py-3.5 border-t border-slate-200 bg-slate-50">
              <div className="text-[11px] text-slate-500">
                Takes effect on the next run.
              </div>
              <div className="flex items-center gap-2">
                {existed && (
                  <button
                    onClick={remove}
                    disabled={saving}
                    className="px-3 py-1.5 text-sm font-semibold text-red-600 hover:bg-red-50 rounded-lg disabled:opacity-50"
                  >
                    Remove contract
                  </button>
                )}
                <button
                  onClick={onClose}
                  className="px-3 py-1.5 text-sm font-semibold text-slate-600 hover:bg-slate-100 rounded-lg"
                >
                  Cancel
                </button>
                <button
                  onClick={save}
                  disabled={saving || guarantees.length === 0 || broken.length > 0}
                  className="px-4 py-1.5 text-sm font-semibold text-white bg-violet-600 hover:bg-violet-700 rounded-lg disabled:opacity-40 disabled:cursor-not-allowed"
                  title={
                    guarantees.length === 0 ? 'Add at least one guarantee'
                      : broken.length > 0 ? broken[0]
                      : undefined
                  }
                >
                  {saving ? 'Saving…' : existed ? 'Update contract' : 'Create contract'}
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
