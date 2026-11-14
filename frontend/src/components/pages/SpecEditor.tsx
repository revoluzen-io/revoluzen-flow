/**
 * The intent spec editor (2026-11-11): what a pipeline was asked to do.
 *
 * The goal is the pipeline's business purpose. The rest is the spec saved on
 * the pipeline (backend `ir/spec.py`), so every version keeps the spec it was
 * built to: who answers for the result, who uses it, where the data comes
 * from, how to tell it worked, and what should change in the business.
 *
 * Each acceptance criterion is the requester's own sentence plus how it is
 * checked. A criterion that names a check becomes a guarantee in the
 * pipeline's Execution Contract when the author makes the contract from the
 * spec; one left to a person is listed as such, never turned into a clause
 * that measures something else.
 */

import { useEffect, useMemo, useState } from 'react';
import { api, type ContractStatus, type PipelineSpec, type SpecCriterion } from '../../api/client';
import { toast } from '../Toast';

type CheckName =
  | 'no_empty_output' | 'row_count_min' | 'row_count_max' | 'row_count_variance'
  | 'schema_contains' | 'no_duplicate_key' | 'freshness_max_age';

type Field = 'min_rows' | 'max_rows' | 'expected_rows' | 'variance_pct' | 'columns' | 'key'
  | 'time_column' | 'max_age_hours';

/** Mirrors `SPEC_CHECKS` in backend/revoluzen_flow/ir/spec.py. */
const CHECKS: { check: CheckName; label: string; fields: Field[]; example: string }[] = [
  { check: 'no_empty_output', label: 'Never empty', fields: [], example: 'There is always at least one order' },
  { check: 'row_count_min', label: 'At least this many rows', fields: ['min_rows'], example: 'All 12 regions appear' },
  { check: 'row_count_max', label: 'At most this many rows', fields: ['max_rows'], example: 'One row per customer, under 50,000' },
  { check: 'row_count_variance', label: 'Close to an expected number of rows', fields: ['expected_rows', 'variance_pct'], example: 'About 20,000 orders a day, give or take 10%' },
  { check: 'schema_contains', label: 'Has these columns', fields: ['columns'], example: 'The dashboard reads region and revenue' },
  { check: 'no_duplicate_key', label: 'One row per key', fields: ['key'], example: 'Every order appears exactly once' },
  { check: 'freshness_max_age', label: 'Newest row no older than', fields: ['time_column', 'max_age_hours'], example: 'Includes yesterday’s orders' },
];
const BY_CHECK = new Map(CHECKS.map((c) => [c.check, c]));

const FIELD_LABEL: Record<Field, string> = {
  min_rows: 'Minimum rows',
  max_rows: 'Maximum rows',
  expected_rows: 'Expected rows',
  variance_pct: 'Tolerance (%)',
  columns: 'Columns',
  key: 'Key column',
  time_column: 'Time column',
  max_age_hours: 'Max age (hours)',
};

interface DraftCriterion {
  id: string;
  text: string;
  check: string;
  min_rows: string;
  max_rows: string;
  expected_rows: string;
  variance_pct: string;
  columns: string;
  key: string;
  time_column: string;
  max_age_hours: string;
  output_step: string;
}

const newId = () => Math.random().toString(16).slice(2, 10);

function blank(): DraftCriterion {
  return {
    id: newId(), text: '', check: '',
    min_rows: '', max_rows: '', expected_rows: '', variance_pct: '10',
    columns: '', key: '', time_column: '', max_age_hours: '', output_step: '',
  };
}

function fromApi(c: SpecCriterion): DraftCriterion {
  const d = blank();
  d.id = c.id || d.id;
  d.text = c.text || '';
  d.check = c.check || '';
  if (c.min_rows != null) d.min_rows = String(c.min_rows);
  if (c.max_rows != null) d.max_rows = String(c.max_rows);
  if (c.expected_rows != null) d.expected_rows = String(c.expected_rows);
  if (c.variance_pct != null) d.variance_pct = String(Math.round(c.variance_pct * 1000) / 10);
  if (c.columns) d.columns = c.columns.join(', ');
  if (c.key) d.key = c.key;
  if (c.time_column) d.time_column = c.time_column;
  if (c.max_age_seconds != null) d.max_age_hours = String(Math.round((c.max_age_seconds / 3600) * 100) / 100);
  if (c.output_step) d.output_step = c.output_step;
  return d;
}

