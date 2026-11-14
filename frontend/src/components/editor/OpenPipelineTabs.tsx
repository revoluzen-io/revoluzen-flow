/**
 * The Editor's tabs: one per pipeline being built here. They sit above the
 * canvas, beside the Nodes panel, the way the Pipelines page's tabs sit
 * beside its project panel, and end with Open and New Pipeline, always at the
 * right end of the strip.
 *
 * The editor shows one pipeline at a time. The others wait in their tabs with
 * their canvas, results and unsaved edits as they were left, and a run keeps
 * going in a tab that is not on screen (see the tab notes in
 * stores/workflowStore.ts). Only pipelines opened in the Editor are here:
 * looking at a pipeline on the Pipelines page does not add it (that page has
 * its own tabs, components/pipelines/PipelineViewTabs.tsx).
 *
 * Only the tabs idea comes from Microsoft Fabric. The look follows Flow's
 * own toolbar.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  useWorkflowStore,
  describeTab,
  restoreSavedTabs,
  MAX_EDITOR_TABS,
  type TabView,
} from '../../stores/workflowStore';
import { api } from '../../api/client';
import { useCan } from '../../auth/RoleGate';
import { navigateTo, navigateToSubRoute } from '../../router';
import { toast } from '../Toast';
import UnsavedChangesModal from '../UnsavedChangesModal';

/** Room a tab takes when working out how many fit; the rest go in "N more". */
export const TAB_SLOT = 188;
/** Room kept for "N more", Open and New Pipeline. */
const BUTTONS_SLOT = 330;

/** The strip, shared with the Pipelines page's tabs. */
export const STRIP_CLASS = 'relative z-20 flex items-end gap-0.5 px-2.5 pt-1.5 min-h-[39px] bg-slate-200 border-b border-slate-300 shrink-0';

/** One tab, shared with the Pipelines page's tabs. A tab narrows before the
 *  buttons at the end of the strip are pushed out of reach. */
export const tabClass = (on: boolean) => `group flex items-center gap-1 h-8 pl-2.5 pr-1 rounded-t-md text-[12.5px] font-semibold min-w-[64px] max-w-[196px] ${
  on
    ? 'bg-white text-slate-900 shadow-[inset_0_2px_0_#3b82f6]'
    : 'bg-slate-300 text-slate-700 hover:bg-[#dbe3ec]'
}`;

/** "N more", which lists the tabs without room in the strip. */
export const MORE_BUTTON_CLASS = 'inline-flex items-center gap-1 h-7 px-2 text-xs font-semibold text-slate-700 rounded-md hover:bg-slate-300 whitespace-nowrap';

/** Open, which picks a saved pipeline for a tab of its own. */
export const OPEN_BUTTON_CLASS = 'inline-flex items-center gap-1.5 h-[26px] px-2.5 text-xs font-semibold text-slate-700 bg-white border border-slate-300 rounded-lg shadow-sm hover:bg-slate-50 hover:border-slate-400 whitespace-nowrap disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-white disabled:hover:border-slate-300';

function tabTitle(v: TabView): string {
  const bits = [v.name];
  if (v.pastRunId) bits.push('a past run, as it ran');
  else if (!v.workflowId) bits.push('not saved yet');
  if (v.isRunning) bits.push('running');
  if (v.isDirty && !v.pastRunId) bits.push('unsaved changes');
  return bits.join(' · ');
}

export function TabGlyph({ past = false }: { past?: boolean }) {
  if (past) {
    return (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-slate-500 shrink-0" aria-hidden="true">
        <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
        <path d="M3 3v5h5" />
        <path d="M12 7v5l3 2" />
      </svg>
    );
  }
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-indigo-500 shrink-0" aria-hidden="true">
      <circle cx="5" cy="6" r="2.5" />
      <circle cx="5" cy="18" r="2.5" />
      <circle cx="19" cy="12" r="2.5" />
      <path d="M7.5 6h3a3 3 0 0 1 3 3v0a3 3 0 0 0 3 3h0M7.5 18h3a3 3 0 0 0 3-3v0a3 3 0 0 1 3-3" />
    </svg>
  );
}

export function CloseGlyph() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" aria-hidden="true">
      <path d="M18 6 6 18M6 6l12 12" />
    </svg>
  );
}

export function ChevronGlyph() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="shrink-0" aria-hidden="true">
      <path d="m6 9 6 6 6-6" />
    </svg>
  );
}

export function FolderOpenGlyph() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-slate-500 shrink-0" aria-hidden="true">
      <path d="M3 19V6a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v2" />
      <path d="M3 19 5.7 11.4A2 2 0 0 1 7.6 10H21l-2.8 7.6A2 2 0 0 1 16.3 19z" />
    </svg>
  );
}

