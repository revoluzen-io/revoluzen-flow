/**
 * The Data Quality rule editor stores each rule as { column, op, value } in the
 * shape the node reads: [low, high] for between, a list for in / not_in, numbers
 * as numbers. These tests type into the real boxes and read back what the panel
 * would save (see dataQualityRules.test.ts for the rules behind it).
 */
import React, { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import DataQualityRules, { IgnoredStepSettings } from '../components/DataQualityRules';

let saved: any[] = [];

/** Holds the rules the way the node's params would, and records what was last saved. */
function Panel({
  initial = [],
  columns = ['id', 'amount', 'region'],
  sourceTypes,
}: {
  initial?: any[];
  columns?: string[];
  sourceTypes?: Record<string, string>;
}) {
  const [rules, setRules] = useState<any[]>(initial);
  saved = rules;
  return <DataQualityRules rules={rules} columns={columns} sourceTypes={sourceTypes} onChange={setRules} />;
}

const box = (label: string) => screen.getByLabelText(label) as HTMLInputElement;
const type = (label: string, text: string) => fireEvent.change(box(label), { target: { value: text } });
const pick = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });

describe('adding a rule', () => {
  it('starts as a not_null check on the first column, with no value', () => {
    render(<Panel />);
    fireEvent.click(screen.getByText('+ Add rule'));
    expect(saved).toEqual([{ column: 'id', op: 'not_null' }]);
    expect('value' in saved[0]).toBe(false);
    expect(screen.queryByLabelText('Rule 1 value')).toBeNull();
  });
});

describe('between', () => {
  it('has a low and a high box, and stores [low, high] with numbers as numbers', () => {
    render(<Panel initial={[{ column: 'amount', op: 'not_null' }]} />);
    pick('Rule 1 check', 'between');
    expect(saved).toEqual([{ column: 'amount', op: 'between', value: ['', ''] }]);

    type('Rule 1 low', '10');
    type('Rule 1 high', '100');
    expect(saved).toEqual([{ column: 'amount', op: 'between', value: [10, 100] }]);
    expect(screen.queryByLabelText('Rule 1 value')).toBeNull();
  });

  it('keeps what is typed while it is typed', () => {
    render(<Panel initial={[{ column: 'amount', op: 'between', value: ['', ''] }]} />);
    type('Rule 1 low', '0.');
    expect(box('Rule 1 low').value).toBe('0.');
    type('Rule 1 low', '0.50');
    expect(box('Rule 1 low').value).toBe('0.50');
    // With the high limit still empty the pair is not numbers yet; once it is, it is.
    expect(saved[0].value).toEqual(['0.50', '']);
    type('Rule 1 high', '1');
    expect(saved[0].value).toEqual([0.5, 1]);
    expect(box('Rule 1 low').value).toBe('0.50');
  });

  it('stores two texts as text', () => {
    render(<Panel initial={[{ column: 'created', op: 'between', value: ['', ''] }]} columns={['created']} />);
    type('Rule 1 low', '2024-01-01');
    type('Rule 1 high', '2024-12-31');
    expect(saved[0].value).toEqual(['2024-01-01', '2024-12-31']);
  });

  it('shows the limits a template stores', () => {
    render(<Panel initial={[{ column: 'amount', op: 'between', value: [0, 1000000] }]} />);
    expect(box('Rule 1 low').value).toBe('0');
    expect(box('Rule 1 high').value).toBe('1000000');
    expect(screen.queryByText(/both limits|above high|plain text/i)).toBeNull();
  });

  it('says so when the limits are missing, in the wrong order, or were saved as plain text', () => {
    render(<Panel initial={[{ column: 'amount', op: 'between', value: ['', ''] }]} />);
    expect(screen.getByText(/both limits/i)).toBeTruthy();
    type('Rule 1 low', '100');
    type('Rule 1 high', '10');
    expect(screen.getByText(/above high/i)).toBeTruthy();
    type('Rule 1 high', '1000');
    expect(screen.queryByText(/above high|both limits/i)).toBeNull();
  });

  it('flags a value saved as the text "10,100" by the old box', () => {
    render(<Panel initial={[{ column: 'amount', op: 'between', value: '10,100' }]} />);
    expect(screen.getByText(/plain text/i)).toBeTruthy();
    // Typing the limits again stores the right shape.
    type('Rule 1 low', '10');
    type('Rule 1 high', '100');
    expect(saved[0].value).toEqual([10, 100]);
    expect(screen.queryByText(/plain text/i)).toBeNull();
  });
});

