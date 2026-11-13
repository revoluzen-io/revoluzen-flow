/**
 * The settings of an event trigger, shown in the Schedule dialog under
 * "When a file arrives" and "After another pipeline".
 *
 * Both forms are controlled: the dialog owns the draft and saves it, so its
 * one Save button and the unsafe-sink acknowledgement work the same for every
 * kind of schedule. Wording and the saved shape live in `triggerText.ts`.
 */
import { useEffect, useMemo, useState } from 'react';
import { api } from '../../api/client';
import {
  FILE_LOCATIONS,
  OUTCOME_OPTIONS,
  POLL_CHOICES,
  guessFileParameter,
  type AfterTriggerDraft,
  type FileLocation,
  type FileTriggerDraft,
} from './triggerText';

const LABEL = 'text-xs font-bold text-slate-500 uppercase tracking-wider block mb-1.5';
const INPUT = 'w-full px-3 py-2 text-sm border border-slate-200 rounded-lg outline-none focus:ring-2 focus:ring-emerald-200 text-slate-700 bg-white';
const HINT = 'mt-1 text-[11px] text-slate-500';

interface ConnectionOption {
  id: string;
  name: string;
  type: string;
  config?: Record<string, unknown>;
}

interface PreviewFile {
  name: string;
  size: number;
  modified: string | null;
}

type Preview =
  | { state: 'idle' }
  | { state: 'checking' }
  | { state: 'ok'; matching: number; total: number; files: PreviewFile[] }
  | { state: 'error'; message: string };