/**
 * The Open menu, shared with the Pipelines page's tabs: the saved pipelines
 * without a tab yet, newest first, with a search box. It is fetched each time
 * it opens, so it lists what is there now.
 */
export function OpenPipelineMenu({
  excludeIds,
  onPick,
}: {
  /** Pipelines that already have a tab. */
  excludeIds: Set<string>;
  onPick: (id: string) => void;
}) {
  const [allPipelines, setAllPipelines] = useState<any[] | null>(null);
  const [query, setQuery] = useState('');
  useEffect(() => {
    let cancelled = false;
    api.listWorkflows()
      .then((list) => { if (!cancelled) setAllPipelines(Array.isArray(list) ? list : []); })
      .catch(() => { if (!cancelled) setAllPipelines([]); });
    return () => { cancelled = true; };
  }, []);

  const q = query.trim().toLowerCase();
  const matches = (allPipelines || [])
    .filter((p) => p && p.id && !excludeIds.has(p.id) && (!q || String(p.name || '').toLowerCase().includes(q)))
    .sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')))
    .slice(0, 8);

  return (
    <div role="menu" aria-label="Open a pipeline" className="absolute top-full mt-1.5 right-0 z-50 w-72 bg-white border border-slate-200 rounded-lg shadow-xl p-1.5">
      <label className="flex items-center gap-2 px-2 h-8 mb-1 rounded-md border border-slate-200 focus-within:border-blue-400">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" className="text-slate-400 shrink-0" aria-hidden="true">
          <circle cx="11" cy="11" r="7" />
          <path d="m20 20-3.5-3.5" />
        </svg>
        <input
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Find a pipeline…"
          aria-label="Find a pipeline"
          className="flex-1 min-w-0 text-[13px] bg-transparent outline-none placeholder:text-slate-400"
        />
      </label>
      <div className="max-h-64 overflow-auto">
        {allPipelines === null ? (
          <div className="px-2 py-2 text-xs text-slate-500">Loading pipelines…</div>
        ) : matches.length === 0 ? (
          <div className="px-2 py-2 text-xs text-slate-500">
            {q ? 'No pipeline matches.' : allPipelines.length === 0 ? 'No saved pipelines yet.' : 'Every pipeline is open already.'}
          </div>
        ) : (
          matches.map((p) => (
            <button
              key={p.id}
              type="button"
              role="menuitem"
              onClick={() => onPick(p.id)}
              className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-[13px] text-slate-700 text-left hover:bg-slate-100"
            >
              <TabGlyph />
              <span className="truncate">{p.name || 'Untitled Pipeline'}</span>
            </button>
          ))
        )}
      </div>
    </div>
  );
}