describe('in / not_in', () => {
  it.each(['in', 'not_in'])('%s: one box of comma-separated values, stored as a list', (op) => {
    render(<Panel initial={[{ column: 'region', op: 'not_null' }]} />);
    pick('Rule 1 check', op);
    expect(saved).toEqual([{ column: 'region', op, value: [] }]);

    type('Rule 1 values', 'north, south');
    expect(saved).toEqual([{ column: 'region', op, value: ['north', 'south'] }]);
    type('Rule 1 values', '1, 2, 3');
    expect(saved[0].value).toEqual([1, 2, 3]);
    expect(screen.queryByLabelText('Rule 1 value')).toBeNull();
  });

  it('keeps the comma while the next value is typed', () => {
    render(<Panel initial={[{ column: 'region', op: 'in', value: [] }]} />);
    type('Rule 1 values', 'a,');
    expect(box('Rule 1 values').value).toBe('a,');
    expect(saved[0].value).toEqual(['a']);
    type('Rule 1 values', 'a,b');
    expect(saved[0].value).toEqual(['a', 'b']);
  });

  it('shows a stored list as text', () => {
    render(<Panel initial={[{ column: 'region', op: 'in', value: ['north', 'south'] }]} />);
    expect(box('Rule 1 values').value).toBe('north, south');
  });

  it('asks for a value, and flags a list saved as one text', () => {
    render(<Panel initial={[{ column: 'region', op: 'in', value: [] }]} />);
    expect(screen.getByText(/at least one value/i)).toBeTruthy();
  });

  it('flags a list saved as the one text "a,b"', () => {
    render(<Panel initial={[{ column: 'region', op: 'in', value: 'a,b' }]} />);
    expect(screen.getByText(/one piece of text/i)).toBeTruthy();
  });
});

describe('the other ops', () => {
  it('a single value is a number when it is one', () => {
    render(<Panel initial={[{ column: 'amount', op: 'gt', value: '' }]} />);
    type('Rule 1 value', '1.');
    expect(saved[0].value).toBe('1.');
    type('Rule 1 value', '1.5');
    expect(saved[0].value).toBe(1.5);
    type('Rule 1 value', 'abc');
    expect(saved[0].value).toBe('abc');
  });

  it('a regex pattern is always text', () => {
    render(<Panel initial={[{ column: 'id', op: 'regex', value: '' }]} />);
    type('Rule 1 value', '100');
    expect(saved[0].value).toBe('100');
  });

  it('says what is missing as soon as a check that needs a pattern is chosen', () => {
    render(<Panel initial={[{ column: 'id', op: 'not_null' }]} />);
    expect(screen.queryByText(/Enter a pattern/i)).toBeNull();
    pick('Rule 1 check', 'regex');
    expect(screen.getByText(/Enter a pattern/i)).toBeTruthy();
    type('Rule 1 value', '^A');
    expect(screen.queryByText(/Enter a pattern/i)).toBeNull();
    expect(saved[0]).toEqual({ column: 'id', op: 'regex', value: '^A' });
  });

  it('min_length is a number', () => {
    render(<Panel initial={[{ column: 'id', op: 'min_length', value: '' }]} />);
    expect(screen.getByText(/whole number/i)).toBeTruthy();
    type('Rule 1 value', '3');
    expect(saved[0].value).toBe(3);
    expect(screen.queryByText(/whole number/i)).toBeNull();
  });

  it('is_null and not_null drop the value', () => {
    render(<Panel initial={[{ column: 'id', op: 'eq', value: 5 }]} />);
    pick('Rule 1 check', 'is_null');
    expect(saved).toEqual([{ column: 'id', op: 'is_null' }]);
    expect('value' in saved[0]).toBe(false);
  });
});

