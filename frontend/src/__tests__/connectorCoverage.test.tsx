/**
 * The connector strip on Help → Node Reference must say what it counts.
 *
 * It used to show the size of the whole registry ("102 connector types across
 * 10 categories") while About, the Dashboard and the Connections gallery all
 * said 66. 102 includes the 14 types marked Coming soon and 22 that the New
 * Connection page never lists. The strip now counts what a user can set up and
 * tallies the certification mix over those, so the chips add up to the number
 * in the headline.
 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ConnectorCoverage from '../components/help/ConnectorCoverage';

// A stand-in for the Connections page's tables: three types on offer and one
// marked Coming soon. The real tables are checked in connectorPickerCount.test.ts.
vi.mock('../components/pages/ConnectionsPage', () => ({
  CONNECTION_TYPES: [
    { type: 'postgresql', label: 'PostgreSQL', category: 'Databases' },
    { type: 'snowflake', label: 'Snowflake', category: 'Warehouses' },
    { type: 'rest_api', label: 'REST API', category: 'APIs' },
    { type: 'pulsar', label: 'Pulsar', category: 'Streaming' },
  ],
  connectorStatus: (type: string) => (type === 'pulsar' ? 'roadmap' : 'certified'),
}));

const registry = {
  count: 6,
  categories: ['relational', 'warehouse', 'api', 'streaming', 'other'],
  maturity_summary: { production: 2, certified: 1, configurable: 1, form_only: 1, declared_only: 1 },
  connectors: [
    { name: 'postgresql', maturity: 'production' },
    { name: 'snowflake', maturity: 'certified' },
    { name: 'rest_api', maturity: 'form_only' },
    { name: 'pulsar', maturity: 'declared_only' }, // marked Coming soon: not offered
    { name: 'airbyte', maturity: 'configurable' }, // registered, never listed on the page
    { name: 'dbt', maturity: 'production' }, // registered, never listed on the page
  ],
};

const answerWith = (body: unknown) =>
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => body }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('Help → connector coverage strip', () => {
  it('counts the connectors the New Connection page offers, not the whole registry', async () => {
    answerWith(registry);
    render(<ConnectorCoverage />);

    expect(await screen.findByText('3 connectors you can set up')).toBeInTheDocument();
    expect(screen.getByText(/catalog registers 6 types in all; the other 3 are not offered there\./)).toBeInTheDocument();
    expect(screen.queryByText(/6 connector/)).toBeNull();
  });

  it('tallies the certification mix over the offered connectors only', async () => {
    answerWith(registry);
    render(<ConnectorCoverage />);
    await screen.findByText('3 connectors you can set up');

    expect(screen.getByText('1 Production')).toBeInTheDocument();
    expect(screen.getByText('1 Certified')).toBeInTheDocument();
    expect(screen.getByText('1 Form-only')).toBeInTheDocument();
    // The Coming soon type, the other Production type and the Configurable one are not counted.
    expect(screen.getByText('0 Configurable')).toBeInTheDocument();
    expect(screen.getByText('0 Declared')).toBeInTheDocument();
  });

  it('falls back to the registry totals, labelled as registered types, when it has no entries to filter', async () => {
    answerWith({ count: 102, categories: ['x'], maturity_summary: { production: 4, form_only: 71 } });
    render(<ConnectorCoverage />);

    expect(await screen.findByText('102 connector types registered')).toBeInTheDocument();
    expect(screen.queryByText(/connectors you can set up/)).toBeNull();
    expect(screen.getByText('71 Form-only')).toBeInTheDocument();
  });

  it('renders nothing when the catalog cannot be fetched', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500, json: async () => ({}) }));
    const { container } = render(<ConnectorCoverage />);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(container).toBeEmptyDOMElement();
  });
});
