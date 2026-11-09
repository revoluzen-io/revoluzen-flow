import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ClarifyCard, { type ClarificationPayload } from '../components/agent/ClarifyCard';
import { api } from '../api/client';
import { classifyIntent } from '../ai/pipelineBuilder';

vi.mock('../api/client', () => ({
  api: {
    post: vi.fn(),
    createConnection: vi.fn(),
    testConnection: vi.fn(),
    listCredentials: vi.fn(),
    getConnectionCatalog: vi.fn(),
  },
}));

// The form reads its connector tables from the Connections page. A small stand-in
// keeps these tests about the card; the real exports are checked by the type check.
vi.mock('../components/pages/ConnectionsPage', () => {
  const CONNECTION_FIELDS: Record<string, any[]> = {
    sqlite: [
      { key: 'file_path', label: 'Database File Path', type: 'text', placeholder: '/data/mydb.sqlite', required: true },
    ],
    snowflake: [
      { key: 'account', label: 'Account Identifier', type: 'text', placeholder: '', required: true },
      { key: 'username', label: 'Username', type: 'text', placeholder: '', required: true },
      { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    ],
    rest_api: [],
  };
  return {
    CONNECTION_TYPES: [
      { type: 'sqlite', label: 'SQLite', category: 'Databases' },
      { type: 'snowflake', label: 'Snowflake', category: 'Data Warehouses' },
      { type: 'slack', label: 'Slack Webhook', category: 'Notifications' },
    ],
    CONNECTION_FIELDS,
    WRITE_ONLY_TYPES: new Set(['slack']),
    connectorStatus: () => 'certified',
    configWithFieldDefaults: (_type: string, config: Record<string, string>) => ({ ...config }),
    buildConnectionConfig: (_type: string, config: Record<string, string>, credentialId: string, map: Record<string, string>) => {
      const next: Record<string, unknown> = { ...config };
      if (credentialId && Object.keys(map).length > 0) {
        next.credential_field_map = map;
        Object.keys(map).forEach((key) => { delete next[key]; });
      }
      return next;
    },
    credentialAutoMap: (fields: Array<{ key: string }>, keys: string[]) =>
      Object.fromEntries(fields.filter((f) => keys.includes(f.key)).map((f) => [f.key, f.key])),
  };
});

const mockedApi = api as unknown as Record<'post' | 'createConnection' | 'testConnection' | 'listCredentials' | 'getConnectionCatalog', ReturnType<typeof vi.fn>>;

const SECRET = 'synthetic-password-not-real-42';

const stockroom = {
  id: 'connection:c_stock', label: 'Stockroom database (SQLite)', kind: 'connection' as const,
  connection_id: 'c_stock', connection_name: 'Stockroom database', connection_type: 'sqlite',
};
const priceApi = {
  id: 'connection:c_price', label: 'Partner price API (REST API)', kind: 'connection' as const,
  connection_id: 'c_price', connection_name: 'Partner price API', connection_type: 'rest_api',
};
const tickets = {
  id: 'file:obj_tickets', label: 'tickets.csv (uploaded file)', kind: 'file' as const,
  file_id: 'obj_tickets', file_name: 'tickets.csv',
};
const newConnection = { id: 'new_connection', label: 'New connection', kind: 'new_connection' as const };

function question(field: string, text: string, options: any[], extra: Record<string, unknown> = {}) {
  return {
    field, question: text, required: true, slot: field, label: field,
    chips: options.map((o) => o.label), options, ...extra,
  };
}

const FILM: ClarificationPayload = {
  source_type: null,
  sink_type: null,
  detected_intent: 'Build a new pipeline',
  question_count: 2,
  table_connection_types: ['sqlite', 'postgresql', 'snowflake'],
  write_mode_connection_types: ['postgresql', 'mysql', 'mssql', 'sqlite'],
  questions: [
    question('source', 'Where should I read the data from?', [stockroom, priceApi, tickets, newConnection]),
    question('destination', 'Where should I write the result?', [
      {
        id: 'new_table', label: 'New managed table in this workspace', kind: 'new_table', recommended: true,
        schema_name: 'default', table_name: 'returns_by_reason',
      },
      stockroom, priceApi, newConnection,
    ], { table_hint: 'returns_by_reason' }),
  ] as any,
};

const NAMED_SNOWFLAKE: ClarificationPayload = {
  source_type: 'postgres',
  sink_type: 'snowflake',
  detected_intent: 'Build a pipeline from PostgreSQL to Snowflake',
  question_count: 1,
  table_connection_types: ['snowflake'],
  write_mode_connection_types: [],
  questions: [
    question('destination', 'This workspace has no Snowflake connection yet. Create one here so I can write to it.', [
      {
        id: 'new_connection', label: 'New Snowflake connection', kind: 'new_connection',
        new_connection_type: 'snowflake', new_connection_label: 'Snowflake',
      },
    ], { table_hint: 'customers' }),
  ] as any,
};

function send() {
  return screen.getByRole('button', { name: 'Send answers' });
}

function chip(name: RegExp | string) {
  return screen.getByRole('button', { name });
}

function inQuestion(text: string) {
  return within(screen.getByText(new RegExp(text)).parentElement as HTMLElement);
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedApi.listCredentials.mockResolvedValue([]);
  mockedApi.getConnectionCatalog.mockResolvedValue({
    supported: true, reason: '', parents: ['main'], kinds: ['table'],
    items: [{ name: 'returns', kind: 'table', parent: 'main', metadata: {} }],
  });
  mockedApi.testConnection.mockResolvedValue({ success: true });
});

