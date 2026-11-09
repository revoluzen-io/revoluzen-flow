/**
 * The tooltip on a canvas node (utils/nodeDocs.ts) must describe the node the
 * editor actually offers.
 *
 * Delete Data, File System, Execute SQL and Data Profile had no entry, so
 * hovering them showed the generic "Run a delete data operation" placeholder,
 * and the Data Quality line listed a rule ("unique") the node does not have.
 * These tests fail when a palette node loses or never gets its entry.
 *
 * The palette is MODULES (modulesPanelData.ts) minus HIDDEN_TYPES, plus the
 * backend node types HIDDEN_TYPES does not hide. The frontend cannot see the
 * backend registry, so those extras are listed here by hand, as in
 * helpNodeCatalog.test.ts.
 */
import { describe, expect, it } from 'vitest';

import { MODULES } from '../components/modulesPanelData';
import { HIDDEN_TYPES } from '../components/hiddenNodeTypes';
import { NODE_DOCS, getNodeDocs } from '../utils/nodeDocs';

/** Registered backend nodes the palette shows beyond MODULES. */
const REGISTRY_EXTRA_TYPES = ['data_profile', 'scd2', 'delete_data', 'file_system', 'execute_sql_task'];

const paletteTypes = (): string[] => [
  ...MODULES.flatMap((group) => group.items.map((item) => item.type)).filter((type) => !HIDDEN_TYPES.has(type)),
  ...REGISTRY_EXTRA_TYPES,
];

describe('node tooltips (nodeDocs.ts)', () => {
  it('has its own entry for every node the palette offers', () => {
    const missing = paletteTypes().filter((type) => !NODE_DOCS[type]);
    expect(missing).toEqual([]);
  });

  it('fills all four answers for each of those nodes', () => {
    for (const type of paletteTypes()) {
      const docs = getNodeDocs(type);
      expect(docs.does.trim().length, `${type}: does`).toBeGreaterThan(10);
      expect(docs.inputs.trim().length, `${type}: inputs`).toBeGreaterThan(3);
      expect(docs.outputs.trim().length, `${type}: outputs`).toBeGreaterThan(3);
      expect(docs.failsWhen.trim().length, `${type}: failsWhen`).toBeGreaterThan(3);
    }
  });

  it('says Delete Data drops rows and deletes nothing in a database or in storage', () => {
    const docs = getNodeDocs('delete_data');
    expect(docs.does).toMatch(/rows/i);
    expect(docs.does).toMatch(/nothing is deleted/i);
  });

  it('does not list a Data Quality rule the node does not have', () => {
    // Its rules are not empty / is empty / equals / does not equal / greater
    // or less than / in a list / between / pattern / length. There is no
    // unique-value rule.
    expect(getNodeDocs('data_quality').does).not.toMatch(/unique/i);
  });
});
