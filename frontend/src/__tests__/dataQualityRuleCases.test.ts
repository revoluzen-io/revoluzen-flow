/**
 * The editor and the node agree on which Data Quality rules can run.
 *
 * The node (backend/revoluzen_flow/nodes/quality.py, `dq_rule_problems`) stops the
 * run on a rule it cannot use: no column, a check it does not have, or a value it
 * cannot read. Before, such a rule compiled to TRUE and passed every row. The
 * editor says the same thing before Run (utils/dataQualityRules.ts, called from
 * validateWorkflow.ts). Both are held to one table, fixtures/dqRuleCases.json,
 * which backend/tests/test_dq_unusable_rules.py reads too.
 */
import { describe, expect, it } from 'vitest';

import table from './fixtures/dqRuleCases.json';
import { DQ_OPS, dqRuleIssues, ignoredParamIssues } from '../utils/dataQualityRules';
import { TEMPLATE_CATALOG } from '../templates/catalog';

interface Case {
  name: string;
  rule: unknown;
  usable: boolean;
}
const CASES: Case[] = (table as { cases: Case[] }).cases;
const errorsFor = (rule: unknown) => dqRuleIssues([rule]).filter((i) => i.level === 'error');

describe('the case table', () => {
  it('has rules that run and rules that do not', () => {
    expect(CASES.filter((c) => c.usable).length).toBeGreaterThan(10);
    expect(CASES.filter((c) => !c.usable).length).toBeGreaterThan(40);
  });

  it('covers every op: one that runs, and for the ops that take a value, one that cannot', () => {
    const ops = (cases: Case[]) =>
      new Set(cases.flatMap((c) => (c.rule && typeof c.rule === 'object' && !Array.isArray(c.rule) ? [(c.rule as any).op || 'not_null'] : [])));
    const run = ops(CASES.filter((c) => c.usable));
    const cannot = ops(CASES.filter((c) => !c.usable));
    expect(DQ_OPS.filter((op) => !run.has(op))).toEqual([]);
    expect(DQ_OPS.filter((op) => op !== 'not_null' && op !== 'is_null' && !cannot.has(op))).toEqual([]);
  });
});

describe('the editor agrees with the node on every rule in the table', () => {
  it.each(CASES.map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const errors = errorsFor(c.rule);
    expect(errors.length === 0, JSON.stringify({ rule: c.rule, errors })).toBe(c.usable);
  });
});

describe('what the editor says', () => {
  it('names the rule by its number, column and check', () => {
    const [issue] = dqRuleIssues([
      { column: 'id', op: 'not_null' },
      { column: 'amount', op: 'between', value: '10,100' },
    ]);
    expect(issue.level).toBe('error');
    expect(issue.message).toMatch(/^Rule 2 \(amount between\): /);
    expect(issue.message).toMatch(/plain text/i);
  });

  it('says what to do for each kind of problem', () => {
    const say = (rule: unknown) => dqRuleIssues([rule])[0]?.message ?? '';
    expect(say({ op: 'not_null' })).toMatch(/Rule 1 \(not_null\): Choose a column\./);
    expect(say({ column: 'v', op: 'unique' })).toMatch(/"unique" is not one of the checks/);
    expect(say({ column: 'v', op: 'eq' })).toMatch(/Enter a value to compare with/);
    expect(say({ column: 'v', op: 'gt', value: [1] })).toMatch(/one value, not a list/);
    expect(say({ column: 'v', op: 'in', value: [] })).toMatch(/at least one value/);
    expect(say({ column: 'v', op: 'in', value: ['a', null] })).toMatch(/empty/);
    expect(say({ column: 'v', op: 'between', value: ['', ''] })).toMatch(/both limits/i);
    expect(say({ column: 'v', op: 'regex', value: '' })).toMatch(/Enter a pattern/);
    expect(say({ column: 'v', op: 'regex', value: 100 })).toMatch(/must be text/);
    expect(say({ column: 'v', op: 'min_length', value: 'abc' })).toMatch(/whole number/);
    expect(say(null)).toMatch(/Rule 1 is not a rule/);
  });

  it('reports a rule with two problems once, with both', () => {
    const issues = dqRuleIssues([{ column: '', op: 'between' }]);
    expect(issues).toHaveLength(1);
    expect(issues[0].message).toMatch(/Choose a column\./);
    expect(issues[0].message).toMatch(/low and high limits/);
  });

  it('numbers every rule, and leaves the ones that run out', () => {
    const issues = dqRuleIssues([
      { column: 'id', op: 'not_null' },
      { column: '', op: 'not_null' },
      { column: 'id', op: 'in', value: [] },
    ]);
    expect(issues.map((i) => i.message.slice(0, 6))).toEqual(['Rule 2', 'Rule 3']);
  });

  it('asks for a list when the rules are not one', () => {
    expect(dqRuleIssues({ column: 'id', op: 'not_null' })[0].message).toMatch(/must be a list/);
    expect(dqRuleIssues('id is not null')[0].level).toBe('error');
  });

  it('has nothing to say when there are no rules (a missing list is reported elsewhere)', () => {
    for (const rules of [undefined, null, [], '']) expect(dqRuleIssues(rules)).toEqual([]);
  });

  it('warns, without stopping the run, when the low limit is above the high one', () => {
    const issues = dqRuleIssues([{ column: 'n', op: 'between', value: [100, 10] }]);
    expect(issues).toHaveLength(1);
    expect(issues[0].level).toBe('warning');
    expect(issues[0].message).toMatch(/above high/i);
  });

  it('warns about a list saved as one piece of text, which still runs', () => {
    const issues = dqRuleIssues([{ column: 's', op: 'in', value: 'a,b' }]);
    expect(issues).toHaveLength(1);
    expect(issues[0].level).toBe('warning');
    expect(issues[0].message).toMatch(/one piece of text/i);
  });
});

describe('the built-in templates', () => {
  it('only use rules that can run, written with keys the step reads', () => {
    const problems = TEMPLATE_CATALOG.flatMap((template) =>
      template.steps
        .filter((step) => step.type === 'data_quality')
        .flatMap((step) => [
          ...dqRuleIssues((step.params as any).rules),
          ...ignoredParamIssues(step.params),
        ].map((i) => `${template.id} / ${step.id}: ${i.message}`)),
    );
    expect(problems).toEqual([]);
  });
});
