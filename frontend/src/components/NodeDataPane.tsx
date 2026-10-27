/**
 * Input / Output pane for the docked node config (2026-10-24).
 *
 * The config used to be a card floating over the canvas while the data it
 * was about lived in the bottom strip BEHIND it, with input and output as
 * tabs you flipped between. So while editing a Filter you could not see
 * the rows going in and the rows coming out at the same time — which, for
 * a data tool, is the entire job.
 *
 * ConfigPanel now lays out Input | Parameters | Output side by side (n8n's
 * NDV). This renders the two outer columns. Both draw rows with
 * `shared/ResultTable`, the same renderer the bottom strip uses, so the
 * two sides are directly comparable rather than merely similar.
 */

import { useMemo, useState } from 'react';
import type { StepResult } from '../stores/workflowStore';
import { DataTable, SchemaView, JsonView } from './shared/ResultTable';
import { ModuleIcon } from './ModulesPanel';

export interface PaneNode {
  id: string;
  label: string;
  stepType: string;
}

function PaneShell({
  kind,
  children,
  header,
  collapsible,
  open = true,
  onToggle,
}: {
  kind: 'input' | 'output';
  children: React.ReactNode;
  header: React.ReactNode;
  /** Set when the dock has stacked its panes into a column. Three full
   *  panes one above another gave each about 200px and buried the canvas
   *  under the dock; collapsed to a header strip, the same three parts
   *  stay in the same order and the parameters form keeps the room. */
  collapsible?: boolean;
  open?: boolean;
  onToggle?: () => void;
}) {
  const shut = collapsible && !open;
  return (
    <section
      data-revoluzen-flow-pane={kind}
      data-collapsed={shut ? 'true' : undefined}
      aria-label={kind === 'input' ? 'Input data' : 'Output data'}
      // flex-1 with a real minimum: the pane shares the row with a
      // parameters column whose width the user can drag, and without a
      // floor it collapses to a sliver instead of pushing back.
      //
      // No `h-full` and no border: the dock is a flex container in BOTH
      // directions — a row when the panes fit side by side, a column
      // when they stack — and `h-full` means 100% of the parent, which
      // overflows three times over in a column. `flex-1` + `min-h-0`
      // works either way, and the dock's `gap-px` over a slate
      // background already draws the separators, in whichever direction
      // it happens to be running.
      className={`flex flex-col min-w-[180px] bg-white overflow-hidden ${
        shut ? 'shrink-0' : 'flex-1 min-h-0'
      }`}
    >
      <div className="px-3 py-2 border-b border-slate-200 bg-slate-50/80 flex items-center gap-2 shrink-0 min-h-[44px]">
        {collapsible && (
          <button
            onClick={onToggle}
            aria-expanded={open}
            title={open ? 'Collapse' : 'Expand'}
            className="shrink-0 p-0.5 -ml-1 rounded text-slate-500 hover:text-slate-700 hover:bg-slate-200 transition-colors"
          >
            <svg
              width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
              className={`transition-transform ${open ? 'rotate-90' : ''}`}
            >
              <polyline points="9 18 15 12 9 6" />
            </svg>
          </button>
        )}
        {header}
      </div>
      {!shut && <div className="flex-1 min-h-0 overflow-auto">{children}</div>}
    </section>
  );
}

function PaneTitle({ kind, count }: { kind: 'input' | 'output'; count?: number | null }) {
  return (
    <>
      <span className="text-[10px] font-bold uppercase tracking-wider text-slate-500 shrink-0">
        {kind === 'input' ? 'Input' : 'Output'}
      </span>
      {count != null && (
        <span className="text-[11px] font-semibold text-slate-600 tabular-nums shrink-0">
          {count.toLocaleString()} {count === 1 ? 'row' : 'rows'}
        </span>
      )}
    </>
  );
}

function Empty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="h-full flex flex-col items-center justify-center gap-1 px-4 text-center">
      <div className="text-xs text-slate-600">{title}</div>
      {hint && <div className="text-[11px] text-slate-500">{hint}</div>}
    </div>
  );
}

