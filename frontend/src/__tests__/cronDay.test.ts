import { describe, expect, it } from 'vitest';
import { cronDay } from '../components/pipelines/cronDay';

describe('cron day of week', () => {
  it('treats 7 as the Sunday it means', () => {
    expect(cronDay(7)).toBe(0);
  });

  it('leaves 0 to 6 alone', () => {
    expect([0, 1, 2, 3, 4, 5, 6].map(cronDay)).toEqual([0, 1, 2, 3, 4, 5, 6]);
  });
});
