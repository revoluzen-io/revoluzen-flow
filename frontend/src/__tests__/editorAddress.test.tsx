/**
 * The editor's address names the pipeline on screen (#editor/<id>). A link
 * from another page, a bookmark or a reload opens that pipeline in its tab,
 * once someone is signed in; the sign-in page fetches nothing for it. A
 * pipeline that cannot be opened is said so plainly, and the editor shows a
 * tab of its own instead of an empty canvas posing as that pipeline. The
 * address then names what is on screen.
 *
 * `editorPage(on)` stands in for App: the hook is on while the editor page
 * shows and someone is signed in. One test renders App itself, signed out.
 */
import { StrictMode, useEffect } from 'react';
import { render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import { toast } from '../components/Toast';
import { useEditorAddress } from '../components/editor/useEditorAddress';
import { describeTab, findPipelineTab, useWorkflowStore } from '../stores/workflowStore';

const store = () => useWorkflowStore.getState();
const tabNames = () => store().openTabs.map((t) => describeTab(t, store()).name);

const NAMES: Record<string, string> = { p1: 'Orders', p2: 'Customers', p7: 'Invoices' };
const NOT_HERE = 'It is not in the workspace you are in. It may have been deleted, or it belongs to another workspace.';

function pipeline(id: string) {
  return {
    version: 1,
    workflow: {
      id,
      name: NAMES[id] || id,
      project_id: 'sales',
      status: 'draft',
      steps: [{ id: 'read', type: 'source', label: 'read', params: {} }],
      connections: [],
    },
  };
}

/** What the API client throws for a pipeline that is not in this workspace. */
function notFound() {
  return Object.assign(new Error('Workflow not found'), { status: 404 });
}

function serves(...ids: string[]) {
  return vi.spyOn(api, 'getWorkflow').mockImplementation(async (id: string) => {
    if (!ids.includes(id)) throw notFound();
    return pipeline(id) as never;
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}

function editorPage(on: boolean, { strict = false } = {}) {
  return renderHook(({ active }) => useEditorAddress(active), {
    initialProps: { active: on },
    wrapper: strict ? StrictMode : undefined,
  });
}

function goTo(hash: string) {
  window.history.replaceState(null, '', `/${hash}`);
}

/** A saved pipeline's tab, as one comes back after a reload: fetched when picked. */
function keptTab(workflowId: string) {
  useWorkflowStore.setState({ openTabs: [{ key: 'kept', workflowId, name: NAMES[workflowId], doc: null, needsLoad: true }] });
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  // Tabs off screen first, so closing the last one brings nothing forward.
  const s = store();
  for (const t of s.openTabs) if (t.key !== s.activeTabKey) store().closeTab(t.key);
  const active = store().activeTabKey;
  if (active) store().closeTab(active);
  store().resetWorkflow();
  useWorkflowStore.setState({ openTabs: [], activeTabKey: null });
  goTo('');
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('a link to a pipeline', () => {
  it('opens that pipeline in a tab when the app is loaded on it', async () => {
    const get = serves('p7');
    goTo('#editor/p7');
    editorPage(true);
    await waitFor(() => expect(store().workflowId).toBe('p7'));
    expect(get).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledWith('p7');
    expect(tabNames()).toEqual(['Invoices']);
    expect(store().nodes.map((n) => n.id)).toEqual(['read']);
    expect(window.location.hash).toBe('#editor/p7');
  });

  it('opens it from a link on another page, beside the pipelines already open', async () => {
    serves('p1', 'p2');
    store().openInEditor(pipeline('p1'));
    goTo('#storage');
    const page = editorPage(false); // the Storage page is on screen
    window.location.hash = '#editor/p2'; // its link is clicked; App switches page
    page.rerender({ active: true });
    await waitFor(() => expect(store().workflowId).toBe('p2'));
    expect(tabNames()).toEqual(['Orders', 'Customers']);
    expect(window.location.hash).toBe('#editor/p2');
  });

  it('goes to the tab of a pipeline that is open already, without fetching it again', async () => {
    const get = serves('p1', 'p2');
    store().openInEditor(pipeline('p1'));
    store().openInEditor(pipeline('p2'));
    goTo('#editor/p1');
    editorPage(true);
    await waitFor(() => expect(store().workflowId).toBe('p1'));
    expect(get).not.toHaveBeenCalled();
    expect(store().openTabs).toHaveLength(2);
  });

  it('follows a link opened while the editor is on screen', async () => {
    serves('p1', 'p2');
    goTo('#editor/p1');
    editorPage(true);
    await waitFor(() => expect(store().workflowId).toBe('p1'));
    window.location.hash = '#editor/p2';
    await waitFor(() => expect(store().workflowId).toBe('p2'));
    expect(tabNames()).toEqual(['Orders', 'Customers']);
  });

  it('opens it once when React mounts the editor twice, as it does in development', async () => {
    const get = serves('p7');
    goTo('#editor/p7');
    let mounts = 0;
    renderHook(() => {
      useEffect(() => { mounts += 1; }, []);
      useEditorAddress(true);
    }, { wrapper: StrictMode });
    await waitFor(() => expect(store().workflowId).toBe('p7'));
    expect(mounts).toBe(2); // the effects did run twice
    expect(get).toHaveBeenCalledTimes(1);
    expect(tabNames()).toEqual(['Invoices']);
  });

  it('ends on the link followed last, even when an earlier one finishes loading after it', async () => {
    const slow = deferred<unknown>();
    vi.spyOn(api, 'getWorkflow').mockImplementation(async (id: string) => {
      if (id === 'p1') return slow.promise as never;
      return pipeline(id) as never;
    });
    goTo('#editor/p1');
    editorPage(true);
    window.location.hash = '#editor/p2';
    await waitFor(() => expect(store().workflowId).toBe('p2'));
    slow.resolve(pipeline('p1'));
    await settle();
    expect(store().workflowId).toBe('p2');
    expect(tabNames()).toEqual(['Customers']);
    expect(window.location.hash).toBe('#editor/p2');
  });

  it('an open still loading when the editor was left writes no address once the editor is back', async () => {
    const first = deferred<unknown>();
    const second = deferred<unknown>();
    vi.spyOn(api, 'getWorkflow').mockImplementation(async (id: string) => {
      if (id === 'p1') return first.promise as never;
      if (id === 'p2') return second.promise as never;
      return pipeline(id) as never;
    });
    store().openInEditor(pipeline('p7')); // on screen
    goTo('#editor/p1');
    const page = editorPage(true);
    goTo('#pipelines');
    page.rerender({ active: false }); // the editor is left while Orders loads
    goTo('#editor/p2');
    page.rerender({ active: true }); // and entered again, on another link
    first.resolve(pipeline('p1'));
    await settle();
    expect(window.location.hash).toBe('#editor/p2'); // not the tab still on screen
    second.resolve(pipeline('p2'));
    await waitFor(() => expect(store().workflowId).toBe('p2'));
    expect(window.location.hash).toBe('#editor/p2');
  });
});

describe('signed out', () => {
  it('fetches nothing and says nothing until someone signs in, then opens the link', async () => {
    const get = serves('p7');
    const error = vi.spyOn(toast, 'error');
    goTo('#editor/p7');
    const page = editorPage(false);
    await settle();
    expect(get).not.toHaveBeenCalled();
    expect(error).not.toHaveBeenCalled();
    page.rerender({ active: true }); // signed in
    await waitFor(() => expect(store().workflowId).toBe('p7'));
    expect(error).not.toHaveBeenCalled();
    expect(window.location.hash).toBe('#editor/p7');
  });

  it('the sign-in page shows no message about the pipeline in the address and keeps the address', async () => {
    const get = serves('p7');
    const error = vi.spyOn(toast, 'error');
    // Whatever else the app asks the server while signed out gets an empty answer.
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 200 })));
    goTo('#editor/p7');
    const { default: App } = await import('../App');
    const view = render(<App />);
    await screen.findByLabelText(/^email/i);
    await settle();
    expect(get).not.toHaveBeenCalled();
    expect(error).not.toHaveBeenCalled();
    expect(window.location.hash).toBe('#editor/p7');
    view.unmount();
  }, 60_000); // the first import of the whole app takes a while
});

