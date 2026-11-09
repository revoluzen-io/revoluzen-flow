import { describe, expect, it } from 'vitest';
import { triggerLabel } from '../utils/triggerLabel';

describe('triggerLabel', () => {
  it('names a run another application started as API', () => {
    expect(triggerLabel('api')).toBe('API');
    expect(triggerLabel('API')).toBe('API');
  });

  it('reads a missing value as a manual run', () => {
    expect(triggerLabel(undefined)).toBe('Manual');
    expect(triggerLabel(null)).toBe('Manual');
    expect(triggerLabel('')).toBe('Manual');
  });

  it('names the other triggers', () => {
    expect(triggerLabel('manual')).toBe('Manual');
    expect(triggerLabel('schedule')).toBe('Scheduled');
    expect(triggerLabel('test')).toBe('Pre-publish test');
    expect(triggerLabel('webhook')).toBe('Webhook');
    expect(triggerLabel('backfill')).toBe('Backfill');
    expect(triggerLabel('websocket')).toBe('Editor');
  });

  it('ignores the id a replay or resume carries', () => {
    expect(triggerLabel('replay:abc12345')).toBe('Replay');
    expect(triggerLabel('resume:abc12345')).toBe('Resume');
  });

  it('shows a value it does not know as it came', () => {
    expect(triggerLabel('nightly_batch')).toBe('nightly_batch');
  });
});