describe('switching the check', () => {
  it('carries a value to an op of the same kind, and a single value into a list', () => {
    render(<Panel initial={[{ column: 'amount', op: 'eq', value: 5 }]} />);
    pick('Rule 1 check', 'gt');
    expect(saved[0]).toEqual({ column: 'amount', op: 'gt', value: 5 });
    pick('Rule 1 check', 'in');
    expect(saved[0]).toEqual({ column: 'amount', op: 'in', value: [5] });
    expect(box('Rule 1 values').value).toBe('5');
    pick('Rule 1 check', 'not_in');
    expect(saved[0]).toEqual({ column: 'amount', op: 'not_in', value: [5] });
  });

  it('starts between empty rather than inheriting a value that is not a range', () => {
    render(<Panel initial={[{ column: 'amount', op: 'eq', value: 5 }]} />);
    pick('Rule 1 check', 'between');
    expect(saved[0]).toEqual({ column: 'amount', op: 'between', value: ['', ''] });
  });
});

describe('a text column keeps numbers as text', () => {
  const sourceTypes = { id: 'BIGINT', amount: 'DOUBLE', region: 'VARCHAR' };

  it('a number column stores numbers, a text column stores the same typing as text', () => {
    render(<Panel initial={[{ column: 'amount', op: 'in', value: [] }]} sourceTypes={sourceTypes} />);
    type('Rule 1 values', '1, 2');
    expect(saved[0].value).toEqual([1, 2]);

    pick('Rule 1 column', 'region');
    expect(saved[0]).toEqual({ column: 'region', op: 'in', value: ['1', '2'] });
    type('Rule 1 values', '1, 2, 3');
    expect(saved[0].value).toEqual(['1', '2', '3']);

    pick('Rule 1 column', 'id');
    expect(saved[0]).toEqual({ column: 'id', op: 'in', value: [1, 2, 3] });
  });
});

describe('several rules', () => {
  it('edits one rule and leaves the others as they were', () => {
    const rules = [
      { column: 'id', op: 'not_null' },
      { column: 'amount', op: 'between', value: [10, 100] },
      { column: 'region', op: 'in', value: ['north', 'south'] },
    ];
    render(<Panel initial={rules} />);
    type('Rule 2 high', '500');
    expect(saved).toEqual([
      { column: 'id', op: 'not_null' },
      { column: 'amount', op: 'between', value: [10, 500] },
      { column: 'region', op: 'in', value: ['north', 'south'] },
    ]);
  });

  it('shows the right values after a rule above is removed', () => {
    const rules = [
      { column: 'id', op: 'eq', value: 7 },
      { column: 'amount', op: 'between', value: [10, 100] },
    ];
    render(<Panel initial={rules} />);
    fireEvent.click(screen.getByLabelText('Remove rule 1'));
    expect(saved).toEqual([{ column: 'amount', op: 'between', value: [10, 100] }]);
    expect(box('Rule 1 low').value).toBe('10');
    expect(box('Rule 1 high').value).toBe('100');
  });

  it('shows the right value after a rule of the same kind above is removed', () => {
    const rules = [
      { column: 'id', op: 'eq', value: 7 },
      { column: 'amount', op: 'gt', value: 9 },
    ];
    render(<Panel initial={rules} />);
    expect(box('Rule 1 value').value).toBe('7');
    fireEvent.click(screen.getByLabelText('Remove rule 1'));
    expect(saved).toEqual([{ column: 'amount', op: 'gt', value: 9 }]);
    expect(box('Rule 1 value').value).toBe('9');
  });

  it('keeps showing a rule\'s own column when it is not among the upstream columns', () => {
    render(<Panel initial={[{ column: 'amount', op: 'not_null' }]} columns={['id', 'region']} />);
    expect((screen.getByLabelText('Rule 1 column') as HTMLSelectElement).value).toBe('amount');
  });
});

