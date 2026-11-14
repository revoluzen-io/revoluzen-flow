import type { ReactNode } from 'react';
import { KPITile, type KPITone } from '../shared/KPI';
import type { Cell, KpiItem, ReportResult, ResultBlock, ResultSection, Tone } from './reportModel';

// A finished report on screen: the same blocks the email, chat message and
// PDF are made from. Cards stay neutral; colour is only in the value.

const TONE_TEXT: Record<string, string> = {
  good: 'text-emerald-600',
  bad: 'text-rose-600',
  warn: 'text-amber-600',
  primary: 'text-blue-600',
  neutral: 'text-slate-800',
};

const KPI_TONE: Record<string, KPITone> = {
  good: 'emerald', bad: 'red', warn: 'amber', primary: 'indigo', neutral: 'slate',
};

const BAR_TONE: Record<string, string> = {
  good: 'bg-emerald-400', bad: 'bg-rose-400', neutral: 'bg-slate-300',
};

function cellText(cell: Cell): string {
  return typeof cell === 'string' ? cell : cell.text;
}

function cellTone(cell: Cell): Tone {
  return typeof cell === 'string' ? '' : cell.tone;
}

function Kpis({ items }: { items: KpiItem[] }) {
  return (
    <div className="grid grid-cols-2 gap-2.5 md:grid-cols-3">
      {items.map((item) => (
        <KPITile
          key={item.label}
          label={item.label}
          value={item.value}
          sublabel={item.detail || undefined}
          tone={KPI_TONE[item.tone] || 'slate'}
        />
      ))}
    </div>
  );
}

function DataTable({ block }: { block: Extract<ResultBlock, { kind: 'table' }> }) {
  if (block.rows.length === 0) return <p className="text-sm text-slate-500">{block.empty}</p>;
  return (
    <div>
      <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table className="w-full text-[12.5px]">
          <thead className="bg-thead-dev-bg text-thead-dev-text border-b-2 border-thead-dev-border">
            <tr>
              {block.columns.map((c) => (
                <th
                  key={c.label}
                  className={`px-3 py-2 text-[10px] font-bold uppercase tracking-wider whitespace-nowrap ${c.align === 'right' ? 'text-right' : 'text-left'}`}
                >
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {block.rows.map((row, i) => (
              <tr key={i}>
                {row.map((cell, j) => {
                  const tone = cellTone(cell);
                  const align = block.columns[j]?.align === 'right' ? 'text-right tabular-nums' : 'text-left';
                  return (
                    <td
                      key={j}
                      className={`px-3 py-2 align-top ${align} ${tone ? `${TONE_TEXT[tone]} font-semibold` : 'text-slate-700'}`}
                    >
                      {cellText(cell)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {block.total > block.rows.length && (
        <p className="mt-1 text-[11px] text-slate-500">Showing {block.rows.length.toLocaleString()} of {block.total.toLocaleString()}.</p>
      )}
    </div>
  );
}

function Bars({ block }: { block: Extract<ResultBlock, { kind: 'bars' }> }) {
  if (!block.has_data) return <p className="text-sm text-slate-500">{block.empty}</p>;
  const biggest = Math.max(1, ...block.rows.map((r) => Object.values(r.values).reduce((a, b) => a + b, 0)));
  return (
    <div>
      <div className="space-y-1">
        {block.rows.map((row) => {
          const total = Object.values(row.values).reduce((a, b) => a + b, 0);
          const failed = row.values.failed || 0;
          return (
            <div key={row.label} className="flex items-center gap-2 text-[11.5px]">
              <span className="w-24 shrink-0 text-slate-500">{row.label}</span>
              <span className="flex h-3 flex-1 overflow-hidden rounded-sm bg-slate-100" role="img" aria-label={`${row.label}: ${total} runs, ${failed} failed`}>
                {block.series.map((series) => {
                  const value = row.values[series.key] || 0;
                  if (!value) return null;
                  return (
                    <span key={series.key} className={BAR_TONE[series.tone] || 'bg-slate-300'} style={{ width: `${(value / biggest) * 100}%` }} />
                  );
                })}
              </span>
              <span className="w-28 shrink-0 text-right tabular-nums text-slate-700">
                {total.toLocaleString()}
                {failed > 0 && <span className="text-rose-600"> ({failed.toLocaleString()} failed)</span>}
              </span>
            </div>
          );
        })}
      </div>
      <div className="mt-2 flex gap-3 text-[11px] text-slate-500">
        {block.series.map((s) => (
          <span key={s.key} className="inline-flex items-center gap-1">
            <span className={`inline-block h-2 w-2 rounded-sm ${BAR_TONE[s.tone] || 'bg-slate-300'}`} />
            {s.label}
          </span>
        ))}
      </div>
    </div>
  );
}

function Block({ block }: { block: ResultBlock }) {
  if (block.kind === 'kpis') return <Kpis items={block.items} />;
  if (block.kind === 'table') return <DataTable block={block} />;
  if (block.kind === 'bars') return <Bars block={block} />;
  return <p className={`whitespace-pre-wrap text-sm ${block.tone ? TONE_TEXT[block.tone] : 'text-slate-700'}`}>{block.text}</p>;
}

export function ResultSectionView({ section, tools }: { section: ResultSection; tools?: ReactNode }) {
  return (
    <section data-testid={`report-section-${section.type}`}>
      {(section.title || tools) && (
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            {section.title && <h3 className="text-sm font-bold text-slate-800">{section.title}</h3>}
            {section.scope_note && <p className="text-[11px] text-slate-500">{section.scope_note}</p>}
          </div>
          {tools}
        </div>
      )}
      <div className="mt-2 space-y-3">
        {section.blocks.map((block, i) => <Block key={i} block={block} />)}
      </div>
    </section>
  );
}

export default function ReportDocument({
  result,
  sectionTools,
}: {
  result: ReportResult;
  sectionTools?: (section: ResultSection) => ReactNode;
}) {
  return (
    <article className="space-y-6 text-slate-700" data-testid="report-document">
      <header>
        <h2 className="text-lg font-bold text-slate-900">{result.name}</h2>
        <p className="text-[13px] text-slate-600">{result.period.label}</p>
        {result.filters.length > 0 && <p className="text-xs text-slate-500">{result.filters.join(' · ')}</p>}
        {result.description && <p className="mt-1 text-[13px] text-slate-600">{result.description}</p>}
        {result.notes.map((note) => <p key={note} className="mt-1 text-xs text-amber-600">{note}</p>)}
      </header>
      {result.sections.map((section) => (
        <ResultSectionView key={section.id} section={section} tools={sectionTools?.(section)} />
      ))}
      <footer className="text-[11px] text-slate-500">Made on {result.generated_label}.</footer>
    </article>
  );
}
