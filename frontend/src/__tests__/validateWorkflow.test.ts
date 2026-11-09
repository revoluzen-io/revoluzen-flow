/**
 * Regression net for the 2026-06-10 validation-audit fixes.
 *
 * Pins the three drift classes the audit caught:
 *   1. Param-shape drift — validator requirements must match what the
 *      backend node actually reads (lookup_key, columns).
 *   2. Cycle detection — neither the canvas guard nor the validator
 *      caught indirect cycles before; both share graphCycles now.
 *   3. Registry drift — palette types must be classified in the intent
 *      map; the AI fallback builder must not emit hidden legacy types.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { validateWorkflow } from '../utils/validateWorkflow';
import { RESERVED_METADATA_COLUMNS } from '../utils/nodeArity';
import { wouldCreateCycle, findCycleNodeIds } from '../utils/graphCycles';
import { INTENT_FOR_STEP_TYPE } from '../components/modulesPanelData';
import { HIDDEN_TYPES } from '../components/hiddenNodeTypes';
import { parsePipelineIntent } from '../ai/pipelineBuilder';

const node = (id: string, stepType: string, params: Record<string, any> = {}) => ({
  id,
  type: 'revoluzenFlowNode',
  data: { stepType, params, label: id },
});
const edge = (source: string, target: string) => ({ id: `${source}->${target}`, source, target });

describe('graphCycles', () => {
  it('detects that closing an indirect loop creates a cycle', () => {
    const edges = [edge('A', 'B'), edge('B', 'C')];
    expect(wouldCreateCycle('C', 'A', edges)).toBe(true);   // C→A closes A→B→C→A
    expect(wouldCreateCycle('A', 'C', edges)).toBe(false);  // A→C is a plain fan-out
    expect(wouldCreateCycle('A', 'A', edges)).toBe(true);   // self-loop
  });

  it('finds every node on a cycle and nothing else', () => {
    const ids = ['A', 'B', 'C', 'D'];
    const edges = [edge('A', 'B'), edge('B', 'C'), edge('C', 'A'), edge('C', 'D')];
    const cyc = findCycleNodeIds(ids, edges);
    expect(cyc).toEqual(new Set(['A', 'B', 'C']));
  });
});

describe('validateWorkflow — cycle detection', () => {
  it('flags an indirect cycle as an error on every member node', () => {
    const nodes = [node('A', 'filter', { condition: 'x > 1' }), node('B', 'filter', { condition: 'x > 2' }), node('C', 'filter', { condition: 'x > 3' })];
    const edges = [edge('A', 'B'), edge('B', 'C'), edge('C', 'A')];
    const issues = validateWorkflow(nodes, edges);
    const cycleErrors = issues.filter((i) => i.level === 'error' && /cycle/i.test(i.message));
    expect(cycleErrors.map((i) => i.nodeId).sort()).toEqual(['A', 'B', 'C']);
  });

  it('does not flag an acyclic chain', () => {
    const nodes = [node('A', 'filter', { condition: 'x > 1' }), node('B', 'filter', { condition: 'x > 2' })];
    const issues = validateWorkflow(nodes, [edge('A', 'B')]);
    expect(issues.some((i) => /cycle/i.test(i.message))).toBe(false);
  });
});

describe('validateWorkflow — lookup param drift (backend reads lookup_key only)', () => {
  const sources = [
    node('main', 'source', { connector_type: 'csv', file_path: 'a.csv' }),
    node('ref', 'source', { connector_type: 'csv', file_path: 'b.csv' }),
  ];
  const wires = [edge('main', 'lk'), edge('ref', 'lk')];

  it('requires lookup_key (the field the backend raises on)', () => {
    const issues = validateWorkflow([...sources, node('lk', 'lookup', {})], wires);
    expect(issues.some((i) => i.nodeId === 'lk' && i.field === 'lookup_key')).toBe(true);
  });

  it('does NOT require the phantom lookup_source field', () => {
    const issues = validateWorkflow(
      [...sources, node('lk', 'lookup', { lookup_key: 'id' })],
      wires,
    );
    expect(issues.filter((i) => i.nodeId === 'lk' && i.level === 'error')).toEqual([]);
  });
});

describe('validateWorkflow — derived_column param shape (backend reads columns array)', () => {
  const src = node('s', 'source', { connector_type: 'csv', file_path: 'a.csv' });

  it('flags the legacy flat name/expression shape as missing columns', () => {
    const issues = validateWorkflow(
      [src, node('d', 'derived_column', { name: 'total', expression: 'a*b' })],
      [edge('s', 'd')],
    );
    expect(issues.some((i) => i.nodeId === 'd' && i.field === 'columns')).toBe(true);
  });

  it('accepts the canonical columns array', () => {
    const issues = validateWorkflow(
      [src, node('d', 'derived_column', { columns: [{ name: 'total', expression: 'a*b' }] })],
      [edge('s', 'd')],
    );
    expect(issues.filter((i) => i.nodeId === 'd' && i.level === 'error')).toEqual([]);
  });
});

describe('validateWorkflow — input contract ceilings', () => {
  it('rejects a 3rd input on join (contract max 2)', () => {
    const nodes = [
      node('a', 'source', { connector_type: 'csv', file_path: 'a.csv' }),
      node('b', 'source', { connector_type: 'csv', file_path: 'b.csv' }),
      node('c', 'source', { connector_type: 'csv', file_path: 'c.csv' }),
      node('j', 'join', {}),
    ];
    const issues = validateWorkflow(nodes, [edge('a', 'j'), edge('b', 'j'), edge('c', 'j')]);
    expect(issues.some((i) => i.nodeId === 'j' && /at most 2/.test(i.message))).toBe(true);
  });
});

describe('validateWorkflow — node-audit round 2 param sanity', () => {
  const src = node('s', 'source', { connector_type: 'csv', file_path: 'a.csv' });
  const wire = [edge('s', 'x')];

  it('deduplicate requires key columns and warns on non-deterministic order', () => {
    const noKeys = validateWorkflow([src, node('x', 'deduplicate', {})], wire);
    expect(noKeys.some((i) => i.nodeId === 'x' && i.level === 'error' && i.field === 'key')).toBe(true);

    const noOrder = validateWorkflow(
      [src, node('x', 'deduplicate', { key: ['id'], strategy: 'keep_first' })], wire,
    );
    expect(noOrder.some((i) => i.nodeId === 'x' && i.level === 'warning' && /non-deterministic/i.test(i.message))).toBe(true);

    const ordered = validateWorkflow(
      [src, node('x', 'deduplicate', { key: ['id'], order_by: 'created_at DESC' })], wire,
    );
    expect(ordered.filter((i) => i.nodeId === 'x')).toEqual([]);
  });

  it('sample validates count/percent per mode, honoring legacy fraction', () => {
    const badPct = validateWorkflow(
      [src, node('x', 'sample', { mode: 'percent', percent: 150 })], wire,
    );
    expect(badPct.some((i) => i.nodeId === 'x' && /between 0 and 100/.test(i.message))).toBe(true);

    const legacyFraction = validateWorkflow(
      [src, node('x', 'sample', { fraction: 0.5 })], wire,   // percent mode inferred
    );
    expect(legacyFraction.filter((i) => i.nodeId === 'x' && i.level === 'error')).toEqual([]);

    const badCount = validateWorkflow(
      [src, node('x', 'sample', { mode: 'rows', count: -5 })], wire,
    );
    expect(badCount.some((i) => i.nodeId === 'x' && /greater than 0/.test(i.message))).toBe(true);
  });

  it('sort flags duplicate sort columns from both token and dict shapes', () => {
    const dupTokens = validateWorkflow(
      [src, node('x', 'sort', { sort_by: ['amount DESC', 'amount ASC'] })], wire,
    );
    expect(dupTokens.some((i) => i.nodeId === 'x' && /Duplicate sort column/.test(i.message))).toBe(true);

    const ok = validateWorkflow(
      [src, node('x', 'sort', { sort_by: ['amount DESC', 'name ASC'] })], wire,
    );
    expect(ok.filter((i) => i.nodeId === 'x')).toEqual([]);
  });

  it('upsert (Keep Latest) requires keys and warns without order_by', () => {
    const noKeys = validateWorkflow([src, node('x', 'upsert', {})], wire);
    expect(noKeys.some((i) => i.nodeId === 'x' && i.level === 'error' && i.field === 'key')).toBe(true);

    const noOrder = validateWorkflow([src, node('x', 'upsert', { key: ['id'] })], wire);
    expect(noOrder.some((i) => i.nodeId === 'x' && /non-deterministic/i.test(i.message))).toBe(true);
  });

  it('scd2 requires business key + tracked columns and warns on overlap', () => {
    const ref = node('r', 'source', { connector_type: 'csv', file_path: 'b.csv' });
    const wires = [edge('s', 'x'), edge('r', 'x')];
    const empty = validateWorkflow([src, ref, node('x', 'scd2', {})], wires);
    expect(empty.some((i) => i.nodeId === 'x' && i.field === 'business_key')).toBe(true);
    expect(empty.some((i) => i.nodeId === 'x' && i.field === 'tracked_columns')).toBe(true);

    const overlapping = validateWorkflow(
      [src, ref, node('x', 'scd2', { business_key: ['customer_id'], tracked_columns: ['customer_id', 'name'] })],
      wires,
    );
    expect(overlapping.some((i) => i.nodeId === 'x' && i.level === 'warning' && /also listed as tracked/.test(i.message))).toBe(true);
  });

  it('delete_data blocks the unimplemented files mode and warns on no-op', () => {
    const filesMode = validateWorkflow(
      [src, node('x', 'delete_data', { target_kind: 'files', target_path: '/data/' })], wire,
    );
    expect(filesMode.some((i) => i.nodeId === 'x' && i.level === 'error' && /NO files would be deleted/.test(i.message))).toBe(true);

    const noCondition = validateWorkflow([src, node('x', 'delete_data', { target_kind: 'rows' })], wire);
    expect(noCondition.some((i) => i.nodeId === 'x' && i.level === 'warning' && /remove nothing/.test(i.message))).toBe(true);
  });

  it('switch_case flags empty + duplicate case values and missing default', () => {
    const issues = validateWorkflow(
      [src, node('x', 'switch_case', {
        column: 'status',
        expression: 'status',
        cases: [{ value: 'PROD' }, { value: 'PROD' }, { value: '' }],
      })],
      wire,
    );
    expect(issues.some((i) => i.nodeId === 'x' && /Duplicate case value/.test(i.message))).toBe(true);
    expect(issues.some((i) => i.nodeId === 'x' && /empty value/.test(i.message))).toBe(true);
    expect(issues.some((i) => i.nodeId === 'x' && i.level === 'warning' && /default branch/i.test(i.message))).toBe(true);
  });

  it('wait_delay validates the delay range against the engine cap', () => {
    const zero = validateWorkflow([src, node('x', 'wait_delay', { seconds: 0 })], wire);
    expect(zero.some((i) => i.nodeId === 'x' && /greater than 0/.test(i.message))).toBe(true);

    const capped = validateWorkflow([src, node('x', 'wait_delay', { seconds: 900 })], wire);
    expect(capped.some((i) => i.nodeId === 'x' && i.level === 'warning' && /capped at 300s/.test(i.message))).toBe(true);

    const legacyUnits = validateWorkflow([src, node('x', 'wait_delay', { duration: 2, unit: 'minutes' })], wire);
    expect(legacyUnits.filter((i) => i.nodeId === 'x' && i.level === 'error')).toEqual([]);
  });

  it('aggregate flags duplicate aliases, bad identifiers, and non-COUNT without a column', () => {
    const dup = validateWorkflow(
      [src, node('x', 'aggregate', { functions: [
        { function: 'SUM', column: 'amount', alias: 'total' },
        { function: 'AVG', column: 'amount', alias: 'total' },
      ] })], wire,
    );
    expect(dup.some((i) => i.nodeId === 'x' && /Duplicate aggregate alias/.test(i.message))).toBe(true);

    const noCol = validateWorkflow(
      [src, node('x', 'aggregate', { functions: [{ function: 'SUM', column: '*', alias: 's' }] })], wire,
    );
    expect(noCol.some((i) => i.nodeId === 'x' && /SUM needs a column/.test(i.message))).toBe(true);

    const countStar = validateWorkflow(
      [src, node('x', 'aggregate', { functions: [{ function: 'COUNT', column: '*', alias: 'n' }] })], wire,
    );
    expect(countStar.filter((i) => i.nodeId === 'x' && i.level === 'error')).toEqual([]);
  });

  it('window requires ORDER BY for ranking/navigation functions', () => {
    const noOrder = validateWorkflow(
      [src, node('x', 'window', { window_functions: [{ function: 'ROW_NUMBER', alias: 'rn' }] })], wire,
    );
    expect(noOrder.some((i) => i.nodeId === 'x' && i.field === 'order_by' && /need an Order By/.test(i.message))).toBe(true);

    const withOrder = validateWorkflow(
      [src, node('x', 'window', { window_functions: [{ function: 'ROW_NUMBER', alias: 'rn' }], order_by: ['amount DESC'] })], wire,
    );
    expect(withOrder.filter((i) => i.nodeId === 'x' && i.level === 'error')).toEqual([]);

    // running totals (SUM/AVG) don't strictly require ORDER BY
    const runningNoOrder = validateWorkflow(
      [src, node('x', 'window', { window_functions: [{ function: 'SUM', column: 'amount', alias: 'rt' }] })], wire,
    );
    expect(runningNoOrder.some((i) => i.field === 'order_by')).toBe(false);
  });

  it('conditional_split validates branch names and conditions', () => {
    const empty = validateWorkflow([src, node('x', 'conditional_split', { conditions: [] })], wire);
    expect(empty.some((i) => i.nodeId === 'x' && i.level === 'warning' && /No split conditions/.test(i.message))).toBe(true);

    const dup = validateWorkflow([src, node('x', 'conditional_split', {
      conditions: [{ name: 'hi', condition: 'a>1' }, { name: 'hi', condition: 'a>2' }],
    })], wire);
    expect(dup.some((i) => i.nodeId === 'x' && /Duplicate branch name/.test(i.message))).toBe(true);

    const noCond = validateWorkflow([src, node('x', 'conditional_split', {
      conditions: [{ name: 'hi', condition: '' }],
    })], wire);
    expect(noCond.some((i) => i.nodeId === 'x' && /no condition/.test(i.message))).toBe(true);

    const ok = validateWorkflow([src, node('x', 'conditional_split', {
      conditions: [{ name: 'hi', condition: 'a>1' }], default_output: 'rest',
    })], wire);
    expect(ok.filter((i) => i.nodeId === 'x' && i.level === 'error')).toEqual([]);
  });

  it('foreach_pipeline requires a sub-pipeline id', () => {
    const missing = validateWorkflow([src, node('x', 'foreach_pipeline', {})], wire);
    expect(missing.some((i) => i.nodeId === 'x' && i.field === 'pipeline_id')).toBe(true);
    const ok = validateWorkflow([src, node('x', 'foreach_pipeline', { pipeline_id: 'child' })], wire);
    expect(ok.filter((i) => i.nodeId === 'x' && i.level === 'error')).toEqual([]);
  });

  it('foreach_pipeline (control-flow guardrails): needs an input to iterate', () => {
    // No wire → nothing to loop over.
    const noInput = validateWorkflow([node('x', 'foreach_pipeline', { pipeline_id: 'child' })], []);
    expect(noInput.some((i) => i.nodeId === 'x' && i.field === 'input')).toBe(true);
    // With an upstream input → no input error.
    const withInput = validateWorkflow([src, node('x', 'foreach_pipeline', { pipeline_id: 'child' })], wire);
    expect(withInput.some((i) => i.nodeId === 'x' && i.field === 'input')).toBe(false);
  });

  it('foreach_pipeline (control-flow guardrails): blocks running ITSELF (recursion)', () => {
    const selfRef = validateWorkflow(
      [src, node('x', 'foreach_pipeline', { pipeline_id: 'wf-1' })], wire, [], 'wf-1',
    );
    expect(selfRef.some((i) => i.nodeId === 'x' && i.field === 'pipeline_id' && /recurse/i.test(i.message))).toBe(true);
    // A different sub-pipeline is fine.
    const other = validateWorkflow(
      [src, node('x', 'foreach_pipeline', { pipeline_id: 'wf-2' })], wire, [], 'wf-1',
    );
    expect(other.some((i) => i.nodeId === 'x' && i.field === 'pipeline_id')).toBe(false);
  });

  it('foreach_pipeline (control-flow guardrails): max_iterations sanity', () => {
    const zero = validateWorkflow([src, node('x', 'foreach_pipeline', { pipeline_id: 'c', max_iterations: 0 })], wire);
    expect(zero.some((i) => i.nodeId === 'x' && i.field === 'max_iterations' && i.level === 'error')).toBe(true);
    const huge = validateWorkflow([src, node('x', 'foreach_pipeline', { pipeline_id: 'c', max_iterations: 5000 })], wire);
    expect(huge.some((i) => i.nodeId === 'x' && i.field === 'max_iterations' && i.level === 'warning')).toBe(true);
  });

  it('warns on an undeclared ${param.x} reference (dynamic-input typo guard)', () => {
    const flt = node('x', 'filter', { condition: 'amount > ${param.min_amount}' });
    // No declared params → warning that names the undeclared parameter.
    const undeclared = validateWorkflow([src, flt], wire);
    expect(undeclared.some((i) => i.nodeId === 'x' && i.level === 'warning' && /min_amount/.test(i.message))).toBe(true);
    // Declared → no undeclared-parameter warning.
    const declared = validateWorkflow([src, flt], wire, [{ name: 'min_amount' }]);
    expect(declared.some((i) => i.nodeId === 'x' && /undeclared parameter/i.test(i.message))).toBe(false);
  });

  it('lookup_activity requires an output_var capture name', () => {
    const noVar = validateWorkflow([src, node('x', 'lookup_activity', {})], wire);
    expect(noVar.some((i) => i.nodeId === 'x' && i.field === 'output_var')).toBe(true);

    const ok = validateWorkflow(
      [src, node('x', 'lookup_activity', { output_var: 'watermark' })], wire,
    );
    expect(ok.filter((i) => i.nodeId === 'x' && i.level === 'error')).toEqual([]);
  });

  it('flatten_explode requires a column', () => {
    const issues = validateWorkflow([src, node('x', 'flatten_explode', { mode: 'explode' })], wire);
    expect(issues.some((i) => i.nodeId === 'x' && i.field === 'column')).toBe(true);
  });
});

describe('palette intent registry', () => {
  it('classifies the managed local-table nodes (regression: fell into Automate)', () => {
    expect(INTENT_FOR_STEP_TYPE.local_table_source).toBe('Import');
    expect(INTENT_FOR_STEP_TYPE.local_table_sink).toBe('Publish');
  });
});

describe('AI fallback builder emits only modern palette types', () => {
  it('normalizes legacy source/sink/validate types before returning', () => {
    const result = parsePipelineIntent(
      'load sales.csv then filter amount > 100 then clean data then save to parquet',
    );
    expect(result).not.toBeNull();
    const types = result!.steps.map((s) => s.type);
    for (const t of types) {
      expect(HIDDEN_TYPES.has(t), `step type "${t}" is a hidden legacy type`).toBe(false);
    }
    expect(types).toContain('source');
    expect(types).toContain('destination');
    expect(types).toContain('data_quality');   // was `validate`
    expect(types).not.toContain('validate');
  });

  it('maps file extensions to the right source connector_type', () => {
    const result = parsePipelineIntent('load events.json then filter x > 1 then save to output.csv');
    expect(result).not.toBeNull();
    const src = result!.steps.find((s) => s.type === 'source');
    expect(src?.params.connector_type).toBe('json');
    const dest = result!.steps.find((s) => s.type === 'destination');
    expect(dest?.params.connector_type).toBe('csv');
  });
});

// C3 / Phase 2 — validation derived from the backend param_schema registry.
// These set window.__revoluzen_flow_node_types (the registry the running app loads) and
// prove: schema-required fields are flagged; the schema OVERRIDES over-strict
// legacy maps (kills drift); one-of groups + dedicated blocks aren't doubled.
describe('validateWorkflow — schema-driven required fields (C3)', () => {
  const REG = [
    { step_type: 'derived_column', param_schema: [{ name: 'columns', required: true }] },
    // filter marks NOTHING required (condition OR rules) — the legacy map's
    // `filter:['condition']` was over-strict.
    { step_type: 'filter', param_schema: [{ name: 'condition' }, { name: 'rules' }] },
    { step_type: 'deduplicate', param_schema: [{ name: 'key', required: true }, { name: 'order_by' }] },
    { step_type: 'csv_source', param_schema: [{ name: 'file_path', required: true }] },
    { step_type: 'source', param_schema: [{ name: 'connector_type', required: true }] },
    { step_type: 'data_quality', param_schema: [{ name: 'rules', required: true }] },
  ];

  beforeEach(() => { (globalThis as any).__revoluzen_flow_node_types = REG; });
  afterEach(() => { delete (globalThis as any).__revoluzen_flow_node_types; });

  it('flags a schema-required field when missing', () => {
    const issues = validateWorkflow([node('d', 'derived_column', {})], []);
    expect(issues.some((i) => i.nodeId === 'd' && i.field === 'columns' && i.level === 'error')).toBe(true);
  });

  it('passes once the schema-required field is present', () => {
    const issues = validateWorkflow(
      [node('d', 'derived_column', { columns: [{ name: 'x', expression: '1' }] })], [],
    );
    expect(issues.some((i) => i.nodeId === 'd' && i.field === 'columns')).toBe(false);
  });

  it('lets the schema override an over-strict legacy map (filter requires nothing)', () => {
    // TRANSFORM_REQUIREMENTS still lists filter:['condition'], but the loaded
    // schema marks nothing required → no false positive on an advanced-mode
    // filter that uses rules instead of a condition.
    const issues = validateWorkflow([node('f', 'filter', { rules: [{ field: 'a' }] })], []);
    expect(issues.some((i) => i.nodeId === 'f' && i.field === 'condition')).toBe(false);
  });

  it('fills a coverage gap the hand-maps missed (data_quality needs rules)', () => {
    const issues = validateWorkflow([node('q', 'data_quality', {})], []);
    expect(issues.some((i) => i.nodeId === 'q' && i.field === 'rules' && i.level === 'error')).toBe(true);
  });

  it('does not double-report a field a dedicated block already covers', () => {
    const nodes = [node('s', 'csv_source', { file_path: 'a.csv' }), node('dd', 'deduplicate', {})];
    const issues = validateWorkflow(nodes, [edge('s', 'dd')]);
    const keyErrors = issues.filter((i) => i.nodeId === 'dd' && i.field === 'key' && i.level === 'error');
    expect(keyErrors.length).toBe(1);   // the dedicated rich message wins, not two
  });

  it('respects one-of groups (csv_source: file_path OR url)', () => {
    const issues = validateWorkflow([node('s', 'csv_source', { url: 'http://x/a.csv' })], []);
    expect(issues.some((i) => i.nodeId === 's' && i.field === 'file_path' && i.level === 'error')).toBe(false);
  });
});

// A5 — a step that adds internal columns, upstream of a sink that writes its rows
// as they are. A Data Quality step adds columns only in some settings: tag adds
// __dq_passed, "Add Quality Score Column" adds __dq_score, and reject adds
// _split_output unless the rows leave by its Pass / Reject output. drop, fail and
// split add none, so warning about them told people to clean up what is not there.
describe('validateWorkflow — internal metadata columns reaching a sink (A5)', () => {
  const src = node('src', 'source', { connector_type: 'csv', file_path: 'a.csv' });
  const sink = node('out', 'destination', { connector_type: 'csv', file_path: 'out.csv' });
  const dq = (params: Record<string, any>) =>
    node('dq', 'data_quality', { rules: [{ column: 'id', op: 'not_null' }], ...params });
  const via = (source: string, target: string, sourceHandle?: string) => ({
    ...edge(source, target),
    ...(sourceHandle ? { sourceHandle } : {}),
  });

  /** The metadata warnings, as { sink, message }. */
  const leaks = (nodes: any[], edges: any[]) =>
    validateWorkflow(nodes, edges)
      .filter((i) => /internal metadata columns/.test(i.message))
      .map((i) => ({ sink: i.nodeId, message: i.message }));
  const straight = (params: Record<string, any>) =>
    leaks([src, dq(params), sink], [edge('src', 'dq'), edge('dq', 'out')]);

  it.each([
    ['drop', { mode: 'drop' }],
    ['no mode set (drop)', {}],
    ['fail', { mode: 'fail' }],
    ['split', { mode: 'split' }],
    ['drop with a pass-rate threshold', { mode: 'drop', quality_threshold: 90 }],
    ['drop with profiling', { mode: 'drop', include_profile: true }],
  ])('does not warn about a Data Quality step in %s mode: it adds no column', (_name, params) => {
    expect(straight(params)).toEqual([]);
  });

  it('warns about tag mode, and names the column it adds', () => {
    const found = straight({ mode: 'tag' });
    expect(found).toHaveLength(1);
    expect(found[0].sink).toBe('out');
    expect(found[0].message).toContain('__dq_passed');
    expect(found[0].message).not.toContain('__dq_score');
  });

  it.each(['drop', 'fail', 'split', 'tag'])('warns when %s mode also adds the quality score', (mode) => {
    const found = straight({ mode, include_score: true });
    expect(found).toHaveLength(1);
    expect(found[0].message).toContain('__dq_score');
  });

  it('names both columns for tag mode with the quality score', () => {
    const [found] = straight({ mode: 'tag', include_score: true });
    expect(found.message).toContain('__dq_passed');
    expect(found.message).toContain('__dq_score');
  });

  it('does not warn when reject mode sends its rows out by Pass and Reject', () => {
    const rejected = node('bad', 'destination', { connector_type: 'csv', file_path: 'bad.csv' });
    const found = leaks(
      [src, dq({ mode: 'reject' }), sink, rejected],
      [edge('src', 'dq'), via('dq', 'out', 'pass'), via('dq', 'bad', 'reject')],
    );
    expect(found).toEqual([]);
  });

  it('warns when reject mode feeds a sink without going through Pass or Reject', () => {
    const found = leaks([src, dq({ mode: 'reject' }), sink], [edge('src', 'dq'), edge('dq', 'out')]);
    expect(found).toHaveLength(1);
    expect(found[0].message).toContain('_split_output');
  });

  it('warns only about the sink that is not routed', () => {
    const routed = node('good', 'destination', { connector_type: 'csv', file_path: 'good.csv' });
    const found = leaks(
      [src, dq({ mode: 'reject' }), routed, sink],
      [edge('src', 'dq'), via('dq', 'good', 'pass'), edge('dq', 'out')],
    );
    expect(found.map((f) => f.sink)).toEqual(['out']);
  });

  it('finds the step through the ones in between', () => {
    const filter = node('keep', 'filter', { condition: 'id > 0' });
    const found = leaks(
      [src, dq({ mode: 'tag' }), filter, sink],
      [edge('src', 'dq'), edge('dq', 'keep'), edge('keep', 'out')],
    );
    expect(found).toHaveLength(1);
    expect(found[0].sink).toBe('out');
  });

  it('lists only the steps that add a column when there are several', () => {
    const dropper = { ...dq({ mode: 'drop' }), id: 'dq1', data: { stepType: 'data_quality', params: dq({ mode: 'drop' }).data.params, label: 'Drop bad rows' } };
    const tagger = { ...dq({ mode: 'tag' }), id: 'dq2', data: { stepType: 'data_quality', params: dq({ mode: 'tag' }).data.params, label: 'Mark rows' } };
    const found = leaks(
      [src, dropper, tagger, sink],
      [edge('src', 'dq1'), edge('dq1', 'dq2'), edge('dq2', 'out')],
    );
    expect(found).toHaveLength(1);
    expect(found[0].message).toContain('Mark rows');
    expect(found[0].message).not.toContain('Drop bad rows');
  });

  it('still warns about the other steps that add columns, naming theirs', () => {
    const loop = node('loop', 'foreach_loop', {});
    const split = node('split', 'conditional_split', {});
    const [fromLoop] = leaks([src, loop, sink], [edge('src', 'loop'), edge('loop', 'out')]);
    expect(fromLoop.message).toContain('_batch_index');
    expect(fromLoop.message).toContain('_batch_total');
    const [fromSplit] = leaks([src, split, sink], [edge('src', 'split'), edge('split', 'out')]);
    expect(fromSplit.message).toContain('_split_output');
    expect(fromSplit.message).not.toContain('__dq_passed');
  });

  it('does not warn when a Switch sends its rows out by its named outputs', () => {
    const split = node('split', 'conditional_split', { conditions: [{ name: 'a', condition: 'x = 1' }, { name: 'b', condition: 'x = 2' }] });
    const other = node('other', 'destination', { connector_type: 'csv', file_path: 'other.csv' });
    expect(leaks([src, split, sink, other], [edge('src', 'split'), via('split', 'out', 'a'), via('split', 'other', 'b')])).toEqual([]);
  });

  it('warns when a Switch feeds a sink by its default output', () => {
    const split = node('split', 'conditional_split', { conditions: [{ name: 'a', condition: 'x = 1' }] });
    const found = leaks([src, split, sink], [edge('src', 'split'), via('split', 'out', 'output')]);
    expect(found).toHaveLength(1);
    expect(found[0].message).toContain('_split_output');
  });

  it('does not warn about a sink that has no such step upstream', () => {
    expect(leaks([src, sink], [edge('src', 'out')])).toEqual([]);
  });

  it('names only columns the engine reserves (nodeArity.RESERVED_METADATA_COLUMNS)', () => {
    const producers: Array<[string, Record<string, any>]> = [
      ['foreach_loop', {}],
      ['conditional_split', {}],
      ['data_quality', { mode: 'tag', include_score: true }],
      ['data_quality', { mode: 'reject' }],
      ['validate', {}],
      ['llm_guardrail', {}],
      ['kafka_source', {}],
    ];
    for (const [stepType, params] of producers) {
      const [found] = leaks([src, node('p', stepType, params), sink], [edge('src', 'p'), edge('p', 'out')]);
      expect(found, `${stepType} ${JSON.stringify(params)}`).toBeTruthy();
      const named = /metadata columns \(([^)]*)\)/.exec(found.message)![1].split(', ');
      expect(named.length).toBeGreaterThan(0);
      for (const column of named) {
        expect(RESERVED_METADATA_COLUMNS.has(column), `${stepType}: ${column}`).toBe(true);
      }
    }
  });
});

