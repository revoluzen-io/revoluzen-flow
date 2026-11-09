/**
 * The Bin tab on the Pipelines page: pipelines that were deleted and can still
 * be restored. Server-backed (GET /workflows/bin), so every user and browser of
 * the workspace sees the same list. A pipeline stays here until the retention
 * window ends (default 30 days, then a daily job removes it), someone deletes it
 * from the Bin, or an admin empties the Bin.
 *
 * Presentational only: the page owns the data and the API calls.
 */
import type { WorkflowBinItem } from '../../api/client';
import { binBanner, binEmptyBody, removalLabel } from '../../utils/binText';

interface PipelinesBinProps {
  items: WorkflowBinItem[];
  retentionDays: number;
  loading: boolean;
  /** Restore and Delete forever (the same rank that can delete). */
  canChange: boolean;
  /** Empty Bin (admins; the server enforces it). */
  canEmpty: boolean;
  /** The row whose request is in flight. */
  busyId: string | null;
  projectLabel: (projectId: string) => string;
  formatTime: (iso?: string) => string;
  onRestore: (item: WorkflowBinItem) => void;
  onPurge: (item: WorkflowBinItem) => void;
  onEmpty: () => void;
}

const TrashIcon = ({ size = 14, color = 'currentColor' }: { size?: number; color?: string }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2">
    <polyline points="3 6 5 6 21 6" /><path d="M19 6l-2 14H7L5 6" /><path d="M9 6V4h6v2" />
  </svg>
);

export default function PipelinesBin({
  items, retentionDays, loading, canChange, canEmpty, busyId,
  projectLabel, formatTime, onRestore, onPurge, onEmpty,
}: PipelinesBinProps) {
  if (items.length === 0) {
    return (
      <div className="bg-white rounded-2xl border border-slate-200 shadow-sm text-center py-16 px-8" data-testid="bin-empty">
        <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-slate-100 flex items-center justify-center">
          <TrashIcon size={24} color="#94a3b8" />
        </div>
        <h2 className="text-lg font-bold text-slate-700 mb-2">{loading ? 'Loading the Bin…' : 'The Bin is empty'}</h2>
        {!loading && <p className="text-sm text-slate-500">{binEmptyBody(retentionDays)}</p>}
      </div>
    );
  }

  return (
    <div className="bg-white rounded-2xl border border-slate-200 overflow-x-auto shadow-sm" data-testid="bin">
      <div className="px-5 py-3 bg-slate-50 border-b border-slate-200 flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 text-xs text-slate-600">
          <TrashIcon color="#64748b" />
          <span className="font-semibold text-slate-700">
            {items.length} pipeline{items.length === 1 ? '' : 's'} in the Bin
          </span>
          <span>{binBanner(retentionDays)}</span>
        </div>
        {canEmpty && (
          <button
            onClick={onEmpty}
            className="text-xs font-medium text-red-500 hover:text-red-700 transition-colors"
          >
            Empty Bin
          </button>
        )}
      </div>
      <table className="w-full border-collapse">
        <thead>
          <tr className="bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900 border-b-2 border-amber-400/40">
            <th className="text-left px-4 py-3 text-xs font-bold text-amber-300 uppercase tracking-wider">Name</th>
            <th className="text-left px-3 py-3 text-xs font-bold text-amber-300 uppercase tracking-wider">Project</th>
            <th className="text-left px-3 py-3 text-xs font-bold text-amber-300 uppercase tracking-wider">Deleted</th>
            <th className="text-left px-3 py-3 text-xs font-bold text-amber-300 uppercase tracking-wider">Removed for good</th>
            <th className="w-56 px-3 py-3 text-right text-xs font-bold text-amber-300 uppercase tracking-wider">Actions</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const busy = busyId === item.id;
            const removal = removalLabel(item.purge_at);
            return (
              <tr key={item.id} data-testid={`bin-row-${item.id}`} className="border-b border-slate-100 hover:bg-slate-50/60 transition-colors">
                <td className="px-4 py-3 max-w-[300px]">
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-slate-700 truncate" title={item.name}>{item.name}</div>
                    <div className="text-xs text-slate-500 font-mono">{item.id.slice(0, 8)}</div>
                  </div>
                </td>
                <td className="px-3 py-3">
                  <span className="text-xs text-slate-600">{projectLabel(item.project_id || 'default')}</span>
                </td>
                <td className="px-3 py-3">
                  <div className="text-xs text-slate-600">{formatTime(item.deleted_at)}</div>
                  {item.deleted_by_name && <div className="text-xs text-slate-500">by {item.deleted_by_name}</div>}
                </td>
                <td className="px-3 py-3">
                  <span className={`text-xs font-medium ${removal === 'Due now' ? 'text-red-500' : 'text-slate-600'}`}>
                    {removal}
                  </span>
                </td>
                <td className="px-3 py-3 text-right">
                  {canChange ? (
                    <div className="flex items-center justify-end gap-1.5">
                      <button
                        onClick={() => onRestore(item)}
                        disabled={busy}
                        className="px-2 py-1 text-xs font-semibold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg hover:bg-emerald-100 transition-colors disabled:opacity-50"
                        title="Restore — back as a draft, with its saved versions"
                      >
                        {busy ? 'Working…' : 'Restore'}
                      </button>
                      <button
                        onClick={() => onPurge(item)}
                        disabled={busy}
                        className="px-2 py-1 text-xs font-medium text-red-500 hover:text-red-700 hover:bg-red-50 rounded-lg transition-colors disabled:opacity-50"
                        title="Remove it from the Bin for good. This cannot be undone."
                      >
                        Delete forever
                      </button>
                    </div>
                  ) : (
                    <span className="text-xs text-slate-400">—</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
