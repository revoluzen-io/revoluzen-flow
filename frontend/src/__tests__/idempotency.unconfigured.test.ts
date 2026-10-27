/**
 * The re-run badge must not contradict the engine (2026-10-24).
 *
 * `classifyIdempotency` treated an EMPTY `mode` as append, so a sink the
 * user had not configured yet showed a red "Re-runs duplicate". The engine
 * disagrees: `nodes/local_table.py` reads
 * `(params.get("mode") or SINK_MODE_REPLACE)` and the delta sink defaults to
 * `overwrite` (`nodes/sinks.py`) — both replace, which is idempotent.
 *
 * Two rules are pinned here:
 *   1. Where the engine's default IS known, mirror it.
 *   2. Where it is not, say nothing. A node with no mode chosen has no
 *      re-run class to report, and a wrong red badge is worse than none —
 *      it trains the user to ignore the badge that matters.
 *
 * The explicitly-configured cases are pinned too, so "don't warn on an
 * unconfigured node" can never widen into "don't warn".
 */

import { describe, it, expect } from 'vitest';
import { classifyIdempotency } from '../utils/idempotency';

describe('unconfigured sinks make no re-run claim', () => {
  it('local_table_sink with no mode mirrors the engine default (replace)', () => {
    const r = classifyIdempotency('local_table_sink', {});
    expect(r?.cls).toBe('replace');
    expect(r?.tone).toBe('amber');
  });

  it('delta_sink with no mode mirrors the engine default (overwrite -> replace)', () => {
    expect(classifyIdempotency('delta_sink', {})?.cls).toBe('replace');
  });

  it.each(['db_sink', 'warehouse_sink', 'parquet_sink'])(
    '%s with no mode reports nothing rather than guessing',
    (type) => {
      expect(classifyIdempotency(type, {})).toBeNull();
    },
  );

  it.each(['destination', 'output'])(
    'the generic %s placeholder reports nothing until a mode is chosen',
    (type) => {
      expect(classifyIdempotency(type, {})).toBeNull();
    },
  );
});

describe('a configured sink still warns', () => {
  it('append is still flagged red on every sink type', () => {
    for (const type of ['local_table_sink', 'db_sink', 'destination', 'output']) {
      const r = classifyIdempotency(type, { mode: 'append' });
      expect(r?.tone, `${type} in append mode`).toBe('red');
      expect(r?.cls).toBe('append_risky');
    }
  });

  it('merge without keys is still flagged', () => {
    expect(classifyIdempotency('db_sink', { mode: 'merge' })?.cls).toBe('merge');
    expect(classifyIdempotency('db_sink', { mode: 'merge' })?.tone).toBe('amber');
  });

  it('merge WITH keys is safe', () => {
    const r = classifyIdempotency('db_sink', { mode: 'merge', merge_on: ['id'] });
    expect(r?.tone).toBe('green');
  });

  it('an unrecognised non-empty mode keeps the pessimistic call', () => {
    // Not the same thing as an unset mode: the user chose something, and the
    // classifier does not know it is safe.
    expect(classifyIdempotency('db_sink', { mode: 'sideways' })?.cls).toBe('append_risky');
  });

  it('external side-effect sinks are unaffected by the mode rules', () => {
    expect(classifyIdempotency('email_sink', {})?.cls).toBe('external');
    expect(classifyIdempotency('webhook_sink', {})?.tone).toBe('red');
  });

  it('a file sink with no mode still replaces, as before', () => {
    expect(classifyIdempotency('csv_sink', {})?.cls).toBe('replace');
  });
});
