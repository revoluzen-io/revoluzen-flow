/**
 * Deleting a folder from the Projects page.
 *
 * The server refuses to guess what to do with the pipelines inside a folder
 * (409 unless told `force` or `move_to_root`). The screen sent neither, so a
 * folder holding a pipeline could not be deleted at all, while its confirm said
 * the pipelines would be "permanently deleted". It now asks what is inside,
 * says so, and sends `force`: they go to the Bin and can be restored.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api/client';
import {
  folderDeleteConfirm, folderDeletedToast, folderImpact, folderPublishedNotice, runFolderDelete,
  type FolderDeleteDeps,
} from '../utils/folderDelete';

const preview = (pipelines: number, subfolders = 0, published: string[] = []) => ({
  folder_id: 'f1',
  folder_name: 'Reports',
  descendant_folder_ids: Array.from({ length: subfolders }, (_, i) => `sub${i}`),
  descendant_folder_names: Array.from({ length: subfolders }, (_, i) => `Sub ${i}`),
  doomed_folder_count: 1 + subfolders,
  affected_workflow_count: pipelines,
  affected_workflow_ids: Array.from({ length: pipelines }, (_, i) => `wf${i}`),
  published_workflow_count: published.length,
  published_workflow_ids: published.map((_, i) => `pub${i}`),
  published_workflow_names: published,
});

function deps(over: Partial<FolderDeleteDeps> = {}) {
  const d = {
    previewDelete: vi.fn().mockResolvedValue(preview(3)),
    deleteFolder: vi.fn().mockResolvedValue({ deleted_workflow_count: 3 }),
    confirm: vi.fn().mockResolvedValue(true),
    notify: vi.fn().mockResolvedValue(undefined),
    toast: { success: vi.fn(), error: vi.fn() },
    ...over,
  };
  return d as typeof d & FolderDeleteDeps;
}

afterEach(() => vi.unstubAllGlobals());

describe('folder delete wording', () => {
  it('says an empty folder has nothing in it', () => {
    expect(folderDeleteConfirm('Reports', { pipelines: 0, subfolders: 0, published: [] })).toEqual({
      title: 'Delete folder "Reports"?',
      message: 'The folder will be deleted. It has no pipelines in it.',
      confirmLabel: 'Delete folder',
    });
  });

  it('says where the pipelines go and how to get them back', () => {
    const one = folderDeleteConfirm('Reports', { pipelines: 1, subfolders: 0, published: [] });
    expect(one.message).toBe(
      "The folder will be deleted, and 1 pipeline in it will be moved to the Bin. Restore them from the Bin to get them back; they return to the project's top level, outside any folder.",
    );
    expect(folderDeleteConfirm('Reports', { pipelines: 4, subfolders: 0, published: [] }).message).toContain('4 pipelines in it will be moved to the Bin');
  });

  it('counts sub-folders when there are any', () => {
    expect(folderDeleteConfirm('Reports', { pipelines: 0, subfolders: 2, published: [] }).message)
      .toBe('The folder and its 2 sub-folders will be deleted. It has no pipelines in it.');
    expect(folderDeleteConfirm('Reports', { pipelines: 5, subfolders: 1, published: [] }).message)
      .toContain('The folder and its 1 sub-folder will be deleted, and 5 pipelines in them will be moved to the Bin.');
  });

  it('no longer promises that anything is permanent', () => {
    for (const impact of [{ pipelines: 0, subfolders: 0, published: [] }, { pipelines: 3, subfolders: 1, published: [] }]) {
      const { title, message, confirmLabel } = folderDeleteConfirm('Reports', impact);
      expect(`${title} ${message} ${confirmLabel}`).not.toMatch(/permanent|cannot be undone|\+ pipelines/i);
    }
  });

  it('reports how many pipelines went to the Bin', () => {
    expect(folderDeletedToast('Reports', 0)).toEqual({ title: 'Folder deleted', message: 'Reports' });
    expect(folderDeletedToast('Reports', 1).message).toBe('"Reports" removed; 1 pipeline moved to the Bin');
    expect(folderDeletedToast('Reports', 3).message).toBe('"Reports" removed; 3 pipelines moved to the Bin');
  });

  it('reads the impact from the server preview', () => {
    expect(folderImpact(preview(2, 1))).toEqual({ pipelines: 2, subfolders: 1, published: [] });
    expect(folderImpact(preview(3, 0, ['Daily']))).toEqual({ pipelines: 3, subfolders: 0, published: ['Daily'] });
    // An older server sends no published fields: read as none.
    expect(folderImpact({ affected_workflow_count: 0, descendant_folder_ids: undefined as any }))
      .toEqual({ pipelines: 0, subfolders: 0, published: [] });
  });
});

describe('published pipelines in the folder', () => {
  it('names one and says to revoke it first', () => {
    expect(folderPublishedNotice('Reports', ['Daily'])).toEqual({
      title: 'Folder "Reports" has published pipelines',
      message:
        '"Daily" is published, and a published pipeline cannot be deleted. Revoke it first on the Pipelines page (row ⋯ menu → Revoke), or move it out of the folder, then delete the folder.',
    });
  });

  it('names two or three, and counts the rest', () => {
    expect(folderPublishedNotice('Reports', ['A', 'B']).message).toMatch(/^"A" and "B" are published, and a published pipeline cannot be deleted\. Revoke them first/);
    expect(folderPublishedNotice('Reports', ['A', 'B', 'C']).message).toMatch(/^"A", "B" and "C" are published/);
    expect(folderPublishedNotice('Reports', ['A', 'B', 'C', 'D', 'E']).message).toMatch(/^"A", "B", "C" and 2 more are published/);
    expect(folderPublishedNotice('Reports', ['A', 'B']).message).toContain('move them out of the folder');
  });
});

describe('runFolderDelete', () => {
  const folder = { id: 'f1', name: 'Reports' };

  it('asks what is inside, confirms with the counts, and sends force for a folder with pipelines', async () => {
    const d = deps();

    expect(await runFolderDelete(folder, d)).toBe(true);

    expect(d.previewDelete).toHaveBeenCalledWith('f1');
    expect(d.confirm).toHaveBeenCalledTimes(1);
    expect(d.confirm.mock.calls[0][0].message).toContain('3 pipelines in it will be moved to the Bin');
    expect(d.deleteFolder).toHaveBeenCalledWith('f1', { force: true });
    expect(d.toast.success).toHaveBeenCalledWith('Folder deleted', '"Reports" removed; 3 pipelines moved to the Bin');
    expect(d.previewDelete.mock.invocationCallOrder[0]).toBeLessThan(d.confirm.mock.invocationCallOrder[0]);
    expect(d.confirm.mock.invocationCallOrder[0]).toBeLessThan(d.deleteFolder.mock.invocationCallOrder[0]);
  });

  it('sends no mode for an empty folder', async () => {
    const d = deps({
      previewDelete: vi.fn().mockResolvedValue(preview(0)),
      deleteFolder: vi.fn().mockResolvedValue({ deleted_workflow_count: 0 }),
    });

    expect(await runFolderDelete(folder, d)).toBe(true);

    expect(d.deleteFolder).toHaveBeenCalledWith('f1', undefined);
    expect(d.toast.success).toHaveBeenCalledWith('Folder deleted', 'Reports');
  });

  it('deletes nothing when the user cancels', async () => {
    const d = deps({ confirm: vi.fn().mockResolvedValue(false) });

    expect(await runFolderDelete(folder, d)).toBe(false);

    expect(d.deleteFolder).not.toHaveBeenCalled();
    expect(d.toast.success).not.toHaveBeenCalled();
    expect(d.toast.error).not.toHaveBeenCalled();
  });

  it('tells the user to revoke published pipelines, and asks and deletes nothing', async () => {
    const d = deps({ previewDelete: vi.fn().mockResolvedValue(preview(3, 0, ['Daily', 'Weekly'])) });

    expect(await runFolderDelete(folder, d)).toBe(false);

    expect(d.notify).toHaveBeenCalledTimes(1);
    expect(d.notify).toHaveBeenCalledWith(folderPublishedNotice('Reports', ['Daily', 'Weekly']));
    expect(d.confirm).not.toHaveBeenCalled();
    expect(d.deleteFolder).not.toHaveBeenCalled();
    expect(d.toast.success).not.toHaveBeenCalled();
    expect(d.toast.error).not.toHaveBeenCalled();
  });

  it('does not show the notice when nothing is published', async () => {
    const d = deps();
    await runFolderDelete(folder, d);
    expect(d.notify).not.toHaveBeenCalled();
  });

  it('does not ask for a confirm it cannot describe when the preview fails', async () => {
    const d = deps({ previewDelete: vi.fn().mockRejectedValue(new Error('Folder not found: f1')) });

    expect(await runFolderDelete(folder, d)).toBe(false);

    expect(d.toast.error).toHaveBeenCalledWith('Could not check the folder', 'Folder not found: f1');
    expect(d.confirm).not.toHaveBeenCalled();
    expect(d.deleteFolder).not.toHaveBeenCalled();
  });

  it('shows the server message when the delete is refused', async () => {
    const d = deps({
      deleteFolder: vi.fn().mockRejectedValue(new Error('Folder contains pipelines — choose move_to_root=true or force=true.')),
    });

    expect(await runFolderDelete(folder, d)).toBe(false);

    expect(d.toast.error).toHaveBeenCalledWith(
      'Could not delete the folder',
      'Folder contains pipelines — choose move_to_root=true or force=true.',
    );
    expect(d.toast.success).not.toHaveBeenCalled();
  });
});

describe('the folder calls', () => {
  function answer(body: unknown = {}) {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => body });
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  it('sends force as a query parameter', async () => {
    const fetchMock = answer({ deleted_workflow_count: 2 });
    await api.deleteFolder('f1', { force: true });
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/folders\/f1\?force=true$/);
    expect(fetchMock.mock.calls[0][1].method).toBe('DELETE');
  });

  it('sends move_to_root as a query parameter', async () => {
    const fetchMock = answer();
    await api.deleteFolder('f1', { moveToRoot: true });
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/folders\/f1\?move_to_root=true$/);
  });

  it('sends no query at all for an empty folder', async () => {
    const fetchMock = answer();
    await api.deleteFolder('f1');
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/folders\/f1$/);
  });

  it('asks the preview endpoint without deleting anything', async () => {
    const fetchMock = answer(preview(1));
    const result = await api.previewFolderDelete('f1');
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/folders\/f1\/delete-preview$/);
    expect(fetchMock.mock.calls[0][1].method ?? 'GET').toBe('GET');
    expect(result.affected_workflow_count).toBe(1);
  });
});
