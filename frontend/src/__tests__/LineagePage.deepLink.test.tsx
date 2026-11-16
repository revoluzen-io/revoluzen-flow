import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import LineagePage from '../components/pages/LineagePage';
import { api } from '../api/client';

vi.mock('../api/client', () => ({ api: { get: vi.fn(), post: vi.fn() } }));
vi.mock('../hooks/usePageContext', () => ({ usePageContext: () => {} }));

const graph = {
  workflow_id: 'wf1',
  nodes: [
    { id: 'n1', data: { step_id: 'src', label: 'Customers export', step_type: 'csv_source', columns: ['customer_id', 'tier'] }, position: { x: 0, y: 0 } },
    { id: 'n2', data: { step_id: 'out', label: 'crm.customers', step_type: 'output', columns: ['customer_id', 'tier'] }, position: { x: 0, y: 0 } },
  ],
  edges: [{ id: 'e1', source: 'n1', target: 'n2', label: '', data: { source_column: 'tier', target_column: 'tier', transform_type: 'passthrough' } }],
};

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.get).mockImplementation(async (url: string) => {
    if (url === '/api/workflows') return [{ id: 'wf1', name: 'Customer Master', description: '' }];
    if (url === '/api/lineage/wf1') return graph;
    throw new Error(`unexpected ${url}`);
  });
});
afterEach(() => {
  cleanup();
  window.location.hash = '';
});

describe('LineagePage deep link', () => {
  it("loads the pipeline a table's View lineage link names, instead of asking for one", async () => {
    window.location.hash = '#lineage?entity=workflow:wf1';
    render(<LineagePage />);
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/lineage/wf1'));
    expect(await screen.findByText('Customers export')).toBeTruthy();
    expect(screen.queryByText('Select a pipeline to visualize its data lineage')).toBeNull();
  });

  it('without a link, still asks for a pipeline and loads none', async () => {
    render(<LineagePage />);
    expect(await screen.findByText('Select a pipeline to visualize its data lineage')).toBeTruthy();
    expect(api.get).not.toHaveBeenCalledWith(expect.stringMatching(/^\/api\/lineage\//));
  });
});