// A Data Quality rule the node cannot use (no column, a check it does not have, a
// value it cannot read) stops the run: the node raises before it checks a row,
// where it used to compile the rule to TRUE and pass every row. The editor says
// so before Run, from the same table of cases the node is tested against
// (fixtures/dqRuleCases.json, see dataQualityRuleCases.test.ts).
describe('validateWorkflow — Data Quality rules the step cannot run', () => {
  const src = node('src', 'source', { connector_type: 'csv', file_path: 'a.csv' });
  const check = (params: Record<string, any>, settings?: Record<string, any>) =>
    validateWorkflow(
      [src, node('dq', 'data_quality', settings ? { ...params, _settings: settings } : params)],
      [edge('src', 'dq')],
    ).filter((i) => i.nodeId === 'dq' && /^Rule \d/.test(i.message));

  it('flags a rule with no column, naming the rule, as an error on the rules field', () => {
    const found = check({ rules: [{ op: 'not_null' }], mode: 'drop' });
    expect(found).toHaveLength(1);
    expect(found[0].level).toBe('error');
    expect(found[0].field).toBe('rules');
    expect(found[0].message).toMatch(/^Rule 1 \(not_null\): Choose a column\./);
  });

  it('flags the shape the old templates used, which the node reads as no rule at all', () => {
    const found = check({ rules: [{ type: 'unique', columns: ['id'] }], on_fail: 'fail' });
    expect(found).toHaveLength(1);
    expect(found[0].message).toMatch(/Choose a column/);
  });

  it('flags a between with empty limits, and one saved as the text "10,100"', () => {
    for (const value of [['', ''], [10, ''], '10,100', undefined]) {
      const found = check({ rules: [{ column: 'amount', op: 'between', value }] });
      expect(found, JSON.stringify(value)).toHaveLength(1);
      expect(found[0].level).toBe('error');
      expect(found[0].message).toMatch(/^Rule 1 \(amount between\): /);
    }
  });

  it('reports every rule that cannot run, once each, and none that can', () => {
    const found = check({
      rules: [
        { column: 'id', op: 'not_null' },
        { column: 'region', op: 'in', value: [] },
        { column: 'amount', op: 'between', value: [10, 100] },
        { column: 'name', op: 'regex', value: '' },
      ],
    });
    expect(found.map((i) => i.message.slice(0, 6))).toEqual(['Rule 2', 'Rule 4']);
    expect(found.every((i) => i.level === 'error')).toBe(true);
  });

  it('says nothing about rules that can run', () => {
    expect(
      check({
        rules: [
          { column: 'id', op: 'not_null' },
          { column: 'amount', op: 'between', value: [10, 100] },
          { column: 'region', op: 'in', value: ['north', 'south'] },
          { column: 'email', op: 'regex', value: '^[^@]+@[^@]+$' },
          { column: 'code', op: 'min_length', value: 3 },
        ],
        mode: 'fail',
      }),
    ).toEqual([]);
  });

  it('only warns when the low limit is above the high one, because that rule runs', () => {
    const found = check({ rules: [{ column: 'amount', op: 'between', value: [100, 10] }] });
    expect(found).toHaveLength(1);
    expect(found[0].level).toBe('warning');
  });

  it('leaves a step with no rules to the required-field check', () => {
    expect(check({ rules: [], mode: 'drop' })).toEqual([]);
    expect(check({ mode: 'drop' })).toEqual([]);
  });

  it('does not look at a step that is turned off', () => {
    expect(check({ rules: [{ op: 'not_null' }] }, { deactivated: true })).toEqual([]);
  });
});

