/**
 * JoinKeySuggester — when the key side repeats a few of its values (a CRM
 * export that lists a customer again when their tier changes), the suggestion
 * says how many sampled rows repeat instead of the general many-to-many line.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';

vi.mock('../api/client', async (importOriginal) => {
  const real: any = await importOriginal();
  return {
    ...real,
    api: {
      ...real.api,
      suggestJoinKeys: vi.fn(async () => ({
        step_id: 'join', left_step: 'paid', right_step: 'cust', left_label: 'Paid only', right_label: 'Customers',
        sampled_rows: { left: 2000, right: 2000 },
        suggestions: [
          {
            left: 'customer_id', right: 'customer_id', same_name: true, score: 98, shape: 'many-to-many', evidence: [],
            left_matched_pct: 100, right_matched_pct: 31, estimated: true,
            repeat_note: 'customer_id repeats in 40 of the 2,000 sampled Customers rows: those rows will match more than once',
          },
          {
            left: 'region', right: 'region', same_name: true, score: 55, shape: 'many-to-many', evidence: [],
            left_matched_pct: 100, right_matched_pct: 100, estimated: false, repeat_note: null,
          },
        ],
      })),
    },
  };
});

import JoinKeySuggester from '../components/join/JoinKeySuggester';

describe('JoinKeySuggester — a key with a few repeats', () => {
  it('says how many rows repeat for the key, and keeps the general warning for a group', async () => {
    render(<JoinKeySuggester nodeId="join" onChange={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: 'Suggest keys' }));
    const note = await screen.findByTestId('join-key-repeats');
    expect(note.textContent).toBe('customer_id repeats in 40 of the 2,000 sampled Customers rows: those rows will match more than once.');
    expect(screen.getByText(/About 100% of values match/)).toBeTruthy();
    expect(screen.getAllByText('Neither side is unique: the join can multiply rows.')).toHaveLength(1);
  });
});
