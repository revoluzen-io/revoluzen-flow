/**
 * The Pipelines page's tabs: Home (the list) first, then one tab per pipeline
 * opened from the list, shown in full on that page. They are separate from
 * the Editor's tabs: looking at a pipeline does not put it in the Editor.
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import PipelineViewTabs from '../components/pipelines/PipelineViewTabs';
import { MAX_VIEW_TABS, usePipelineViewTabs } from '../stores/pipelineViewTabs';
import { useWorkflowStore } from '../stores/workflowStore';

const views = () => usePipelineViewTabs.getState();

beforeEach(() => {
  usePipelineViewTabs.setState({ tabs: [] });
  for (const t of [...useWorkflowStore.getState().openTabs]) useWorkflowStore.getState().closeTab(t.key);
  useWorkflowStore.setState({ openTabs: [], activeTabKey: null });
  window.location.hash = '';
  act(() => {
    views().open('p1', 'Orders');
    views().open('p2', 'Customers');
  });
});

afterEach(() => { vi.restoreAllMocks(); });

describe('the Pipelines page\'s tabs', () => {
  it('start with Home, marked while the list is shown', () => {
    render(<PipelineViewTabs shownId={null} />);
    const tabs = screen.getAllByRole('tab');
    expect(tabs.map((t) => t.textContent)).toEqual(['Home', 'Orders', 'Customers']);
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true');
  });

  it('show a pipeline in full from its tab, and Home goes back to the same place in the panel', async () => {
    window.location.hash = '#pipelines?project=sales';
    const { rerender } = render(<PipelineViewTabs shownId={null} />);
    await act(async () => { fireEvent.click(screen.getByRole('tab', { name: /Orders/ })); });
    expect(window.location.hash).toBe('#pipelines/p1?project=sales');

    rerender(<PipelineViewTabs shownId="p1" />);
    expect(screen.getByRole('tab', { name: /Orders/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: /Home/ })).toHaveAttribute('aria-selected', 'false');

    await act(async () => { fireEvent.click(screen.getByRole('tab', { name: /Home/ })); });
    expect(window.location.hash).toBe('#pipelines?project=sales');
  });

  it('close without asking; closing the one shown brings its neighbour, then Home', async () => {
    window.location.hash = '#pipelines/p2';
    const { rerender } = render(<PipelineViewTabs shownId="p2" />);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Close Customers' })); });
    expect(screen.queryByText('Save changes before closing?')).toBeNull();
    expect(window.location.hash).toBe('#pipelines/p1');

    rerender(<PipelineViewTabs shownId="p1" />);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Close Orders' })); });
    expect(window.location.hash).toBe('#pipelines');
    expect(screen.getAllByRole('tab').map((t) => t.textContent)).toEqual(['Home']);
  });

  it('closing a tab that is not shown leaves the page where it is', async () => {
    window.location.hash = '#pipelines/p1';
    render(<PipelineViewTabs shownId="p1" />);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Close Customers' })); });
    expect(window.location.hash).toBe('#pipelines/p1');
  });

  it('offer Open at the right end in place of "+": pipelines without a tab here, and no New pipeline item', async () => {
    vi.spyOn(api, 'listWorkflows').mockResolvedValue([
      { id: 'p1', name: 'Orders', updated_at: '2026-11-01T00:00:00Z' },
      { id: 'p2', name: 'Customers', updated_at: '2026-11-02T00:00:00Z' },
      { id: 'p3', name: 'Invoices', updated_at: '2026-11-03T00:00:00Z' },
    ] as never);
    window.location.hash = '#pipelines?project=sales';
    render(<PipelineViewTabs shownId={null} />);
    expect(screen.queryByRole('button', { name: 'Open another pipeline' })).toBeNull();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Open a pipeline' })); });
    const menu = screen.getByRole('menu', { name: 'Open a pipeline' });
    const item = await within(menu).findByRole('menuitem', { name: 'Invoices' });
    expect(within(menu).getAllByRole('menuitem').map((m) => m.textContent)).toEqual(['Invoices']);
    expect(within(menu).queryByText(/New pipeline/i)).toBeNull();

    await act(async () => { fireEvent.click(item); });
    expect(window.location.hash).toBe('#pipelines/p3?project=sales');
    // Shown on this page only: the Editor's tabs are untouched.
    expect(useWorkflowStore.getState().openTabs).toHaveLength(0);
  });

  it(`turn Open off while ${MAX_VIEW_TABS} pipelines are open here`, () => {
    for (let i = 3; i <= MAX_VIEW_TABS; i += 1) views().open(`p${i}`, `P${i}`);
    render(<PipelineViewTabs shownId={null} />);
    expect(screen.getByRole('button', { name: 'Open a pipeline' })).toBeDisabled();
  });
});

describe('the Pipelines page\'s tab list', () => {
  it('stays out of the Editor: looking at pipelines opens no editor tab', () => {
    expect(views().tabs.map((t) => t.id)).toEqual(['p1', 'p2']);
    expect(useWorkflowStore.getState().openTabs).toHaveLength(0);
  });

  it('opens a pipeline once, keeps its place, and follows a rename', () => {
    views().open('p1', 'Orders (renamed)');
    expect(views().tabs.map((t) => t.name)).toEqual(['Orders (renamed)', 'Customers']);
    views().rename('p2', 'Customers 2');
    expect(views().tabs.map((t) => t.name)).toEqual(['Orders (renamed)', 'Customers 2']);
  });

  it(`holds ${MAX_VIEW_TABS} at most`, () => {
    for (let i = 3; i <= MAX_VIEW_TABS; i += 1) expect(views().open(`p${i}`, `P${i}`)).toBe(true);
    expect(views().open('one-more', 'One more')).toBe(false);
    expect(views().tabs).toHaveLength(MAX_VIEW_TABS);
  });

  it('comes back after a reload for the same person only, and is forgotten at sign-out', async () => {
    localStorage.clear();
    localStorage.setItem('revoluzen_flow_user', JSON.stringify({ id: 'u1' }));

    vi.resetModules();
    const before = await import('../stores/pipelineViewTabs');
    before.restoreViewTabs(); // signed in: the list is kept from now on
    before.usePipelineViewTabs.getState().open('p1', 'Orders');
    before.usePipelineViewTabs.getState().open('p2', 'Customers');
    const kept = JSON.parse(localStorage.getItem('revoluzen_flow_pipeline_view_tabs_v1') || 'null');
    expect(kept.owner).toBe('u1::default');
    expect(kept.tabs.map((t: { id: string }) => t.id)).toEqual(['p1', 'p2']);

    vi.resetModules();
    const after = await import('../stores/pipelineViewTabs');
    after.restoreViewTabs();
    expect(after.usePipelineViewTabs.getState().tabs.map((t) => t.name)).toEqual(['Orders', 'Customers']);

    localStorage.setItem('revoluzen_flow_user', JSON.stringify({ id: 'u2' }));
    vi.resetModules();
    const someoneElse = await import('../stores/pipelineViewTabs');
    someoneElse.restoreViewTabs();
    expect(someoneElse.usePipelineViewTabs.getState().tabs).toHaveLength(0);

    after.forgetViewTabs();
    expect(localStorage.getItem('revoluzen_flow_pipeline_view_tabs_v1')).toBeNull();
    localStorage.clear();
  });
});
