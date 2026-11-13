/**
 * Open pipelines, one tab each. The editor shows one pipeline at a time and
 * every other open one waits in its tab as it was left: canvas, results and
 * unsaved edits. A run or a save that finishes after another tab was picked
 * belongs to the tab it started in.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import {
  MAX_EDITOR_TABS,
  describeTab,
  findPipelineTab,
  useWorkflowStore,
  type EditorTab,
} from '../stores/workflowStore';

const store = () => useWorkflowStore.getState();

function pipeline(id: string, name: string, steps: string[] = ['read']) {
  return {
    version: 1,
    workflow: {
      id,
      name,
      project_id: 'sales',
      folder_id: null,
      status: 'draft',
      steps: steps.map((s) => ({ id: s, type: 'source', label: s, params: {} })),
      connections: [],
    },
  };
}

const views = () => store().openTabs.map((t) => describeTab(t, store()));
const tabFor = (id: string): EditorTab => {
  const tab = findPipelineTab(store(), id);
  if (!tab) throw new Error(`no tab for ${id}`);
  return tab;
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}

function closeEverything() {
  for (const t of [...store().openTabs]) store().closeTab(t.key);
  store().resetWorkflow();
  useWorkflowStore.setState({ openTabs: [], activeTabKey: null, isRunning: false, chatMessages: [] });
}

beforeEach(() => {
  vi.restoreAllMocks();
  closeEverything();
});

describe('opening pipelines', () => {
  it('gives each pipeline a tab and keeps the others as they were left', async () => {
    store().openInEditor(pipeline('p1', 'Orders', ['read', 'write']));
    store().setWorkflowName('Orders v2'); // an edit nobody saved
    store().openInEditor(pipeline('p2', 'Customers'));

    expect(views().map((v) => v.name)).toEqual(['Orders v2', 'Customers']);
    expect(store().workflowId).toBe('p2');
    expect(store().nodes).toHaveLength(1);
    expect(views()[0].isDirty).toBe(true);

    await store().activateTab(tabFor('p1').key);
    expect(store().workflowId).toBe('p1');
    expect(store().workflowName).toBe('Orders v2');
    expect(store().isDirty).toBe(true);
    expect(store().nodes.map((n) => n.id)).toEqual(['read', 'write']);
  });

  it('goes to the tab of a pipeline that is open already, with its unsaved edits', () => {
    store().openInEditor(pipeline('p1', 'Orders'));
    store().setWorkflowName('Orders v2');
    store().openInEditor(pipeline('p2', 'Customers'));

    store().openInEditor(pipeline('p1', 'Orders'));
    expect(store().openTabs).toHaveLength(2);
    expect(store().workflowId).toBe('p1');
    expect(store().workflowName).toBe('Orders v2');
  });

  it('brings a clean open pipeline up to date when it is opened again', () => {
    store().openInEditor(pipeline('p2', 'Customers'));
    store().openInEditor(pipeline('p1', 'Orders'));
    store().openInEditor(pipeline('p2', 'Customers (renamed)'));
    expect(store().workflowName).toBe('Customers (renamed)');
    expect(store().openTabs).toHaveLength(2);
  });

  it('uses the empty canvas on screen instead of opening another tab', () => {
    store().newTab();
    store().openInEditor(pipeline('p1', 'Orders'));
    expect(store().openTabs).toHaveLength(1);
    expect(store().workflowId).toBe('p1');
  });

  it('takes the project and folder of a saved pipeline, and keeps a new one where it was started', () => {
    store().newTab({ projectId: 'ops', folderId: 'nightly' });
    expect(store().projectId).toBe('ops');
    expect(store().folderId).toBe('nightly');
    store().openInEditor(pipeline('p1', 'Orders'));
    expect(store().projectId).toBe('sales');
    expect(store().folderId).toBeNull();
  });

  it('puts a template on a new tab, never over a pipeline being edited', async () => {
    store().openInEditor(pipeline('p1', 'Orders', ['read', 'write']));
    await store().useTemplate('simple_etl');
    expect(store().openTabs).toHaveLength(2);
    expect(store().workflowId).toBeNull();
    const first = views()[0];
    expect(first.workflowId).toBe('p1');
    expect(store().openTabs[0].doc?.nodes.map((n) => n.id)).toEqual(['read', 'write']);
  });

  it(`opens ${MAX_EDITOR_TABS} at most`, () => {
    for (let i = 0; i < MAX_EDITOR_TABS; i += 1) {
      expect(store().openInEditor(pipeline(`p${i}`, `Pipeline ${i}`))).toBe(true);
    }
    expect(store().openInEditor(pipeline('one-more', 'One more'))).toBe(false);
    expect(store().newTab()).toBe(false);
    expect(store().openTabs).toHaveLength(MAX_EDITOR_TABS);
    expect(store().workflowId).toBe(`p${MAX_EDITOR_TABS - 1}`);
  });
});

describe('closing tabs', () => {
  it('brings the next tab forward, then the one before, then an empty canvas', async () => {
    store().openInEditor(pipeline('p1', 'One'));
    store().openInEditor(pipeline('p2', 'Two'));
    store().openInEditor(pipeline('p3', 'Three'));
    await store().activateTab(tabFor('p2').key);

    store().closeTab(tabFor('p2').key);
    expect(store().workflowId).toBe('p3');
    store().closeTab(tabFor('p3').key);
    expect(store().workflowId).toBe('p1');
    store().closeTab(tabFor('p1').key);
    expect(store().openTabs).toHaveLength(0);
    expect(store().workflowId).toBeNull();
    expect(store().nodes).toHaveLength(0);
  });

  it('closing a tab that is not on screen leaves the screen alone', () => {
    store().openInEditor(pipeline('p1', 'One'));
    store().openInEditor(pipeline('p2', 'Two'));
    store().closeTab(tabFor('p1').key);
    expect(store().workflowId).toBe('p2');
    expect(views().map((v) => v.name)).toEqual(['Two']);
  });
});

describe('work that finishes after another tab is picked', () => {
  it('a run lands in the tab it was started in', async () => {
    const answer = deferred<any>();
    vi.spyOn(api, 'runWorkflow').mockReturnValue(answer.promise);
    store().openInEditor(pipeline('p1', 'Orders', ['read', 'write']));
    const run = store().runWorkflow(true);
    expect(store().isRunning).toBe(true);

    store().openInEditor(pipeline('p2', 'Customers'));
    expect(store().isRunning).toBe(false);
    expect(views()[0].isRunning).toBe(true);

    const ok = (id: string) => ({ step_id: id, status: 'success', row_count: 3, columns: [], sample_data: [], schema_info: [], duration_ms: 1 });
    answer.resolve({ status: 'success', step_results: { read: ok('read'), write: ok('write') } });
    await run;

    // The screen still shows Customers, untouched.
    expect(store().workflowId).toBe('p2');
    expect(store().stepResults).toEqual({});
    // Orders has its results.
    const orders = store().openTabs[0].doc!;
    expect(orders.isRunning).toBe(false);
    expect(Object.keys(orders.stepResults).sort()).toEqual(['read', 'write']);
    expect(orders.nodes.map((n) => n.data.status)).toEqual(['success', 'success']);
  });

  it('a save marks only its own tab saved', async () => {
    const saved = deferred<any>();
    vi.spyOn(api, 'updateWorkflow').mockReturnValue(saved.promise);
    store().openInEditor(pipeline('p1', 'Orders'));
    store().setWorkflowName('Orders v2');
    const save = store().ensureWorkflow();

    store().openInEditor(pipeline('p2', 'Customers'));
    store().setWorkflowName('Customers v2');
    saved.resolve({});
    await expect(save).resolves.toBe('p1');

    expect(store().workflowId).toBe('p2');
    expect(store().isDirty).toBe(true);
    expect(store().openTabs[0].doc!.isDirty).toBe(false);
  });

  it('a run in a tab closed meanwhile goes nowhere', async () => {
    const answer = deferred<any>();
    vi.spyOn(api, 'runWorkflow').mockReturnValue(answer.promise);
    store().openInEditor(pipeline('p1', 'Orders'));
    const run = store().runWorkflow(true);
    store().openInEditor(pipeline('p2', 'Customers'));
    store().closeTab(tabFor('p1').key);
    answer.resolve({ status: 'success', step_results: { read: { step_id: 'read', status: 'success' } } });
    await run;
    expect(views().map((v) => v.name)).toEqual(['Customers']);
    expect(store().stepResults).toEqual({});
  });
});

describe('past runs and pipelines changed elsewhere', () => {
  const pastRun = {
    id: 'run-1',
    workflow_id: 'p1',
    started_at: '2026-11-01T10:00:00Z',
    workflow_snapshot: { id: 'p1', name: 'Orders', steps: [{ id: 'read', type: 'source', params: {} }], connections: [] },
    step_logs: [{ step_id: 'read', status: 'error', error_message: 'gone' }],
  };

  it('a past run opens in a tab of its own and is not taken for the pipeline', () => {
    store().openInEditor(pipeline('p1', 'Orders'));
    store().loadExecutionIntoEditor(pastRun);
    expect(store().openTabs).toHaveLength(2);
    expect(store().pastRunId).toBe('run-1');

    expect(findPipelineTab(store(), 'p1')?.key).toBe(store().openTabs[0].key);
    store().openInEditor(pipeline('p1', 'Orders'));
    expect(store().openTabs).toHaveLength(2);
    expect(store().pastRunId).toBeNull();
    expect(store().workflowId).toBe('p1');
  });

  it('a past run names the project its pipeline lives in, not the empty tab\'s', () => {
    store().newTab({ projectId: 'sandbox', folderId: null });
    store().loadExecutionIntoEditor({
      ...pastRun,
      workflow_snapshot: { ...pastRun.workflow_snapshot, project_id: 'supply-chain', folder_id: 'partner-feeds' },
    });
    expect(store().projectId).toBe('supply-chain');
    expect(store().folderId).toBe('partner-feeds');
    // A snapshot without one falls back to the run's own project.
    store().loadExecutionIntoEditor({ ...pastRun, id: 'run-2', project_id: 'finance' });
    expect(store().projectId).toBe('finance');
  });

  it('a restored version reaches a clean tab and leaves unsaved edits alone', async () => {
    store().openInEditor(pipeline('p1', 'Orders'));
    store().setWorkflowName('Orders (editing)');
    store().openInEditor(pipeline('p2', 'Customers'));

    store().refreshOpenPipeline('p1', pipeline('p1', 'Orders (restored)'));
    store().refreshOpenPipeline('p2', pipeline('p2', 'Customers (restored)'));
    expect(store().workflowName).toBe('Customers (restored)');

    await store().activateTab(tabFor('p1').key);
    expect(store().workflowName).toBe('Orders (editing)');
  });
});

describe('entering the editor', () => {
  it('opens the pipeline the address names', async () => {
    vi.spyOn(api, 'getWorkflow').mockResolvedValue(pipeline('p7', 'Seven') as never);
    await store().ensureEditorTab('p7');
    expect(store().workflowId).toBe('p7');
    expect(store().openTabs).toHaveLength(1);
    await store().ensureEditorTab('p7');
    expect(api.getWorkflow).toHaveBeenCalledTimes(1);
  });

  it('says so when the pipeline in the address cannot be opened, and adds no tab', async () => {
    vi.spyOn(api, 'getWorkflow').mockRejectedValue(new Error('Workflow not found'));
    await store().ensureEditorTab('deleted');
    expect(store().openTabs.filter((t) => describeTab(t, store()).workflowId === 'deleted')).toHaveLength(0);
  });

  it('with no pipeline named, shows an empty canvas in a tab of its own', async () => {
    await store().ensureEditorTab(null);
    expect(store().openTabs).toHaveLength(1);
    expect(store().activeTabKey).toBe(store().openTabs[0].key);
    expect(store().nodes).toHaveLength(0);
  });
});

describe('after a reload', () => {
  it('brings back the saved pipelines that were open, for the same person only', async () => {
    localStorage.clear();
    localStorage.setItem('revoluzen_flow_user', JSON.stringify({ id: 'u1' }));

    vi.resetModules();
    const before = await import('../stores/workflowStore');
    before.restoreSavedTabs(); // signed in: the list is kept from now on
    before.useWorkflowStore.getState().openInEditor(pipeline('p1', 'Orders'));
    before.useWorkflowStore.getState().openInEditor(pipeline('p2', 'Customers'));
    before.useWorkflowStore.getState().newTab(); // not saved: nothing to fetch later
    await before.useWorkflowStore.getState().activateTab(before.useWorkflowStore.getState().openTabs[0].key);

    const kept = JSON.parse(localStorage.getItem('revoluzen_flow_editor_tabs_v1') || 'null');
    expect(kept.owner).toBe('u1::default');
    expect(kept.tabs.map((t: { workflowId: string }) => t.workflowId)).toEqual(['p1', 'p2']);
    expect(kept.active).toBe('p1');

    vi.resetModules();
    const after = await import('../stores/workflowStore');
    const { api: freshApi } = await import('../api/client');
    vi.spyOn(freshApi, 'getWorkflow').mockImplementation(async (id: string) => pipeline(id, id === 'p1' ? 'Orders' : 'Customers') as never);
    await after.useWorkflowStore.getState().ensureEditorTab(null);
    const s = after.useWorkflowStore.getState();
    expect(s.openTabs.map((t) => after.describeTab(t, s).name)).toEqual(['Orders', 'Customers']);
    expect(s.workflowId).toBe('p1');
    expect(freshApi.getWorkflow).toHaveBeenCalledTimes(1); // the other is fetched when picked

    localStorage.setItem('revoluzen_flow_user', JSON.stringify({ id: 'u2' }));
    vi.resetModules();
    const someoneElse = await import('../stores/workflowStore');
    someoneElse.restoreSavedTabs();
    expect(someoneElse.useWorkflowStore.getState().openTabs).toHaveLength(0);

    after.forgetSavedTabs();
    expect(localStorage.getItem('revoluzen_flow_editor_tabs_v1')).toBeNull();
    localStorage.clear();
  });
});
