import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { api } from '../../api/client';
import { toast } from '../Toast';
import ReportDocument from './ReportDocument';
import {
  AddSectionPalette, ChosenSections, INPUT, NarrowIt, ProblemList, ScheduleAndShare, SectionMoveTools, WhatGoesIn,
  type PickerData, type Update,
} from './ReportDesignerPanels';
import {
  blankDefinition, definitionFromTemplate, definitionProblems, previewKey,
  type Catalog, type ReportDefinition, type ReportResult, type ResultSection, type SavedReport,
} from './reportModel';
import { reportsApi } from './reportsApi';

// The designer. The three layouts below use the same panels and the same live
// preview, so they can be compared on real data. Once one is chosen: delete
// the other two `*Layout` functions, LAYOUTS, LayoutSwitch and the `layout`
// state.

export type DesignerLayout = 'split' | 'steps' | 'document';

const LAYOUTS: { value: DesignerLayout; label: string; hint: string }[] = [
  { value: 'split', label: 'Side by side', hint: 'Choices on the left, the report on the right.' },
  { value: 'steps', label: 'Step by step', hint: 'One decision at a time, the report at the end.' },
  { value: 'document', label: 'Report first', hint: 'The report fills the page; change it where it stands.' },
];
const LAYOUT_KEY = 'revoluzen_flow_report_designer_layout';

function storedLayout(): DesignerLayout {
  try {
    const saved = localStorage.getItem(LAYOUT_KEY);
    if (saved === 'split' || saved === 'steps' || saved === 'document') return saved;
  } catch {
    /* falls through to the default */
  }
  return 'split';
}

