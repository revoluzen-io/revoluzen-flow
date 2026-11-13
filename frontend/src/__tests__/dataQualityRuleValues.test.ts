/**
 * What the Data Quality editor stores for a rule's value.
 *
 * The node (backend/revoluzen_flow/nodes/quality.py) reads `{ column, op, value }`
 * and needs `[low, high]` for between and a list for in / not_in. The editor had
 * one text box per rule and stored whatever was typed as a string, so "10,100"
 * for between was read as BETWEEN '1' AND '0' and "a,b" for in matched only the
 * text "a,b": every row dropped. These tests pin the shape each op stores, and
 * hold it to the shape the built-in templates are written in.
 */
import { describe, expect, it } from 'vitest';

import { TEMPLATE_CATALOG } from '../templates/catalog';
import {
  DQ_OPS,
  dqRuleIssues,
  dqValueKind,
  ignoredParamIssues,
  ignoredParamKeys,
  ignoredRuleKeys,
  ignoredRuleKeysNote,
  isFailureSettingKey,
  isTextType,
  numberFromText,
  ruleNote,
  ruleProblem,
  textsFromValue,
  valueProblem,
  valueForColumnType,
  valueForOpChange,
  valueFromTexts,
} from '../utils/dataQualityRules';

describe('what text is a number', () => {
  it('reads plain decimals as numbers', () => {
    expect(numberFromText('10')).toBe(10);
    expect(numberFromText(' 10 ')).toBe(10);
    expect(numberFromText('-5')).toBe(-5);
    expect(numberFromText('3.14')).toBe(3.14);
    expect(numberFromText('0')).toBe(0);
    expect(Object.is(numberFromText('-0'), 0)).toBe(true);
  });

  it('leaves codes, ids and anything else as text', () => {
    for (const text of ['007', '02134', '1e3', '1,000', '.5', '5.', '+5', '0x10', 'abc', '', ' ', '-']) {
      expect(numberFromText(text), JSON.stringify(text)).toBeNull();
    }
  });

  it('leaves a number too big to hold exactly as text', () => {
    expect(numberFromText('9007199254740993')).toBeNull();
    expect(numberFromText('12345678901234567890')).toBeNull();
  });
});

describe('between stores [low, high]', () => {
  it('stores two numbers as numbers', () => {
    expect(valueFromTexts('between', ['10', '100'])).toEqual([10, 100]);
    expect(valueFromTexts('between', [' 0.5 ', '1'])).toEqual([0.5, 1]);
  });

  it('stores two texts as text (dates, names)', () => {
    expect(valueFromTexts('between', ['2024-01-01', '2024-12-31'])).toEqual(['2024-01-01', '2024-12-31']);
    expect(valueFromTexts('between', ['a', 'm'])).toEqual(['a', 'm']);
  });

  it('never mixes a number with text', () => {
    expect(valueFromTexts('between', ['10', 'z'])).toEqual(['10', 'z']);
  });

  it('always has two entries, empty until typed', () => {
    expect(valueFromTexts('between', ['', ''])).toEqual(['', '']);
    expect(valueFromTexts('between', ['10', ''])).toEqual(['10', '']);
    expect(valueFromTexts('between', [])).toEqual(['', '']);
  });

  it('is never the string "10,100", which the node reads as BETWEEN \'1\' AND \'0\'', () => {
    const stored = valueFromTexts('between', ['10', '100']);
    expect(Array.isArray(stored)).toBe(true);
    expect(typeof stored).not.toBe('string');
  });
});

describe('in / not_in store a list', () => {
  it.each(['in', 'not_in'])('%s: splits on commas and trims', (op) => {
    expect(valueFromTexts(op, ['north, south ,east'])).toEqual(['north', 'south', 'east']);
    expect(valueFromTexts(op, ['a,,b, '])).toEqual(['a', 'b']);
  });

  it.each(['in', 'not_in'])('%s: a list of numbers is numbers', (op) => {
    expect(valueFromTexts(op, ['1, 2, 3'])).toEqual([1, 2, 3]);
    expect(valueFromTexts(op, ['2.5,10'])).toEqual([2.5, 10]);
  });

  it.each(['in', 'not_in'])('%s: one non-number makes the whole list text', (op) => {
    expect(valueFromTexts(op, ['1, b'])).toEqual(['1', 'b']);
    expect(valueFromTexts(op, ['007, 12'])).toEqual(['007', '12']);
  });

  it.each(['in', 'not_in'])('%s: an empty box is an empty list, not the string ""', (op) => {
    expect(valueFromTexts(op, [''])).toEqual([]);
    expect(valueFromTexts(op, ['  , '])).toEqual([]);
  });

  it('is never the string "a,b", which the node reads as the one value "a,b"', () => {
    expect(valueFromTexts('in', ['a,b'])).toEqual(['a', 'b']);
  });
});

