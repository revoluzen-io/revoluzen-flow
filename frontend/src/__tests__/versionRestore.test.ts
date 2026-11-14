/**
 * Saves say what made them, and Restore puts an older version on the
 * pipeline's tab as unsaved changes that wait for an explicit Save.
 */
import { act } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import { useWorkflowStore } from '../stores/workflowStore';

const store = () => useWorkflowStore.getState();

function version(n: number, label: string) {
  return {
    version: n,
    workflow: {
      id: 'p1',
      name: 'Customer sync',
      status: 'draft',
      steps: [{ id: 'read', type: 'source', label, params: {} }],
      connections: [],
    },
  };
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, 'updateWorkflow').mockResolvedValue({} as never);
  for (const t of [...store().openTabs]) store().closeTab(t.key);
  store().resetWorkflow();
  useWorkflowStore.setState({ openTabs: [], activeTabKey: null });
});

describe('what each save records', () => {
  beforeEach(() => {
    act(() => { store().openInEditor(version(7, 'Read customers')); });
    useWorkflowStore.setState({ isDirty: true });
  });

  it('Save is a save', async () => {
    await store().ensureWorkflow({ allowCreate: true });
    expect(api.updateWorkflow).toHaveBeenCalledWith('p1', expect.anything(), 'Saved', 'saved');
  });

  it('auto-save is an auto-save', async () => {
    await store().ensureWorkflow();
    expect(api.updateWorkflow).toHaveBeenCalledWith('p1', expect.anything(), 'Auto-save', 'auto');
  });
});

describe('Restore', () => {
  it('puts the older version on the pipeline as unsaved changes, keeping its status and version', () => {
    act(() => { store().openInEditor({ ...version(7, 'Read customers'), workflow: { ...version(7, 'x').workflow, status: 'published' } }); });
    const result = store().restoreVersionInEditor(version(4, 'Read old customers'), 4, { version: 7, status: 'published' });
    expect(result).toBe('ok');
    const s = store();
    expect(s.workflowId).toBe('p1');
    expect(s.isDirty).toBe(true);
    expect(s.restoredFrom).toBe(4);
    expect(s.version).toBe(7);
    expect(s.status).toBe('published');
    expect((s.nodes[0].data as any).label).toBe('Read old customers');
    expect(s.openTabs).toHaveLength(1);
  });

  it('waits for an explicit Save, which records the restore', async () => {
    act(() => { store().openInEditor(version(7, 'Read customers')); });
    store().restoreVersionInEditor(version(4, 'Read old customers'), 4, { version: 7, status: 'draft' });
    // Auto-save and the save before a run leave it alone, even on a draft.
    await store().ensureWorkflow();
    expect(api.updateWorkflow).not.toHaveBeenCalled();
    await store().ensureWorkflow({ allowCreate: true });
    expect(api.updateWorkflow).toHaveBeenCalledWith('p1', expect.anything(), 'Restored version 4', 'restored');
    expect(store().restoredFrom).toBeNull();
    expect(store().isDirty).toBe(false);
  });

  it('never overwrites edits nobody has saved', () => {
    act(() => { store().openInEditor(version(7, 'Read customers')); });
    store().setWorkflowName('Customer sync, being edited');
    const result = store().restoreVersionInEditor(version(4, 'Read old customers'), 4, { version: 7, status: 'draft' });
    expect(result).toBe('dirty');
    expect(store().workflowName).toBe('Customer sync, being edited');
    expect(store().restoredFrom).toBeNull();
  });

  it('opens a tab for the pipeline when none is open', () => {
    act(() => { store().openInEditor({ version: 2, workflow: { id: 'other', name: 'Other', steps: [], connections: [] } }); });
    const result = store().restoreVersionInEditor(version(4, 'Read old customers'), 4, { version: 7, status: 'draft' });
    expect(result).toBe('ok');
    expect(store().openTabs).toHaveLength(2);
    expect(store().workflowId).toBe('p1');
    expect(store().restoredFrom).toBe(4);
  });
});
