/**
 * "Save with details…" opens with what the pipeline already has.
 *
 * The dialog used to open blank, so saving through it moved the pipeline to
 * the Default project, cleared its description and tags, and switched its
 * time limit off. The time limit now stops runs, so losing it matters.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SaveDialog from '../components/SaveDialog';
import { api } from '../api/client';
import { useWorkflowStore } from '../stores/workflowStore';

const SAVED = {
  version: 3,
  workflow: {
    id: 'wf-orders',
    name: 'Nightly orders',
    description: 'Loads yesterday\'s orders',
    project_id: 'proj-finance',
    tags: ['finance', 'nightly'],
    metadata: {
      execution_settings: {
        max_runtime_minutes: 45,
        enable_timeout: true,
        overlap_policy: 'queue',
        enable_overlap_detection: true,
      },
    },
    steps: [],
    connections: [],
  },
};

describe('Save with details', () => {
  beforeEach(() => {
    useWorkflowStore.setState({
      workflowId: 'wf-orders',
      workflowName: 'Nightly orders',
      nodes: [],
      edges: [],
      ensureWorkflow: vi.fn().mockResolvedValue('wf-orders'),
    } as any);
    vi.spyOn(api, 'listProjects').mockResolvedValue([
      { id: 'default', name: 'Default' },
      { id: 'proj-finance', name: 'Finance' },
    ] as any);
    vi.spyOn(api, 'getWorkflow').mockResolvedValue(SAVED as any);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('opens with the saved description, project, tags and time limit', async () => {
    render(<SaveDialog open onClose={() => {}} />);

    expect(await screen.findByDisplayValue('Loads yesterday\'s orders')).toBeInTheDocument();
    expect(screen.getByDisplayValue('finance, nightly')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByDisplayValue('Finance')).toBeInTheDocument());

    fireEvent.click(screen.getByText('Execution'));
    expect(await screen.findByDisplayValue('45')).toBeInTheDocument();
    expect(screen.getByText(/is stopped and marked failed, so failure alerts go out/)).toBeInTheDocument();
    expect(screen.getByText(/Stops a run after 45 minutes/)).toBeInTheDocument();
  });

  it('saving keeps the pipeline\'s settings rather than resetting them', async () => {
    const put = vi.spyOn(api, 'updateWorkflow').mockResolvedValue({} as any);
    render(<SaveDialog open onClose={() => {}} />);
    await screen.findByDisplayValue('Loads yesterday\'s orders');

    fireEvent.click(screen.getByRole('button', { name: /Save Pipeline/ }));

    await waitFor(() => expect(put).toHaveBeenCalled());
    const sent = put.mock.calls[0][1] as any;
    expect(sent.project_id).toBe('proj-finance');
    expect(sent.description).toBe('Loads yesterday\'s orders');
    expect(sent.metadata.execution_settings.max_runtime_minutes).toBe(45);
    expect(sent.metadata.execution_settings.enable_timeout).toBe(true);
    expect(sent.metadata.execution_settings.overlap_policy).toBe('queue');
  });

  it('a pipeline without a limit opens with the limit off', async () => {
    vi.spyOn(api, 'getWorkflow').mockResolvedValue({
      workflow: { ...SAVED.workflow, metadata: {} },
    } as any);
    render(<SaveDialog open onClose={() => {}} />);
    await screen.findByDisplayValue('Loads yesterday\'s orders');
    fireEvent.click(screen.getByText('Execution'));
    expect(screen.queryByText(/Stops a run after/)).toBeNull();
  });
});