describe('the ops with one value', () => {
  it.each(['eq', 'ne', 'gt', 'lt', 'gte', 'lte'])('%s: a number is stored as a number, other text as text', (op) => {
    expect(valueFromTexts(op, ['100'])).toBe(100);
    expect(valueFromTexts(op, ['-2.5'])).toBe(-2.5);
    expect(valueFromTexts(op, ['active'])).toBe('active');
    expect(valueFromTexts(op, ['007'])).toBe('007');
    expect(valueFromTexts(op, [''])).toBe('');
  });

  it('keeps what is half-typed as text until it is a number', () => {
    expect(valueFromTexts('gt', ['1.'])).toBe('1.');
    expect(valueFromTexts('gt', ['-'])).toBe('-');
    expect(valueFromTexts('gt', ['1.5'])).toBe(1.5);
  });

  it('min_length / max_length: a number', () => {
    expect(valueFromTexts('min_length', ['5'])).toBe(5);
    expect(valueFromTexts('max_length', ['12'])).toBe(12);
  });

  it('regex: a pattern is always text, even one made of digits', () => {
    expect(valueFromTexts('regex', ['100'])).toBe('100');
    expect(valueFromTexts('regex', ['^[A-Z]{3}$'])).toBe('^[A-Z]{3}$');
    expect(valueFromTexts('regex', [' 5 '])).toBe(' 5 ');
  });

  it('not_null / is_null: no value at all', () => {
    expect(valueFromTexts('not_null', ['x'])).toBeUndefined();
    expect(valueFromTexts('is_null', [])).toBeUndefined();
    expect(valueFromTexts(undefined, ['x'])).toBeUndefined();
  });
});

describe('a text column keeps numeric-looking values as text', () => {
  // DuckDB compares a text column with a string, and refuses a number.
  const text = { textColumn: true };

  it('one value', () => {
    expect(valueFromTexts('eq', ['123'], text)).toBe('123');
  });

  it('a list', () => {
    expect(valueFromTexts('in', ['1, 2'], text)).toEqual(['1', '2']);
  });

  it('a range', () => {
    expect(valueFromTexts('between', ['10', '100'], text)).toEqual(['10', '100']);
  });

  it('is read again when the rule moves to a text column and back', () => {
    expect(valueForColumnType('eq', 100, text)).toBe('100');
    expect(valueForColumnType('in', [1, 2], text)).toEqual(['1', '2']);
    expect(valueForColumnType('eq', '100')).toBe(100);
    expect(valueForColumnType('in', ['1', '2'])).toEqual([1, 2]);
    expect(valueForColumnType('not_null', 'x', text)).toBeUndefined();
  });

  it('knows which DuckDB types are text', () => {
    for (const t of ['VARCHAR', 'varchar(20)', 'TEXT', 'STRING', 'CHAR(2)', ' VARCHAR ']) {
      expect(isTextType(t), t).toBe(true);
    }
    for (const t of ['INTEGER', 'BIGINT', 'DOUBLE', 'DECIMAL(10,2)', 'DATE', 'BOOLEAN', '', undefined, null]) {
      expect(isTextType(t as any), String(t)).toBe(false);
    }
  });
});