describe('a link to a pipeline that cannot be opened', () => {
  it('says so plainly and shows an empty canvas in a tab of its own, not posing as that pipeline', async () => {
    serves();
    const error = vi.spyOn(toast, 'error');
    goTo('#editor/somewhere-else');
    editorPage(true);
    await waitFor(() => expect(window.location.hash).toBe('#editor'));
    expect(error).toHaveBeenCalledTimes(1);
    expect(error).toHaveBeenCalledWith('Could not open the pipeline', NOT_HERE);
    expect(store().openTabs).toHaveLength(1);
    expect(store().activeTabKey).toBe(store().openTabs[0].key);
    expect(store().workflowId).toBeNull();
    expect(store().nodes).toHaveLength(0);
  });

  it('says it once when React mounts the editor twice', async () => {
    const get = serves();
    const error = vi.spyOn(toast, 'error');
    goTo('#editor/somewhere-else');
    editorPage(true, { strict: true });
    await waitFor(() => expect(window.location.hash).toBe('#editor'));
    expect(get).toHaveBeenCalledTimes(1);
    expect(error).toHaveBeenCalledTimes(1);
    expect(store().openTabs).toHaveLength(1);
  });

  it('leaves the pipeline on screen as it is, and the address names it again', async () => {
    serves('p1');
    const error = vi.spyOn(toast, 'error');
    store().openInEditor(pipeline('p1'));
    goTo('#storage');
    const page = editorPage(false);
    window.location.hash = '#editor/gone';
    page.rerender({ active: true });
    await waitFor(() => expect(window.location.hash).toBe('#editor/p1'));
    expect(error).toHaveBeenCalledTimes(1);
    expect(store().workflowId).toBe('p1');
    expect(tabNames()).toEqual(['Orders']);
  });

  it('goes back to the tab used last when the app is loaded on it', async () => {
    serves('p1');
    const error = vi.spyOn(toast, 'error');
    keptTab('p1');
    goTo('#editor/gone');
    editorPage(true);
    await waitFor(() => expect(window.location.hash).toBe('#editor/p1'));
    expect(error).toHaveBeenCalledTimes(1);
    expect(error).toHaveBeenCalledWith('Could not open the pipeline', NOT_HERE);
    expect(store().workflowId).toBe('p1');
    expect(tabNames()).toEqual(['Orders']);
  });

  it('and to an empty canvas in a tab of its own when the pipeline of that tab is gone too', async () => {
    serves();
    const error = vi.spyOn(toast, 'error');
    keptTab('p2');
    goTo('#editor/gone');
    editorPage(true);
    await waitFor(() => expect(window.location.hash).toBe('#editor'));
    expect(error.mock.calls).toEqual([
      ['Could not open the pipeline', NOT_HERE],
      ['Could not open "Customers"', NOT_HERE],
    ]);
    expect(store().openTabs).toHaveLength(1);
    expect(store().activeTabKey).toBe(store().openTabs[0].key);
    expect(store().workflowId).toBeNull();
  });

  it('a tab kept from before whose pipeline is gone drops out and is named in the message', async () => {
    serves();
    const error = vi.spyOn(toast, 'error');
    keptTab('p2');
    goTo('#editor/p2');
    editorPage(true);
    await waitFor(() => expect(window.location.hash).toBe('#editor'));
    expect(error).toHaveBeenCalledWith('Could not open "Customers"', NOT_HERE);
    expect(findPipelineTab(store(), 'p2')).toBeUndefined();
    expect(store().openTabs).toHaveLength(1);
    expect(store().workflowId).toBeNull();
  });
});

