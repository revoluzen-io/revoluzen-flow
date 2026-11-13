/**
 * The project panel beside the Pipelines list: it picks the place the list
 * shows, says how many pipelines each place holds, finds a project by a folder
 * inside it, and offers project actions only to those allowed them.
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import ProjectExplorer from '../components/pipelines/ProjectExplorer';
import { countPipelines, type PipelineScope } from '../components/pipelines/projectScope';

const projects = [
  { id: 'default', name: 'Default' },
  { id: 'sales', name: 'Sales', description: 'Revenue reporting', metadata: { department: 'Finance' } },
  { id: 'ops', name: 'Operations' },
];
const foldersByProject = {
  sales: [
    { id: 'daily', name: 'Daily loads', project_id: 'sales' },
    { id: 'monthly', name: 'Month end', project_id: 'sales' },
  ],
};
const counts = countPipelines(
  [
    { project_id: 'sales', folder_id: 'daily' },
    { project_id: 'sales', folder_id: null },
    { project_id: 'a-project-nobody-can-see' },
  ],
  {
    visibleProjects: new Set(['default', 'sales', 'ops']),
    foldersByProject: new Map([['sales', new Set(['daily', 'monthly'])]]),
  },
);

function renderPanel(props: { scope?: PipelineScope; canManageProjects?: boolean; canEditFolders?: boolean } = {}) {
  const onSelect = vi.fn();
  render(
    <ProjectExplorer
      projects={projects as never}
      foldersByProject={foldersByProject}
      counts={counts}
      scope={props.scope ?? { kind: 'all' }}
      onSelect={onSelect}
      archivedCount={2}
      onShowArchived={vi.fn()}
      canManageProjects={props.canManageProjects ?? true}
      canEditFolders={props.canEditFolders ?? true}
      onNewProject={vi.fn()}
      onEditProject={vi.fn()}
      onExportProject={vi.fn()}
      onArchiveProject={vi.fn()}
      onNewFolder={vi.fn()}
      onDeleteFolder={vi.fn()}
    />,
  );
  return { onSelect };
}

beforeEach(() => {
  localStorage.clear();
});

describe('the project panel', () => {
  it('picks every project, a project, a folder, the pipelines in no folder, or Unassigned', () => {
    const { onSelect } = renderPanel({ scope: { kind: 'project', projectId: 'sales' } });

    fireEvent.click(screen.getByRole('button', { name: /^Daily loads/ }));
    expect(onSelect).toHaveBeenLastCalledWith({ kind: 'folder', projectId: 'sales', folderId: 'daily' });
    fireEvent.click(screen.getByRole('button', { name: /^Not in a folder/ }));
    expect(onSelect).toHaveBeenLastCalledWith({ kind: 'unfiled', projectId: 'sales' });
    fireEvent.click(screen.getByRole('button', { name: /^Operations/ }));
    expect(onSelect).toHaveBeenLastCalledWith({ kind: 'project', projectId: 'ops' });
    fireEvent.click(screen.getByRole('button', { name: /^All projects/ }));
    expect(onSelect).toHaveBeenLastCalledWith({ kind: 'all' });
    fireEvent.click(screen.getByRole('button', { name: /^Unassigned/ }));
    expect(onSelect).toHaveBeenLastCalledWith({ kind: 'unassigned' });
  });

  it('shows how many pipelines each place holds and marks the one being looked at', () => {
    renderPanel({ scope: { kind: 'folder', projectId: 'sales', folderId: 'daily' } });
    expect(screen.getByRole('button', { name: /^All projects/ })).toHaveTextContent('3');
    expect(screen.getByRole('button', { name: /^Sales/ })).toHaveTextContent('2');
    expect(screen.getByRole('button', { name: /^Daily loads/ })).toHaveAttribute('aria-current', 'true');
    expect(screen.getByRole('button', { name: /^Sales/ })).not.toHaveAttribute('aria-current');
    expect(screen.getByRole('button', { name: /^Unassigned/ })).toHaveTextContent('1');
  });

  it('finds a project by the name of a folder inside it', () => {
    renderPanel();
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search projects and folders' }), { target: { value: 'month' } });
    expect(screen.getByRole('button', { name: /^Sales/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Month end/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Daily loads/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Operations/ })).toBeNull();
  });

  it('offers creating and archiving projects only to those who manage them', () => {
    renderPanel({ canManageProjects: false, canEditFolders: false });
    expect(screen.getByRole('button', { name: 'New project' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Actions for Sales' }));
    const menu = screen.getByRole('menu', { name: 'Actions for Sales' });
    expect(within(menu).queryByRole('menuitem', { name: 'Archive project' })).toBeNull();
    expect(within(menu).queryByRole('menuitem', { name: 'New folder' })).toBeNull();
    expect(within(menu).getByRole('menuitem', { name: 'Export as JSON' })).toBeInTheDocument();
  });

  it('opens only projects with folders, and offers New folder in the project menu, not as a row', () => {
    renderPanel();
    expect(screen.getByRole('button', { name: /folders of Sales/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /folders of Operations/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /New folder/ })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Actions for Operations' }));
    const menu = screen.getByRole('menu', { name: 'Actions for Operations' });
    expect(within(menu).getByRole('menuitem', { name: 'New folder' })).toBeInTheDocument();
  });

  it('never offers to archive the Default project', () => {
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Actions for Default' }));
    const menu = screen.getByRole('menu', { name: 'Actions for Default' });
    expect(within(menu).queryByRole('menuitem', { name: 'Archive project' })).toBeNull();
  });
});
