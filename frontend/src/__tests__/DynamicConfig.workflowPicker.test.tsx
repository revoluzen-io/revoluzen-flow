/**
 * DynamicConfig — a `workflow_picker` field (ForEach's Sub-Pipeline) is a
 * list of saved pipelines by name. It used to fall through to a text box that
 * showed the stored id.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';

vi.mock('../api/client', async (importOriginal) => {
  const real: any = await importOriginal();
  return {
    ...real,
    api: {
      ...real.api,
      listWorkflows: vi.fn(async () => [
        { id: 'p-self', name: 'Regional Revenue Files' },
        { id: 'p-one', name: "Send One Region's Revenue" },
        { id: 'p-two', name: 'Daily Sales' },
      ]),
    },
  };
});

import DynamicConfig from '../components/DynamicConfig';
import { useWorkflowStore } from '../stores/workflowStore';

const TYPE = 'foreach_pipeline';
const NODE_META = [
  {
    type: TYPE,
    label: 'ForEach',
    category: 'control',
    description: '',
    default_params: { pipeline_id: '' },
    param_schema: [{ name: 'pipeline_id', type: 'workflow_picker', label: 'Sub-Pipeline', required: true }],
  },
];

describe('DynamicConfig — pipeline picker', () => {
  beforeEach(() => {
    (window as any).__revoluzen_flow_node_types = NODE_META;
    useWorkflowStore.setState({ workflowId: 'p-self' } as any);
  });
  afterEach(() => { delete (window as any).__revoluzen_flow_node_types; });

  it('lists saved pipelines by name, selects the stored one, and leaves out the pipeline being edited', async () => {
    const onChange = vi.fn();
    render(<DynamicConfig stepType={TYPE} params={{ pipeline_id: 'p-one' }} nodeId="s1" onChange={onChange} />);
    const option = await screen.findByRole('option', { name: "Send One Region's Revenue" });
    const select = option.closest('select') as HTMLSelectElement;
    expect(select.value).toBe('p-one');
    expect(screen.queryByRole('option', { name: 'Regional Revenue Files' })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'p-one' })).not.toBeInTheDocument();
    fireEvent.change(select, { target: { value: 'p-two' } });
    expect(JSON.stringify(onChange.mock.calls)).toContain('p-two');
  });

  it('says so when the stored pipeline no longer exists', async () => {
    render(<DynamicConfig stepType={TYPE} params={{ pipeline_id: 'gone-123' }} nodeId="s1" onChange={vi.fn()} />);
    expect(await screen.findByText(/No saved pipeline has this id any more/)).toBeInTheDocument();
  });
});
