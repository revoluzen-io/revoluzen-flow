/**
 * A connection's History tab: the kept versions newest first, a version set
 * against the settings now, and a restore that asks for an old password
 * (Flow does not keep it) and lists the pipelines that use the connection.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { api, type ConnectionSettings, type ConnectionVersionHistory, type ConnectionVersionItem } from '../api/client';
import ConnectionHistory from '../components/connections/ConnectionHistory';
import { toast } from '../components/Toast';

const at = '2026-11-03T10:00:00Z';
const secret = (tag: string) => ({ secret: true as const, tag });

function settings(over: Partial<ConnectionSettings> = {}, config: Record<string, any> = {}): ConnectionSettings {
  return {
    name: 'Orders DB',
    type: 'postgresql',
    config: { host: 'db1.example.test', database: 'orders', password: secret('aaaaaaaaaaaa'), ...config },
    credential_id: null,
    project_id: null,
    environment: 'all',
    description: '',
    tags: [],
    capabilities: ['read', 'write'],
    ...over,
  };
}

const now = settings({}, { host: 'db2.example.test', password: secret('bbbbbbbbbbbb') });

function item(version: number, over: Partial<ConnectionVersionItem>): ConnectionVersionItem {
  return {
    version, created_by: 'ops@example.test', created_at: at, note: 'Saved', kind: 'saved',
    changed: [], same_as_current: false, settings: settings(), ...over,
  };
}

const history: ConnectionVersionHistory = {
  connection_id: 'c1',
  current: now,
  keep: 50,
  used_by: [
    { workflow_id: 'wf1', name: 'Nightly orders', role: 'source', status: 'published', deployed_version: 4 },
    { workflow_id: 'wf2', name: 'Orders backfill', role: 'destination', status: 'draft', deployed_version: null },
  ],
  versions: [
    item(3, { note: 'Host and password changed', changed: ['host', 'password'], same_as_current: true, settings: now }),
    item(2, { note: 'Description changed', changed: ['description'], settings: settings({ description: 'Nightly' }) }),
    item(1, { note: 'Created', created_by: '' }),
  ],
};

const names = { type: (t: string) => (t === 'postgresql' ? 'PostgreSQL' : undefined) };

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, 'getConnectionVersions').mockResolvedValue(history);
});

function show(canRestore = true, onRestored = vi.fn()) {
  render(
    <ConnectionHistory connectionId="c1" connectionName="Orders DB" canRestore={canRestore} names={names} onRestored={onRestored} />,
  );
  return onRestored;
}

async function pick(version: number) {
  const list = await screen.findByRole('complementary', { name: 'Versions' });
  fireEvent.click(within(list).getByText(history.versions.find((v) => v.version === version)!.note));
}

describe('the History tab', () => {
  it('lists the versions newest first and marks the current one', async () => {
    show();
    const list = await screen.findByRole('complementary', { name: 'Versions' });
    const rows = within(list).getAllByRole('button');
    expect(rows.map((r) => r.getAttribute('data-version'))).toEqual(['3', '2', '1']);
    expect(within(rows[0]).getByText('Current')).toBeInTheDocument();
    expect(within(rows[2]).getByText(/Not recorded/)).toBeInTheDocument();
    expect(within(list).getByText(/newest 50 versions and the first one/)).toBeInTheDocument();
  });

  it('first sets the newest older version against the settings now, changed settings only', async () => {
    show();
    const compare = await screen.findByTestId('connection-compare');
    expect(within(compare).getByText('Host, password and description changed since version 2.')).toBeInTheDocument();
    const changed = within(compare).getAllByRole('row').filter((r) => r.getAttribute('data-changed') === 'yes');
    expect(changed).toHaveLength(3);
    expect(within(compare).getByText('db1.example.test')).toBeInTheDocument();
    expect(within(compare).getByText('Set, a different one')).toBeInTheDocument();
    expect(within(compare).queryByText('orders')).toBeNull(); // unchanged, hidden

    fireEvent.click(screen.getByLabelText('Show every setting'));
    expect(within(compare).getAllByText('orders')).toHaveLength(2);
    expect(within(compare).getAllByText('PostgreSQL')).toHaveLength(2);
  });

  it('never shows a secret or its tag', async () => {
    show();
    await screen.findByTestId('connection-compare');
    fireEvent.click(screen.getByLabelText('Show every setting'));
    expect(document.body.textContent).not.toContain('aaaaaaaaaaaa');
    expect(document.body.textContent).not.toContain('bbbbbbbbbbbb');
  });

  it('restores with the old password typed in, after listing what changes and who uses it', async () => {
    const restore = vi.spyOn(api, 'restoreConnectionVersion').mockResolvedValue({
      connection: { id: 'c1', name: 'Orders DB' }, restored_from: 1, version: 4, unchanged: false,
      kept_secrets: [], missing_secrets: [], kept_settings: [], renamed_to: null,
    });
    const success = vi.spyOn(toast, 'success');
    const onRestored = show();
    await pick(1);
    fireEvent.click(await screen.findByRole('button', { name: /Restore version 1/ }));

    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('Restore version 1?')).toBeInTheDocument();
    const changes = within(dialog).getByTestId('restore-changes');
    expect(changes.textContent).toContain('Host: db2.example.test → db1.example.test');
    const used = within(dialog).getByTestId('restore-used-by');
    expect(within(used).getByText('Nightly orders')).toBeInTheDocument();
    expect(within(used).getByText('Live in PROD')).toBeInTheDocument();
    expect(within(used).getByText('Writes to it')).toBeInTheDocument();
    expect(within(dialog).getByText(/2 pipelines use it/)).toBeInTheDocument();

    fireEvent.change(within(dialog).getByLabelText('Password'), { target: { value: 'typed-old-password' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restore version 1' }));

    await waitFor(() => expect(restore).toHaveBeenCalledWith('c1', 1, { password: 'typed-old-password' }));
    await waitFor(() => expect(onRestored).toHaveBeenCalledWith({ id: 'c1', name: 'Orders DB' }));
    expect(success).toHaveBeenCalledWith('Restored version 1', 'Saved as version 4.');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await waitFor(() => expect(api.getConnectionVersions).toHaveBeenCalledTimes(2)); // the history is read again
  });

  it("keeps today's password when none is typed and says so", async () => {
    const restore = vi.spyOn(api, 'restoreConnectionVersion').mockResolvedValue({
      connection: { id: 'c1' }, restored_from: 1, version: 4, unchanged: false,
      kept_secrets: ['password'], missing_secrets: [], kept_settings: [], renamed_to: null,
    });
    const success = vi.spyOn(toast, 'success');
    show();
    await pick(1);
    fireEvent.click(await screen.findByRole('button', { name: /Restore version 1/ }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Restore version 1' }));
    await waitFor(() => expect(restore).toHaveBeenCalledWith('c1', 1, {}));
    await waitFor(() => expect(success).toHaveBeenCalledWith('Restored version 1', "Saved as version 4. Kept today's password."));
  });

  it('offers no restore without edit rights', async () => {
    show(false);
    await screen.findByTestId('connection-compare');
    expect(screen.queryByRole('button', { name: /Restore version/ })).toBeNull();
  });

  it('explains an empty history', async () => {
    vi.spyOn(api, 'getConnectionVersions').mockResolvedValue({ ...history, versions: [] });
    show();
    expect(await screen.findByText('No versions yet')).toBeInTheDocument();
    expect(screen.getByText(/Passwords and keys are never kept/)).toBeInTheDocument();
  });
});
