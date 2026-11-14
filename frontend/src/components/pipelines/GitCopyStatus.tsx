import { useCallback, useEffect, useState } from 'react';
import { api, type GitCopyRecord } from '../../api/client';
import { toast } from '../Toast';
import TimeAgo from '../shared/TimeAgo';

/**
 * How the newest copy of this pipeline to the customer's Git repository went.
 * Says nothing when Git copy is not set up.
 */
export default function GitCopyStatus({ pipelineId, refreshKey = 0, canRetry = false }: {
  pipelineId: string;
  /** Change this after a publish, so the new copy is picked up. */
  refreshKey?: number;
  canRetry?: boolean;
}) {
  const [latest, setLatest] = useState<GitCopyRecord | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [retrying, setRetrying] = useState(false);

  const load = useCallback(async () => {
    try {
      const state = await api.getPipelineGitCopy(pipelineId);
      setEnabled(state.enabled);
      setLatest(state.latest);
    } catch {
      setEnabled(false);
      setLatest(null);
    }
  }, [pipelineId]);

  useEffect(() => { void load(); }, [load, refreshKey]);

  // A copy is made in the background, so look again until it has finished.
  const inProgress = latest?.status === 'pending' || latest?.status === 'working';
  useEffect(() => {
    if (!inProgress) return;
    const timer = setInterval(() => { void load(); }, 2500);
    return () => clearInterval(timer);
  }, [inProgress, load]);

  const retry = async () => {
    setRetrying(true);
    try {
      await api.retryPipelineGitCopy(pipelineId);
      await load();
    } catch (err: any) {
      toast.error('Could not try again', err?.message || 'Try again in a moment.');
    }
    setRetrying(false);
  };

  if (!enabled || !latest) return null;

  if (inProgress) {
    return <span className="inline-flex items-center gap-1.5 text-slate-600" data-testid="git-copy-status">Saving a copy to Git…</span>;
  }
  if (latest.status === 'failed') {
    return (
      <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1" data-testid="git-copy-status">
        <span className="font-semibold text-red-700">Not saved to Git.</span>
        <span className="text-slate-600">{latest.error}</span>
        {latest.will_retry_at && <span className="text-slate-500">Flow will try again.</span>}
        {canRetry && (
          <button type="button" onClick={() => void retry()} disabled={retrying} className="font-semibold text-blue-700 underline hover:text-blue-900 disabled:opacity-50">
            Try again now
          </button>
        )}
      </span>
    );
  }
  if (latest.status === 'saved' || latest.status === 'unchanged') {
    const short = latest.commit_sha.slice(0, 7);
    return (
      <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-1 text-slate-600" data-testid="git-copy-status">
        <span className="font-semibold text-emerald-700">Saved to Git</span>
        {short && (latest.commit_url
          ? <a href={latest.commit_url} target="_blank" rel="noreferrer" className="font-mono underline hover:text-slate-900">{short}</a>
          : <span className="font-mono">{short}</span>)}
        <TimeAgo value={latest.updated_at} className="!text-slate-500" />
      </span>
    );
  }
  return null;
}
