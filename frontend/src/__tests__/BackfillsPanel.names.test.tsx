import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import BackfillsPanel from '../components/pages/BackfillsPanel';
import { api } from '../api/client';

vi.mock('../api/client', () => ({ api: { listBackfills: vi.fn(), listWorkflows: vi.fn(), get: vi.fn(), post: vi.fn() } }));
vi.mock('../components/Toast', () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

const row = (pipeline_id: string) => ({
  id: `bf_${pipeline_id}`, pipeline_id, status: 'success', window_start: '2026-11-01T00:00:00Z', window_end: '2026-11-08T00:00:00Z',
  total_windows: 7, succeeded_windows: 7, failed_windows: 0, skipped_windows: 0, window_size: 'daily', concurrency: 2,
  on_failure: 'stop', created_at: '2026-11-14T08:00:00Z',
});

beforeEach(() => {
  vi.mocked(api.listBackfills).mockReset();
  vi.mocked(api.listWorkflows).mockReset();
});
afterEach(cleanup);

describe('BackfillsPanel', () => {
  it('names each backfill by its pipeline, not its id', async () => {
    vi.mocked(api.listBackfills).mockResolvedValue([row('wf1')] as any);
    vi.mocked(api.listWorkflows).mockResolvedValue([{ id: 'wf1', name: 'Daily Visitors by Region' }] as any);
    render(<BackfillsPanel />);
    expect(await screen.findByText('Daily Visitors by Region')).toBeTruthy();
    expect(screen.queryByText('wf1')).toBeNull();
  });

  it("keeps the id when the pipeline is no longer listed", async () => {
    vi.mocked(api.listBackfills).mockResolvedValue([row('gone1')] as any);
    vi.mocked(api.listWorkflows).mockResolvedValue([] as any);
    render(<BackfillsPanel />);
    expect(await screen.findByText('gone1')).toBeTruthy();
  });
});
