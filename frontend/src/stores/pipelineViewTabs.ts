/**
 * The Pipelines page's tabs: Home (the list of every pipeline) and the
 * pipelines opened from the list, each shown in full on that page.
 *
 * These are not the Editor's tabs. The Editor holds only the pipelines being
 * built there (the tab block in stores/workflowStore.ts); looking at a
 * pipeline here does not put it in the Editor. Edit / Edit in Editor does.
 */
import { create } from 'zustand';

import { toast } from '../components/Toast';
import { tabsOwner } from './workflowStore';

export interface ViewTab {
  id: string;
  name: string;
}

/** How many pipelines the Pipelines page keeps open at once, besides Home. */
export const MAX_VIEW_TABS = 10;
const STORAGE_KEY = 'revoluzen_flow_pipeline_view_tabs_v1';

interface ViewTabsState {
  tabs: ViewTab[];
  /** A tab for pipeline `id`: a new one at the end, or the one it has (named
   *  anew). False when ten are open. */
  open: (id: string, name: string) => boolean;
  close: (id: string) => void;
  /** Keep a tab's label in step with a renamed pipeline. */
  rename: (id: string, name: string) => void;
}

// The saved list is written only once it has been read back, so the empty
// list at start-up never wipes it.
let restored = false;
let lastSaved = '';

export const usePipelineViewTabs = create<ViewTabsState>((set, get) => ({
  tabs: [],
  open: (id, name) => {
    restoreViewTabs();
    const tabs = get().tabs;
    const existing = tabs.find((t) => t.id === id);
    if (existing) {
      if (name && existing.name !== name) set({ tabs: tabs.map((t) => (t.id === id ? { ...t, name } : t)) });
      return true;
    }
    if (tabs.length >= MAX_VIEW_TABS) {
      toast.warning('Ten pipelines are open', 'Close one of the tabs to open another.');
      return false;
    }
    set({ tabs: [...tabs, { id, name: name || 'Pipeline' }] });
    return true;
  },
  close: (id) => set((s) => ({ tabs: s.tabs.filter((t) => t.id !== id) })),
  rename: (id, name) => set((s) => (
    name && s.tabs.some((t) => t.id === id && t.name !== name)
      ? { tabs: s.tabs.map((t) => (t.id === id ? { ...t, name } : t)) }
      : s
  )),
}));

/** Bring back the tabs that were open before a reload, for the same user in
 *  the same workspace. Runs once a user is signed in; later calls do nothing. */
export function restoreViewTabs() {
  if (restored) return;
  const owner = tabsOwner();
  if (!owner) return;
  restored = true;
  let raw: any = null;
  try {
    raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
  } catch {
    raw = null;
  }
  if (!raw || raw.owner !== owner || !Array.isArray(raw.tabs)) return;
  const now = usePipelineViewTabs.getState().tabs;
  const have = new Set(now.map((t) => t.id));
  const saved: ViewTab[] = raw.tabs
    .filter((t: any) => t && typeof t.id === 'string' && t.id && !have.has(t.id))
    .map((t: any) => ({ id: t.id, name: String(t.name || 'Pipeline') }));
  if (!saved.length) return;
  usePipelineViewTabs.setState({ tabs: [...saved, ...now].slice(0, MAX_VIEW_TABS) });
}

/** Sign-out: forget the saved tabs, so the next person to sign in on this
 *  browser does not see them. */
export function forgetViewTabs() {
  restored = false;
  lastSaved = '';
  try { localStorage.removeItem(STORAGE_KEY); } catch { /* storage unavailable */ }
}

usePipelineViewTabs.subscribe((s) => {
  if (!restored) return;
  const owner = tabsOwner();
  if (!owner) return;
  const json = JSON.stringify({ owner, tabs: s.tabs });
  if (json === lastSaved) return;
  lastSaved = json;
  try {
    if (s.tabs.length) localStorage.setItem(STORAGE_KEY, json);
    else localStorage.removeItem(STORAGE_KEY);
  } catch { /* storage unavailable */ }
});
