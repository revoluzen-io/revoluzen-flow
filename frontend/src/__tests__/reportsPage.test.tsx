import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import ReportsPage from '../components/pages/ReportsPage';
import { reportsApi } from '../components/reports/reportsApi';
import type { Catalog, SavedReport } from '../components/reports/reportModel';

vi.mock('../components/reports/reportsApi', () => ({
  reportsApi: {
    catalog: vi.fn(), list: vi.fn(), get: vi.fn(), run: vi.fn(), preview: vi.fn(),
    setSchedule: vi.fn(), send: vi.fn(), remove: vi.fn(), duplicate: vi.fn(),
  },
  downloadReport: vi.fn(),
}));

const catalog: Catalog = {
  sections: [{ type: 'run_summary', group: 'Runs', title: 'Run summary', description: 'Counts', source: 'Run history' }],
  datasets: {},
  templates: [{ id: 'weekly', name: 'Weekly operations digest', description: 'The week', time_range: 'last_7_days', frequency: 'weekly', sections: [] }],
  environments: [{ value: 'dev', label: 'Development' }],
  channels: { email: true, slack_settings: false, teams_settings: false },
};

function report(overrides: Partial<SavedReport> = {}): SavedReport {
  return {
    id: 'r1', workspace_id: 'default', name: 'Ops digest', description: '', timezone: 'UTC',
    filters: { environment: 'all', project_ids: [], workflow_ids: [], run_kinds: 'all', time_range: { kind: 'last_7_days', start: '', end: '' } },
    sections: [{ id: 's', type: 'run_summary', title: '', options: {} }],
    schedule: { enabled: true, frequency: 'weekly', time: '09:00', weekly_days: [0], day_of_month: 1, send_when: 'always' },
    recipients: [{ id: 'x', type: 'email', target: 'a@test.invalid' }],
    attach_pdf: false, created_by_name: '', created_at: '', updated_at: '',
    time_range_label: 'Last 7 days', schedule_text: 'Every Mon at 09:00 UTC',
    next_send_at: '2026-11-16T09:00:00+00:00',
    delivery: { last_attempt_at: '2026-11-09T09:00:00+00:00', last_sent_at: null, last_status: 'failed', last_error: 'Mail server down', last_trigger: 'schedule' },
    ...overrides,
  };
}

const admin = { id: 'u', email: 'a@test.invalid', role: 'super_admin' };
const viewer = { id: 'v', email: 'v@test.invalid', role: 'viewer' };

describe('the Reports page', () => {
  beforeEach(() => {
    window.location.hash = '#reports';
    vi.mocked(reportsApi.catalog).mockResolvedValue(catalog);
    vi.mocked(reportsApi.list).mockResolvedValue({ reports: [report()] });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('lists each report with its schedule, next send and how the last send went', async () => {
    render(<ReportsPage user={admin} embedded />);
    expect(await screen.findByText('Ops digest')).toBeInTheDocument();
    expect(screen.getByText('Every Mon at 09:00 UTC')).toBeInTheDocument();
    expect(screen.getByText(/Next: .* 1 recipient/)).toBeInTheDocument();
    expect(screen.getByText('Not delivered')).toBeInTheDocument();
    expect(screen.getByText('Has not reached anyone yet')).toBeInTheDocument();
  });

  it('offers templates and keeps the inventory document', async () => {
    render(<ReportsPage user={admin} embedded />);
    expect(await screen.findByText('Weekly operations digest')).toBeInTheDocument();
    expect(screen.getByText('Installation inventory')).toBeInTheDocument();
  });

  it('shows a viewer the reports but nothing that changes them', async () => {
    render(<ReportsPage user={viewer} embedded />);
    expect(await screen.findByText('Ops digest')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '+ New report' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull();
    expect(screen.queryByText('Weekly operations digest')).toBeNull();
    expect(screen.getByRole('switch')).toBeDisabled();
  });

  it('does not open the designer for a viewer who has the link', async () => {
    window.location.hash = '#reports/new';
    render(<ReportsPage user={viewer} embedded />);
    await waitFor(() => expect(screen.getByTestId('reports-home')).toBeInTheDocument());
    expect(screen.queryByTestId('report-designer')).toBeNull();
  });
});
