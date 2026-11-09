/**
 * The Bin list on the Pipelines page is shared by everyone in the workspace, so
 * it is read again every time the Bin tab is opened. Before, it was read once when
 * the page opened and after the page's own changes, so a pipeline someone else
 * deleted in the meantime did not show until the page was reloaded.
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ listWorkflowBin: vi.fn() }));
vi.mock('../api/client', () => ({ api: { listWorkflowBin: mocks.listWorkflowBin } }));

import { useWorkflowBin } from '../hooks/useWorkflowBin';

const item = (id: string, name = id) => ({
  id, name, project_id: 'default', folder_id: null, status_before: 'draft',
  deleted_at: '2026-11-08T10:00:00+00:00', deleted_by_id: 'u1', deleted_by_name: 'Asha',
  version_count: 1, purge_at: '2026-12-08T10:00:00+00:00',
});
const bin = (ids: string[], retention_days = 30) => ({ retention_days, count: ids.length, items: ids.map((i) => item(i)) });

beforeEach(() => {
  mocks.listWorkflowBin.mockReset();
  mocks.listWorkflowBin.mockResolvedValue(bin(['a']));
});

describe('useWorkflowBin', () => {
  it('reads the Bin when the page opens', async () => {
    const { result } = renderHook(() => useWorkflowBin(false));

    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(mocks.listWorkflowBin).toHaveBeenCalledTimes(1);
    expect(result.current.items.map((i) => i.id)).toEqual(['a']);
    expect(result.current.retentionDays).toBe(30);
  });

  it('does not read it again while the Bin tab stays closed', async () => {
    const { result, rerender } = renderHook(({ active }) => useWorkflowBin(active), { initialProps: { active: false } });
    await waitFor(() => expect(result.current.loading).toBe(false));

    rerender({ active: false });
    rerender({ active: false });

    expect(mocks.listWorkflowBin).toHaveBeenCalledTimes(1);
  });

  it('reads it again each time the Bin tab is opened, and shows what changed meanwhile', async () => {
    const { result, rerender } = renderHook(({ active }) => useWorkflowBin(active), { initialProps: { active: false } });
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    // Someone else deletes two more while this page sits on another tab.
    mocks.listWorkflowBin.mockResolvedValue(bin(['a', 'b', 'c']));
    rerender({ active: true });
    await waitFor(() => expect(result.current.items.map((i) => i.id)).toEqual(['a', 'b', 'c']));
    expect(mocks.listWorkflowBin).toHaveBeenCalledTimes(2);

    // Away and back: another read, and another change shows up.
    rerender({ active: false });
    expect(mocks.listWorkflowBin).toHaveBeenCalledTimes(2);
    mocks.listWorkflowBin.mockResolvedValue(bin(['b']));
    rerender({ active: true });
    await waitFor(() => expect(result.current.items.map((i) => i.id)).toEqual(['b']));
    expect(mocks.listWorkflowBin).toHaveBeenCalledTimes(3);
  });

  it('keeps showing the last list when a read fails', async () => {
    const { result, rerender } = renderHook(({ active }) => useWorkflowBin(active), { initialProps: { active: false } });
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    mocks.listWorkflowBin.mockRejectedValue(new Error('offline'));
    rerender({ active: true });
    await waitFor(() => expect(mocks.listWorkflowBin).toHaveBeenCalledTimes(2));

    expect(result.current.items.map((i) => i.id)).toEqual(['a']);
    expect(result.current.loading).toBe(false);
  });

  it('stops saying it is loading even when the first read fails', async () => {
    mocks.listWorkflowBin.mockRejectedValue(new Error('offline'));
    const { result } = renderHook(() => useWorkflowBin(false));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.items).toEqual([]);
  });

  it('reads again when the page calls reload after one of its own changes', async () => {
    const { result } = renderHook(() => useWorkflowBin(true));
    await waitFor(() => expect(result.current.loading).toBe(false));
    const before = mocks.listWorkflowBin.mock.calls.length;

    mocks.listWorkflowBin.mockResolvedValue(bin([]));
    await act(async () => { await result.current.reload(); });

    expect(mocks.listWorkflowBin.mock.calls.length).toBe(before + 1);
    expect(result.current.items).toEqual([]);
  });

  it('takes the retention from the server, and tolerates a reply without a list', async () => {
    mocks.listWorkflowBin.mockResolvedValue({ retention_days: 7, count: 0 });
    const { result } = renderHook(() => useWorkflowBin(false));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.retentionDays).toBe(7);
    expect(result.current.items).toEqual([]);
  });

  it('drops an older read that finishes after a newer one', async () => {
    let finishOld: (v: unknown) => void = () => {};
    mocks.listWorkflowBin
      .mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve; })) // the page-open read, slow
      .mockResolvedValueOnce(bin(['new']));                                             // the tab-open read, fast
    const { result, rerender } = renderHook(({ active }) => useWorkflowBin(active), { initialProps: { active: false } });

    rerender({ active: true });
    await waitFor(() => expect(result.current.items.map((i) => i.id)).toEqual(['new']));
    await act(async () => { finishOld(bin(['old'])); });

    expect(result.current.items.map((i) => i.id)).toEqual(['new']);
  });
});
