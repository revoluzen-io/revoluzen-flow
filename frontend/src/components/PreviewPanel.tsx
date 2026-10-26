import { useEffect, useMemo, useState } from 'react';
import {
  useWorkflowStore,
  type StepResult,
  type WranglerPreviewEntry,
} from '../stores/workflowStore';
import ResizeHandle from './shared/ResizeHandle';
import { DataTable, SchemaView, JsonView } from './shared/ResultTable';
import { humanizeExecutionError } from '../utils/errorMessages';

// Z8 — bounds for the drag-to-resize bottom panel. Tighter than the
// Storage drawer's [220, 85vh] because here the canvas above the panel
// needs to stay usable; capping at 70vh leaves at least ~30vh of canvas
// even on a small viewport.
const PREVIEW_MIN_PX = 180;
const PREVIEW_MAX_FRACTION = 0.7;
const PREVIEW_HEIGHT_KEY = 'revoluzen_flow_editor_preview_height';

function loadStoredPreviewHeight(): number {
  try {
    const raw = localStorage.getItem(PREVIEW_HEIGHT_KEY);
    const parsed = raw ? parseInt(raw, 10) : NaN;
    if (Number.isFinite(parsed) && parsed >= PREVIEW_MIN_PX) {
      return Math.min(parsed, 1200);
    }
  } catch {
    // localStorage disabled / private mode — fall through to default
  }
  return 240; // matches the legacy `h-60` height so first-load is unchanged
}

type Tab = 'input' | 'output' | 'schema' | 'json';

function FriendlyErrorBlock({ error }: { error: unknown }) {
  const friendly = humanizeExecutionError(error);
  return (
    <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-red-800">
      <div className="text-xs font-bold text-red-600 uppercase tracking-wider mb-1">Error</div>
      <div className="text-sm font-bold text-red-900">{friendly.title}</div>
      <div className="text-xs mt-0.5">{friendly.message}</div>
      {friendly.actions.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-xs list-disc pl-4">
          {friendly.actions.map((action) => <li key={action}>{action}</li>)}
        </ul>
      )}
      {friendly.technical && friendly.technical !== friendly.message && (
        <details className="mt-2">
          <summary className="text-[11px] font-semibold text-red-700 cursor-pointer hover:text-red-900">
            Show technical details
          </summary>
          <div className="mt-1 text-[11px] text-red-700 font-mono whitespace-pre-wrap break-words bg-white/60 border border-red-100 rounded-md p-2 max-h-32 overflow-auto">
            {friendly.technical}
          </div>
        </details>
      )}
    </div>
  );
}

/** Synthesize a StepResult from a wrangler sub-step so the existing
 *  OutputView / SchemaView / JsonView code paths can render it without
 *  branching on node type. The synthetic result is read-only — duration
 *  is reported as 0ms because preview is not a real execution. */
function wranglerStepToResult(
  nodeId: string,
  entry: WranglerPreviewEntry,
): StepResult | null {
  const step = entry.steps[entry.selectedStepIndex] ?? entry.steps[entry.steps.length - 1];
  if (!step) return null;
  return {
    step_id: nodeId,
    status: 'success',
    row_count: step.row_count,
    columns: step.columns.map((c) => c.name),
    sample_data: (step.sample_data || []) as Record<string, any>[],
    schema_info: step.columns.map((c) => ({
      name: c.name,
      type: c.type,
      nullable: true,
    })),
    error: null,
    duration_ms: 0,
  };
}

/** Walk backward through edges to find ALL ancestor nodes (not just direct parent) */
function findAllAncestors(
  nodeId: string,
  edges: Array<{ source: string; target: string }>,
  nodes: Array<any>,
): Array<{ id: string; label: string; stepType: string; depth: number }> {
  const visited = new Set<string>();
  const ancestors: Array<{ id: string; label: string; stepType: string; depth: number }> = [];

  function walk(currentId: string, depth: number) {
    const incoming = edges.filter((e) => e.target === currentId);
    for (const edge of incoming) {
      if (visited.has(edge.source)) continue;
      visited.add(edge.source);
      const n = nodes.find((nd: any) => nd.id === edge.source);
      if (n) {
        ancestors.push({
          id: n.id,
          label: n.data?.label || n.data?.stepType || n.id,
          stepType: n.data?.stepType || '',
          depth,
        });
        walk(edge.source, depth + 1);
      }
    }
  }

  walk(nodeId, 1);
  return ancestors;
}

