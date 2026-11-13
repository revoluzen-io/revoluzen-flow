/**
 * A connection's History tab: every kept version, newest first, compared
 * setting by setting with the connection as it is now, and restored from.
 *
 * Passwords and keys are never kept. The history shows only whether a
 * version's secret is the same as today's, and a restore keeps today's
 * password unless the old one is typed in. Before restoring, the dialog
 * lists the pipelines that use the connection.
 */
import { useEffect, useMemo, useState, type ReactNode } from 'react';

import {
  api,
  type ConnectionPipelineUse,
  type ConnectionRestoreResult,
  type ConnectionVersionHistory,
  type ConnectionVersionItem,
} from '../../api/client';
import { toast } from '../Toast';
import { changedSummary, diffConnection, type ConnectionDiff, type ConnectionDiffRow, type DiffNames } from './connectionDiff';

/** "Today, 11:40", "Yesterday, 09:10", "3 Nov, 15:02". */
export function historyWhen(iso: string, now: Date = new Date()): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === now.toDateString()) return `Today, ${time}`;
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return `Yesterday, ${time}`;
  const date = d.toLocaleDateString(undefined, {
    day: 'numeric',
    month: 'short',
    ...(d.getFullYear() !== now.getFullYear() ? { year: 'numeric' as const } : {}),
  });
  return `${date}, ${time}`;
}

function author(v: ConnectionVersionItem): string {
  return v.created_by && v.created_by !== 'user' ? v.created_by : 'Not recorded';
}

const ROLE: Record<string, string> = { source: 'Reads from it', destination: 'Writes to it', activity: 'Uses it' };
const STATUS: Record<string, string> = {
  draft: 'Draft', testing: 'Testing', published: 'Published', archived: 'Archived', failed: 'Last run failed',
};

const LOCK = 'M5 11h14v10H5z M8 11V7a4 4 0 0 1 8 0v4';

