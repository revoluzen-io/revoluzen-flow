import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../components/Toast', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock('../api/client', () => ({
  api: {
    getGitCopySettings: vi.fn(),
    saveGitCopySettings: vi.fn(),
    testGitCopy: vi.fn(),
    saveAllToGit: vi.fn(),
    getPipelineGitCopy: vi.fn(),
    retryPipelineGitCopy: vi.fn(),
  },
}));

import GitCopyCard from '../components/settings/GitCopyCard';
import GitCopyStatus from '../components/pipelines/GitCopyStatus';
import { api } from '../api/client';

const SAVED = {
  enabled: true, repo_url: 'https://github.com/acme/flow.git', branch: 'main', folder: 'flow-pipelines',
  username: '', token_set: true, updated_at: '', updated_by: '', git_installed: true, git_version: '2.53',
};

const RECORD = {
  id: 'r1', workflow_id: 'wf1', version: 7, event: 'published', status: 'saved',
  commit_sha: 'abcdef1234567', commit_url: 'https://github.com/acme/flow/commit/abcdef1234567', error: '',
  actor_name: 'Priya', note: '', attempts: 1, will_retry_at: null, created_at: '2026-11-11T08:00:00Z', updated_at: '2026-11-11T08:00:00Z',
};

describe('GitCopyCard', () => {
  beforeEach(() => vi.clearAllMocks());

  it('never fills in the saved token, and a blank keeps it', async () => {
    (api.getGitCopySettings as any).mockResolvedValue(SAVED);
    (api.saveGitCopySettings as any).mockResolvedValue(SAVED);
    render(<GitCopyCard />);
    const token = await screen.findByLabelText('Access token') as HTMLInputElement;
    expect(token.value).toBe('');
    expect(token.type).toBe('password');
    expect(token.placeholder).toMatch(/Leave blank to keep it/);
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(api.saveGitCopySettings).toHaveBeenCalled());
    expect((api.saveGitCopySettings as any).mock.calls[0][0].token).toBe('');
  });

  it('reports the connection test in words', async () => {
    (api.getGitCopySettings as any).mockResolvedValue(SAVED);
    (api.testGitCopy as any).mockResolvedValue({ ok: false, message: 'The Git server refused the access token.' });
    render(<GitCopyCard />);
    fireEvent.click(await screen.findByRole('button', { name: 'Test connection' }));
    expect(await screen.findByRole('status')).toHaveTextContent('refused the access token');
  });

  it('says when git is not on the server', async () => {
    (api.getGitCopySettings as any).mockResolvedValue({ ...SAVED, git_installed: false });
    render(<GitCopyCard />);
    expect(await screen.findByText(/git program is not installed/)).toBeInTheDocument();
  });

  it('shows nothing to someone who is not an admin', async () => {
    (api.getGitCopySettings as any).mockRejectedValue(new Error('Requires role >= admin'));
    const { container } = render(<GitCopyCard />);
    await waitFor(() => expect(api.getGitCopySettings).toHaveBeenCalled());
    await waitFor(() => expect(container.querySelector('[data-testid="git-copy-card"]')).toBeNull());
  });
});

describe('GitCopyStatus', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows nothing when Git copy is off', async () => {
    (api.getPipelineGitCopy as any).mockResolvedValue({ enabled: false, latest: null });
    const { container } = render(<GitCopyStatus pipelineId="wf1" />);
    await waitFor(() => expect(api.getPipelineGitCopy).toHaveBeenCalled());
    expect(container.textContent).toBe('');
  });

  it('links to the commit', async () => {
    (api.getPipelineGitCopy as any).mockResolvedValue({ enabled: true, latest: RECORD });
    render(<GitCopyStatus pipelineId="wf1" />);
    expect(await screen.findByText('Saved to Git')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'abcdef1' })).toHaveAttribute('href', RECORD.commit_url);
  });

  it('says why it failed and offers to try again', async () => {
    (api.getPipelineGitCopy as any).mockResolvedValue({
      enabled: true,
      latest: { ...RECORD, status: 'failed', commit_sha: '', commit_url: '', error: 'The Git server refused the access token.', will_retry_at: '2026-11-11T09:00:00Z' },
    });
    (api.retryPipelineGitCopy as any).mockResolvedValue({ status: 'queued' });
    render(<GitCopyStatus pipelineId="wf1" canRetry />);
    expect(await screen.findByText('Not saved to Git.')).toBeInTheDocument();
    expect(screen.getByText(/refused the access token/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Try again now' }));
    await waitFor(() => expect(api.retryPipelineGitCopy).toHaveBeenCalledWith('wf1'));
  });

  it('hides Try again from someone who cannot use it', async () => {
    (api.getPipelineGitCopy as any).mockResolvedValue({
      enabled: true, latest: { ...RECORD, status: 'failed', error: 'x' },
    });
    render(<GitCopyStatus pipelineId="wf1" />);
    await screen.findByText('Not saved to Git.');
    expect(screen.queryByRole('button', { name: 'Try again now' })).toBeNull();
  });
});