function ErrorBlock({ message }: { message: string }) {
  return (
    <div className="m-3 rounded-lg border border-red-200 bg-red-50 p-3">
      <div className="text-[10px] font-bold uppercase tracking-wider text-red-600 mb-1">Failed</div>
      <div className="text-xs text-red-800 break-words font-mono leading-relaxed">{message}</div>
    </div>
  );
}

/** Column-name chips, for a node that has a known schema but has not run. */
function ColumnList({ columns }: { columns: string[] }) {
  return (
    <div className="p-3">
      <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500 mb-1.5">
        {columns.length} column{columns.length === 1 ? '' : 's'} · not run yet
      </div>
      <div className="flex flex-wrap gap-1">
        {columns.map((c) => (
          <span
            key={c}
            className="text-[11px] font-mono px-1.5 py-0.5 rounded border border-slate-200 bg-slate-50 text-slate-700"
          >
            {c}
          </span>
        ))}
      </div>
    </div>
  );
}

/**
 * Left column. When the node has several upstreams (a Join), the selector
 * picks which one to show — the pane deliberately shows ONE input at a
 * time rather than splitting an already-narrow column in two.
 */
export function InputPane({
  sources,
  activeId,
  onActiveChange,
  stepResults,
  fallbackColumns,
  collapsible,
  open,
  onToggle,
}: {
  sources: PaneNode[];
  activeId: string | null;
  onActiveChange: (id: string) => void;
  stepResults: Record<string, StepResult>;
  fallbackColumns: string[];
  collapsible?: boolean;
  open?: boolean;
  onToggle?: () => void;
}) {
  const active = sources.find((s) => s.id === activeId) ?? sources[0] ?? null;
  const result = active ? stepResults[active.id] : undefined;

  let body: React.ReactNode;
  if (sources.length === 0) {
    body = (
      <Empty
        title="No input"
        hint="This node reads from outside the pipeline, or nothing is connected to it yet."
      />
    );
  } else if (result?.status === 'error') {
    body = <ErrorBlock message={result.error || 'The upstream step failed.'} />;
  } else if (result?.status === 'success') {
    body = <DataTable result={result} />;
  } else if (fallbackColumns.length > 0) {
    body = <ColumnList columns={fallbackColumns} />;
  } else {
    body = (
      <Empty
        title={`${active?.label ?? 'The upstream node'} has not run yet.`}
        hint="Run this node to see the rows coming in."
      />
    );
  }

  return (
    <PaneShell
      kind="input"
      collapsible={collapsible}
      open={open}
      onToggle={onToggle}
      header={
        <>
          <PaneTitle kind="input" count={result?.status === 'success' ? result.row_count : null} />
          <div className="flex-1" />
          {sources.length > 1 ? (
            <select
              value={active?.id ?? ''}
              onChange={(e) => onActiveChange(e.target.value)}
              aria-label="Which input to show"
              className="text-[11px] font-semibold text-slate-700 bg-white border border-slate-200 rounded-md px-1.5 py-0.5 max-w-[150px] focus:outline-none focus:ring-2 focus:ring-blue-300"
            >
              {sources.map((s) => (
                <option key={s.id} value={s.id}>{s.label}</option>
              ))}
            </select>
          ) : active ? (
            <span className="flex items-center gap-1.5 min-w-0" title={active.label}>
              <ModuleIcon type={active.stepType} size={16} />
              <span className="text-[11px] font-semibold text-slate-700 truncate max-w-[130px]">
                {active.label}
              </span>
            </span>
          ) : null}
        </>
      }
    >
      {body}
    </PaneShell>
  );
}

type InspectView = 'output' | 'schema' | 'json' | 'input';

