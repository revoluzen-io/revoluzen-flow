/**
 * The project panel on the Pipelines page: every project and its folders, with
 * how many pipelines each holds. Picking a row narrows the table beside it.
 *
 * It replaces the separate Projects page (2026-11-10). That page listed the same
 * projects and folders, but its pipeline rows could not be opened, folders could
 * only be added there, and moving between a project and its pipelines meant
 * changing page each time. Search, pins, New project, Edit, Export, Archive and
 * the archived list all came across; folders are one level deep, as the server
 * allows.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import type { PipelineScope, ScopeCounts } from './projectScope';
import { sameScope, scopeProjectId } from './projectScope';
import { ProjectIcon, type ProjectRecord } from '../projects/ProjectFormDialog';

export interface ExplorerFolder {
  id: string;
  name: string;
  project_id: string;
}

const PINS_KEY = 'revoluzen_flow_projects_pinned';
const OPEN_KEY = 'revoluzen_flow_explorer_open';

function readSet(key: string): Set<string> {
  try {
    const raw = JSON.parse(localStorage.getItem(key) || '[]');
    return new Set(Array.isArray(raw) ? raw.filter((v) => typeof v === 'string') : []);
  } catch {
    return new Set();
  }
}

function writeSet(key: string, value: Set<string>) {
  try { localStorage.setItem(key, JSON.stringify(Array.from(value))); } catch { /* storage unavailable */ }
}

interface Props {
  projects: ProjectRecord[];
  foldersByProject: Record<string, ExplorerFolder[]>;
  counts: ScopeCounts;
  scope: PipelineScope;
  onSelect: (scope: PipelineScope) => void;
  archivedCount: number;
  onShowArchived: () => void;
  /** New project, Edit, Archive: admins only, as the server requires. */
  canManageProjects: boolean;
  /** New folder and Delete folder. */
  canEditFolders: boolean;
  onNewProject: () => void;
  onEditProject: (projectId: string) => void;
  onExportProject: (projectId: string) => void;
  onArchiveProject: (projectId: string) => void;
  onNewFolder: (projectId: string) => void;
  onDeleteFolder: (folder: ExplorerFolder) => void;
  loading?: boolean;
  className?: string;
  /** Shown as a drawer on narrow screens: a close button appears. */
  onClose?: () => void;
}

const chevron = (open: boolean) => (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" className={`transition-transform ${open ? 'rotate-90' : ''}`} aria-hidden>
    <polyline points="9 18 15 12 9 6" />
  </svg>
);

const folderGlyph = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
  </svg>
);

const dotsGlyph = (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
    <circle cx="5" cy="12" r="1.7" /><circle cx="12" cy="12" r="1.7" /><circle cx="19" cy="12" r="1.7" />
  </svg>
);

