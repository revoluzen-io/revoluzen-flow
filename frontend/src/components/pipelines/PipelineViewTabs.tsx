/**
 * The Pipelines page's tabs, above the list and beside the project panel:
 * Home (the list of every pipeline) first, then one tab per pipeline opened
 * from the list, each showing that pipeline in full on this page. Open, at
 * the right end, picks another pipeline to show; New Pipeline is in the page
 * header above.
 *
 * They are not the Editor's tabs. The Editor holds only the pipelines being
 * built there (components/editor/OpenPipelineTabs.tsx); looking at a
 * pipeline here leaves the Editor as it is, and Edit in Editor is what puts
 * it there. Nothing is edited in these tabs, so closing one never asks.
 */
import { useEffect, useRef, useState } from 'react';

import { readHashQuery } from '../../router';
import { MAX_VIEW_TABS, restoreViewTabs, usePipelineViewTabs, type ViewTab } from '../../stores/pipelineViewTabs';
import {
  ChevronGlyph,
  CloseGlyph,
  FolderOpenGlyph,
  MORE_BUTTON_CLASS,
  OPEN_BUTTON_CLASS,
  OpenPipelineMenu,
  STRIP_CLASS,
  TAB_SLOT,
  TabGlyph,
  tabClass,
} from '../editor/OpenPipelineTabs';

/** Room kept for Home, "N more" and Open. */
const BUTTONS_SLOT = 280;

/** The address of the list (no id) or of a pipeline's tab, keeping the
 *  project or folder picked in the panel, so Home comes back to it. */
export function pipelinesAddress(id?: string | null): string {
  const query = readHashQuery();
  delete query.new;
  const qs = new URLSearchParams(query).toString();
  return `#pipelines${id ? `/${id}` : ''}${qs ? `?${qs}` : ''}`;
}

function HomeGlyph() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-slate-600 shrink-0" aria-hidden="true">
      <path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z" />
    </svg>
  );
}

export default function PipelineViewTabs({
  shownId,
}: {
  /** The pipeline shown in full; null while Home (the list) is shown. */
  shownId: string | null;
}) {
  const tabs = usePipelineViewTabs((s) => s.tabs);

  // Tabs open before a reload come back.
  useEffect(() => { restoreViewTabs(); }, []);

  const stripRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = stripRef.current;
    if (!el) return;
    const update = () => setWidth(el.clientWidth);
    update();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
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

  const homeOn = !shownId;
  const fit = width > 0 ? Math.max(1, Math.floor((width - BUTTONS_SLOT) / TAB_SLOT)) : tabs.length;
  let visible = tabs.slice(0, fit);
  // The pipeline shown always has its tab in view, last if it has to be.
  if (shownId && !visible.some((t) => t.id === shownId)) {
    const shown = tabs.find((t) => t.id === shownId);
    if (shown) visible = [...visible.slice(0, Math.max(0, fit - 1)), shown];
  }
  const hidden = tabs.filter((t) => !visible.includes(t));
  const full = tabs.length >= MAX_VIEW_TABS;
  const openIds = new Set(tabs.map((t) => t.id));

  const go = (id: string | null) => {
    setMenu(null);
    window.location.hash = pipelinesAddress(id);
  };

  // Closing the pipeline shown brings its neighbour forward, or Home.
  const close = (t: ViewTab) => {
    setMenu(null);
    const idx = tabs.findIndex((x) => x.id === t.id);
    const rest = tabs.filter((x) => x.id !== t.id);
    usePipelineViewTabs.getState().close(t.id);
    if (t.id === shownId) {
      const next = rest[idx] || rest[idx - 1] || null;
      go(next ? next.id : null);
    }
  };

  return (
    <div ref={stripRef} data-testid="pipeline-view-tabs" className={STRIP_CLASS}>
      <div role="tablist" aria-label="Pipelines" className="flex items-end gap-0.5 min-w-0">
        <div role="presentation" title="All pipelines" className={tabClass(homeOn)}>
          <button
            type="button"
            role="tab"
            aria-selected={homeOn}
            onClick={() => go(null)}
            className="flex items-center gap-1.5 h-full pr-1.5 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 rounded"
          >
            <HomeGlyph />
            <span>Home</span>
          </button>
        </div>
        {visible.map((t) => {
          const on = t.id === shownId;
          return (
            <div key={t.id} role="presentation" title={t.name} className={tabClass(on)}>
              <button
                type="button"
                role="tab"
                aria-selected={on}
                onClick={() => go(t.id)}
                onAuxClick={(e) => { if (e.button === 1) { e.preventDefault(); close(t); } }}
                className="flex items-center gap-1.5 min-w-0 h-full focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 rounded"
              >
                <TabGlyph />
                <span className="truncate">{t.name}</span>
              </button>
              <button
                type="button"
                aria-label={`Close ${t.name}`}
                onClick={() => close(t)}
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
              {hidden.map((t) => (
                <div key={t.id} className="flex items-center gap-1 rounded-md hover:bg-slate-100">
                  <button
                    type="button"
                    role="menuitem"
                    title={t.name}
                    onClick={() => go(t.id)}
                    className="flex-1 min-w-0 flex items-center gap-2 px-2 py-1.5 text-[13px] text-slate-700 text-left"
                  >
                    <TabGlyph />
                    <span className="truncate">{t.name}</span>
                  </button>
                  <button
                    type="button"
                    aria-label={`Close ${t.name}`}
                    onClick={() => close(t)}
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

      {/* Open, at the right end as in the Editor's tabs. */}
      <div className="relative ml-auto self-center pl-2 shrink-0">
        <button
          type="button"
          aria-label="Open a pipeline"
          aria-haspopup="menu"
          aria-expanded={menu === 'open'}
          disabled={full}
          title={full ? `${MAX_VIEW_TABS} pipelines are open. Close one to open another.` : 'Show another pipeline in a tab of its own'}
          onClick={() => setMenu(menu === 'open' ? null : 'open')}
          className={OPEN_BUTTON_CLASS}
        >
          <FolderOpenGlyph />
          Open
          <ChevronGlyph />
        </button>
        {menu === 'open' && <OpenPipelineMenu excludeIds={openIds} onPick={go} />}
      </div>
    </div>
  );
}