// The step reads a rule as { column, op, value } and its own settings as rules, mode,
// quality_threshold, include_score and include_profile. Anything else is ignored, and
// the old templates were full of it: { column, check: 'between', min, max } ran as a
// plain not-null, and on_failure: 'fail' left the step dropping rows. The editor warns
// about such keys before Run (the node cannot raise on them: it may be a note someone
// added, and the step still runs).
describe('validateWorkflow — Data Quality keys the step does not read', () => {
  const src = node('src', 'source', { connector_type: 'csv', file_path: 'a.csv' });
  const check = (params: Record<string, any>, settings?: Record<string, any>) =>
    validateWorkflow(
      [src, node('dq', 'data_quality', settings ? { ...params, _settings: settings } : params)],
      [edge('src', 'dq')],
    ).filter((i) => i.nodeId === 'dq' && /not read by this step/.test(i.message));

  it('warns about the keys of the old { column, check, min, max } shape, and says what the rule does', () => {
    const found = check({ rules: [{ column: 'amount', check: 'between', min: 0, max: 100 }], mode: 'drop' });
    expect(found).toHaveLength(1);
    expect(found[0].level).toBe('warning');
    expect(found[0].field).toBe('rules');
    expect(found[0].message).toMatch(/^Rule 1 \(amount not_null\): "check", "max" and "min" are not read by this step/);
    expect(found[0].message).toMatch(/only checks that the column is not empty/);
  });

  it('warns about on_failure, which is not the On Failure setting', () => {
    const found = check({ rules: [{ column: 'id', op: 'not_null' }], on_failure: 'fail' });
    expect(found).toHaveLength(1);
    expect(found[0].level).toBe('warning');
    expect(found[0].field).toBe('mode');
    expect(found[0].message).toMatch(/"on_failure" is not read by this step/);
    expect(found[0].message).toMatch(/drops failing rows/);
  });

  it('reports the whole old template shape: the rule that cannot run, and on_fail', () => {
    const all = validateWorkflow(
      [src, node('dq', 'data_quality', { rules: [{ type: 'unique', columns: ['id'] }], on_fail: 'fail' })],
      [edge('src', 'dq')],
    ).filter((i) => i.nodeId === 'dq' && (i.field === 'rules' || i.field === 'mode'));
    expect(all.map((i) => [i.level, i.field])).toEqual([
      ['error', 'rules'],
      ['warning', 'mode'],
    ]);
    expect(all[0].message).toMatch(/Choose a column\..*"columns" and "type" are not read/);
  });

  it('says nothing when every key is one the step reads', () => {
    expect(
      check({
        rules: [
          { column: 'id', op: 'not_null', value: '' },
          { column: 'amount', op: 'between', value: [10, 100] },
        ],
        mode: 'reject',
        quality_threshold: 90,
        include_score: true,
        include_profile: false,
      }),
    ).toEqual([]);
  });

  it('leaves the editor and engine keys alone, and a key that was removed', () => {
    expect(
      check({ rules: [{ column: 'id', op: 'not_null' }], mode: 'drop', on_fail: undefined }, { on_error: 'continue' }),
    ).toEqual([]);
  });

  it('does not look at a step that is turned off', () => {
    expect(check({ rules: [{ column: 'id', check: 'not_null' }], on_fail: 'fail' }, { deactivated: true })).toEqual([]);
  });
});

