import { describe, expect, it } from 'vitest';
import {
  blankDefinition, canAuthorReports, definitionFromTemplate, definitionProblems, deliveryLabel, describeSchedule,
  isEmail, moveSection, newSection, previewKey, splitAddresses,
  type DatasetSpec, type SectionSpec, type TemplateSpec,
} from '../components/reports/reportModel';

const datasets: Record<string, DatasetSpec> = {
  runs: {
    label: 'Runs', row: 'run', dimensions: [], measures: [], list_columns: [], filters: [],
    default_group: ['pipeline'], default_columns: ['runs', 'failed'],
  },
};

function spec(type: string): SectionSpec {
  return { type, group: 'Runs', title: type, description: '', source: '' };
}

describe('starting a report', () => {
  it('takes its sections, period and frequency from a template', () => {
    const template: TemplateSpec = {
      id: 'weekly', name: 'Weekly', description: 'Each week', time_range: 'last_30_days', frequency: 'monthly',
      sections: [{ type: 'run_summary', options: {} }, { type: 'failures', options: {} }],
    };
    const d = definitionFromTemplate(template, 'Asia/Kolkata');
    expect(d.name).toBe('Weekly');
    expect(d.timezone).toBe('Asia/Kolkata');
    expect(d.filters.time_range.kind).toBe('last_30_days');
    expect(d.schedule.frequency).toBe('monthly');
    expect(d.schedule.enabled).toBe(false);
    expect(d.sections.map((s) => s.type)).toEqual(['run_summary', 'failures']);
    expect(new Set(d.sections.map((s) => s.id)).size).toBe(2);
  });

  it('gives a new custom table workable defaults', () => {
    const section = newSection(spec('custom_table'), datasets);
    expect(section.options).toMatchObject({ dataset: 'runs', group_by: ['pipeline'], columns: ['runs', 'failed'], sort_by: 'runs' });
  });
});

describe('moving sections', () => {
  const sections = ['a', 'b', 'c'].map((id) => ({ id, type: id, title: '', options: {} }));

  it('moves up and down', () => {
    expect(moveSection(sections, 'c', -1).map((s) => s.id)).toEqual(['a', 'c', 'b']);
    expect(moveSection(sections, 'a', 1).map((s) => s.id)).toEqual(['b', 'a', 'c']);
  });

  it('stays put at the ends', () => {
    expect(moveSection(sections, 'a', -1)).toBe(sections);
    expect(moveSection(sections, 'c', 1)).toBe(sections);
  });
});

describe('addresses', () => {
  it('splits whatever was pasted', () => {
    expect(splitAddresses('a@x.io, b@x.io;c@x.io  d@x.io')).toEqual(['a@x.io', 'b@x.io', 'c@x.io', 'd@x.io']);
  });

  it('knows an email address when it sees one', () => {
    expect(isEmail('ops.team+x@company.co.in')).toBe(true);
    expect(isEmail('nope')).toBe(false);
    expect(isEmail('a@b')).toBe(false);
  });
});

describe('what stops a save', () => {
  const ready = () => ({
    ...blankDefinition('UTC'),
    name: 'Digest',
    sections: [{ id: '1', type: 'run_summary', title: '', options: {} }],
  });

  const messages = (d: ReturnType<typeof ready>) => definitionProblems(d).map((p) => p.message);

  it('passes a complete report', () => {
    expect(messages(ready())).toEqual([]);
  });

  it('needs a name and a section', () => {
    expect(messages({ ...ready(), name: '  ' })).toContain('Give the report a name.');
    expect(messages({ ...ready(), sections: [] })).toContain('Add at least one section.');
  });

  it('needs recipients for a schedule', () => {
    const d = ready();
    d.schedule.enabled = true;
    expect(messages(d)).toContain('Add at least one recipient, or turn the schedule off.');
    d.recipients = [{ id: 'r', type: 'email', target: 'a@x.io' }];
    expect(messages(d)).toEqual([]);
  });

  it('will not schedule a fixed period', () => {
    const d = ready();
    d.schedule.enabled = true;
    d.recipients = [{ id: 'r', type: 'email', target: 'a@x.io' }];
    d.filters.time_range = { kind: 'custom', start: '2026-01-01', end: '2026-01-31' };
    expect(messages(d)).toContain('A report on a schedule needs a period that moves, such as Last 7 days.');
  });

  it('checks a custom period', () => {
    const d = ready();
    d.filters.time_range = { kind: 'custom', start: '2026-02-01', end: '2026-01-01' };
    expect(messages(d)).toContain('The custom period ends before it starts.');
    d.filters.time_range = { kind: 'custom', start: '', end: '' };
    expect(messages(d)).toContain('Enter both dates for a custom period.');
  });

  it('checks recipients and notes', () => {
    const d = ready();
    d.recipients = [{ id: 'r', type: 'email', target: 'oops' }, { id: 'w', type: 'webhook', target: '' }];
    d.sections = [...d.sections, { id: '2', type: 'note', title: '', options: { text: ' ' } }];
    const got = messages(d);
    expect(got).toContain("'oops' is not an email address.");
    expect(got).toContain('Enter the webhook address.');
    expect(got).toContain('Write something in the note, or remove it.');
  });

  it('needs a day for a weekly schedule', () => {
    const d = ready();
    d.schedule.enabled = true;
    d.schedule.weekly_days = [];
    d.recipients = [{ id: 'r', type: 'email', target: 'a@x.io' }];
    expect(messages(d)).toContain('Choose at least one day of the week.');
  });
});

describe('words on the page', () => {
  it('describes a schedule', () => {
    const base = blankDefinition('UTC').schedule;
    expect(describeSchedule({ ...base, frequency: 'daily', time: '06:30' }, 'Asia/Kolkata')).toBe('Every day at 06:30 Asia/Kolkata');
    expect(describeSchedule({ ...base, weekly_days: [4, 0] }, 'UTC')).toBe('Every Mon, Fri at 09:00 UTC');
    expect(describeSchedule({ ...base, frequency: 'monthly', day_of_month: 3 }, 'UTC')).toBe('Monthly on day 3 at 09:00 UTC');
  });

  it('labels delivery results plainly', () => {
    expect(deliveryLabel('failed')).toBe('Not delivered');
    expect(deliveryLabel('skipped')).toBe('Nothing to send');
  });

  it('matches who the server lets author', () => {
    expect(canAuthorReports('developer')).toBe(true);
    expect(canAuthorReports('super_admin')).toBe(true);
    expect(canAuthorReports('analyst')).toBe(false);
    expect(canAuthorReports('viewer')).toBe(false);
    expect(canAuthorReports(undefined)).toBe(false);
  });
});

describe('refreshing the preview', () => {
  it('ignores who it goes to, which does not change the numbers', () => {
    const d = blankDefinition('UTC');
    const before = previewKey(d);
    expect(previewKey({ ...d, recipients: [{ id: 'r', type: 'email', target: 'a@x.io' }] })).toBe(before);
    expect(previewKey({ ...d, schedule: { ...d.schedule, enabled: true } })).toBe(before);
    expect(previewKey({ ...d, filters: { ...d.filters, run_kinds: 'automatic' } })).not.toBe(before);
  });
});