describe('the address follows the editor', () => {
  it('names the tab picked, and drops the id for a pipeline not saved yet', async () => {
    serves('p1', 'p2');
    goTo('#editor/p2');
    editorPage(true);
    await waitFor(() => expect(store().workflowId).toBe('p2'));
    store().openInEditor(pipeline('p1'));
    expect(window.location.hash).toBe('#editor/p1');
    store().newTab();
    expect(window.location.hash).toBe('#editor');
  });

  it('stops following once the editor is left', async () => {
    serves('p1', 'p2');
    goTo('#editor/p1');
    const page = editorPage(true);
    await waitFor(() => expect(store().workflowId).toBe('p1'));
    goTo('#pipelines');
    page.rerender({ active: false });
    store().openInEditor(pipeline('p2'));
    expect(window.location.hash).toBe('#pipelines');
  });
});

describe('entering the editor twice at once', () => {
  it('ends on the tab used before the reload, fetched once', async () => {
    localStorage.setItem('revoluzen_flow_user', JSON.stringify({ id: 'u1' }));
    localStorage.setItem('revoluzen_flow_editor_tabs_v1', JSON.stringify({
      owner: 'u1::default',
      tabs: [{ workflowId: 'p1', name: 'Orders' }, { workflowId: 'p2', name: 'Customers' }],
      active: 'p1',
    }));
    vi.resetModules();
    const fresh = await import('../stores/workflowStore');
    const { api: freshApi } = await import('../api/client');
    const get = vi.spyOn(freshApi, 'getWorkflow').mockImplementation(async (id: string) => pipeline(id) as never);
    const { ensureEditorTab } = fresh.useWorkflowStore.getState();
    await Promise.all([ensureEditorTab(null), ensureEditorTab(null)]);
    expect(fresh.useWorkflowStore.getState().workflowId).toBe('p1');
    expect(get).toHaveBeenCalledTimes(1);
  });
});

describe('what the server answered', () => {
  it('goes with the error, so a pipeline that is not there reads plainly', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'Workflow not found' }), { status: 404 })));
    await expect(api.getWorkflow('gone')).rejects.toMatchObject({ status: 404, message: 'Workflow not found' });
  });
});
