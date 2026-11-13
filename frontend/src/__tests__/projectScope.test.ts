/**
 * The Pipelines page lists pipelines for one place at a time, picked in the
 * project panel and kept in the address: every project, one project, one
 * folder, a project's pipelines in no folder, or Unassigned. These are the
 * rules for which pipelines each place lists and what a stale link falls back
 * to.
 */
import { describe, expect, it } from 'vitest';

import {
  ALL_SCOPE,
  countPipelines,
  inScope,
  queryHasScope,
  scopeFromQuery,
  scopeToQuery,
  validScope,
  type PipelineScope,
  type ScopeContext,
} from '../components/pipelines/projectScope';

const ctx: ScopeContext = {
  visibleProjects: new Set(['default', 'sales', 'ops']),
  foldersByProject: new Map([
    ['sales', new Set(['daily', 'monthly'])],
    ['ops', new Set<string>()],
  ]),
};

const pipelines = [
  { id: 'a', project_id: 'sales', folder_id: 'daily' },
  { id: 'b', project_id: 'sales', folder_id: null },
  { id: 'c', project_id: 'sales', folder_id: 'deleted-folder' },
  { id: 'd', project_id: null },
  { id: 'e', project_id: 'archived-project' },
  { id: 'f', project_id: 'ops', folder_id: null },
];

const listed = (scope: PipelineScope, context: ScopeContext = ctx) =>
  pipelines.filter((p) => inScope(p, scope, context)).map((p) => p.id);

describe('the place in the address', () => {
  it.each<[string, PipelineScope]>([
    ['every project', { kind: 'all' }],
    ['a project', { kind: 'project', projectId: 'sales' }],
    ['a folder', { kind: 'folder', projectId: 'sales', folderId: 'daily' }],
    ['not in a folder', { kind: 'unfiled', projectId: 'sales' }],
    ['unassigned', { kind: 'unassigned' }],
  ])('%s survives the round trip', (_label, scope) => {
    expect(scopeFromQuery(scopeToQuery(scope))).toEqual(scope);
  });

  it('reads a bare #pipelines as every project', () => {
    expect(scopeFromQuery({})).toEqual(ALL_SCOPE);
    expect(queryHasScope({})).toBe(false);
    expect(queryHasScope({ project: 'sales' })).toBe(true);
    expect(queryHasScope({ view: 'unassigned' })).toBe(true);
  });

  it('ignores a folder without a project', () => {
    expect(scopeFromQuery({ folder: 'daily' })).toEqual(ALL_SCOPE);
  });
});

describe('which pipelines a place lists', () => {
  it('every project lists everything', () => {
    expect(listed(ALL_SCOPE)).toEqual(['a', 'b', 'c', 'd', 'e', 'f']);
  });

  it('a project lists its pipelines, folders included', () => {
    expect(listed({ kind: 'project', projectId: 'sales' })).toEqual(['a', 'b', 'c']);
  });

  it('a folder lists only its own', () => {
    expect(listed({ kind: 'folder', projectId: 'sales', folderId: 'daily' })).toEqual(['a']);
  });

  it('"Not in a folder" includes a pipeline left in a deleted folder, so it can still be found', () => {
    expect(listed({ kind: 'unfiled', projectId: 'sales' })).toEqual(['b', 'c']);
  });

  it('a pipeline with no project is in Default, as the server has it', () => {
    expect(listed({ kind: 'project', projectId: 'default' })).toEqual(['d']);
  });

  it('a pipeline whose project the user cannot see is under Unassigned', () => {
    expect(listed({ kind: 'unassigned' })).toEqual(['e']);
  });

  it('while projects are loading, a pipeline counts under the project it names', () => {
    const loading: ScopeContext = { visibleProjects: null, foldersByProject: new Map() };
    expect(listed({ kind: 'project', projectId: 'archived-project' }, loading)).toEqual(['e']);
    expect(listed({ kind: 'unassigned' }, loading)).toEqual([]);
  });

  it('counts each pipeline once, in the same places it is listed', () => {
    const counts = countPipelines(pipelines, ctx);
    expect(counts.total).toBe(6);
    expect(Object.fromEntries(counts.byProject)).toEqual({ sales: 3, default: 1, ops: 1 });
    expect(Object.fromEntries(counts.byFolder)).toEqual({ daily: 1 });
    expect(Object.fromEntries(counts.unfiledByProject)).toEqual({ sales: 2, default: 1, ops: 1 });
    expect(counts.unassigned).toBe(1);
    const projects = [...counts.byProject.values()].reduce((a, b) => a + b, 0);
    expect(projects + counts.unassigned).toBe(counts.total);
  });
});

describe('a link to a place that is gone', () => {
  it('falls back to every project when the project cannot be seen', () => {
    expect(validScope({ kind: 'project', projectId: 'archived-project' }, ctx)).toEqual(ALL_SCOPE);
    expect(validScope({ kind: 'folder', projectId: 'archived-project', folderId: 'x' }, ctx)).toEqual(ALL_SCOPE);
  });

  it('falls back to the project when the folder is gone', () => {
    expect(validScope({ kind: 'folder', projectId: 'sales', folderId: 'deleted-folder' }, ctx)).toEqual({
      kind: 'project',
      projectId: 'sales',
    });
  });

  it('keeps a folder while its project folders are still loading', () => {
    const scope: PipelineScope = { kind: 'folder', projectId: 'default', folderId: 'later' };
    expect(validScope(scope, ctx)).toEqual(scope);
  });

  it('leaves everything alone while projects are loading', () => {
    const scope: PipelineScope = { kind: 'project', projectId: 'anything' };
    expect(validScope(scope, { visibleProjects: null, foldersByProject: new Map() })).toEqual(scope);
  });
});
