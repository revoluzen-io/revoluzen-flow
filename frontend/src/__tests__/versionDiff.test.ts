/**
 * What changed between two saved versions of a pipeline, as the Versions
 * view shows it: steps added, removed and changed with every setting before
 * and after, links between steps, and pipeline settings.
 */
import { describe, expect, it } from 'vitest';

import { diffVersions, settingLabel, settingText } from '../components/pipelines/versionDiff';

const read = { id: 'read', type: 'source', label: 'Read customers', params: { connection_id: 'c1', object: 'Account' }, position: { x: 0, y: 0 } };
const keep = { id: 'keep', type: 'filter', label: 'Keep active', params: { condition: "status = 'Active'" }, position: { x: 260, y: 0 } };
const write = { id: 'write', type: 'destination', label: 'Write customers', params: { connection_id: 'c2', write_mode: 'replace' }, position: { x: 520, y: 0 } };
const notify = { id: 'notify', type: 'slack_notify', label: 'Notify sales', params: { channel: '#sales' }, position: { x: 780, y: 0 } };
const clean = { id: 'clean', type: 'data_wrangler', label: 'Clean emails', params: { column: 'email' }, position: { x: 130, y: 0 } };

const older = {
  name: 'Customer sync',
  parameters: [{ name: 'since', type: 'string', default: '2026-01-01' }],
  steps: [read, keep, write, notify],
  connections: [
    { from_step: 'read', to_step: 'keep' },
    { from_step: 'keep', to_step: 'write' },
    { from_step: 'write', to_step: 'notify' },
  ],
};
const newer = {
  name: 'Customer sync',
  parameters: [{ name: 'since', type: 'string', default: 'yesterday' }],
  steps: [
    read,
    clean,
    { ...keep, params: { condition: "status in ('Active', 'Trial')" } },
    { ...write, params: { connection_id: 'c2', write_mode: 'upsert', key: 'Id' } },
  ],
  connections: [
    { from_step: 'read', to_step: 'clean' },
    { from_step: 'clean', to_step: 'keep' },
    { from_step: 'keep', to_step: 'write' },
  ],
};

describe('comparing two versions', () => {
  const diff = diffVersions(older, newer, { names: { c1: 'Salesforce CRM', c2: 'Warehouse' } });
  const step = (id: string) => diff.steps.find((s) => s.id === id)!;

  it('sorts every step into added, removed, changed or the same', () => {
    expect(step('clean').kind).toBe('added');
    expect(step('notify').kind).toBe('removed');
    expect(step('keep').kind).toBe('changed');
    expect(step('write').kind).toBe('changed');
    expect(step('read').kind).toBe('same');
    expect([diff.added, diff.removed, diff.changed, diff.same]).toEqual([1, 1, 2, 1]);
    expect(diff.identical).toBe(false);
  });

  it('shows each setting before and after, by name, with connection names', () => {
    const rows = step('write').rows;
    const mode = rows.find((r) => r.key === 'write_mode')!;
    expect(mode).toMatchObject({ label: 'Write mode', before: 'replace', after: 'upsert', changed: true });
    const key = rows.find((r) => r.key === 'key')!;
    expect(key).toMatchObject({ before: '—', after: 'Id', changed: true });
    const conn = rows.find((r) => r.key === 'connection_id')!;
    expect(conn).toMatchObject({ label: 'Connection', before: 'Warehouse', after: 'Warehouse', changed: false });
    // A removed step keeps its settings, with nothing after.
    expect(step('notify').rows[0]).toMatchObject({ before: '#sales', after: null });
  });

  it('lists links added and removed', () => {
    const changed = diff.links.filter((l) => l.kind !== 'same').map((l) => `${l.kind} ${l.from}>${l.to}`);
    expect(changed.sort()).toEqual(['added clean>keep', 'added read>clean', 'removed read>keep', 'removed write>notify']);
    expect(diff.linksChanged).toBe(true);
  });

  it('lists pipeline settings that changed', () => {
    expect(diff.pipeline).toEqual([
      expect.objectContaining({ label: 'Parameter “since”', before: 'string, default 2026-01-01', after: 'string, default yesterday' }),
    ]);
  });

  it('reads left to right, as the canvas does', () => {
    expect(diff.steps.map((s) => s.id)).toEqual(['read', 'clean', 'keep', 'write', 'notify']);
  });
});

describe('versions that are the same', () => {
  it('says so', () => {
    const diff = diffVersions(older, JSON.parse(JSON.stringify(older)));
    expect(diff.identical).toBe(true);
    expect(diff.layoutOnly).toBe(false);
  });

  it('tells a moved step from a change', () => {
    const moved = JSON.parse(JSON.stringify(older));
    moved.steps[1].position = { x: 300, y: 80 };
    const diff = diffVersions(older, moved);
    expect(diff.identical).toBe(false);
    expect(diff.layoutOnly).toBe(true);
    expect(diff.changed).toBe(0);
  });

  it('counts a renamed step as changed, not removed and added', () => {
    const renamed = JSON.parse(JSON.stringify(older));
    renamed.steps[1].label = 'Keep live customers';
    const diff = diffVersions(older, renamed);
    expect([diff.added, diff.removed, diff.changed]).toEqual([0, 0, 1]);
    expect(diff.steps.find((s) => s.id === 'keep')!.rows[0]).toMatchObject({
      label: 'Step name', before: 'Keep active', after: 'Keep live customers',
    });
  });
});

