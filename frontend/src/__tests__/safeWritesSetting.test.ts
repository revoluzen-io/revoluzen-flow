/**
 * One safe-writes switch for both chat surfaces.
 *
 * The editor chat hard-coded `allow_safe_writes: false` while holding the
 * rich canvas context, and the global panel had the switch with a thinner
 * context — so the configuration the repair evaluation measures was one no
 * user could select. Both now read this.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { allowSafeWrites, setAllowSafeWrites } from '../ai/safeWritesSetting';

describe('safe-writes setting', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('is off until someone turns it on', () => {
    expect(allowSafeWrites()).toBe(false);
  });

  it('remembers being turned on', () => {
    setAllowSafeWrites(true);
    expect(allowSafeWrites()).toBe(true);
  });

  it('remembers being turned back off', () => {
    setAllowSafeWrites(true);
    setAllowSafeWrites(false);
    expect(allowSafeWrites()).toBe(false);
  });

  it('is the same value for both surfaces', () => {
    // There is one key, so whichever surface wrote it, the other reads it.
    setAllowSafeWrites(true);
    expect(localStorage.getItem('revoluzen_flow_allow_safe_writes')).toBe('1');
    expect(allowSafeWrites()).toBe(true);
  });

  it('reads as off rather than throwing when storage is unavailable', () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    expect(allowSafeWrites()).toBe(false);
    getItem.mockRestore();
  });

  it('does not throw when storage refuses a write', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    expect(() => setAllowSafeWrites(true)).not.toThrow();
    setItem.mockRestore();
  });
});