function Card({ title, step, children }: { title: string; step?: number; children: ReactNode }) {
  return (
    <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <h3 className="mb-3 flex items-center gap-2 text-sm font-bold text-slate-800">
        {step !== undefined && <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-slate-100 text-[11px] font-bold text-slate-600">{step}</span>}
        {title}
      </h3>
      {children}
    </section>
  );
}

interface LayoutProps {
  definition: ReportDefinition;
  update: Update;
  catalog: Catalog;
  pickers: PickerData;
  problems: ReturnType<typeof definitionProblems>;
  preview: ReactNode;
  sendTest?: () => void;
  sendingTest: boolean;
  result: ReportResult | null;
  previewBody: (tools?: (s: ResultSection) => ReactNode) => ReactNode;
}

function SplitLayout({ definition, update, catalog, pickers, problems, preview, sendTest, sendingTest }: LayoutProps) {
  return (
    <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-[minmax(340px,430px)_minmax(0,1fr)]">
      <div className="min-w-0 space-y-4">
        <Card title="What goes in" step={1}><WhatGoesIn definition={definition} update={update} catalog={catalog} problems={problems} /></Card>
        <Card title="Narrow it" step={2}><NarrowIt definition={definition} update={update} catalog={catalog} pickers={pickers} problems={problems} /></Card>
        <Card title="Schedule and share" step={3}><ScheduleAndShare definition={definition} update={update} catalog={catalog} problems={problems} onSendTest={sendTest} sendingTest={sendingTest} /></Card>
      </div>
      <div className="min-w-0 lg:sticky lg:top-0">{preview}</div>
    </div>
  );
}

const STEPS = ['What goes in', 'Narrow it', 'Schedule and share', 'Check and save'];

function StepsLayout({ definition, update, catalog, pickers, problems, preview, sendTest, sendingTest }: LayoutProps) {
  const [step, setStep] = useState(0);
  return (
    <div className="space-y-4">
      <ol className="flex flex-wrap gap-1.5" aria-label="Steps">
        {STEPS.map((name, i) => (
          <li key={name}>
            <button
              type="button"
              aria-current={i === step ? 'step' : undefined}
              onClick={() => setStep(i)}
              className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-semibold ${i === step ? 'border-blue-300 bg-blue-50 text-blue-700' : 'border-slate-200 bg-white text-slate-600 hover:border-slate-300'}`}
            >
              <span className="tabular-nums">{i + 1}</span>{name}
            </button>
          </li>
        ))}
      </ol>
      <div className="mx-auto max-w-3xl">
        {step === 0 && <Card title={STEPS[0]}><WhatGoesIn definition={definition} update={update} catalog={catalog} problems={problems} /></Card>}
        {step === 1 && <Card title={STEPS[1]}><NarrowIt definition={definition} update={update} catalog={catalog} pickers={pickers} problems={problems} /></Card>}
        {step === 2 && <Card title={STEPS[2]}><ScheduleAndShare definition={definition} update={update} catalog={catalog} problems={problems} onSendTest={sendTest} sendingTest={sendingTest} /></Card>}
        {step === 3 && preview}
        <div className="mt-3 flex justify-between">
          <button type="button" disabled={step === 0} onClick={() => setStep(step - 1)} className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 disabled:opacity-40">Back</button>
          {step < STEPS.length - 1 && <button type="button" onClick={() => setStep(step + 1)} className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs font-semibold text-white hover:bg-slate-700">Next</button>}
        </div>
      </div>
    </div>
  );
}

function Sheet({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <>
      <div className="fixed inset-0 z-[60] bg-black/30" onClick={onClose} />
      <aside role="dialog" aria-label={title} className="fixed inset-y-0 right-0 z-[65] flex w-[460px] max-w-[96vw] flex-col border-l border-slate-200 bg-white shadow-2xl">
        <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
          <h3 className="text-sm font-bold text-slate-800">{title}</h3>
          <button type="button" aria-label="Close" onClick={onClose} className="rounded p-1 text-slate-500 hover:bg-slate-100">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-4">{children}</div>
      </aside>
    </>
  );
}

function DocumentLayout({ definition, update, catalog, pickers, problems, sendTest, sendingTest, previewBody }: LayoutProps) {
  const [sheet, setSheet] = useState<'add' | 'share' | null>(null);
  const tools = (shown: ResultSection) => {
    const section = definition.sections.find((s) => s.id === shown.id);
    return section ? <SectionMoveTools section={{ ...section, title: shown.title || section.type }} definition={definition} update={update} /> : null;
  };
  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
        <NarrowIt definition={definition} update={update} catalog={catalog} pickers={pickers} problems={problems} compact />
        <div className="mt-3 flex flex-wrap gap-2">
          <button type="button" onClick={() => setSheet('add')} className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:border-slate-300">+ Add or change sections</button>
          <button type="button" onClick={() => setSheet('share')} className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:border-slate-300">Schedule and share{definition.schedule.enabled ? ' (on)' : ''}</button>
        </div>
        <div className="mt-2"><ProblemList problems={problems} areas={['sections', 'schedule', 'recipients']} /></div>
      </div>
      <div className="mx-auto max-w-5xl rounded-xl border border-slate-200 bg-white p-6 shadow-sm">{previewBody(tools)}</div>
      {sheet === 'add' && (
        <Sheet title="Sections" onClose={() => setSheet(null)}>
          <div className="space-y-4">
            <ChosenSections definition={definition} update={update} catalog={catalog} />
            <AddSectionPalette definition={definition} update={update} catalog={catalog} />
          </div>
        </Sheet>
      )}
      {sheet === 'share' && (
        <Sheet title="Schedule and share" onClose={() => setSheet(null)}>
          <ScheduleAndShare definition={definition} update={update} catalog={catalog} problems={problems} onSendTest={sendTest} sendingTest={sendingTest} />
        </Sheet>
      )}
    </div>
  );
}

function LayoutSwitch({ value, onChange }: { value: DesignerLayout; onChange: (v: DesignerLayout) => void }) {
  return (
    <div role="group" aria-label="Designer layout" className="inline-flex items-center gap-1 rounded-lg border border-dashed border-slate-300 bg-white p-0.5" data-testid="layout-switch">
      <span className="px-1.5 text-[10px] font-bold uppercase tracking-wider text-slate-500">Compare layouts</span>
      {LAYOUTS.map((l) => (
        <button
          key={l.value}
          type="button"
          title={l.hint}
          aria-pressed={l.value === value}
          onClick={() => onChange(l.value)}
          className={`rounded-md px-2 py-1 text-xs font-semibold ${l.value === value ? 'bg-slate-800 text-white' : 'text-slate-600 hover:bg-slate-100'}`}
        >
          {l.label}
        </button>
      ))}
    </div>
  );
}

export default function ReportDesigner({
  catalog, existing, templateId, onSaved, onCancel,
}: {
  catalog: Catalog;
  existing?: SavedReport | null;
  templateId?: string | null;
  onSaved: (report: SavedReport) => void;
  onCancel: () => void;
}) {
  const [definition, setDefinition] = useState<ReportDefinition>(() => {
    if (existing) return existing;
    const template = catalog.templates.find((t) => t.id === templateId);
    return template ? definitionFromTemplate(template) : blankDefinition();
  });
  const startedAs = useRef(JSON.stringify(definition));
  const dirty = JSON.stringify(definition) !== startedAs.current;

  const [pickers, setPickers] = useState<PickerData>({ projects: [], pipelines: [] });
  const [layout, setLayout] = useState<DesignerLayout>(storedLayout);
  const [result, setResult] = useState<ReportResult | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState('');
  const [saving, setSaving] = useState(false);
  const [sendingTest, setSendingTest] = useState(false);
  const [showProblems, setShowProblems] = useState(false);

  const update: Update = useCallback((patch) => setDefinition((d) => ({ ...d, ...patch })), []);

  useEffect(() => {
    void Promise.all([api.listProjects(), api.listWorkflows()]).then(([projects, pipelines]: any[]) => {
      const known = (rows: any[]) => (rows || []).map((r: any) => ({ id: r.id, name: r.name || r.id, project_id: r.project_id || '' }));
      const next = { projects: known(projects), pipelines: known(pipelines) };
      setPickers(next);
      // A saved report can point at something that has since been deleted.
      setDefinition((d) => {
        const projectIds = d.filters.project_ids.filter((id) => next.projects.some((p) => p.id === id));
        const workflowIds = d.filters.workflow_ids.filter((id) => next.pipelines.some((p) => p.id === id));
        if (projectIds.length === d.filters.project_ids.length && workflowIds.length === d.filters.workflow_ids.length) return d;
        return { ...d, filters: { ...d.filters, project_ids: projectIds, workflow_ids: workflowIds } };
      });
    }).catch(() => { /* the pickers stay empty; "all" still works */ });
  }, []);

  const key = previewKey(definition);
  const latest = useRef(0);
  useEffect(() => {
    if (definition.sections.length === 0) {
      setResult(null);
      setPreviewError('');
      return undefined;
    }
    const range = definition.filters.time_range;
    if (range.kind === 'custom' && (!range.start || !range.end || range.end < range.start)) return undefined;
    const ticket = latest.current + 1;
    latest.current = ticket;
    setPreviewing(true);
    const timer = window.setTimeout(() => {
      reportsApi.preview(definition)
        .then((made) => { if (latest.current === ticket) { setResult(made); setPreviewError(''); } })
        .catch((err: Error) => { if (latest.current === ticket) setPreviewError(err.message || 'The preview could not be made.'); })
        .finally(() => { if (latest.current === ticket) setPreviewing(false); });
    }, 400);
    return () => window.clearTimeout(timer);
    // `key` is everything in the definition that changes the numbers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const chooseLayout = (next: DesignerLayout) => {
    setLayout(next);
    try { localStorage.setItem(LAYOUT_KEY, next); } catch { /* only remembered for this visit */ }
  };

  const problems = useMemo(() => definitionProblems(definition), [definition]);
  const shownProblems = showProblems ? problems : [];

  const persist = async (): Promise<SavedReport | null> => {
    setShowProblems(true);
    if (problems.length > 0) {
      toast.error('The report is not ready to save', problems[0].message);
      return null;
    }
    setSaving(true);
    try {
      const saved = existing
        ? await reportsApi.update(existing.id, definition)
        : await reportsApi.create(definition);
      startedAs.current = JSON.stringify(definition);
      return saved;
    } catch (err: any) {
      toast.error('Could not save the report', err.message);
      return null;
    } finally {
      setSaving(false);
    }
  };

  const save = async () => {
    const saved = await persist();
    if (saved) {
      toast.success('Report saved', saved.schedule_text || undefined);
      onSaved(saved);
    }
  };

  const sendTest = existing ? async () => {
    if (dirty) {
      toast.info('Save first', 'A test sends the saved report.');
      return;
    }
    setSendingTest(true);
    try {
      const delivery = await reportsApi.send(existing.id, true);
      if (delivery.status === 'delivered') toast.success('Test sent', delivery.detail);
      else toast.error('The test could not be delivered', delivery.detail);
    } catch (err: any) {
      toast.error('The test could not be sent', err.message);
    } finally {
      setSendingTest(false);
    }
  } : undefined;

  const leave = () => {
    if (dirty && !window.confirm('Leave without saving your changes?')) return;
    onCancel();
  };

  useEffect(() => {
    if (!dirty) return undefined;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);

  const previewBody = (tools?: (s: ResultSection) => ReactNode) => {
    if (definition.sections.length === 0) {
      return <p className="py-16 text-center text-sm text-slate-500">Add a section and the report appears here, with your real numbers.</p>;
    }
    if (previewError) return <p className="py-10 text-center text-sm text-rose-600" role="alert">{previewError}</p>;
    if (!result) return <p className="py-16 text-center text-sm text-slate-500">Working out the numbers…</p>;
    return <ReportDocument result={result} sectionTools={tools} />;
  };

  const preview = (
    <section className="rounded-xl border border-slate-200 bg-white shadow-sm">
      <div className="flex items-center justify-between border-b border-slate-100 px-4 py-2.5">
        <h3 className="text-sm font-bold text-slate-800">Preview</h3>
        <span className="text-[11px] text-slate-500" aria-live="polite">{previewing ? 'Updating…' : 'Your real numbers'}</span>
      </div>
      <div className="max-h-[calc(100vh-190px)] overflow-y-auto p-4">{previewBody()}</div>
    </section>
  );

  const shared: LayoutProps = {
    definition, update, catalog, pickers, problems: shownProblems, preview, sendTest, sendingTest, result, previewBody,
  };

  return (
    <div className="space-y-4" data-testid="report-designer">
      <div className="flex flex-wrap items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 shadow-sm">
        <button type="button" onClick={leave} className="text-xs font-semibold text-slate-500 hover:text-slate-800">← Reports</button>
        <input
          className={`${INPUT} max-w-sm font-semibold`}
          aria-label="Report name"
          placeholder="Name this report"
          maxLength={120}
          value={definition.name}
          onChange={(e) => update({ name: e.target.value })}
        />
        <input
          className={`${INPUT} max-w-md flex-1`}
          aria-label="Description"
          placeholder="What it is for (optional)"
          maxLength={500}
          value={definition.description}
          onChange={(e) => update({ description: e.target.value })}
        />
        <div className="ml-auto flex items-center gap-2">
          <LayoutSwitch value={layout} onChange={chooseLayout} />
          <button type="button" onClick={leave} className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 hover:border-slate-300">Cancel</button>
          <button type="button" disabled={saving} onClick={save} className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs font-bold text-white hover:bg-slate-700 disabled:opacity-50">{saving ? 'Saving…' : 'Save report'}</button>
        </div>
      </div>
      <ProblemList problems={shownProblems} areas={['name']} />
      {layout === 'split' && <SplitLayout {...shared} />}
      {layout === 'steps' && <StepsLayout {...shared} />}
      {layout === 'document' && <DocumentLayout {...shared} />}
    </div>
  );
}
