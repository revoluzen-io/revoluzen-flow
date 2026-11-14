/**
 * The two event triggers a schedule row can be, in the words the app shows,
 * and the rules of the form that sets them up.
 *
 * A schedule row with `schedule_type: 'file_arrival'` starts one run for each
 * new file in a folder; one with `'after_pipeline'` starts a run when another
 * pipeline finishes. The backend is `backend/revoluzen_flow/triggers/`. Kept
 * apart from the dialog so the wording and the saved shape can be tested on
 * their own.
 */

export type TriggerKind = 'time' | 'file_arrival' | 'after_pipeline';
export type FileLocation = 'local' | 'sftp' | 'smb' | 's3' | 'gcs' | 'azure_blob';
export type AfterOutcome = 'success' | 'failure' | 'always';

export interface LocationOption {
  value: FileLocation;
  label: string;
  /** Where it is, in a sentence: "in /upload on an SFTP server". */
  where: string;
  /** Saved connection types that can reach it. Empty: no connection. */
  connectionTypes: string[];
  /** The kind of connection, for "There is no saved SFTP connection yet". */
  connectionName?: string;
  folderLabel: string;
  folderPlaceholder: string;
  folderHint: string;
}

export const FILE_LOCATIONS: LocationOption[] = [
  {
    value: 'local', label: 'A folder on the Flow server', where: 'on the Flow server', connectionTypes: [],
    folderLabel: 'Folder', folderPlaceholder: 'incoming/orders',
    folderHint: 'A full path, or a folder inside Flow’s data folder.',
  },
  {
    value: 'sftp', label: 'An SFTP server', where: 'on an SFTP server', connectionTypes: ['sftp', 'ftp'],
    connectionName: 'SFTP',
    folderLabel: 'Folder on the server', folderPlaceholder: '/upload/orders',
    folderHint: 'The server and sign-in come from the connection.',
  },
  {
    value: 'smb', label: 'A Windows file share', where: 'on a Windows file share', connectionTypes: ['smb'],
    connectionName: 'Windows file share',
    folderLabel: 'Folder on the share', folderPlaceholder: 'daily',
    folderHint: 'The server, share and sign-in come from the connection. The folder is inside the connection’s folder: leave it empty for that folder, or start with \\ for the top of the share.',
  },
  {
    value: 's3', label: 'Amazon S3 or MinIO', where: 'in S3', connectionTypes: ['s3', 'minio'],
    connectionName: 'S3 or MinIO',
    folderLabel: 'Folder in the bucket', folderPlaceholder: 'incoming/orders/',
    folderHint: 'The bucket comes from the connection. Leave empty for the top of the bucket.',
  },
  {
    value: 'gcs', label: 'Google Cloud Storage', where: 'in Google Cloud Storage', connectionTypes: ['gcs'],
    connectionName: 'Google Cloud Storage',
    folderLabel: 'Folder in the bucket', folderPlaceholder: 'incoming/orders/',
    folderHint: 'The bucket comes from the connection. Leave empty for the top of the bucket.',
  },
  {
    value: 'azure_blob', label: 'Azure Blob Storage', where: 'in Azure Blob Storage', connectionTypes: ['azure_blob'],
    connectionName: 'Azure Blob Storage',
    folderLabel: 'Folder in the container', folderPlaceholder: 'incoming/orders/',
    folderHint: 'The container comes from the connection. Leave empty for the top of the container.',
  },
];

export const OUTCOME_OPTIONS: { value: AfterOutcome; label: string; verb: string }[] = [
  { value: 'success', label: 'Succeeds', verb: 'succeeds' },
  { value: 'failure', label: 'Fails', verb: 'fails' },
  { value: 'always', label: 'Finishes either way', verb: 'finishes' },
];

export const POLL_CHOICES: { seconds: number; label: string }[] = [
  { seconds: 30, label: 'Every 30 seconds' },
  { seconds: 60, label: 'Every minute' },
  { seconds: 300, label: 'Every 5 minutes' },
  { seconds: 900, label: 'Every 15 minutes' },
  { seconds: 3600, label: 'Every hour' },
];

export function triggerKindOf(schedule: { schedule_type?: string } | null | undefined): TriggerKind {
  const kind = schedule?.schedule_type;
  return kind === 'file_arrival' || kind === 'after_pipeline' ? kind : 'time';
}

export function isEventTrigger(schedule: { schedule_type?: string } | null | undefined): boolean {
  return triggerKindOf(schedule) !== 'time';
}

function locationOf(value: string | undefined): LocationOption | undefined {
  return FILE_LOCATIONS.find((l) => l.value === value);
}

type NameOf = (workflowId: string) => string | undefined;

/** "When a file named orders_*.csv arrives in /upload on an SFTP server". */
export function describeFileTrigger(schedule: any): string {
  const location = locationOf(schedule?.file_location);
  const pattern = (schedule?.file_pattern || '*').trim();
  const folder = (schedule?.file_folder || '').trim();
  const named = pattern && pattern !== '*' ? `a file named ${pattern}` : 'a file';
  const place = folder ? `in ${folder} ` : '';
  return `When ${named} arrives ${place}${location?.where ?? ''}`.replace(/\s+/g, ' ').trim();
}