describe('labels and values', () => {
  it('names settings the way people read them', () => {
    expect(settingLabel('write_mode')).toBe('Write mode');
    expect(settingLabel('batchSize')).toBe('Batch size');
    expect(settingLabel('connection_id')).toBe('Connection');
    expect(settingLabel('_settings')).toBe('Step settings');
  });

  it('writes values the same way every time', () => {
    expect(settingText(true)).toBe('Yes');
    expect(settingText('')).toBe('—');
    expect(settingText(['a', 'b'])).toBe('a, b');
    expect(settingText({ b: 1, a: 2 })).toBe(settingText({ a: 2, b: 1 }));
  });

  it('names the shared rule sets a Data Quality step uses, not their ids', () => {
    const a = { steps: [], connections: [] };
    const b = { steps: [{ id: 'dq', type: 'data_quality', params: { rule_sets: ['rs_1', 'rs_2'], mode: 'drop' } }], connections: [] };
    const diff = diffVersions(a, b, { names: { rs_1: 'Order line checks', rs_2: 'Price list checks' } });
    const row = diff.steps[0].rows.find((r) => r.key === 'rule_sets');
    expect(row).toMatchObject({ label: 'Shared rule sets', before: null, after: 'Order line checks, Price list checks' });
    // A set nobody can name (deleted, or the list could not be read) still shows its id.
    expect(settingText(['rs_9'], 'rule_sets', { rs_1: 'Order line checks' })).toBe('rs_9');
  });

  it('shows one row per entry of a small settings bag', () => {
    const a = { steps: [{ id: 's', type: 'filter', params: { _settings: { deactivated: false } } }], connections: [] };
    const b = { steps: [{ id: 's', type: 'filter', params: { _settings: { deactivated: true } } }], connections: [] };
    const row = diffVersions(a, b).steps[0].rows[0];
    expect(row).toMatchObject({ label: 'Step settings › deactivated', before: 'No', after: 'Yes', changed: true });
  });
});

describe('passwords typed into a step', () => {
  const step = (params: Record<string, unknown>) => ({ id: 's', type: 'db_source', label: 'Read orders', params, position: { x: 0, y: 0 } });
  const diff = (a: Record<string, unknown>, b: Record<string, unknown>, extra: Record<string, unknown> = {}) =>
    diffVersions({ steps: [step(a)], connections: [], ...extra }, { steps: [step(b)], connections: [], ...extra });

  it('say a password changed without showing it', () => {
    const d = diff(
      { host: 'h', password: 'old-pass-1', headers: { Authorization: 'Bearer old-token-1' } },
      { host: 'h', password: 'new-pass-2', headers: { Authorization: 'Bearer new-token-2' } },
    );
    const rows = d.steps[0].rows;
    expect(rows.find((r) => r.key === 'password')).toMatchObject({ before: 'Set', after: 'Set, a different value', changed: true });
    expect(rows.find((r) => r.key === 'headers.Authorization')).toMatchObject({ before: 'Set', after: 'Set, a different value', changed: true });
    const text = JSON.stringify(d);
    for (const secret of ['old-pass-1', 'new-pass-2', 'old-token-1', 'new-token-2']) expect(text).not.toContain(secret);
  });

  it('show an unchanged password as set, and one added or removed', () => {
    expect(diff({ api_key: 'k1' }, { api_key: 'k1' }).steps[0].kind).toBe('same');
    expect(diff({}, { client_secret: 's1' }).steps[0].rows[0]).toMatchObject({ before: '—', after: 'Set', changed: true });
    expect(diff({ apiKey: 's1' }, {}).steps[0].rows[0]).toMatchObject({ before: 'Set', after: '—', changed: true });
  });

  it('keep showing settings that only look like secrets, and references', () => {
    const rows = diff(
      { key: 'Id', max_tokens: 100, token_url: 'https://auth.example.test/token', password: '${secret.orders_db}' },
      { key: 'Email', max_tokens: 200, token_url: 'https://auth.example.test/v2/token', password: '${secret.orders_db_v2}' },
    ).steps[0].rows;
    const shown = Object.fromEntries(rows.map((r) => [r.key, r.after]));
    expect(shown).toEqual({
      key: 'Email', max_tokens: '200', token_url: 'https://auth.example.test/v2/token', password: '${secret.orders_db_v2}',
    });
  });

  it('hide a password parameter default', () => {
    const d = diffVersions(
      { steps: [], connections: [], parameters: [{ name: 'db_password', type: 'string', default: 'old-default-1' }] },
      { steps: [], connections: [], parameters: [{ name: 'db_password', type: 'string', default: 'new-default-2' }] },
    );
    expect(d.pipeline).toEqual([expect.objectContaining({ before: 'string, default set', after: 'string, default set, a different value' })]);
    expect(JSON.stringify(d)).not.toContain('default-');
  });
});