afterEach(cleanup);

describe('ClarifyCard', () => {
  it('shows a plain heading and the real options', () => {
    render(<ClarifyCard payload={NAMED_SNOWFLAKE} onSubmit={vi.fn()} />);
    expect(screen.getByRole('heading')).toHaveTextContent('Build a pipeline from PostgreSQL to Snowflake');
    expect(document.body.textContent).not.toContain('*');
    render(<ClarifyCard payload={FILM} onSubmit={vi.fn()} />);
    for (const name of ['tickets.csv (uploaded file)', 'New connection']) {
      expect(screen.getAllByRole('button', { name })[0]).toBeVisible();
    }
    expect(chip(/New managed table in this workspace/)).toHaveTextContent('Recommended');
  });

  it('holds Send until every required question is answered', () => {
    render(<ClarifyCard payload={FILM} onSubmit={vi.fn()} />);
    expect(send()).toBeDisabled();
    fireEvent.click(chip(/tickets\.csv/));
    expect(send()).toBeDisabled();
    fireEvent.click(chip(/New managed table/));
    expect(send()).toBeEnabled();
  });

  it('sends ids and names for a file and a new managed table', () => {
    const onSubmit = vi.fn();
    render(<ClarifyCard payload={FILM} onSubmit={onSubmit} />);
    fireEvent.click(chip(/tickets\.csv/));
    fireEvent.click(chip(/New managed table/));
    expect(screen.getByLabelText('What should the new table be called?')).toHaveValue('returns_by_reason');
    fireEvent.click(send());
    const [text, choices] = onSubmit.mock.calls[0];
    expect(text).toBe('Read from tickets.csv.\nWrite to a new managed table called returns_by_reason.');
    expect(choices).toEqual({
      source: { kind: 'file', file_id: 'obj_tickets', file_name: 'tickets.csv' },
      destination: { kind: 'new_table', table_name: 'returns_by_reason' },
    });
  });

  it('asks which table after a database source, from the real list', async () => {
    const onSubmit = vi.fn();
    render(<ClarifyCard payload={FILM} onSubmit={onSubmit} />);
    fireEvent.click(screen.getAllByRole('button', { name: /Stockroom database/ })[0]);
    expect(screen.getByText('Which table should I read?')).toBeVisible();
    fireEvent.click(chip(/New managed table/));
    expect(send()).toBeDisabled();

    const tableChip = await screen.findByRole('button', { name: 'returns' });
    expect(mockedApi.getConnectionCatalog).toHaveBeenCalledWith('c_stock');
    fireEvent.click(tableChip);
    expect(send()).toBeEnabled();
    fireEvent.click(send());
    const [text, choices] = onSubmit.mock.calls[0];
    expect(text).toContain('Read from Stockroom database, table returns.');
    expect(choices.source).toEqual({ kind: 'connection', connection_id: 'c_stock', table: 'returns', schema: 'main' });
  });

  it('takes a typed table when the list is unavailable', async () => {
    mockedApi.getConnectionCatalog.mockResolvedValue({ supported: false, reason: 'x', items: [], parents: [], kinds: [] });
    const onSubmit = vi.fn();
    render(<ClarifyCard payload={FILM} onSubmit={onSubmit} />);
    fireEvent.click(screen.getAllByRole('button', { name: /Stockroom database/ })[0]);
    expect(await screen.findByText(/I could not list the tables in Stockroom database/)).toBeVisible();
    fireEvent.change(screen.getByLabelText('Table in Stockroom database'), { target: { value: 'public.returns' } });
    fireEvent.click(chip(/New managed table/));
    fireEvent.click(send());
    expect(onSubmit.mock.calls[0][1].source).toMatchObject({ connection_id: 'c_stock', table: 'public.returns' });
  });

  it('asks for a table and how to write when the destination is a database', () => {
    const onSubmit = vi.fn();
    render(<ClarifyCard payload={FILM} onSubmit={onSubmit} />);
    fireEvent.click(chip(/tickets\.csv/));
    fireEvent.click(screen.getAllByRole('button', { name: /Stockroom database/ })[1]);
    expect(screen.getByLabelText('Which table should I write to?')).toHaveValue('returns_by_reason');
    expect(send()).toBeDisabled();
    fireEvent.click(chip('Update rows that match'));
    expect(send()).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Which column identifies a row?'), { target: { value: 'reason' } });
    fireEvent.click(send());
    const [text, choices] = onSubmit.mock.calls[0];
    expect(choices.destination).toMatchObject({ kind: 'connection', connection_id: 'c_stock', table: 'returns_by_reason' });
    expect(choices.write_mode).toBe('merge');
    expect(choices.merge_on).toEqual(['reason']);
    expect(text).toContain('Update rows that match on reason.');
  });

  it('asks no follow-up for a connection that has no tables', () => {
    const onSubmit = vi.fn();
    render(<ClarifyCard payload={FILM} onSubmit={onSubmit} />);
    fireEvent.click(screen.getAllByRole('button', { name: /Partner price API/ })[0]);
    expect(screen.queryByText('Which table should I read?')).toBeNull();
    fireEvent.click(chip(/New managed table/));
    expect(send()).toBeEnabled();
  });

  it('accepts a typed answer in place of a chip', () => {
    const onSubmit = vi.fn();
    render(<ClarifyCard payload={FILM} onSubmit={onSubmit} />);
    fireEvent.change(screen.getByLabelText(/Your own answer to: Where should I read/), { target: { value: 'the finance export' } });
    fireEvent.click(chip(/New managed table/));
    fireEvent.click(send());
    expect(onSubmit.mock.calls[0][1].source).toEqual({ kind: 'text', text: 'the finance export' });
  });

  it('keeps plain chip questions working', () => {
    const onSubmit = vi.fn();
    const payload: ClarificationPayload = {
      source_type: 'csv_file', sink_type: 'sql_server', detected_intent: 'Build a pipeline', question_count: 1,
      questions: [{ field: 'schedule_interval', question: 'How often?', chips: ['Every hour', 'Weekly'], required: true, label: 'Schedule' }],
    };
    render(<ClarifyCard payload={payload} onSubmit={onSubmit} />);
    fireEvent.click(chip('Every hour'));
    fireEvent.click(send());
    expect(onSubmit).toHaveBeenCalledWith('Schedule: Every hour', { answers: { schedule_interval: 'Every hour' } });
  });

  it('reads a slot question that only needs a table', async () => {
    const onSubmit = vi.fn();
    const payload: ClarificationPayload = {
      source_type: 'sqlite', sink_type: null, detected_intent: 'Build a pipeline from Stockroom database', question_count: 1,
      questions: [{
        field: 'source_table', slot: 'source_table', question: 'Which table in Stockroom database should I read?',
        chips: [], required: true, connection_id: 'c_stock', connection_name: 'Stockroom database',
      }],
    };
    render(<ClarifyCard payload={payload} onSubmit={onSubmit} />);
    fireEvent.click(await screen.findByRole('button', { name: 'returns' }));
    fireEvent.click(send());
    expect(onSubmit.mock.calls[0][1].source_table).toEqual({ table: 'returns', schema: 'main' });
  });
});

