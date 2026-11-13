/**
 * Deleting a folder, as the project panel on the Pipelines page does it.
 *
 * The server will not guess what to do with the pipelines inside a folder:
 * `DELETE /folders/{id}` on a folder that holds pipelines answers 409 unless it is
 * told to move them to the project's top level (`move_to_root`) or to delete them
 * with the folder (`force`). The screen used to send neither, so a folder with
 * pipelines in it could not be deleted at all, while its confirm said they would
 * be "permanently deleted".
 *
 * It now asks the server what is inside (`delete-preview`), says so, and sends
 * `force`. The pipelines go to the Bin, where they can be restored; they come
 * back in the project's top level, because the folder they were in is gone.
 */
import type { FolderDeletePreview } from '../api/client';

const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`;

export interface FolderDeleteImpact {
  pipelines: number;
  subfolders: number;
  /** Names of the pipelines in it that are published. */
  published: string[];
}

/** What a delete would take with it, from the server's preview. */
export function folderImpact(
  preview: Pick<FolderDeletePreview, 'affected_workflow_count' | 'descendant_folder_ids'>
    & Partial<Pick<FolderDeletePreview, 'published_workflow_names'>>,
): FolderDeleteImpact {
  return {
    pipelines: preview.affected_workflow_count || 0,
    subfolders: (preview.descendant_folder_ids || []).length,
    published: preview.published_workflow_names || [],
  };
}

/** "A", "A" and "B", "A", "B" and "C", then "A", "B", "C" and 2 more. */
function nameList(names: string[], max = 3): string {
  const shown = names.slice(0, max).map((n) => `"${n}"`);
  const more = names.length - shown.length;
  if (more > 0) return `${shown.join(', ')} and ${more} more`;
  if (shown.length <= 1) return shown.join('');
  return `${shown.slice(0, -1).join(', ')} and ${shown[shown.length - 1]}`;
}

/**
 * Shown instead of the confirm when published pipelines are in the folder. The
 * Pipelines page offers Delete only for a draft, failed or archived pipeline (a
 * published one has to be unpublished first), and the server refuses a folder delete
 * with `force` on the same grounds, so the screen says so up front.
 */
export function folderPublishedNotice(name: string, published: string[]): { title: string; message: string } {
  const one = published.length === 1;
  return {
    title: `Folder "${name}" has published pipelines`,
    message:
      `${nameList(published)} ${one ? 'is' : 'are'} published, and a published pipeline cannot be deleted. ` +
      `Unpublish ${one ? 'it' : 'them'} first on the Pipelines page (row ⋯ menu → Unpublish), or move ${one ? 'it' : 'them'} ` +
      'out of the folder, then delete the folder.',
  };
}

/** The confirm shown before a folder is deleted. */
export function folderDeleteConfirm(name: string, impact: FolderDeleteImpact): {
  title: string;
  message: string;
  confirmLabel: string;
} {
  const { pipelines, subfolders } = impact;
  const sub = subfolders > 0 ? ` and its ${plural(subfolders, 'sub-folder')}` : '';
  const title = `Delete folder "${name}"?`;
  if (pipelines === 0) {
    return { title, message: `The folder${sub} will be deleted. It has no pipelines in it.`, confirmLabel: 'Delete folder' };
  }
  return {
    title,
    message:
      `The folder${sub} will be deleted, and ${plural(pipelines, 'pipeline')} in ${subfolders > 0 ? 'them' : 'it'} ` +
      "will be moved to the Bin. Restore them from the Bin to get them back; they return to the project's top level, outside any folder.",
    confirmLabel: 'Delete folder',
  };
}

/** The toast after a folder is deleted. `moved` is how many pipelines went to the Bin. */
export function folderDeletedToast(name: string, moved: number): { title: string; message: string } {
  return {
    title: 'Folder deleted',
    message: moved === 0 ? name : `"${name}" removed; ${plural(moved, 'pipeline')} moved to the Bin`,
  };
}

export interface FolderDeleteDeps {
  previewDelete: (id: string) => Promise<FolderDeletePreview>;
  deleteFolder: (id: string, opts?: { force?: boolean }) => Promise<{ deleted_workflow_count?: number } | undefined>;
  confirm: (options: { title: string; message: string; confirmLabel: string }) => Promise<boolean>;
  /** A message with one OK button (the app's themed alert). */
  notify: (options: { title: string; message: string }) => Promise<void>;
  toast: {
    success: (title: string, message?: string) => void;
    error: (title: string, message?: string) => void;
  };
}

/**
 * Ask what is inside, confirm, delete. Resolves to true when the folder was
 * deleted, so the caller knows to reload its lists. If published pipelines are in
 * it, the user is told to revoke them first and nothing is asked or deleted.
 * `force` is sent only when the preview showed pipelines: if one turns up between
 * the preview and the delete, the server refuses (409) and the user is told,
 * instead of a pipeline they were never shown going to the Bin.
 */
export async function runFolderDelete(
  folder: { id: string; name: string },
  deps: FolderDeleteDeps,
): Promise<boolean> {
  let impact: FolderDeleteImpact;
  try {
    impact = folderImpact(await deps.previewDelete(folder.id));
  } catch (err: any) {
    deps.toast.error('Could not check the folder', err?.message || String(err));
    return false;
  }
  if (impact.published.length > 0) {
    await deps.notify(folderPublishedNotice(folder.name, impact.published));
    return false;
  }
  if (!(await deps.confirm(folderDeleteConfirm(folder.name, impact)))) return false;
  try {
    const result = await deps.deleteFolder(folder.id, impact.pipelines > 0 ? { force: true } : undefined);
    const toast = folderDeletedToast(folder.name, result?.deleted_workflow_count || 0);
    deps.toast.success(toast.title, toast.message);
    return true;
  } catch (err: any) {
    deps.toast.error('Could not delete the folder', err?.message || String(err));
    return false;
  }
}
