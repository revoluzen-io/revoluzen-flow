import { useEffect, useState } from 'react';
import { api, authHeaders } from '../../api/client';
import { toast } from '../Toast';
import Icon from '../shared/Icon';
import { renderMarkdown } from '../../utils/markdown';

// The installation inventory: one fixed document describing everything in this
// install, as PDF or Word. What used to be the whole Reports page; it stays as
// a starting point because auditors and handovers need exactly this.
//
// Endpoints: GET /api/reports/inventory?format=<docx|pdf|json>&scope=&scope_id=&env=

type Scope = 'admin' | 'user' | 'project' | 'pipeline';
type Format = 'pdf' | 'docx';

interface Option { id: string; name: string }

function currentEnvironment(): 'dev' | 'prod' {
  try {
    return (localStorage.getItem('revoluzen_flow_env') || 'dev').toLowerCase() === 'prod' ? 'prod' : 'dev';
  } catch {
    return 'dev';
  }
}

export default function InventoryDocument({ isAdmin, onClose }: { isAdmin: boolean; onClose: () => void }) {
  const [scope, setScope] = useState<Scope>(isAdmin ? 'admin' : 'user');
  const [targetId, setTargetId] = useState('');
  const [format, setFormat] = useState<Format>('pdf');
  const [options, setOptions] = useState<Option[]>([]);
  const [busy, setBusy] = useState<'' | 'view' | 'download'>('');
  const [viewData, setViewData] = useState<any | null>(null);

  useEffect(() => {
    setTargetId('');
    setOptions([]);
    if (scope === 'project') {
      void api.listProjects().then((rows: any[]) => setOptions((rows || []).map((p: any) => ({ id: p.id, name: p.name || p.id }))));
    } else if (scope === 'pipeline') {
      void api.listWorkflows().then((rows: any[]) => setOptions((rows || []).map((p: any) => ({ id: p.id, name: p.name || p.id }))));
    }
  }, [scope]);

  const needsTarget = scope === 'project' || scope === 'pipeline';
  const query = (fmt: string) => {
    const params = new URLSearchParams({ format: fmt, scope, env: currentEnvironment() });
    if (needsTarget && targetId) params.set('scope_id', targetId);
    return `/api/reports/inventory?${params.toString()}`;
  };

  const download = async () => {
    setBusy('download');
    try {
      const res = await fetch(query(format), { headers: authHeaders() });
      if (!res.ok) throw new Error(`Download failed (${res.status})`);
      const blob = await res.blob();
      const match = /filename="([^"]+)"/.exec(res.headers.get('content-disposition') || '');
      const filename = match ? match[1] : `revoluzen-flow-inventory.${format}`;
      const objectId = res.headers.get('x-storage-object-id') || '';
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      if (objectId) {
        try { sessionStorage.setItem('revoluzen_flow_storage_highlight_object', objectId); } catch { /* only highlights the row */ }
      }
      toast.success(`Downloaded ${filename}`, objectId ? 'A copy is in Storage > Files.' : undefined);
    } catch (err: any) {
      toast.error('The download failed', err.message);
    } finally {
      setBusy('');
    }
  };

  const view = async () => {
    setBusy('view');
    try {
      const res = await fetch(query('json'), { headers: authHeaders() });
      if (!res.ok) throw new Error(`Could not open the report (${res.status})`);
      setViewData(await res.json());
    } catch (err: any) {
      toast.error('Could not open the report', err.message);
    } finally {
      setBusy('');
    }
  };

  const scopes: { value: Scope; label: string; hint: string; adminOnly?: boolean }[] = [
    { value: 'admin', label: 'Whole installation', hint: 'Every project, pipeline and connection', adminOnly: true },
    { value: 'project', label: 'One project', hint: 'Its pipelines and assets' },
    { value: 'pipeline', label: 'One pipeline', hint: 'Its configuration, runs and schedule' },
    { value: 'user', label: 'What I can see', hint: 'Limited to your own access' },
  ];

  return (
    <>
      <div className="fixed inset-0 z-[60] bg-black/40" onClick={onClose} />
      <div className="pointer-events-none fixed inset-0 z-[65] flex items-center justify-center p-4">
        {viewData ? (
          <div role="dialog" aria-label="Inventory report" className="pointer-events-auto flex h-[90vh] w-[960px] max-w-[96vw] flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl">
            <div className="flex shrink-0 items-center justify-between border-b border-slate-200 px-5 py-3">
              <button type="button" className="text-xs font-semibold text-slate-500 hover:text-slate-800" onClick={() => setViewData(null)}>← Back</button>
              <button type="button" disabled={Boolean(busy)} className="inline-flex items-center gap-1.5 rounded-lg bg-slate-800 px-3 py-1.5 text-xs font-bold text-white hover:bg-slate-700 disabled:opacity-50" onClick={() => void download()}>
                <Icon name="download" size={12} />Download {format === 'pdf' ? 'PDF' : 'Word'}
              </button>
            </div>
            <div className="flex-1 overflow-y-auto p-6"><InventoryView data={viewData} /></div>
          </div>
        ) : (
          <div role="dialog" aria-label="Installation inventory" className="pointer-events-auto w-[560px] max-w-[96vw] rounded-2xl border border-slate-200 bg-white p-5 shadow-2xl">
            <h3 className="text-sm font-bold text-slate-900">Installation inventory</h3>
            <p className="mt-1 text-xs text-slate-500">One document listing what is in this install: projects, pipelines, connections, schedules, alert rules and triggers, with passwords and keys left out.</p>
            <div className="mt-3 grid grid-cols-2 gap-2">
              {scopes.filter((s) => !s.adminOnly || isAdmin).map((s) => (
                <button
                  key={s.value}
                  type="button"
                  aria-pressed={scope === s.value}
                  onClick={() => setScope(s.value)}
                  className={`rounded-lg border p-2.5 text-left ${scope === s.value ? 'border-blue-300 bg-blue-50' : 'border-slate-200 bg-white hover:border-slate-300'}`}
                >
                  <div className="text-[13px] font-medium text-slate-800">{s.label}</div>
                  <div className="text-[11.5px] text-slate-500">{s.hint}</div>
                </button>
              ))}
            </div>
            {needsTarget && (
              <select aria-label={`Choose a ${scope}`} className="mt-3 w-full rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-sm text-slate-800" value={targetId} onChange={(e) => setTargetId(e.target.value)}>
                <option value="">Choose a {scope}</option>
                {options.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
              </select>
            )}
            <div className="mt-3 flex items-center gap-1">
              {(['pdf', 'docx'] as Format[]).map((f) => (
                <button key={f} type="button" aria-pressed={format === f} onClick={() => setFormat(f)} className={`rounded-md px-3 py-1 text-xs font-semibold ${format === f ? 'bg-slate-800 text-white' : 'border border-slate-200 bg-white text-slate-600'}`}>
                  {f === 'pdf' ? 'PDF' : 'Word'}
                </button>
              ))}
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <button type="button" onClick={onClose} className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600">Close</button>
              <button type="button" disabled={Boolean(busy) || (needsTarget && !targetId)} onClick={() => void view()} className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 disabled:opacity-50">{busy === 'view' ? 'Opening…' : 'View'}</button>
              <button type="button" disabled={Boolean(busy) || (needsTarget && !targetId)} onClick={() => void download()} className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs font-bold text-white hover:bg-slate-700 disabled:opacity-50">{busy === 'download' ? 'Making…' : 'Download'}</button>
            </div>
          </div>
        )}
      </div>
    </>
  );
}

