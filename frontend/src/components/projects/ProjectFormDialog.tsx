/**
 * New Project / Edit Project form.
 *
 * Moved here from the retired Projects page (2026-11-10) when projects and
 * folders moved into the panel on the Pipelines page. The fields, the
 * admin-only rule and the "keep metadata keys this form does not know" merge
 * are unchanged; only where the form opens from is new.
 */
import { useEffect, useState } from 'react';
import { api } from '../../api/client';
import { toast } from '../Toast';

export const PROJECT_COLORS = [
  '#6366f1', '#3b82f6', '#10b981', '#f59e0b', '#ef4444',
  '#8b5cf6', '#ec4899', '#14b8a6', '#f97316', '#06b6d4',
];

// Line-style SVGs that inherit currentColor, so a project's tile takes the
// project's colour without a separate asset.
const icon = (path: React.ReactNode, size = 18) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    {path}
  </svg>
);

const ICON_PATHS: Record<string, React.ReactNode> = {
  folder: <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />,
  database: <><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" /><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" /></>,
  chart: <><line x1="18" y1="20" x2="18" y2="10" /><line x1="12" y1="20" x2="12" y2="4" /><line x1="6" y1="20" x2="6" y2="14" /></>,
  gear: <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h0a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></>,
  rocket: <><path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z" /><path d="M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z" /><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0" /><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5" /></>,
  star: <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />,
  lightning: <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />,
  globe: <><circle cx="12" cy="12" r="10" /><line x1="2" y1="12" x2="22" y2="12" /><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" /></>,
  briefcase: <><rect x="2" y="7" width="20" height="14" rx="2" ry="2" /><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16" /></>,
  shield: <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />,
  box: <><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" /><polyline points="3.27 6.96 12 12.01 20.73 6.96" /><line x1="12" y1="22.08" x2="12" y2="12" /></>,
  activity: <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />,
};

export const PROJECT_ICONS: { id: string; label: string }[] = [
  { id: 'folder', label: 'Folder' },
  { id: 'database', label: 'Database' },
  { id: 'chart', label: 'Analytics' },
  { id: 'gear', label: 'Engineering' },
  { id: 'rocket', label: 'Launch' },
  { id: 'star', label: 'Featured' },
  { id: 'lightning', label: 'Fast' },
  { id: 'globe', label: 'Global' },
  { id: 'briefcase', label: 'Business' },
  { id: 'shield', label: 'Security' },
  { id: 'box', label: 'Inventory' },
  { id: 'activity', label: 'Activity' },
];

/** A project's icon, drawn in the current text colour. Unknown ids get the folder. */
export function ProjectIcon({ icon: id, size = 18 }: { icon?: string; size?: number }) {
  return icon(ICON_PATHS[id || 'folder'] || ICON_PATHS.folder, size);
}

export interface ProjectMetadata {
  cost_center?: string;
  sponsor?: string;
  department?: string;
  priority?: 'low' | 'medium' | 'high' | 'critical';
  tags?: string[];
  notes?: string;
}

export interface ProjectRecord {
  id: string;
  name: string;
  description?: string;
  owner?: string;
  owner_id?: string;
  color?: string;
  icon?: string;
  created_by?: string;
  team?: string;
  members?: string[];
  metadata?: ProjectMetadata & Record<string, unknown>;
  status?: 'active' | 'archived';
  archived_at?: string;
  archived_by?: string;
  pipeline_count?: number;
  updated_at?: string;
  created_at?: string;
}

interface FormState {
  name: string;
  description: string;
  color: string;
  icon: string;
  members: string[];
  metadata: ProjectMetadata;
}

const emptyForm = (): FormState => ({
  name: '', description: '', color: '#6366f1', icon: 'folder',
  members: [],
  metadata: { cost_center: '', sponsor: '', department: '', priority: 'medium', tags: [], notes: '' },
});

const formFor = (p: ProjectRecord): FormState => {
  const m = p.metadata || {};
  return {
    name: p.name || '',
    description: p.description || '',
    color: p.color || '#6366f1',
    icon: p.icon || 'folder',
    members: p.members || [],
    metadata: {
      cost_center: m.cost_center || '', sponsor: m.sponsor || '',
      department: m.department || '', priority: (m.priority as ProjectMetadata['priority']) || 'medium',
      tags: m.tags || [], notes: m.notes || '',
    },
  };
};

interface Props {
  /** The project to edit, or null to create one. */
  project: ProjectRecord | null;
  onClose: () => void;
  /** Called after the server accepted the change, with what it returned. */
  onSaved: (project: ProjectRecord) => void;
}