function Glyph({ d, size = 13, className = '' }: { d: string; size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={`shrink-0 ${className}`} aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

function Pill({ cls, children }: { cls: string; children: ReactNode }) {
  return <span className={`whitespace-nowrap rounded-full px-1.5 py-px text-[10.5px] font-bold ${cls}`}>{children}</span>;
}

function Value({ text }: { text: string | null }) {
  return text === null ? <span className="italic text-slate-400">Not set</span> : <>{text}</>;
}

export interface ConnectionHistoryProps {
  connectionId: string;
  connectionName: string;
  /** Edit rights in this environment. */
  canRestore: boolean;
  names: DiffNames;
  dark?: boolean;
  /** Called with the connection as saved by a restore. */
  onRestored: (connection: any) => void;
}

export default function ConnectionHistory({ connectionId, connectionName, canRestore, names, dark = false, onRestored }: ConnectionHistoryProps) {
  const [history, setHistory] = useState<ConnectionVersionHistory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [picked, setPicked] = useState<number | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [restoring, setRestoring] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    api.getConnectionVersions(connectionId)
      .then((h) => { if (!cancelled) setHistory(h); })
      .catch((e: any) => { if (!cancelled) setError(e?.message || 'The version history could not be read.'); });
    return () => { cancelled = true; };
  }, [connectionId, reloadKey]);

  // First look: the newest version that differs from now.
  useEffect(() => {
    if (!history) return;
    if (picked !== null && history.versions.some((v) => v.version === picked)) return;
    const first = history.versions.find((v) => !v.same_as_current) || history.versions[0];
    setPicked(first ? first.version : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [history]);

  const item = history?.versions.find((v) => v.version === picked) || null;
  const diff = useMemo(() => (item && history ? diffConnection(item.settings, history.current, names) : null), [item, history, names]);

  const card = `rounded-xl border shadow-sm ${dark ? 'bg-[#0f1726] border-white/[0.08]' : 'bg-white border-slate-200'}`;

  if (error) {
    return (
      <div className={`${card} p-8 text-center`}>
        <p className="text-sm font-semibold text-slate-700">The version history could not be read</p>
        <p className="mt-1 text-xs text-slate-500">{error}</p>
        <button type="button" onClick={() => setReloadKey((n) => n + 1)} className="mt-3 px-3 py-1.5 text-xs font-semibold rounded-lg border border-slate-300 bg-white hover:bg-slate-50">Try again</button>
      </div>
    );
  }
  if (!history) {
    return (
      <div className={`${card} p-10 grid place-items-center`} data-testid="connection-history">
        <div className="w-6 h-6 border-2 border-blue-300 border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }
  if (history.versions.length === 0) {
    return (
      <div className={`${card} p-6`} data-testid="connection-history">
        <p className="text-sm font-semibold text-slate-800">No versions yet</p>
        <p className="mt-1 text-[13px] text-slate-600 max-w-2xl">
          This connection was made before Flow kept versions of connections. The next time it is changed, Flow keeps
          its settings as they are now as version 1, and the change as version 2.
        </p>
        <p className="mt-2 text-xs text-slate-500">Passwords and keys are never kept in the history.</p>
      </div>
    );
  }

  const newest = history.versions[0];
  const keepNote = history.keep > 0
    ? `Flow keeps the newest ${history.keep} versions and the first one.`
    : 'Flow keeps every version.';

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[320px_minmax(0,1fr)] gap-5 items-start" data-testid="connection-history">
      {/* Every kept version, newest first. */}
      <aside className={`${card} flex flex-col lg:max-h-[calc(100vh-260px)] min-h-0`} aria-label="Versions">
        <div className="flex items-center justify-between px-4 pt-3 pb-2">
          <b className="text-[11px] font-bold uppercase tracking-wider text-slate-500">Versions</b>
          <span className="text-[11.5px] text-slate-500">{history.versions.length} kept</span>
        </div>
        <div className="flex-1 min-h-0 overflow-auto px-2 pb-2">
          {history.versions.map((v) => {
            const isPicked = v.version === picked;
            const isCurrent = v.version === newest.version && v.same_as_current;
            return (
              <button
                key={v.version}
                type="button"
                onClick={() => setPicked(v.version)}
                aria-pressed={isPicked}
                data-version={v.version}
                className={`w-full grid grid-cols-[30px_minmax(0,1fr)] gap-2.5 rounded-xl py-2 px-2 text-left ${isPicked ? 'bg-blue-50 ring-1 ring-blue-200' : 'hover:bg-slate-50'}`}
              >
                <span className={`grid h-[30px] w-[30px] place-items-center rounded-full border text-[11px] font-bold ${
                  isCurrent ? 'border-slate-800 bg-slate-800 text-white'
                    : isPicked ? 'border-blue-600 bg-blue-600 text-white'
                      : 'border-slate-200 bg-slate-50 text-slate-600'
                }`}>v{v.version}</span>
                <span className="min-w-0">
                  <span className="flex flex-wrap items-center gap-1">
                    <b className="mr-0.5 text-[13px] font-semibold text-slate-900">{v.note || 'Saved'}</b>
                    {isCurrent && <Pill cls="bg-slate-800 text-white">Current</Pill>}
                    {!isCurrent && v.same_as_current && <Pill cls="bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200">Same as now</Pill>}
                    {v.kind === 'restored' && <Pill cls="bg-violet-100 text-violet-700">Restored</Pill>}
                  </span>
                  <span className="mt-0.5 block text-[11.5px] text-slate-500">{author(v)} · {historyWhen(v.created_at)}</span>
                </span>
              </button>
            );
          })}
        </div>
        <p className="border-t border-slate-200 px-4 py-2.5 text-[11.5px] leading-snug text-slate-500">
          {keepNote} Passwords and keys are never kept: a version only shows whether its password was the same as today's.
        </p>
      </aside>

      {/* The picked version set against the settings now. */}
      <section className={`${card} min-w-0`} aria-label="Compare with now">
        {item && diff && (
          <>
            <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 px-5 py-3">
              <span className="rounded-full bg-blue-50 px-2.5 py-0.5 text-xs font-bold text-blue-700">Version {item.version}</span>
              <span className="text-[13px] font-semibold text-slate-700">compared with the settings now</span>
              <div className="ml-auto flex items-center gap-2">
                <label className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-600">
                  <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} className="rounded border-slate-300" />
                  Show every setting
                </label>
                {canRestore && !item.same_as_current && (
                  <button
                    type="button"
                    onClick={() => setRestoring(true)}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-700 hover:border-slate-400 hover:bg-slate-50"
                  >
                    <Glyph d="M3 12a9 9 0 1 0 3-6.7 M3 4v5h5" />
                    Restore version {item.version}…
                  </button>
                )}
              </div>
            </div>
            <CompareTable item={item} diff={diff} showAll={showAll} />
          </>
        )}
      </section>

      {restoring && item && diff && (
        <RestoreDialog
          connectionId={connectionId}
          connectionName={connectionName}
          item={item}
          diff={diff}
          usedBy={history.used_by}
          onClose={() => setRestoring(false)}
          onDone={(result) => {
            setRestoring(false);
            announce(result);
            onRestored(result.connection);
            setPicked(null);
            setReloadKey((n) => n + 1);
          }}
        />
      )}
    </div>
  );
}

function CompareTable({ item, diff, showAll }: { item: ConnectionVersionItem; diff: ConnectionDiff; showAll: boolean }) {
  const rows = showAll || diff.identical ? diff.rows : diff.changed;
  return (
    <div className="px-5 py-4" data-testid="connection-compare">
      <p className="text-sm font-semibold text-slate-800">
        {item.same_as_current || diff.identical
          ? `Version ${item.version} has the same settings as now.`
          : `${changedSummary(diff.changed.map((r) => r.label))} changed since version ${item.version}.`}
      </p>
      <table className="mt-3 w-full table-fixed border-separate border-spacing-y-1 text-[12.5px]">
        <thead>
          <tr className="text-left text-[11px] font-bold uppercase tracking-wider text-slate-500">
            <th className="w-48 pr-2 pb-1">Setting</th>
            <th className="px-2 pb-1">Version {item.version}</th>
            <th className="px-2 pb-1">Now</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className="align-top" data-changed={r.changed ? 'yes' : 'no'}>
              <th scope="row" className="pr-2 py-1 text-left font-semibold text-slate-600 break-words">
                <span className="inline-flex items-center gap-1">
                  {r.secretPath && <Glyph d={LOCK} size={12} className="text-slate-400" />}
                  {r.label}
                </span>
              </th>
              <td className={`px-2 py-1 rounded whitespace-pre-wrap break-words ${r.changed ? 'bg-red-50 text-red-800' : 'text-slate-600'}`}>
                <Value text={r.version} />
              </td>
              <td className={`px-2 py-1 rounded whitespace-pre-wrap break-words ${r.changed ? 'bg-emerald-50 text-emerald-800 font-semibold' : 'text-slate-600'}`}>
                <Value text={r.now} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!showAll && !diff.identical && diff.rows.length > diff.changed.length && (
        <p className="mt-2 text-xs text-slate-500">{diff.rows.length - diff.changed.length} other settings are the same.</p>
      )}
    </div>
  );
}

function announce(result: ConnectionRestoreResult) {
  if (result.unchanged) {
    toast.info(`Version ${result.restored_from} matches the settings now`, 'Nothing needed to change.');
    return;
  }
  const notes: string[] = [`Saved as version ${result.version}.`];
  if (result.kept_secrets.length) notes.push(`Kept today's ${result.kept_secrets.join(', ')}.`);
  if (result.missing_secrets.length) notes.push(`Restored without ${result.missing_secrets.join(', ')}: type it in Edit to add it.`);
  if (result.kept_settings.length) notes.push(`Kept today's ${result.kept_settings.join(', ')}: the one in version ${result.restored_from} no longer exists.`);
  if (result.renamed_to) notes.push(`Named "${result.renamed_to}": another connection has the old name now.`);
  toast.success(`Restored version ${result.restored_from}`, notes.join(' '));
}

function UsedByList({ usedBy }: { usedBy: ConnectionPipelineUse[] }) {
  if (usedBy.length === 0) return <p className="text-[13px] text-slate-600">No pipeline uses this connection now.</p>;
  return (
    <>
      <p className="text-[13px] text-slate-600">
        {usedBy.length === 1
          ? 'One pipeline uses it. Its next run uses the restored settings.'
          : `${usedBy.length} pipelines use it. Their next runs use the restored settings.`}
      </p>
      <ul className="mt-2 max-h-40 overflow-auto rounded-lg border border-slate-200 divide-y divide-slate-100" data-testid="restore-used-by">
        {usedBy.map((p) => (
          <li key={p.workflow_id} className="flex items-center gap-2 px-3 py-1.5 text-[12.5px]">
            <span className="min-w-0 flex-1 truncate font-semibold text-slate-800">{p.name || p.workflow_id}</span>
            <span className="shrink-0 text-slate-500">{ROLE[p.role] || 'Uses it'}</span>
            {p.deployed_version != null && <Pill cls="bg-emerald-100 text-emerald-700">Live in PROD</Pill>}
            <Pill cls={p.status === 'published' ? 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200' : 'bg-slate-100 text-slate-600'}>
              {STATUS[p.status] || p.status}
            </Pill>
          </li>
        ))}
      </ul>
    </>
  );
}

function RestoreDialog({
  connectionId,
  connectionName,
  item,
  diff,
  usedBy,
  onClose,
  onDone,
}: {
  connectionId: string;
  connectionName: string;
  item: ConnectionVersionItem;
  diff: ConnectionDiff;
  usedBy: ConnectionPipelineUse[];
  onClose: () => void;
  onDone: (result: ConnectionRestoreResult) => void;
}) {
  const [typed, setTyped] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const settings = diff.changed.filter((r) => !r.secretPath || r.secretState === 'only-now');
  const asks = diff.changed.filter((r) => r.secretState === 'changed' || r.secretState === 'only-version');
  const same = diff.rows.filter((r) => r.secretState === 'same');

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !busy) onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [busy, onClose]);

  const submit = async () => {
    setBusy(true);
    setError(null);
    const secrets: Record<string, string> = {};
    for (const r of asks) {
      const value = typed[r.secretPath!];
      if (value) secrets[r.secretPath!] = value;
    }
    try {
      const result = await api.restoreConnectionVersion(connectionId, item.version, secrets);
      onDone(result);
    } catch (e: any) {
      setError(e?.message || 'The version could not be restored.');
      setBusy(false);
    }
  };

  const change = (r: ConnectionDiffRow) => (r.secretState === 'only-now'
    ? <><b className="font-semibold text-slate-800">{r.label}</b>: removed (version {item.version} had none)</>
    : <><b className="font-semibold text-slate-800">{r.label}</b>: <Value text={r.now} /> → <Value text={r.version} /></>);

  return (
    <div className="fixed inset-0 z-[9999] flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-labelledby="restore-connection-title">
      <div className="absolute inset-0 bg-slate-900/40 backdrop-blur-sm" onClick={() => { if (!busy) onClose(); }} />
      <form
        onSubmit={(e) => { e.preventDefault(); void submit(); }}
        className="relative z-10 w-full max-w-lg max-h-[90vh] flex flex-col rounded-xl bg-white shadow-2xl border border-slate-200 overflow-hidden"
      >
        <div className="px-5 pt-5 pb-4 overflow-auto">
          <h2 id="restore-connection-title" className="text-base font-bold text-slate-800">Restore version {item.version}?</h2>
          <p className="mt-1.5 text-sm text-slate-600">
            <b className="font-semibold">{connectionName}</b> goes back to the settings it had in version {item.version}.
            The restore is saved as a new version, so nothing is lost.
          </p>

          {settings.length > 0 && (
            <section className="mt-4">
              <h3 className="text-[11px] font-bold uppercase tracking-wider text-slate-500">What changes</h3>
              <ul className="mt-1.5 space-y-1 text-[12.5px] text-slate-600" data-testid="restore-changes">
                {settings.map((r) => <li key={r.key} className="break-words">{change(r)}</li>)}
              </ul>
            </section>
          )}

          {(asks.length > 0 || same.length > 0) && (
            <section className="mt-4">
              <h3 className="text-[11px] font-bold uppercase tracking-wider text-slate-500">Passwords and keys</h3>
              {asks.length > 0 && (
                <p className="mt-1 text-[12.5px] text-slate-600">Flow does not keep old passwords, so it cannot put them back by itself.</p>
              )}
              {asks.map((r, i) => {
                const id = `restore-secret-${r.secretPath}`;
                return (
                  <div key={r.key} className="mt-2.5">
                    <label htmlFor={id} className="block text-[12.5px] font-semibold text-slate-700">{r.label}</label>
                    <input
                      id={id}
                      autoFocus={i === 0}
                      type="password"
                      autoComplete="new-password"
                      value={typed[r.secretPath!] || ''}
                      onChange={(e) => setTyped((t) => ({ ...t, [r.secretPath!]: e.target.value }))}
                      placeholder={r.secretState === 'changed' ? "Leave blank to keep today's" : 'Leave blank to restore without it'}
                      className="mt-1 w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none"
                    />
                    <p className="mt-1 text-[11.5px] text-slate-500">
                      {r.secretState === 'changed'
                        ? `Version ${item.version} had a different one. Type it to use it again, or leave this blank to keep today's.`
                        : `The connection has none now. Type version ${item.version}'s to restore it, or leave this blank to restore without it.`}
                    </p>
                  </div>
                );
              })}
              {same.length > 0 && (
                <p className="mt-2 text-[12.5px] text-slate-600">
                  {same.map((r) => r.label).join(', ')}: the same as now, kept.
                </p>
              )}
            </section>
          )}

          <section className="mt-4">
            <h3 className="text-[11px] font-bold uppercase tracking-wider text-slate-500">Pipelines that use this connection</h3>
            <div className="mt-1.5"><UsedByList usedBy={usedBy} /></div>
          </section>

          {error && <p className="mt-3 text-[12.5px] font-medium text-red-600" role="alert">{error}</p>}
        </div>
        <div className="px-5 py-3 bg-slate-50 border-t border-slate-200 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="px-4 py-2 text-sm font-semibold text-slate-700 bg-white border border-slate-300 rounded-lg hover:bg-slate-100 transition-colors disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy}
            className="px-4 py-2 text-sm font-bold text-white rounded-lg shadow-sm transition-colors bg-amber-500 hover:bg-amber-600 disabled:opacity-60"
          >
            {busy ? 'Restoring…' : `Restore version ${item.version}`}
          </button>
        </div>
      </form>
    </div>
  );
}