const INSPECT_VIEWS: Array<{ key: InspectView; label: string; hint: string }> = [
  { key: 'output', label: 'Output', hint: 'Rows this node produced' },
  { key: 'schema', label: 'Schema', hint: 'Output columns, types and nullability' },
  { key: 'json', label: 'JSON', hint: 'The first rows as raw JSON' },
  { key: 'input', label: 'Input', hint: 'The rows that went in — same as the left pane' },
];

/**
 * Right column: an inspector over the node, not just its output.
 *
 * The left pane is a fixed reference — what came in. This side is the one
 * you point at whatever you need to see: the rows out (the default),
 * their schema, the raw JSON, or the input again, which is how you
 * compare two sides of a Join without losing the left pane's selection.
 * These are the same four views the bottom strip had as tabs; they were
 * never the problem, their being hidden behind the config card was.
 */
export function InspectPane({
  result,
  inputResult,
  inputColumns,
  isRunning,
  actions,
  showInputView = true,
  collapsible,
  open,
  onToggle,
}: {
  result: StepResult | undefined;
  inputResult: StepResult | undefined;
  inputColumns: string[];
  isRunning: boolean;
  actions?: React.ReactNode;
  /** False when the Input COLUMN is already on screen beside this pane —
   *  offering "Input" here too put the word on screen twice for no gain.
   *  True in the narrow layout, where this pane is the only way to see
   *  the input at all. */
  showInputView?: boolean;
  collapsible?: boolean;
  open?: boolean;
  onToggle?: () => void;
}) {
  const [view, setView] = useState<InspectView>('output');
  const views = showInputView ? INSPECT_VIEWS : INSPECT_VIEWS.filter((v) => v.key !== 'input');

  const added = useMemo(() => {
    if (result?.status !== 'success' || inputColumns.length === 0) return undefined;
    const before = new Set(inputColumns);
    const diff = new Set(result.columns.filter((c) => !before.has(c)));
    return diff.size > 0 ? diff : undefined;
  }, [result, inputColumns]);

  // Every view except `input` is about this node's own result.
  const subject = view === 'input' ? inputResult : result;

  let body: React.ReactNode;
  if (view !== 'input' && isRunning) {
    body = <Empty title="Running…" />;
  } else if (subject?.status === 'error') {
    body = (
      <ErrorBlock
        message={subject.error || (view === 'input' ? 'The upstream step failed.' : 'This step failed.')}
      />
    );
  } else if (subject?.status === 'success') {
    body =
      view === 'schema' ? <SchemaView result={subject} />
      : view === 'json' ? <JsonView result={subject} />
      : <DataTable result={subject} highlightColumns={view === 'output' ? added : undefined} />;
  } else {
    body = (
      <Empty
        title="Not run yet"
        hint={view === 'input'
          ? 'Run the upstream node to see the rows coming in.'
          : 'Run this node to see what it produces.'}
      />
    );
  }

  return (
    <PaneShell
      kind="output"
      collapsible={collapsible}
      open={open}
      onToggle={onToggle}
      header={
        <>
          <div className="flex items-center rounded-md border border-slate-200 bg-white overflow-hidden shrink-0">
            {views.map((v) => (
              <button
                key={v.key}
                onClick={() => setView(v.key)}
                title={v.hint}
                aria-pressed={view === v.key}
                className={`px-2 py-1 text-[10px] font-bold uppercase tracking-wider transition-colors border-r border-slate-200 last:border-r-0 ${
                  view === v.key
                    ? 'bg-blue-50 text-blue-700'
                    : 'text-slate-600 hover:bg-slate-50'
                }`}
              >
                {v.label}
              </button>
            ))}
          </div>
          {subject?.status === 'success' && (
            <span className="text-[11px] font-semibold text-slate-600 tabular-nums shrink-0">
              {subject.row_count.toLocaleString()} {subject.row_count === 1 ? 'row' : 'rows'}
              {view !== 'input' && ` · ${subject.duration_ms} ms`}
            </span>
          )}
          <div className="flex-1" />
          {actions}
        </>
      }
    >
      {body}
    </PaneShell>
  );
}