describe('the boxes show what is stored', () => {
  it('reads each shape back as text', () => {
    expect(textsFromValue('between', [10, 100])).toEqual(['10', '100']);
    expect(textsFromValue('in', ['a', 'b'])).toEqual(['a, b']);
    expect(textsFromValue('not_in', [1, 2, 3])).toEqual(['1, 2, 3']);
    expect(textsFromValue('eq', 5)).toEqual(['5']);
    expect(textsFromValue('eq', undefined)).toEqual(['']);
    expect(textsFromValue('not_null', 'x')).toEqual([]);
  });

  it('shows a value saved as plain text in the first box, as it is', () => {
    expect(textsFromValue('between', '10,100')).toEqual(['10,100', '']);
    expect(textsFromValue('in', 'a,b')).toEqual(['a,b']);
  });

  it('round-trips every value the built-in templates store', () => {
    const rules = TEMPLATE_CATALOG.flatMap((t) =>
      t.steps.filter((s) => s.type === 'data_quality').flatMap((s) => ((s.params as any).rules ?? []) as any[]),
    );
    expect(rules.length).toBeGreaterThan(0);
    for (const rule of rules) {
      const texts = textsFromValue(rule.op, rule.value);
      expect(valueFromTexts(rule.op, texts), JSON.stringify(rule)).toEqual(rule.value);
      expect(ruleProblem(rule), JSON.stringify(rule)).toBeNull();
    }
  });

  it('covers between and a list, which the templates write as arrays', () => {
    const ops = new Set(
      TEMPLATE_CATALOG.flatMap((t) =>
        t.steps.filter((s) => s.type === 'data_quality').flatMap((s) => ((s.params as any).rules ?? []).map((r: any) => r.op)),
      ),
    );
    expect(ops.has('between')).toBe(true);
  });
});

describe('switching a rule to another op', () => {
  it('keeps the value between ops that take the same kind', () => {
    expect(valueForOpChange('eq', 'gt', 5)).toBe(5);
    expect(valueForOpChange('in', 'not_in', [1, 2])).toEqual([1, 2]);
    expect(valueForOpChange('between', 'between', [1, 2])).toEqual([1, 2]);
  });

  it('turns one value into a one-item list, and a list into its first item', () => {
    expect(valueForOpChange('eq', 'in', 5)).toEqual([5]);
    expect(valueForOpChange('eq', 'in', '')).toEqual([]);
    expect(valueForOpChange('in', 'eq', ['a', 'b'])).toBe('a');
    expect(valueForOpChange('not_in', 'gt', [7, 8])).toBe(7);
  });

  it('starts empty when the kinds do not match', () => {
    expect(valueForOpChange('eq', 'between', 5)).toEqual(['', '']);
    expect(valueForOpChange('between', 'eq', [1, 2])).toBe('');
    expect(valueForOpChange('between', 'in', [1, 2])).toEqual([]);
    expect(valueForOpChange('not_null', 'eq', undefined)).toBe('');
    expect(valueForOpChange('not_null', 'between', undefined)).toEqual(['', '']);
    expect(valueForOpChange(undefined, 'in', undefined)).toEqual([]);
  });

  it('drops the value for an op that takes none', () => {
    expect(valueForOpChange('eq', 'not_null', 5)).toBeUndefined();
    expect(valueForOpChange('between', 'is_null', [1, 2])).toBeUndefined();
  });

  it('keeps a pattern as text and reads a number as one when it is not a pattern', () => {
    expect(valueForOpChange('eq', 'regex', 100)).toBe('100');
    expect(valueForOpChange('regex', 'eq', '100')).toBe(100);
    expect(valueForOpChange('regex', 'eq', '^a')).toBe('^a');
  });
});