/** 'After "Extract orders" succeeds'. */
export function describeAfterTrigger(schedule: any, nameOf?: NameOf): string {
  const outcome = OUTCOME_OPTIONS.find((o) => o.value === schedule?.after_outcome) ?? OUTCOME_OPTIONS[0];
  const name = nameOf?.(schedule?.after_workflow_id || '');
  return name ? `After “${name}” ${outcome.verb}` : `After another pipeline ${outcome.verb}`;
}

export function describeEventTrigger(schedule: any, nameOf?: NameOf): string {
  return triggerKindOf(schedule) === 'file_arrival'
    ? describeFileTrigger(schedule)
    : describeAfterTrigger(schedule, nameOf);
}

/**
 * The short words for the Pipelines table's Schedule column, which is narrow:
 * longer text wraps to several lines and makes the row tall. The full
 * description goes in the chip's tooltip.
 */
export function shortTriggerLabel(schedule: any): string {
  return triggerKindOf(schedule) === 'file_arrival' ? 'On a new file' : 'After a pipeline';
}

// ── The form ────────────────────────────────────────────────────────────

export interface FileTriggerDraft {
  location: FileLocation | '';
  connectionId: string;
  folder: string;
  pattern: string;
  parameter: string;
  settleSeconds: number;
  pollSeconds: number;
  includeExisting: boolean;
}

export interface AfterTriggerDraft {
  upstreamId: string;
  outcome: AfterOutcome;
}

export const EMPTY_FILE_DRAFT: FileTriggerDraft = {
  location: '',
  connectionId: '',
  folder: '',
  pattern: '*',
  parameter: '',
  settleSeconds: 60,
  pollSeconds: 60,
  includeExisting: false,
};

export const EMPTY_AFTER_DRAFT: AfterTriggerDraft = { upstreamId: '', outcome: 'success' };

export function fileDraftFrom(schedule: any): FileTriggerDraft {
  return {
    location: (locationOf(schedule?.file_location)?.value ?? '') as FileLocation | '',
    connectionId: schedule?.file_connection_id || '',
    folder: schedule?.file_folder || '',
    pattern: schedule?.file_pattern || '*',
    parameter: schedule?.file_parameter || '',
    settleSeconds: Number.isFinite(schedule?.file_settle_seconds) ? schedule.file_settle_seconds : 60,
    pollSeconds: Number.isFinite(schedule?.file_poll_seconds) ? schedule.file_poll_seconds : 60,
    includeExisting: !!schedule?.file_include_existing,
  };
}

export function afterDraftFrom(schedule: any): AfterTriggerDraft {
  const outcome = OUTCOME_OPTIONS.find((o) => o.value === schedule?.after_outcome)?.value ?? 'success';
  return { upstreamId: schedule?.after_workflow_id || '', outcome };
}

/** What still has to be filled in, in words, or null when it can be saved. */
export function fileDraftProblem(draft: FileTriggerDraft): string | null {
  const location = locationOf(draft.location);
  if (!location) return 'Choose where the files arrive.';
  if (location.connectionTypes.length > 0 && !draft.connectionId) return 'Choose the connection to use.';
  if (draft.location === 'local' && !draft.folder.trim()) return 'Enter the folder to watch.';
  const pattern = draft.pattern.trim();
  if (/[\\/]/.test(pattern)) return 'File names cannot contain a folder. Put the folder in the box above.';
  if (!Number.isFinite(draft.settleSeconds) || draft.settleSeconds < 0 || draft.settleSeconds > 86_400) {
    return 'The waiting time must be between 0 seconds and one day.';
  }
  return null;
}

export function afterDraftProblem(draft: AfterTriggerDraft, pipelineId: string): string | null {
  if (!draft.upstreamId) return 'Choose the pipeline this one runs after.';
  if (draft.upstreamId === pipelineId) return 'A pipeline cannot run after itself.';
  return null;
}

export function filePayload(draft: FileTriggerDraft): Record<string, unknown> {
  const local = draft.location === 'local';
  return {
    schedule_type: 'file_arrival',
    file_location: draft.location,
    file_connection_id: local ? '' : draft.connectionId,
    file_folder: draft.folder.trim(),
    file_pattern: draft.pattern.trim() || '*',
    file_parameter: draft.parameter,
    file_settle_seconds: Math.round(draft.settleSeconds),
    file_poll_seconds: Math.round(draft.pollSeconds),
    file_include_existing: draft.includeExisting,
  };
}

export function afterPayload(draft: AfterTriggerDraft): Record<string, unknown> {
  return {
    schedule_type: 'after_pipeline',
    after_workflow_id: draft.upstreamId,
    after_outcome: draft.outcome,
  };
}

/** The parameter most likely meant to receive a file's path, or '' if none fits. */
export function guessFileParameter(parameters: { name: string; type?: string }[]): string {
  const text = (parameters || []).filter((p) => !p.type || p.type === 'string');
  const preferred = ['file_path', 'filepath', 'path', 'file', 'file_name', 'filename', 'key', 'object_key'];
  for (const name of preferred) {
    const hit = text.find((p) => p.name.toLowerCase() === name);
    if (hit) return hit.name;
  }
  const loose = text.find((p) => /file|path|key/i.test(p.name));
  return loose ? loose.name : '';
}

/** How long ago, in words, for "Checked 2 minutes ago". */
export function checkedAgo(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return '';
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return '';
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 60) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return minutes === 1 ? '1 minute ago' : `${minutes} minutes ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return hours === 1 ? '1 hour ago' : `${hours} hours ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? '1 day ago' : `${days} days ago`;
}
