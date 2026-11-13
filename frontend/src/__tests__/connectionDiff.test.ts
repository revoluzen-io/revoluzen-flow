/**
 * A connection version set against the settings now. Secrets arrive as tags
 * only, so a secret row can say "the same as now" or "a different one" and
 * never shows a value.
 */
import { describe, expect, it } from 'vitest';

import type { ConnectionSettings } from '../api/client';
import { changedSummary, diffConnection, settingLabel } from '../components/connections/connectionDiff';

const secret = (tag: string) => ({ secret: true as const, tag });

function settings(over: Partial<ConnectionSettings> = {}): ConnectionSettings {
  return {
    name: 'Orders DB',
    type: 'postgresql',
    config: { host: 'db1.example.test', port: '5432', database: 'orders', password: secret('aaaaaaaaaaaa') },
    credential_id: null,
    project_id: null,
    environment: 'all',
    description: '',
    tags: [],
    capabilities: ['read', 'write'],
    ...over,
  };
}

describe('diffConnection', () => {
  it('finds nothing to show between two equal settings', () => {
    const diff = diffConnection(settings(), settings());
    expect(diff.identical).toBe(true);
    expect(diff.changed).toEqual([]);
    expect(diff.rows.map((r) => r.key)).toEqual([
      'name', 'type', 'config.host', 'config.port', 'config.database', 'config.password',
      'credential_id', 'project_id', 'environment', 'capabilities', 'description', 'tags',
    ]);
  });

  it('shows a changed setting with the version value and the value now', () => {
    const diff = diffConnection(settings(), settings({ config: { ...settings().config, host: 'db2.example.test' } }));
    expect(diff.changed).toEqual([
      expect.objectContaining({ key: 'config.host', label: 'Host', version: 'db1.example.test', now: 'db2.example.test', changed: true }),
    ]);
  });

  it('tells the same secret from a different one by its tag', () => {
    const same = diffConnection(settings(), settings()).rows.find((r) => r.key === 'config.password')!;
    expect(same).toMatchObject({ changed: false, secretState: 'same', secretPath: 'password', version: 'Set, the same as now', now: 'Set' });
    const other = diffConnection(settings(), settings({ config: { ...settings().config, password: secret('bbbbbbbbbbbb') } }))
      .rows.find((r) => r.key === 'config.password')!;
    expect(other).toMatchObject({ changed: true, secretState: 'changed', version: 'Set, a different one', now: 'Set' });
  });

  it('says when a secret is only in the version or only there now', () => {
    const { password: _, ...without } = settings().config;
    const onlyVersion = diffConnection(settings(), settings({ config: without })).rows.find((r) => r.key === 'config.password')!;
    expect(onlyVersion).toMatchObject({ secretState: 'only-version', now: null, changed: true });
    const onlyNow = diffConnection(settings({ config: without }), settings()).rows.find((r) => r.key === 'config.password')!;
    expect(onlyNow).toMatchObject({ secretState: 'only-now', version: null, changed: true });
  });

  it('walks nested settings, secrets included', () => {
    const a = settings({ config: { host: 'h', auth: { user: 'svc', password: secret('aaaaaaaaaaaa') } } });
    const b = settings({ config: { host: 'h', auth: { user: 'svc2', password: secret('aaaaaaaaaaaa') } } });
    const diff = diffConnection(a, b);
    expect(diff.changed.map((r) => r.label)).toEqual(['Auth › user']);
    expect(diff.rows.find((r) => r.key === 'config.auth.password')).toMatchObject({ secretPath: 'auth.password', secretState: 'same' });
  });

  it('names credentials, projects, environments and roles', () => {
    const names = {
      credential: (id: string) => (id === 'cred1' ? 'Warehouse login' : undefined),
      project: (id: string) => (id === 'p1' ? 'Finance' : undefined),
      type: (t: string) => (t === 'postgresql' ? 'PostgreSQL' : undefined),
    };
    const diff = diffConnection(
      settings({ credential_id: 'cred1', project_id: 'p1', environment: 'prod', capabilities: ['read'] }),
      settings({ credential_id: 'gone', project_id: null, environment: 'all', capabilities: [] }),
      names,
    );
    const row = (key: string) => diff.rows.find((r) => r.key === key)!;
    expect(row('type')).toMatchObject({ version: 'PostgreSQL', now: 'PostgreSQL' });
    expect(row('credential_id')).toMatchObject({ version: 'Warehouse login', now: 'gone (not found)' });
    expect(row('project_id')).toMatchObject({ version: 'Finance', now: 'Global' });
    expect(row('environment')).toMatchObject({ version: 'PROD', now: 'DEV and PROD' });
    expect(row('capabilities')).toMatchObject({ version: 'Read', now: 'Read · Write' });
  });
});

describe('labels', () => {
  it('reads settings and lists of changes as people say them', () => {
    expect(settingLabel('client_secret')).toBe('Client secret');
    expect(settingLabel('auth.api_key')).toBe('Auth › api key');
    expect(changedSummary(['Host'])).toBe('Host');
    expect(changedSummary(['Host', 'Password'])).toBe('Host and password');
    expect(changedSummary(['Host', 'Port', 'Password'])).toBe('Host, port and password');
    expect(changedSummary(['Host', 'Port', 'Password', 'Tags'])).toBe('Host, port and 2 more');
  });
});
