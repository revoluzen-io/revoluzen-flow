import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ReportDocument from '../components/reports/ReportDocument';
import { NarrowIt, ScheduleAndShare, WhatGoesIn, type Update } from '../components/reports/ReportDesignerPanels';
import { blankDefinition, type Catalog, type ReportDefinition, type ReportResult } from '../components/reports/reportModel';

const catalog: Catalog = {
  sections: [
    { type: 'run_summary', group: 'Runs', title: 'Run summary', description: 'Counts', source: 'Run history' },
    { type: 'failures', group: 'Runs', title: 'Failures', description: 'Which failed', source: 'Run history', has_limit: true, default_limit: 10 },
    { type: 'custom_table', group: 'Your own', title: 'A table you design', description: 'Yours', source: 'Run history' },
  ],
  datasets: {
    runs: {
      label: 'Runs', row: 'run',
      dimensions: [{ key: 'pipeline', label: 'Pipeline' }, { key: 'status', label: 'Status' }],
      measures: [{ key: 'runs', label: 'Runs' }, { key: 'failed', label: 'Failed' }],
      list_columns: [{ key: 'started', label: 'Started' }, { key: 'pipeline', label: 'Pipeline' }, { key: 'error', label: 'Error' }],
      filters: [{ key: 'status', label: 'Status', values: ['Succeeded', 'Failed'] }],
      default_group: ['pipeline'], default_columns: ['runs', 'failed'],
    },
  },
  templates: [],
  environments: [{ value: 'dev', label: 'Development' }],
  channels: { email: false, slack_settings: true, teams_settings: false },
};

function Harness({ render: draw }: { render: (d: ReportDefinition, u: Update) => React.ReactNode }) {
  const [definition, setDefinition] = useState<ReportDefinition>(() => blankDefinition('UTC'));
  const update: Update = (patch) => setDefinition((d) => ({ ...d, ...patch }));
  return (
    <>
      {draw(definition, update)}
      <output data-testid="state">{JSON.stringify(definition)}</output>
    </>
  );
}

const state = (): ReportDefinition => JSON.parse(screen.getByTestId('state').textContent || '{}');

describe('choosing what goes in', () => {
  const panel = () => render(<Harness render={(d, u) => <WhatGoesIn definition={d} update={u} catalog={catalog} problems={[]} />} />);

  it('adds a section once and takes it off the list', () => {
    panel();
    fireEvent.click(screen.getByTestId('add-run_summary'));
    expect(state().sections.map((s) => s.type)).toEqual(['run_summary']);
    expect(screen.queryByTestId('add-run_summary')).toBeNull();
  });

  it('allows several tables of your own', () => {
    panel();
    fireEvent.click(screen.getByTestId('add-custom_table'));
    fireEvent.click(screen.getByTestId('add-custom_table'));
    expect(state().sections).toHaveLength(2);
  });

  it('removes and reorders', () => {
    panel();
    fireEvent.click(screen.getByTestId('add-run_summary'));
    fireEvent.click(screen.getByTestId('add-failures'));
    fireEvent.click(screen.getByLabelText('Move Failures up'));
    expect(state().sections.map((s) => s.type)).toEqual(['failures', 'run_summary']);
    fireEvent.click(screen.getByLabelText('Remove Failures'));
    expect(state().sections.map((s) => s.type)).toEqual(['run_summary']);
  });

  it('switches a designed table to one row per run, with that row\'s columns', () => {
    panel();
    fireEvent.click(screen.getByTestId('add-custom_table'));
    fireEvent.change(screen.getByLabelText('Group by'), { target: { value: '' } });
    const options = state().sections[0].options;
    expect(options.group_by).toEqual([]);
    expect(options.columns).toEqual(['started', 'pipeline', 'error']);
  });

  it('keeps the sort on a column that is still there', () => {
    panel();
    fireEvent.click(screen.getByTestId('add-custom_table'));
    fireEvent.click(screen.getByLabelText('Runs'));
    const options = state().sections[0].options;
    expect(options.columns).toEqual(['failed']);
    expect(options.sort_by).toBe('failed');
  });
});

describe('narrowing it', () => {
  const pickers = {
    projects: [{ id: 'p1', name: 'Finance' }, { id: 'p2', name: 'Marketing' }],
    pipelines: [{ id: 'w1', name: 'Sales', project_id: 'p1' }, { id: 'w2', name: 'Campaign', project_id: 'p2' }],
  };

  it('shows the one environment this edition has, without a choice', () => {
    render(<Harness render={(d, u) => <NarrowIt definition={d} update={u} catalog={catalog} pickers={pickers} problems={[]} />} />);
    expect(screen.getByText('Development')).toBeInTheDocument();
    expect(screen.queryByLabelText('Environment')).toBeNull();
  });

  it('offers a choice where there is one', () => {
    const two = { ...catalog, environments: [{ value: 'all', label: 'All' }, { value: 'prod', label: 'Production' }] };
    render(<Harness render={(d, u) => <NarrowIt definition={d} update={u} catalog={two} pickers={pickers} problems={[]} />} />);
    fireEvent.change(screen.getByLabelText('Environment'), { target: { value: 'prod' } });
    expect(state().filters.environment).toBe('prod');
  });

  it('limits the pipelines to the chosen projects', () => {
    render(<Harness render={(d, u) => <NarrowIt definition={d} update={u} catalog={catalog} pickers={pickers} problems={[]} />} />);
    fireEvent.click(screen.getByRole('button', { name: /Projects: All projects/ }));
    fireEvent.click(screen.getByLabelText('Finance'));
    expect(state().filters.project_ids).toEqual(['p1']);
    fireEvent.click(screen.getByRole('button', { name: /Pipelines: All pipelines/ }));
    expect(screen.getByLabelText('Sales')).toBeInTheDocument();
    expect(screen.queryByLabelText('Campaign')).toBeNull();
  });

  it('asks for dates only for a custom period', () => {
    render(<Harness render={(d, u) => <NarrowIt definition={d} update={u} catalog={catalog} pickers={pickers} problems={[]} />} />);
    expect(screen.queryByLabelText('From')).toBeNull();
    fireEvent.change(screen.getByLabelText('Period'), { target: { value: 'custom' } });
    expect(screen.getByLabelText('From')).toBeInTheDocument();
  });
});

