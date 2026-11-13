import { describe, expect, it } from 'vitest';
import { validateWorkflow } from '../utils/validateWorkflow';

// A Delta table is a folder or cloud address in Table Path; the Delta steps
// take no saved connection. Asking for one made Delta impossible to use from
// Source and Destination (2026-11-11, mirrors ir/validator.py).
const node = (id: string, stepType: string, params: Record<string, any> = {}) => ({
  id,
  type: 'revoluzenFlowNode',
  data: { stepType, params, label: id },
});
const edge = (source: string, target: string) => ({ id: `${source}->${target}`, source, target });

const deltaIssues = (params: Record<string, any>) =>
  validateWorkflow(
    [node('src', 'source', { connector_type: 'csv', file_path: 'orders.csv' }), node('out', 'destination', { connector_type: 'delta', ...params })],
    [edge('src', 'out')],
  ).filter((i) => i.nodeId === 'out' && i.level === 'error');

describe('Delta destination checks', () => {
  it('needs no saved connection when Table Path is set', () => {
    expect(deltaIssues({ table_path: 'lake/orders' })).toEqual([]);
  });

  it('asks for the Table Path when it is missing', () => {
    const issues = deltaIssues({});
    expect(issues.some((i) => i.field === 'table_path')).toBe(true);
    expect(issues.some((i) => /saved connection/.test(i.message))).toBe(false);
  });
});
