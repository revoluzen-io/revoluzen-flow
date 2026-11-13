import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api/client';
import { useWorkflowStore } from '../stores/workflowStore';

const store = () => useWorkflowStore.getState();

const metadata = { scaffolded_from: 'storage_file', source_object_id: 'obj_1', source_object_name: 'people.csv' };
const step = (id: string, type: string) => ({ id, type, label: id, params: {}, position: { x: 0, y: 0 } });
const scaffold = (over: Record<string, unknown> = {}) => ({
  workflow: {
    id: null,
    name: 'people.csv data prep',
    steps: [step('src', 'source'), step('snk', 'local_table_sink')],
    connections: [{ from_step: 'src', to_step: 'snk' }],
    parameters: [],
    metadata,
    ...over,
  },
  version: 1,
});
const savedAs = (over: Record<string, unknown> = {}) => ({
  workflow_id: 'wf_9',
  workflow_name: 'default.people data prep',
  version: 1,
  created: true,
  unchanged: false,
  status: 'draft',
  ...over,
});

describe('a file Data Prep is saved as its table\'s preparation', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'saveFilePreparation').mockResolvedValue(savedAs() as never);
    vi.spyOn(api, 'createWorkflow').mockResolvedValue({ id: 'wf_plain' } as never);
    vi.spyOn(api, 'updateWorkflow').mockResolvedValue({} as never);
    useWorkflowStore.setState({ openTabs: [], activeTabKey: null });
    store().resetWorkflow();
  });

  it('saves with what it was opened from, and becomes that pipeline', async () => {
    store().openInEditor(scaffold());
    expect(store().editorSurface).toBe('file_data_prep');
    expect(store().workflowId).toBeNull();

    await expect(store().ensureWorkflow({ allowCreate: true })).resolves.toBe('wf_9');

    expect(api.saveFilePreparation).toHaveBeenCalledTimes(1);
    expect(api.createWorkflow).not.toHaveBeenCalled();
    const [sent] = vi.mocked(api.saveFilePreparation).mock.calls[0];
    expect(sent.metadata).toEqual(metadata);
    expect(sent.name).toBe('people.csv data prep');
    expect(sent.steps.map((s: { id: string }) => s.id)).toEqual(['src', 'snk']);
    expect(store().workflowId).toBe('wf_9');
    expect(store().workflowName).toBe('default.people data prep');
    expect(store().isDirty).toBe(false);
  });

  it('saves into the project and folder chosen for it', async () => {
    store().openInEditor(scaffold());
    useWorkflowStore.setState({ projectId: 'p1', folderId: 'f1' });
    await store().ensureWorkflow({ allowCreate: true });
    expect(vi.mocked(api.saveFilePreparation).mock.calls[0].slice(1)).toEqual(['p1', 'f1']);
  });

  it('is an ordinary update once saved', async () => {
    store().openInEditor(scaffold());
    await store().ensureWorkflow({ allowCreate: true });
    await store().ensureWorkflow({ allowCreate: true });
    expect(api.saveFilePreparation).toHaveBeenCalledTimes(1);
    expect(api.updateWorkflow).toHaveBeenCalledTimes(1);
  });

  it('does not save a draft nobody asked to save', async () => {
    store().openInEditor(scaffold());
    await expect(store().ensureWorkflow()).resolves.toBeNull();
    expect(api.saveFilePreparation).not.toHaveBeenCalled();
  });

  it('keeps what it was opened from when another tab comes forward', async () => {
    store().openInEditor(scaffold());
    const dataPrepTab = store().activeTabKey as string;
    store().newTab();
    expect(store().scaffoldMetadata).toBeNull();
    await store().activateTab(dataPrepTab);
    expect(store().scaffoldMetadata).toEqual(metadata);
  });

  it('closes a clean tab holding the preparation it just added a version to', async () => {
    store().openInEditor({ workflow: { ...scaffold().workflow, id: 'wf_9', name: 'default.people data prep' }, version: 1 });
    const stale = store().activeTabKey;
    store().openInEditor(scaffold());
    vi.mocked(api.saveFilePreparation).mockResolvedValue(savedAs({ created: false, version: 2 }) as never);

    await store().ensureWorkflow({ allowCreate: true });

    expect(store().openTabs.map((t) => t.key)).not.toContain(stale);
    expect(store().openTabs).toHaveLength(1);
    expect(store().workflowId).toBe('wf_9');
  });

  it('leaves a tab with unsaved edits open', async () => {
    store().openInEditor({ workflow: { ...scaffold().workflow, id: 'wf_9', name: 'default.people data prep' }, version: 1 });
    useWorkflowStore.setState({ isDirty: true });
    store().openInEditor(scaffold());
    vi.mocked(api.saveFilePreparation).mockResolvedValue(savedAs({ created: false, version: 2 }) as never);

    await store().ensureWorkflow({ allowCreate: true });

    expect(store().openTabs).toHaveLength(2);
  });

  it('leaves an ordinary pipeline to the normal save', async () => {
    store().openInEditor({ workflow: { id: null, name: 'Orders', steps: [step('a', 'source')], connections: [], parameters: [] }, version: 1 });
    expect(store().editorSurface).toBe('pipeline');
    await store().ensureWorkflow({ allowCreate: true });
    expect(api.createWorkflow).toHaveBeenCalledTimes(1);
    expect(api.saveFilePreparation).not.toHaveBeenCalled();
  });
});
