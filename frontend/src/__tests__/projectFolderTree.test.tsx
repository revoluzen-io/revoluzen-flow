/**
 * Folders on the Projects page are one level deep: a project holds folders and a
 * folder holds pipelines.
 *
 * Every folder row used to offer "+ Sub-folder". The server has always refused it
 * ("Sub-folders are not supported in Revoluzen Flow"), so the button only ever
 * produced an error toast. It is gone. "+ Folder" on the project still works, a
 * folder nested in older data is still shown, and Delete asks the server what is
 * inside before it confirms.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  api: {
    projectTree: vi.fn(),
    listWorkflows: vi.fn(),
    listFolders: vi.fn(),
    createFolder: vi.fn(),
    previewFolderDelete: vi.fn(),
    deleteFolder: vi.fn(),
  },
  toast: { success: vi.fn(), error: vi.fn() },
  uiPrompt: vi.fn(),
  uiConfirm: vi.fn(),
  uiAlert: vi.fn(),
}));

vi.mock('../api/client', () => ({ api: mocks.api }));
vi.mock('../components/Toast', () => ({ toast: mocks.toast }));
vi.mock('../ui/dialog', () => ({ uiPrompt: mocks.uiPrompt, uiConfirm: mocks.uiConfirm, uiAlert: mocks.uiAlert }));

import ProjectFolderTree from '../components/ProjectFolderTree';

const folder = (over: Record<string, unknown> = {}) => ({
  id: 'f1', name: 'Reports', project_id: 'p1', parent_folder_id: null, ...over,
});

beforeEach(() => {
  localStorage.clear();
  mocks.api.projectTree.mockResolvedValue([{ id: 'p1', name: 'Sales', pipeline_count: 1 }]);
  mocks.api.listWorkflows.mockResolvedValue([
    { id: 'w1', name: 'Daily', project_id: 'p1', folder_id: 'f1', status: 'draft' },
  ]);
  mocks.api.listFolders.mockResolvedValue([folder()]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function openTheProject() {
  // The Filter button only shows when the page passes a filter setter, as the Projects page does.
  render(<ProjectFolderTree onSelectProject={() => {}} onFolderFilterChange={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Expand' }));
  await screen.findByText('Reports');
}

describe('folder row', () => {
  it('offers Filter and Delete, and no way to add a sub-folder', async () => {
    await openTheProject();

    expect(screen.getByTitle(/^Delete this folder/)).toBeInTheDocument();
    expect(screen.getByTitle('Scope the Projects view to only "Reports" and its pipelines.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /sub-folder/i })).toBeNull();
    expect(screen.queryByTitle(/sub-folder/i)).toBeNull();
    expect(screen.queryByText(/\+ Sub-folder/)).toBeNull();
  });

  it('still shows a folder nested in older data, with nothing to add under it', async () => {
    localStorage.setItem('revoluzen_flow_folders_expanded', JSON.stringify(['f1']));
    mocks.api.listFolders.mockResolvedValue([folder(), folder({ id: 'f9', name: 'Legacy', parent_folder_id: 'f1' })]);

    await openTheProject();

    expect(await screen.findByText('Legacy')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /sub-folder/i })).toBeNull();
  });
});

describe('adding a folder', () => {
  it('still works from the project row, and never names a parent', async () => {
    mocks.uiPrompt.mockResolvedValue('Ingestion');
    mocks.api.createFolder.mockResolvedValue({ id: 'f2' });
    await openTheProject();

    fireEvent.click(screen.getByTitle('Add a folder inside "Sales".'));

    await waitFor(() => expect(mocks.api.createFolder).toHaveBeenCalled());
    expect(mocks.api.createFolder).toHaveBeenCalledWith({ name: 'Ingestion', project_id: 'p1' });
    expect(mocks.uiPrompt.mock.calls[0][0].title).toBe('New folder');
    await waitFor(() => expect(mocks.toast.success).toHaveBeenCalledWith('Folder created', 'Ingestion'));
  });

  it('does nothing when the name is left empty', async () => {
    mocks.uiPrompt.mockResolvedValue('   ');
    await openTheProject();

    fireEvent.click(screen.getByTitle('Add a folder inside "Sales".'));
    await waitFor(() => expect(mocks.uiPrompt).toHaveBeenCalled());

    expect(mocks.api.createFolder).not.toHaveBeenCalled();
  });
});

describe('deleting a folder', () => {
  it('asks the server what is inside, says so, and sends force', async () => {
    mocks.api.previewFolderDelete.mockResolvedValue({
      folder_id: 'f1', folder_name: 'Reports', descendant_folder_ids: [], descendant_folder_names: [],
      doomed_folder_count: 1, affected_workflow_count: 1, affected_workflow_ids: ['w1'],
    });
    mocks.uiConfirm.mockResolvedValue(true);
    mocks.api.deleteFolder.mockResolvedValue({ deleted_workflow_count: 1 });
    await openTheProject();

    fireEvent.click(screen.getByTitle(/^Delete this folder/));

    await waitFor(() => expect(mocks.api.deleteFolder).toHaveBeenCalledWith('f1', { force: true }));
    expect(mocks.api.previewFolderDelete).toHaveBeenCalledWith('f1');
    expect(mocks.uiConfirm.mock.calls[0][0].message).toContain('1 pipeline in it will be moved to the Bin');
    expect(mocks.toast.success).toHaveBeenCalledWith('Folder deleted', '"Reports" removed; 1 pipeline moved to the Bin');
  });

  it('says so and asks nothing when a published pipeline is inside', async () => {
    mocks.api.previewFolderDelete.mockResolvedValue({
      folder_id: 'f1', folder_name: 'Reports', descendant_folder_ids: [], descendant_folder_names: [],
      doomed_folder_count: 1, affected_workflow_count: 2, affected_workflow_ids: ['w1', 'w2'],
      published_workflow_count: 1, published_workflow_ids: ['w1'], published_workflow_names: ['Daily'],
    });
    mocks.uiAlert.mockResolvedValue(undefined);
    await openTheProject();

    fireEvent.click(screen.getByTitle(/^Delete this folder/));

    await waitFor(() => expect(mocks.uiAlert).toHaveBeenCalledTimes(1));
    const notice = mocks.uiAlert.mock.calls[0][0];
    expect(notice.title).toBe('Folder "Reports" has published pipelines');
    expect(notice.message).toContain('"Daily" is published');
    expect(notice.message).toContain('Revoke it first');
    expect(mocks.uiConfirm).not.toHaveBeenCalled();
    expect(mocks.api.deleteFolder).not.toHaveBeenCalled();
  });

  it('deletes nothing when the user says no', async () => {
    mocks.api.previewFolderDelete.mockResolvedValue({
      folder_id: 'f1', folder_name: 'Reports', descendant_folder_ids: [], descendant_folder_names: [],
      doomed_folder_count: 1, affected_workflow_count: 1, affected_workflow_ids: ['w1'],
    });
    mocks.uiConfirm.mockResolvedValue(false);
    await openTheProject();

    fireEvent.click(screen.getByTitle(/^Delete this folder/));
    await waitFor(() => expect(mocks.uiConfirm).toHaveBeenCalled());

    expect(mocks.api.deleteFolder).not.toHaveBeenCalled();
  });
});
