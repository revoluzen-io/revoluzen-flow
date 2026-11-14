/**
 * Incidents (2026-11-11): the pipelines that have started failing.
 *
 * A failed run already shows in Runs and the bell. What neither answers is
 * the first question an operator asks: what changed? An incident groups a
 * pipeline's failures from unattended runs (schedules, triggers, API calls)
 * until one succeeds again, and shows the cause class of the error, the
 * step that failed with what feeds it and what it feeds, the versions saved
 * since the pipeline last worked, and what the auto-recover policy tried.
 */

import { useCallback, useEffect, useState } from 'react';
import { api, type Incident, type IncidentDetail, type SharedCause } from '../../api/client';
import { askCopilot } from '../../hooks/useAgentChatStore';
import { toast } from '../Toast';

const CLASS_LABEL: Record<string, { label: string; tone: string; hint: string }> = {
  transient: { label: 'Passing problem', tone: 'bg-sky-50 text-sky-800 border-sky-200', hint: 'A timeout or rate limit. Running again usually works.' },
  dependency: { label: 'Outside system', tone: 'bg-amber-50 text-amber-800 border-amber-200', hint: 'A source or destination was unreachable or refused the login.' },
  data_quality: { label: 'Data', tone: 'bg-red-50 text-red-800 border-red-200', hint: 'The data broke a rule. Running again repeats the failure.' },
  user_input: { label: 'Configuration', tone: 'bg-violet-50 text-violet-800 border-violet-200', hint: 'A setting is wrong or a file is missing.' },
  fatal: { label: 'Crash', tone: 'bg-red-50 text-red-800 border-red-200', hint: 'Out of memory or disk.' },
  unknown: { label: 'Unclassified', tone: 'bg-slate-50 text-slate-700 border-slate-200', hint: 'Flow could not tell what kind of failure this is.' },
};

const EVENT_LABEL: Record<string, string> = {
  opened: 'Opened',
  failed_again: 'Failed again',
  resolved: 'Resolved',
  note: 'Note',
  auto_resume_scheduled: 'Resume scheduled by policy',
  auto_resume_started: 'Resume started',
  auto_resume_finished: 'Resume finished',
  auto_resume_failed: 'Resume could not run',
  auto_resume_skipped: 'Resume not run',
  auto_recover_skipped: 'Not resumed',
};

const when = (iso?: string | null) => (iso ? new Date(iso).toLocaleString() : '—');

function ClassChip({ cls }: { cls: string }) {
  const meta = CLASS_LABEL[cls] || CLASS_LABEL.unknown;
  return (
    <span className={`text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded border ${meta.tone}`} title={meta.hint}>
      {meta.label}
    </span>
  );
}

function eventLine(kind: string, d: Record<string, any>): string {
  if (kind === 'note') return `${d.note} — ${d.by}`;
  if (kind === 'resolved') return d.resolution || '';
  if (kind === 'auto_resume_scheduled') return `Attempt ${d.attempt} of ${d.of}, in ${Math.round((d.in_seconds || 0) / 60)} min`;
  if (kind === 'auto_resume_finished') return `The resumed run ended: ${d.status}`;
  if (d.reason) return d.reason;
  if (d.error) return d.error;
  if (d.failed_step) return `at ${d.failed_step}`;
  return '';
}

