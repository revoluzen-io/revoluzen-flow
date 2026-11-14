import { useEffect, useMemo, useRef, useState } from 'react';
import Icon from '../shared/Icon';
import {
  TIME_RANGES, WEEK_DAYS, describeSchedule, isEmail, localId, moveSection, newSection, splitAddresses,
  timezoneOptions,
  type Catalog, type DatasetSpec, type Problem, type Recipient, type RecipientType, type ReportDefinition,
  type ReportSection, type SectionSpec,
} from './reportModel';

// The three things someone decides about a report. Each layout on the
// designer arranges these same panels, so they have one behaviour.

export const INPUT = 'w-full rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-blue-300';
const LABEL = 'block text-xs font-semibold text-slate-600';
const SMALL_BUTTON = 'inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-xs font-semibold text-slate-600 hover:border-slate-300 hover:text-slate-800 disabled:cursor-not-allowed disabled:opacity-40';

export type Update = (patch: Partial<ReportDefinition>) => void;

export function ProblemList({ problems, areas }: { problems: Problem[]; areas: Problem['area'][] }) {
  const mine = problems.filter((p) => areas.includes(p.area));
  if (mine.length === 0) return null;
  return (
    <ul className="space-y-0.5 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800" role="alert">
      {mine.map((p) => <li key={p.message}>{p.message}</li>)}
    </ul>
  );
}

// ── What goes in ────────────────────────────────────────────────────────

function ArrowButton({ label, disabled, onClick, up }: { label: string; disabled: boolean; onClick: () => void; up: boolean }) {
  return (
    <button type="button" aria-label={label} title={label} disabled={disabled} onClick={onClick} className="rounded p-0.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700 disabled:opacity-30">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <polyline points={up ? '18 15 12 9 6 15' : '6 9 12 15 18 9'} />
      </svg>
    </button>
  );
}

function RemoveButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button type="button" aria-label={label} title={label} onClick={onClick} className="rounded p-0.5 text-slate-400 hover:bg-rose-50 hover:text-rose-600">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
    </button>
  );
}

export function SectionMoveTools({
  section, definition, update,
}: { section: ReportSection; definition: ReportDefinition; update: Update }) {
  const index = definition.sections.findIndex((s) => s.id === section.id);
  const title = section.title || section.type;
  return (
    <span className="flex shrink-0 items-center">
      <ArrowButton up label={`Move ${title} up`} disabled={index <= 0} onClick={() => update({ sections: moveSection(definition.sections, section.id, -1) })} />
      <ArrowButton up={false} label={`Move ${title} down`} disabled={index === definition.sections.length - 1} onClick={() => update({ sections: moveSection(definition.sections, section.id, 1) })} />
      <RemoveButton label={`Remove ${title}`} onClick={() => update({ sections: definition.sections.filter((s) => s.id !== section.id) })} />
    </span>
  );
}

