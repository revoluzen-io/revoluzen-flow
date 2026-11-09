/**
 * The built-in templates' Data Quality steps must be written in the shape the
 * Data Quality node reads (backend/revoluzen_flow/nodes/quality.py): rules of
 * { column, op, value? } and a `mode`.
 *
 * Eleven steps were not. They used { type: 'unique', columns } or
 * { column, check, min, max } with `on_fail` / `on_failure`, none of which the
 * node reads. A rule it cannot read passes every row, so "Drop empty rows" kept
 * the empty rows, "Validate required" never failed a run, and a unique-key
 * check did nothing. These tests fail when a template goes back to such a shape.
 * backend/tests/test_template_data_quality_steps.py runs the same steps through
 * the real node on a fixture with one bad row per rule.
 */
import { describe, expect, it } from 'vitest';

import { TEMPLATE_CATALOG } from '../templates/catalog';
import { branchPortsFor } from '../utils/branchPorts';

// The node's rule vocabulary: `_DQ_OPS` and the `mode` options in quality.py.
// The frontend cannot see the backend, so the lists are written out here, as in
// nodeDocsCoverage.test.ts; the backend test holds them to the node.
const OPS_WITHOUT_VALUE = ['not_null', 'is_null'];
const OPS_WITH_ONE_VALUE = ['eq', 'ne', 'gt', 'lt', 'gte', 'lte'];
const OPS_WITH_A_LIST = ['in', 'not_in'];
const OPS_WITH_A_LENGTH = ['min_length', 'max_length'];
const KNOWN_OPS = [
  ...OPS_WITHOUT_VALUE,
  ...OPS_WITH_ONE_VALUE,
  ...OPS_WITH_A_LIST,
  'regex',
  'between',
  ...OPS_WITH_A_LENGTH,
];
const MODES = ['drop', 'fail', 'tag', 'reject', 'split'];
const RULE_KEYS = ['column', 'op', 'value'];
const PARAM_KEYS = ['rules', 'mode', 'quality_threshold', 'include_score', 'include_profile'];

const isScalar = (v: unknown): boolean => ['string', 'number', 'boolean'].includes(typeof v);

/** What is wrong with a rule, or null when the node would read it as written. */
function ruleProblem(rule: any): string | null {
  if (!rule || typeof rule !== 'object' || Array.isArray(rule)) return 'is not an object';
  const stray = Object.keys(rule).filter((k) => !RULE_KEYS.includes(k));
  if (stray.length) return `has keys the node does not read: ${stray.join(', ')}`;
  if (typeof rule.column !== 'string' || !rule.column || rule.column !== rule.column.trim()) {
    return 'has no column';
  }
  if (!KNOWN_OPS.includes(rule.op)) return `op ${JSON.stringify(rule.op)} is not one the node has`;

  const v = rule.value;
  if (OPS_WITHOUT_VALUE.includes(rule.op)) {
    return v === undefined ? null : `${rule.op} takes no value`;
  }
  if (OPS_WITH_ONE_VALUE.includes(rule.op)) {
    return isScalar(v) ? null : `${rule.op} needs one value`;
  }
  if (OPS_WITH_A_LIST.includes(rule.op)) {
    return Array.isArray(v) && v.length > 0 && v.every(isScalar) ? null : `${rule.op} needs a list of values`;
  }
  if (OPS_WITH_A_LENGTH.includes(rule.op)) {
    return Number.isInteger(v) && v >= 0 ? null : `${rule.op} needs a whole number`;
  }
  if (rule.op === 'regex') {
    if (typeof v !== 'string' || !v) return 'regex needs a pattern';
    try {
      new RegExp(v);
    } catch {
      return `regex pattern ${JSON.stringify(v)} does not compile`;
    }
    return null;
  }
  // between: [low, high], both numbers or both strings, low not above high.
  if (!Array.isArray(v) || v.length !== 2 || !v.every(isScalar) || typeof v[0] !== typeof v[1]) {
    return 'between needs [low, high]';
  }
  return v[0] <= v[1] ? null : 'between has low above high';
}

/** What is wrong with a step's rules, one line each; empty when the node reads them as written. */
function rulesProblems(params: Record<string, unknown>): string[] {
  const rules = params.rules;
  if (!Array.isArray(rules) || rules.length === 0) return ['has no rules'];
  return rules.flatMap((rule: unknown, i: number) => {
    const problem = ruleProblem(rule);
    return problem ? [`rule ${i + 1} ${problem}`] : [];
  });
}

/** What is wrong with a step's mode and its other parameters. */
function paramsProblems(params: Record<string, unknown>): string[] {
  const problems: string[] = [];
  if (!MODES.includes(params.mode as string)) {
    problems.push(`mode ${JSON.stringify(params.mode)} is not one of ${MODES.join(', ')}`);
  }
  const stray = Object.keys(params).filter((k) => !PARAM_KEYS.includes(k));
  if (stray.length) problems.push(`sets ${stray.join(', ')}, which the node does not read`);
  return problems;
}

const dqSteps = TEMPLATE_CATALOG.flatMap((template) =>
  template.steps.filter((step) => step.type === 'data_quality').map((step) => ({ template, step })),
);
const where = ({ template, step }: (typeof dqSteps)[number]): string =>
  `${template.id} / ${step.id} "${step.label}"`;