function Detail({ id, onChanged }: { id: string; onChanged: () => void }) {
  const [data, setData] = useState<IncidentDetail | null>(null);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api.getIncident(id).then(setData).catch((e) => toast.error(`Could not load the incident: ${e instanceof Error ? e.message : e}`));
  }, [id]);
  useEffect(() => { setData(null); load(); }, [load]);

  if (!data) return <div className="p-6 text-sm text-slate-500">Loading…</div>;
  const { incident: inc, events, context } = data;

  const act = async (fn: () => Promise<unknown>, done: string) => {
    setBusy(true);
    try {
      await fn();
      toast.success(done);
      setNote('');
      load();
      onChanged();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="p-5 space-y-5">
      <div>
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-base font-semibold text-slate-900">{inc.workflow_name}</span>
          <ClassChip cls={inc.failure_class} />
          <span className={`text-[10px] font-bold uppercase px-1.5 py-0.5 rounded ${inc.status === 'open' ? 'bg-red-100 text-red-700' : 'bg-emerald-100 text-emerald-700'}`}>{inc.status}</span>
        </div>
        <div className="text-xs text-slate-500 mt-1">
          Failing since {when(inc.opened_at)} · {inc.occurrences} failed run{inc.occurrences === 1 ? '' : 's'}
          {inc.failed_step && <> · at <span className="font-semibold text-slate-700">{inc.failed_step}</span></>}
        </div>
        {inc.error && (
          <pre className="mt-2 text-xs bg-slate-50 border border-slate-200 rounded-lg p-2.5 whitespace-pre-wrap break-words text-slate-700 max-h-40 overflow-auto">{inc.error}</pre>
        )}
        <div className="text-[11px] text-slate-500 mt-1">{(CLASS_LABEL[inc.failure_class] || CLASS_LABEL.unknown).hint}</div>
      </div>

      <div className="grid md:grid-cols-2 gap-4">
        <div className="rounded-lg border border-slate-200 p-3">
          <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500 mb-1.5">What changed since it last worked</div>
          <div className="text-xs text-slate-600 mb-2">
            {context.last_success
              ? <>Last good run: {when(context.last_success.started_at)} ({context.last_success.triggered_by})</>
              : <>No successful run on record before this.</>}
          </div>
          {context.changes_since.length === 0 ? (
            <div className="text-xs text-slate-500">No version was saved since then, so the pipeline did not change: look at its sources.</div>
          ) : (
            <ul className="space-y-1">
              {context.changes_since.map((v) => (
                <li key={v.version} className="text-xs text-slate-700">
                  <span className="font-mono font-semibold">v{v.version}</span>{v.live && <span className="ml-1 text-[10px] font-bold text-emerald-700">LIVE</span>}
                  {' '}{v.change_summary || v.kind} <span className="text-slate-400">— {v.created_by}, {when(v.created_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="rounded-lg border border-slate-200 p-3">
          <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500 mb-1.5">Around the failed step</div>
          <div className="text-xs text-slate-700"><span className="text-slate-500">Reads from:</span> {context.upstream.length ? context.upstream.join(' ← ') : 'nothing (it is a source)'}</div>
          <div className="text-xs text-slate-700 mt-1"><span className="text-slate-500">Feeds:</span> {context.downstream.length ? context.downstream.join(', ') : 'nothing'}</div>
          <div className="text-xs text-slate-500 mt-2">
            {data.policy.enabled
              ? <>Auto-recover is on: {data.policy.failure_classes.join(', ')} failures are resumed up to {data.policy.max_attempts} time(s). Attempts so far: {inc.auto_attempts}.</>
              : <>Auto-recover is off. An admin can turn it on in Settings.</>}
          </div>
        </div>
      </div>

      <div>
        <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500 mb-1.5">Timeline</div>
        <ol className="space-y-1">
          {events.map((e) => (
            <li key={e.id} className="text-xs text-slate-700 flex gap-2">
              <span className="text-slate-400 whitespace-nowrap">{when(e.at)}</span>
              <span className="font-semibold whitespace-nowrap">{EVENT_LABEL[e.kind] || e.kind}</span>
              <span className="text-slate-600">{eventLine(e.kind, e.detail)}</span>
              {e.run_id && (
                <a href={`#executions/${e.run_id}`} className="text-blue-600 hover:underline whitespace-nowrap">run</a>
              )}
            </li>
          ))}
        </ol>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button
          onClick={() => askCopilot(`Why is the pipeline "${inc.workflow_name}" failing? Read incident ${inc.id}, tell me what changed since it last worked, and suggest a fix.`)}
          className="px-3 py-1.5 text-sm font-semibold text-white bg-violet-600 hover:bg-violet-700 rounded-lg"
        >
          Ask the Copilot
        </button>
        <a href={`#editor/${inc.workflow_id}`} className="px-3 py-1.5 text-sm font-semibold text-slate-700 border border-slate-200 rounded-lg hover:bg-slate-50">Open pipeline</a>
        {inc.status === 'open' && (
          <>
            <input
              className="flex-1 min-w-[12rem] px-2.5 py-1.5 text-sm border border-slate-300 rounded-lg"
              placeholder="Add a note: what was tried, who is on it"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
            <button disabled={busy || !note.trim()} onClick={() => act(() => api.addIncidentNote(inc.id, note), 'Note added.')}
              className="px-3 py-1.5 text-sm font-semibold text-slate-700 border border-slate-200 rounded-lg hover:bg-slate-50 disabled:opacity-40">Add note</button>
            <button disabled={busy} onClick={() => act(() => api.resolveIncident(inc.id, note), 'Incident resolved.')}
              className="px-3 py-1.5 text-sm font-semibold text-emerald-700 border border-emerald-200 rounded-lg hover:bg-emerald-50 disabled:opacity-40">Mark resolved</button>
          </>
        )}
      </div>
    </div>
  );
}

export default function IncidentsPanel() {
  const [status, setStatus] = useState<'open' | 'resolved'>('open');
  const [items, setItems] = useState<Incident[] | null>(null);
  const [shared, setShared] = useState<SharedCause[]>([]);
  const [selected, setSelected] = useState<string | null>(null);

  const load = useCallback(() => {
    api.listIncidents(status)
      .then((r) => { setItems(r.incidents); setShared(r.shared_causes || []); })
      .catch(() => { setItems([]); setShared([]); });
  }, [status]);
  useEffect(() => { setItems(null); load(); }, [load]);

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <div className="text-sm text-slate-600 flex-1">
          Pipelines whose unattended runs are failing, grouped until a run succeeds again.
        </div>
        {(['open', 'resolved'] as const).map((s) => (
          <button key={s} onClick={() => { setStatus(s); setSelected(null); }}
            className={`px-3 py-1 text-xs font-semibold rounded-lg border ${status === s ? 'bg-slate-800 text-white border-slate-800' : 'text-slate-600 border-slate-200 hover:bg-slate-50'}`}>
            {s === 'open' ? 'Open' : 'Resolved'}
          </button>
        ))}
      </div>
      {/* 2026-11-11: open incidents that fail for one reason (incidents/shared.py). */}
      {status === 'open' && shared.map((g, n) => (
        <div key={n} className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900" data-testid="shared-cause">
          <div className="font-semibold">
            {g.count} pipelines are failing for one reason: {g.cause}
          </div>
          <div className="text-xs mt-1">
            {g.pipelines.map((p) => p.workflow_name).join(', ')}.{' '}
            {g.connection
              ? `Fix or test the connection "${g.connection.name}" once, rather than each pipeline.`
              : 'They share the same error; the cause is likely upstream of them all.'}
          </div>
        </div>
      ))}
      <div className="grid lg:grid-cols-[minmax(18rem,26rem)_1fr] gap-4">
        <div className="rounded-xl border border-slate-200 bg-white overflow-hidden">
          {items === null ? (
            <div className="p-6 text-sm text-slate-500">Loading…</div>
          ) : items.length === 0 ? (
            <div className="p-6 text-sm text-slate-500">
              {status === 'open' ? 'Nothing is failing. Incidents open when a scheduled, triggered or API run fails.' : 'No resolved incidents yet.'}
            </div>
          ) : (
            <ul className="divide-y divide-slate-100">
              {items.map((i) => (
                <li key={i.id}>
                  <button onClick={() => setSelected(i.id)} className={`w-full text-left px-4 py-3 hover:bg-slate-50 ${selected === i.id ? 'bg-slate-50' : ''}`}>
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-semibold text-slate-800 truncate flex-1">{i.workflow_name}</span>
                      <ClassChip cls={i.failure_class} />
                    </div>
                    <div className="text-[11px] text-slate-500 mt-0.5">
                      {i.failed_step ? `at ${i.failed_step} · ` : ''}{i.occurrences} failure{i.occurrences === 1 ? '' : 's'} · since {when(i.opened_at)}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="rounded-xl border border-slate-200 bg-white min-h-[12rem]">
          {selected ? <Detail id={selected} onChanged={load} /> : (
            <div className="p-6 text-sm text-slate-500">Pick an incident to see what changed and what it touches.</div>
          )}
        </div>
      </div>
    </div>
  );
}
