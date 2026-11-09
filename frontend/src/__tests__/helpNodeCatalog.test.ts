/**
 * The Help page's Node Reference must list the nodes the editor offers.
 *
 * It used to be a hand-written list of 91 entries (one per file format, cloud
 * and sink, plus nodes that are hidden or off by default) against the 45
 * nodes in the palette, and its Delete Data entry promised files and S3
 * objects when the node only removes rows. These tests fail when the palette
 * and the page drift apart again, and say which side moved.
 *
 * The palette is MODULES (modulesPanelData.ts) minus HIDDEN_TYPES, plus every
 * backend node type that HIDDEN_TYPES does not hide, shown under the label
 * the backend gives it (makeReconciledModulesHook). The frontend cannot see
 * the backend registry, so those extras are listed here by hand: if one is
 * added or hidden, the "same nodes" test fails until this list and the Help
 * catalog both follow.
 */
import { describe, expect, it } from 'vitest';

import { MODULES } from '../components/modulesPanelData';
import { HIDDEN_TYPES } from '../components/hiddenNodeTypes';
import { NODE_CATALOG, NODE_COUNT } from '../components/help/nodeCatalog';

/** Registered backend nodes the palette shows beyond MODULES, by group. */
const REGISTRY_EXTRAS: Record<string, string[]> = {
  Transform: ['Data Profile', 'SCD Type 2'],
  Action: ['Delete Data', 'File System', 'Execute SQL'],
};

const paletteByGroup = (): Record<string, string[]> => {
  const out: Record<string, string[]> = {};
  for (const group of MODULES) {
    const visible = group.items.filter((i) => !HIDDEN_TYPES.has(i.type)).map((i) => i.label);
    out[group.name] = [...visible, ...(REGISTRY_EXTRAS[group.name] ?? [])];
  }
  return out;
};

describe('Help → Node Reference', () => {
  it('lists the same groups as the node panel, in the same order', () => {
    expect(NODE_CATALOG.map((g) => g.category)).toEqual(MODULES.map((g) => g.name));
  });

  it('lists exactly the nodes the palette offers, group by group', () => {
    const palette = paletteByGroup();
    for (const group of NODE_CATALOG) {
      expect({ group: group.category, nodes: group.nodes.map((n) => n.name).sort() })
        .toEqual({ group: group.category, nodes: [...(palette[group.category] ?? [])].sort() });
    }
  });

  it('counts every palette node once', () => {
    const palette = Object.values(paletteByGroup()).flat();
    expect(NODE_COUNT).toBe(palette.length);
    expect(new Set(NODE_CATALOG.flatMap((g) => g.nodes.map((n) => n.name))).size).toBe(NODE_COUNT);
  });

  it('describes every node', () => {
    for (const group of NODE_CATALOG) {
      for (const node of group.nodes) {
        expect(node.desc.trim().length, `${node.name} has no description`).toBeGreaterThan(20);
      }
    }
  });

  it('says Delete Data removes rows only, not files or S3 objects', () => {
    const entry = NODE_CATALOG.flatMap((g) => g.nodes).find((n) => n.name === 'Delete Data');
    expect(entry).toBeDefined();
    expect(entry!.desc).toMatch(/rows/i);
    expect(entry!.desc).not.toMatch(/S3/);
    // It may point at File System for files, but must not claim to do that itself.
    expect(entry!.desc).not.toMatch(/^Delete files/i);
  });
});
