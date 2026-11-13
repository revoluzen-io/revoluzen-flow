/**
 * Archived projects, with Restore and Delete now. This was the Archived section
 * of the retired Projects page; it opens from the Archived row at the bottom of
 * the project panel on the Pipelines page.
 */
import { useEffect, useState } from 'react';
import { uiConfirm } from '../../ui/dialog';
import { deleteProjectForGood, restoreProject } from './projectActions';
import { ProjectIcon, type ProjectRecord } from './ProjectFormDialog';

interface Props {
  projects: ProjectRecord[];
  /** Restore and Delete now are for admins; others see the list only. */
  canManage: boolean;
  onClose: () => void;
  /** Called after a restore or a delete, so the caller reloads its lists. */
  onChanged: () => void;
}

export default function ArchivedProjectsDialog({ projects, canManage, onClose, onChanged }: Props) {
  const [busyId, setBusyId] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const restore = async (p: ProjectRecord) => {
    setBusyId(p.id);
    const ok = await restoreProject(p);
    setBusyId(null);
    if (ok) onChanged();
  };

  const remove = async (p: ProjectRecord) => {
    const confirmed = await uiConfirm({
      title: `Delete "${p.name}"?`,
      message: 'This permanently deletes the project. It is refused while the project still holds pipelines, connections or credentials: move them to another project first.',
      danger: true,
      confirmLabel: 'Delete permanently',
    });
    if (!confirmed) return;
    setBusyId(p.id);
    const ok = await deleteProjectForGood(p);
    setBusyId(null);
    if (ok) onChanged();
  };

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="archived-projects-title"
        className="bg-white rounded-2xl shadow-xl w-full max-w-2xl max-h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-6 pt-5 pb-3 flex items-start justify-between gap-4">
          <div>
            <h2 id="archived-projects-title" className="text-lg font-bold text-slate-800">Archived projects</h2>
            <p className="text-xs text-slate-500 mt-1">
              Archived projects keep their pipelines, connections and credentials until you restore them or delete them.
            </p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="w-8 h-8 rounded-lg flex items-center justify-center text-slate-500 hover:bg-slate-100">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-6 pb-6">
          {projects.length === 0 ? (
            <p className="text-sm text-slate-500 py-8 text-center">No archived projects.</p>
          ) : (
            <ul className="divide-y divide-slate-100 border border-slate-200 rounded-lg">
              {projects.map((p) => (
                <li key={p.id} className="flex items-center gap-3 px-4 py-3">
                  <span className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0 opacity-60" style={{ background: `${p.color || '#6366f1'}1a`, color: p.color || '#6366f1' }}>
                    <ProjectIcon icon={p.icon} size={16} />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium text-slate-700 truncate" title={p.name}>{p.name}</div>
                    <div className="text-xs text-slate-500">
                      Archived {p.archived_at ? new Date(p.archived_at).toLocaleDateString() : ''}
                      {p.archived_by ? ` by ${p.archived_by}` : ''}
                    </div>
                  </div>
                  {canManage && (
                    <div className="flex gap-1.5 shrink-0">
                      <button
                        type="button"
                        disabled={busyId === p.id}
                        onClick={() => void restore(p)}
                        className="px-3 py-1.5 text-xs font-semibold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg hover:bg-emerald-100 disabled:opacity-50"
                      >
                        Restore
                      </button>
                      <button
                        type="button"
                        disabled={busyId === p.id}
                        onClick={() => void remove(p)}
                        className="px-3 py-1.5 text-xs font-semibold text-red-600 bg-red-50 border border-red-200 rounded-lg hover:bg-red-100 disabled:opacity-50"
                      >
                        Delete now
                      </button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