function shortTime(s: string): string {
  if (!s) return '—';
  if (s.includes('T')) {
    const [d, r] = s.split('T');
    return `${d} ${(r || '').slice(0, 5)}`.trim();
  }
  return s.slice(0, 16);
}

function InventoryView({ data }: { data: any }) {
  const t = data.totals || {};
  const health = data.health || {};
  const projects: any[] = data.projects || [];
  const score = health.score ?? 0;
  const scoreClass = score >= 80 ? 'text-emerald-600' : score >= 50 ? 'text-amber-600' : 'text-rose-600';
  const stats: [string, any, string][] = [
    ['Health', `${score}/100`, scoreClass],
    ['Pipelines', t.pipelines ?? 0, 'text-slate-900'],
    ['Connections', t.connections ?? 0, 'text-slate-900'],
    ['Projects', t.projects ?? 0, 'text-slate-900'],
  ];
  return (
    <div className="text-slate-700">
      <h1 className="text-xl font-bold text-slate-900">{data.workspace_name || 'Workspace'}: inventory</h1>
      <p className="mt-1 text-xs text-slate-500">Made {shortTime(data.generated_at)} · Revoluzen Flow {data.revoluzen_flow_version}</p>
      <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        {stats.map(([label, value, tone]) => (
          <div key={label} className="rounded-lg border border-slate-200 bg-white px-3 py-2">
            <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500">{label}</div>
            <div className={`text-lg font-bold ${tone}`}>{value}</div>
          </div>
        ))}
      </div>
      {Array.isArray(health.issues) && health.issues.length > 0 && (
        <ul className="mt-3 list-disc space-y-0.5 rounded-lg border border-amber-200 bg-amber-50 px-6 py-2 text-xs text-amber-800">
          {health.issues.slice(0, 6).map((issue: string) => <li key={issue}>{issue}</li>)}
        </ul>
      )}
      {projects.length === 0 && <p className="mt-6 text-sm text-slate-500">No projects or published pipelines here yet.</p>}
      {projects.map((project: any) => (
        <section key={project.id} className="mt-6">
          <h2 className="border-b border-slate-100 pb-1.5 text-base font-semibold text-slate-900">{project.name}</h2>
          {(project.pipelines || []).map((p: any) => (
            <div key={p.id} className="mt-3 rounded-xl border border-slate-200 p-4">
              <div className="flex items-center justify-between gap-2">
                <h3 className="text-sm font-semibold text-slate-900">{p.name}</h3>
                <span className="shrink-0 text-xs text-slate-500">{p.status}</span>
              </div>
              {p.business_purpose && <p className="mt-1 text-sm">{p.business_purpose}</p>}
              <p className="mt-1 text-xs text-slate-500">
                v{p.latest_version} · {p.step_count} steps · {p.last_run_status ? `last run ${p.last_run_status}${p.last_run_at ? `, ${shortTime(p.last_run_at)}` : ''}` : 'never run'}
              </p>
              {p.readme && <div className="mt-2 rounded-lg border border-slate-100 bg-slate-50 px-3 py-2 text-sm">{renderMarkdown(p.readme)}</div>}
            </div>
          ))}
        </section>
      ))}
    </div>
  );
}
