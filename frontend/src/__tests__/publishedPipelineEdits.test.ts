import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api/client';
import { useWorkflowStore } from '../stores/workflowStore';

const answer = { step_id: 'a', status: 'success', row_count: 1, columns: [], sample_data: [], schema_info: [], duration_ms: 1 };

describe('edits on a published pipeline stay out of live runs until Save', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'updateWorkflow').mockResolvedValue({} as never);
    vi.spyOn(api, 'runStep').mockResolvedValue(answer as never);
    vi.spyOn(api, 'runStepEphemeral').mockResolvedValue(answer as never);
    vi.spyOn(api, 'resumeFromStep').mockResolvedValue(answer as never);
    useWorkflowStore.setState({ workflowId: 'w1', workflowName: 'Feed', status: 'published', isDirty: true, pastRunId: null, nodes: [], edges: [] });
  });

  it('tests a step without saving the edit', async () => {
    await useWorkflowStore.getState().runStep('a');
    expect(api.updateWorkflow).not.toHaveBeenCalled();
    expect(api.runStep).not.toHaveBeenCalled();
    expect(api.runStepEphemeral).toHaveBeenCalledTimes(1);
    expect(useWorkflowStore.getState().isDirty).toBe(true);
  });

  it('ignores a save nobody asked for', async () => {
    await expect(useWorkflowStore.getState().ensureWorkflow()).resolves.toBe('w1');
    expect(api.updateWorkflow).not.toHaveBeenCalled();
    expect(useWorkflowStore.getState().isDirty).toBe(true);
  });

  it('saves when asked, and stays published so auto-save stays off', async () => {
    await expect(useWorkflowStore.getState().ensureWorkflow({ allowCreate: true })).resolves.toBe('w1');
    expect(api.updateWorkflow).toHaveBeenCalledTimes(1);
    expect(useWorkflowStore.getState().status).toBe('published');
    expect(useWorkflowStore.getState().isDirty).toBe(false);
  });

  it('reruns unsaved edits without saving them', async () => {
    await useWorkflowStore.getState().resumeFromStep('a');
    expect(api.updateWorkflow).not.toHaveBeenCalled();
    expect(api.resumeFromStep).not.toHaveBeenCalled();
    expect(api.runStepEphemeral).toHaveBeenCalledTimes(1);
  });

  it('reruns a clean canvas from the saved pipeline, without a new version', async () => {
    useWorkflowStore.setState({ isDirty: false });
    await useWorkflowStore.getState().resumeFromStep('a');
    expect(api.updateWorkflow).not.toHaveBeenCalled();
    expect(api.resumeFromStep).toHaveBeenCalledTimes(1);
  });

  it('still saves a draft as it goes', async () => {
    useWorkflowStore.setState({ status: 'draft' });
    await useWorkflowStore.getState().runStep('a');
    expect(api.updateWorkflow).toHaveBeenCalledTimes(1);
    expect(api.runStep).toHaveBeenCalledTimes(1);
    await useWorkflowStore.getState().ensureWorkflow();
    expect(api.updateWorkflow).toHaveBeenCalledTimes(2);
    expect(useWorkflowStore.getState().status).toBe('draft');
  });
});
