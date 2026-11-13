/**
 * Project actions that used to live on the retired Projects page: export a
 * project as a JSON bundle, archive it, restore it, delete it for good. The
 * server rules are unchanged (archive, restore and delete are admin-only; a
 * project that still holds pipelines, connections or credentials cannot be
 * deleted). Each function reports its own outcome and resolves to true when
 * the server accepted the change.
 */
import { api } from '../../api/client';
import { toast } from '../Toast';

const slug = (name: string) =>
  (name || 'project').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'project';

/** Download a project as a JSON bundle, named after the project. */
export async function exportProjectBundle(project: { id: string; name: string }): Promise<boolean> {
  try {
    const data = await api.exportProject(project.id);
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${slug(project.name)}-project-export.json`;
    a.click();
    URL.revokeObjectURL(url);
    toast.success('Project exported', `Downloaded ${a.download}`);
    return true;
  } catch (err: any) {
    toast.error('Export failed', err?.message || 'Could not export project');
    return false;
  }
}

/** Archive a project: it leaves the list until someone restores it. */
export async function archiveProject(project: { id: string; name: string }): Promise<boolean> {
  try {
    await api.archiveProject(project.id);
    toast.success('Project archived', `"${project.name}" moved to Archived. Restore it any time.`);
    return true;
  } catch (err: any) {
    toast.error('Archive failed', err?.message || 'Could not archive project');
    return false;
  }
}

export async function restoreProject(project: { id: string; name: string }): Promise<boolean> {
  try {
    await api.restoreProject(project.id);
    toast.success('Project restored', `"${project.name}" is active again`);
    return true;
  } catch (err: any) {
    toast.error('Restore failed', err?.message || 'Could not restore project');
    return false;
  }
}

/**
 * The server answers 409 with `{message, pipelines, connections, credentials}`
 * when the project still holds things. Say exactly what has to move first.
 */
export function deleteRefusalMessage(err: unknown): string {
  const raw = (err as any)?.message;
  let detail = typeof raw === 'string' && raw ? raw : 'Failed';
  try {
    const parsed = typeof raw === 'string' && raw.trim().startsWith('{') ? JSON.parse(raw) : null;
    if (parsed && (parsed.pipelines || parsed.connections || parsed.credentials)) {
      const bits: string[] = [];
      if (parsed.pipelines) bits.push(`${parsed.pipelines} pipeline${parsed.pipelines === 1 ? '' : 's'}`);
      if (parsed.connections) bits.push(`${parsed.connections} connection${parsed.connections === 1 ? '' : 's'}`);
      if (parsed.credentials) bits.push(`${parsed.credentials} credential${parsed.credentials === 1 ? '' : 's'}`);
      detail = `Still contains ${bits.join(', ')}. Move them to another project first.`;
    }
  } catch { /* not JSON: keep the server's text */ }
  return detail;
}

export async function deleteProjectForGood(project: { id: string; name: string }): Promise<boolean> {
  try {
    await api.deleteProject(project.id);
    toast.success('Project permanently deleted', project.name);
    return true;
  } catch (err) {
    toast.error('Cannot delete project', deleteRefusalMessage(err));
    return false;
  }
}
