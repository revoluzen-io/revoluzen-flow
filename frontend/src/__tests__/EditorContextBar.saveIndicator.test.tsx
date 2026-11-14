/**
 * EditorContextBar — "Saved just now" means this tab was saved. Picking
 * another tab also turns the on-screen state from unsaved to clean, and used
 * to read as a save of a pipeline nobody had touched.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import React from 'react';

vi.mock('../api/client', async (importOriginal) => {
  const real: any = await importOriginal();
  return { ...real, api: { ...real.api, listProjects: vi.fn(async () => []) } };
});

import EditorContextBar from '../components/EditorContextBar';
import { useWorkflowStore } from '../stores/workflowStore';

describe('EditorContextBar — save indicator', () => {
  beforeEach(() => {
    useWorkflowStore.setState({ activeTabKey: 'a', isDirty: true, workflowName: 'Orders with Customers', nodes: [], edges: [] } as any);
  });

  it('says saved when the same tab goes from unsaved to clean', () => {
    render(<EditorContextBar />);
    expect(screen.getByText('Unsaved')).toBeInTheDocument();
    act(() => { useWorkflowStore.setState({ isDirty: false } as any); });
    expect(screen.getByText('Saved just now')).toBeInTheDocument();
  });

  it('says nothing about saving when another, clean tab comes to the front', () => {
    render(<EditorContextBar />);
    act(() => { useWorkflowStore.setState({ activeTabKey: 'b', isDirty: false, workflowName: 'Payment Reconciliation' } as any); });
    expect(screen.queryByText(/^Saved/)).not.toBeInTheDocument();
  });
});
