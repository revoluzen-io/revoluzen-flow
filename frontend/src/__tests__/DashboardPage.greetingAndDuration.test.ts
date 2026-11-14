/**
 * The Dashboard's greeting addresses the account's role, and its Avg duration
 * card has a trend line like the cards beside it.
 */
import { describe, expect, it } from 'vitest';

import { avgDurationLine, greetingName } from '../components/pages/DashboardPage';

describe('who the Dashboard greets', () => {
  it('names the role, as the chip beside the page title does', () => {
    expect(greetingName('super_admin', 'Asha Menon')).toBe('Admin');
    expect(greetingName('admin', 'Asha Menon')).toBe('Admin');
    expect(greetingName('developer', 'Daniel Okafor')).toBe('Developer');
    expect(greetingName('viewer', 'Lena Fischer')).toBe('Viewer');
  });

  it('keeps the name when the role is not known', () => {
    expect(greetingName(undefined, 'Asha Menon')).toBe('Asha Menon');
    expect(greetingName('auditor', 'Ravi Kumar')).toBe('Ravi Kumar');
  });
});

describe('the Avg duration trend line', () => {
  it('averages each bucket over the runs that recorded a time', () => {
    expect(avgDurationLine([{ dur: 3000, timed: 2 }, { dur: 1000, timed: 1 }])).toEqual([1500, 1000]);
  });

  it('carries the last average through quiet buckets, and the first back to the start', () => {
    expect(avgDurationLine([
      { dur: 0, timed: 0 }, { dur: 2000, timed: 2 }, { dur: 0, timed: 0 }, { dur: 900, timed: 3 },
    ])).toEqual([1000, 1000, 1000, 300]);
  });

  it('is empty when no run recorded a time', () => {
    expect(avgDurationLine([{ dur: 0, timed: 0 }, { dur: 0, timed: 0 }])).toEqual([]);
  });
});