export default function OpenPipelineTabs({
  newSeed,
  canCreate = true,
  className = '',
}: {
  /** Where New Pipeline puts the pipeline. Defaults to the project of the
   *  pipeline on screen. */
  newSeed?: { projectId: string | null; folderId: string | null };
  /** False where pipelines are not made (PROD): New Pipeline is left out. */
  canCreate?: boolean;
  className?: string;
}) {
  const openTabs = useWorkflowStore((s) => s.openTabs);
  const activeTabKey = useWorkflowStore((s) => s.activeTabKey);
  // The on-screen tab shows live values, so the strip follows them too.
  const liveName = useWorkflowStore((s) => s.workflowName);
  const liveId = useWorkflowStore((s) => s.workflowId);
  const liveDirty = useWorkflowStore((s) => s.isDirty);
  const liveRunning = useWorkflowStore((s) => s.isRunning);
  const livePast = useWorkflowStore((s) => s.pastRunId);

  const views = useMemo(() => {
    const s = useWorkflowStore.getState();
    return openTabs.map((t) => describeTab(t, s));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openTabs, activeTabKey, liveName, liveId, liveDirty, liveRunning, livePast]);

  // Pipelines open before a reload come back as tabs (fetched when picked).
  useEffect(() => { restoreSavedTabs(); }, []);

  const stripRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = stripRef.current;
    if (!el) return;
    const root = document.documentElement;
    const update = () => {
      setWidth(el.clientWidth);
      // The node settings panel opens below the toolbar and this strip
      // (.config-overlay in globals.css), so the tabs stay in reach.
      root.style.setProperty('--editor-tabs-height', `${el.offsetHeight}px`);
    };
    update();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);
    observer?.observe(el);
    return () => {
      observer?.disconnect();
      root.style.removeProperty('--editor-tabs-height');
    };
  }, []);

  const [menu, setMenu] = useState<'more' | 'open' | null>(null);
  useEffect(() => {
    if (!menu) return;
    const onDown = (e: MouseEvent) => {
      if (!stripRef.current?.contains(e.target as Node)) setMenu(null);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setMenu(null); };
    document.addEventListener('mousedown', onDown);
    window.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      window.removeEventListener('keydown', onKey);
    };
  }, [menu]);

  const markedKey = activeTabKey;
  const fit = width > 0 ? Math.max(1, Math.floor((width - BUTTONS_SLOT) / TAB_SLOT)) : views.length;
  let visible = views.slice(0, fit);
  // The tab on screen always shows, in the last place if it has to.
  if (markedKey && !visible.some((v) => v.key === markedKey)) {
    const marked = views.find((v) => v.key === markedKey);
    if (marked) visible = [...visible.slice(0, Math.max(0, fit - 1)), marked];
  }
  const hidden = views.filter((v) => !visible.includes(v));
  const full = views.length >= MAX_EDITOR_TABS;
  const allowNew = useCan('create') && canCreate;

  const openIds = new Set(views.filter((v) => v.workflowId && !v.pastRunId).map((v) => v.workflowId as string));

  // ── Picking, opening and closing ──
  const pick = async (v: TabView) => {
    setMenu(null);
    await useWorkflowStore.getState().activateTab(v.key);
  };

  const openPipeline = async (id: string) => {
    setMenu(null);
    try {
      const data = await api.getWorkflow(id);
      if (!useWorkflowStore.getState().openInEditor(data)) return;
      navigateToSubRoute('editor', id);
    } catch (err: any) {
      toast.error('Failed to open pipeline', err?.message || 'It may have been deleted.');
    }
  };

  // An empty canvas in a tab of its own (the empty one on screen, or a new
  // one), without leaving the Editor: its "Describe your pipeline" card
  // offers Use template, so templates stay a click away. The address follows
  // the tab on screen (App.tsx). With ten open, newTab says why it cannot.
  const newPipeline = () => {
    setMenu(null);
    const s = useWorkflowStore.getState();
    const seed = newSeed ?? { projectId: s.activeTabKey ? s.projectId : null, folderId: null };
    if (!s.newTab(seed)) return;
    useWorkflowStore.setState({ chatMessages: [] });
  };

  const [closing, setClosing] = useState<TabView | null>(null);
  const [saving, setSaving] = useState(false);

  // With the last tab closed, the editor hands back to the list.
  const afterClose = () => {
    if (useWorkflowStore.getState().openTabs.length === 0) navigateTo('pipelines');
  };

  const requestClose = (v: TabView) => {
    setMenu(null);
    // A past run is a copy of what ran; closing it loses nothing.
    if (v.isDirty && !v.pastRunId) {
      setClosing(v);
      return;
    }
    useWorkflowStore.getState().closeTab(v.key);
    afterClose();
  };

  const saveAndClose = async () => {
    const v = closing;
    if (!v) return;
    setSaving(true);
    try {
      const before = useWorkflowStore.getState().activeTabKey;
      // Saving works on the pipeline on screen, so the tab comes forward first.
      if (!(await useWorkflowStore.getState().activateTab(v.key))) {
        setClosing(null);
        return;
      }
      const id = await useWorkflowStore.getState().ensureWorkflow({ allowCreate: true });
      if (!id) return; // the name prompt was cancelled: the dialog stays
      toast.success('Pipeline saved');
      setClosing(null);
      useWorkflowStore.getState().closeTab(v.key);
      // Back to the tab that was on screen before, when it was another one.
      if (before && before !== v.key && useWorkflowStore.getState().openTabs.some((t) => t.key === before)) {
        await useWorkflowStore.getState().activateTab(before);
      }
      afterClose();
    } catch {
      toast.error('Save failed', 'Could not save the pipeline. Try again, or discard to close it without saving.');
    } finally {
      setSaving(false);
    }
  };

  const discard = () => {
    const v = closing;
    if (!v) return;
    setClosing(null);
    useWorkflowStore.getState().closeTab(v.key);
    afterClose();
  };

  return (
    // pointer-events-auto: in PROD the editor row is view only
    // (pointer-events-none), and switching between open pipelines still works.
    <div
      ref={stripRef}
      data-testid="open-pipeline-tabs"
      className={`${STRIP_CLASS} pointer-events-auto ${className}`}
    >
      <div role="tablist" aria-label="Pipelines open in the editor" className="flex items-end gap-0.5 min-w-0">
        {visible.map((v) => {
          const on = v.key === markedKey;
          return (
            <div key={v.key} role="presentation" title={tabTitle(v)} className={tabClass(on)}>
              <button
                type="button"
                role="tab"
                aria-selected={on}
                data-tab-key={v.key}
                onClick={() => void pick(v)}
                onAuxClick={(e) => { if (e.button === 1) { e.preventDefault(); requestClose(v); } }}
                className="flex items-center gap-1.5 min-w-0 h-full focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 rounded"
              >
                <TabGlyph past={!!v.pastRunId} />
                <span className="truncate">{v.name}</span>
                {v.isRunning && (
                  <span
                    className="w-[11px] h-[11px] rounded-full border-2 border-blue-200 border-t-blue-600 motion-safe:animate-spin shrink-0"
                    role="img"
                    aria-label="Running"
                  />
                )}
                {v.isDirty && !v.pastRunId && (
                  <span className="w-[7px] h-[7px] rounded-full bg-amber-500 shrink-0" role="img" aria-label="Unsaved changes" />
                )}
              </button>
              <button
                type="button"
                aria-label={`Close ${v.name}`}
                onClick={() => requestClose(v)}
                className="w-[18px] h-[18px] grid place-items-center rounded text-slate-500 hover:text-slate-800 hover:bg-slate-400/40 shrink-0"
              >
                <CloseGlyph />
              </button>
            </div>
          );
        })}
      </div>

      {hidden.length > 0 && (
        <div className="relative self-center shrink-0">
          <button
            type="button"
            aria-haspopup="menu"
            aria-expanded={menu === 'more'}
            onClick={() => setMenu(menu === 'more' ? null : 'more')}
            className={MORE_BUTTON_CLASS}
          >
            {hidden.length} more
            <ChevronGlyph />
          </button>
          {menu === 'more' && (
            <div role="menu" aria-label="More open pipelines" className="absolute top-full mt-1.5 left-0 z-50 w-64 max-h-80 overflow-auto bg-white border border-slate-200 rounded-lg shadow-xl p-1">
              {hidden.map((v) => (
                <div key={v.key} className="flex items-center gap-1 rounded-md hover:bg-slate-100">
                  <button
                    type="button"
                    role="menuitem"
                    title={tabTitle(v)}
                    onClick={() => void pick(v)}
                    className="flex-1 min-w-0 flex items-center gap-2 px-2 py-1.5 text-[13px] text-slate-700 text-left"
                  >
                    <TabGlyph past={!!v.pastRunId} />
                    <span className="truncate">{v.name}</span>
                    {v.isRunning && <span className="w-[10px] h-[10px] rounded-full border-2 border-blue-200 border-t-blue-600 motion-safe:animate-spin shrink-0" role="img" aria-label="Running" />}
                    {v.isDirty && !v.pastRunId && <span className="w-[7px] h-[7px] rounded-full bg-amber-500 shrink-0" role="img" aria-label="Unsaved changes" />}
                  </button>
                  <button
                    type="button"
                    aria-label={`Close ${v.name}`}
                    onClick={() => requestClose(v)}
                    className="w-6 h-6 mr-1 grid place-items-center rounded text-slate-400 hover:text-slate-700 hover:bg-slate-200 shrink-0"
                  >
                    <CloseGlyph />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Open and New Pipeline: at the right end, in the same place however
          many tabs are open. */}
      <div className="ml-auto self-center flex items-center gap-1.5 pl-2 shrink-0">
        <div className="relative">
          <button
            type="button"
            aria-label="Open a pipeline"
            aria-haspopup="menu"
            aria-expanded={menu === 'open'}
            disabled={full}
            title={full ? `${MAX_EDITOR_TABS} pipelines are open. Close one to open another.` : 'Open a saved pipeline in a tab of its own'}
            onClick={() => setMenu(menu === 'open' ? null : 'open')}
            className={OPEN_BUTTON_CLASS}
          >
            <FolderOpenGlyph />
            Open
            <ChevronGlyph />
          </button>
          {menu === 'open' && <OpenPipelineMenu excludeIds={openIds} onPick={(id) => void openPipeline(id)} />}
        </div>
        {allowNew && (
          <button
            type="button"
            onClick={newPipeline}
            title="Start a new pipeline on an empty canvas, in a tab of its own"
            className="inline-flex items-center gap-1.5 h-[26px] px-3 text-white text-xs font-bold rounded-lg shadow-sm hover:shadow-md transition-shadow whitespace-nowrap"
            style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" aria-hidden="true">
              <path d="M12 5v14M5 12h14" />
            </svg>
            New Pipeline
          </button>
        )}
      </div>

      {/* On document.body: the strip sits inside the canvas column's stacking
          context, which would put other panels above the dialog. */}
      {createPortal(
        <UnsavedChangesModal
          open={!!closing}
          dirty
          saving={saving}
          workflowName={closing?.name}
          saveActionLabel="Save & Close"
          onSaveAndClose={() => void saveAndClose()}
          onDiscard={discard}
          onCancel={() => setClosing(null)}
        />,
        document.body,
      )}
    </div>
  );
}
