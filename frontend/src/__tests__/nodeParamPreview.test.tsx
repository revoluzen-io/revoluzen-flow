/**
 * A node's param preview must never be able to take the canvas down.
 *
 * `paramPreview` read `(params?.group_by || []).join(', ')`. The `|| []`
 * guards null and undefined only, so a STRING passed through and
 * `"region".join` threw a TypeError. That throw happens while ReactFlow
 * renders a node, which means the nearest error boundary is the app's —
 * a single scalar param replaced the whole editor with "Something went
 * wrong", not just that node's subtitle.
 *
 * It is easy to end up with a scalar there: hand-authored pipeline JSON,
 * an importer, an older schema version, or the API. These tests pin the
 * tolerant behaviour for the three list-ish previews that crashed, and
 * the "never throws" property for every node type.
 */
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { ReactFlowProvider } from '@xyflow/react';

import RevoluzenFlowNode, { paramPreview } from '../components/nodes/RevoluzenFlowNode';

/** Render one node and hand back its text, or the thrown error. */
function renderNode(stepType: string, params: unknown) {
  const data = {
    label: `${stepType} node`,
    stepType,
    params,
    config: params,
  } as never;

  return render(
    <ReactFlowProvider>
      {/* @ts-expect-error — the node takes ReactFlow's full prop set; the
          preview path only reads `data`, which is what is under test. */}
      <RevoluzenFlowNode id="n1" type={stepType} data={data} selected={false} />
    </ReactFlowProvider>,
  );
}

// The exact shapes that crashed, plus the shapes that already worked.
const LIST_PARAM_CASES: Array<[string, Record<string, unknown>]> = [
  ['aggregate', { group_by: 'region' }],                    // string — crashed
  ['aggregate', { group_by: ['region', 'segment'] }],       // array — fine
  ['aggregate', { group_by: null }],
  ['aggregate', {}],
  ['deduplicate', { key: 'customer_id' }],                  // string — crashed
  ['deduplicate', { key: ['a', 'b'] }],
  ['deduplicate', { keys: 'customer_id' }],
  ['sort', { columns: 'revenue DESC' }],                    // string — crashed
  ['sort', { sort_by: ['a', 'b'] }],
  ['sort', { by: 'revenue DESC' }],
  ['unpivot', { value_columns: 'q1,q2,q3' }],
  ['unpivot', { value_columns: ['q1', 'q2'] }],
];

describe('node param preview tolerates scalars where arrays were assumed', () => {
  it.each(LIST_PARAM_CASES)('renders %s with %j without throwing', (stepType, params) => {
    expect(() => renderNode(stepType, params)).not.toThrow();
  });

  it('shows a string group_by rather than swallowing it', () => {
    expect(paramPreview('aggregate', { group_by: 'region' })).toContain('region');
  });

  it('still joins an array group_by with commas', () => {
    expect(paramPreview('aggregate', { group_by: ['region', 'segment'] }))
      .toContain('region, segment');
  });

  it('says "none" when a list param is missing', () => {
    expect(paramPreview('aggregate', {})).toContain('none');
    expect(paramPreview('deduplicate', { key: null })).toContain('none');
  });

  it('counts a comma-separated unpivot list, not its characters', () => {
    // "q1,q2,q3".length is 8 — the old `.length` on a string said that.
    expect(paramPreview('unpivot', { value_columns: 'q1,q2,q3' })).toContain('3 columns');
    expect(paramPreview('unpivot', { value_columns: 'q1,q2,q3' })).not.toContain('8 columns');
    expect(paramPreview('unpivot', { value_columns: ['q1', 'q2'] })).toContain('2 columns');
  });

  it('drops empty entries instead of rendering stray commas', () => {
    expect(paramPreview('sort', { columns: ['a', null, '', 'b'] })).toContain('a, b');
  });
});

describe('no node type can crash the canvas through its preview', () => {
  // Hostile shapes: the preview must degrade, never throw.
  const NASTY: unknown[] = [
    undefined, null, {}, 42, 'plain string', [], { group_by: 42 },
    { key: { nested: true } }, { columns: [null, undefined, ''] },
  ];

  const TYPES = [
    'csv_source', 'db_source', 'api_source', 'filter', 'transform',
    'deduplicate', 'aggregate', 'join', 'lookup', 'union', 'sort',
    'rename', 'typecast', 'pivot', 'unpivot', 'window', 'sample',
    'if_condition', 'slack_notify', 'local_table_sink', 'csv_sink',
    'data_quality', 'derived_column', 'unknown_future_type',
  ];

  it.each(TYPES)('%s survives every malformed param shape', (stepType) => {
    for (const params of NASTY) {
      expect(() => renderNode(stepType, params)).not.toThrow();
    }
  });
});