describe('the shape check', () => {
  // The shapes the templates used before. The node reads none of their keys.
  const OLD_NOT_NULL = { rules: [{ type: 'not_null', columns: ['order_id'] }], on_fail: 'drop' };
  const OLD_UNIQUE = { rules: [{ type: 'unique', columns: ['id'] }], on_fail: 'fail' };
  const OLD_BETWEEN = {
    rules: [{ column: 'amount', check: 'between', min: 0, max: 1000000 }],
    on_failure: 'tag',
  };

  it('catches every shape the node ignored', () => {
    for (const params of [OLD_NOT_NULL, OLD_UNIQUE, OLD_BETWEEN]) {
      expect(rulesProblems(params), JSON.stringify(params)).not.toEqual([]);
      expect(paramsProblems(params), JSON.stringify(params)).not.toEqual([]);
    }
  });

  it('passes the same checks written the way the node reads them', () => {
    const fixed = [
      { rules: [{ column: 'order_id', op: 'not_null' }], mode: 'drop' },
      { rules: [{ column: 'amount', op: 'between', value: [0, 1000000] }], mode: 'tag' },
      { rules: [{ column: 'email', op: 'regex', value: '^[^@]+@[^@]+\\.[^@]+$' }], mode: 'reject' },
    ];
    for (const params of fixed) {
      expect(rulesProblems(params), JSON.stringify(params)).toEqual([]);
      expect(paramsProblems(params), JSON.stringify(params)).toEqual([]);
    }
  });

  it('wants the value each op needs, and no more', () => {
    expect(ruleProblem({ column: 'a', op: 'between', value: [9, 5] })).toMatch(/low above high/);
    expect(ruleProblem({ column: 'a', op: 'between', value: 5 })).toMatch(/\[low, high\]/);
    expect(ruleProblem({ column: 'a', op: 'in', value: [] })).toMatch(/list/);
    expect(ruleProblem({ column: 'a', op: 'gt' })).toMatch(/one value/);
    expect(ruleProblem({ column: 'a', op: 'regex', value: '(' })).toMatch(/does not compile/);
    expect(ruleProblem({ column: 'a', op: 'min_length', value: -1 })).toMatch(/whole number/);
    expect(ruleProblem({ column: 'a', op: 'not_null', value: 'x' })).toMatch(/no value/);
    expect(ruleProblem({ column: '', op: 'not_null' })).toMatch(/no column/);
    expect(ruleProblem({ column: 'a', op: 'unique' })).toMatch(/not one the node has/);
  });
});

describe('built-in templates: Data Quality steps', () => {
  it('has steps to check', () => {
    expect(dqSteps.length).toBeGreaterThan(0);
  });

  it('write rules the node reads: a column, an op, and a value where the op needs one', () => {
    const problems = dqSteps.flatMap((entry) =>
      rulesProblems(entry.step.params).map((p) => `${where(entry)}: ${p}`),
    );
    expect(problems).toEqual([]);
  });

  it('name an On Failure mode the node has, and set no parameter it ignores', () => {
    const problems = dqSteps.flatMap((entry) =>
      paramsProblems(entry.step.params).map((p) => `${where(entry)}: ${p}`),
    );
    expect(problems).toEqual([]);
  });

  it('do not ask for a unique-value rule, which the node does not have', () => {
    // To collapse duplicate keys a template uses a Deduplicate step.
    const asking = dqSteps.filter((entry) => /unique/i.test(JSON.stringify(entry.step.params)));
    expect(asking.map(where)).toEqual([]);
  });
});

describe('built-in templates: wiring out of a named output', () => {
  it('only leave a step by an output port that the step offers', () => {
    const problems: string[] = [];
    for (const template of TEMPLATE_CATALOG) {
      const steps = new Map(template.steps.map((s) => [s.id, s]));
      for (const c of template.connections) {
        if (!c.from_port) continue;
        const from = steps.get(c.from_step);
        if (!from) {
          problems.push(`${template.id}: ${c.from_step} -> ${c.to_step} starts at a step that is not there`);
          continue;
        }
        const offered = branchPortsFor(from.type, from.params).map((p) => p.id);
        if (!offered.includes(c.from_port)) {
          problems.push(
            `${template.id}: ${c.from_step} -> ${c.to_step} leaves by "${c.from_port}", ` +
              `but ${from.type} offers ${offered.join(', ')}`,
          );
        }
      }
    }
    expect(problems).toEqual([]);
  });

  it('feed both the Pass and the Reject output of a Data Quality step in reject mode', () => {
    const rejecting = dqSteps.filter((entry) => (entry.step.params as any).mode === 'reject');
    expect(rejecting.length).toBeGreaterThan(0);
    const problems: string[] = [];
    for (const entry of rejecting) {
      const used = entry.template.connections
        .filter((c) => c.from_step === entry.step.id)
        .map((c) => c.from_port ?? 'output');
      for (const port of ['pass', 'reject']) {
        if (!used.includes(port)) problems.push(`${where(entry)}: nothing is wired to its ${port} output`);
      }
    }
    expect(problems).toEqual([]);
  });

  it('use a named port only from a Data Quality step that is in reject mode', () => {
    const problems: string[] = [];
    for (const entry of dqSteps) {
      if ((entry.step.params as any).mode === 'reject') continue;
      const named = entry.template.connections.filter(
        (c) => c.from_step === entry.step.id && c.from_port && c.from_port !== 'output',
      );
      if (named.length) problems.push(`${where(entry)}: leaves by ${named.map((c) => c.from_port).join(', ')}`);
    }
    expect(problems).toEqual([]);
  });
});
