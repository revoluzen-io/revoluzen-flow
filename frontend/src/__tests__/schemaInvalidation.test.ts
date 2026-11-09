/**
 * The upstream-schema cache and the config panel's Data In band.
 *
 * Typing in a config panel used to wipe the whole cache, including what flows
 * into the node being edited, so Data In said "Not connected" and the column
 * pickers became text boxes until a refetch returned.
 */
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api/client';
import { useUpstreamSchema } from '../hooks/useUpstreamSchema';
import { useWorkflowStore } from '../stores/workflowStore';
import { buildDataIn, resolveInputs } from '../utils/nodeUiContract';

const entry = (id: string, extra = {}): any => ({ step_id: id, is_source: false, inputs: [], ...extra });
const node = (id: string, stepType = 'filter') => ({ id, position: { x: 0, y: 0 }, data: { stepType, label: id.toUpperCase(), params: {} } });
const edge = (source: string, target: string) => ({ id: `${source}-${target}`, source, target });

// a (source) -> b -> c -> d, with x off to one side.
beforeEach(() => {
  useWorkflowStore.setState({
    nodes: [node('a', 'source'), node('b'), node('c'), node('d'), node('x')],
    edges: [edge('a', 'b'), edge('b', 'c'), edge('c', 'd')],
    stepSchemas: { a: entry('a', { is_source: true }), b: entry('b'), c: entry('c'), d: entry('d'), x: entry('x') },
    schemaVersion: 1,
    undoStack: [],
    redoStack: [],
  } as any);
});

const cachedIds = () => Object.keys(useWorkflowStore.getState().stepSchemas).sort();

describe('schema cache invalidation', () => {
  it("editing a node keeps what flows into it and drops what's downstream", () => {
    useWorkflowStore.getState().updateNodeParams('b', { condition: 'x > 1' });
    expect(cachedIds()).toEqual(['a', 'b', 'x']);
    expect(useWorkflowStore.getState().schemaVersion).toBe(2);
  });

  it("editing a source drops its own entry, which describes its own output", () => {
    useWorkflowStore.getState().updateNodeParams('a', { file_path: 'other.csv' });
    expect(cachedIds()).toEqual(['x']);
  });

  it('connecting nodes drops the target and everything after it', () => {
    useWorkflowStore.getState().onConnect({ source: 'x', target: 'c', sourceHandle: null, targetHandle: null });
    expect(cachedIds()).toEqual(['a', 'b', 'x']);
    expect(useWorkflowStore.getState().schemaVersion).toBe(2);
  });

  it('renaming an edge alias drops the target and everything after it', () => {
    useWorkflowStore.getState().updateEdgeAlias('b', 'c', 'orders');
    expect(cachedIds()).toEqual(['a', 'b', 'x']);
  });

  it('undo and redo drop everything, since the graph may be different', () => {
    useWorkflowStore.getState().pushUndoState();
    useWorkflowStore.getState().undo();
    expect(cachedIds()).toEqual([]);
    useWorkflowStore.setState({ stepSchemas: { b: entry('b') } } as any);
    useWorkflowStore.getState().redo();
    expect(cachedIds()).toEqual([]);
  });
});

describe('useUpstreamSchema while typing', () => {
  const resp = {
    step_id: 'c',
    is_source: false,
    inputs: [{ upstream_step_id: 'b', upstream_label: 'B', schema: { columns: [{ name: 'region', type: 'VARCHAR' }] } }],
  } as any;

  beforeEach(() => {
    vi.useFakeTimers();
    useWorkflowStore.setState({ stepSchemas: {} } as any);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  const settle = () => act(async () => { await vi.advanceTimersByTimeAsync(400); });

  it("keeps the columns and doesn't refetch when the step's own params change", async () => {
    const fetch = vi.spyOn(api, 'getStepSchema').mockResolvedValue(resp);
    const { result } = renderHook(() => useUpstreamSchema('c'));
    await settle();
    expect(result.current.columns.map((c) => c.name)).toEqual(['region']);
    expect(fetch).toHaveBeenCalledTimes(1);

    for (const alias of ['s', 'sa', 'sal']) {
      act(() => useWorkflowStore.getState().updateNodeParams('c', { alias }));
      // The moment that used to empty the panel: right after the keystroke.
      expect(result.current.columns.map((c) => c.name)).toEqual(['region']);
    }
    await settle();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('refetches when something upstream changes', async () => {
    const fetch = vi.spyOn(api, 'getStepSchema').mockResolvedValue(resp);
    renderHook(() => useUpstreamSchema('c'));
    await settle();
    expect(fetch).toHaveBeenCalledTimes(1);

    act(() => useWorkflowStore.getState().updateNodeParams('b', { condition: 'x > 1' }));
    await settle();
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});

describe('resolveInputs', () => {
  const nodes = [node('l'), node('r'), node('j', 'join')];
  const edges = [edge('r', 'j'), edge('l', 'j')];
  const schema = [{ upstream_step_id: 'l', upstream_label: 'Old label', schema: { columns: [{ name: 'id' }] } }];

  it('a wired node is connected even with no schema yet', () => {
    const inputs = resolveInputs('j', edges, nodes, [], () => undefined);
    expect(inputs.map((i) => i.label)).toEqual(['R', 'L']);
    expect(inputs[0].columns).toEqual([]);
    const band = buildDataIn('join', inputs);
    expect(band.ports.every((p) => p.connected)).toBe(true);
  });

  it('a node with nothing wired is not connected, whatever the schema says', () => {
    const band = buildDataIn('filter', resolveInputs('zzz', edges, nodes, schema, () => undefined));
    expect(band.ports[0].connected).toBe(false);
  });

  it('takes columns from the schema, then from the last run', () => {
    const inputs = resolveInputs('j', edges, nodes, schema, (id) => (id === 'r' ? ['from_run'] : undefined));
    expect(inputs.find((i) => i.label === 'L')?.columns).toEqual(['id']);
    expect(inputs.find((i) => i.label === 'R')?.columns).toEqual(['from_run']);
  });

  it("uses the node's current label and puts a Join's left side first", () => {
    const inputs = resolveInputs('j', edges, nodes, schema, () => undefined, 'l');
    expect(inputs[0].label).toBe('L');
    expect(inputs[1].label).toBe('R');
  });
});