describe('scheduling and sharing', () => {
  const panel = () => render(<Harness render={(d, u) => <ScheduleAndShare definition={d} update={u} catalog={catalog} problems={[]} />} />);

  it('turns what is typed into recipients, keeping what is wrong for correcting', () => {
    panel();
    const box = screen.getByLabelText('Email address');
    fireEvent.change(box, { target: { value: 'a@x.io, oops, b@x.io' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(state().recipients.map((r) => r.target)).toEqual(['a@x.io', 'b@x.io']);
    expect((box as HTMLInputElement).value).toBe('oops');
    expect(screen.getByText('oops is not an email address.')).toBeInTheDocument();
  });

  it('does not add the same address twice', () => {
    panel();
    const box = screen.getByLabelText('Email address');
    fireEvent.change(box, { target: { value: 'a@x.io' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    fireEvent.change(box, { target: { value: 'A@x.io' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(state().recipients).toHaveLength(1);
  });

  it('says when email has no mail server behind it', () => {
    panel();
    const box = screen.getByLabelText('Email address');
    fireEvent.change(box, { target: { value: 'a@x.io' } });
    fireEvent.blur(box);
    expect(screen.getByText(/Email needs a mail server/)).toBeInTheDocument();
  });

  it('adds Slack and says the saved webhook will be used', () => {
    panel();
    fireEvent.click(screen.getByLabelText('Post the headlines to Slack'));
    expect(state().recipients).toEqual([expect.objectContaining({ type: 'slack', target: '' })]);
    expect(screen.getByText('Leave empty to use the Slack webhook from Settings.')).toBeInTheDocument();
  });

  it('shows the schedule controls only when it is on', () => {
    panel();
    expect(screen.queryByRole('group', { name: 'How often' })).toBeNull();
    fireEvent.click(screen.getByTestId('schedule-on'));
    expect(state().schedule.enabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Wed' }));
    expect(state().schedule.weekly_days).toEqual([0, 2]);
    expect(screen.getByText(/Every Mon, Wed at 09:00 UTC/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Monthly' }));
    expect(screen.getByLabelText('Day of the month')).toBeInTheDocument();
  });
});

describe('a finished report', () => {
  const result: ReportResult = {
    name: 'Digest',
    description: '',
    generated_label: '10 Nov 2026 12:00 UTC',
    timezone: 'UTC',
    period: { label: 'Last 7 days', start: '', end: '' },
    filters: ['Projects: Finance'],
    notes: [],
    sections: [
      { id: 'a', type: 'run_summary', title: 'Run summary', source: '', blocks: [{ kind: 'kpis', items: [{ label: 'Failed', value: '3', tone: 'bad', detail: '1 the period before' }] }] },
      { id: 'b', type: 'failures', title: 'Failures', source: '', blocks: [{
        kind: 'table',
        columns: [{ label: 'Pipeline', align: 'left' }, { label: 'Failed', align: 'right' }],
        rows: [['Sales', { text: '3', tone: 'bad' }]],
        total: 9, empty: 'None.',
      }] },
      { id: 'c', type: 'schedules', title: 'Schedules', source: '', scope_note: 'As it stands now.', blocks: [{ kind: 'table', columns: [{ label: 'Pipeline', align: 'left' }], rows: [], total: 0, empty: 'No schedules set up.' }] },
      { id: 'd', type: 'run_trend', title: 'Runs over time', source: '', blocks: [{ kind: 'bars', series: [{ key: 'failed', label: 'Failed', tone: 'bad' }], rows: [{ label: 'Mon', values: { failed: 2 } }], empty: '', has_data: true }] },
    ],
  };

  it('shows the numbers, what was narrowed, and any cut-off table', () => {
    render(<ReportDocument result={result} />);
    expect(screen.getByText('Projects: Finance')).toBeInTheDocument();
    expect(screen.getAllByText('3')).toHaveLength(2);
    expect(screen.getByText('Showing 1 of 9.')).toBeInTheDocument();
    expect(screen.getByText('No schedules set up.')).toBeInTheDocument();
    expect(screen.getByText('As it stands now.')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Mon: 2 runs, 2 failed' })).toBeInTheDocument();
  });

  it('puts colour in the value and uses the standard table header', () => {
    render(<ReportDocument result={result} />);
    const cell = screen.getAllByText('3').find((el) => el.tagName === 'TD');
    expect(cell?.className).toContain('text-rose-600');
    expect(cell?.closest('table')?.querySelector('thead')?.className).toContain('bg-thead-dev-bg');
  });
});
