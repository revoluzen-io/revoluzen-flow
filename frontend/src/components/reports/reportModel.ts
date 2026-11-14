/**
 * What a report is, on the page. The backend holds the rules that matter
 * (backend: reports/builder/models.py); the checks here only let the designer
 * say what is missing before it asks the server.
 */

export interface SectionSpec {
  type: string;
  group: string;
  title: string;
  description: string;
  source: string;
  has_limit?: boolean;
  default_limit?: number;
  ignores_filters?: boolean;
}

export interface FieldSpec { key: string; label: string }
export interface FilterSpec extends FieldSpec { values: string[] }

export interface DatasetSpec {
  label: string;
  row: string;
  dimensions: FieldSpec[];
  measures: FieldSpec[];
  list_columns: FieldSpec[];
  filters: FilterSpec[];
  default_group: string[];
  default_columns: string[];
}

export interface TemplateSpec {
  id: string;
  name: string;
  description: string;
  time_range: string;
  frequency: Frequency;
  sections: { type: string; options: Record<string, unknown> }[];
}

export interface Catalog {
  sections: SectionSpec[];
  datasets: Record<string, DatasetSpec>;
  templates: TemplateSpec[];
  environments: { value: string; label: string }[];
  channels: { email: boolean; slack_settings: boolean; teams_settings: boolean };
}

export type Frequency = 'daily' | 'weekly' | 'monthly';
export type RecipientType = 'email' | 'slack' | 'teams' | 'webhook';

export interface ReportSection {
  id: string;
  type: string;
  title: string;
  options: Record<string, any>;
}

export interface Recipient {
  id: string;
  type: RecipientType;
  target: string;
}

export interface ReportSchedule {
  enabled: boolean;
  frequency: Frequency;
  time: string;
  weekly_days: number[];
  day_of_month: number;
  send_when: 'always' | 'failures';
}

export interface TimeRange { kind: string; start: string; end: string }

export interface ReportFilters {
  environment: string;
  project_ids: string[];
  workflow_ids: string[];
  run_kinds: 'all' | 'automatic';
  time_range: TimeRange;
}

export interface ReportDefinition {
  name: string;
  description: string;
  timezone: string;
  filters: ReportFilters;
  sections: ReportSection[];
  schedule: ReportSchedule;
  recipients: Recipient[];
  attach_pdf: boolean;
}

export interface SavedReport extends ReportDefinition {
  id: string;
  workspace_id: string;
  created_by_name: string;
  created_at: string;
  updated_at: string;
  time_range_label: string;
  schedule_text: string;
  next_send_at: string | null;
  delivery: {
    last_attempt_at: string | null;
    last_sent_at: string | null;
    last_status: string;
    last_error: string;
    last_trigger: string;
  };
}

export type Tone = 'good' | 'bad' | 'warn' | 'primary' | 'neutral' | '';
export type Cell = string | { text: string; tone: Tone };

export interface KpiItem { label: string; value: string; tone: Tone; detail: string }

export type ResultBlock =
  | { kind: 'kpis'; items: KpiItem[] }
  | { kind: 'table'; columns: { label: string; align: 'left' | 'right' }[]; rows: Cell[][]; total: number; empty: string }
  | { kind: 'bars'; series: { key: string; label: string; tone: Tone }[]; rows: { label: string; values: Record<string, number> }[]; empty: string; has_data: boolean }
  | { kind: 'text'; text: string; tone: Tone };

export interface ResultSection {
  id: string;
  type: string;
  title: string;
  source: string;
  scope_note?: string;
  blocks: ResultBlock[];
}

export interface ReportResult {
  name: string;
  description: string;
  generated_label: string;
  timezone: string;
  period: { label: string; start: string; end: string };
  filters: string[];
  sections: ResultSection[];
  notes: string[];
}

export interface DeliveryTarget { type: string; target: string; status: string; error: string }

export interface ReportDelivery {
  id: string;
  report_id: string;
  report_name: string;
  trigger: 'schedule' | 'manual' | 'test';
  status: 'delivered' | 'partial' | 'failed' | 'skipped';
  detail: string;
  period: string;
  requested_by: string;
  targets: DeliveryTarget[];
  started_at: string;
  finished_at: string | null;
  has_snapshot?: boolean;
}

export const TIME_RANGES: { value: string; label: string }[] = [
  { value: 'last_24_hours', label: 'Last 24 hours' },
  { value: 'last_7_days', label: 'Last 7 days' },
  { value: 'last_30_days', label: 'Last 30 days' },
  { value: 'last_90_days', label: 'Last 90 days' },
  { value: 'today', label: 'Today' },
  { value: 'yesterday', label: 'Yesterday' },
  { value: 'this_week', label: 'This week' },
  { value: 'last_week', label: 'Last week' },
  { value: 'this_month', label: 'This month' },
  { value: 'last_month', label: 'Last month' },
  { value: 'custom', label: 'Custom dates' },
];

export const WEEK_DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

let counter = 0;

export function localId(): string {
  counter += 1;
  return `${Date.now().toString(36)}${counter.toString(36)}`;
}

export function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

export function timezoneOptions(current: string): string[] {
  let zones: string[] = [];
  try {
    const supported = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf;
    if (supported) zones = supported('timeZone');
  } catch {
    zones = [];
  }
  const all = new Set(['UTC', ...zones, current].filter(Boolean));
  return Array.from(all).sort((a, b) => (a === 'UTC' ? -1 : b === 'UTC' ? 1 : a.localeCompare(b)));
}