function toApi(d: DraftCriterion): SpecCriterion {
  const meta = BY_CHECK.get(d.check as CheckName);
  const out: SpecCriterion = { id: d.id, text: d.text.trim(), check: meta ? d.check : '' };
  if (d.output_step) out.output_step = d.output_step;
  const num = (raw: string) => (raw.trim() === '' || Number.isNaN(Number(raw)) ? null : Number(raw));
  for (const f of meta?.fields || []) {
    if (f === 'columns') {
      const cols = d.columns.split(',').map((c) => c.trim()).filter(Boolean);
      if (cols.length) out.columns = cols;
    } else if (f === 'key' || f === 'time_column') {
      if (d[f].trim()) out[f] = d[f].trim();
    } else if (f === 'variance_pct') {
      // Said as a percentage, stored as a fraction (0.1 == within 10%).
      const pct = num(d.variance_pct);
      if (pct != null) out.variance_pct = pct / 100;
    } else if (f === 'max_age_hours') {
      const hours = num(d.max_age_hours);
      if (hours != null) out.max_age_seconds = Math.round(hours * 3600);
    } else {
      const n = num(d[f]);
      if (n != null) out[f] = Math.round(n);
    }
  }
  return out;
}

/** Settings a criterion's check needs and does not have yet. */
function missing(d: DraftCriterion): string[] {
  const meta = BY_CHECK.get(d.check as CheckName);
  if (!meta) return [];
  return meta.fields
    .filter((f) => f !== 'variance_pct' || d.variance_pct.trim() === '')
    .filter((f) => d[f].trim() === '')
    .map((f) => FIELD_LABEL[f].toLowerCase());
}

const input = 'w-full px-2.5 py-1.5 text-sm border border-slate-300 rounded-md focus:outline-none focus:ring-2 focus:ring-violet-300';
const small = 'px-2 py-1 text-xs border border-slate-300 rounded-md focus:outline-none focus:ring-2 focus:ring-violet-300';

