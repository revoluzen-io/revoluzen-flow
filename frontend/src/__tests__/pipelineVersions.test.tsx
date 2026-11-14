/**
 * The pipeline tab's Versions view: the kept versions newest first, the
 * comparison with the current version, and what can be done with an older
 * version (restore it into the Editor, name it).
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { api, type VersionHistory, type VersionHistoryItem } from '../api/client';
import PipelineVersions from '../components/pipelines/PipelineVersions';
import { useWorkflowStore } from '../stores/workflowStore';

const store = () => useWorkflowStore.getState();
const at = '2026-11-03T10:00:00Z';

function item(v: number, over: Partial<VersionHistoryItem>): VersionHistoryItem {
  return {
    version: v, kind: 'saved', name: '', created_by: 'Siva', created_at: at, change_summary: 'Saved',
    step_count: 2, content_hash: `h${v}`, published: false, published_at: null, deployed: false,
    runs: 0, last_run_status: '', last_run_at: '', ...over,
  };
}

const history: VersionHistory = {
  workflow_id: 'p1',
  current_version: 7,
  status: 'published',
  deployed_version: null,
  removed: 2,
  keep: { saves: 50, auto_saves: 10 },
  versions: [
    item(7, { created_by: 'Priya', change_summary: 'Keep trial customers' }),
    item(6, { kind: 'auto', change_summary: 'Auto-save' }),
    item(5, { kind: 'auto', change_summary: 'Auto-save' }),
    item(4, { name: 'Go-live', change_summary: 'Added checks', published: true, runs: 5, last_run_status: 'success', last_run_at: at }),
    item(3, { kind: 'review', change_summary: 'Approved by Siva' }),
    item(1, { change_summary: 'Initial creation', created_by: 'user' }),
  ],
};

const read = { id: 'read', type: 'source', label: 'Read customers', params: { object: 'Account' }, position: { x: 0, y: 0 } };
const keep = (condition: string) => ({ id: 'keep', type: 'filter', label: 'Keep active', params: { condition }, position: { x: 260, y: 0 } });
const write = { id: 'write', type: 'destination', label: 'Write customers', params: { table: 'crm.customers' }, position: { x: 520, y: 0 } };
const older = { id: 'p1', name: 'Customer sync', status: 'draft', steps: [read, keep("status = 'Active'")], connections: [{ from_step: 'read', to_step: 'keep' }] };
const current = {
  id: 'p1', name: 'Customer sync', status: 'published',
  steps: [read, keep("status in ('Active', 'Trial')"), write],
  connections: [{ from_step: 'read', to_step: 'keep' }, { from_step: 'keep', to_step: 'write' }],
};

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, 'getWorkflowHistory').mockResolvedValue(history);
  vi.spyOn(api, 'getWorkflowVersion').mockImplementation(async (_id: string, v: number) => ({ version: v, workflow: v === 7 ? current : older }));
  vi.spyOn(api, 'listConnections').mockResolvedValue([] as never);
  for (const t of [...store().openTabs]) store().closeTab(t.key);
  store().resetWorkflow();
  useWorkflowStore.setState({ openTabs: [], activeTabKey: null });
  window.location.hash = '#pipelines/p1';
});

function show() {
  return render(
    <PipelineVersions
      pipelineId="p1"
      pipelineName="Customer sync"
      projectId={null}
      folderId={null}
      canEdit
      onOpenVersion={() => {}}
    />,
  );
}

describe('the Versions view', () => {
  it('lists the versions newest first, folds auto-saves and marks the current, live and published ones', async () => {
    show();
    const list = await screen.findByRole('complementary', { name: 'Versions' });
    expect(within(list).getByText('Keep trial customers')).toBeInTheDocument();
    expect(within(list).getByText('Current')).toBeInTheDocument();
    expect(within(list).getByText('Live')).toBeInTheDocument();
    expect(within(list).getByText('Go-live')).toBeInTheDocument();
    expect(within(list).getByText('Published')).toBeInTheDocument();
    expect(within(list).getByRole('button', { name: /2 auto-saves/ })).toHaveAttribute('aria-expanded', 'false');
    expect(within(list).getByText('Approved by Siva')).toBeInTheDocument();
    // Versions from before Flow recorded who saved them say so.
    expect(within(list).getByText(/Not recorded/)).toBeInTheDocument();
    expect(within(list).getByText(/the last 50 saves and the last 10 auto-saves\. 2 older versions have been removed\./)).toBeInTheDocument();
  });

  it('compares the version before the current one with the current one', async () => {
    show();
    const summary = await screen.findByTestId('version-summary');
    await waitFor(() => expect(within(summary).getByText('+1 added')).toBeInTheDocument());
    expect(within(summary).getByText('1 changed')).toBeInTheDocument();
    expect((screen.getByLabelText('Compare') as HTMLSelectElement).value).toBe('6');
    expect((screen.getByLabelText('with') as HTMLSelectElement).value).toBe('7');
    // The changed step's settings show below the canvas.
    const dock = screen.getByTestId('version-step-dock');
    expect(within(dock).getByText("status = 'Active'")).toBeInTheDocument();
    expect(within(dock).getByText("status in ('Active', 'Trial')")).toBeInTheDocument();
  });

  it('shows the changes as a list', async () => {
    show();
    await screen.findByText('+1 added');
    fireEvent.click(screen.getByRole('button', { name: 'List' }));
    const list = screen.getByTestId('version-change-list');
    expect(within(list).getByText('Steps changed')).toBeInTheDocument();
    expect(within(list).getByText('Steps added')).toBeInTheDocument();
    expect(within(list).getByText(/From version 6 to the current version: 1 step added, 1 changed\./)).toBeInTheDocument();
  });

  it('restores into the Editor as unsaved changes after asking', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    show();
    await screen.findByText('+1 added');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Restore…' })); });
    await waitFor(() => expect(store().restoredFrom).toBe(6));
    expect(store().workflowId).toBe('p1');
    expect(store().isDirty).toBe(true);
    expect(store().version).toBe(7);
    expect(window.location.hash).toBe('#editor/p1');
  });

  it('names a version', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('Trial customers');
    const named = vi.spyOn(api, 'nameWorkflowVersion').mockResolvedValue({ workflow_id: 'p1', version: 7, name: 'Trial customers' });
    show();
    await screen.findByText('+1 added');
    fireEvent.click(screen.getAllByRole('button', { name: 'More for version 7' })[0]);
    await act(async () => { fireEvent.click(screen.getByRole('menuitem', { name: /Name this version/ })); });
    expect(named).toHaveBeenCalledWith('p1', 7, 'Trial customers');
  });
});