function CheckboxList({
  legend, items, chosen, onToggle,
}: { legend: string; items: { key: string; label: string }[]; chosen: string[]; onToggle: (key: string) => void }) {
  return (
    <fieldset>
      <legend className={LABEL}>{legend}</legend>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
        {items.map((item) => (
          <label key={item.key} className="inline-flex items-center gap-1 text-xs text-slate-700">
            <input type="checkbox" checked={chosen.includes(item.key)} onChange={() => onToggle(item.key)} />
            {item.label}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

function CustomTableEditor({
  section, datasets, change,
}: { section: ReportSection; datasets: Record<string, DatasetSpec>; change: (options: Record<string, any>) => void }) {
  const o = section.options;
  const dataset = datasets[o.dataset] || datasets.runs;
  const groupBy: string[] = o.group_by || [];
  const grouped = groupBy.length > 0;
  const columnChoices = grouped ? dataset.measures : dataset.list_columns;
  const columns: string[] = o.columns || [];
  const where: Record<string, string[]> = o.where || {};
  const sortChoices = [
    ...columnChoices.filter((c) => columns.includes(c.key)),
    ...dataset.dimensions.filter((d) => groupBy.includes(d.key)),
  ];

  const setDataset = (key: string) => {
    const next = datasets[key];
    change({ ...o, dataset: key, group_by: [...next.default_group], columns: [...next.default_columns], where: {}, sort_by: next.default_columns[0], sort_dir: 'desc' });
  };
  const setGroup = (index: number, value: string) => {
    const next = [...groupBy];
    if (value) next[index] = value; else next.splice(index, 1);
    const unique = next.filter((v, i) => v && next.indexOf(v) === i);
    if (unique.length === 0 && grouped) {
      const first = dataset.list_columns.slice(0, 4).map((c) => c.key);
      change({ ...o, group_by: [], columns: first, sort_by: first[0] });
    } else if (unique.length > 0 && !grouped) {
      change({ ...o, group_by: unique, columns: [...dataset.default_columns], sort_by: dataset.default_columns[0] });
    } else {
      change({ ...o, group_by: unique });
    }
  };
  const toggleColumn = (key: string) => {
    const next = columns.includes(key) ? columns.filter((c) => c !== key) : [...columns, key];
    const sortStill = next.includes(o.sort_by) || groupBy.includes(o.sort_by);
    change({ ...o, columns: next, sort_by: sortStill ? o.sort_by : next[0] || '' });
  };
  const toggleWhere = (key: string, value: string) => {
    const current = where[key] || [];
    const next = current.includes(value) ? current.filter((v) => v !== value) : [...current, value];
    change({ ...o, where: { ...where, [key]: next } });
  };

  return (
    <div className="space-y-2.5">
      <div className="grid grid-cols-2 gap-2">
        <label className={LABEL}>Data
          <select className={`${INPUT} mt-1`} value={o.dataset} onChange={(e) => setDataset(e.target.value)}>
            {Object.entries(datasets).map(([key, d]) => <option key={key} value={key}>{d.label}</option>)}
          </select>
        </label>
        <label className={LABEL}>Group by
          <select className={`${INPUT} mt-1`} value={groupBy[0] || ''} onChange={(e) => setGroup(0, e.target.value)}>
            <option value="">Nothing: one row per {dataset.row}</option>
            {dataset.dimensions.map((d) => <option key={d.key} value={d.key}>{d.label}</option>)}
          </select>
        </label>
        {grouped && (
          <label className={LABEL}>Then by
            <select className={`${INPUT} mt-1`} value={groupBy[1] || ''} onChange={(e) => setGroup(1, e.target.value)}>
              <option value="">Nothing</option>
              {dataset.dimensions.filter((d) => d.key !== groupBy[0]).map((d) => <option key={d.key} value={d.key}>{d.label}</option>)}
            </select>
          </label>
        )}
      </div>
      <CheckboxList legend="Columns" items={columnChoices} chosen={columns} onToggle={toggleColumn} />
      {dataset.filters.map((f) => (
        <CheckboxList
          key={f.key}
          legend={`Only these: ${f.label} (none ticked means all)`}
          items={f.values.map((v) => ({ key: v, label: v }))}
          chosen={where[f.key] || []}
          onToggle={(v) => toggleWhere(f.key, v)}
        />
      ))}
      <div className="grid grid-cols-3 gap-2">
        <label className={LABEL}>Sort by
          <select className={`${INPUT} mt-1`} value={o.sort_by || ''} onChange={(e) => change({ ...o, sort_by: e.target.value })}>
            {sortChoices.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
          </select>
        </label>
        <label className={LABEL}>Order
          <select className={`${INPUT} mt-1`} value={o.sort_dir || 'desc'} onChange={(e) => change({ ...o, sort_dir: e.target.value })}>
            <option value="desc">Highest first</option>
            <option value="asc">Lowest first</option>
          </select>
        </label>
        <label className={LABEL}>Rows
          <input className={`${INPUT} mt-1`} type="number" min={1} max={200} value={o.limit ?? 20} onChange={(e) => change({ ...o, limit: clampLimit(e.target.value) })} />
        </label>
      </div>
    </div>
  );
}

function clampLimit(raw: string): number {
  const n = parseInt(raw, 10);
  return Number.isFinite(n) ? Math.max(1, Math.min(200, n)) : 10;
}

function SectionRow({
  section, spec, definition, update, catalog,
}: { section: ReportSection; spec: SectionSpec; definition: ReportDefinition; update: Update; catalog: Catalog }) {
  const custom = section.type === 'custom_table';
  const note = section.type === 'note';
  const [open, setOpen] = useState(custom || note);
  const change = (patch: Partial<ReportSection>) => update({
    sections: definition.sections.map((s) => (s.id === section.id ? { ...s, ...patch } : s)),
  });
  const adjustable = custom || note || spec.has_limit;
  return (
    <li className="rounded-lg border border-slate-200 bg-white" data-testid={`chosen-${section.type}`}>
      <div className="flex items-center gap-2 px-2.5 py-1.5">
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-slate-800">{section.title || spec.title}</span>
        <button type="button" className="text-[11px] font-semibold text-slate-500 hover:text-slate-800" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
          {open ? 'Close' : adjustable ? 'Change' : 'Rename'}
        </button>
        <SectionMoveTools section={{ ...section, title: section.title || spec.title }} definition={definition} update={update} />
      </div>
      {open && (
        <div className="space-y-2.5 border-t border-slate-100 px-2.5 py-2.5">
          <label className={LABEL}>Heading
            <input className={`${INPUT} mt-1`} value={section.title} placeholder={note ? 'No heading' : spec.title} maxLength={120} onChange={(e) => change({ title: e.target.value })} />
          </label>
          {spec.has_limit && (
            <label className={LABEL}>Rows to show
              <input className={`${INPUT} mt-1 w-24`} type="number" min={1} max={200} value={section.options.limit ?? spec.default_limit ?? 10} onChange={(e) => change({ options: { ...section.options, limit: clampLimit(e.target.value) } })} />
            </label>
          )}
          {note && (
            <label className={LABEL}>Your text
              <textarea className={`${INPUT} mt-1`} rows={3} maxLength={4000} value={section.options.text || ''} onChange={(e) => change({ options: { ...section.options, text: e.target.value } })} />
            </label>
          )}
          {custom && <CustomTableEditor section={section} datasets={catalog.datasets} change={(options) => change({ options })} />}
        </div>
      )}
    </li>
  );
}

export function AddSectionPalette({
  definition, update, catalog,
}: { definition: ReportDefinition; update: Update; catalog: Catalog }) {
  const groups = useMemo(() => {
    const out = new Map<string, SectionSpec[]>();
    for (const spec of catalog.sections) out.set(spec.group, [...(out.get(spec.group) || []), spec]);
    return Array.from(out.entries());
  }, [catalog.sections]);
  const used = new Set(definition.sections.map((s) => s.type));
  const available = (spec: SectionSpec) => spec.type === 'custom_table' || spec.type === 'note' || !used.has(spec.type);
  return (
    <div className="space-y-3">
      {groups.filter(([, specs]) => specs.some(available)).map(([group, specs]) => (
        <div key={group}>
          <div className="text-[11px] font-bold uppercase tracking-wider text-slate-500">{group}</div>
          <ul className="mt-1 space-y-1">
            {specs.filter(available).map((spec) => (
              <li key={spec.type}>
                <button
                  type="button"
                  data-testid={`add-${spec.type}`}
                  onClick={() => update({ sections: [...definition.sections, newSection(spec, catalog.datasets)] })}
                  className="w-full rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-left transition hover:border-blue-300"
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="text-[13px] font-medium text-slate-800">{spec.title}</span>
                    <span className="text-[11px] font-semibold text-slate-500">+ Add</span>
                  </span>
                  <span className="mt-0.5 block text-[11.5px] leading-snug text-slate-500">{spec.description}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

export function ChosenSections({
  definition, update, catalog,
}: { definition: ReportDefinition; update: Update; catalog: Catalog }) {
  const specs = useMemo(() => new Map(catalog.sections.map((s) => [s.type, s])), [catalog.sections]);
  if (definition.sections.length === 0) {
    return <p className="rounded-lg border border-dashed border-slate-300 px-3 py-4 text-center text-xs text-slate-500">Nothing yet. Add sections from the list below.</p>;
  }
  return (
    <ol className="space-y-1.5">
      {definition.sections.map((section) => {
        const spec = specs.get(section.type);
        if (!spec) return null;
        return <SectionRow key={section.id} section={section} spec={spec} definition={definition} update={update} catalog={catalog} />;
      })}
    </ol>
  );
}

export function WhatGoesIn({
  definition, update, catalog, problems,
}: { definition: ReportDefinition; update: Update; catalog: Catalog; problems: Problem[] }) {
  return (
    <div className="space-y-3">
      <ProblemList problems={problems} areas={['sections']} />
      <div>
        <div className="mb-1 text-xs font-semibold text-slate-600">In this report, in this order</div>
        <ChosenSections definition={definition} update={update} catalog={catalog} />
      </div>
      <div>
        <div className="mb-1 text-xs font-semibold text-slate-600">Add a section</div>
        <AddSectionPalette definition={definition} update={update} catalog={catalog} />
      </div>
    </div>
  );
}

// ── Narrow it ───────────────────────────────────────────────────────────

function MultiPicker({
  label, allLabel, options, selected, onChange,
}: {
  label: string;
  allLabel: string;
  options: { id: string; name: string }[];
  selected: string[];
  onChange: (ids: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');
  const box = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return undefined;
    const close = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);
  const shown = options.filter((o) => o.name.toLowerCase().includes(search.toLowerCase()));
  const summary = selected.length === 0 ? allLabel : selected.length === 1
    ? (options.find((o) => o.id === selected[0])?.name || '1 chosen')
    : `${selected.length} chosen`;
  return (
    <div ref={box} className="relative">
      <span className={LABEL}>{label}</span>
      <button type="button" aria-expanded={open} aria-label={`${label}: ${summary}`} className={`${INPUT} mt-1 flex items-center justify-between text-left`} onClick={() => setOpen((v) => !v)}>
        <span className="truncate">{summary}</span>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="shrink-0 text-slate-400"><polyline points="6 9 12 15 18 9" /></svg>
      </button>
      {open && (
        <div className="absolute z-30 mt-1 w-full rounded-lg border border-slate-200 bg-white p-2 shadow-lg">
          <input className={INPUT} placeholder="Search" value={search} onChange={(e) => setSearch(e.target.value)} aria-label={`Search ${label}`} />
          <ul className="mt-1.5 max-h-52 space-y-0.5 overflow-auto">
            {shown.length === 0 && <li className="px-1 py-1 text-xs text-slate-500">Nothing found.</li>}
            {shown.map((o) => (
              <li key={o.id}>
                <label className="flex items-center gap-2 rounded px-1 py-0.5 text-[13px] text-slate-700 hover:bg-slate-50">
                  <input
                    type="checkbox"
                    checked={selected.includes(o.id)}
                    onChange={() => onChange(selected.includes(o.id) ? selected.filter((i) => i !== o.id) : [...selected, o.id])}
                  />
                  <span className="truncate">{o.name}</span>
                </label>
              </li>
            ))}
          </ul>
          {selected.length > 0 && (
            <button type="button" className="mt-1.5 text-xs font-semibold text-slate-500 hover:text-slate-800" onClick={() => onChange([])}>Clear</button>
          )}
        </div>
      )}
    </div>
  );
}

export interface PickerData {
  projects: { id: string; name: string }[];
  pipelines: { id: string; name: string; project_id: string }[];
}

export function NarrowIt({
  definition, update, catalog, pickers, problems, compact = false,
}: {
  definition: ReportDefinition;
  update: Update;
  catalog: Catalog;
  pickers: PickerData;
  problems: Problem[];
  compact?: boolean;
}) {
  const f = definition.filters;
  const setFilters = (patch: Partial<ReportDefinition['filters']>) => update({ filters: { ...f, ...patch } });
  const range = f.time_range;
  const environments = catalog.environments;
  const pipelines = f.project_ids.length
    ? pickers.pipelines.filter((p) => f.project_ids.includes(p.project_id))
    : pickers.pipelines;
  const tzs = useMemo(() => timezoneOptions(definition.timezone), [definition.timezone]);
  return (
    <div className="space-y-3">
      <ProblemList problems={problems} areas={['narrow']} />
      <div className={compact ? 'grid grid-cols-2 gap-2.5 md:grid-cols-4 xl:grid-cols-6' : 'grid grid-cols-2 gap-2.5'}>
        <label className={LABEL}>Period
          <select
            className={`${INPUT} mt-1`}
            value={range.kind}
            onChange={(e) => setFilters({ time_range: { ...range, kind: e.target.value } })}
          >
            {TIME_RANGES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
          </select>
        </label>
        <label className={LABEL}>Time zone
          <select className={`${INPUT} mt-1`} value={definition.timezone} onChange={(e) => update({ timezone: e.target.value })}>
            {tzs.map((z) => <option key={z} value={z}>{z}</option>)}
          </select>
        </label>
        {range.kind === 'custom' && (
          <>
            <label className={LABEL}>From
              <input type="date" className={`${INPUT} mt-1`} value={range.start} onChange={(e) => setFilters({ time_range: { ...range, start: e.target.value } })} />
            </label>
            <label className={LABEL}>To
              <input type="date" className={`${INPUT} mt-1`} value={range.end} onChange={(e) => setFilters({ time_range: { ...range, end: e.target.value } })} />
            </label>
          </>
        )}
        {environments.length > 1 ? (
          <label className={LABEL}>Environment
            <select className={`${INPUT} mt-1`} value={f.environment} onChange={(e) => setFilters({ environment: e.target.value })}>
              {environments.map((env) => <option key={env.value} value={env.value}>{env.label}</option>)}
            </select>
          </label>
        ) : (
          <div>
            <span className={LABEL}>Environment</span>
            <div className="mt-1 rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1.5 text-sm text-slate-600" title="This edition has one environment.">
              {environments[0]?.label || 'Development'}
            </div>
          </div>
        )}
        <label className={LABEL}>Runs to count
          <select className={`${INPUT} mt-1`} value={f.run_kinds} onChange={(e) => setFilters({ run_kinds: e.target.value as 'all' | 'automatic' })}>
            <option value="all">All runs</option>
            <option value="automatic">Automatic runs only</option>
          </select>
        </label>
        <MultiPicker label="Projects" allLabel="All projects" options={pickers.projects} selected={f.project_ids} onChange={(ids) => setFilters({ project_ids: ids })} />
        <MultiPicker label="Pipelines" allLabel="All pipelines" options={pipelines} selected={f.workflow_ids} onChange={(ids) => setFilters({ workflow_ids: ids })} />
      </div>
      {f.run_kinds === 'automatic' && (
        <p className="text-[11px] text-slate-500">Leaves out runs someone started by hand while building: scheduled, API, webhook, replayed and pipeline-started runs stay in.</p>
      )}
    </div>
  );
}

// ── Schedule and share ──────────────────────────────────────────────────

function SegmentedChoice<T extends string | number>({
  value, options, onChange, label,
}: { value: T; options: { value: T; label: string }[]; onChange: (v: T) => void; label: string }) {
  return (
    <div role="group" aria-label={label} className="inline-flex rounded-lg border border-slate-200 bg-slate-50 p-0.5">
      {options.map((o) => (
        <button
          key={String(o.value)}
          type="button"
          aria-pressed={o.value === value}
          onClick={() => onChange(o.value)}
          className={`rounded-md px-2.5 py-1 text-xs font-semibold transition ${o.value === value ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-600 hover:text-slate-800'}`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function ChannelRow({
  type, title, placeholder, hint, recipients, onChange,
}: {
  type: RecipientType;
  title: string;
  placeholder: string;
  hint: string;
  recipients: Recipient[];
  onChange: (next: Recipient[]) => void;
}) {
  const current = recipients.find((r) => r.type === type);
  const toggle = (on: boolean) => {
    onChange(on ? [...recipients, { id: localId(), type, target: '' }] : recipients.filter((r) => r.type !== type));
  };
  return (
    <div>
      <label className="inline-flex items-center gap-2 text-[13px] text-slate-700">
        <input type="checkbox" checked={Boolean(current)} onChange={(e) => toggle(e.target.checked)} />
        {title}
      </label>
      {current && (
        <div className="ml-5 mt-1">
          <input
            className={INPUT}
            aria-label={`${title} address`}
            placeholder={placeholder}
            value={current.target}
            onChange={(e) => onChange(recipients.map((r) => (r.type === type ? { ...r, target: e.target.value } : r)))}
          />
          {hint && <p className="mt-0.5 text-[11px] text-slate-500">{hint}</p>}
        </div>
      )}
    </div>
  );
}

export function ScheduleAndShare({
  definition, update, catalog, problems, onSendTest, sendingTest,
}: {
  definition: ReportDefinition;
  update: Update;
  catalog: Catalog;
  problems: Problem[];
  onSendTest?: () => void;
  sendingTest?: boolean;
}) {
  const s = definition.schedule;
  const setSchedule = (patch: Partial<ReportDefinition['schedule']>) => update({ schedule: { ...s, ...patch } });
  const [typed, setTyped] = useState('');
  const [typedError, setTypedError] = useState('');
  const recipients = definition.recipients;
  const emails = recipients.filter((r) => r.type === 'email');
  const setRecipients = (next: Recipient[]) => update({ recipients: next });

  const addTyped = () => {
    const parts = splitAddresses(typed);
    if (parts.length === 0) return;
    const bad = parts.filter((p) => !isEmail(p));
    const known = new Set(emails.map((e) => e.target.toLowerCase()));
    const fresh = parts.filter((p) => isEmail(p) && !known.has(p.toLowerCase()));
    if (fresh.length) setRecipients([...recipients, ...fresh.map((target) => ({ id: localId(), type: 'email' as const, target }))]);
    setTyped(bad.join(', '));
    setTypedError(bad.length ? `${bad.join(', ')} ${bad.length === 1 ? 'is' : 'are'} not an email address.` : '');
  };

  const toggleDay = (day: number) => {
    const days = s.weekly_days.includes(day) ? s.weekly_days.filter((d) => d !== day) : [...s.weekly_days, day];
    setSchedule({ weekly_days: days });
  };

  return (
    <div className="space-y-4">
      <ProblemList problems={problems} areas={['schedule', 'recipients']} />
      <div>
        <div className={LABEL}>Send it to</div>
        <div className="mt-1 flex flex-wrap items-center gap-1 rounded-lg border border-slate-200 bg-white px-1.5 py-1 focus-within:ring-2 focus-within:ring-blue-300">
          {emails.map((r) => (
            <span key={r.id} className="inline-flex items-center gap-1 rounded-md bg-slate-100 px-1.5 py-0.5 text-xs text-slate-700">
              {r.target}
              <button type="button" aria-label={`Remove ${r.target}`} className="text-slate-400 hover:text-rose-600" onClick={() => setRecipients(recipients.filter((x) => x.id !== r.id))}>×</button>
            </span>
          ))}
          <input
            className="min-w-[10rem] flex-1 bg-transparent px-1 py-0.5 text-sm text-slate-800 focus:outline-none"
            aria-label="Email address"
            placeholder={emails.length ? 'Add another' : 'name@company.com'}
            value={typed}
            onChange={(e) => { setTyped(e.target.value); setTypedError(''); }}
            onBlur={addTyped}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ',' || e.key === ';') { e.preventDefault(); addTyped(); }
            }}
          />
        </div>
        {typedError && <p className="mt-0.5 text-[11px] text-amber-700">{typedError}</p>}
        {emails.length > 0 && !catalog.channels.email && (
          <p className="mt-1 rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-xs text-amber-800">
            Email needs a mail server. <a className="font-semibold underline" href="#settings/notifications" onClick={() => { try { sessionStorage.setItem('revoluzen_flow_settings_jump_to', 'smtp'); } catch { /* just opens the tab */ } }}>Set one up in Settings</a>.
          </p>
        )}
      </div>

      <div className="space-y-1.5">
        <ChannelRow
          type="slack" title="Post the headlines to Slack" placeholder="https://hooks.slack.com/…" recipients={recipients} onChange={setRecipients}
          hint={catalog.channels.slack_settings ? 'Leave empty to use the Slack webhook from Settings.' : 'Paste the webhook address from Slack.'}
        />
        <ChannelRow
          type="teams" title="Post the headlines to Teams" placeholder="https://…webhook.office.com/…" recipients={recipients} onChange={setRecipients}
          hint={catalog.channels.teams_settings ? 'Leave empty to use the Teams webhook from Settings.' : 'Paste the webhook address from Teams.'}
        />
        <ChannelRow type="webhook" title="Send the full report to a webhook" placeholder="https://…" recipients={recipients} onChange={setRecipients} hint="" />
      </div>

      <label className="inline-flex items-center gap-2 text-[13px] text-slate-700">
        <input type="checkbox" checked={definition.attach_pdf} onChange={(e) => update({ attach_pdf: e.target.checked })} />
        Attach a PDF to the email
      </label>

      <div className="rounded-lg border border-slate-200 bg-white p-3">
        <label className="inline-flex items-center gap-2 text-[13px] font-semibold text-slate-800">
          <input type="checkbox" data-testid="schedule-on" checked={s.enabled} onChange={(e) => setSchedule({ enabled: e.target.checked })} />
          Send this report automatically
        </label>
        {s.enabled && (
          <div className="mt-2.5 space-y-2.5">
            <SegmentedChoice
              label="How often"
              value={s.frequency}
              onChange={(frequency) => setSchedule({ frequency })}
              options={[{ value: 'daily', label: 'Daily' }, { value: 'weekly', label: 'Weekly' }, { value: 'monthly', label: 'Monthly' }]}
            />
            {s.frequency === 'weekly' && (
              <div role="group" aria-label="Days of the week" className="flex flex-wrap gap-1">
                {WEEK_DAYS.map((name, day) => (
                  <button
                    key={name}
                    type="button"
                    aria-pressed={s.weekly_days.includes(day)}
                    onClick={() => toggleDay(day)}
                    className={`rounded-md border px-2 py-1 text-xs font-semibold ${s.weekly_days.includes(day) ? 'border-blue-300 bg-blue-50 text-blue-700' : 'border-slate-200 bg-white text-slate-600'}`}
                  >
                    {name}
                  </button>
                ))}
              </div>
            )}
            <div className="grid grid-cols-2 gap-2">
              {s.frequency === 'monthly' && (
                <label className={LABEL}>Day of the month
                  <select className={`${INPUT} mt-1`} value={s.day_of_month} onChange={(e) => setSchedule({ day_of_month: Number(e.target.value) })}>
                    {Array.from({ length: 28 }, (_, i) => i + 1).map((d) => <option key={d} value={d}>{d}</option>)}
                  </select>
                </label>
              )}
              <label className={LABEL}>At
                <input type="time" className={`${INPUT} mt-1`} value={s.time} onChange={(e) => setSchedule({ time: e.target.value })} />
              </label>
            </div>
            <div>
              <div className={LABEL}>Send</div>
              <div className="mt-1">
                <SegmentedChoice
                  label="When to send"
                  value={s.send_when}
                  onChange={(send_when) => setSchedule({ send_when })}
                  options={[{ value: 'always', label: 'Every time' }, { value: 'failures', label: 'Only when runs failed' }]}
                />
              </div>
            </div>
            <p className="text-xs text-slate-500">{describeSchedule(s, definition.timezone)}. If this server is off at that time, the report goes out when it is next running.</p>
          </div>
        )}
      </div>

      {onSendTest && (
        <button type="button" className={SMALL_BUTTON} disabled={sendingTest} onClick={onSendTest}>
          <Icon name="mail" size={12} />
          {sendingTest ? 'Sending…' : 'Send a test to me'}
        </button>
      )}
    </div>
  );
}