describe('keys the step does not read', () => {
  it('names them under the rule, and a click removes only those keys', () => {
    const rules = [
      { column: 'id', op: 'not_null' },
      { column: 'amount', check: 'between', min: 0, max: 100 },
    ];
    render(<Panel initial={rules} />);
    expect(screen.getAllByText(/Not read by this step/)).toHaveLength(1);
    expect(screen.getByText(/Not read by this step: check, max, min\./)).toBeTruthy();
    fireEvent.click(screen.getByLabelText('Rule 2: remove check, max, min'));
    expect(saved).toEqual([
      { column: 'id', op: 'not_null' },
      { column: 'amount' },
    ]);
    expect(screen.queryByText(/Not read by this step/)).toBeNull();
  });

  it('says nothing about a rule that has only the keys the step reads', () => {
    render(<Panel initial={[{ column: 'id', op: 'not_null', value: '' }, { column: 'n', op: 'gt', value: 1 }]} />);
    expect(screen.queryByText(/Not read by this step/)).toBeNull();
  });

  it('shows the rule as the step reads it: with no op, a not_null check', () => {
    render(<Panel initial={[{ column: 'amount', check: 'between', min: 0, max: 100 }]} />);
    expect((screen.getByLabelText('Rule 1 check') as HTMLSelectElement).value).toBe('not_null');
    pick('Rule 1 check', 'between');
    type('Rule 1 low', '0');
    type('Rule 1 high', '100');
    // The stray keys stay until they are removed; the rule itself is now the one that was meant.
    expect(saved[0]).toEqual({ column: 'amount', check: 'between', min: 0, max: 100, op: 'between', value: [0, 100] });
    fireEvent.click(screen.getByLabelText('Rule 1: remove check, max, min'));
    expect(saved).toEqual([{ column: 'amount', op: 'between', value: [0, 100] }]);
  });
});

describe('settings of the step it does not read', () => {
  it('names them, mentions On Failure for the setting people wrote instead of it, and removes them', () => {
    const removed: string[][] = [];
    render(<IgnoredStepSettings params={{ rules: [], mode: 'drop', on_failure: 'fail', description: 'x' }} onRemove={(keys) => removed.push(keys)} />);
    expect(screen.getByText(/Not read by this step: description, on_failure\./)).toBeTruthy();
    expect(screen.getByText(/On Failure above/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText('Remove description, on_failure'));
    expect(removed).toEqual([['description', 'on_failure']]);
  });

  it('does not mention On Failure for another setting', () => {
    render(<IgnoredStepSettings params={{ rules: [], description: 'x' }} onRemove={() => {}} />);
    expect(screen.queryByText(/On Failure above/)).toBeNull();
  });

  it('shows nothing when every setting is one the step reads', () => {
    const { container } = render(
      <IgnoredStepSettings params={{ rules: [], mode: 'tag', quality_threshold: 90, _settings: {} }} onRemove={() => {}} />,
    );
    expect(container.textContent).toBe('');
  });
});

describe('a rule with no column yet', () => {
  it('asks for one instead of showing the first column it does not have', () => {
    render(<Panel initial={[{ column: '', op: 'not_null' }]} />);
    const select = screen.getByLabelText('Rule 1 column') as HTMLSelectElement;
    expect(select.value).toBe('');
    expect(select.options[select.selectedIndex].text).toBe('Choose a column');
    pick('Rule 1 column', 'amount');
    expect(saved).toEqual([{ column: 'amount', op: 'not_null' }]);
    expect((screen.getByLabelText('Rule 1 column') as HTMLSelectElement).value).toBe('amount');
  });
});

describe('without upstream columns', () => {
  it('types the column name', () => {
    render(<Panel initial={[{ column: '', op: 'not_null' }]} columns={[]} />);
    type('Rule 1 column', 'amount');
    expect(saved).toEqual([{ column: 'amount', op: 'not_null' }]);
  });
});