describe('the problems the editor points out', () => {
  it('between needs both limits, in the right order', () => {
    expect(ruleProblem({ op: 'between', value: [10, 100] })).toBeNull();
    expect(ruleProblem({ op: 'between', value: ['2024-01-01', '2024-12-31'] })).toBeNull();
    expect(ruleProblem({ op: 'between', value: ['', ''] })).toMatch(/both limits/i);
    expect(ruleProblem({ op: 'between', value: [10, ''] })).toMatch(/both limits/i);
    expect(ruleProblem({ op: 'between', value: [100, 10] })).toMatch(/above high/i);
    expect(ruleProblem({ op: 'between', value: ['100', '10'] })).toMatch(/above high/i);
    expect(ruleProblem({ op: 'between' })).toMatch(/low and high/i);
  });

  it('between saved as plain text is called out', () => {
    expect(ruleProblem({ op: 'between', value: '10,100' })).toMatch(/plain text/i);
  });

  it('in / not_in need a value, and a list saved as one text is called out', () => {
    expect(ruleProblem({ op: 'in', value: ['a', 'b'] })).toBeNull();
    expect(ruleProblem({ op: 'in', value: 'a' })).toBeNull();
    expect(ruleProblem({ op: 'not_in', value: [] })).toMatch(/at least one/i);
    expect(ruleProblem({ op: 'in', value: '' })).toMatch(/at least one/i);
    expect(ruleProblem({ op: 'in', value: 'a,b' })).toMatch(/one piece of text/i);
  });

  it('min_length / max_length need a whole number', () => {
    expect(ruleProblem({ op: 'min_length', value: 3 })).toBeNull();
    expect(ruleProblem({ op: 'max_length', value: '12' })).toBeNull();
    expect(ruleProblem({ op: 'min_length', value: '' })).toMatch(/whole number/i);
    expect(ruleProblem({ op: 'max_length', value: 'abc' })).toMatch(/whole number/i);
    expect(ruleProblem({ op: 'min_length', value: 1.5 })).toMatch(/whole number/i);
    expect(ruleProblem({ op: 'min_length', value: -1 })).toMatch(/whole number/i);
  });

  it('one-value ops need a value, and not a list', () => {
    expect(ruleProblem({ op: 'eq', value: '' })).toBeNull(); // an empty string is a value
    expect(ruleProblem({ op: 'eq', value: 0 })).toBeNull();
    expect(ruleProblem({ op: 'eq' })).toMatch(/value to compare with/i);
    expect(ruleProblem({ op: 'gt', value: null })).toMatch(/value to compare with/i);
    expect(ruleProblem({ op: 'lte', value: [1, 2] })).toMatch(/one value/i);
  });

  it('regex needs a pattern, written as text', () => {
    expect(ruleProblem({ op: 'regex', value: '^a' })).toBeNull();
    expect(ruleProblem({ op: 'regex', value: ' ' })).toBeNull();
    expect(ruleProblem({ op: 'regex', value: '' })).toMatch(/pattern/i);
    expect(ruleProblem({ op: 'regex' })).toMatch(/pattern/i);
    expect(ruleProblem({ op: 'regex', value: 100 })).toMatch(/must be text/i);
  });

  it('a list may not hold an empty entry', () => {
    expect(ruleProblem({ op: 'not_in', value: ['a', null] })).toMatch(/empty/i);
    expect(ruleProblem({ op: 'in', value: [['a']] })).toMatch(/empty/i);
  });

  it('keeps what the node stops on apart from what only deserves a note', () => {
    // Stops the run:
    expect(valueProblem('between', ['', ''])).not.toBeNull();
    expect(valueProblem('in', [])).not.toBeNull();
    // Runs, but is probably not what was meant:
    expect(valueProblem('between', [100, 10])).toBeNull();
    expect(ruleNote({ op: 'between', value: [100, 10] })).toMatch(/above high/i);
    expect(valueProblem('in', 'a,b')).toBeNull();
    expect(ruleNote({ op: 'in', value: 'a,b' })).toMatch(/one piece of text/i);
  });

  it('says nothing about the other ops', () => {
    expect(ruleProblem({ op: 'not_null' })).toBeNull();
    expect(ruleProblem({ op: 'eq', value: '' })).toBeNull();
    expect(ruleProblem({ op: 'regex', value: '^a' })).toBeNull();
    expect(ruleProblem({})).toBeNull();
  });
});

describe('the ops', () => {
  it('lists the fourteen ops the node has, each with a kind', () => {
    expect(DQ_OPS).toHaveLength(14);
    expect(new Set(DQ_OPS).size).toBe(14);
    expect(DQ_OPS.map(dqValueKind).sort()).toEqual(
      ['list', 'list', 'none', 'none', 'one', 'one', 'one', 'one', 'one', 'one', 'one', 'one', 'one', 'range'].sort(),
    );
  });
});

