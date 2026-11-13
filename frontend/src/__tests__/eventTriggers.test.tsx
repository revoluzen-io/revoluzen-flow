/**
 * The Schedule dialog's two event triggers: "A file arriving" and "Another
 * pipeline finishing". The words a person reads, the checks before saving,
 * the shape saved (backend/revoluzen_flow/scheduling/models.py), and the
 * folder form's own behaviour: only connections that can reach the chosen
 * place are offered, and "Check the folder now" lists what is there.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/client', () => ({
  api: {
    listConnections: vi.fn(),
    getWorkflow: vi.fn(),
    listWorkflows: vi.fn(),
    post: vi.fn(),
  },
}));

import { api } from '../api/client';
import { AfterTriggerForm, FileTriggerForm } from '../components/pipelines/EventTriggerForm';
import {
  EMPTY_AFTER_DRAFT,
  EMPTY_FILE_DRAFT,
  afterDraftFrom,
  afterDraftProblem,
  afterPayload,
  checkedAgo,
  describeAfterTrigger,
  describeEventTrigger,
  describeFileTrigger,
  fileDraftFrom,
  fileDraftProblem,
  filePayload,
  guessFileParameter,
  isEventTrigger,
  shortTriggerLabel,
  triggerKindOf,
  type AfterTriggerDraft,
  type FileTriggerDraft,
} from '../components/pipelines/triggerText';

const names: Record<string, string> = { a1: 'Extract orders' };
const nameOf = (id: string) => names[id];

describe('the words', () => {
  it('describes a file trigger by where and which names', () => {
    expect(describeFileTrigger({ file_location: 'sftp', file_folder: '/upload', file_pattern: 'orders_*.csv' }))
      .toBe('When a file named orders_*.csv arrives in /upload on an SFTP server');
    expect(describeFileTrigger({ file_location: 's3', file_folder: '', file_pattern: '*' }))
      .toBe('When a file arrives in S3');
    expect(describeFileTrigger({ file_location: 'local', file_folder: 'incoming', file_pattern: '*.csv' }))
      .toBe('When a file named *.csv arrives in incoming on the Flow server');
  });

  it('names the pipeline it runs after, and how that has to end', () => {
    expect(describeAfterTrigger({ after_workflow_id: 'a1', after_outcome: 'success' }, nameOf))
      .toBe('After “Extract orders” succeeds');
    expect(describeAfterTrigger({ after_workflow_id: 'a1', after_outcome: 'failure' }, nameOf))
      .toBe('After “Extract orders” fails');
    expect(describeAfterTrigger({ after_workflow_id: 'gone', after_outcome: 'always' }, nameOf))
      .toBe('After another pipeline finishes');
  });

  it('tells event triggers from the clock', () => {
    expect(triggerKindOf({ schedule_type: 'cron' })).toBe('time');
    expect(triggerKindOf({ schedule_type: 'file_arrival' })).toBe('file_arrival');
    expect(isEventTrigger({ schedule_type: 'after_pipeline' })).toBe(true);
    expect(isEventTrigger(null)).toBe(false);
    expect(describeEventTrigger({ schedule_type: 'after_pipeline', after_workflow_id: 'a1' }, nameOf))
      .toBe('After “Extract orders” succeeds');
  });

  it('keeps the table chip short enough for its narrow column', () => {
    expect(shortTriggerLabel({ schedule_type: 'file_arrival', file_folder: '/a/very/long/folder' })).toBe('On a new file');
    expect(shortTriggerLabel({ schedule_type: 'after_pipeline', after_workflow_id: 'a1' })).toBe('After a pipeline');
  });

  it('says how long ago a folder was checked', () => {
    const now = Date.parse('2026-11-11T10:00:00Z');
    expect(checkedAgo('2026-11-11T09:59:40Z', now)).toBe('just now');
    expect(checkedAgo('2026-11-11T09:58:00Z', now)).toBe('2 minutes ago');
    expect(checkedAgo('2026-11-11T08:00:00Z', now)).toBe('2 hours ago');
    expect(checkedAgo(null, now)).toBe('');
  });
});

describe('before saving', () => {
  it('asks for the place, the connection and a local folder', () => {
    expect(fileDraftProblem(EMPTY_FILE_DRAFT)).toBe('Choose where the files arrive.');
    expect(fileDraftProblem({ ...EMPTY_FILE_DRAFT, location: 'sftp' })).toBe('Choose the connection to use.');
    expect(fileDraftProblem({ ...EMPTY_FILE_DRAFT, location: 'local' })).toBe('Enter the folder to watch.');
    expect(fileDraftProblem({ ...EMPTY_FILE_DRAFT, location: 's3', connectionId: 'c1' })).toBeNull();
    expect(fileDraftProblem({ ...EMPTY_FILE_DRAFT, location: 'local', folder: 'in', pattern: 'sub/*.csv' }))
      .toMatch(/cannot contain a folder/);
  });

  it('asks for the pipeline to run after, and never itself', () => {
    expect(afterDraftProblem(EMPTY_AFTER_DRAFT, 'me')).toBe('Choose the pipeline this one runs after.');
    expect(afterDraftProblem({ upstreamId: 'me', outcome: 'success' }, 'me')).toBe('A pipeline cannot run after itself.');
    expect(afterDraftProblem({ upstreamId: 'a1', outcome: 'success' }, 'me')).toBeNull();
  });
});

describe('what is saved', () => {
  it('saves a file trigger in the shape the server reads, and reads it back', () => {
    const draft: FileTriggerDraft = {
      location: 'sftp', connectionId: 'c1', folder: ' /upload ', pattern: '', parameter: 'file_path',
      settleSeconds: 120, pollSeconds: 300, includeExisting: true,
    };
    const saved = filePayload(draft);
    expect(saved).toEqual({
      schedule_type: 'file_arrival', file_location: 'sftp', file_connection_id: 'c1', file_folder: '/upload',
      file_pattern: '*', file_parameter: 'file_path', file_settle_seconds: 120, file_poll_seconds: 300,
      file_include_existing: true,
    });
    expect(fileDraftFrom(saved)).toEqual({ ...draft, folder: '/upload', pattern: '*' });
  });

  it('drops the connection for a folder on the server', () => {
    expect(filePayload({ ...EMPTY_FILE_DRAFT, location: 'local', connectionId: 'stale', folder: 'in' }).file_connection_id).toBe('');
  });

  it('saves an after-pipeline trigger and reads it back', () => {
    const draft: AfterTriggerDraft = { upstreamId: 'a1', outcome: 'always' };
    expect(afterPayload(draft)).toEqual({ schedule_type: 'after_pipeline', after_workflow_id: 'a1', after_outcome: 'always' });
    expect(afterDraftFrom(afterPayload(draft))).toEqual(draft);
    expect(afterDraftFrom({ after_outcome: 'nonsense' }).outcome).toBe('success');
  });

  it('picks the parameter meant for a path', () => {
    expect(guessFileParameter([{ name: 'region' }, { name: 'file_path', type: 'string' }])).toBe('file_path');
    expect(guessFileParameter([{ name: 'inputFile', type: 'string' }])).toBe('inputFile');
    expect(guessFileParameter([{ name: 'path', type: 'int' }, { name: 'region' }])).toBe('');
  });
});

function FileHarness({ initial = EMPTY_FILE_DRAFT, isNew = true }: { initial?: FileTriggerDraft; isNew?: boolean }) {
  const [draft, setDraft] = useState(initial);
  return (
    <>
      <FileTriggerForm pipelineId="me" draft={draft} onChange={setDraft} isNew={isNew} />
      <output data-testid="draft">{JSON.stringify(draft)}</output>
    </>
  );
}

const draftOnScreen = () => JSON.parse(screen.getByTestId('draft').textContent || '{}');

describe('the folder form', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.listConnections as any).mockResolvedValue([
      { id: 's3a', name: 'Landing bucket', type: 's3' },
      { id: 'min', name: 'MinIO', type: 'minio' },
      { id: 'ftp1', name: 'Plain FTP', type: 'ftp', config: { protocol: 'ftp' } },
      { id: 'ftp2', name: 'Partner SFTP', type: 'ftp', config: { protocol: 'sftp' } },
      { id: 'pg', name: 'Warehouse', type: 'postgresql' },
    ]);
    (api.getWorkflow as any).mockResolvedValue({ workflow: { parameters: [{ name: 'file_path', type: 'string' }] } });
  });

  it('offers only connections that can reach the chosen place', async () => {
    render(<FileHarness />);
    fireEvent.change(screen.getByLabelText('Where the files arrive'), { target: { value: 's3' } });
    const connection = await screen.findByLabelText('Connection');
    await waitFor(() => expect(connection.querySelectorAll('option').length).toBe(3));
    expect([...connection.querySelectorAll('option')].map((o) => o.textContent)).toEqual([
      'Choose a saved connection', 'Landing bucket', 'MinIO',
    ]);
    fireEvent.change(screen.getByLabelText('Where the files arrive'), { target: { value: 'sftp' } });
    await waitFor(() => expect([...screen.getByLabelText('Connection').querySelectorAll('option')].map((o) => o.textContent))
      .toEqual(['Choose a saved connection', 'Partner SFTP']));
  });

  it('picks the file_path parameter for a new trigger', async () => {
    render(<FileHarness />);
    await waitFor(() => expect(draftOnScreen().parameter).toBe('file_path'));
  });

  it('leaves an edited trigger without a parameter as it was', async () => {
    render(<FileHarness isNew={false} initial={{ ...EMPTY_FILE_DRAFT, location: 'local', folder: 'in' }} />);
    await waitFor(() => expect(api.getWorkflow).toHaveBeenCalled());
    expect(draftOnScreen().parameter).toBe('');
  });

  it('checks the folder and lists what matches', async () => {
    (api.post as any).mockResolvedValue({
      total: 3, matching: 2,
      files: [{ name: 'orders_2.csv', size: 2048, modified: null }, { name: 'orders_1.csv', size: 10, modified: null }],
    });
    render(<FileHarness initial={{ ...EMPTY_FILE_DRAFT, location: 'local', folder: 'incoming', pattern: 'orders_*.csv' }} />);
    fireEvent.click(screen.getByRole('button', { name: 'Check the folder now' }));
    const preview = await screen.findByTestId('folder-preview');
    expect(api.post).toHaveBeenCalledWith('/api/schedules/check-folder', {
      file_location: 'local', file_connection_id: '', file_folder: 'incoming', file_pattern: 'orders_*.csv',
    });
    expect(preview).toHaveTextContent('2 matching files there now');
    expect(preview).toHaveTextContent('orders_2.csv (2.0 KB)');
  });

  it('shows why the folder could not be checked', async () => {
    (api.post as any).mockRejectedValue(new Error('Cannot list /upload on sftp.example.com: Authentication failed.'));
    render(<FileHarness initial={{ ...EMPTY_FILE_DRAFT, location: 'sftp', connectionId: 'ftp2', folder: '/upload' }} />);
    fireEvent.click(screen.getByRole('button', { name: 'Check the folder now' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Authentication failed');
  });
});

describe('a Windows file share', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getWorkflow as any).mockResolvedValue({ workflow: { parameters: [{ name: 'file_path', type: 'string' }] } });
  });

  it('is described in words and saved with its connection', () => {
    expect(describeFileTrigger({ file_location: 'smb', file_folder: 'daily', file_pattern: '*.DAT' }))
      .toBe('When a file named *.DAT arrives in daily on a Windows file share');
    expect(describeFileTrigger({ file_location: 'smb', file_folder: '', file_pattern: '*' }))
      .toBe('When a file arrives on a Windows file share');
    expect(fileDraftProblem({ ...EMPTY_FILE_DRAFT, location: 'smb' })).toBe('Choose the connection to use.');
    // An empty folder is the connection's own folder.
    const draft: FileTriggerDraft = { ...EMPTY_FILE_DRAFT, location: 'smb', connectionId: 'share', pattern: '*.DAT' };
    expect(fileDraftProblem(draft)).toBeNull();
    expect(filePayload(draft)).toMatchObject({ file_location: 'smb', file_connection_id: 'share', file_folder: '' });
    expect(fileDraftFrom(filePayload(draft)).location).toBe('smb');
  });

  it('offers only file share connections, and says when there are none', async () => {
    (api.listConnections as any).mockResolvedValue([
      { id: 'share', name: 'Mainframe share', type: 'smb' },
      { id: 's3a', name: 'Landing bucket', type: 's3' },
    ]);
    const first = render(<FileHarness />);
    fireEvent.change(screen.getByLabelText('Where the files arrive'), { target: { value: 'smb' } });
    await waitFor(() => expect([...screen.getByLabelText('Connection').querySelectorAll('option')].map((o) => o.textContent))
      .toEqual(['Choose a saved connection', 'Mainframe share']));
    expect(screen.getByLabelText('Folder on the share')).toHaveAttribute('placeholder', 'daily');
    first.unmount();

    (api.listConnections as any).mockResolvedValue([{ id: 's3a', name: 'Landing bucket', type: 's3' }]);
    render(<FileHarness initial={{ ...EMPTY_FILE_DRAFT, location: 'smb' }} />);
    expect(await screen.findByText(/There is no saved Windows file share connection yet/)).toBeInTheDocument();
  });

  it('checks the share folder and lists what matches', async () => {
    (api.listConnections as any).mockResolvedValue([{ id: 'share', name: 'Mainframe share', type: 'smb' }]);
    (api.post as any).mockResolvedValue({
      total: 2, matching: 1, files: [{ name: 'CUSTMAST.DAT', size: 4096, modified: '2026-11-11T08:00:00+00:00' }],
    });
    render(<FileHarness initial={{ ...EMPTY_FILE_DRAFT, location: 'smb', connectionId: 'share', pattern: '*.dat' }} />);
    fireEvent.click(screen.getByRole('button', { name: 'Check the folder now' }));
    const preview = await screen.findByTestId('folder-preview');
    expect(api.post).toHaveBeenCalledWith('/api/schedules/check-folder', {
      file_location: 'smb', file_connection_id: 'share', file_folder: '', file_pattern: '*.dat',
    });
    expect(preview).toHaveTextContent('1 matching file there now');
    expect(preview).toHaveTextContent('CUSTMAST.DAT (4.0 KB)');
  });
});

describe('the after-pipeline form', () => {
  it('lists the other pipelines and says which are not published', async () => {
    (api.listWorkflows as any).mockResolvedValue([
      { id: 'me', name: 'This one', status: 'published' },
      { id: 'a1', name: 'Extract orders', status: 'published' },
      { id: 'd1', name: 'Draft thing', status: 'draft' },
      { id: 'x1', name: 'Old', status: 'archived' },
    ]);
    const onChange = vi.fn();
    render(<AfterTriggerForm pipelineId="me" draft={EMPTY_AFTER_DRAFT} onChange={onChange} />);
    const select = await screen.findByLabelText('Run after');
    await waitFor(() => expect(select.querySelectorAll('option').length).toBe(3));
    expect([...select.querySelectorAll('option')].map((o) => o.textContent)).toEqual([
      'Choose a pipeline', 'Draft thing (not published)', 'Extract orders',
    ]);
    fireEvent.click(screen.getByRole('radio', { name: 'Fails' }));
    expect(onChange).toHaveBeenCalledWith({ upstreamId: '', outcome: 'failure' });
  });
});