export default function ProjectFormDialog({ project, onClose, onSaved }: Props) {
  const editing = !!project;
  const [form, setForm] = useState<FormState>(() => (project ? formFor(project) : emptyForm()));
  const [tagInput, setTagInput] = useState('');
  const [allUsers, setAllUsers] = useState<any[]>([]);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.listUsers().then((u) => setAllUsers(Array.isArray(u) ? u : [])).catch(() => setAllUsers([]));
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const submit = async () => {
    if (!form.name.trim() || saving) return;
    setSaving(true);
    try {
      if (project) {
        const saved = await api.updateProject(project.id, {
          name: form.name, description: form.description, color: form.color, icon: form.icon,
          members: form.members,
          // The server replaces metadata wholesale, and this form only knows
          // six keys. Start from what the project already has so a key set
          // elsewhere (the API, an import) isn't wiped by an unrelated edit.
          metadata: { ...(project.metadata || {}), ...form.metadata },
        });
        toast.success('Project updated');
        onSaved(saved || { ...project, name: form.name });
      } else {
        const saved = await api.createProject({
          name: form.name, description: form.description, color: form.color, icon: form.icon,
          members: form.members,
          metadata: form.metadata as Record<string, any>,
        });
        toast.success('Project created', saved?.name || form.name);
        onSaved(saved);
      }
    } catch (err: any) {
      toast.error(editing ? 'Could not update the project' : 'Could not create the project', err?.message || 'Try again');
    } finally {
      setSaving(false);
    }
  };

  const setMeta = (patch: Partial<ProjectMetadata>) => setForm((f) => ({ ...f, metadata: { ...f.metadata, ...patch } }));

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="project-form-title"
        className="bg-white rounded-2xl shadow-xl w-full max-w-2xl p-6 max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="project-form-title" className="text-lg font-bold text-slate-800 mb-4">
          {editing ? 'Edit Project' : 'New Project'}
        </h2>

        <div className="space-y-4">
          <div>
            <label htmlFor="project-form-name" className="text-xs font-semibold text-slate-600 mb-1 block">Name *</label>
            <input
              id="project-form-name"
              type="text" value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); void submit(); } }}
              placeholder="My Pipeline Project"
              className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-indigo-300 focus:border-indigo-300 outline-none"
              autoFocus
            />
          </div>
          <div>
            <label htmlFor="project-form-description" className="text-xs font-semibold text-slate-600 mb-1 block">Description</label>
            <textarea
              id="project-form-description"
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
              placeholder="What is this project for?"
              className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-indigo-300 focus:border-indigo-300 outline-none resize-none h-16"
            />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <span className="text-xs font-semibold text-slate-600 mb-1 block">Color</span>
              <div className="flex gap-2 flex-wrap">
                {PROJECT_COLORS.map((c) => (
                  <button
                    key={c}
                    type="button"
                    aria-label={`Colour ${c}`}
                    aria-pressed={form.color === c}
                    onClick={() => setForm({ ...form, color: c })}
                    className={`w-7 h-7 rounded-lg transition-all ${form.color === c ? 'ring-2 ring-offset-2 ring-slate-400 scale-110' : 'hover:scale-105'}`}
                    style={{ background: c }}
                  />
                ))}
              </div>
            </div>
            <div>
              <span className="text-xs font-semibold text-slate-600 mb-1 block">Icon</span>
              <div className="flex gap-2 flex-wrap">
                {PROJECT_ICONS.map((i) => (
                  <button
                    key={i.id}
                    type="button"
                    onClick={() => setForm({ ...form, icon: i.id })}
                    aria-pressed={form.icon === i.id}
                    className={`w-8 h-8 rounded-lg flex items-center justify-center text-sm transition-all ${form.icon === i.id ? 'bg-indigo-50 ring-2 ring-indigo-300' : 'bg-slate-50 hover:bg-slate-100'}`}
                    title={i.label}
                    aria-label={i.label}
                  >
                    <ProjectIcon icon={i.id} />
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div className="border-t border-slate-200 pt-3">
            <h3 className="text-xs font-bold text-slate-500 uppercase tracking-wider mb-3">Project Governance <span className="text-slate-300 font-normal normal-case">(optional)</span></h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label htmlFor="project-form-cost" className="text-xs font-semibold text-slate-500 mb-1 block">Cost Center</label>
                <input id="project-form-cost" type="text" value={form.metadata.cost_center || ''} onChange={(e) => setMeta({ cost_center: e.target.value })} placeholder="e.g. CC-4500" className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-indigo-300 focus:border-indigo-300 outline-none" />
              </div>
              <div>
                <label htmlFor="project-form-sponsor" className="text-xs font-semibold text-slate-500 mb-1 block">Project Sponsor</label>
                <input id="project-form-sponsor" type="text" value={form.metadata.sponsor || ''} onChange={(e) => setMeta({ sponsor: e.target.value })} placeholder="e.g. VP Engineering" className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-indigo-300 focus:border-indigo-300 outline-none" />
              </div>
              <div>
                <label htmlFor="project-form-department" className="text-xs font-semibold text-slate-500 mb-1 block">Department</label>
                <input id="project-form-department" type="text" value={form.metadata.department || ''} onChange={(e) => setMeta({ department: e.target.value })} placeholder="e.g. Data Engineering" className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-indigo-300 focus:border-indigo-300 outline-none" />
              </div>
              <div>
                <label htmlFor="project-form-priority" className="text-xs font-semibold text-slate-500 mb-1 block">Priority</label>
                <select id="project-form-priority" value={form.metadata.priority || 'medium'} onChange={(e) => setMeta({ priority: e.target.value as ProjectMetadata['priority'] })} className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-indigo-300 focus:border-indigo-300 outline-none bg-white">
                  <option value="low">Low</option>
                  <option value="medium">Medium</option>
                  <option value="high">High</option>
                  <option value="critical">Critical</option>
                </select>
              </div>
            </div>

            <div className="mt-3">
              <label htmlFor="project-form-tag" className="text-xs font-semibold text-slate-500 mb-1 block">Tags</label>
              <div className="flex flex-wrap gap-1.5 mb-1.5">
                {(form.metadata.tags || []).map((tag, i) => (
                  <span key={`${tag}-${i}`} className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium bg-indigo-50 text-indigo-600 border border-indigo-200 rounded-full">
                    {tag}
                    <button type="button" aria-label={`Remove tag ${tag}`} onClick={() => setMeta({ tags: (form.metadata.tags || []).filter((_, idx) => idx !== i) })} className="text-indigo-400 hover:text-indigo-600">&times;</button>
                  </span>
                ))}
              </div>
              <input
                id="project-form-tag"
                type="text" value={tagInput}
                onChange={(e) => setTagInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && tagInput.trim()) {
                    e.preventDefault();
                    setMeta({ tags: [...(form.metadata.tags || []), tagInput.trim()] });
                    setTagInput('');
                  }
                }}
                placeholder="Type tag and press Enter"
                className="w-full px-3 py-1.5 text-xs border border-slate-200 rounded-lg focus:ring-1 focus:ring-indigo-300 outline-none"
              />
            </div>

            <div className="mt-3">
              <label htmlFor="project-form-notes" className="text-xs font-semibold text-slate-500 mb-1 block">Notes</label>
              <textarea id="project-form-notes" value={form.metadata.notes || ''} onChange={(e) => setMeta({ notes: e.target.value })} placeholder="Internal notes, compliance requirements, etc." className="w-full px-3 py-2 text-xs border border-slate-200 rounded-lg focus:ring-2 focus:ring-indigo-300 focus:border-indigo-300 outline-none resize-none h-14" />
            </div>
          </div>

          {/* Per-project access is a multi-user concern: shown only when more
              than one account exists, as before. */}
          {allUsers.length > 1 && (
            <div className="border-t border-slate-200 pt-3">
              <h3 className="text-xs font-bold text-slate-500 uppercase tracking-wider mb-3">Project access</h3>
              {form.members.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mb-3">
                  {form.members.map((uid) => {
                    const u = allUsers.find((x) => x.id === uid);
                    const label = u?.name || u?.email || uid;
                    return (
                      <span key={uid} className="inline-flex items-center gap-1.5 pl-1.5 pr-2 py-1 text-xs font-medium bg-emerald-50 text-emerald-700 border border-emerald-200 rounded-full">
                        <span className="w-5 h-5 rounded-full bg-emerald-200 text-emerald-700 flex items-center justify-center text-[10px] font-bold">{String(label).charAt(0).toUpperCase()}</span>
                        {label}
                        <button type="button" aria-label={`Remove ${label}`} onClick={() => setForm({ ...form, members: form.members.filter((m) => m !== uid) })} className="text-emerald-400 hover:text-red-500 ml-0.5">&times;</button>
                      </span>
                    );
                  })}
                </div>
              )}
              <div className="flex flex-wrap gap-1.5">
                {allUsers.filter((u) => !form.members.includes(u.id)).map((u) => (
                  <button
                    key={u.id}
                    type="button"
                    onClick={() => setForm({ ...form, members: [...form.members, u.id] })}
                    className="inline-flex items-center gap-1.5 pl-1.5 pr-2 py-1 text-xs font-medium bg-slate-50 text-slate-500 border border-slate-200 rounded-full hover:bg-indigo-50 hover:text-indigo-600 hover:border-indigo-200 transition-colors"
                  >
                    <span className="w-5 h-5 rounded-full bg-slate-200 text-slate-500 flex items-center justify-center text-[10px] font-bold">{(u.name || u.email || '?').charAt(0).toUpperCase()}</span>
                    {u.name || u.email}
                    <span className="text-slate-300">+</span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 mt-6 pt-4 border-t border-slate-100">
          <button type="button" onClick={onClose} className="px-4 py-2 text-sm text-slate-600 hover:text-slate-800 rounded-lg hover:bg-slate-50">
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={!form.name.trim() || saving}
            className="px-4 py-2 text-sm text-white font-semibold rounded-lg disabled:opacity-50"
            style={{ background: 'linear-gradient(135deg, #6366f1, #4f46e5)' }}
          >
            {saving ? 'Saving…' : editing ? 'Save Changes' : 'Create Project'}
          </button>
        </div>
      </div>
    </div>
  );
}