describe('keys the step does not read', () => {
  it('lists the keys of a rule other than column, op and value', () => {
    expect(ignoredRuleKeys({ column: 'amount', check: 'between', min: 0, max: 100 })).toEqual(['check', 'max', 'min']);
    expect(ignoredRuleKeys({ column: 'id', op: 'not_null', value: '' })).toEqual([]);
    expect(ignoredRuleKeys({ type: 'unique', columns: ['id'] })).toEqual(['columns', 'type']);
  });

  it('does not count a key that is undefined, an editor key, or something that is not a rule', () => {
    expect(ignoredRuleKeys({ column: 'id', min: undefined })).toEqual([]);
    expect(ignoredRuleKeys({ column: 'id', _id: 7 })).toEqual([]);
    for (const notARule of [null, undefined, 'id', 5, ['id']]) expect(ignoredRuleKeys(notARule)).toEqual([]);
  });

  it('lists the settings of the step other than the ones it reads', () => {
    expect(ignoredParamKeys({ rules: [], mode: 'drop', on_failure: 'fail', description: 'x' })).toEqual(['description', 'on_failure']);
    expect(
      ignoredParamKeys({ rules: [], mode: 'drop', quality_threshold: 0, include_score: false, include_profile: false }),
    ).toEqual([]);
    expect(ignoredParamKeys({ rules: [], _settings: { deactivated: true }, _step_id: 'dq' })).toEqual([]);
    expect(ignoredParamKeys({ on_fail: undefined })).toEqual([]);
    expect(ignoredParamKeys(undefined)).toEqual([]);
  });

  it('knows the settings people wrote instead of mode', () => {
    for (const key of ['on_fail', 'on_failure', 'onFailure', 'ON_FAIL']) expect(isFailureSettingKey(key), key).toBe(true);
    for (const key of ['mode', 'failure_mode', 'on_failed_rows']) expect(isFailureSettingKey(key), key).toBe(false);
  });

  it('says what a rule does as written, and where each key belongs', () => {
    const note = ignoredRuleKeysNote({ column: 'amount', check: 'between', min: 0, max: 100 })!;
    expect(note).toContain('"check", "max" and "min" are not read by this step');
    expect(note).toContain('reads only column, op and value');
    expect(note).toContain('The check goes in "op".');
    expect(note).toContain('value goes in "value"');
    expect(note).toContain('With no "op" the rule only checks that the column is not empty.');
  });

  it('leaves the "no op" sentence out once an op is set, and when the rule does not run anyway', () => {
    expect(ignoredRuleKeysNote({ column: 'a', op: 'gt', value: 1, severity: 'high' })).toBe(
      '"severity" is not read by this step, which reads only column, op and value.',
    );
    expect(ignoredRuleKeysNote({ type: 'unique', columns: ['id'] }, false)).not.toContain('only checks');
    expect(ignoredRuleKeysNote({ column: 'a', op: 'gt', value: 1 })).toBeNull();
  });

  it('says what the step does instead of reading on_failure', () => {
    const [issue] = ignoredParamIssues({ rules: [], on_failure: 'fail' });
    expect(issue.field).toBe('mode');
    expect(issue.message).toContain('"on_failure" is not read by this step');
    expect(issue.message).toContain('"mode" (drop, fail, tag, reject or split)');
    expect(issue.message).toContain('With no "mode" the step drops failing rows.');
    const [withMode] = ignoredParamIssues({ rules: [], mode: 'tag', on_fail: 'fail' });
    expect(withMode.message).not.toContain('drops failing rows');
  });

  it('names the settings it does read when another one is ignored', () => {
    const [issue] = ignoredParamIssues({ rules: [], mode: 'drop', description: 'nightly check' });
    expect(issue.field).toBe('description');
    expect(issue.message).toBe(
      '"description" is not read by this step, which reads rules, rule_sets, mode, quality_threshold, include_score and include_profile.',
    );
    expect(ignoredParamIssues({ rules: [], mode: 'drop' })).toEqual([]);
  });

  it('is a warning, not an error, in the check before Run', () => {
    const issues = dqRuleIssues([{ column: 'amount', check: 'between', min: 0, max: 100 }]);
    expect(issues).toHaveLength(1);
    expect(issues[0].level).toBe('warning');
    expect(issues[0].message).toMatch(/^Rule 1 \(amount not_null\): "check", "max" and "min" are not read/);
  });

  it('puts the unread keys in the error when the rule cannot run either', () => {
    const issues = dqRuleIssues([{ type: 'unique', columns: ['id'] }]);
    expect(issues).toHaveLength(1);
    expect(issues[0].level).toBe('error');
    expect(issues[0].message).toContain('Choose a column.');
    expect(issues[0].message).toContain('"columns" and "type" are not read by this step');
    expect(issues[0].message).not.toContain('only checks that the column is not empty');
  });

  it('puts them in one warning with the note about a range the wrong way round', () => {
    const issues = dqRuleIssues([{ column: 'n', op: 'between', value: [100, 10], severity: 'high' }]);
    expect(issues).toHaveLength(1);
    expect(issues[0].level).toBe('warning');
    expect(issues[0].message).toContain('Low is above high');
    expect(issues[0].message).toContain('"severity" is not read');
  });
});
