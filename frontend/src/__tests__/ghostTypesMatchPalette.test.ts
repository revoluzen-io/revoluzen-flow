/**
 * The AI ghost-suggestion whitelist must match the palette.
 *
 * It used to be a second hand-written list in hiddenNodeTypes.ts with a
 * "keep in sync" comment, and it did not stay in sync: 11 entries named
 * node types the backend does not register (so the AI could suggest a
 * node the user then could not add — exactly what the gate exists to
 * prevent) and 10 real palette nodes were missing (so valid suggestions
 * were silently dropped).
 *
 * It is now derived from MODULES. These tests exist so that if anyone
 * reintroduces a literal list, or hides a palette node without thinking
 * about suggestions, CI says so.
 */
import { describe, expect, it } from 'vitest';

import { MODULES, VALID_GHOST_TYPES } from '../components/modulesPanelData';
import { HIDDEN_TYPES } from '../components/hiddenNodeTypes';

const paletteTypes = MODULES.flatMap((c) => c.items.map((i) => i.type));

describe('VALID_GHOST_TYPES', () => {
  it('contains exactly the visible curated palette', () => {
    const expected = [...new Set(paletteTypes.filter((t) => !HIDDEN_TYPES.has(t)))].sort();
    expect([...VALID_GHOST_TYPES].sort()).toEqual(expected);
  });

  it('never suggests a type that is hidden from the palette', () => {
    const leaked = [...VALID_GHOST_TYPES].filter((t) => HIDDEN_TYPES.has(t));
    expect(leaked).toEqual([]);
  });

  it('covers every curated palette entry', () => {
    const missing = paletteTypes.filter(
      (t) => !HIDDEN_TYPES.has(t) && !VALID_GHOST_TYPES.has(t),
    );
    expect(missing).toEqual([]);
  });

  it('is not empty, so a bad derive cannot silently disable all suggestions', () => {
    // An empty set would make remapLegacyGhost drop every suggestion —
    // a total feature outage that no other assertion here would catch.
    expect(VALID_GHOST_TYPES.size).toBeGreaterThan(30);
  });
});

describe('MODULES', () => {
  it('declares no duplicate node types across categories', () => {
    const seen = new Map<string, number>();
    for (const t of paletteTypes) seen.set(t, (seen.get(t) ?? 0) + 1);
    expect([...seen].filter(([, n]) => n > 1)).toEqual([]);
  });

  it('does not ship a palette tile for a hidden type', () => {
    // Hiding a type is how legacy nodes are retired; leaving its tile in
    // MODULES would keep it addable while claiming it is gone.
    expect(paletteTypes.filter((t) => HIDDEN_TYPES.has(t))).toEqual([]);
  });
});
