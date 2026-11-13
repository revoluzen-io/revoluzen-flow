/**
 * The Editor's tabs: one per pipeline being built there. The one on the
 * canvas is marked, unsaved edits are flagged, and a tab with unsaved edits
 * asks before it closes. (The Pipelines page has tabs of its own; see
 * pipelineViewTabs.test.tsx.)
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import OpenPipelineTabs from '../components/editor/OpenPipelineTabs';
import { useWorkflowStore } from '../stores/workflowStore';

const store = () => useWorkflowStore.getState();

function pipeline(id: string, name: string) {
  return {
    version: 1,
    workflow: { id, name, status: 'draft', steps: [{ id: 'read', type: 'source', params: {} }], connections: [] },
  };
}

beforeEach(() => {
  for (const t of [...store().openTabs]) store().closeTab(t.key);
  store().resetWorkflow();
  useWorkflowStore.setState({ openTabs: [], activeTabKey: null });
  window.location.hash = '';
  act(() => {
    store().openInEditor(pipeline('p1', 'Orders'));
    store().setWorkflowName('Orders v2'); // an edit nobody saved
    store().openInEditor(pipeline('p2', 'Customers'));
  });
});

afterEach(() => { vi.restoreAllMocks(); });

describe('the Editor\'s tabs', () => {
  it('show only the pipelines in the editor, mark the one on the canvas and flag unsaved edits', () => {
    render(<OpenPipelineTabs />);
    const tabs = screen.getAllByRole('tab');
    expect(tabs.map((t) => t.textContent)).toEqual(['Orders v2', 'Customers']);
    expect(tabs[1]).toHaveAttribute('aria-selected', 'true');
    expect(tabs[0]).toHaveAttribute('aria-selected', 'false');
    expect(within(tabs[0]).getByRole('img', { name: 'Unsaved changes' })).toBeInTheDocument();
    expect(within(tabs[1]).queryByRole('img', { name: 'Unsaved changes' })).toBeNull();
  });

  it('switch the canvas when a tab is picked', async () => {
    render(<OpenPipelineTabs />);
    await act(async () => { fireEvent.click(screen.getByRole('tab', { name: /Orders v2/ })); });
    expect(store().workflowId).toBe('p1');
    expect(screen.getByRole('tab', { name: /Orders v2/ })).toHaveAttribute('aria-selected', 'true');
  });

  it('close a clean tab at once and ask before closing one with unsaved edits', async () => {
    render(<OpenPipelineTabs />);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Close Customers' })); });
    expect(screen.getAllByRole('tab').map((t) => t.textContent)).toEqual(['Orders v2']);

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Close Orders v2' })); });
    expect(screen.getByText('Save changes before closing?')).toBeInTheDocument();
    expect(store().openTabs).toHaveLength(1);

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Discard Changes' })); });
    expect(store().openTabs).toHaveLength(0);
    // Nothing left open: back to the list.
    expect(window.location.hash).toBe('#pipelines');
  });

  it('start a pipeline from New Pipeline: an empty tab of its own, without leaving the Editor', async () => {
    window.location.hash = '#editor';
    render(<OpenPipelineTabs />);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'New Pipeline' })); });
    expect(store().openTabs).toHaveLength(3);
    expect(store().workflowId).toBeNull();
    expect(store().nodes).toHaveLength(0);
    const tabs = screen.getAllByRole('tab');
    expect(tabs).toHaveLength(3);
    expect(tabs[2]).toHaveAttribute('aria-selected', 'true');
    expect(window.location.hash).toBe('#editor');
  });

  it('leave New Pipeline out where pipelines are not made', () => {
    render(<OpenPipelineTabs canCreate={false} />);
    expect(screen.queryByRole('button', { name: 'New Pipeline' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Open a pipeline' })).toBeEnabled();
  });

  it('list under Open only the saved pipelines not open yet, with no New pipeline item', async () => {
    vi.spyOn(api, 'listWorkflows').mockResolvedValue([
      { id: 'p1', name: 'Orders', updated_at: '2026-11-01T00:00:00Z' },
      { id: 'p2', name: 'Customers', updated_at: '2026-11-02T00:00:00Z' },
      { id: 'p3', name: 'Invoices', updated_at: '2026-11-03T00:00:00Z' },
    ] as never);
    vi.spyOn(api, 'getWorkflow').mockResolvedValue(pipeline('p3', 'Invoices') as never);
    render(<OpenPipelineTabs />);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Open a pipeline' })); });
    const menu = screen.getByRole('menu', { name: 'Open a pipeline' });
    const item = await within(menu).findByRole('menuitem', { name: 'Invoices' });
    expect(within(menu).getAllByRole('menuitem').map((m) => m.textContent)).toEqual(['Invoices']);
    expect(within(menu).queryByText(/New pipeline/i)).toBeNull();

    await act(async () => { fireEvent.click(item); });
    expect(store().workflowId).toBe('p3');
    expect(window.location.hash).toBe('#editor/p3');
  });
});
