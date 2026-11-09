/**
 * The Bin tab on the Pipelines page, and the wording around deleting.
 *
 * Deleting moves a pipeline to the Bin (server-backed) where it can be restored
 * until the retention window ends. Every screen takes the number of days from the
 * server (`retention_days`), and 0 means nothing is removed automatically.
 */
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import PipelinesBin from '../components/pages/PipelinesBin';
import type { WorkflowBinItem } from '../api/client';
import {
  binBanner, binEmptyBody, bulkDeleteConfirm, daysPhrase, deleteOptionDescription, removalLabel,
} from '../utils/binText';

const DAY = 24 * 60 * 60 * 1000;

const item = (over: Partial<WorkflowBinItem> = {}): WorkflowBinItem => ({
  id: 'abcdef123456',
  name: 'Revenue by region',
  project_id: 'default',
  folder_id: null,
  status_before: 'draft',
  deleted_at: new Date(Date.now() - 2 * DAY).toISOString(),
  deleted_by_id: 'u1',
  deleted_by_name: 'Asha',
  version_count: 4,
  purge_at: new Date(Date.now() + 12 * DAY - 1000).toISOString(),
  ...over,
});

const renderBin = (over: Partial<React.ComponentProps<typeof PipelinesBin>> = {}) => {
  const props = {
    items: [item(), item({ id: 'ffffff000000', name: 'Churn', project_id: 'p1', deleted_by_name: '' })],
    retentionDays: 30,
    loading: false,
    canChange: true,
    canEmpty: true,
    busyId: null,
    projectLabel: (id: string) => (id === 'default' ? 'Default' : id === 'p1' ? 'Finance' : id),
    formatTime: () => '2d ago',
    onRestore: vi.fn(),
    onPurge: vi.fn(),
    onEmpty: vi.fn(),
    ...over,
  };
  render(<PipelinesBin {...props} />);
  return props;
};

afterEach(cleanup);

describe('Bin tab', () => {
  it('lists each deleted pipeline with its project, who deleted it, and when it is removed', () => {
    renderBin();
    const row = screen.getByTestId('bin-row-abcdef123456');
    expect(within(row).getByText('Revenue by region')).toBeInTheDocument();
    expect(within(row).getByText('Default')).toBeInTheDocument();
    expect(within(row).getByText('by Asha')).toBeInTheDocument();
    expect(within(row).getByText('In 12 days')).toBeInTheDocument();
    const other = screen.getByTestId('bin-row-ffffff000000');
    expect(within(other).getByText('Finance')).toBeInTheDocument();
    expect(within(other).queryByText(/^by /)).toBeNull();
  });

  it('says how long pipelines stay and counts them', () => {
    renderBin();
    expect(screen.getByText('2 pipelines in the Bin')).toBeInTheDocument();
    expect(screen.getByText('Pipelines stay here for 30 days, then are removed for good.')).toBeInTheDocument();
  });

  it('restores and deletes forever through the page, one row at a time', () => {
    const props = renderBin();
    const row = screen.getByTestId('bin-row-abcdef123456');
    fireEvent.click(within(row).getByRole('button', { name: 'Restore' }));
    expect(props.onRestore).toHaveBeenCalledWith(props.items[0]);
    fireEvent.click(within(row).getByRole('button', { name: 'Delete forever' }));
    expect(props.onPurge).toHaveBeenCalledWith(props.items[0]);
  });

  it('shows Empty Bin only to someone who may use it', () => {
    const props = renderBin();
    fireEvent.click(screen.getByRole('button', { name: 'Empty Bin' }));
    expect(props.onEmpty).toHaveBeenCalledTimes(1);
    cleanup();
    renderBin({ canEmpty: false });
    expect(screen.queryByRole('button', { name: 'Empty Bin' })).toBeNull();
  });

  it('gives a read-only user the list but no buttons', () => {
    renderBin({ canChange: false });
    expect(screen.queryByRole('button', { name: 'Restore' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Delete forever' })).toBeNull();
  });

  it('disables the row whose request is in flight', () => {
    renderBin({ busyId: 'abcdef123456' });
    const row = screen.getByTestId('bin-row-abcdef123456');
    expect(within(row).getByRole('button', { name: 'Working…' })).toBeDisabled();
    expect(within(row).getByRole('button', { name: 'Delete forever' })).toBeDisabled();
  });

  it('says so when nothing is removed automatically', () => {
    renderBin({ retentionDays: 0, items: [item({ purge_at: null })] });
    expect(screen.getByText('Pipelines stay here until you delete them from the Bin or empty it.')).toBeInTheDocument();
    expect(screen.getByText('Not automatic')).toBeInTheDocument();
  });

  it('has an empty state that names the window', () => {
    renderBin({ items: [] });
    expect(screen.getByText('The Bin is empty')).toBeInTheDocument();
    expect(screen.getByText('Deleted pipelines stay here for 30 days, so you can restore them.')).toBeInTheDocument();
  });

  it('does not claim the Bin is empty while it is still loading', () => {
    renderBin({ items: [], loading: true });
    expect(screen.queryByText('The Bin is empty')).toBeNull();
    expect(screen.getByText('Loading the Bin…')).toBeInTheDocument();
  });
});

describe('Delete wording', () => {
  it('promises only what the server does: a Bin, for the number of days it reports', () => {
    expect(deleteOptionDescription(30, false)).toBe(
      'Move the pipeline to the Bin. Removed for good after 30 days; restore it from the Bin until then.',
    );
    expect(deleteOptionDescription(1, true)).toBe(
      'Move the pipelines to the Bin. Removed for good after 1 day; restore them from the Bin until then.',
    );
    expect(daysPhrase(1)).toBe('1 day');
    expect(daysPhrase(30)).toBe('30 days');
  });

  it('promises no removal date when retention is off', () => {
    expect(deleteOptionDescription(0, false)).toBe(
      'Move the pipeline to the Bin. It stays there until you delete it from the Bin or empty the Bin.',
    );
    expect(binBanner(0)).not.toMatch(/\d+ days?/);
    expect(binEmptyBody(0)).not.toMatch(/\d+ days?/);
  });

  it('asks before moving several pipelines, and says they can come back', () => {
    const one = bulkDeleteConfirm(1, 30);
    expect(one.title).toBe('Move 1 pipeline to the Bin?');
    expect(one.message).toBe(
      'The selected pipeline will be moved to the Bin and removed for good after 30 days. Restore it from the Bin until then.',
    );
    expect(one.confirmLabel).toBe('Move to Bin');
    const many = bulkDeleteConfirm(3, 30);
    expect(many.title).toBe('Move 3 pipelines to the Bin?');
    expect(many.message).toContain('All 3 selected pipelines will be moved to the Bin');
    expect(many.confirmLabel).toBe('Move 3 to Bin');
    // Never the old promise that nothing could be undone.
    expect(`${one.message} ${many.message}`).not.toMatch(/permanent|cannot be undone/i);
  });

  it('counts the days to removal, rounding up, and says when it is due', () => {
    const now = Date.parse('2026-11-01T12:00:00Z');
    expect(removalLabel(new Date(now + 12 * DAY).toISOString(), now)).toBe('In 12 days');
    expect(removalLabel(new Date(now + 1 * DAY - 1000).toISOString(), now)).toBe('In 1 day');
    expect(removalLabel(new Date(now - 1000).toISOString(), now)).toBe('Due now');
    expect(removalLabel(null, now)).toBe('Not automatic');
    expect(removalLabel('not a date', now)).toBe('Not automatic');
  });
});