export default function ProjectExplorer({
  projects, foldersByProject, counts, scope, onSelect,
  archivedCount, onShowArchived,
  canManageProjects, canEditFolders,
  onNewProject, onEditProject, onExportProject, onArchiveProject,
  onNewFolder, onDeleteFolder,
  loading = false, className = '', onClose,
}: Props) {
  const [query, setQuery] = useState('');
  const [pinned, setPinned] = useState<Set<string>>(() => readSet(PINS_KEY));
  const [open, setOpen] = useState<Set<string>>(() => readSet(OPEN_KEY));
  const [menu, setMenu] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  // Show the folders of the project being looked at, so the place you are in
  // is always visible in the panel.
  const selectedProject = scopeProjectId(scope);
  useEffect(() => {
    if (!selectedProject) return;
    setOpen((prev) => {
      if (prev.has(selectedProject)) return prev;
      const next = new Set(prev);
      next.add(selectedProject);
      writeSet(OPEN_KEY, next);
      return next;
    });
  }, [selectedProject]);

  useEffect(() => {
    if (!menu) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenu(null);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setMenu(null); };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [menu]);

  const toggleOpen = (projectId: string) => {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(projectId)) next.delete(projectId); else next.add(projectId);
      writeSet(OPEN_KEY, next);
      return next;
    });
  };

  const togglePin = (projectId: string) => {
    setPinned((prev) => {
      const next = new Set(prev);
      if (next.has(projectId)) next.delete(projectId); else next.add(projectId);
      writeSet(PINS_KEY, next);
      return next;
    });
  };

  const q = query.trim().toLowerCase();
  const folderMatches = (projectId: string) =>
    !!q && (foldersByProject[projectId] || []).some((f) => f.name.toLowerCase().includes(q));

  const visible = useMemo(() => {
    const matches = (p: ProjectRecord) =>
      !q ||
      [p.name, p.description, p.metadata?.department, p.owner, p.created_by, p.id]
        .some((v) => String(v || '').toLowerCase().includes(q)) ||
      (foldersByProject[p.id] || []).some((f) => f.name.toLowerCase().includes(q));
    return projects
      .filter(matches)
      .sort((a, b) => {
        const pa = pinned.has(a.id) ? 0 : 1;
        const pb = pinned.has(b.id) ? 0 : 1;
        if (pa !== pb) return pa - pb;
        return (a.name || '').localeCompare(b.name || '');
      });
  }, [projects, q, pinned, foldersByProject]);

  const rowBase = 'flex items-center h-8 rounded-md transition-colors';
  const selectedCls = 'bg-amber-50 text-amber-900 ring-1 ring-inset ring-amber-200';
  const idleCls = 'text-slate-700 hover:bg-slate-100';

  const countBadge = (n: number | undefined, selected: boolean) => (
    <span className={`ml-auto pl-2 text-[11px] tabular-nums shrink-0 ${selected ? 'text-amber-800' : 'text-slate-500'}`}>{n ?? 0}</span>
  );

  const menuItem = (label: string, onClick: () => void, danger = false) => (
    <button
      type="button"
      role="menuitem"
      onClick={() => { setMenu(null); onClick(); }}
      className={`w-full text-left px-3 py-1.5 text-sm rounded-md ${danger ? 'text-red-600 hover:bg-red-50' : 'text-slate-700 hover:bg-slate-100'}`}
    >
      {label}
    </button>
  );

  const renderProject = (p: ProjectRecord) => {
    const isOpen = open.has(p.id) || folderMatches(p.id);
    const folders = (foldersByProject[p.id] || []).slice().sort((a, b) => a.name.localeCompare(b.name));
    const shownFolders = q && !String(p.name || '').toLowerCase().includes(q)
      ? folders.filter((f) => f.name.toLowerCase().includes(q))
      : folders;
    const selected = sameScope(scope, { kind: 'project', projectId: p.id });
    const color = p.color || '#6366f1';
    const unfiled = counts.unfiledByProject.get(p.id) || 0;
    const menuKey = `p:${p.id}`;
    return (
      <li key={p.id} className="relative">
        <div className={`${rowBase} ${selected ? selectedCls : idleCls} group`}>
          {/* Only a project with folders opens; one without has nothing to
              show under it. New folder lives in the project's menu (…) and on
              the list's bar while the project is picked, not as a row in the
              tree (founders, 2026-11-10). */}
          {folders.length > 0 ? (
            <button
              type="button"
              onClick={() => toggleOpen(p.id)}
              aria-expanded={isOpen}
              aria-label={`${isOpen ? 'Hide' : 'Show'} folders of ${p.name}`}
              className="w-6 h-8 flex items-center justify-center text-slate-500 hover:text-slate-800 shrink-0"
            >
              {chevron(isOpen)}
            </button>
          ) : (
            <span className="w-6 h-8 shrink-0" aria-hidden="true" />
          )}
          <button
            type="button"
            onClick={() => onSelect({ kind: 'project', projectId: p.id })}
            aria-current={selected ? 'true' : undefined}
            title={p.description ? `${p.name}: ${p.description}` : p.name}
            className="flex-1 min-w-0 h-full flex items-center gap-2 pr-1 text-left"
          >
            <span className="w-5 h-5 rounded flex items-center justify-center shrink-0" style={{ background: `${color}1f`, color }}>
              <ProjectIcon icon={p.icon} size={12} />
            </span>
            <span className={`truncate text-sm ${selected ? 'font-semibold' : 'font-medium'}`}>{p.name}</span>
            {pinned.has(p.id) && (
              <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" className="text-amber-500 shrink-0" aria-label="Pinned">
                <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
              </svg>
            )}
            {countBadge(counts.byProject.get(p.id), selected)}
          </button>
          {/* Export and Pin are open to everyone, so every project has a menu. */}
          <button
            type="button"
            onClick={() => setMenu(menu === menuKey ? null : menuKey)}
            aria-label={`Actions for ${p.name}`}
            aria-haspopup="menu"
            aria-expanded={menu === menuKey}
            className={`w-7 h-7 mr-0.5 rounded flex items-center justify-center text-slate-500 hover:text-slate-800 hover:bg-white/70 shrink-0 ${menu === menuKey ? '' : 'opacity-0 group-hover:opacity-100 focus:opacity-100'}`}
          >
            {dotsGlyph}
          </button>
        </div>
        {menu === menuKey && (
          <div ref={menuRef} role="menu" aria-label={`Actions for ${p.name}`} className="absolute right-1 top-8 z-30 w-48 bg-white border border-slate-200 rounded-lg shadow-lg p-1">
            {canEditFolders && menuItem('New folder', () => onNewFolder(p.id))}
            {canManageProjects && menuItem('Edit project', () => onEditProject(p.id))}
            {menuItem('Export as JSON', () => onExportProject(p.id))}
            {menuItem(pinned.has(p.id) ? 'Unpin' : 'Pin to top', () => togglePin(p.id))}
            {canManageProjects && p.id !== 'default' && menuItem('Archive project', () => onArchiveProject(p.id), true)}
          </div>
        )}
        {isOpen && folders.length > 0 && (
          <ul className="mt-0.5 mb-1">
            {shownFolders.map((f) => {
              const fSelected = sameScope(scope, { kind: 'folder', projectId: p.id, folderId: f.id });
              const fMenuKey = `f:${f.id}`;
              return (
                <li key={f.id} className="relative">
                  <div className={`${rowBase} ${fSelected ? selectedCls : idleCls} group pl-6`}>
                    <button
                      type="button"
                      onClick={() => onSelect({ kind: 'folder', projectId: p.id, folderId: f.id })}
                      aria-current={fSelected ? 'true' : undefined}
                      title={`${p.name} / ${f.name}`}
                      className="flex-1 min-w-0 h-full flex items-center gap-2 pl-1.5 pr-1 text-left"
                    >
                      <span className="text-amber-600 shrink-0">{folderGlyph}</span>
                      <span className={`truncate text-sm ${fSelected ? 'font-semibold' : ''}`}>{f.name}</span>
                      {countBadge(counts.byFolder.get(f.id), fSelected)}
                    </button>
                    {canEditFolders && (
                      <button
                        type="button"
                        onClick={() => setMenu(menu === fMenuKey ? null : fMenuKey)}
                        aria-label={`Actions for folder ${f.name}`}
                        aria-haspopup="menu"
                        aria-expanded={menu === fMenuKey}
                        className={`w-7 h-7 mr-0.5 rounded flex items-center justify-center text-slate-500 hover:text-slate-800 hover:bg-white/70 shrink-0 ${menu === fMenuKey ? '' : 'opacity-0 group-hover:opacity-100 focus:opacity-100'}`}
                      >
                        {dotsGlyph}
                      </button>
                    )}
                  </div>
                  {menu === fMenuKey && (
                    <div ref={menuRef} role="menu" aria-label={`Actions for folder ${f.name}`} className="absolute right-1 top-8 z-30 w-48 bg-white border border-slate-200 rounded-lg shadow-lg p-1">
                      {menuItem('Delete folder', () => onDeleteFolder(f), true)}
                    </div>
                  )}
                </li>
              );
            })}
            {folders.length > 0 && unfiled > 0 && !q && (() => {
              const uSelected = sameScope(scope, { kind: 'unfiled', projectId: p.id });
              return (
                <li>
                  <div className={`${rowBase} ${uSelected ? selectedCls : idleCls} pl-6`}>
                    <button
                      type="button"
                      onClick={() => onSelect({ kind: 'unfiled', projectId: p.id })}
                      aria-current={uSelected ? 'true' : undefined}
                      title={`Pipelines in ${p.name} that are not in a folder`}
                      className="flex-1 min-w-0 h-full flex items-center gap-2 pl-1.5 pr-1 text-left"
                    >
                      <span className="text-slate-400 shrink-0">{folderGlyph}</span>
                      <span className="truncate text-sm italic text-slate-500">Not in a folder</span>
                      {countBadge(unfiled, uSelected)}
                    </button>
                  </div>
                </li>
              );
            })()}
          </ul>
        )}
      </li>
    );
  };

  const allSelected = scope.kind === 'all';
  const unassignedSelected = scope.kind === 'unassigned';

  return (
    <nav aria-label="Projects and folders" className={`flex flex-col min-h-0 bg-white ${className}`}>
      <div className="flex items-center justify-between px-3 pt-3 pb-2 shrink-0">
        <span className="text-[11px] font-bold tracking-wider uppercase text-slate-500">Projects</span>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={onNewProject}
            disabled={!canManageProjects}
            title={canManageProjects ? 'New project' : 'Only admins can create projects. Ask a workspace admin.'}
            aria-label="New project"
            className="h-7 px-2 rounded-md flex items-center gap-1 text-xs font-semibold text-indigo-700 hover:bg-indigo-50 disabled:opacity-40 disabled:hover:bg-transparent"
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" aria-hidden><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg>
            New
          </button>
          {onClose && (
            <button type="button" onClick={onClose} aria-label="Close projects" className="w-7 h-7 rounded-md flex items-center justify-center text-slate-500 hover:bg-slate-100">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" aria-hidden><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
            </button>
          )}
        </div>
      </div>
      <div className="px-3 pb-2 shrink-0">
        <div className="relative">
          <svg className="absolute left-2 top-1/2 -translate-y-1/2 text-slate-400" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden><circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" /></svg>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search projects and folders"
            aria-label="Search projects and folders"
            className="w-full pl-7 pr-2 py-1.5 text-sm bg-white border border-slate-300 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-300 focus:border-indigo-400 placeholder:text-slate-400"
          />
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto px-2 pb-2">
        <div className={`${rowBase} ${allSelected ? selectedCls : idleCls}`}>
          <button
            type="button"
            onClick={() => onSelect({ kind: 'all' })}
            aria-current={allSelected ? 'true' : undefined}
            className="flex-1 min-w-0 h-full flex items-center gap-2 px-2 text-left"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-slate-500 shrink-0" aria-hidden>
              <rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" />
            </svg>
            <span className={`truncate text-sm ${allSelected ? 'font-semibold' : 'font-medium'}`}>All projects</span>
            {countBadge(counts.total, allSelected)}
          </button>
        </div>

        {loading && projects.length === 0 ? (
          <p className="px-2 py-3 text-xs text-slate-500">Loading projects…</p>
        ) : projects.length === 0 ? (
          <div className="px-2 py-3 text-xs text-slate-500 leading-relaxed">
            No projects yet.{canManageProjects ? ' Use New above to create one; you can then add folders inside it.' : ' Ask an admin to create one.'}
          </div>
        ) : visible.length === 0 ? (
          <p className="px-2 py-3 text-xs text-slate-500">No project or folder matches "{query.trim()}".</p>
        ) : (
          <ul className="mt-0.5 space-y-0.5">{visible.map(renderProject)}</ul>
        )}

        {counts.unassigned > 0 && !q && (
          <div className={`${rowBase} mt-1 ${unassignedSelected ? selectedCls : idleCls}`}>
            <button
              type="button"
              onClick={() => onSelect({ kind: 'unassigned' })}
              aria-current={unassignedSelected ? 'true' : undefined}
              title="Pipelines whose project you cannot see, or that no longer exists. Open one and move it to a project."
              className="flex-1 min-w-0 h-full flex items-center gap-2 px-2 text-left"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-slate-400 shrink-0" aria-hidden>
                <circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" />
              </svg>
              <span className="truncate text-sm italic text-slate-500">Unassigned</span>
              {countBadge(counts.unassigned, unassignedSelected)}
            </button>
          </div>
        )}
      </div>

      {/* The copyright mark is pinned to the window's bottom-left corner
          (CopyrightFooter), right where this row ends up on the page, so the
          row sits above it there. The drawer covers the mark instead. */}
      <div className={`border-t border-slate-200 px-2 pt-1.5 shrink-0 ${onClose ? 'pb-1.5' : 'pb-10'}`}>
        <button
          type="button"
          onClick={onShowArchived}
          className={`${rowBase} w-full px-2 gap-2 text-left ${idleCls}`}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-slate-400 shrink-0" aria-hidden>
            <polyline points="21 8 21 21 3 21 3 8" /><rect x="1" y="3" width="22" height="5" /><line x1="10" y1="12" x2="14" y2="12" />
          </svg>
          <span className="text-sm text-slate-600">Archived projects</span>
          <span className="ml-auto text-[11px] tabular-nums text-slate-500">{archivedCount}</span>
        </button>
      </div>
    </nav>
  );
}