export default function PreviewPanel() {
  const { selectedNodeId, stepResults, nodes, edges } = useWorkflowStore();
  const wranglerPreview = useWorkflowStore((s) => s.wranglerPreview);
  const setWranglerSelectedStep = useWorkflowStore((s) => s.setWranglerSelectedStep);
  const [tab, setTab] = useState<Tab>('output');
  const [selectedInputNodeId, setSelectedInputNodeId] = useState<string | null>(null);

  // 2026-06-04 — gate panel visibility on the same double-click /
  // "Open Settings" event ConfigPanel listens to. Previously this
  // panel mounted whenever a node was selected (single-click), so
  // selecting a node to inspect it also opened the bottom Preview
  // panel — which is the user's "focus mode" surface, not a
  // selection sidebar. Single-click now selects the node visually
  // and leaves the canvas layout alone; double-click (or context
  // menu "Open Settings" / "Fix configuration") enters focus mode
  // — ConfigPanel opens, side panels collapse, AND this Preview
  // panel reveals together. Background click clears selection and
  // closes the panel.
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const handler = (e: Event) => {
      const id = (e as CustomEvent).detail?.id as string | undefined;
      if (id) setOpen(true);
    };
    window.addEventListener('revoluzen-flow-node-opened', handler);
    return () => window.removeEventListener('revoluzen-flow-node-opened', handler);
  }, []);
  useEffect(() => {
    if (!selectedNodeId) setOpen(false);
  }, [selectedNodeId]);

  // Z8 / Z9 — drag-to-resize via the shared <ResizeHandle>. Panel owns
  // the height; the handle component handles mouse / touch / keyboard.
  // Persistence to localStorage keeps the user's preference across
  // reloads. Canvas above is flex-1 so it auto-shrinks to fit.
  const [previewHeight, setPreviewHeight] = useState<number>(loadStoredPreviewHeight);

  useEffect(() => {
    try {
      localStorage.setItem(PREVIEW_HEIGHT_KEY, String(previewHeight));
    } catch {
      // ignore — localStorage disabled
    }
    // Z12 — publish the live height as a CSS variable so overlays that
    // need to stop ABOVE the preview panel (ConfigPanel modal,
    // backdrop dim layer) can track the resize without prop-drilling
    // through the React tree.
    if (typeof document !== 'undefined') {
      document.documentElement.style.setProperty(
        '--revoluzen-flow-preview-panel-height',
        `${previewHeight}px`,
      );
    }
  }, [previewHeight]);

  // Reset the CSS variable when the panel unmounts (selected node
  // cleared / page swap) so layered overlays don't keep a phantom gap.
  useEffect(() => {
    return () => {
      if (typeof document !== 'undefined') {
        document.documentElement.style.removeProperty('--revoluzen-flow-preview-panel-height');
      }
    };
  }, []);

  // ALL hooks MUST be before any early returns (React Rules of Hooks)
  const allAncestors = useMemo(
    () => selectedNodeId ? findAllAncestors(selectedNodeId, edges, nodes) : [],
    [selectedNodeId, edges, nodes],
  );

  // Gate on BOTH selection AND explicit open (double-click event).
  // Single-click selects the node visually but never reveals this panel.
  if (!selectedNodeId || !open) return null;

  const node = nodes.find((n) => n.id === selectedNodeId);
  // Wrangler synthetic result — when the selected node is a Data Wrangler
  // with a live preview in the store, prefer the preview (which reflects
  // the user's unsaved edits) over any stale stepResults entry from a
  // previous Run All. The selectedStepIndex defaults to the last step.
  const isWrangler = node?.data?.stepType === 'data_wrangler';
  const wranglerEntry = isWrangler ? wranglerPreview[selectedNodeId] : undefined;
  const wranglerResult = wranglerEntry
    ? wranglerStepToResult(selectedNodeId, wranglerEntry)
    : null;
  const result = wranglerResult ?? stepResults[selectedNodeId];

  if (!node) return null;

  // Direct upstream (default selection)
  const upstreamEdges = edges.filter((e) => e.target === selectedNodeId);
  const defaultUpstreamId = upstreamEdges.length > 0 ? upstreamEdges[0].source : null;

  // Currently selected input node (default to direct upstream)
  const activeInputId = selectedInputNodeId || defaultUpstreamId;
  const upstreamResult = activeInputId ? stepResults[activeInputId] : null;
  const upstreamNode = activeInputId ? nodes.find((n) => n.id === activeInputId) : null;

  const isSource = node.data.category === 'source';

  return (
    <div
      className="relative bg-white border-t border-pipe-200 flex flex-col shrink-0 overflow-hidden"
      style={{ height: previewHeight }}
    >
      <ResizeHandle
        orientation="vertical"
        edge="top-edge"
        value={previewHeight}
        onResize={setPreviewHeight}
        min={PREVIEW_MIN_PX}
        max={() => Math.round(window.innerHeight * PREVIEW_MAX_FRACTION)}
        ariaLabel="Drag to resize preview panel"
        className="absolute top-0 left-0 right-0 -mt-1 z-20"
      />
      {/* Header */}
      <div className="flex flex-wrap items-center px-3 py-2 border-b border-amber-200/30 gap-2 shrink-0">
        <span className="text-sm font-semibold text-slate-700">{String((node.data as any)?.label ?? '')}</span>

        {/* Step ▾ selector — Data Wrangler nodes only. Lets the user inspect
            any sub-step's output via the existing OutputView/SchemaView/JsonView
            tabs without leaving the modal. */}
        {wranglerEntry && wranglerEntry.steps.length > 0 && (
          <select
            value={wranglerEntry.selectedStepIndex}
            onChange={(e) => setWranglerSelectedStep(selectedNodeId, Number(e.target.value))}
            className="text-xs font-semibold text-emerald-800 bg-white border border-emerald-200 rounded-md px-2 py-0.5 focus:outline-none focus:ring-2 focus:ring-emerald-300 cursor-pointer"
            title="Inspect output after a specific wrangler sub-step"
          >
            {wranglerEntry.steps.map((s, i) => (
              <option key={i} value={i}>
                Step {i + 1}: {s.label || s.op || 'step'}
              </option>
            ))}
          </select>
        )}

        {result && (
          <span
            className={`text-xs px-2 py-0.5 rounded-full font-medium ${
              result.status === 'success'
                ? 'bg-green-50 text-green-600'
                : result.status === 'error'
                ? 'bg-red-50 text-red-500'
                : 'bg-slate-100 text-slate-500'
            }`}
          >
            {result.status === 'success'
              ? wranglerEntry
                ? `${result.row_count.toLocaleString()} rows · ${result.columns.length} cols · sample of ${wranglerEntry.sample_rows}`
                : `${result.row_count.toLocaleString()} rows · ${result.columns.length} cols · ${result.duration_ms}ms`
              : result.status === 'error'
              ? 'Error'
              : result.status}
          </span>
        )}

        {/* Tabs: Input | Output | Schema | JSON */}
        <div className="flex flex-wrap gap-0.5">
          {(!isSource ? ['input', 'output', 'schema', 'json'] as Tab[] : ['output', 'schema', 'json'] as Tab[]).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-2.5 py-1 text-xs font-semibold rounded-md transition-colors uppercase tracking-wider ${
                tab === t
                  ? t === 'input' ? 'bg-blue-100 text-blue-700'
                  : t === 'output' ? 'bg-emerald-100 text-emerald-700'
                  : 'bg-pipe-100 text-pipe-700'
                  : 'text-slate-500 hover:text-slate-600 hover:bg-slate-50'
              }`}
            >
              {t === 'output' && result?.status === 'success' && (
                <span className="inline-block w-1.5 h-1.5 bg-emerald-500 rounded-full mr-1 -mt-px" />
              )}
              {t}
            </button>
          ))}
        </div>

        <div className="flex-1" />
      </div>

      {/* Content */}
      <div className="flex-1 overflow-auto">
        {tab === 'input' && !isSource ? (
          <InputView
            upstreamResult={upstreamResult}
            upstreamNode={upstreamNode}
            allAncestors={allAncestors}
            stepResults={stepResults}
            activeInputId={activeInputId}
            onSelectNode={setSelectedInputNodeId}
          />
        ) : tab === 'output' ? (
          !result ? (
            <div className="flex items-center justify-center h-full text-slate-500 text-xs">
              <div className="text-center">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" className="mx-auto mb-2 text-slate-300">
                  <polygon points="5 3 19 12 5 21 5 3" />
                </svg>
                Click <strong>"Test Node"</strong> or <strong>"Run All"</strong> to see output
              </div>
            </div>
          ) : result.status === 'error' ? (
            <div className="p-4">
              <FriendlyErrorBlock error={result.error} />
            </div>
          ) : (
            <OutputView result={result} />
          )
        ) : !result ? (
          <div className="flex items-center justify-center h-full text-slate-500 text-xs">
            Click "Test Node" or "Run All" to see output
          </div>
        ) : result.status === 'error' ? (
          <div className="p-4">
            <FriendlyErrorBlock error={result.error} />
          </div>
        ) : tab === 'schema' ? (
          <SchemaView result={result} />
        ) : tab === 'json' ? (
          <JsonView result={result} />
        ) : null}
      </div>
    </div>
  );
}

/* ── Input tab — browse ANY ancestor node's output ── */
function InputView({
  upstreamResult,
  upstreamNode,
  allAncestors,
  stepResults,
  activeInputId,
  onSelectNode,
}: {
  upstreamResult: StepResult | null;
  upstreamNode: any;
  allAncestors: Array<{ id: string; label: string; stepType: string; depth: number }>;
  stepResults: Record<string, StepResult>;
  activeInputId: string | null;
  onSelectNode: (id: string) => void;
}) {
  if (allAncestors.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-slate-500 text-xs">
        No upstream node connected. Connect a source or transform node.
      </div>
    );
  }

  const activeResult = activeInputId ? stepResults[activeInputId] : upstreamResult;
  const activeNode = upstreamNode;

  return (
    <div className="h-full flex flex-col">
      {/* Node selector bar — browse any previous node */}
      <div className="px-4 py-2 bg-blue-50/60 border-b border-blue-200/40 flex items-center gap-3 shrink-0">
        <div className="flex items-center gap-1.5">
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="text-blue-500">
            <path d="M5 12h14"/><path d="M12 5l7 7-7 7"/>
          </svg>
          <span className="text-[10px] font-bold text-blue-600 uppercase tracking-wider">Input from</span>
        </div>

        {/* Node selector dropdown */}
        <select
          value={activeInputId || ''}
          onChange={(e) => onSelectNode(e.target.value)}
          className="text-xs font-semibold text-blue-800 bg-white border border-blue-200 rounded-lg px-2 py-1 focus:outline-none focus:ring-2 focus:ring-blue-300 cursor-pointer min-w-[140px]"
        >
          {allAncestors.map((anc) => {
            const hasData = stepResults[anc.id]?.status === 'success';
            const rowCount = stepResults[anc.id]?.row_count;
            return (
              <option key={anc.id} value={anc.id}>
                {'  '.repeat(anc.depth - 1)}{anc.label}
                {hasData ? ` (${rowCount?.toLocaleString()} rows)` : ' (not executed)'}
              </option>
            );
          })}
        </select>

        <div className="flex-1" />

        {/* SQL reference hint — the picker stays (functional UI for
            choosing which ancestor's output to inspect), but the
            row/column count and column-name chips are dropped:
              - row/column count is already in the panel header pill
              - column names are visible in the DataTable thead, with
                coloured type chips making them more informative
                than a flat repeat would be. */}
        {activeInputId && (
          <div className="flex items-center gap-1 text-[10px] text-blue-400">
            <span>SQL:</span>
            <code className="bg-white px-1.5 py-0.5 rounded border border-blue-200 text-blue-600 font-mono font-semibold">
              source_table
            </code>
          </div>
        )}
      </div>

      {/* Data content */}
      {!activeResult ? (
        <div className="flex flex-col items-center justify-center flex-1 text-slate-500 text-xs gap-2">
          <div>Node <strong className="text-slate-500">{allAncestors.find(a => a.id === activeInputId)?.label || '...'}</strong> has not been executed yet.</div>
          <div>Click <strong>"Test Node"</strong> or <strong>"Run All"</strong> to run the pipeline.</div>
        </div>
      ) : activeResult.status === 'error' ? (
        <div className="p-4 text-red-500 text-xs font-mono">
          Error: {activeResult.error}
        </div>
      ) : (
        <div className="flex-1 overflow-auto">
          <DataTable result={activeResult} />
        </div>
      )}
    </div>
  );
}

/* ── Output tab — shows current node's output with summary header ── */
function OutputView({ result }: { result: StepResult }) {
  // Z7 (2026-05-23) — previously this view rendered a green status strip
  // with "● OUTPUT · 100 rows · 13 columns · 18.01ms · [col pills]". The
  // row/column/duration metrics moved to the panel header label and the
  // column-name pills were redundant with the table thead (which now
  // shows the same column names + coloured type chips). Strip removed.
  return (
    <div className="h-full flex flex-col">
      <div className="flex-1 overflow-auto">
        <DataTable result={result} />
      </div>
    </div>
  );
}

/* DataTable, SchemaView, TypeChip, isNumericType and JsonView moved to
   shared/ResultTable.tsx on 2026-10-24 so the node config's Input and
   Output panes draw rows with exactly this markup. Two grids drift; the
   point of showing input beside output is that they compare directly. */