function fitsLocation(connection: ConnectionOption, location: FileLocation | ''): boolean {
  const option = FILE_LOCATIONS.find((l) => l.value === location);
  if (!option || !option.connectionTypes.includes(connection.type)) return false;
  // An FTP connection only counts when it is set to SFTP.
  if (location === 'sftp' && connection.type === 'ftp') {
    return String(connection.config?.protocol || '').toLowerCase() === 'sftp';
  }
  return true;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function FileTriggerForm({ pipelineId, draft, onChange, isNew }: {
  pipelineId: string;
  draft: FileTriggerDraft;
  onChange: (next: FileTriggerDraft) => void;
  /** A new trigger picks the parameter that looks meant for a file path. */
  isNew: boolean;
}) {
  const [connections, setConnections] = useState<ConnectionOption[]>([]);
  const [parameters, setParameters] = useState<{ name: string; type?: string }[] | null>(null);
  const [preview, setPreview] = useState<Preview>({ state: 'idle' });

  useEffect(() => {
    api.listConnections()
      .then((list: any) => setConnections(Array.isArray(list) ? list : []))
      .catch(() => setConnections([]));
  }, []);

  useEffect(() => {
    api.getWorkflow(pipelineId)
      .then((data: any) => {
        const wf = data?.workflow || data;
        setParameters(Array.isArray(wf?.parameters) ? wf.parameters : []);
      })
      .catch(() => setParameters([]));
  }, [pipelineId]);

  useEffect(() => {
    if (!isNew || !parameters || draft.parameter) return;
    const guess = guessFileParameter(parameters);
    if (guess) onChange({ ...draft, parameter: guess });
    // Only when the parameters arrive; later choices are the user's.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [parameters]);

  const location = FILE_LOCATIONS.find((l) => l.value === draft.location);
  const usable = useMemo(
    () => connections.filter((c) => fitsLocation(c, draft.location)),
    [connections, draft.location],
  );
  const textParameters = (parameters || []).filter((p) => !p.type || p.type === 'string');

  const set = (patch: Partial<FileTriggerDraft>) => {
    setPreview({ state: 'idle' });
    onChange({ ...draft, ...patch });
  };

  const checkFolder = async () => {
    setPreview({ state: 'checking' });
    try {
      const result: any = await api.post('/api/schedules/check-folder', {
        file_location: draft.location,
        file_connection_id: draft.location === 'local' ? '' : draft.connectionId,
        file_folder: draft.folder.trim(),
        file_pattern: draft.pattern.trim() || '*',
      });
      setPreview({
        state: 'ok',
        matching: Number(result?.matching || 0),
        total: Number(result?.total || 0),
        files: Array.isArray(result?.files) ? result.files : [],
      });
    } catch (err: any) {
      setPreview({ state: 'error', message: err?.message || 'The folder could not be checked.' });
    }
  };

  const canCheck = !!location && (location.connectionTypes.length === 0 ? !!draft.folder.trim() : !!draft.connectionId);

  return (
    <div className="space-y-4" data-testid="file-trigger-form">
      <div>
        <label htmlFor="trigger-location" className={LABEL}>Where the files arrive</label>
        <select
          id="trigger-location"
          value={draft.location}
          onChange={(e) => {
            const next = e.target.value as FileLocation | '';
            const keep = connections.find((c) => c.id === draft.connectionId);
            set({ location: next, connectionId: keep && fitsLocation(keep, next) ? keep.id : '' });
          }}
          className={INPUT}
        >
          <option value="">Choose a place</option>
          {FILE_LOCATIONS.map((l) => <option key={l.value} value={l.value}>{l.label}</option>)}
        </select>
      </div>

      {location && location.connectionTypes.length > 0 && (
        <div>
          <label htmlFor="trigger-connection" className={LABEL}>Connection</label>
          <select
            id="trigger-connection"
            value={draft.connectionId}
            onChange={(e) => set({ connectionId: e.target.value })}
            className={INPUT}
          >
            <option value="">Choose a saved connection</option>
            {usable.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          {usable.length === 0 && (
            <p className={HINT}>
              There is no saved {location.connectionName ?? location.label} connection yet. Add one on the Connections page first.
            </p>
          )}
        </div>
      )}

      {location && (
        <div>
          <label htmlFor="trigger-folder" className={LABEL}>{location.folderLabel}</label>
          <input
            id="trigger-folder"
            type="text"
            value={draft.folder}
            onChange={(e) => set({ folder: e.target.value })}
            placeholder={location.folderPlaceholder}
            className={`${INPUT} font-mono`}
          />
          <p className={HINT}>{location.folderHint} Only files directly in this folder count.</p>
        </div>
      )}

      <div>
        <label htmlFor="trigger-pattern" className={LABEL}>File names</label>
        <input
          id="trigger-pattern"
          type="text"
          value={draft.pattern}
          onChange={(e) => set({ pattern: e.target.value })}
          placeholder="orders_*.csv"
          className={`${INPUT} font-mono`}
        />
        <p className={HINT}>* stands for any characters, so orders_*.csv matches orders_2026-11-11.csv. Separate several with ;</p>
      </div>

      {canCheck && (
        <div>
          <button
            type="button"
            onClick={checkFolder}
            disabled={preview.state === 'checking'}
            className="px-3 py-1.5 text-xs font-semibold rounded-lg bg-white text-slate-700 border border-slate-300 hover:bg-slate-50 disabled:opacity-50"
          >
            {preview.state === 'checking' ? 'Checking…' : 'Check the folder now'}
          </button>
          {preview.state === 'ok' && (
            <div className="mt-2 text-xs text-slate-600" data-testid="folder-preview">
              {preview.matching === 0
                ? `No matching files there now (${preview.total} file${preview.total === 1 ? '' : 's'} in all). New ones will start runs.`
                : `${preview.matching} matching file${preview.matching === 1 ? '' : 's'} there now:`}
              {preview.files.length > 0 && (
                <ul className="mt-1 space-y-0.5 font-mono text-[11px]">
                  {preview.files.slice(0, 5).map((f) => (
                    <li key={f.name} className="truncate">{f.name} <span className="text-slate-400">({formatSize(f.size)})</span></li>
                  ))}
                  {preview.files.length > 5 && <li className="text-slate-400">and {preview.matching - 5} more</li>}
                </ul>
              )}
            </div>
          )}
          {preview.state === 'error' && (
            <p role="alert" className="mt-2 text-xs text-red-600">{preview.message}</p>
          )}
        </div>
      )}

      <div>
        <label htmlFor="trigger-parameter" className={LABEL}>Give the file's path to</label>
        <select
          id="trigger-parameter"
          value={draft.parameter}
          onChange={(e) => set({ parameter: e.target.value })}
          className={INPUT}
        >
          <option value="">Don't pass the path</option>
          {textParameters.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
        </select>
        <p className={HINT}>
          {textParameters.length === 0 && parameters !== null
            ? 'This pipeline has no text parameters. To read the file that arrived, add one (such as file_path) in the Editor and use it as the Source step’s path.'
            : draft.parameter
              ? `Each run gets the path in this parameter. The Source step reads it as \${param.${draft.parameter}}.`
              : 'Without it, the pipeline runs as saved, and has to find the files itself.'}
        </p>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label htmlFor="trigger-settle" className={LABEL}>Wait (seconds)</label>
          <input
            id="trigger-settle"
            type="number"
            min={0}
            max={86400}
            value={draft.settleSeconds}
            onChange={(e) => set({ settleSeconds: Math.max(0, parseInt(e.target.value || '0', 10)) })}
            className={INPUT}
          />
        </div>
        <div>
          <label htmlFor="trigger-poll" className={LABEL}>Look for new files</label>
          <select
            id="trigger-poll"
            value={draft.pollSeconds}
            onChange={(e) => set({ pollSeconds: parseInt(e.target.value, 10) })}
            className={INPUT}
          >
            {POLL_CHOICES.map((c) => <option key={c.seconds} value={c.seconds}>{c.label}</option>)}
          </select>
        </div>
      </div>
      <p className={`${HINT} -mt-2`}>
        A file has to stop changing for the waiting time before its run starts, so one still being copied in is not read half-written.
      </p>

      <label className="flex items-start gap-2.5 cursor-pointer">
        <input
          type="checkbox"
          checked={draft.includeExisting}
          onChange={(e) => set({ includeExisting: e.target.checked })}
          className="mt-0.5"
        />
        <span>
          <span className="block text-sm font-semibold text-slate-700">Also run for files already in the folder</span>
          <span className="block text-xs text-slate-500 mt-0.5">
            Off: only files that arrive after you save count. Each file runs once, oldest first, one at a time.
          </span>
        </span>
      </label>
    </div>
  );
}

interface PipelineOption {
  id: string;
  name: string;
  status?: string;
}

export function AfterTriggerForm({ pipelineId, draft, onChange }: {
  pipelineId: string;
  draft: AfterTriggerDraft;
  onChange: (next: AfterTriggerDraft) => void;
}) {
  const [pipelines, setPipelines] = useState<PipelineOption[] | null>(null);

  useEffect(() => {
    api.listWorkflows()
      .then((list: any) => setPipelines(
        (Array.isArray(list) ? list : [])
          .filter((w: any) => w.id !== pipelineId && w.status !== 'archived')
          .map((w: any) => ({ id: w.id, name: w.name, status: w.status }))
          .sort((a: PipelineOption, b: PipelineOption) => a.name.localeCompare(b.name)),
      ))
      .catch(() => setPipelines([]));
  }, [pipelineId]);

  return (
    <div className="space-y-4" data-testid="after-trigger-form">
      <div>
        <label htmlFor="trigger-upstream" className={LABEL}>Run after</label>
        <select
          id="trigger-upstream"
          value={draft.upstreamId}
          onChange={(e) => onChange({ ...draft, upstreamId: e.target.value })}
          className={INPUT}
        >
          <option value="">Choose a pipeline</option>
          {(pipelines || []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}{p.status && p.status !== 'published' ? ' (not published)' : ''}
            </option>
          ))}
        </select>
        {pipelines !== null && pipelines.length === 0 && (
          <p className={HINT}>There are no other pipelines to run after yet.</p>
        )}
      </div>

      <div>
        <span className={LABEL}>When it</span>
        <div role="radiogroup" aria-label="When it" className="grid grid-cols-3 gap-2">
          {OUTCOME_OPTIONS.map((o) => (
            <button
              key={o.value}
              type="button"
              role="radio"
              aria-checked={draft.outcome === o.value}
              onClick={() => onChange({ ...draft, outcome: o.value })}
              className={`px-2 py-2 rounded-lg border text-xs font-medium transition-all ${
                draft.outcome === o.value
                  ? 'border-emerald-300 bg-emerald-50 text-emerald-700'
                  : 'border-slate-200 hover:border-slate-300 text-slate-600'
              }`}
            >
              {o.label}
            </button>
          ))}
        </div>
      </div>

      <p className={HINT}>
        Its runs started by a schedule, a file, another pipeline or another application count. Runs someone starts by hand, and runs someone stops, do not.
        If this pipeline is still running when the other one finishes again, it runs once more afterwards.
      </p>
    </div>
  );
}
