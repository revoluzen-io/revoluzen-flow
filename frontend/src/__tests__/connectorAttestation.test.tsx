/**
 * Connector attestation is reported per connector, never summed.
 *
 * The rule these tests defend: a connector with no recorded fixtures has not
 * failed anything — nobody has run its acceptance suite yet. Those two states
 * are different claims, and a UI that renders them alike (a "0 / 27 Verified"
 * counter, a red "Not verified" badge) tells the reader the connectors were
 * tested and found wanting. They were not tested at all.
 *
 * So the aggregate is gone from the API (`cert_matrix()` returns no `by_tier`
 * or `production_total`) and gone from this page. What remains is per
 * connector: an attestation date where there is evidence, and an em-dash where
 * there is none.
 *
 * These assertions exist because the instinct to add the counter back is
 * strong — it reads as a KPI, and a KPI is easier to ship than fixtures.
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import TrustPage from '../components/pages/TrustPage';
import { api } from '../api/client';

vi.mock('../hooks/usePageContext', () => ({ usePageContext: vi.fn() }));

const posture = {
  as_of: '2026-10-20T12:00:00Z',
  sovereignty: {
    telemetry_currently_enabled: null,
    active_provider_summary: {
      provider: 'local', model: 'test-model', is_local: true, status: 'configured',
    },
  },
  security_baseline: [],
};

// One attested connector, two that have never been given fixtures — the real
// shape of the matrix, not a hypothetical.
const MATRIX = {
  total: 3,
  by_label: { beta: 2, 'v1-functional': 1 },
  by_category: { dev_tools: 1, crm: 2 },
  v2_total: 3,
  hidden_total: 0,
  rows: [
    {
      id: 'github.v2',
      display_name: 'GitHub',
      category: 'dev_tools',
      depth_score: 3,
      depth_label: 'beta',
      validation_status: 'pass',
      streams_count: 3,
      tier: 'verified',
      cat_green: true,
      attested_at: '2026-10-20T03:39:51.381372+00:00',
      attested_checks: 4,
    },
    {
      id: 'salesforce.v2',
      display_name: 'Salesforce',
      category: 'crm',
      depth_score: 3,
      depth_label: 'beta',
      validation_status: 'pass',
      streams_count: 5,
      tier: 'beta',
      cat_green: false,
    },
    {
      id: 'hubspot.v2',
      display_name: 'HubSpot',
      category: 'crm',
      depth_score: 2,
      depth_label: 'v1-functional',
      validation_status: 'pass',
      streams_count: 4,
      tier: 'beta',
      cat_green: false,
    },
  ],
};

beforeEach(() => {
  vi.spyOn(api, 'getTrustPosture').mockResolvedValue(posture as any);
  vi.spyOn(api, 'getTrustEvalSummary').mockResolvedValue({ ran: false } as any);
  vi.spyOn(api, 'getCertMatrix').mockResolvedValue(MATRIX as any);
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); window.location.hash = ''; });

/** Expand the collapsed per-connector matrix; it is the only place tiers show. */
async function openMatrix() {
  render(<TrustPage embedded />);
  const toggle = await screen.findByRole('button', { name: /Show full matrix/i });
  fireEvent.click(toggle);   // fireEvent wraps the state update in act()
  await waitFor(() => expect(screen.getByText('GitHub')).toBeInTheDocument());
}

it('shows the attestation date for a connector that has one', async () => {
  await openMatrix();

  // The date and the check count — an attestation without an "as of" is not
  // an attestation, so both must be on the row.
  expect(screen.getByText(/20 Oct 2026/)).toBeInTheDocument();
  expect(screen.getByText(/4\/4/)).toBeInTheDocument();
});

it('shows no attestation claim for connectors that have no fixtures', async () => {
  await openMatrix();

  // Salesforce and HubSpot have never been attested. They must read as
  // "nothing claimed", which is the em-dash — never as a zero, and never as
  // a badge that implies a test was run and failed.
  const dashes = screen.getAllByTitle(/No acceptance-test fixtures recorded/i);
  expect(dashes).toHaveLength(2);
  for (const cell of dashes) {
    expect(cell.textContent).toBe('—');
  }
  expect(screen.queryByText(/Not verified/i)).toBeNull();
  expect(screen.queryByText(/0\/0/)).toBeNull();
});

it('publishes no attested-vs-total ratio anywhere on the page', async () => {
  await openMatrix();

  // "1 of 3", "1/3", "33%" — any of these turns honest per-connector
  // evidence back into the number that invites the wrong reading.
  const body = document.body.textContent || '';
  expect(body).not.toMatch(/\b1\s*(of|\/)\s*(3|27)\b/);
  expect(body).not.toMatch(/\b\d+\s*%\s*(verified|attested)/i);

  // And no counter card labelled with an attestation tier. The inventory
  // cards above the table count declared capability labels, which is a
  // different claim and stays — but nothing counts attestations.
  expect(screen.queryByText(/^Verified$/)).toBeNull();
  expect(screen.queryByText(/Declared production/)).toBeInTheDocument();
});