export default function SpecEditor({ pipelineId, onClose, onContractMade }: {
  pipelineId: string;
  onClose: () => void;
  onContractMade?: () => void;
}) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [goal, setGoal] = useState('');
  const [owner, setOwner] = useState('');
  const [consumers, setConsumers] = useState('');
  const [sources, setSources] = useState('');
  const [outcome, setOutcome] = useState('');
  const [criteria, setCriteria] = useState<DraftCriterion[]>([]);
  const [steps, setSteps] = useState<{ id: string; label: string }[]>([]);
  const [status, setStatus] = useState<ContractStatus | null>(null);
  const [savedBy, setSavedBy] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [wfRes, statusRes] = await Promise.allSettled([
        api.getWorkflow(pipelineId),
        api.getExecutionContractStatus(pipelineId),
      ]);
      if (cancelled) return;
      if (wfRes.status === 'fulfilled') {
        const wf = (wfRes.value as any)?.workflow || wfRes.value;
        setGoal(wf?.business_purpose || '');
        setSteps(((wf?.steps || []) as any[]).map((s) => ({ id: s.id, label: s.label || s.id })));
        const spec: PipelineSpec | null = wf?.spec || null;
        if (spec) {
          setOwner(spec.business_owner || '');
          setConsumers(spec.consumers || '');
          setSources((spec.sources || []).join('\n'));
          setOutcome(spec.outcome || '');
          setCriteria((spec.acceptance || []).map(fromApi));
          setSavedBy(spec.updated_by ? `${spec.updated_by}${spec.updated_at ? `, ${new Date(spec.updated_at).toLocaleString()}` : ''}` : null);
        }
      }
      if (statusRes.status === 'fulfilled') setStatus(statusRes.value);
      setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [pipelineId]);

  const update = (i: number, patch: Partial<DraftCriterion>) =>
    setCriteria((prev) => prev.map((c, j) => (j === i ? { ...c, ...patch } : c)));

  const checked = criteria.filter((c) => c.check && missing(c).length === 0).length;
  const byPeople = criteria.filter((c) => !c.check && c.text.trim()).length;
  const incomplete = useMemo(
    () => criteria.flatMap((c, i) => (missing(c).length ? [`#${i + 1} needs ${missing(c).join(', ')}`] : [])),
    [criteria],
  );

  const spec = (): PipelineSpec => ({
    business_owner: owner.trim(),
    consumers: consumers.trim(),
    sources: sources.split('\n').map((s) => s.trim()).filter(Boolean),
    acceptance: criteria.filter((c) => c.text.trim() || c.check).map(toApi),
    outcome: outcome.trim(),
  });

  const save = async (makeContract: boolean) => {
    setSaving(true);
    try {
      await api.updateWorkflowDocs(pipelineId, { business_purpose: goal.trim(), spec: spec() });
      if (!makeContract) {
        toast.success('Spec saved with a new version of the pipeline.');
        onClose();
        return;
      }
      const made = await api.contractFromSpec(pipelineId);
      const parts = [`${made.from_spec} check${made.from_spec === 1 ? '' : 's'} from the spec`];
      if (made.kept) parts.push(`${made.kept} written by hand kept`);
      if (made.checked_by_people.length) parts.push(`${made.checked_by_people.length} left to a person`);
      if (made.incomplete.length) parts.push(`${made.incomplete.length} incomplete, not added`);
      toast.success(`Contract made: ${parts.join(', ')}. It applies from the next run.`);
      onContractMade?.();
    } catch (e) {
      toast.error(`Could not save: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return <div className="flex-1 px-6 py-10 text-sm text-slate-500">Loading spec…</div>;
  }

  const verdict = status?.last_verdict;
  return (
    <>
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
        <div className="rounded-lg border border-violet-200 bg-violet-50/50 px-3.5 py-2.5 text-xs text-violet-900">
          What this pipeline is for, in the words of whoever asked for it. The spec is saved with
          every version. Criteria that say how they are checked become the pipeline’s contract,
          so each run is judged against them and its receipt names this spec.
        </div>

        {status?.contract && (
          <div className={`rounded-lg border px-3.5 py-2.5 text-xs ${
            status.contract.behind_spec ? 'bg-amber-50 border-amber-200 text-amber-900' : 'bg-slate-50 border-slate-200 text-slate-700'
          }`}>
            {status.contract.from_spec > 0
              ? <>The contract holds {status.contract.from_spec} check{status.contract.from_spec === 1 ? '' : 's'} made from{status.contract.spec_version != null ? ` version ${status.contract.spec_version} of` : ''} this spec.</>
              : <>The contract was written by hand; none of its checks came from this spec.</>}
            {status.contract.behind_spec && <> The spec has changed since, so make the contract again to apply it.</>}
            {verdict && (
              <div className={`mt-1 font-semibold ${verdict.verdict === 'verified' ? 'text-emerald-700' : 'text-red-700'}`}>
                Last run of this version: {verdict.verdict === 'verified' ? 'kept every promise' : `broke ${verdict.failed.join('; ')}`}
              </div>
            )}
          </div>
        )}

        <label className="block">
          <span className="text-xs font-semibold text-slate-600">Goal</span>
          <textarea
            className={`${input} mt-1`}
            rows={2}
            placeholder="Give finance one table of orders by region for the month-end close"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
          />
          <span className="text-[11px] text-slate-400">The pipeline’s business purpose. Publishing asks for it.</span>
        </label>

        <div className="grid grid-cols-2 gap-3">
          <label className="block">
            <span className="text-xs font-semibold text-slate-600">Business owner</span>
            <input className={`${input} mt-1`} placeholder="Finance operations" value={owner} onChange={(e) => setOwner(e.target.value)} />
          </label>
          <label className="block">
            <span className="text-xs font-semibold text-slate-600">Who uses the output</span>
            <input className={`${input} mt-1`} placeholder="The month-end close dashboard" value={consumers} onChange={(e) => setConsumers(e.target.value)} />
          </label>
        </div>

        <label className="block">
          <span className="text-xs font-semibold text-slate-600">Where the data comes from</span>
          <textarea
            className={`${input} mt-1`}
            rows={2}
            placeholder={'ERP orders export\nCRM region list'}
            value={sources}
            onChange={(e) => setSources(e.target.value)}
          />
          <span className="text-[11px] text-slate-400">One per line, as the requester names them.</span>
        </label>

        <div>
          <div className="flex items-center justify-between mb-2">
            <span className="text-[10px] font-bold uppercase tracking-wider text-slate-500">How to tell it worked</span>
            <span className="text-[11px] text-slate-500">
              {checked} checked by the contract · {byPeople} by a person
            </span>
          </div>
          {criteria.length === 0 && (
            <div className="rounded-lg border border-dashed border-slate-300 px-4 py-5 text-center text-sm text-slate-500">
              No acceptance criteria yet.
            </div>
          )}
          <div className="space-y-3">
            {criteria.map((c, i) => {
              const meta = BY_CHECK.get(c.check as CheckName);
              const gaps = missing(c);
              return (
                <div key={c.id} className="rounded-lg border border-slate-200 p-3">
                  <div className="flex items-start gap-2">
                    <input
                      className={`${input} flex-1`}
                      placeholder={`In the requester’s words — e.g. “${(meta || CHECKS[5]).example}”`}
                      value={c.text}
                      onChange={(e) => update(i, { text: e.target.value })}
                    />
                    <button
                      onClick={() => setCriteria((p) => p.filter((_, j) => j !== i))}
                      className="w-7 h-7 rounded flex items-center justify-center text-slate-400 hover:text-red-600 hover:bg-red-50 shrink-0"
                      title="Remove this criterion"
                      aria-label="Remove this criterion"
                    >
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="3 6 5 6 21 6" /><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" /></svg>
                    </button>
                  </div>
                  <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-2">
                    <label className="flex items-center gap-2">
                      <span className="text-xs text-slate-600 shrink-0">Checked by</span>
                      <select
                        className={`${small} bg-white`}
                        value={c.check}
                        onChange={(e) => update(i, { check: e.target.value })}
                      >
                        <option value="">A person</option>
                        {CHECKS.map((k) => <option key={k.check} value={k.check}>The contract: {k.label.toLowerCase()}</option>)}
                      </select>
                    </label>
                    {meta?.fields.map((f) => (
                      <label key={f} className="flex items-center gap-2">
                        <span className="text-xs text-slate-600 shrink-0">{FIELD_LABEL[f]}</span>
                        <input
                          className={`${small} ${f === 'columns' ? 'w-48' : f === 'key' || f === 'time_column' ? 'w-36' : 'w-24'}`}
                          type={f === 'columns' || f === 'key' || f === 'time_column' ? 'text' : 'number'}
                          min={f === 'columns' || f === 'key' || f === 'time_column' ? undefined : 0}
                          placeholder={f === 'columns' ? 'region, revenue' : f === 'key' ? 'order_id' : f === 'time_column' ? 'ordered_at' : ''}
                          value={c[f]}
                          onChange={(e) => update(i, { [f]: e.target.value } as Partial<DraftCriterion>)}
                        />
                      </label>
                    ))}
                    {meta && steps.length > 0 && (
                      <label className="flex items-center gap-2">
                        <span className="text-xs text-slate-600 shrink-0">Measure step</span>
                        <select
                          className={`${small} bg-white`}
                          value={c.output_step}
                          onChange={(e) => update(i, { output_step: e.target.value })}
                        >
                          <option value="">The last step</option>
                          {steps.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
                        </select>
                      </label>
                    )}
                  </div>
                  {gaps.length > 0 && (
                    <div className="mt-2 text-[11px] font-semibold text-amber-700">
                      Needs {gaps.join(', ')} before it can go into the contract.
                    </div>
                  )}
                  {!meta && (
                    <div className="mt-2 text-[11px] text-slate-500">
                      Listed in the contract summary as checked by a person; no run passes or fails it.
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          <button
            onClick={() => setCriteria((p) => [...p, blank()])}
            className="mt-3 px-3 py-1.5 text-sm font-semibold text-violet-700 bg-violet-50 hover:bg-violet-100 border border-violet-200 rounded-lg transition-colors"
          >
            + Add a criterion
          </button>
        </div>

        <label className="block">
          <span className="text-xs font-semibold text-slate-600">What should change when it works</span>
          <textarea
            className={`${input} mt-1`}
            rows={2}
            placeholder="Finance closes the month a day sooner"
            value={outcome}
            onChange={(e) => setOutcome(e.target.value)}
          />
        </label>
      </div>

      <div className="flex items-center justify-between gap-3 px-6 py-3.5 border-t border-slate-200 bg-slate-50">
        <div className="text-[11px] text-slate-500 truncate">
          {incomplete.length ? `Incomplete: ${incomplete[0]}` : savedBy ? `Last saved by ${savedBy}` : 'Saved as a new version of the pipeline.'}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button onClick={onClose} className="px-3 py-1.5 text-sm font-semibold text-slate-600 hover:bg-slate-100 rounded-lg">
            Cancel
          </button>
          <button
            onClick={() => save(false)}
            disabled={saving}
            className="px-3 py-1.5 text-sm font-semibold text-violet-700 bg-white border border-violet-200 hover:bg-violet-50 rounded-lg disabled:opacity-40"
          >
            Save spec
          </button>
          <button
            onClick={() => save(true)}
            disabled={saving || checked === 0}
            title={checked === 0 ? 'Pick how at least one criterion is checked, and fill in its settings' : undefined}
            className="px-4 py-1.5 text-sm font-semibold text-white bg-violet-600 hover:bg-violet-700 rounded-lg disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {saving ? 'Saving…' : 'Save and make contract'}
          </button>
        </div>
      </div>
    </>
  );
}