// A database step with a saved connection must name what it reads or writes. A Source says
// so in `source_mode`, and the node reads a query unless told otherwise; a Destination
// writes to a table. The check read a `kind` that no panel writes any more, so it took every
// Source for a table Source: a database query Source with its connection chosen was refused
// with "Missing required field: table", and Run stayed blocked however it was filled in.
describe('validateWorkflow — what a database step with a saved connection must name', () => {
  const sink = node('out', 'destination', { connector_type: 'csv', file_path: 'out.csv' });
  const database = (params: Record<string, any>) =>
    node('src', 'source', { connector_type: 'database', connection_id: 'c1', ...params });
  const sourceErrors = (params: Record<string, any>) =>
    validateWorkflow([database(params), sink], [edge('src', 'out')])
      .filter((i) => i.level === 'error' && i.nodeId === 'src')
      .map((i) => ({ field: i.field, message: i.message }));
  const sinkErrors = (params: Record<string, any>) =>
    validateWorkflow(
      [node('src', 'source', { connector_type: 'csv', file_path: 'a.csv' }), node('out', 'destination', params)],
      [edge('src', 'out')],
    )
      .filter((i) => i.level === 'error' && i.nodeId === 'out')
      .map((i) => ({ field: i.field, message: i.message }));

  it('accepts a query Source that does not say its mode: the node reads a query by default', () => {
    expect(sourceErrors({ query: 'SELECT 1' })).toEqual([]);
  });

  it('accepts a query Source in query mode', () => {
    expect(sourceErrors({ source_mode: 'query', query: 'SELECT 1' })).toEqual([]);
  });

  it.each([
    ['no mode set', {}],
    ['query mode', { source_mode: 'query' }],
    ['query mode with a blank query', { source_mode: 'query', query: '  ' }],
  ])('asks a query Source for its query, not a table (%s)', (_name, params) => {
    const found = sourceErrors(params);
    expect(found).toHaveLength(1);
    expect(found[0].field).toBe('query');
    expect(found[0].message).toMatch(/Missing required field: query/);
  });

  it('accepts a table Source that names its table, and asks one that does not', () => {
    expect(sourceErrors({ source_mode: 'table', table: 'orders' })).toEqual([]);
    const found = sourceErrors({ source_mode: 'table' });
    expect(found).toHaveLength(1);
    expect(found[0].field).toBe('table');
    expect(found[0].message).toMatch(/Missing required field: table/);
  });

  it('asks a stored-procedure Source for its procedure', () => {
    expect(sourceErrors({ source_mode: 'procedure', procedure: 'sp_refresh' })).toEqual([]);
    const found = sourceErrors({ source_mode: 'procedure' });
    expect(found).toHaveLength(1);
    expect(found[0].field).toBe('procedure');
  });

  it('ignores a `kind` left in the settings: the node does not read it', () => {
    expect(sourceErrors({ kind: 'table', source_mode: 'query', query: 'SELECT 1' })).toEqual([]);
    expect(sourceErrors({ kind: 'query', source_mode: 'table' }).map((e) => e.field)).toEqual(['table']);
  });

  it.each(['database', 'warehouse'])('asks a %s Destination for the table it writes to', (connector) => {
    expect(sinkErrors({ connector_type: connector, connection_id: 'c1', table: 'orders' })).toEqual([]);
    const found = sinkErrors({ connector_type: connector, connection_id: 'c1' });
    expect(found).toHaveLength(1);
    expect(found[0].field).toBe('table');
    expect(found[0].message).toMatch(/Missing required field: table/);
  });

  it('asks for the connection first, and for nothing else until there is one', () => {
    const noConnection = validateWorkflow(
      [node('src', 'source', { connector_type: 'database', query: 'SELECT 1' }), sink],
      [edge('src', 'out')],
    ).filter((i) => i.level === 'error' && i.nodeId === 'src');
    expect(noConnection.map((i) => i.field)).toEqual(['connection_id']);
  });
});