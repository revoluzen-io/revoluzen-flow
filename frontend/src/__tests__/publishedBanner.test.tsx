import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../ui/dialog', () => ({ uiConfirm: vi.fn(async () => true) }));
vi.mock('../components/Toast', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock('../api/client', () => ({ api: { discardDraft: vi.fn() } }));

import PublishedBanner from '../components/editor/PublishedBanner';
import { useWorkflowStore } from '../stores/workflowStore';
import { api } from '../api/client';

function state(patch: Record<string, unknown>) {
  act(() => {
    useWorkflowStore.setState({
      workflowId: 'wf1', status: 'published', isDirty: false, publishedVersion: 7, hasUnpublishedChanges: false, ...patch,
    });
  });
}

describe('PublishedBanner', () => {
  beforeEach(() => vi.clearAllMocks());

  it('says the published version is locked and that Save makes a draft', () => {
    state({});
    render(<PublishedBanner />);
    expect(screen.getByTestId('published-banner').textContent).toMatch(/Published and locked\. Version 7 keeps running/);
    expect(screen.getByTestId('published-banner').textContent).toMatch(/Saving makes a draft/);
    expect(screen.queryByRole('button', { name: 'Discard draft' })).toBeNull();
  });

  it('warns about unsaved changes without promising they go live', () => {
    state({ isDirty: true });
    render(<PublishedBanner />);
    const text = screen.getByTestId('published-banner').textContent || '';
    expect(text).toMatch(/Save keeps them as a draft/);
    expect(text).not.toMatch(/next scheduled run uses/);
  });

  it('offers to see what is live and to discard a saved draft', () => {
    state({ hasUnpublishedChanges: true });
    render(<PublishedBanner />);
    expect(screen.getByTestId('published-banner').textContent).toMatch(/Draft of a published pipeline\. Version 7 is locked and keeps running/);
    expect(screen.getByRole('link', { name: 'See what is live' }).getAttribute('href')).toContain('#pipelines/wf1~v7');
    expect(screen.getByRole('button', { name: 'Discard draft' })).toBeEnabled();
  });

  it('will not discard over unsaved changes', () => {
    state({ hasUnpublishedChanges: true, isDirty: true });
    render(<PublishedBanner />);
    expect(screen.getByRole('button', { name: 'Discard draft' })).toBeDisabled();
  });

  it('discards through the server and shows what comes back', async () => {
    (api.discardDraft as any).mockResolvedValue({
      version: 10, published_version: 7, has_unpublished_changes: false,
      workflow: { id: 'wf1', name: 'Daily', status: 'published', steps: [], connections: [] },
    });
    state({ hasUnpublishedChanges: true });
    render(<PublishedBanner />);
    fireEvent.click(screen.getByRole('button', { name: 'Discard draft' }));
    await waitFor(() => expect(api.discardDraft).toHaveBeenCalledWith('wf1'));
    await waitFor(() => expect(useWorkflowStore.getState().hasUnpublishedChanges).toBe(false));
    expect(useWorkflowStore.getState().publishedVersion).toBe(7);
  });
});
