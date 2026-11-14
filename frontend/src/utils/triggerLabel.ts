/**
 * What started a run, in the words the app shows.
 *
 * The backend stores short values on `triggered_by`, some with a suffix
 * (`replay:<execution id>`). Executions and the Pool's Run History read them
 * through here, so a run another application started says API everywhere rather
 * than `api` on some pages.
 */
const TRIGGER_LABELS: Record<string, string> = {
  manual: 'Manual',
  schedule: 'Scheduled',
  test: 'Pre-publish test',
  api: 'API',
  // A signed call to a published endpoint.
  webhook: 'Webhook',
  backfill: 'Backfill',
  // Started by an Execute Pipeline step in another pipeline.
  pipeline: 'Pipeline',
  // Event triggers set up in the Schedule dialog.
  file_arrival: 'File arrived',
  after_pipeline: 'After another pipeline',
  // The Editor's live run goes over a WebSocket.
  websocket: 'Editor',
  executor: 'Editor',
};

export function triggerLabel(raw?: string | null): string {
  const value = (raw || 'manual').trim().toLowerCase();
  if (value.startsWith('replay')) return 'Replay';
  if (value.startsWith('resume')) return 'Resume';
  return TRIGGER_LABELS[value] ?? raw ?? 'Manual';
}