export function blankDefinition(timezone = browserTimezone()): ReportDefinition {
  return {
    name: '',
    description: '',
    timezone,
    filters: {
      environment: 'all',
      project_ids: [],
      workflow_ids: [],
      run_kinds: 'all',
      time_range: { kind: 'last_7_days', start: '', end: '' },
    },
    sections: [],
    schedule: { enabled: false, frequency: 'weekly', time: '09:00', weekly_days: [0], day_of_month: 1, send_when: 'always' },
    recipients: [],
    attach_pdf: false,
  };
}

export function definitionFromTemplate(template: TemplateSpec, timezone = browserTimezone()): ReportDefinition {
  const base = blankDefinition(timezone);
  return {
    ...base,
    name: template.name,
    description: template.description,
    filters: { ...base.filters, time_range: { kind: template.time_range, start: '', end: '' } },
    sections: template.sections.map((s) => ({ id: localId(), type: s.type, title: '', options: { ...s.options } })),
    schedule: { ...base.schedule, frequency: template.frequency },
  };
}

export function newSection(spec: SectionSpec, datasets: Record<string, DatasetSpec>): ReportSection {
  const options: Record<string, any> = {};
  if (spec.type === 'custom_table') {
    const dataset = datasets.runs;
    options.dataset = 'runs';
    options.group_by = [...dataset.default_group];
    options.columns = [...dataset.default_columns];
    options.where = {};
    options.sort_by = dataset.default_columns[0];
    options.sort_dir = 'desc';
    options.limit = 20;
  }
  if (spec.type === 'note') options.text = '';
  return { id: localId(), type: spec.type, title: '', options };
}

export function moveSection(sections: ReportSection[], id: string, delta: -1 | 1): ReportSection[] {
  const from = sections.findIndex((s) => s.id === id);
  const to = from + delta;
  if (from < 0 || to < 0 || to >= sections.length) return sections;
  const next = [...sections];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

const EMAIL = /^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$/;

export function isEmail(value: string): boolean {
  return EMAIL.test(value.trim());
}

/** Addresses out of whatever was typed or pasted. */
export function splitAddresses(text: string): string[] {
  return text.split(/[\s,;]+/).map((a) => a.trim()).filter(Boolean);
}

export interface Problem { area: 'name' | 'sections' | 'narrow' | 'schedule' | 'recipients'; message: string }

export function definitionProblems(d: ReportDefinition): Problem[] {
  const problems: Problem[] = [];
  if (!d.name.trim()) problems.push({ area: 'name', message: 'Give the report a name.' });
  if (d.sections.length === 0) problems.push({ area: 'sections', message: 'Add at least one section.' });
  for (const s of d.sections) {
    if (s.type === 'note' && !String(s.options.text || '').trim()) {
      problems.push({ area: 'sections', message: 'Write something in the note, or remove it.' });
    }
    if (s.type === 'custom_table' && !(s.options.columns || []).length) {
      problems.push({ area: 'sections', message: 'Choose at least one column for your table.' });
    }
  }
  const range = d.filters.time_range;
  if (range.kind === 'custom') {
    if (!range.start || !range.end) problems.push({ area: 'narrow', message: 'Enter both dates for a custom period.' });
    else if (range.end < range.start) problems.push({ area: 'narrow', message: 'The custom period ends before it starts.' });
  }
  for (const r of d.recipients) {
    if (r.type === 'email' && !isEmail(r.target)) {
      problems.push({ area: 'recipients', message: `'${r.target}' is not an email address.` });
    }
    if (r.type === 'webhook' && !r.target.trim()) {
      problems.push({ area: 'recipients', message: 'Enter the webhook address.' });
    }
  }
  if (d.schedule.enabled) {
    if (d.recipients.length === 0) problems.push({ area: 'recipients', message: 'Add at least one recipient, or turn the schedule off.' });
    if (range.kind === 'custom') problems.push({ area: 'schedule', message: 'A report on a schedule needs a period that moves, such as Last 7 days.' });
    if (d.schedule.frequency === 'weekly' && d.schedule.weekly_days.length === 0) {
      problems.push({ area: 'schedule', message: 'Choose at least one day of the week.' });
    }
  }
  return problems;
}

/** The schedule in a sentence, before it has been saved. */
export function describeSchedule(s: ReportSchedule, timezone: string): string {
  const tail = `at ${s.time} ${timezone || 'UTC'}`;
  if (s.frequency === 'daily') return `Every day ${tail}`;
  if (s.frequency === 'weekly') {
    const days = [...s.weekly_days].sort((a, b) => a - b).map((d) => WEEK_DAYS[d]).join(', ');
    return days ? `Every ${days} ${tail}` : 'Choose at least one day';
  }
  return `Monthly on day ${s.day_of_month} ${tail}`;
}

export function deliveryLabel(status: string): string {
  return {
    delivered: 'Delivered',
    partial: 'Partly delivered',
    failed: 'Not delivered',
    skipped: 'Nothing to send',
  }[status] || status;
}

export function deliveryPillStatus(status: string): string {
  return { delivered: 'success', partial: 'warning', failed: 'error', skipped: 'skipped' }[status] || 'info';
}

export function triggerLabel(trigger: string): string {
  return { schedule: 'On schedule', manual: 'Sent now', test: 'Test to me' }[trigger] || trigger;
}

export function formatWhen(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

/** The parts of a report that change its numbers, for deciding when to refresh. */
export function previewKey(d: ReportDefinition): string {
  return JSON.stringify({ t: d.timezone, f: d.filters, s: d.sections, n: d.name, de: d.description });
}

const AUTHOR_RANK: Record<string, number> = {
  super_admin: 100, admin: 90, workspace_admin: 90, lead: 90, data_engineer: 70, developer: 50, member: 50,
};

/** Who can make and send reports: at least a developer, same as alert rules. */
export function canAuthorReports(role: string | undefined): boolean {
  return (AUTHOR_RANK[role || ''] || 0) >= 50;
}
