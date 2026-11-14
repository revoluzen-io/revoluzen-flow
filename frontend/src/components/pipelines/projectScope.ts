/**
 * Where the Pipelines list is looking: every project, one project, one folder,
 * a project's pipelines that sit in no folder, or the pipelines whose project
 * the user cannot see.
 *
 * The scope lives in the address (`#pipelines?project=…&folder=…`) so a reload,
 * a shared link and the Back button all land in the same place. Before the
 * project panel there were two filters for the same thing (the project the page
 * was opened for, and a separate "All projects" dropdown on the table) and they
 * could disagree; this is now the only one.
 */

export type PipelineScope =
  | { kind: 'all' }
  | { kind: 'project'; projectId: string }
  | { kind: 'folder'; projectId: string; folderId: string }
  | { kind: 'unfiled'; projectId: string }
  | { kind: 'unassigned' };

export const ALL_SCOPE: PipelineScope = { kind: 'all' };

/** The value `folder=` takes for a project's pipelines that are in no folder. */
export const UNFILED_PARAM = 'none';

/** Read the scope from the address's query (`readHashQuery()`). */
export function scopeFromQuery(query: Record<string, string | undefined>): PipelineScope {
  if (query.view === 'unassigned') return { kind: 'unassigned' };
  const projectId = (query.project || '').trim();
  if (!projectId) return ALL_SCOPE;
  const folder = (query.folder || '').trim();
  if (folder === UNFILED_PARAM) return { kind: 'unfiled', projectId };
  if (folder) return { kind: 'folder', projectId, folderId: folder };
  return { kind: 'project', projectId };
}

/** True when the address names a scope at all (as opposed to a bare `#pipelines`). */
export function queryHasScope(query: Record<string, string | undefined>): boolean {
  return !!(query.project || query.view === 'unassigned');
}

/** The query that puts `scope` in the address. */
export function scopeToQuery(scope: PipelineScope): Record<string, string | undefined> {
  switch (scope.kind) {
    case 'all': return {};
    case 'unassigned': return { view: 'unassigned' };
    case 'project': return { project: scope.projectId };
    case 'folder': return { project: scope.projectId, folder: scope.folderId };
    case 'unfiled': return { project: scope.projectId, folder: UNFILED_PARAM };
  }
}

/** One string per scope, for comparisons and React keys. */
export function scopeKey(scope: PipelineScope): string {
  switch (scope.kind) {
    case 'all': return 'all';
    case 'unassigned': return 'unassigned';
    case 'project': return `project:${scope.projectId}`;
    case 'folder': return `folder:${scope.projectId}:${scope.folderId}`;
    case 'unfiled': return `unfiled:${scope.projectId}`;
  }
}

export function sameScope(a: PipelineScope, b: PipelineScope): boolean {
  return scopeKey(a) === scopeKey(b);
}

/** The project a scope belongs to, or null for All projects and Unassigned. */
export function scopeProjectId(scope: PipelineScope): string | null {
  return scope.kind === 'project' || scope.kind === 'folder' || scope.kind === 'unfiled'
    ? scope.projectId
    : null;
}

/** The folder a scope is in, or null. */
export function scopeFolderId(scope: PipelineScope): string | null {
  return scope.kind === 'folder' ? scope.folderId : null;
}

export interface PipelineLike {
  project_id?: string | null;
  folder_id?: string | null;
}

export interface ScopeContext {
  /**
   * Ids of the projects the user can see. `null` while they are still loading:
   * every pipeline then counts under the project it names.
   */
  visibleProjects: ReadonlySet<string> | null;
  /** Folder ids per project id. A project that is missing has no folders yet. */
  foldersByProject: ReadonlyMap<string, ReadonlySet<string>>;
}

/**
 * The project a pipeline is listed under. A pipeline that names no project
 * belongs to the built-in Default project, as the server treats it. One whose
 * project the user cannot see (deleted, archived, or outside their access) is
 * listed under Unassigned, which is `null` here.
 */
export function pipelineProject(p: PipelineLike, ctx: Pick<ScopeContext, 'visibleProjects'>): string | null {
  const pid = p.project_id || 'default';
  if (!ctx.visibleProjects) return pid;
  return ctx.visibleProjects.has(pid) ? pid : null;
}

/**
 * The folder a pipeline is listed in, or null for its project's top level.
 * A folder id that is not one of its project's folders counts as none, so a
 * pipeline left pointing at a deleted folder is still findable.
 */
export function pipelineFolder(p: PipelineLike, projectId: string | null, ctx: ScopeContext): string | null {
  const fid = p.folder_id || null;
  if (!fid || !projectId) return null;
  const folders = ctx.foldersByProject.get(projectId);
  if (!folders) return fid;
  return folders.has(fid) ? fid : null;
}

/** Does pipeline `p` belong in the list for `scope`? */
export function inScope(p: PipelineLike, scope: PipelineScope, ctx: ScopeContext): boolean {
  if (scope.kind === 'all') return true;
  const pid = pipelineProject(p, ctx);
  if (scope.kind === 'unassigned') return pid === null;
  if (pid !== scope.projectId) return false;
  if (scope.kind === 'project') return true;
  const fid = pipelineFolder(p, pid, ctx);
  if (scope.kind === 'folder') return fid === scope.folderId;
  return fid === null;
}

export interface ScopeCounts {
  total: number;
  byProject: Map<string, number>;
  byFolder: Map<string, number>;
  unfiledByProject: Map<string, number>;
  unassigned: number;
}

/** How many pipelines each project, folder and top level holds. */
export function countPipelines(pipelines: PipelineLike[], ctx: ScopeContext): ScopeCounts {
  const byProject = new Map<string, number>();
  const byFolder = new Map<string, number>();
  const unfiledByProject = new Map<string, number>();
  let unassigned = 0;
  for (const p of pipelines) {
    const pid = pipelineProject(p, ctx);
    if (pid === null) { unassigned += 1; continue; }
    byProject.set(pid, (byProject.get(pid) || 0) + 1);
    const fid = pipelineFolder(p, pid, ctx);
    if (fid) byFolder.set(fid, (byFolder.get(fid) || 0) + 1);
    else unfiledByProject.set(pid, (unfiledByProject.get(pid) || 0) + 1);
  }
  return { total: pipelines.length, byProject, byFolder, unfiledByProject, unassigned };
}

/**
 * Check a scope against what exists. A link can name a project or folder that
 * has since been deleted or archived; the list then falls back to the nearest
 * place that still exists rather than showing an empty page with no way out.
 */
export function validScope(scope: PipelineScope, ctx: ScopeContext): PipelineScope {
  if (scope.kind === 'all' || scope.kind === 'unassigned' || !ctx.visibleProjects) return scope;
  if (!ctx.visibleProjects.has(scope.projectId)) return ALL_SCOPE;
  if (scope.kind === 'folder') {
    const folders = ctx.foldersByProject.get(scope.projectId);
    if (folders && !folders.has(scope.folderId)) return { kind: 'project', projectId: scope.projectId };
  }
  return scope;
}