describe('New connection in the chat', () => {
  it('filters connector types by search and leaves out write-only ones for a source', async () => {
    render(<ClarifyCard payload={FILM} onSubmit={vi.fn()} />);
    fireEvent.click(inQuestion('Where should I read').getByRole('button', { name: 'New connection' }));
    const search = await screen.findByLabelText('Connector type');
    expect(screen.queryByRole('button', { name: 'Slack Webhook' })).toBeNull();
    fireEvent.change(search, { target: { value: 'sql' } });
    expect(screen.getByRole('button', { name: 'SQLite' })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Snowflake' })).toBeNull();
  });

  it('films the whole path: SQLite, test, save, then the table', async () => {
    mockedApi.post.mockResolvedValue({ success: true, latency_ms: 12 });
    mockedApi.createConnection.mockResolvedValue({
      id: 'c_returns', name: 'Returns database', type: 'sqlite', config: { file_path: 'warehouse.db' },
    });
    const onSubmit = vi.fn();
    render(<ClarifyCard payload={FILM} onSubmit={onSubmit} />);

    fireEvent.click(inQuestion('Where should I read').getByRole('button', { name: 'New connection' }));
    fireEvent.change(await screen.findByLabelText('Connector type'), { target: { value: 'sqlite' } });
    fireEvent.click(screen.getByRole('button', { name: 'SQLite' }));
    expect(screen.getByText('New SQLite connection')).toBeVisible();

    const save = screen.getByRole('button', { name: 'Save' });
    const test = screen.getByRole('button', { name: 'Test' });
    expect(save).toBeDisabled();
    expect(test).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/Connection name/), { target: { value: 'Returns database' } });
    fireEvent.change(screen.getByLabelText(/Database File Path/), { target: { value: 'warehouse.db' } });

    fireEvent.click(test);
    expect(await screen.findByText('Connected')).toBeVisible();
    expect(mockedApi.post).toHaveBeenCalledWith('/api/connections/test-inline', {
      type: 'sqlite', config: { file_path: 'warehouse.db' }, credential_id: undefined,
    });

    fireEvent.click(save);
    await waitFor(() => expect(mockedApi.createConnection).toHaveBeenCalledTimes(1));
    expect(mockedApi.createConnection.mock.calls[0][0]).toMatchObject({
      name: 'Returns database', type: 'sqlite', config: { file_path: 'warehouse.db' }, environment: 'dev',
    });

    // The new connection is now the selected answer, and its tables are offered.
    const newChip = await screen.findByRole('button', { name: 'Returns database' });
    expect(newChip).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByTestId('inline-connection-form')).toBeNull();
    expect(mockedApi.testConnection).toHaveBeenCalledWith('c_returns');
    fireEvent.click(await screen.findByRole('button', { name: 'returns' }));

    fireEvent.click(chip(/New managed table/));
    fireEvent.click(send());
    const [text, choices] = onSubmit.mock.calls[0];
    expect(text).toBe('Read from Returns database, table returns.\nWrite to a new managed table called returns_by_reason.');
    expect(choices.source).toEqual({ kind: 'connection', connection_id: 'c_returns', table: 'returns', schema: 'main' });
  });

  it('opens on the named technology with its password masked', async () => {
    render(<ClarifyCard payload={NAMED_SNOWFLAKE} onSubmit={vi.fn()} />);
    fireEvent.click(chip('New Snowflake connection'));
    expect(await screen.findByText('New Snowflake connection')).toBeVisible();
    expect(screen.queryByPlaceholderText('Search connectors…')).toBeNull();
    expect(screen.getByLabelText(/Password/)).toHaveAttribute('type', 'password');
  });

  it('never lets a secret reach the transcript or the choices', async () => {
    mockedApi.post.mockResolvedValue({ success: false, error: 'Could not reach the account' });
    mockedApi.createConnection.mockResolvedValue({
      id: 'c_sf', name: 'Analytics Snowflake', type: 'snowflake',
      config: { account: 'a', username: 'u', password: SECRET },
    });
    const onSubmit = vi.fn();
    render(<ClarifyCard payload={NAMED_SNOWFLAKE} onSubmit={onSubmit} />);
    fireEvent.click(chip('New Snowflake connection'));
    fireEvent.change(await screen.findByLabelText(/Connection name/), { target: { value: 'Analytics Snowflake' } });
    fireEvent.change(screen.getByLabelText(/Account Identifier/), { target: { value: 'a' } });
    fireEvent.change(screen.getByLabelText(/Username/), { target: { value: 'u' } });
    fireEvent.change(screen.getByLabelText(/Password/), { target: { value: SECRET } });

    // Saving doesn't need a passing test: a laptop can't always reach production.
    fireEvent.click(screen.getByRole('button', { name: 'Test' }));
    expect(await screen.findByText('Could not connect')).toBeVisible();
    expect(screen.getByText('Could not reach the account')).toBeVisible();
    expect(mockedApi.post.mock.calls[0][1].config.password).toBe(SECRET);

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByRole('button', { name: 'Analytics Snowflake' });
    expect(mockedApi.testConnection).not.toHaveBeenCalled();
    expect(document.body.innerHTML).not.toContain(SECRET);

    fireEvent.click(send());
    const sent = JSON.stringify(onSubmit.mock.calls[0]);
    expect(sent).toContain('c_sf');
    expect(sent).toContain('customers');
    expect(sent).not.toContain(SECRET);
    expect(sent).not.toContain('password');
  });

  it('fills the fields a saved credential holds, as the Connections page does', async () => {
    mockedApi.listCredentials.mockResolvedValue([
      { id: 'cred1', name: 'Warehouse login', config: { username: 'u***', password: '***' } },
    ]);
    mockedApi.createConnection.mockResolvedValue({ id: 'c_sf', name: 'Analytics Snowflake', type: 'snowflake' });
    render(<ClarifyCard payload={NAMED_SNOWFLAKE} onSubmit={vi.fn()} />);
    fireEvent.click(chip('New Snowflake connection'));
    const picker = await screen.findByLabelText('Saved credential');
    await screen.findByRole('option', { name: 'Warehouse login' });
    fireEvent.change(picker, { target: { value: 'cred1' } });

    expect(screen.getByLabelText(/Password/)).toHaveValue('From Warehouse login · password');
    expect(screen.getByLabelText(/Password/)).toHaveAttribute('readonly');
    expect(screen.getByLabelText(/Username/)).toHaveValue('From Warehouse login · username');

    fireEvent.change(screen.getByLabelText(/Connection name/), { target: { value: 'Analytics Snowflake' } });
    fireEvent.change(screen.getByLabelText(/Account Identifier/), { target: { value: 'a' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(mockedApi.createConnection).toHaveBeenCalledTimes(1));
    const sent = mockedApi.createConnection.mock.calls[0][0];
    expect(sent.credential_id).toBe('cred1');
    expect(sent.config).toEqual({
      account: 'a',
      credential_field_map: { username: 'username', password: 'password' },
    });
  });

  it('takes back what a credential filled when it is unpicked', async () => {
    mockedApi.listCredentials.mockResolvedValue([
      { id: 'cred1', name: 'Warehouse login', config: { username: 'u***' } },
    ]);
    render(<ClarifyCard payload={NAMED_SNOWFLAKE} onSubmit={vi.fn()} />);
    fireEvent.click(chip('New Snowflake connection'));
    const picker = await screen.findByLabelText('Saved credential');
    await screen.findByRole('option', { name: 'Warehouse login' });
    fireEvent.change(picker, { target: { value: 'cred1' } });
    expect(screen.getByLabelText(/Username/)).toHaveValue('From Warehouse login · username');
    fireEvent.change(picker, { target: { value: '' } });
    expect(screen.getByLabelText(/Username/)).toHaveValue('');
    expect(screen.getByLabelText(/Username/)).not.toHaveAttribute('readonly');
  });

  it('returns to the chips on Cancel, keeping what was picked', async () => {
    render(<ClarifyCard payload={FILM} onSubmit={vi.fn()} />);
    const source = inQuestion('Where should I read');
    fireEvent.click(source.getByRole('button', { name: /tickets\.csv/ }));
    fireEvent.click(source.getByRole('button', { name: 'New connection' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
    expect(screen.queryByTestId('inline-connection-form')).toBeNull();
    expect(chip(/tickets\.csv/)).toHaveAttribute('aria-pressed', 'true');
    expect(mockedApi.createConnection).not.toHaveBeenCalled();
  });

  it('shows why a save failed and stays open', async () => {
    mockedApi.createConnection.mockRejectedValue(new Error('Unknown connection type'));
    render(<ClarifyCard payload={NAMED_SNOWFLAKE} onSubmit={vi.fn()} />);
    fireEvent.click(chip('New Snowflake connection'));
    fireEvent.change(await screen.findByLabelText(/Connection name/), { target: { value: 'X' } });
    fireEvent.change(screen.getByLabelText(/Account Identifier/), { target: { value: 'a' } });
    fireEvent.change(screen.getByLabelText(/Username/), { target: { value: 'u' } });
    fireEvent.change(screen.getByLabelText(/Password/), { target: { value: SECRET } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Unknown connection type');
    expect(screen.getByTestId('inline-connection-form')).toBeVisible();
    expect(send()).toBeDisabled();
  });
});

describe('classifyIntent', () => {
  it.each([
    'Copy customers from Postgres to Snowflake every night',
    'Sync Salesforce accounts to BigQuery',
    'Move customers from Postgres to Snowflake',
    'Join orders.csv with customers.csv and save to a managed table',
    'Build a pipeline that shows returns by reason',
  ])('treats %s as a build request', (text) => {
    expect(classifyIntent(text)).toBe('create');
  });

  it.each(['Move the filter step after the join', 'Copy this', 'Why did it fail'])('leaves %s alone', (text) => {
    expect(classifyIntent(text)).not.toBe('create');
  });
});
